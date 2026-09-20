const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
function extract(name) {
  const start = source.indexOf('  function ' + name + '(');
  assert(start >= 0);
  return source.slice(start, source.indexOf('\n  }', start) + 4);
}
class Element {
  constructor() {
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.title = '';
    this.classList = { add() {} };
  }
  appendChild(el) { this.children.push(el); }
  insertBefore(el, before) {
    this.children = this.children.filter(child => child !== el);
    const index = before ? this.children.indexOf(before) : this.children.length;
    this.children.splice(index, 0, el); el.parent = this;
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  focus() { this.focused = true; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
}
const fakeRow = e => Object.assign(new Element(), e, { children: [{ textContent: e.kind }] });
const context = vm.createContext({
  document: { createElement: () => new Element() },
  hhmm: ts => ts.slice(11, 16),
  missRow: fakeRow, hitRow: fakeRow, eventRow: fakeRow,
});
vm.runInContext(extract('usageHistoryGroup') + '\n' + extract('renderHistory'), context);
const events = [
  { kind: 'compact', ts: '2026-09-17T11:00:00Z' },
  { kind: 'hit', ts: '2026-09-17T13:59:00+03:00' },
  { kind: 'miss', ts: '2026-09-17T11:02:00Z' },
  { kind: 'account', ts: '2026-09-17T11:01:00Z' },
  { kind: 'hit', ts: '2026-09-17T11:03:00Z' },
];
const before = JSON.stringify(events);
const rendered = [];
context.renderHistory({ appendChild: e => rendered.push(e) }, { history: events }, 60, 5);
assert.deepEqual(rendered.slice(1).map(e => e.kind), ['hit', 'miss', 'account', 'compact', 'hit']);
assert.equal(JSON.stringify(events), before, 'Rendering must not reorder shared telemetry');
const fallback = [];
context.renderHistory({ appendChild: e => fallback.push(e) }, { miss_log: [
  { ts: '2026-09-17T11:00:00Z', written: 50 },
  { ts: '2026-09-17T11:01:00Z', written: 60 },
] }, 60, 5);
assert.equal(fallback[1].ts, '2026-09-17T11:01:00Z');
const grouped = [];
context.renderHistory({ appendChild: e => grouped.push(e) }, { history: [
  { kind: 'hit', ts: '2026-09-17T11:04:00Z', started_ts: '2026-09-17T11:02:00Z', count: 3, read: 600 },
  { kind: 'compact', ts: '2026-09-17T11:01:00Z' },
] }, 60, 5);
assert.equal(grouped[1].children[0].textContent, 'hit × 3');
assert.equal(grouped[1].read, 600);
assert.match(grouped[1].title, /11:02.*11:04/);
assert.equal(grouped[2].kind, 'compact');
for (const kind of ['hit', 'miss', 'compact', 'account']) {
  const details = Object.freeze([
    Object.freeze({ kind, ts: '2026-09-17T11:02:00Z', read: 100 }),
    Object.freeze({ kind, ts: '2026-09-17T11:04:00Z', read: 200 }),
  ]);
  const output = [];
  context.renderHistory({ appendChild: e => output.push(e) }, { history: [{
    kind, ts: details[1].ts, started_ts: details[0].ts, count: 2, read: 300, details,
  }] }, 60, 5);
  const [summary, box] = output[1].children;
  assert.equal(summary.getAttribute('role'), 'button');
  assert.equal(summary.getAttribute('tabindex'), '0');
  assert.equal(summary.getAttribute('aria-expanded'), 'false');
  assert.equal(box.hidden, true);
  assert.match(summary.children[0].textContent, /^▸ /);
  summary.listeners.click();
  assert.equal(summary.getAttribute('aria-expanded'), 'true');
  assert.equal(box.hidden, false);
  assert.match(summary.children[0].textContent, /^▾ /);
  assert.deepEqual(box.children.map(e => e.ts), [details[1].ts, details[0].ts]);
  assert.deepEqual(box.children.map(e => e.read), [200, 100]);
  let prevented = 0;
  summary.listeners.keydown({ key: 'Enter', preventDefault() { prevented++; } });
  assert.equal(box.hidden, true);
  summary.listeners.keydown({ key: ' ', preventDefault() { prevented++; } });
  assert.equal(box.hidden, false);
  assert.equal(box.children.length, 2, 'Reopening must not duplicate details');
  assert.equal(prevented, 2);
  summary.listeners.keydown({ key: 'Escape', preventDefault() { throw Error('Unexpected key'); } });
  assert.equal(box.hidden, false);
}
console.log('Usage history: newest first, expandable groups, click and keyboard: OK');
const missContext = vm.createContext({
  hhmm: ts => ts.slice(11, 16), human: String, money: String,
  gapText: String, MISS_LOSS_MULT: 1.9,
  row: (label, value, cls, hint) => ({ label, value, cls, hint }),
});
vm.runInContext(extract('missRow'), missContext);
const cold = missContext.missRow({ ts: '2026-09-17T12:00:00Z', written: 0,
  fresh: 63627, gap: 0.5, verdict: 'промах' }, 60, 5);
assert.match(cold.label, /промах кэша/);
assert.equal(cold.value, 'свежий ввод 63627');
assert(!cold.value.includes('NaN') && !cold.value.includes('переписано'));
const explained = missContext.missRow({ ts: '2026-09-17T12:00:00Z', written: 0,
  fresh: 5000, gap: 0.5, explain: 'account' }, 60, 5);
assert.match(explained.hint, /после смены аккаунта/);
assert(!explained.hint.includes('кэш должен был выжить'));
const partial = missContext.missRow({ ts: '2026-09-17T12:00:00Z', written: 200,
  gap: 0.5, verdict: 'частичное' }, 60, 5);
assert.match(partial.label, /частичный промах/);
assert.match(partial.value, /переписано 200/);
console.log('Usage misses: OpenAI fresh input, explained miss and partial hit: OK');

const laterOpenaiMiss = missContext.missRow({ ts: '2026-09-17T12:00:00Z',
  gap: 40, ttl_minutes: 30, written: 0, fresh: 1000 }, 60, 5);
assert.equal(laterOpenaiMiss.cls, 'claude-cache-expired');
assert.match(laterOpenaiMiss.hint, /30 мин/);
const earlyAnthropicMiss = missContext.missRow({ ts: '2026-09-17T12:00:00Z',
  gap: 40, ttl_minutes: 60, written: 200 }, 30, 5);
assert.equal(earlyAnthropicMiss.cls, 'claude-cache-unexpected');
assert.match(earlyAnthropicMiss.hint, /60 мин/);

const keepaliveContext = vm.createContext({
  IDLE_MINUTES: 55, TTL_MINUTES: 60, MESSAGE: 'ok', MIN_CONTEXT: 100,
  human: String, applyStateAll() {}, logInfo() {},
});
vm.runInContext(extract('applyLiveConfig') + '\n' + extract('normalizeWindow'), keepaliveContext);
const config = { cacheKeepaliveMinutes: 55, cacheKeepaliveTtlMinutes: 60 };
keepaliveContext.applyLiveConfig(config, 30);
assert.equal(keepaliveContext.TTL_MINUTES, 30);
assert.equal(keepaliveContext.IDLE_MINUTES, 25, 'OpenAI keepalive must fit inside 30 minutes');
keepaliveContext.applyLiveConfig(config, 60);
assert.equal(keepaliveContext.TTL_MINUTES, 60);
assert.equal(keepaliveContext.IDLE_MINUTES, 55, 'Switching back restores the configured interval');
keepaliveContext.applyLiveConfig({}, 1);
assert(keepaliveContext.IDLE_MINUTES < 1, 'Even a one-minute TTL must have a nonempty window');
console.log('Provider TTL: historical misses and live keepalive switching: OK');

(async () => {
  let requests = 0;
  let disposed = false;
  let timer;
  let closed = 0;
  let revealResult = false;
  const jumps = [];
  const timing = { render(el, info, sent, task, running) { el.timing = { info, sent, task, running }; },
    reveal(session, id) { jumps.push([session, id]); return revealResult; },
    status() { return { running: true }; } };
  const payload = { ok: true, messages: [
    { id: 'old', text: 'first', sent_at: '2020-01-01T10:00:00Z', completed_at: '2020-01-01T10:00:05Z' },
    { id: 'new', text: '<b>literal text</b>', sent_at: '2020-01-01T10:01:00Z', attachments: 1 },
  ] };
  const tabsContext = vm.createContext({
    panel: {}, historyTab: 'cache', historyCleanup: null,
    usageSessions: new Map(), captureUsageView() {}, restoreUsageView() {},
    closePanel() { closed++; },
    document: { createElement: () => new Element() },
    window: { __claudeMessageTiming: timing },
    renderHistory(el) { el.appendChild(new Element()); },
    renderError(el, text) { el.textContent = text; },
    fetch: async () => { requests++; return { json: async () => payload }; },
    setInterval(fn) { timer = fn; return 1; }, clearInterval() { disposed = true; },
  });
  vm.runInContext(extract('usageSession') + '\n' + extract('renderHistoryTabs'), tabsContext);
  const root = new Element();
  tabsContext.renderHistoryTabs(root, { session: 'session' }, 30, 5);
  const [heading, tabs, cache, chat] = root.children;
  assert.equal(heading.textContent, 'история');
  assert.equal(tabs.getAttribute('role'), 'tablist');
  assert.equal(cache.hidden, false);
  assert.equal(chat.hidden, true);
  assert.equal(requests, 0, 'Chat payload must load only when its tab is selected');
  tabs.children[1].listeners.click();
  for (let i = 0; i < 8; i++) await Promise.resolve();
  assert.equal(cache.hidden, true);
  assert.equal(chat.hidden, false);
  assert.equal(requests, 1);
  const list = chat.children[1];
  assert.equal(list.children.length, 2);
  assert.equal(list.children[0].children[0].children[0].textContent, '<b>literal text</b>');
  assert.equal(list.children[0].children[1].children[0].timing.info.id, 'new');
  assert.equal(list.children[0].children[1].children[0].timing.running, true);
  const jump = list.children[0].children[1].children[1];
  assert.equal(jump.getAttribute('aria-label'), 'Перейти к сообщению в чате');
  jump.listeners.click();
  assert.equal(closed, 0, 'Missing message leaves Usage open with an explanation');
  assert.match(list.children[0].children[2].textContent, /не загружено/);
  revealResult = true;
  jump.listeners.click();
  list.children[1].children[1].children[1].listeners.click();
  assert.equal(closed, 2);
  assert.deepEqual(jumps, [['session', 'new'], ['session', 'new'], ['session', 'old']]);
  list.children[0].children[0].open = true;
  list.children[0].children[0].listeners.toggle();
  assert.equal(list.children[0].children[0].children.length, 1, 'Expanded message has only one text block');
  assert.equal(list.children[0].children[0].children[0].textContent, '<b>literal text</b>\nВложений: 1');
  timing.status = () => ({ running: false, task: { completed_at: '2020-01-01T10:02:00Z' } });
  timer();
  assert.equal(list.children[0].children[1].children[0].timing.running, false);
  tabs.children[0].listeners.click();
  assert.equal(cache.hidden, false);
  assert.equal(chat.hidden, true);
  tabs.children[0].listeners.keydown({ key: 'ArrowRight', preventDefault() {} });
  assert.equal(chat.hidden, false);
  assert.equal(tabs.children[1].focused, true);
  assert.equal(list.children[0].children[0].open, true, 'Switching tabs preserves expanded messages');
  tabsContext.historyCleanup();
  assert.equal(disposed, true);
  const reopened = new Element();
  tabsContext.panel = {};
  tabsContext.renderHistoryTabs(reopened, { session: 'session' }, 30, 5);
  assert.equal(reopened.children[3].children[1].children.length, 2, 'Cached chat renders before any network response');
  assert.equal(reopened.children[3].hidden, false);
  tabsContext.historyCleanup();
  console.log('Usage history tabs: lazy chat, newest first, safe text, live timing, keyboard and cleanup: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
