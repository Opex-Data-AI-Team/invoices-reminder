# Collections reminders

Weekday email digest to sales reps for unpaid or never-sent Zoho Books invoices, with collection stats and an escalating tone. Reminders start the day after an invoice is raised (early, before it is due) and repeat daily until it is paid, then escalate once overdue (coach, firm, blunt, decision). A Promised follow-up does not pause them. Zoho is read-only.

## Setup
1. Import `templates/*.csv` as tabs (Reps, Clients, Followups) of one Google Sheet, fill in real data, then File > Share > Publish each tab as CSV.
2. Zoho API console (US): create a Self Client, scope `ZohoBooks.invoices.READ`, generate a refresh token.
3. Gmail: enable 2-Step Verification, create an app password.
4. Repo secrets: `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `REPS_SRC`, `CLIENTS_SRC`, `FOLLOWUPS_SRC` (the three CSV links).
5. Repo variable `ZOHO_ORGS`, e.g. `Regtech=894826960,OPEX Consulting=790222018`. The org name must match the Clients tab. Add `SEND=1` only when ready to send for real.

## Run locally (dry run, prints messages)
```
REPS_SRC=templates/Reps.csv CLIENTS_SRC=templates/Clients.csv FOLLOWUPS_SRC=templates/Followups.csv \
INVOICES_FIXTURE=fixture.json FORCE=1 python remind.py
```
Tests: `python test_remind.py`

## Notes
- The repo is private. Actions minutes count against the org's free 2,000 a month; this job uses roughly 1 minute per weekday run.
- In CI the script never prints message bodies (only counts), so run local dry runs to review wording.
- Collection rate = value paid / value invoiced, for all non-void invoices (drafts included) dated in the current fiscal year (starts month `FY_START_MONTH` in `remind.py`, default January). Team includes unassigned clients; reps do not.
- Amounts are assumed to be NGN, with no currency conversion.
