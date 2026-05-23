"""Pure-function PDF generator.

Ported from /Marketing/brand/templates/generate-assessment-pdf.py. The math and
template substitution logic is byte-for-byte identical; the only changes are:

  1. `generate_pdf(lead: dict) -> bytes` returns PDF bytes instead of writing
     a file (so the FastAPI handler can attach it to a Resend payload).
  2. The HTML template is read from this module's directory.

When you change the scoring algorithm, change it in ONE place:
/Playbooks/05 - Assessment Scoring Algorithm.md is the spec, and BOTH the
client-side JS in /assessment.html AND this file must be updated together.
"""

from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import qrcode
import qrcode.image.svg
from weasyprint import HTML

# ============================================================
# ASSESSMENT MATH — mirrors /assessment.html calculate()
# ============================================================

TASKS_BY_INDUSTRY: dict[str, list[dict[str, Any]]] = {
    "trades": [
        {"v": "lien_waivers",   "hours": 6, "diff": 2, "name": "Lien waiver tracking and filing"},
        {"v": "est_followup",   "hours": 3, "diff": 1, "name": "Estimate follow-up sequence"},
        {"v": "ap_intake",      "hours": 5, "diff": 2, "name": "Vendor bill intake with three-way matching"},
        {"v": "review_chase",   "hours": 3, "diff": 1, "name": "Review request and response workflow"},
        {"v": "ar_collections", "hours": 4, "diff": 2, "name": "Automated collections workflow"},
        {"v": "change_orders",  "hours": 4, "diff": 2, "name": "Change order workflow"},
        {"v": "coi_subs",       "hours": 3, "diff": 1, "name": "Subcontractor insurance tracking and renewal"},
        {"v": "aia",            "hours": 5, "diff": 2, "name": "Pay-application generation and reconciliation"},
        {"v": "cert_payroll",   "hours": 4, "diff": 2, "name": "Certified payroll automation"},
        {"v": "data_entry",     "hours": 4, "diff": 1, "name": "Cross-system data sync"},
    ],
    "restoration": [
        {"v": "supplements",      "hours": 6, "diff": 2, "name": "Supplement workflow"},
        {"v": "adjuster_cadence", "hours": 4, "diff": 2, "name": "Adjuster relationship cadence"},
        {"v": "adjuster_chase",   "hours": 5, "diff": 2, "name": "Adjuster communication workflow"},
        {"v": "post_job_review",  "hours": 2, "diff": 1, "name": "Post-job review workflow"},
        {"v": "photo_docs",       "hours": 3, "diff": 1, "name": "Photo intake, tagging, and routing"},
        {"v": "mortgage_holder",  "hours": 3, "diff": 2, "name": "Mortgage holder coordination automation"},
        {"v": "ap_intake_rest",   "hours": 3, "diff": 1, "name": "Vendor bill intake and equipment ledger"},
        {"v": "subro",            "hours": 3, "diff": 2, "name": "Subrogation package generation"},
        {"v": "job_profit",       "hours": 3, "diff": 2, "name": "Automated job profitability reporting"},
        {"v": "data_entry",       "hours": 4, "diff": 1, "name": "Cross-system data sync"},
    ],
    "hospitality": [
        {"v": "vendor_invoices", "hours": 5, "diff": 2, "name": "Vendor invoice processing and cost matching"},
        {"v": "review_response", "hours": 5, "diff": 2, "name": "Review response automation"},
        {"v": "ota_recon",       "hours": 5, "diff": 2, "name": "Booking-platform commission reconciliation"},
        {"v": "guest_reengage",  "hours": 4, "diff": 2, "name": "Guest re-engagement workflow"},
        {"v": "labor_sched",     "hours": 4, "diff": 2, "name": "Forecast-based labor scheduling"},
        {"v": "beo",             "hours": 4, "diff": 2, "name": "Event order workflow and quoting"},
        {"v": "tax_remit",       "hours": 3, "diff": 2, "name": "Tax remittance automation"},
        {"v": "tip_pool",        "hours": 3, "diff": 1, "name": "Tip pool reconciliation"},
        {"v": "folio_recon",     "hours": 3, "diff": 2, "name": "Folio and event billing reconciliation"},
        {"v": "data_entry",      "hours": 4, "diff": 1, "name": "Booking-to-accounting data sync"},
    ],
    "ae": [
        {"v": "wip",              "hours": 5, "diff": 2, "name": "Automated profitability reporting"},
        {"v": "rfp_packet",       "hours": 6, "diff": 2, "name": "Proposal packet assembly"},
        {"v": "timesheets",       "hours": 4, "diff": 1, "name": "Timesheet reconciliation automation"},
        {"v": "client_touch",     "hours": 3, "diff": 1, "name": "Client touchpoint workflow"},
        {"v": "rfis",             "hours": 4, "diff": 2, "name": "Field question and submittal automation"},
        {"v": "invoicing_ae",     "hours": 4, "diff": 2, "name": "Multi-phase invoicing automation"},
        {"v": "change_orders_ae", "hours": 4, "diff": 2, "name": "Change order tracking workflow"},
        {"v": "subconsultants",   "hours": 3, "diff": 2, "name": "Subconsultant coordination workflow"},
        {"v": "dcaa",             "hours": 3, "diff": 2, "name": "Federal contract compliance reporting"},
        {"v": "data_entry",       "hours": 4, "diff": 1, "name": "Cross-system data sync"},
    ],
    "other": [
        {"v": "cust_followup", "hours": 3, "diff": 1, "name": "Customer follow-up workflow"},
        {"v": "invoicing",     "hours": 3, "diff": 1, "name": "Invoicing and payment automation"},
        {"v": "lead_intake",   "hours": 3, "diff": 1, "name": "Lead intake automation"},
        {"v": "scheduling",    "hours": 3, "diff": 1, "name": "Scheduling automation"},
        {"v": "followup",      "hours": 4, "diff": 1, "name": "Reminder and confirmation automation"},
        {"v": "onboarding",    "hours": 4, "diff": 2, "name": "Client onboarding automation"},
        {"v": "reporting",     "hours": 3, "diff": 2, "name": "Reporting automation"},
        {"v": "compliance",    "hours": 3, "diff": 2, "name": "Compliance documentation workflow"},
        {"v": "recon",         "hours": 4, "diff": 2, "name": "Cross-system reconciliation"},
        {"v": "data_entry",    "hours": 4, "diff": 1, "name": "Cross-system data sync"},
    ],
}

