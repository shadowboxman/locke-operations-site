# Supabase: schema, migrations, seed

Database schema and seed data for the Locke Operations client portal.

## Structure

```
supabase/
  README.md                                    (this file)
  migrations/
    20260525120000_initial_schema.sql          (Phase 1 foundation)
  seed.sql                                     (fake orgs + users for dev)
```

## Setup (one-time, after Supabase project exists)

1. Install the Supabase CLI: `brew install supabase/tap/supabase`
2. From the repo root: `supabase login`
3. Link this directory to the project: `supabase link --project-ref <your-project-ref>` (run from `/Site/service/`)
4. Verify the link: `supabase status`

## Apply migrations to your Supabase project

```bash
cd Site/service
supabase db push          # applies all pending migrations from supabase/migrations/
supabase db seed          # runs supabase/seed.sql against the linked DB
```

Or for a clean slate during dev: `supabase db reset` (drops everything and re-applies all migrations + seed).

## Smoke test after applying

Open the Supabase SQL editor and run the queries at the bottom of `seed.sql`. They confirm:

1. Three orgs, three users, three memberships exist.
2. RLS scopes Alice (client_admin at Acme) to only Acme's data.
3. RLS lets Dan (locke_admin) see everything.
4. `audit_events` rejects UPDATE and DELETE.

## Role model

The backend uses two Postgres roles depending on the request type:

| Request type | DB role | RLS | Authz layer |
|---|---|---|---|
| User-facing read (`/api/me`, `/api/documents`) | `authenticated` | Enforced | RLS + app code |
| Admin write (`/api/admin/orgs`) | `service_role` | Bypassed | App code only |
| Webhooks (`/webhooks/clerk`) | `service_role` | Bypassed | Webhook signature |
| Migrations | `service_role` | Bypassed | n/a |

Per-request setup for user-facing reads:

```sql
SET LOCAL ROLE authenticated;
SET LOCAL app.current_user_id = '<users.id from Clerk lookup>';
```

The `LOCAL` keyword means both settings reset when the transaction ends, so connection pooling is safe.

## Adding a migration

```bash
cd Site/service
supabase migration new <descriptive_name>
# Edit the generated file under supabase/migrations/
supabase db push
```

Never edit a migration file after it's been applied to production. If you need to fix something, add a new migration.

## Things to know

- **`gen_random_uuid()`** is built into Postgres 13+. No `uuid-ossp` extension needed.
- **`citext`** (case-insensitive text) is used for email columns. The extension is created in the initial migration.
- **`audit_events` is append-only.** A trigger blocks UPDATE and DELETE. Even service_role hits the trigger.
- **Soft delete** on `documents` (via `deleted_at`). Hard delete is reserved for legal/retention purges and is done manually.
- **`is_internal`** on `organizations` flags the Locke internal org so we never accidentally treat it as a client.
