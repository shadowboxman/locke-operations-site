# HubSpot contact form — setup checklist

One-time setup so the homepage contact form (`/api/contact`) captures leads in
HubSpot in addition to emailing hello@. Until `HUBSPOT_CONTACT_FORM_ID` is set
in Railway, the form still works and emails you; HubSpot capture is skipped
cleanly (look for `contact.hubspot_skipped reason=not_configured` in the logs).

## 1. Create the form in HubSpot

- [ ] In HubSpot, go to **Marketing > Forms**.
- [ ] Click **Create form**, choose **Embedded form**, then a **Blank** template.
- [ ] Name it something obvious, e.g. `Website — Contact`. This name is internal.

## 2. Add exactly these fields

The backend posts these HubSpot internal field names. They must exist on the
form or the submission is rejected. All are standard contact properties.

- [ ] `email` (Email) — set as the required, primary field
- [ ] `firstname` (First name)
- [ ] `lastname` (Last name)
- [ ] `company` (Company name)
- [ ] `message` (Message) — multi-line text

Notes:
- If `message` isn't offered, it's a default contact property; add it via the
  field search in the form editor. If your portal somehow lacks it, create a
  single-line or multi-line text property named `message` first
  (Settings > Properties > Contact properties).
- Leave HubSpot's own field validation relaxed; our backend already validates.

## 3. Turn off / accept HubSpot options that don't apply to API submits

- [ ] You do NOT need to embed the form anywhere. We submit via the Forms API,
      not HubSpot's embed script.
- [ ] **Turn OFF the form's submission notification email** (form editor >
      Options/Settings > Notifications > clear recipients). Our platform sends
      the notification to hello@ (reply-to the submitter); leaving HubSpot's on
      too means two emails per submission. HubSpot stays on only for the
      silent CRM capture.
- [ ] HubSpot's built-in reCAPTCHA option only protects HubSpot-hosted/embedded
      forms. It does NOT cover our API submission path, so it has no effect
      here either way (spam protection for our path is handled separately — see
      the spam note below).
- [ ] Set the follow-up/notification options as you like (e.g. notify yourself
      in HubSpot). These are independent of our hello@ email.
- [ ] **Publish** the form.

## 4. Grab the two IDs

- [ ] **Portal ID** (a.k.a. Hub ID / account ID): top-right account menu, or in
      any form's embed code as `portalId`. You likely already have this set as
      `HUBSPOT_PORTAL_ID` (the assessment uses it).
- [ ] **Form GUID**: open the form in the editor and copy the long
      `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` value from the page URL, or click
      **Embed** and copy the `formId` value from the embed snippet.

## 5. Wire it into Railway

- [ ] In the Railway service, add an environment variable:
      `HUBSPOT_CONTACT_FORM_ID` = the form GUID from step 4.
- [ ] (Optional) `CONTACT_NOTIFY_EMAIL` if the notification email should go
      somewhere other than hello@lockeoperations.com.
- [ ] Redeploy (or let the variable change trigger a redeploy).

## 6. Verify

- [ ] Submit the form on the homepage with a test entry.
- [ ] Confirm the email arrives at hello@ (reply-to is the test address).
- [ ] Confirm a contact/submission appears in HubSpot under the new form.
- [ ] Check Railway logs: you want `hubspot.contact ok`, not
      `contact.hubspot_skipped` (means the env var didn't load) or
      `contact.hubspot_failed` (means the form fields don't match — re-check
      step 2).

## Rollback

- Removing `HUBSPOT_CONTACT_FORM_ID` reverts to email-only with no errors.
- Nothing about this touches the assessment form or its `HUBSPOT_FORM_ID`.

## ⚠️ Production cutover (DO THIS BEFORE LAUNCH)

As of 2026-06-01 the Railway service points at the **HubSpot developer/test
account, portal `246275387`**. Both the assessment form (`HUBSPOT_FORM_ID`)
and the contact form (`HUBSPOT_CONTACT_FORM_ID`) live there. **All assessment
and contact leads are currently landing in the dev account, not production.**

Production is the **`246274191`** account (region **`na2`** — note the
`js-na2.hsforms.net` embed host). When cutting over to production:

- [ ] Recreate BOTH forms in `246274191`: the assessment form (with all its
      `assessment_*` fields) and the `Website - Contact` form (five fields:
      `email`, `firstname`, `lastname`, `company`, `message`).
- [ ] On the new contact form, turn OFF its submission notification email
      (the platform sends the notification; HubSpot's would be a duplicate).
- [ ] Flip all three Railway vars together:
      `HUBSPOT_PORTAL_ID` → `246274191`,
      `HUBSPOT_FORM_ID` → production assessment form GUID,
      `HUBSPOT_CONTACT_FORM_ID` → production contact form GUID.
- [ ] Region check: the code submits to `api.hsforms.com`, which works for the
      dev account. `246274191` is `na2`; confirm the submission endpoint host
      is correct for that region. If submissions 404 / region-error after the
      cutover, that's the cause — point `hubspot_client.py` at the region host
      (consider adding a `HUBSPOT_REGION` env var so it's config, not code).
- [ ] Re-test both the assessment and the contact form; confirm leads land in
      `246274191` (`hubspot.submit ok` / `hubspot.contact ok` in the logs).
- [ ] Decide whether any real leads captured in the dev account during build
      need exporting/importing into production.
