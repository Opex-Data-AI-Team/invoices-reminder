# Go-live checklist

## 1. Config (GitHub → Settings)
- [ ] Secrets set: `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `REPS_SRC`, `CLIENTS_SRC`, `FOLLOWUPS_SRC`
- [ ] Response tracking secrets set: `APPS_SCRIPT_URL`, `LINK_SECRET` (see `apps_script/README.md`; same `LINK_SECRET` in Apps Script)
- [ ] Variable `ZOHO_ORGS` set, and the org names match the Clients tab
- [ ] Variable `LINK_PAGE_URL` = the deployed `page/` (static tap page, e.g. on Vercel). Without it, links open Apps Script directly, which breaks on phones signed into several Google accounts (Google rewrites to `/macros/u/N/`)
- [ ] Optional variable `DAILY_SEND_CAP` (default 400)
- [ ] Variable `SEND` is unset

## 2. Data alignment
- [ ] Every `Rep` name on the Reps tab (Role = rep) matches the Zoho salesperson name, case aside
- [ ] Every rep and manager has a valid `Email`, and each rep's `Manager` matches a manager's `Rep` name
- [ ] The Clients tab covers your main paying clients (it's the fallback when Zoho has no salesperson). Regtech invoices have no salesperson set in Zoho, so this tab decides who gets each one.
- [ ] Reps tab has each rep's `WhatsApp` number (used later for the WhatsApp buttons)
- [ ] Followups tab headers are exactly `Organization, Invoice, Rep note, Promised pay date, Status`
- [ ] Finance knows: stale drafts (30+ days) are never voided. They are dormant opportunities, listed to management every Monday and kept in the `Opportunities` tab.

## 3. Dry run (Actions → remind → Run workflow, `send=0`)
- [ ] The unit tests step is green
- [ ] Log shows one `[DRY RUN]` line per rep who should hear from you, and no expected rep is missing
- [ ] `cc` appears only on escalations
- [ ] The manager's unassigned section is empty or understood. Fix name mismatches and rerun until it is.

## 4. Content check (local)
- [ ] Export the secrets locally, run `python remind.py` without `SEND`, and open `preview/*.html`
- [ ] Amounts, client names, tiers and collection rates look right
- [ ] Rep email: open invoices show **Log update**; recent drafts sit under "Waiting on billing" with **Client is waiting**; no stale draft appears
- [ ] Invoice numbers link to the Zoho client page (`zohosecurepay.com/...`) for issued invoices; drafts have no link
- [ ] Manager email: "Unbilled pipeline" line, drafts for finance, and on Mondays the "Dormant opportunities" list. No `?t=` tokens in the manager email.

## 5. Safe live test (email)
- [ ] Point `REPS_SRC` at a copy of the Reps sheet with every Email set to yours
- [ ] Run with `send=1`
- [ ] Confirm the mail arrives, renders in Gmail, and the plain-text fallback is fine
- [ ] Restore the real `REPS_SRC`

## 6. Response tracking: live functional tests (Phase 1)
Use a **test copy of the Google Sheet** (Reps, Clients, Followups) with the Apps Script deployed on that copy, and every rep email set to yours. Do **not** run `installTriggers` yet (tests 20 only). Test on a **phone**: reps will tap there.

### 6a. Setup
- [ ] Apps Script pasted from `apps_script/Code.gs`; script properties `LINK_SECRET` and `ALERT_EMAIL` (yours) set
- [ ] Deployed as Web app: execute as **Me**, access **Anyone**; URL ends in `/exec`
- [ ] `APPS_SCRIPT_URL` and `LINK_SECRET` set in GitHub, pointing at the test deployment

### 6b. Links and logging
| # | Do this | Expected |
|---|---|---|
| 1 | Open the web app URL with no token, then `?t=garbage` | "Link invalid or expired". No data changes. |
| 2 | Run workflow with `send=0` (secrets set) | Passes. Nothing sent, nothing logged. |
| 3 | Run with `send=1` (all rep emails go to you) | Emails arrive. `Log` tab appears with one `sent` row per rep and one `run` row. |
| 4 | Re-run `send=1` the same day | No duplicate rows in `Log`. |
| 5 | On your phone, tap **Log update** on an overdue invoice | A page opens with the invoice number. **Nothing is saved yet**; Followups is unchanged. |
| 6 | Tap **Chasing** | "Saved: Chasing." Followups row with Status = Chasing; a `tap` row in `Log`. |
| 7 | Open the link again, pick tomorrow's date, tap **Save promise** | Status = Promised; date stored as plain text like `2026-10-14` (not converted to a date). |
| 8 | Try a past date, then a date 90 days out | Both rejected with a message. |
| 9 | Tap **Paid**, then **Disputed** on the same invoice | Followups shows the latest only. `Log` shows both taps. |
| 10 | Tap **Pause**, then confirm (needs a second tap) | Followups Status = `Dead`. After the next run the invoice no longer appears in emails. |
| 11 | Change one character of the token in a link | "Link invalid or expired". |
| 12 | Two people (or two browser tabs) tap different buttons at the same moment, 3 or 4 times | No lost rows, no garbled cells. |
| 13 | Paste a link into WhatsApp and send it to yourself | The link preview alone changes no data. |
| 13a | Open a link you already used | Page shows `Currently: <last status>`. After a tap the buttons stay, and tapping another one overwrites Followups (`Log` keeps both). |
| 13b | After test 10 (Pause), run `send=1` | The invoice is listed under **Paused** with a **Resume** button. Tapping it sets Status = `Chasing` and the invoice returns to **Needs action** on the next run. |

### 6c. Drafts and opportunities
| # | Do this | Expected |
|---|---|---|
| 14 | Open a draft's **Client is waiting** link and tap | Followups Status = `Client waiting`. Next run shows "CLIENT WAITING" on it and no second button. |
| 15 | (Optional) Replay a draft token with a non-draft action | Rejected. Covered by code review only; skip if awkward. |
| 16 | After a `send=1` run, open the `Opportunities` tab | One row per draft with First seen / Last seen. A second run keeps one row per draft and updates Last seen only. |
| 17 | Next morning, compare the email after tests 6 or 7 | **Expected: reminders unchanged.** Taps are recorded but do not change reminders yet (Phase 2). |

### 6d. Failure drills
| # | Do this | Expected |
|---|---|---|
| 18 | Set `APPS_SCRIPT_URL` to a wrong address and run `send=1` | Emails still send. Log shows "WARNING: could not write run log". Restore the URL. |
| 19 | Set `DAILY_SEND_CAP` to 1 and run `send=1` | Run stops with "Refusing to send ...". No emails go out. Clear the variable. |
| 20 | Run `installTriggers`, skip one workday's run, wait for 10:30 WAT | You get "ALERT: collections reminder did not run today". |
| 21 | (Optional) Break `ZOHO_REFRESH_TOKEN` and run | Run fails and GitHub emails you. No `run` row is logged. Restore the token. |

### 6e. Pass criteria
- [ ] Tests 1 to 14, 16, and 18 to 20 pass. Tests 15, 17 and 21 are informational or optional.
- [ ] Any email that looks wrong on a phone is noted and fixed
- [ ] `Log` and `Opportunities` contents match what you expected
- [ ] Production Sheet gets its own Apps Script deployment, with new `APPS_SCRIPT_URL` and `LINK_SECRET`; the `Opportunities` tab has restricted edit access

## 7. Go live
- [ ] Warn the reps and manager that daily emails start, including early reminders before the due date, and explain the **Log update** buttons
- [ ] Run once manually with `send=1`
- [ ] Confirm no bounces and no Gmail send errors
- [ ] Run `installTriggers` in Apps Script (turns on the 10:30 "did it run?" alert)
- [ ] Set variable `SEND=1`

## 8. After go-live (first week)
- [ ] Check the 08:00 UTC (09:00 WAT) weekday run is green and a `run` row appears in `Log` each day
- [ ] Check the unassigned count each day
- [ ] Check how many reps tapped a button (`tap` rows in `Log`); ask the others why not
- [ ] On the first Monday, check the "Dormant opportunities" list reached management
- [ ] Ask the reps if anything looks wrong

**Rollback:** delete the `SEND` variable. The next run goes back to dry-run, and nothing else needs undoing. Links already emailed stay valid for 14 days and only update one invoice's follow-up status.