RESPONSE_LIFTS = {"fast": 0, "hour": 0.005, "day": 0.015, "slow": 0.025}
INDUSTRY_DISPLAY = {
    "trades": "Trades", "restoration": "Restoration",
    "hospitality": "Hospitality", "ae": "A&E", "other": "Other",
}

INDUSTRY_EQUIVS = {
    "trades": [
        "A part-time admin coordinator's annual cost, fully loaded.",
        "A new service van every other year.",
    ],
    "restoration": [
        "A part-time claim coordinator's annual cost, fully loaded.",
        "Two more major claims fully closed per year.",
    ],
    "hospitality": [
        "A part-time booking coordinator's annual cost, fully loaded.",
        "Roughly a third of a full-time front-desk manager.",
    ],
    "ae": [
        "A part-time project coordinator's annual cost, fully loaded.",
        "About two months of a senior designer's billable capacity.",
    ],
    "other": [
        "A part-time admin coordinator's annual cost, fully loaded.",
        "About four months of a mid-level employee's loaded cost.",
    ],
}


def calculate(a: dict) -> dict:
    """Pure function over the answers dict. Mirror of assessment.html calculate()."""
    leads = a.get("leads", 0)
    if leads <= 5:
        volume_bonus = 3
    elif leads <= 25:
        volume_bonus = 8
    elif leads <= 100:
        volume_bonus = 12
    else:
        volume_bonus = 15

    readiness = a.get("maturity", 0) + a.get("consistency", 0) + volume_bonus
    capture = 0.55 if readiness >= 70 else 0.40 if readiness >= 40 else 0.25
    time_savings = a.get("hours", 0) * 52 * a.get("rate", 0) * capture
    lead_value = a.get("leads", 0) * 12 * a.get("value", 0) * RESPONSE_LIFTS.get(a.get("response", ""), 0)
    error_reduction = round(time_savings * 0.20)
    total = round(time_savings + lead_value + error_reduction)
    low = round(total * 0.7)
    high = round(total * 1.3)

    if readiness >= 70:
        band, band_label, band_short = "high", "Ready to automate", "ready."
    elif readiness >= 40:
        band, band_label, band_short = "mid", "High opportunity, light foundation work first", "mid-band."
    else:
        band, band_label, band_short = "low", "Big upside, processes need standardizing first", "low-band."

    industry = a.get("industry", "other")
    industry_tasks = TASKS_BY_INDUSTRY.get(industry, TASKS_BY_INDUSTRY["other"])
    selected = a.get("tasks") or []
    total_seg_hours = sum(
        next((t["hours"] for t in industry_tasks if t["v"] == tid), 0)
        for tid in selected
    )

    wins = []
    for tid in selected:
        meta = next((t for t in industry_tasks if t["v"] == tid), None)
        if not meta:
            continue
        if a.get("hours", 0) > 0 and total_seg_hours > 0:
            task_hours = min(meta["hours"], a["hours"] * (meta["hours"] / total_seg_hours))
        else:
            task_hours = meta["hours"]
        yearly_value = round(task_hours * 52 * a.get("rate", 0) * capture)
        diff = meta["diff"]
        if readiness < 40:
            diff += 1
        if readiness >= 70 and diff > 1:
            diff -= 1
        wins.append({
            "name": meta["name"],
            "hours": meta["hours"],
            "value": yearly_value,
            "diff": diff,
        })
    wins.sort(key=lambda w: w["value"], reverse=True)
    wins = wins[:3]

    return {
        "total": total, "low": low, "high": high,
        "timeSavings": round(time_savings),
        "leadValue": round(lead_value),
        "errorReduction": error_reduction,
        "readiness": readiness, "band": band,
        "bandLabel": band_label, "bandShort": band_short,
        "wins": wins, "capture": capture,
    }


