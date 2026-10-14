/**
 * Collections follow-up endpoint. Bound to the same Google Sheet that holds Reps / Clients / Followups.
 *
 * - doGet : rep opens a "Log update" link -> page with Chasing / Promised / Paid / Disputed / Dead buttons.
 *           GET never changes data (email scanners and WhatsApp previews open links); only the button click does.
 * - logUpdate : button click (google.script.run) -> upserts the Followups tab and appends to the Log tab.
 * - doPost: remind.py posts "sent"/"run" events (HMAC-signed body).
 * - heartbeat: daily trigger, emails ALERT_EMAIL if remind.py did not log a run on a workday.
 *
 * Script properties (Project Settings): LINK_SECRET (same as the GitHub secret), ALERT_EMAIL, SHEET_ID (only if the script is not bound to the Sheet).
 */
var FOLLOWUPS = 'Followups';
var LOG = 'Log';
var LOG_HEAD = ['ts', 'event_id', 'type', 'rep', 'org', 'invoice', 'action', 'detail'];
var ACTIONS = { chasing: 'Chasing', promised: 'Promised', paid: 'Paid', disputed: 'Disputed', dead: 'Dead', urgent: 'Client waiting' };
var TZ = 'Africa/Lagos';

function prop_(k) { return PropertiesService.getScriptProperties().getProperty(k) || ''; }
// Bound script: getActive() works. Standalone script (getActive() is null): set script property SHEET_ID.
function ss_() { return SpreadsheetApp.getActive() || SpreadsheetApp.openById(prop_('SHEET_ID')); }
function today_() { return Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd'); }
function hex_(bytes) {
  return bytes.map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
}
function hmac_(msg) { return hex_(Utilities.computeHmacSha256Signature(msg, prop_('LINK_SECRET'))); }
function same_(a, b) { // constant-time compare
  if (a.length !== b.length) return false;
  var d = 0;
  for (var i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}
function esc_(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/** Returns {r,o,n,e} or null if the token is forged, malformed or expired. */
function verifyToken_(token) {
  try {
    if (!prop_('LINK_SECRET') || !token) return null;
    var parts = String(token).split('.');
    if (parts.length !== 2 || !same_(parts[1], hmac_(parts[0]))) return null;
    var b = parts[0];
    while (b.length % 4) b += '=';
    var p = JSON.parse(Utilities.newBlob(Utilities.base64DecodeWebSafe(b)).getDataAsString());
    if (!p.r || !p.o || !p.n || !p.e || p.e < today_()) return null;
    return p;
  } catch (err) { return null; }
}

function page_(html) {
  return HtmlService.createHtmlOutput(html).setTitle('Collections update')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

function doGet(e) {
  var p = verifyToken_(e && e.parameter && e.parameter.t);
  if (p && e.parameter.s === '1') { // read-only status for the static tap page (fetch)
    return ContentService.createTextOutput(JSON.stringify({ status: statusOf_(p.o, p.n) })).setMimeType(ContentService.MimeType.JSON);
  }
  if (!p) return page_('<body style="font-family:sans-serif;padding:24px"><h3>Link invalid or expired</h3>' +
    '<p>Use the link in today\'s collections email.</p></body>');
  var t = HtmlService.createTemplate(
    '<body style="font-family:Segoe UI,Arial,sans-serif;max-width:420px;margin:0 auto;padding:20px">' +
    '<h2 style="margin:0">Invoice <?= num ?></h2>' +
    '<p style="color:#64748b;margin:4px 0 20px"><?= org ?> &middot; <?= rep ?></p>' +
    '<div id="btns">' +
    '<? if (draft) { ?><p>This invoice is still a draft. Finance issues it; you cannot change it.</p>' +
    '<button onclick="go(\'urgent\')">Client is waiting</button><? } else { ?>' +
    '<button onclick="go(\'chasing\')">Chasing</button>' +
    '<label style="display:block;margin:12px 0 4px">Promised to pay on</label>' +
    '<input type="date" id="d" style="font-size:16px;padding:8px;width:100%;box-sizing:border-box">' +
    '<button onclick="go(\'promised\')">Save promise</button>' +
    '<button onclick="go(\'paid\')">Client says paid</button>' +
    '<button onclick="go(\'disputed\')">Disputed</button>' +
    '<button id="dead" style="background:#fff;color:#dc2626;border-color:#dc2626" onclick="dead()">Pause: client unreachable (stop reminders)</button><? } ?>' +
    '</div><p id="msg" style="font-weight:600"></p>' +
    '<style>button{display:block;width:100%;margin-top:10px;padding:14px;font-size:16px;border-radius:8px;' +
    'border:1px solid #0f172a;background:#0f172a;color:#fff}</style>' +
    '<script>var T=<?!= tok ?>,armed=false;' +
    'function dead(){var b=document.getElementById("dead");if(!armed){armed=true;b.textContent="Tap again to confirm: pause reminders";return;}go("dead");}' +
    'function go(a){var e=document.getElementById("d"),d=e?e.value:"";document.getElementById("msg").textContent="Saving...";' +
    'google.script.run.withSuccessHandler(function(m){document.getElementById("btns").style.display="none";' +
    'document.getElementById("msg").textContent=m;}).withFailureHandler(function(x){document.getElementById("msg").textContent=' +
    '"Not saved: "+x.message;}).logUpdate(T,a,d);}</script></body>');
  t.num = p.n; t.org = p.o; t.rep = p.r; t.draft = p.k === 'd'; t.tok = JSON.stringify(e.parameter.t);
  return page_(t.evaluate().getContent());
}

/** Called by the page. Re-verifies the token; the browser is never trusted. */
function logUpdate(token, action, date) {
  var p = verifyToken_(token);
  if (!p) throw new Error('Link invalid or expired');
  if (!ACTIONS.hasOwnProperty(action)) throw new Error('Unknown action');
  if ((p.k === 'd') !== (action === 'urgent') || (p.k === 'p' && action !== 'chasing')) throw new Error('That action is not available for this invoice'); // drafts: only "Client is waiting"; paused: only Resume (Chasing)
  if (action === 'promised') {
    var max = Utilities.formatDate(new Date(Date.now() + 60 * 864e5), TZ, 'yyyy-MM-dd');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date || '') || date < today_() || date > max) throw new Error('Pick a date within the next 60 days');
  } else { date = ''; }
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    upsertFollowup_(p.o, p.n, ACTIONS[action], date);
    appendLog_([{ id: 'tap:' + Utilities.getUuid(), type: 'tap', rep: p.r, org: p.o, invoice: p.n,
      action: action, detail: date }]);
  } finally { lock.releaseLock(); }
  return 'Saved: ' + ACTIONS[action] + (date ? ' ' + date : '') + '. You can close this page.';
}

function followLookup_(org, inv) { // -> {sh, data, c, row}; row is the 1-based sheet row, or -1 when this invoice has none yet
  var sh = ss_().getSheetByName(FOLLOWUPS);
  var data = sh.getDataRange().getValues();
  var h = data[0].map(function (x) { return String(x).trim(); });
  var c = { org: h.indexOf('Organization'), inv: h.indexOf('Invoice'), st: h.indexOf('Status'), dt: h.indexOf('Promised pay date') };
  if (c.org < 0 || c.inv < 0 || c.st < 0 || c.dt < 0) throw new Error('Followups tab is missing a required column');
  var row = -1;
  for (var i = 1; i < data.length; i++) {
    if (String(data[i][c.org]).trim().toLowerCase() === org.toLowerCase() && String(data[i][c.inv]).trim() === String(inv)) { row = i + 1; break; }
  }
  return { sh: sh, data: data, c: c, row: row };
}

function upsertFollowup_(org, inv, status, date) {
  var f = followLookup_(org, inv), sh = f.sh, c = f.c, row = f.row;
  if (row < 0) { row = sh.getLastRow() + 1; sh.getRange(row, c.org + 1).setValue(org); sh.getRange(row, c.inv + 1).setNumberFormat('@').setValue(inv); }
  sh.getRange(row, c.st + 1).setValue(status);
  sh.getRange(row, c.dt + 1).setNumberFormat('@').setValue(date); // text, so Sheets never turns it into a locale date
}

/** Current Followups status for an invoice, e.g. "Promised 2026-10-20"; '' when none. */
function statusOf_(org, inv) {
  var f = followLookup_(org, inv);
  if (f.row < 0) return '';
  var st = String(f.data[f.row - 1][f.c.st]).trim(), dt = String(f.data[f.row - 1][f.c.dt]).trim();
  return st && (st + (dt ? ' ' + dt : ''));
}

function appendLog_(events) {
  var ss = ss_();
  var sh = ss.getSheetByName(LOG) || ss.insertSheet(LOG);
  if (sh.getLastRow() === 0) sh.appendRow(LOG_HEAD);
  var seen = {};
  if (sh.getLastRow() > 1) sh.getRange(2, 2, sh.getLastRow() - 1, 1).getValues().forEach(function (r) { seen[r[0]] = 1; });
  var ts = Utilities.formatDate(new Date(), TZ, "yyyy-MM-dd'T'HH:mm:ss");
  var rows = [];
  events.forEach(function (ev) {
    if (!ev.id || seen[ev.id]) return; // idempotent: retries and double taps are ignored
    seen[ev.id] = 1;
    rows.push([ts, ev.id, ev.type || '', ev.rep || '', ev.org || '', ev.invoice || (ev.invoices || []).join(', '), ev.action || '', ev.detail || '']);
  });
  if (rows.length) sh.getRange(sh.getLastRow() + 1, 1, rows.length, LOG_HEAD.length).setValues(rows);
}

/** Opportunities tab: one row per draft invoice, kept forever. Drafts are lost opportunities to win back (upsell agent), never voided. */
function upsertDrafts_(rows) {
  var ss = ss_();
  var sh = ss.getSheetByName('Opportunities') || ss.insertSheet('Opportunities');
  var head = ['Organization', 'Invoice', 'Client', 'Amount', 'Draft created', 'Age (days)', 'Created by', 'Rep', 'First seen', 'Last seen'];
  if (sh.getLastRow() === 0) sh.appendRow(head);
  var idx = {};
  if (sh.getLastRow() > 1) sh.getRange(2, 1, sh.getLastRow() - 1, 2).getValues().forEach(function (r, i) {
    idx[String(r[0]).toLowerCase() + '|' + r[1]] = i + 2;
  });
  var day = today_(), fresh = [];
  rows.forEach(function (r) {
    var vals = [r.o, r.n, r.client, r.amount, r.created, r.days, r.by, r.rep], at = idx[String(r.o).toLowerCase() + '|' + r.n];
    if (at) { sh.getRange(at, 1, 1, 8).setValues([vals]); sh.getRange(at, 10).setValue(day); } // keeps First seen
    else fresh.push(vals.concat([day, day]));
  });
  if (fresh.length) sh.getRange(sh.getLastRow() + 1, 1, fresh.length, head.length).setValues(fresh);
}

function doPost(e) {
  var body = (e && e.postData && e.postData.contents) || '';
  var sig = (e && e.parameter && e.parameter.sig) || '';
  if (!sig) { // tap from the static page (page/index.html): {t, a, d}; logUpdate re-verifies the token
    var out;
    try { var o = JSON.parse(body); out = { ok: true, msg: logUpdate(o.t, o.a, o.d) }; }
    catch (err) { out = { ok: false, msg: String(err.message || err) }; }
    return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
  }
  if (!prop_('LINK_SECRET') || !same_(sig, hmac_(body))) return ContentService.createTextOutput('{"ok":false}');
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var events = JSON.parse(body);
    events.filter(function (ev) { return ev.type === 'drafts'; }).forEach(function (ev) { upsertDrafts_(ev.rows || []); });
    appendLog_(events.filter(function (ev) { return ev.type !== 'drafts'; }));
  } finally { lock.releaseLock(); }
  return ContentService.createTextOutput('{"ok":true}');
}

/** Daily trigger (~10:30 WAT). remind.py logs a "run" event after each real send. */
function heartbeat() {
  var day = Utilities.formatDate(new Date(), TZ, 'u'); // 1=Mon..7=Sun
  if (day > 5) return;
  var sh = ss_().getSheetByName(LOG);
  var want = today_() + ':run';
  var ok = sh && sh.getLastRow() > 1 && sh.getRange(2, 2, sh.getLastRow() - 1, 1).getValues().some(function (r) { return r[0] === want; });
  if (!ok && prop_('ALERT_EMAIL')) {
    MailApp.sendEmail(prop_('ALERT_EMAIL'), 'ALERT: collections reminder did not run today',
      'No run was logged for ' + today_() + '. Check the GitHub Actions run (failed, delayed or skipped) and re-run it manually if needed.');
  }
}

/** Run once from the editor, after setting the script properties. */
function installTriggers() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('heartbeat').timeBased().atHour(10).nearMinute(30).everyDays(1).inTimezone(TZ).create();
}
