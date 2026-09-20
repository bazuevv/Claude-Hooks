// Exercise the actual Mood module, including polling, tooltip and history values.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const start = source.lastIndexOf('(function () {', source.indexOf('if (window.__claudeMoodGaugeInstalled)'));
const end = source.indexOf('\n})();', start) + '\n})();'.length;
let response;
const root = { classList: { remove() {}, add() {} }, querySelector() { return null; } };
const context = {
  window: { __CLAUDE_CUSTOM_CONFIG__: { moodGauge: true }, __claudeSessionId: () => 'test',
    __claudeDomWatch: { register() {} }, addEventListener() {} },
  document: { querySelectorAll: () => [root], addEventListener() {}, hidden: false },
  setInterval() {}, setTimeout() {}, console,
  fetch: async () => ({ json: async () => response }),
};
vm.createContext(context);
// Expose the history calculation without duplicating its implementation.
const moduleSource = source.slice(start, end).replace('  /* ---------- монтирование ---------- */',
  '  window.historyValue = valueOfPoint;\n  /* ---------- монтирование ---------- */');
const clean = { ok: true, early_misses: 0, early_chances: 100, context_peak: 258400 };
response = clean;
vm.runInContext(moduleSource, context);
const api = context.window.__claudeMood;
const event = (before, after, extra = {}) => ({ kind: 'compact', ts: '2026-09-17T11:00:00Z',
  context_before: before, context_after: after, context_window: 258400, ...extra });
async function poll(history, extra = {}) {
  response = { ...clean, history, ...extra };
  api.refresh();
  await new Promise(resolve => setImmediate(resolve));
  return api.level();
}
(async () => {
  assert.equal(await poll([]), 3, 'No compaction preserves the cache score');
  assert.equal(await poll([event(239362, 235375)]), 0, 'Ineffective compaction overrides perfect cache');
  assert.match(root.title, /красная зона/);
  assert.match(root.title, /129\s200/);
  assert.equal(await poll([event(200000, 100000)]), 0, '50% of input is insufficient: use maximum window');
  assert.equal(await poll([event(200000, 70801)]), 0, 'One token below required reduction fails');
  assert.equal(await poll([event(200000, 70800)]), 3, 'Exactly half the window succeeds');
  assert.equal(await poll([event(226696, 72707)]), 3, 'Real successful session compaction');
  assert.equal(await poll([event(230000, 235000)]), 0, 'Growing context fails');
  assert.equal(await poll([event(230000, 230000)], { early_chances: 0 }), 0, 'Young sessions also turn red');
  for (const extra of [{ context_after: null }, { context_after: 0 }, { context_before: 0 },
    { context_window: null }, { context_window: 0 }, { context_after: NaN }]) {
    assert.equal(await poll([event(230000, 230000, extra)]), 3, 'Incomplete measurements are not failures');
  }
  assert.equal(await poll([event(150000, 40000, { context_window: 200000 })],
    { context_window: 1000000 }), 3, 'Use window at compaction, not the currently selected model');
  const bad = event(239362, 235375);
  const good = event(226696, 72707, { ts: '2026-09-17T12:00:00Z' });
  const pending = event(230000, null, { ts: '2026-09-17T13:00:00Z' });
  assert.equal(await poll([bad, pending]), 0, 'Pending measurement keeps the last verdict');
  assert.equal(await poll([], { compactions: [bad] }), 0, 'Short Usage history must not erase the Mood warning');
  assert.equal(await poll([good, pending, bad]), 3, 'Latest successful compaction clears red even if unordered');
  const point = { early_misses: 0, early_chances: 100, context_peak: 258400 };
  const historyValue = context.window.historyValue;
  assert(historyValue({ ...point, ts: '2026-09-17T10:00:00Z' }, [bad, good]) >= 75);
  assert(historyValue({ ...point, ts: '2026-09-17T11:30:00Z' }, [bad, good]) < 25);
  assert(historyValue({ ...point, ts: '2026-09-17T12:30:00Z' }, [bad, good]) >= 75);
  assert.equal(await poll([]), 3, 'Switching to another session does not retain the red verdict');
  console.log('Mood polling, threshold, tooltip, reset and history: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