def render_equivalents(answers: dict, result: dict, override: list[str] | None = None) -> str:
    if override:
        lines = override
    else:
        industry = answers.get("industry", "other")
        lines = list(INDUSTRY_EQUIVS.get(industry, INDUSTRY_EQUIVS["other"]))
        rate = answers.get("rate", 0)
        if rate > 0:
            hours = round(result["total"] / rate / 50) * 50
            lines.append(
                f"~{hours} hours of admin time you could redirect to billable work or growth."
            )
    items = "".join(
        f'<div class="equiv"><span class="approx">≈</span><span>{line}</span></div>\n    '
        for line in lines
    )
    return items.rstrip()


def render_wins(result: dict) -> str:
    wins = result["wins"]
    if not wins:
        return ""
    max_value = max(w["value"] for w in wins) or 1
    badge_class = {1: "easy", 2: "med", 3: "hard"}
    badge_label = {1: "Easy", 2: "Medium", 3: "Heavy lift"}
    parts = []
    for w in wins:
        diff = min(max(w["diff"], 1), 3)
        bar_pct = (w["value"] / max_value) * 100
        parts.append(f'''
    <div class="win">
      <div class="win-header">
        <div class="win-text">
          <div class="win-title-row">
            <span class="win-title">{w["name"]}</span>
            <span class="win-badge {badge_class[diff]}">{badge_label[diff]}</span>
          </div>
          <div class="win-meta">About {w["hours"]} hrs/week saved across your team</div>
        </div>
        <div class="win-value">${w["value"]:,}/yr</div>
      </div>
      <div class="win-bar"><div class="fill" style="width: {bar_pct:.1f}%"></div></div>
    </div>''')
    return "".join(parts).strip()


