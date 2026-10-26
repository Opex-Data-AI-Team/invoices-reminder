# Go-live checklist

## 1. Config (GitHub → Settings)
- [ ] Secrets set: `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `REPS_SRC`, `CLIENTS_SRC`, `FOLLOWUPS_SRC` (each a `...exec?tab=<Tab>&key=...` link, not Publish to web: see README, "Sheet CSV feeds")
- [ ] Response tracking secrets set: `APPS_SCRIPT_URL`, `LINK_SECRET` (README Part 9a; same `LINK_SECRET` in Apps Script)
- [ ] Variable `ZOHO_ORGS` set, and the org names match the Clients tab
- [ ] Variable `LINK_PAGE_URL` = the deployed `page/` (static tap page, e.g. on Vercel). Without it, links open Apps Script directly, which breaks on phones signed into several Google accounts (Google rewrites to `/macros/u/N/`)
- [ ] Optional variable `DAILY_SEND_CAP` (default 400)
- [ ] Variable `SEND` is unset

## 2. Data alignment
- [ ] Every `Rep` name on the Reps tab (Role = rep) matches the Zoho salesperson name, case aside
- [ ] Every rep and manager has a valid `Email`, and each rep's `Manager` matches a manager's `Rep` name
- [ ] The Clients tab covers your main paying clients (it's the fallback when Zoho has no salesperson). Regtech invoices have no salesperson set in Zoho, so this tab decides who gets each one.
- [ ] Reps tab has each rep's `WhatsApp` number (feeds the manager's "Nudge on WhatsApp" buttons; local `0803...` or `+234803...` both work)
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

## 7b. Phase 2: response-aware rules (after a week of real taps)
- [ ] Add secret `LOG_SRC`: the same CSV link as `REPS_SRC` with `tab=Log` (confirm it returns the `ts,event_id,...` header in a private window); re-run `installTriggers` (adds the monthly Log archive)
- [ ] Preview, any time and as often as you like: Actions > remind > Run workflow with `send=0` and `phase2=1`. Nothing is sent, reps are unaffected, and the manager summary shows on the run page. Check the "Rep response" table and the Escalations list against what you know
- [ ] Rules in play: a live promise moves to a quiet "Promised" list until its date; no tap for `IGNORE_DAYS` (3) working days raises the tier one step; scorecard covers the last 30 days
- [ ] Happy with it: set variable `PHASE2=1`; every run, and so every rep email, uses the new rules. Rollback: delete `PHASE2`

## 7c. End-to-end validation (test copy of the Sheet, every rep email set to yours)
Pass bar: A to D before `SEND=1`; E and F before `PHASE2=1`; G any time. Overlaps with §6 on purpose: this is the single full pass.

### A. Offline
- [ ] A1 `python test_remind.py`: all 23 pass
- [ ] A2 `python remind.py` without `SEND`, open `preview/*.html`: numbers, client names and tier colours right; open invoices show **Log update**; drafts under 30 days sit under "Waiting on billing"; no stale draft in rep emails; no `?t=` tokens in manager emails

### B. Data and feeds
- [ ] B1 Each `...exec?tab=Reps/Clients/Followups/Log&key=...` link in a private window returns CSV with the right header row
- [ ] B2 A tab outside the allow-list (e.g. `tab=Secrets`) is rejected (proves the live script has the allow-list)
- [ ] B3 A wrong `key` is rejected
- [ ] B4 Dry run (`send=0`): one `[DRY RUN]` line per expected rep; unassigned section empty or understood; `cc` only on escalations

### C. Tap page and link flow (on a phone, ideally signed into several Google accounts)
- [ ] C1 Tap page with no token, then `?t=garbage`: "Link invalid or expired"
- [ ] C2 **Log update** from a real email opens on the tap page (not `/macros/u/N/`), shows invoice number, client and balance, saves nothing yet
- [ ] C3 **Chasing**: Followups = `Chasing`, `tap` row in `Log`
- [ ] C4 Valid future date + **Save promise** stored as plain text (e.g. `2026-10-20`); past date and 90-days-out date rejected
- [ ] C5 **Rep note** is stored in Followups
- [ ] C6 **Paid** then **Disputed**: Followups holds the latest, `Log` holds both
- [ ] C7 **Pause** + confirm: Status `Dead`; next run lists it under **Paused** with **Resume**
- [ ] C8 **Resume**: Status `Chasing`, invoice back under **Needs action**
- [ ] C9 One character of the token changed: rejected
- [ ] C10 Link pasted into WhatsApp: the preview alone changes nothing
- [ ] C11 Two tabs tapping at once, 3 or 4 times: no lost rows, no garbled cells
- [ ] C12 Re-opened used link shows `Currently: <last status>`
- [ ] C13 Draft **Client is waiting**: Status `Client waiting`; next run shows "CLIENT WAITING" and no second button

### D. Sending, logging, safety
- [ ] D1 `send=1`: emails arrive in Gmail, plain-text fallback fine; `Log` has one `sent` row per rep and one `run` row; `Opportunities` has one row per draft
- [ ] D2 Same-day re-run: no duplicate `Log` rows; `Opportunities` only updates `Last seen`
- [ ] D3 `DAILY_SEND_CAP=1` + `send=1`: "Refusing to send ...", nothing sent (clear the variable after)
- [ ] D4 Wrong `APPS_SCRIPT_URL` + `send=1`: emails still send, log shows "WARNING: could not write run log" (restore the URL)
- [ ] D5 `installTriggers`, skip a workday: "ALERT: collections reminder did not run today" arrives at 10:30 WAT
- [ ] D6 (Optional) Broken `ZOHO_REFRESH_TOKEN`: run fails, GitHub emails you, no `run` row (restore it)
- [ ] D7 Weekend run exits as a non-workday unless `FORCE` is set

### E. Phase 2 (needs real or test taps in `Log`)
- [ ] E1 `send=0` + `phase2=1`: manager summary on the run page with Rep response table and Escalations list
- [ ] E2 A future-dated promise moves to the quiet **Promised** list
- [ ] E3 After the promise date passes, the invoice returns to **Needs action**
- [ ] E4 No tap for 3 working days after the first email: tier up one step, manager Cc'd a tier early
- [ ] E5 Scorecard (last 30 days) counts per rep match the taps you made and skipped
- [ ] E6 Without `PHASE2=1`, reminders are unchanged (rollback check)
- [ ] E7 Monthly Log archive moves old rows to an archive tab and leaves the live `Log` intact

### F. WhatsApp buttons (manager email)
- [ ] F1 "Nudge on WhatsApp" for a `0803...` number opens WhatsApp to `234803...` with message and links prefilled
- [ ] F2 Same for a `+234803...` number
- [ ] F3 A rep with more than 3 invoices: message capped at 3, button still opens
- [ ] F4 "Post to the reps group": summary only, no links
- [ ] F5 Both buttons work from the manager email on iOS and Android

### G. Docs and deploy
- [ ] G1 `npx vercel --prod` as written in the README deploys the tap page
- [ ] G2 Someone else follows the README (Zoho Self Client steps included) against a blank Sheet and finishes without asking you anything

## 8. After go-live (first week)
- [ ] Check the 08:00 UTC (09:00 WAT) weekday run is green and a `run` row appears in `Log` each day
- [ ] Check the unassigned count each day
- [ ] Check how many reps tapped a button (`tap` rows in `Log`); ask the others why not
- [ ] On the first Monday, check the "Dormant opportunities" list reached management
- [ ] Ask the reps if anything looks wrong

**Rollback:** delete the `SEND` variable. The next run goes back to dry-run, and nothing else needs undoing. Links already emailed stay valid for 14 days and only update one invoice's follow-up status.
