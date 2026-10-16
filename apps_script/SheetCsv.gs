/**
 * Sheet CSV feed: serves any tab of the Sheet as CSV, so GitHub Actions can read Reps / Clients / Followups / Log
 * without "Publish to web". Reference copy of the script deployed in the Sheet's own Apps Script project
 * (a SEPARATE project from Code.gs, with its own /exec URL). Change it there, then update this copy.
 *
 *   <exec URL>?tab=<Tab name>&key=<KEY>
 *
 * Script property: KEY (any long random string). Only the tabs in TABS can be read, so the key never opens anything else.
 * Add a tab to TABS before making a secret for it.
 * Deploy as a web app, execute as you, access "Anyone": GitHub Actions calls it without logging in.
 * Answers: 'forbidden' (missing/wrong key), 'no such tab' (not in TABS, or misspelled), otherwise CSV.
 * Date cells are written as yyyy-MM-dd (time is dropped), which is why the Log tab is written as text.
 */
var TABS = ['Reps', 'Clients', 'Followups', 'Log'];

function cell(c) {
  if (c instanceof Date) c = Utilities.formatDate(c, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  return '"' + String(c).replace(/"/g, '""') + '"';
}

function doGet(e) {
  var key = PropertiesService.getScriptProperties().getProperty('KEY');
  if (!key || e.parameter.key !== key) return ContentService.createTextOutput('forbidden');
  var sh = TABS.indexOf(e.parameter.tab) < 0 ? null : SpreadsheetApp.getActive().getSheetByName(e.parameter.tab);
  if (!sh) return ContentService.createTextOutput('no such tab');
  var csv = sh.getDataRange().getValues().map(function (r) { return r.map(cell).join(','); }).join('\n');
  return ContentService.createTextOutput(csv).setMimeType(ContentService.MimeType.CSV);
}
