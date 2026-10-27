# Collections reminders

A weekday job that reads unpaid and never-sent invoices from Zoho Books and emails each sales rep what to chase, with an escalating tone. Reps log the outcome with one tap from the email. Managers get a summary, a scorecard and WhatsApp buttons to nudge reps. Zoho is read-only: nothing here changes an invoice.

This README is the single source of truth for building, running and changing the system. Follow Parts 1 to 10 in order to stand it up from nothing. Parts 11 onwards cover the optional rules, reference material and operations. **If you change behaviour, config or setup, update this file in the same commit.**

## Contents
1. [How it works](#1-how-it-works)
2. [Prerequisites](#2-prerequisites)
3. [Get the code running locally](#3-get-the-code-running-locally)
4. [The Google Sheet](#4-the-google-sheet)
5. [Zoho Books API access](#5-zoho-books-api-access)
6. [Gmail sender](#6-gmail-sender)
7. [Sheet CSV feed (Apps Script #1)](#7-sheet-csv-feed-apps-script-1)
8. [GitHub: repo, secrets, variables, first dry run](#8-github-repo-secrets-variables-first-dry-run)
9. [Response tracking: tap endpoint and tap page](#9-response-tracking-tap-endpoint-and-tap-page)
10. [Test it, then go live](#10-test-it-then-go-live)
11. [Phase 2: response-aware rules and scorecard](#11-phase-2-response-aware-rules-and-scorecard)
12. [WhatsApp nudges](#12-whatsapp-nudges)
13. [Reference: rules, config, environment](#13-reference-rules-config-environment)
14. [Operating it](#14-operating-it)
15. [Repo map and design notes](#15-repo-map-and-design-notes)

---

## 1. How it works

```
 GitHub Actions (cron 08:00 UTC = 09:00 Lagos, Mon-Fri)
        |  runs remind.py (Python, standard library only)
        |
        |-- reads invoices ---------> Zoho Books API (read-only)
        |-- reads Reps / Clients / Followups / Log tabs ---> Apps Script #1 "SheetCsv" (CSV over HTTPS) <--- Google Sheet
        |-- sends emails -----------> Gmail SMTP  (one per rep, one per manager)
        '-- posts "sent"/"run" events -> Apps Script #2 "Code.gs" ---> Log / Opportunities tabs

 Rep's phone: taps "Log update" in the email
        -> static tap page (page/, hosted on any static host)
        -> Apps Script #2 (signed link, writes Followups + Log)
        -> next morning's run reads Followups / Log and changes the emails
```

- **Source of truth for money:** Zoho. **For people and follow-ups:** the Google Sheet. **Memory:** the `Log` tab (every send and every tap).
- **Two small Apps Script projects:**
  1. `apps_script/SheetCsv.gs` serves tabs as CSV so GitHub can read them. (The Sheet's "Publish to web" links were not reachable from GitHub Actions, so we do not use them.)
  2. `apps_script/Code.gs` receives taps, keeps the Log, upserts Followups and Opportunities, archives old Log rows and raises an alert if a run is missed.
- **No servers, no paid services, no dependencies.** Cost is zero at this scale (about 5 to 50 reps).

## 2. Prerequisites

| You need | Notes |
|---|---|
| Zoho Books admin access | To create an API client and read organization IDs. Data centre assumed **US** (`.com`); see Part 5 for others. |
| A Google account that owns the Sheet | The same account runs both Apps Script projects. |
| A Gmail (or Workspace) mailbox to send from | 2-Step Verification on, to create an app password. Free Gmail sends about 500 emails a day; Workspace about 2,000. |
| A GitHub account and a **private** repository | Logs, run summaries and secrets hold client data. Keep it private. The Actions free tier is 2,000 minutes a month; a run takes about 1 minute. |
| A static host for `page/` | Anything works (Vercel, Cloudflare Pages, Netlify). Vercel's Hobby plan is for non-commercial use only; prefer Cloudflare Pages for a business deployment. |
| Python 3.9+ and Git locally | Only for development and local dry runs. CI uses `ubuntu-latest`'s `python3`. |
| GitHub CLI (`gh`), optional | Makes setting secrets one command. |

## 3. Get the code running locally

```bash
git clone <your repo url> && cd invoices-reminder
python test_remind.py          # prints "all tests passed"; no installs needed
```

A local dry run needs sample invoices. `fixture.json` is git-ignored (it can hold real client data), so generate a throwaway one. It maps an organization name to a list of Zoho-style invoices, with dates relative to today so the tiers look realistic:

```bash
python - <<'EOF'
import json
from datetime import date, timedelta
day = lambda n: (date.today() + timedelta(days=n)).isoformat()
def inv(num, client, status, total, balance, due_in, sp=""):
    return dict(invoice_number=num, customer_name=client, status=status, total=total, balance=balance,
                date=day(due_in - 30), due_date=day(due_in), created_time=day(due_in - 30) + "T09:00:00+0100",
                salesperson_name=sp, created_by="Finance", invoice_url="")
json.dump({"Regtech": [
    inv("INV-1", "Client A Limited", "sent", 500000, 500000, -20),   # 20 days overdue
    inv("INV-2", "Client B Microfinance Bank", "sent", 250000, 250000, -8),
    inv("INV-3", "Client C Financial Services", "sent", 90000, 90000, 5),   # not yet due
    inv("INV-4", "Client A Limited", "draft", 120000, 120000, 0),
]}, open("fixture.json", "w"), indent=1)
EOF

REPS_SRC=templates/Reps.csv CLIENTS_SRC=templates/Clients.csv FOLLOWUPS_SRC=templates/Followups.csv \
INVOICES_FIXTURE=fixture.json FORCE=1 python remind.py
```

(On Windows PowerShell set the variables first with `$env:NAME="value"`.) It prints a `[DRY RUN]` line plus the plain-text body per recipient and writes HTML emails to `preview/*.html`; open them in a browser. `FORCE=1` lets it run on a weekend. Nothing is sent unless `SEND=1`.

The `*_SRC` values can be local file paths (as above) or HTTPS links (as in production).

## 4. The Google Sheet

Create one Google Sheet. Import each `templates/*.csv` as its own tab (File > Import > Upload > "Insert new sheet"), then replace the sample rows with real data. **Tab and header names must match exactly.**

| Tab | Headers (exact) | Purpose |
|---|---|---|
| `Reps` | `Rep, Email, WhatsApp, Role, Manager` | One row per person. `Role` is `rep` or `manager`. A rep's `Manager` is a manager's `Rep` value; that manager is copied on escalations (falls back to the first manager). `WhatsApp` is a number (`0803...` or `+234803...`), used only for the manager's WhatsApp buttons. |
| `Clients` | `Organization, Client name, Rep` | Fallback owner of a client when Zoho has no matching salesperson on the invoice. `Organization` must equal a name in `ZOHO_ORGS` (Part 8). |
| `Followups` | `Organization, Invoice, Rep note, Promised pay date, Status` | One row per invoice a rep has acted on. Written by the tap page, editable by hand. `Status` is `Chasing`, `Promised`, `Paid`, `Disputed`, `Dead` (shown to reps as "Paused") or `Client waiting`. |

Tabs created automatically later: `Log` (every send and tap), `Opportunities` (every draft invoice, kept forever for win-back and upsell; restrict who can edit it) and `Archive` (Log rows older than 90 days).

**Rep attribution, in order:** the Zoho invoice's salesperson (if it matches a `Reps` name, case aside), then the `Clients` tab, then `(unassigned)`. Unassigned invoices go only to managers, so check the unassigned count daily at first. Some Zoho organizations leave salesperson blank (Regtech does), and for those the `Clients` tab decides everything. After Part 8 you can generate a starter `Clients` list from your real invoices (Part 14, "Seeding the Clients tab").

## 5. Zoho Books API access

1. Go to the Zoho API console for your data centre (US: `https://api-console.zoho.com`) and create a **Self Client**. Note its **Client ID** and **Client Secret**.
2. In the Self Client's *Generate Code* tab use scope `ZohoBooks.invoices.READ`, a 10-minute duration and any description. Copy the code (it expires fast).
3. Exchange the code for a refresh token, once:
   ```bash
   curl -X POST "https://accounts.zoho.com/oauth/v2/token" \
     -d grant_type=authorization_code -d client_id=<ID> -d client_secret=<SECRET> -d code=<CODE>
   ```
   Keep the `refresh_token` from the response. It does not expire; the script exchanges it for a short-lived access token on every run.
4. Find each organization's ID in Zoho Books: **Settings > Organization Profile > Organization ID**. You set `ZOHO_ORGS` as `Name=ID,Name2=ID2` in Part 8. The names you choose are the ones that must appear in the `Organization` column of `Clients` and `Followups`.

Other data centres (EU, IN, AU and so on) use different hosts: change `API` and `TOKEN_URL` at the top of `remind.py` (for example `zohoapis.eu` and `accounts.zoho.eu`) and use the matching API console.

## 6. Gmail sender

1. On the sending Google account turn on **2-Step Verification**.
2. Create an **App password** (Google Account > Security > App passwords) and copy the 16 characters.
3. `GMAIL_USER` is the sending address and `GMAIL_APP_PASSWORD` is that password. The script sends through `smtp.gmail.com:465`.
4. A run refuses to send more than `DAILY_SEND_CAP` (default 400) emails, to protect the account.

## 7. Sheet CSV feed (Apps Script #1)

`remind.py` fetches `REPS_SRC`, `CLIENTS_SRC`, `FOLLOWUPS_SRC` (and `LOG_SRC` in Part 11) with a plain, anonymous request, so each is a link that returns CSV. We serve them from a small Apps Script web app that is **bound to the Sheet**.

1. In the Sheet: **Extensions > Apps Script**. Replace the default code with `apps_script/SheetCsv.gs`. Name the project, for example, "Sheet CSV feed". (A Sheet can have only one bound script, so this uses it; the tap endpoint in Part 9 is a standalone project.)
2. **Project Settings > Script properties > Add**: `KEY` = a long random string (40+ characters).
3. **Deploy > New deployment > Web app**. Execute as: **Me**. Who has access: **Anyone** (GitHub calls it without logging in; the `KEY` is the protection). Authorize when asked, then copy the URL ending `/exec`.
4. Build one link per tab (same URL and key, only `tab` changes):
   ```
   <exec URL>?tab=Reps&key=<KEY>
   <exec URL>?tab=Clients&key=<KEY>
   <exec URL>?tab=Followups&key=<KEY>
   <exec URL>?tab=Log&key=<KEY>          (needed from Part 11)
   ```
5. **Verify each in a private browser window.** You must see CSV text, not a login page. A wrong key shows `forbidden`; a tab outside the allow-list, or misspelled, shows `no such tab`.

Notes:
- Only the tabs in `TABS` at the top of `SheetCsv.gs` can be read (`Reps, Clients, Followups, Log`). To expose another tab, add it there and redeploy.
- Any edit to the script needs **Deploy > Manage deployments > pencil > New version**. Do not create a new deployment or the URL changes.
- Treat the full link as a password; it is stored only as GitHub secrets.
- Date cells are written as `yyyy-MM-dd` (time dropped). `Code.gs` writes the Log as text so timestamps keep their time.

## 8. GitHub: repo, secrets, variables, first dry run

Push this code to your private repo. Then **Settings > Secrets and variables > Actions**, or use `gh secret set NAME` and `gh variable set NAME`.

**Secrets**

| Name | Value |
|---|---|
| `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN` | From Part 5 |
| `GMAIL_USER`, `GMAIL_APP_PASSWORD` | From Part 6 |
| `REPS_SRC`, `CLIENTS_SRC`, `FOLLOWUPS_SRC` | The `tab=Reps`, `tab=Clients`, `tab=Followups` links from Part 7 |
| `APPS_SCRIPT_URL`, `LINK_SECRET` | Added in Part 9 (response tracking) |
| `LOG_SRC` | The `tab=Log` link; added in Part 11 |

**Variables**

| Name | Value |
|---|---|
| `ZOHO_ORGS` | `Regtech=894826960,OPEX Consulting=790222018` style. Names must match the `Clients` tab. |
| `LINK_PAGE_URL` | Part 9: the deployed tap page |
| `SEND` | **Leave unset until Part 10.** Unset means every scheduled run is a dry run. |
| `PHASE2` | Part 11 |
| `DAILY_SEND_CAP` | Optional, default 400 |

**First dry run:** Actions > *remind* > *Run workflow* with `send` = `0`. The run executes the unit tests, then `remind.py`. In CI the log shows only one `[DRY RUN] to=... subject=...` line per message and never a body (the log could be exposed). Check that:
- the tests step is green, and a line exists for every rep who should hear from you;
- `cc` appears only on escalations;
- the manager line exists.

Fix name mismatches (rep names vs Zoho, organization names) and rerun until it matches. To read bodies and `preview/*.html`, do a local run (Part 3) with the real `*_SRC` links in the environment.

At this point the daily reminder email works in dry-run. Parts 9 to 12 add response tracking and the manager tooling.

## 9. Response tracking: tap endpoint and tap page

### 9a. Tap endpoint (Apps Script #2 = `apps_script/Code.gs`)
1. Go to `script.google.com` > **New project**. This is a **standalone** project, because the Sheet's one bound script is the CSV feed. Paste `apps_script/Code.gs`. Name it, for example, "Collections taps".
2. **Project Settings > Script properties**:
   - `SHEET_ID`: the long ID between `/d/` and `/edit` in the Sheet's URL (required for a standalone project).
   - `LINK_SECRET`: a long random string. The **same value** goes in the GitHub secret `LINK_SECRET`.
   - `ALERT_EMAIL`: who is told when a weekday run was missed.
3. **Deploy > New deployment > Web app**, execute as **Me**, access **Anyone**. The signed link is the access control: a link only updates the one invoice it was made for, for the rep it was made for, for 14 days. Authorize when asked and copy the `/exec` URL.
4. GitHub secrets: `APPS_SCRIPT_URL` = that URL, `LINK_SECRET` = the same string.
5. **Do not run `installTriggers` yet.** It starts the 10:30 heartbeat, which alerts whenever a workday has no real send logged, so it would complain during dry runs. You run it at go-live (Part 10).

After any edit to `Code.gs`: Deploy > Manage deployments > pencil > New version (the URL stays the same).

### 9b. Tap page (`page/`)
Emails link to a small static page that calls the endpoint with `fetch`. This avoids a Google quirk: a phone signed into several Google accounts gets redirected to `/macros/u/N/` and a direct link breaks.

1. Open `page/index.html` and set the `API` constant near the top to **your** `/exec` URL from 9a.
2. Deploy the `page/` folder to any static host. Example with Vercel: `cd page && npx vercel --prod`. Copy the public URL.
3. GitHub variable `LINK_PAGE_URL` = that URL. (If unset, links open the Apps Script page directly; it works but breaks on multi-account phones.)

### What a rep sees
Each open invoice in the email has a **Log update** button. Choose one decision: **Chasing**, **Promised to pay** (with a date within 60 days), **Client says paid**, **Disputed** or **Pause**. The selected decision is highlighted. Add an optional **Rep note** below (up to 300 characters), then click **Submit update** to save the decision and note together. Selecting a decision alone saves nothing. Pause asks for confirmation when submitting. Drafts offer **Client is waiting**; paused invoices offer **Resume reminders**. The latest note is stored in `Followups` under `Rep note` (the column is added if missing). Each submission is recorded in `Log`. Failed submissions retain the decision and note for retry.

## 10. Test it, then go live

Work through `GO-LIVE.md`. It has the full checklist and the tap-by-tap test table; run it against a **test copy of the Sheet** with every rep email set to yours, and test on a phone. In order:

1. Data alignment (rep names match Zoho, emails valid, managers set).
2. Dry run, then a local content check of `preview/*.html`.
3. Safe live test: point `REPS_SRC` at a copy of the Reps sheet with every email set to yours, run with `send=1`, then restore the real `REPS_SRC`.
4. Tap tests (6b to 6e in `GO-LIVE.md`) and the failure drills.
5. **Go live:** warn the reps; run once manually with `send=1`; confirm no bounces; run `installTriggers` in the tap Apps Script (turns on the 10:30 "did it run?" alert and the monthly Log archive); then set the variable `SEND=1`.

**Rollback at any time:** delete the `SEND` variable. The next run is a dry run and nothing else needs undoing.

## 11. Phase 2: response-aware rules and scorecard

Off by default. It needs a week or so of real taps in the Log first.

1. Add the secret `LOG_SRC` (the `tab=Log` link from Part 7). In a private window it must show the header `ts,event_id,type,rep,org,invoice,action,detail`.
2. Redeploy `Code.gs` and re-run `installTriggers` (adds the monthly Log archive).
3. **Preview safely, as often as you like:** Actions > remind > Run workflow with `send` = `0` and `phase2` = `1`. Nothing is sent. The manager summary (plain text) appears on the run's **Summary** page. Check the "Rep response" table and the escalations list against what you know. (Run summaries show client names, so the repo must be private.)
4. When satisfied, set the variable `PHASE2=1`. Every run now uses the rules. Delete the variable to roll back.

What it changes:
- **Live promise:** an invoice a rep marked *Promised* for a future date moves to a quiet "Promised" list (with an Update button) and leaves "Needs action" and the escalation list until the date passes. A missed promise escalates to at least "blunt".
- **No response:** if a rep has not tapped an invoice for `IGNORE_DAYS` (3) working days after it was first emailed, its tier goes up one step (so the manager is copied earlier) and it is flagged "No response". Any tap resets the clock.
- **Scorecard** (manager email, last `SCORE_DAYS` = 30 days), per rep and for the team: *Responded* (emailed invoices that got a tap), *Median first tap* (time from the email to the first tap) and *Promises kept* (promise dates that passed with the invoice no longer open in Zoho).

## 12. WhatsApp nudges

Reps answer on WhatsApp, and there is no budget for the WhatsApp Business API, so nothing is sent automatically. The manager summary includes a **Nudge on WhatsApp** section:
- **Post to the reps group:** one button. It opens WhatsApp with a ready message (a line per rep: how many invoices need an update, and how many have no response). WhatsApp asks which chat; the manager picks the reps' group and presses Send. The message deliberately has no invoice links or client names: links are signed to one rep and the group sees everyone. A link cannot create a real @mention; the manager can type `@` over a bold name to notify someone.
- **Per rep:** one button per rep with a number in `Reps.WhatsApp`. It opens a private chat with that rep's three most overdue invoices and their **Log update** links.

## 13. Reference: rules, config, environment

**Tiers**, by days overdue: not yet due = *early*; 0 to 6 = *coach*; 7 to 13 = *firm*; 14 to 20 = *blunt*; 21+ = *decision*. A broken promise is at least *blunt*. A rep's manager is copied when the rep's worst tier is *blunt* or above.

**What gets emailed**
- Reminders start the day after an invoice is raised (early, before it is due) and repeat every weekday until it is paid. Weekends are skipped.
- **Drafts** (never issued) are finance's job. After `DRAFT_GRACE` days they appear under "Waiting on billing" with a **Client is waiting** button. Drafts `STALE_DRAFT_DAYS`+ old leave the rep emails and go to management as "dormant opportunities" (top `STALE_LIST_MAX` by value, Mondays only). Drafts are never voided and are copied into the `Opportunities` tab.
- **Collection rate** = paid / invoiced, for issued, non-void invoices dated in the current fiscal year (starts in month `FY_START_MONTH`). The team figure includes unassigned invoices; reps do not. Drafts are excluded (they are unbilled pipeline, shown separately).
- Amounts are assumed to be NGN, with no conversion.

**Constants at the top of `remind.py`**

| Constant | Default | Meaning |
|---|---|---|
| `FY_START_MONTH` | 1 | Fiscal year start month |
| `DRAFT_GRACE` | 3 | Days before a never-sent draft is flagged |
| `STALE_DRAFT_DAYS` / `STALE_LIST_MAX` | 30 / 40 | Dormant-draft cutoff and Monday list size |
| `LINK_DAYS` | 14 | How long a Log update link works |
| `IGNORE_DAYS` | 3 | Working days of silence before a tier bump (Phase 2) |
| `HISTORY_DAYS` / `SCORE_DAYS` | 60 / 30 | Log window for the rules / for the scorecard |
| `WA_MAX_INVOICES` | 3 | Invoices per per-rep WhatsApp message |
| `COUNTRY_CODE` | 234 | Prefix for local `0...` WhatsApp numbers |
| `DEFAULT_SEND_CAP` | 400 | Abort a run that would send more than this |
| `TIERS`, `OPENERS` | | Tier thresholds and the opening sentence per tier |

**Environment variables** (the workflow `.github/workflows/remind.yml` sets them from the secrets and variables above)

| Variable | Required | Meaning |
|---|---|---|
| `ZOHO_*`, `ZOHO_ORGS`, `GMAIL_*` | yes | Parts 5, 6, 8 |
| `REPS_SRC`, `CLIENTS_SRC`, `FOLLOWUPS_SRC` | yes | CSV link or local file |
| `SEND` | no | `1` = really send; otherwise dry run |
| `APPS_SCRIPT_URL`, `LINK_SECRET`, `LINK_PAGE_URL` | for response tracking | Links, logging, tap page |
| `LOG_SRC`, `PHASE2` | for Phase 2 | Part 11 |
| `DAILY_SEND_CAP` | no | Overrides the default cap |
| `FORCE` | no | `1` = run on a weekend (set automatically on manual runs) |
| `INVOICES_FIXTURE` | no | Read invoices from a JSON file instead of Zoho (local testing) |
| `DIAGNOSE` | no | `1` = explain who gets what, send nothing |
| `LIST_CLIENTS` | no | `1` or `all` = print client lists to seed the Sheet |

**Manual workflow inputs:** `send` (1 = really send), `phase2` (1 = preview Phase 2), `diagnose` (1) and `list_clients` (`1` or `all`).

**Log event types:** `sent` (one per rep per run, lists the invoices), `run` (one per real send; the heartbeat looks for today's) and `tap` (a rep's button press: rep, org, invoice, action, and the promised date in `detail`).

## 14. Operating it

**Day to day**
- The schedule is `0 8 * * 1-5` UTC (09:00 Lagos). Check the run is green and a `run` row appears in the `Log`. If a workday has none by 10:30 WAT, the heartbeat emails `ALERT_EMAIL`.
- Watch the unassigned count and how many reps tap (`tap` rows, or the scorecard).
- A missed run can be re-run from the Actions tab (*Run workflow*, `send` = `1`).

**Seeding the Clients tab:** run the workflow with `list_clients` = `1`. The run's Summary page shows a CSV of clients with unpaid or draft invoices, biggest first. Paste it into `Clients` and fill the `Rep` column. `all` produces a `Client, Region, Rep` list of every client on any invoice.

**Troubleshooting**

| Symptom | Likely cause and fix |
|---|---|
| A rep gets nothing | The rep name does not match the Zoho salesperson and the client is not on `Clients`; or `Role` is not `rep`. Run with `diagnose` = `1` (it prints counts and names only). |
| Everything is `(unassigned)` | The organization name in `Clients` does not match `ZOHO_ORGS`, or salesperson is blank in Zoho and `Clients` is empty. |
| A CSV secret returns a login page | It is not the `SheetCsv` web app link, or its access is not "Anyone". |
| `forbidden` / `no such tab` | Wrong `KEY`; or the tab is not in `TABS` in `SheetCsv.gs`. |
| Tap page says link invalid or expired | The token is older than 14 days or was edited, or `LINK_SECRET` differs between GitHub and Apps Script. `diagnose` = `1` prints the GitHub value's length, whether it has stray whitespace, and the first 8 hex characters of its SHA-256, which you can compare with `printf %s "<secret>" \| sha256sum \| cut -c1-8`. |
| Taps work on a computer but not a phone | `LINK_PAGE_URL` is unset, or the `API` constant in `page/index.html` is wrong. |
| No `run` rows, or heartbeat alerts | The run failed or was skipped (check Actions), or `APPS_SCRIPT_URL` / `LINK_SECRET` are wrong. A failed Log write never blocks emails; it prints a warning. |
| "Refusing to send N emails" | The run is over `DAILY_SEND_CAP`; raise it if that is expected. |

**Rotating `LINK_SECRET`:** change it in Apps Script and in the GitHub secret together. Links already emailed stop working; tomorrow's emails are fine. **Rotating the CSV `KEY`:** change the script property and the four `*_SRC` secrets.

**Rollback:** delete `SEND` (back to dry run) or `PHASE2` (back to Phase 1 behaviour). Links already sent stay valid until they expire and can only update one invoice's follow-up.

## 15. Repo map and design notes

```
remind.py                      all logic: Zoho fetch, rules, email text and HTML, WhatsApp links, sending, Log events
test_remind.py                 unit tests (python test_remind.py); CI runs them before every send
.github/workflows/remind.yml   schedule, manual inputs, environment
apps_script/SheetCsv.gs        Apps Script #1 (bound to the Sheet): tabs as CSV (allow-list + KEY)
apps_script/Code.gs            Apps Script #2 (standalone): taps, Log, Followups, Opportunities, heartbeat, archive
page/index.html                static tap page (set its API constant)
templates/*.csv                starter rows for the Reps / Clients / Followups tabs
GO-LIVE.md                     pre-launch checklist and tap test table
fixture.json, preview/         local only, git-ignored (they contain client data)
```

- **Why a Sheet and not a database:** managers and finance edit it by hand, and it needs no hosting. It is fine to about 50 reps. `parse_history` and `scorecard` read plain rows, so moving the Log and Followups to a database later (for example Cloudflare Workers with D1) touches only the reading and writing, not the rules.
- **Why Apps Script for taps:** it is free, runs under the Sheet owner's permissions, and a signed (HMAC-SHA256) link needs no login for reps. The page never trusts the browser: the endpoint re-verifies every token.
- **Privacy:** CI never prints email bodies. Group WhatsApp messages carry no client data or links. The repo, the Sheet and the Log hold client names and balances, so keep them private and restrict the `Opportunities` tab.
- **Known limits:** the Gmail send cap; GitHub cron can run late; the Sheet is the database; holidays count as working days; NGN only.

### Local preview

From the repository folder, run `python -m http.server 8000 --bind 127.0.0.1 --directory page`, then open `http://127.0.0.1:8000/?preview=1`. This local demo simulates submission without contacting the spreadsheet. Local sample tokens ending in `.preview` also work. On the hosted app, a valid invoice token is required. Stop the server with Ctrl+C.
