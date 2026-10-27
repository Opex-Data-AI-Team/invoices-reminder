// Run with: node --test test_apps_script.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(kind, rows) {
  const sheets = {};
  function sheet(data) {
    return {
      data,
      getLastRow() { return data.length; },
      getLastColumn() { return Math.max(0, ...data.map(r => r.length)); },
      getDataRange() { return this.getRange(1, 1, data.length, this.getLastColumn()); },
      appendRow(row) { data.push(Array.from(row)); },
      getRange(row, col, height = 1, width = 1) {
        return {
          setNumberFormat(format) { assert.equal(format, '@'); return this; },
          setValue(value) { return this.setValues([[value]]); },
          setValues(values) {
            values.forEach((valuesRow, i) => valuesRow.forEach((value, j) => {
              data[row - 1 + i] ||= [];
              data[row - 1 + i][col - 1 + j] = value;
            }));
            return this;
          },
          getValues() {
            return Array.from({ length: height }, (_, i) =>
              Array.from({ length: width }, (_, j) => data[row - 1 + i]?.[col - 1 + j] ?? ''));
          }
        };
      }
    };
  }
  sheets.Followups = sheet(rows);
  const ss = { getSheetByName: name => sheets[name], insertSheet: name => (sheets[name] = sheet([])) };
  const ctx = vm.createContext({
    SpreadsheetApp: { getActive: () => ss },
    LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
    Utilities: { getUuid: () => String(sheets.Log?.data.length || 0), formatDate: () => '2026-10-27' }
  });
  vm.runInContext(fs.readFileSync('apps_script/Code.gs', 'utf8'), ctx);
  ctx.verifyToken_ = token => token === 'valid' ? { r: 'Rep', o: 'Org', n: '001', k: kind } : null;
  return { ctx, sheets };
}

