# Locke Assessment Service

FastAPI service that receives assessment form submissions, writes them to HubSpot, generates the personalized PDF report, and emails it via Resend.

Deployed to Railway. Static site (assessment.html) lives at the repo root and deploys to Vercel.

## Architecture

```
Browser (assessment.html on Vercel)
    │  POST /api/submit  (CORS)
    ▼
Railway service (this code, FastAPI in Docker)
    ├─→ HubSpot Forms API (SYNCHRONOUS — lead captured before we 200)
    ├─→ generate_pdf() via WeasyPrint (BACKGROUND)
    └─→ Resend API with PDF attached (BACKGROUND)
```

The HubSpot write is synchronous on purpose: if the background work later fails, the lead is still captured and Playbook 06 (manual PDF send) remains a working fallback.

## Local development

Requires Python 3.12 plus Pango/Cairo system libs (`brew install pango` on macOS).

```bash
cd service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
uvicorn main:app --reload --port 8000
```

Test the endpoint:

```bash
curl -s http://localhost:8000/api/health
curl -s -X POST http://localhost:8000/api/submit \
  -H 'Content-Type: application/json' \
  -d @../../Marketing/brand/templates/sample-lead.json   # contact + answers shape
```

To point the browser at your local service while iterating on the form, add this **before** the existing `<script>` block in `assessment.html`:

```html
<script>window.LOCKE_API_ENDPOINT = 'http://localhost:8000';</script>
```

## Deploy (Railway)

1. Push to `main` on GitHub. The repo's already connected; no Vercel/Railway config files conflict.
2. In Railway, create a new project → **Deploy from GitHub repo** → select `locke-operations-site` → set **Root Directory** to `service`. Railway autodetects the Dockerfile.
3. Add env vars from `.env.example` under the service's **Variables** tab.
4. After first deploy succeeds, **Settings → Networking → Generate Domain** for a `*.up.railway.app` URL. Test it:
   ```bash
   curl https://your-service.up.railway.app/api/health
   ```
5. **Settings → Networking → Custom Domain** → add `api.lockeoperations.com`. Railway gives you a CNAME target; add it at your DNS registrar.

## Env vars

See [.env.example](.env.example). All required vars must be set in Railway before first deploy or the container won't start.

## Files

- `main.py` — FastAPI app and `/api/submit` endpoint.
- `pdf_generator.py` — WeasyPrint PDF generation. Math + template substitution.
- `hubspot_client.py` — HubSpot Forms API submission.
- `email_client.py` — Resend send with PDF attachment.
- `assessment-result-template.html` — HTML template that becomes the PDF. **Source of truth** for the PDF design; copied here from `/Marketing/brand/templates/` for self-containment.
- `Dockerfile` — Production image; installs Pango/Cairo + DejaVu fonts.
- `railway.json` — Railway build + healthcheck config.

## When the scoring algorithm changes

The math lives in TWO places (intentionally — client-side for instant feedback, server-side for the PDF):

- `/assessment.html` → `calculate()` (JS)
- `/service/pdf_generator.py` → `calculate()` (Python)

These MUST stay in sync. The spec is `/Playbooks/05 - Assessment Scoring Algorithm.md`. If you change one, change the other and bump the version in this README.

Last sync: 2026-05-23 (IDC heuristic, 20% of time_savings).
