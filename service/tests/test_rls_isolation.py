"""Cross-tenant RLS isolation test (codifies BUILD_PLAN exit criterion 5.6).

Proves that the database itself refuses to return another org's rows when a
request is scoped to a user via user_conn(), independent of any WHERE clause
in application code. This is the invariant the whole multi-tenant model rests
on; it is run before Phase 2 document endpoints widen the cross-tenant surface,
and extended (test_documents_isolation) to cover documents specifically.

Run:  cd Site/service && python -m pytest tests/ -v
Skips automatically if DATABASE_URL is not set (e.g. CI without a DB).

The test seeds its own throwaway orgs/users/documents and tears them down,
so it does not depend on seed.sql and leaves no residue.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

# Allow `import db` when pytest is invoked from the repo root or elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

REQUIRES_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; RLS test needs a live Supabase Postgres",
)


async def _seed_org(conn, label: str) -> dict:
    suffix = uuid.uuid4().hex[:10]
    org = await conn.fetchrow(
        """
        INSERT INTO organizations (name, slug, status)
        VALUES ($1, $2, 'active')
        RETURNING id
        """,
        f"RLS Test {label} {suffix}", f"rls-test-{label.lower()}-{suffix}",
    )
    user = await conn.fetchrow(
        """
        INSERT INTO users (clerk_user_id, email, name)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        f"user_rlstest_{suffix}", f"rlstest+{suffix}@example.com",
        f"RLS Test User {label}",
    )
    await conn.execute(
        """
        INSERT INTO memberships (user_id, org_id, role, status)
        VALUES ($1, $2, 'client_admin', 'active')
        """,
        user["id"], org["id"],
    )
    doc = await conn.fetchrow(
        """
        INSERT INTO documents (org_id, category, name, storage_key)
        VALUES ($1, 'audit_report', $2, $3)
        RETURNING id
        """,
        org["id"], f"{label} report",
        f"{org['id']}/{uuid.uuid4()}/1",
    )
    return {"org_id": org["id"], "user_id": user["id"], "doc_id": doc["id"]}


async def _teardown(conn, *seeds) -> None:
    # documents block org delete via ON DELETE RESTRICT, so remove them first.
    for s in seeds:
        await conn.execute("DELETE FROM documents WHERE org_id = $1", s["org_id"])
        await conn.execute("DELETE FROM memberships WHERE org_id = $1", s["org_id"])
        await conn.execute("DELETE FROM users WHERE id = $1", s["user_id"])
        await conn.execute("DELETE FROM organizations WHERE id = $1", s["org_id"])


async def _run() -> None:
    await db.init_pool()
    try:
        async with db.admin_conn() as conn:
            a = await _seed_org(conn, "A")
            b = await _seed_org(conn, "B")

        try:
            # Acting as user A under RLS:
            async with db.user_conn(a["user_id"]) as conn:
                # Can see own org's document...
                own = await conn.fetch("SELECT id FROM documents")
                own_ids = {r["id"] for r in own}
                assert a["doc_id"] in own_ids, "user A cannot see own document"
                # ...and cannot see org B's document, even with no WHERE filter.
                assert b["doc_id"] not in own_ids, "RLS leaked org B document to user A"

                # Cannot see org B's organization row.
                orgs = await conn.fetch("SELECT id FROM organizations")
                org_ids = {r["id"] for r in orgs}
                assert a["org_id"] in org_ids, "user A cannot see own org"
                assert b["org_id"] not in org_ids, "RLS leaked org B org row to user A"

                # Cannot see org B's memberships.
                mems = await conn.fetch("SELECT org_id FROM memberships")
                mem_orgs = {r["org_id"] for r in mems}
                assert b["org_id"] not in mem_orgs, "RLS leaked org B membership to user A"

                # Targeted fetch of org B's document by id still returns nothing.
                leaked = await conn.fetchrow(
                    "SELECT id FROM documents WHERE id = $1", b["doc_id"]
                )
                assert leaked is None, "RLS allowed targeted read of org B document"
        finally:
            async with db.admin_conn() as conn:
                await _teardown(conn, a, b)
    finally:
        await db.close_pool()


@REQUIRES_DB
def test_cross_tenant_isolation():
    """orgs, memberships, and documents are invisible across tenants under RLS."""
    asyncio.run(_run())