def render_holdback(answers: dict, result: dict) -> str:
    if result["band"] == "high":
        return ""
    gaps = []
    if answers.get("maturity", 0) < 35:
        gaps.append("your work mostly lives outside connected software")
    if answers.get("consistency", 0) < 22:
        gaps.append("your processes vary too much from job to job")
    if not gaps:
        gaps.append("volume is still building, so payback takes a bit longer")
    text = " and ".join(gaps)
    return f'''
  <div class="holdback">
    <h3>What's holding you back</h3>
    <p>Right now {text}. Fix that and the capture rate on these savings roughly doubles. Foundation work is usually faster than people think (two to six weeks).</p>
  </div>'''


def generate_qr_svg(url: str = "https://www.lockeoperations.com") -> str:
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(
        url, image_factory=factory, box_size=10, border=0,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    svg = svg.replace("#000000", "#1A2332").replace('fill="black"', 'fill="#1A2332"')
    svg = svg.replace("<svg ", '<svg style="background:#F4F1EA" ', 1)
    return svg


def fmt_money(n: int | float) -> str:
    return f"${n:,.0f}" if isinstance(n, float) else f"${n:,}"


def generate_pdf(lead: dict) -> bytes:
    """Generate a PDF from a lead dict matching sample-lead.json schema.

    Returns the PDF bytes (suitable for attaching to email).
    Does not write to disk.
    """
    contact = lead["contact"]
    answers = lead["answers"]
    report_date = lead.get("date") or date.today().isoformat()
    equivalents_override = lead.get("equivalents")

    result = calculate(answers)

    if result["total"] > 0:
        seg1_pct = result["timeSavings"] / result["total"] * 100
        seg2_pct = result["leadValue"] / result["total"] * 100
        seg3_pct = result["errorReduction"] / result["total"] * 100
    else:
        seg1_pct = seg2_pct = seg3_pct = 0

    subs = {
        "contact_caps": f"{contact['first_name'].upper()} {contact['last_name'].upper()}",
        "company_caps": contact["company"].upper(),
        "date": report_date,
        "hero_low": fmt_money(result["low"]),
        "hero_high": fmt_money(result["high"]),
        "hours": str(answers.get("hours", 0)),
        "rate": str(answers.get("rate", 0)),
        "midpoint": fmt_money(result["total"]),
        "equivalents_html": render_equivalents(answers, result, equivalents_override),
        "seg1_value": fmt_money(result["timeSavings"]),
        "seg2_value": fmt_money(result["leadValue"]),
        "seg3_value": fmt_money(result["errorReduction"]),
        "seg1_pct": f"{seg1_pct:.1f}",
        "seg2_pct": f"{seg2_pct:.1f}",
        "seg3_pct": f"{seg3_pct:.1f}",
        "seg1_pct_label": str(round(seg1_pct)),
        "seg2_pct_label": str(round(seg2_pct)),
        "seg3_pct_label": str(round(seg3_pct)),
        "readiness_score": str(result["readiness"]),
        "band_short": result["bandShort"],
        "readiness_label": result["bandLabel"],
        "holdback_html": render_holdback(answers, result),
        "wins_html": render_wins(result),
    }

    template_path = Path(__file__).parent / "assessment-result-template.html"
    html = template_path.read_text()
    for k, v in subs.items():
        html = html.replace("{{" + k + "}}", str(v))

    qr_svg = generate_qr_svg()
    html = html.replace("<!-- QR_PLACEHOLDER -->", qr_svg)

    buf = io.BytesIO()
    HTML(string=html, base_url=str(template_path.parent)).write_pdf(buf)
    return buf.getvalue()


def pdf_filename(lead: dict) -> str:
    """Canonical filename used in both the email attachment and any local debugging."""
    company = lead["contact"]["company"]
    report_date = lead.get("date") or date.today().isoformat()
    return f"Assessment - {company} - {report_date}.pdf"


# CLI fallback for local debugging — keeps parity with the original script.
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("Usage: python pdf_generator.py path/to/lead.json")
    lead = json.loads(Path(sys.argv[1]).read_text())
    pdf_bytes = generate_pdf(lead)
    out = Path.cwd() / pdf_filename(lead)
    out.write_bytes(pdf_bytes)
    print(f"Wrote: {out}")
