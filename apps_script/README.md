# Apps Script setup (one-time, about 10 minutes)

This is what receives the "Log update" taps and keeps the run log. Phase 1 of the response-tracking build.

1. Open the Google Sheet with the Reps / Clients / Followups tabs > **Extensions > Apps Script**. Paste `Code.gs` over the default file.
2. **Project Settings > Script properties**, add:
   - `LINK_SECRET`: a long random string (e.g. 40+ characters). Same value goes in the GitHub secret `LINK_SECRET`.
   - `ALERT_EMAIL`: where the "reminder did not run" alert goes (techops).
3. **Deploy > New deployment > Web app**. Execute as: **Me**. Who has access: **Anyone** (the signed token is the access control; the page only lets someone update the one invoice the link was made for). Copy the web app URL ending in `/exec`.
4. GitHub repo **Settings > Secrets**: add `APPS_SCRIPT_URL` (the URL above) and `LINK_SECRET`.
5. In the editor run `installTriggers` once (approve permissions). **Do this at go-live, not before**: the heartbeat alerts whenever a workday has no real send logged, so during dry runs it would complain.
6. The Followups tab must have headers `Organization, Invoice, Rep note, Promised pay date, Status`. A `Log` tab is created automatically.

After any change to `Code.gs`: **Deploy > Manage deployments > edit > New version** (the URL stays the same).

Rotating `LINK_SECRET`: change it in both places; links already emailed stop working, tomorrow's work.

7. An `Opportunities` tab is created on the first real send: every draft invoice (client, amount, dates, creator), upserted daily with First seen / Last seen. Keep it; it is the data for win-back and upsell. Restrict edit access to it.
