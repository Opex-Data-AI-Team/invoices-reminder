# Collections reminders

Weekday email digest to sales reps for unpaid or never-sent Zoho Books invoices, with collection stats and an escalating tone. Reminders start the day after an invoice is raised (early, before it is due) and repeat daily until it is paid, then escalate once overdue (coach, firm, blunt, decision). A Promised follow-up does not pause them. Zoho is read-only.

## Setup
1. Import `templates/*.csv` as tabs (Reps, Clients, Followups) of one Google Sheet, fill in real data. Do not use File > Share > Publish to web (the links were not reachable from GitHub Actions). Feed the tabs to the script through a CSV endpoint instead: see "Sheet CSV feeds" below.
2. Zoho API console (US): create a Self Client, scope `ZohoBooks.invoices.READ`, generate a refresh token.
3. Gmail: enable 2-Step Verification, create an app password.
4. Repo secrets: `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `REPS_SRC`, `CLIENTS_SRC`, `FOLLOWUPS_SRC` (the three CSV links, built as in "Sheet CSV feeds"). Optional for Phase 2: `LOG_SRC` (the `Log` tab, same way).
5. Repo variable `ZOHO_ORGS`, e.g. `Regtech=894826960,OPEX Consulting=790222018`. The org name must match the Clients tab. Add `SEND=1` only when ready to send for real.

## Sheet CSV feeds (`REPS_SRC`, `CLIENTS_SRC`, `FOLLOWUPS_SRC`, `LOG_SRC`)
`remind.py` fetches each of these with a plain request, so each secret is a link that returns CSV to an anonymous caller. We serve them from a small Apps Script web app instead of Publish to web:

    <web app /exec URL>?tab=<Tab name>&key=<key>

- One secret per tab: `tab=Reps`, `tab=Clients`, `tab=Followups`, `tab=Log`. Same URL and key, only `tab` changes.
- A new tab is a new secret. Test it first in a private browser window: it must show CSV (for Log, the header `ts,event_id,type,rep,org,invoice,action,detail`), not a login page. Then `gh secret set <NAME>` and paste the link.
- Only the tabs listed in `TABS` in `apps_script/SheetCsv.gs` can be read (Reps, Clients, Followups, Log). A new tab must be added there first, in the live script. The endpoint answers `forbidden` for a wrong or missing key and `no such tab` for anything else.
- The endpoint is `apps_script/SheetCsv.gs`, deployed as its own web app in the Sheet's Apps Script project (separate from `Code.gs`, so a different `/exec` URL). Script property `KEY` is the key; deploy it as execute-as-you, access Anyone. Treat the full link like a password.

## Run locally (dry run, prints messages)
```
REPS_SRC=templates/Reps.csv CLIENTS_SRC=templates/Clients.csv FOLLOWUPS_SRC=templates/Followups.csv \
INVOICES_FIXTURE=fixture.json FORCE=1 python remind.py
```
Tests: `python test_remind.py`

## Notes
- The repo is private. Actions minutes count against the org's free 2,000 a month; this job uses roughly 1 minute per weekday run.
- In CI the script never prints message bodies (only counts), so run local dry runs to review wording.
- Collection rate = value paid / value invoiced, for all issued, non-void invoices dated in the current fiscal year (starts month `FY_START_MONTH` in `remind.py`, default January). Team includes unassigned clients; reps do not. Drafts are **not** in the rate: they are unbilled pipeline, shown separately in the manager summary and stored in the `Opportunities` tab (never voided; they are candidates for win-back and upsell).
- Amounts are assumed to be NGN, with no currency conversion.