const header = ['Organization', 'Invoice', 'Rep note', 'Promised pay date', 'Status'];
function form(kind, preview = false, search, hostname = '127.0.0.1') {
  const elements = {};
  const buttons = [];
  for (const id of ['h', 'sub', 'info', 'cur', 'btns', 'msg']) elements[id] = { textContent: '' };
  elements.btns.appendChild = b => buttons.push(b);
  elements.btns.insertAdjacentHTML = (_, html) => {
    for (const id of ['n', 'd']) if (html.includes('id="' + id + '"')) elements[id] = { value: '' };
  };
  const requests = [];
  const payload = { n: '001', o: 'Org', r: 'Rep', k: kind, e: '2099-12-31' };
  const token = Buffer.from(JSON.stringify(payload)).toString('base64url') + (preview ? '.preview' : '.signed');
  const ctx = vm.createContext({
    URLSearchParams, atob, location: { search: search ?? '?t=' + token, hostname },
    window: { confirm: () => true },
    document: {
      getElementById: id => elements[id],
      createElement: () => ({ dataset: {}, setAttribute(name, value) { this[name] = value; } }),
      querySelectorAll: selector => selector === 'button[data-action]' ? buttons.filter(b => b.dataset.action) : [...buttons, elements.n, elements.d].filter(Boolean)
    },
    fetch: (url, options) => {
      if (!options) return Promise.resolve({ json: () => ({ status: '' }) });
      requests.push(JSON.parse(options.body));
      return Promise.resolve({ json: () => ({ ok: false, msg: 'Test rejection' }) });
    }
  });
  const source = fs.readFileSync('page/index.html', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInContext(source, ctx);
  return { ctx, buttons, elements, requests };
}
test('decision selection sends nothing; submit sends decision, date and note together once', async () => {
  const { ctx, buttons, elements, requests } = form();
  buttons.find(b => b.textContent === 'Promised to pay').onclick();
  assert.equal(requests.length, 0);
  assert.equal(buttons.find(b => b.dataset.action === 'promised')['aria-pressed'], 'true');
  elements.d.value = '2026-11-01';
  elements.n.value = 'Waiting for approval';
  const submit = buttons.at(-1);
  assert.equal(submit.textContent, 'Submit update');
  submit.onclick(); submit.onclick();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].a, 'promised');
  assert.equal(requests[0].d, '2026-11-01');
  assert.equal(requests[0].m, 'Waiting for approval');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ctx.busy, false);
  assert.equal(elements.n.value, 'Waiting for approval');
  assert.match(elements.msg.textContent, /Not saved/);
});
test('requires a decision and a date for promises; switching decisions changes the submitted action', () => {
  const { ctx, elements, requests } = form();
  ctx.go();
  assert.match(elements.msg.textContent, /Select a decision/);
  ctx.select('promised'); ctx.go();
  assert.match(elements.msg.textContent, /payment date/);
  assert.equal(requests.length, 0);
  ctx.select('paid'); ctx.go();
  assert.equal(requests[0].a, 'paid');
  assert.equal(requests[0].d, '');
});
for (const kind of ['d', 'p']) {
  test(`draft/paused selection waits for submit (${kind})`, () => {
    const { buttons, requests } = form(kind);
    assert.equal(buttons.length, 2);
    buttons[0].onclick();
    assert.equal(requests.length, 0);
    buttons[1].onclick();
    assert.equal(requests[0].a, kind === 'd' ? 'urgent' : 'chasing');
  });
}
test('local demo submission never calls the endpoint', () => {
  const { ctx, requests, elements } = form(undefined, true);
  ctx.select('paid'); ctx.go();
  assert.equal(requests.length, 0);
  assert.match(elements.msg.textContent, /No spreadsheet was changed/);
});
test('short localhost preview link opens the form without a token', () => {
  const { ctx, elements, requests } = form(undefined, false, '?preview=1');
  assert.equal(elements.h.textContent, 'Invoice PREVIEW-001');
  assert.ok(elements.n);
  ctx.select('paid'); ctx.go();
  assert.equal(requests.length, 0);
  assert.match(elements.msg.textContent, /No spreadsheet was changed/);
});
test('preview parameter cannot bypass missing tokens on a hosted page', () => {
  const { elements, buttons } = form(undefined, false, '?preview=1', 'example.com');
  assert.equal(elements.h.textContent, 'Link invalid or expired');
  assert.equal(buttons.length, 0);
});
test('Apps Script page generates valid client JavaScript with a single submit control', () => {
  const { ctx } = setup(undefined, [header.slice()]);
  let template;
  ctx.HtmlService = {
    createTemplate: html => { template = html; return { evaluate: () => ({ getContent: () => html }) }; },
    createHtmlOutput: () => ({ setTitle() { return this; }, addMetaTag() { return this; } })
  };
  ctx.doGet({ parameter: { t: 'valid' } });
  const script = template.match(/<script>([\s\S]*?)<\/script>/)[1].replace('<?!= tok ?>', '"valid"');
  new vm.Script(script);
  assert.equal((template.match(/onclick="go\(\)"/g) || []).length, 1);
  assert.ok(template.indexOf('id="n"') > template.indexOf('data-action="dead"'));
});
for (const kind of [undefined, 'd', 'p']) {
  test(`standalone note preserves status/date and logs attributed text (kind ${kind})`, () => {
    const { ctx, sheets } = setup(kind, [header.slice(), ['Org', '001', 'old', '2026-11-01', 'Promised']]);
    ctx.logUpdate('valid', 'note', '', '  Client needs a call\nTomorrow  ');
    assert.deepEqual(sheets.Followups.data[1], ['Org', '001', 'Client needs a call\nTomorrow', '2026-11-01', 'Promised']);
    assert.deepEqual(sheets.Log.data[1].slice(2), ['tap', 'Rep', 'Org', '001', 'note', 'Client needs a call\nTomorrow']);
  });
}
test('adds a missing note column and creates an invoice row without assigning a status', () => {
  const { ctx, sheets } = setup('d', [['Organization', 'Invoice', 'Status', 'Promised pay date']]);
  ctx.logUpdate('valid', 'note', '', '=literal text');
  assert.equal(sheets.Followups.data[0][4], 'Rep note');
  assert.equal(sheets.Followups.data[1][4], '=literal text');
  assert.equal(sheets.Followups.data[1][1], '001');
  assert.equal(ctx.statusOf_('Org', '001'), '');
});
test('rejects blank notes, invalid tokens and prohibited status changes without writing', () => {
  const { ctx, sheets } = setup('d', [header.slice()]);
  assert.throws(() => ctx.logUpdate('valid', 'note', '', ' \n '), /Type a note/);
  assert.throws(() => ctx.logUpdate('invalid', 'note', '', 'text'), /Link invalid/);
  assert.throws(() => ctx.logUpdate('valid', 'paid', '', 'text'), /not available/);
  assert.equal(sheets.Followups.data.length, 1);
  assert.equal(sheets.Log, undefined);
});
test('limits note length and keeps status updates with and without notes working', () => {
  const { ctx, sheets } = setup(undefined, [header.slice()]);
  ctx.logUpdate('valid', 'note', '', 'x'.repeat(350));
  assert.equal(sheets.Followups.data[1][2].length, 300);
  ctx.logUpdate('valid', 'promised', '2026-10-27', 'Will pay today');
  assert.deepEqual(sheets.Followups.data[1], ['Org', '001', 'Will pay today', '2026-10-27', 'Promised']);
  assert.equal(sheets.Log.data[2][7], '2026-10-27');
  ctx.logUpdate('valid', 'chasing', '', '');
  assert.deepEqual(sheets.Followups.data[1], ['Org', '001', 'Will pay today', '', 'Chasing']);
});
