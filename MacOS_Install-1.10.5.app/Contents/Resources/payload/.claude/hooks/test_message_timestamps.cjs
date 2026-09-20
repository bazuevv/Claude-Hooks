const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const start = source.indexOf('(function () {', source.indexOf(' * USER MESSAGE TIMES'));
const code = source.slice(start, source.indexOf('/* ============================================================', start));
const sid = '7a9cc98b-3322-4644-8e74-4674add744d8';
const secondSid = '7a9cc98b-3322-4644-8e74-4674add744d9';
const sent = '2026-09-12T10:09:59.757Z';
class Element {
  constructor() {
    this.children = []; this.attributes = {};
    const classes = new Set();
    this.classList = { add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name) };
  }
  scrollIntoView(options) { this.scrolled = options; }
  get textContent() { return (this._text || '') + this.children.map(c => c.textContent).join(''); }
  set textContent(value) { this._text = value; this.children = []; }
  appendChild(child) { this.children.push(child); child.parent = this; }
  querySelector() { return this.children.find(c => c.className === 'claude-user-message-time') || null; }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  remove() { this.parent.children = this.parent.children.filter(c => c !== this); }
}
function row(id, session = sid, extra = {}) {
  const element = new Element();
  element.__reactFiber$test = { return: { memoizedProps: {
    session: { sessionId: { value: session } },
    message: { type: 'user', uuid: id, timestamp: Date.now(), ...extra },
  } } };
  return element;
}
function harness(rows, payloads, enabled = true, format, lifecycle = {}) {
  let scan;
  let tick;
  let now = Date.now();
  const requests = [];
  const posts = [];
  const timers = [];
  const context = vm.createContext({
    Date: class extends Date {
      constructor(...args) { super(...(args.length ? args : [now])); }
      static now() { return now; }
    },
    window: { __CLAUDE_CUSTOM_CONFIG__: { userMessageTimestamps: enabled, userMessageTimestampFormat: format },
      __claudeDomWatch: { register(name, fn) { scan = fn; }, kick() {} } },
    document: { readyState: 'complete', querySelectorAll: () => rows, createElement: () => new Element() },
    fetch: async (url, options = {}) => {
      requests.push(url);
      if (options.method === 'POST') {
        const stopped = JSON.parse(options.body);
        posts.push(stopped);
        for (const id of stopped.message_ids) lifecycle.tasks[id] = { completed_at: stopped.completed_at, source: 'ui_stop' };
      }
      return { json: async () => ({ ok: true, ...lifecycle,
        times: payloads[new URL(url).searchParams.get('session')] || {} }) };
    },
    setTimeout(fn) { timers.push(fn); return timers.length; },
    clearTimeout(id) { timers[id - 1] = () => {}; },
    setInterval(fn) { tick = fn; },
  });
  vm.runInContext(code, context);
  return { scan: () => scan && scan(), tick: () => tick && tick(), requests, posts, timers,
    reveal: context.window.__claudeMessageTiming.reveal,
    setNow(value) { now = value; }, advance(ms) { now += ms; } };
}
async function flush() { for (let i = 0; i < 8; i++) await Promise.resolve(); }
(async () => {
  const old = row('old');
  const synthetic = row('synthetic', sid, { isSynthetic: true });
  const tool = row('tool', sid, { parentToolUseId: 'tool-id' });
  const other = row('old', secondSid);
  const rows = [old, synthetic, tool, other];
  const payloads = { [sid]: { old: sent }, [secondSid]: { old: '2026-09-17T12:00:00Z' } };
  const h = harness(rows, payloads);
  h.scan();
  assert.equal(old.children.length, 0, 'Never display load time while the journal is loading');
  await flush();
  h.scan();
  assert.equal(h.requests.length, 2, 'Batch requests per session');
  assert.equal(old.children[0].dateTime, sent, 'Use historical send time, not native Date.now timestamp');
  assert.equal(other.children[0].dateTime, '2026-09-17T12:00:00.000Z');
  assert.equal(synthetic.children.length, 0);
  assert.equal(tool.children.length, 0);
  assert.match(old.children[0].textContent, /^2026-09-12 \d{2}:\d{2}:\d{2}$/);
  assert.match(old.children[0].title, /12\.09\.2026/);
  assert.equal(old.children[0].children[0].className, 'claude-user-message-date');
  assert.equal(old.children[0].children[0].textContent, '2026-09-12');
  assert.match(old.children[0].children[1].textContent, /^ \d{2}:\d{2}:\d{2}$/);
  h.scan();
  assert.equal(old.children.length, 1, 'Repeated scans must not duplicate stamps');
  assert.equal(h.requests.length, 2, 'Known messages need no further fetch');
  old.children[0].remove();
  h.scan();
  assert.equal(old.children.length, 1, 'Restore the stamp after a React rerender');
  const reloaded = row('old');
  const reload = harness([reloaded], payloads);
  reload.scan(); await flush(); reload.scan();
  assert.equal(reloaded.children[0].dateTime, sent, 'Reload preserves original send time');
  const customFormat = row('old');
  const formatted = harness([customFormat], payloads, true, 'DD.MM.YYYY HH:mm');
  formatted.scan(); await flush(); formatted.scan();
  assert.match(customFormat.children[0].textContent, /^12\.09\.2026 \d{2}:\d{2}$/);
  assert.equal(customFormat.children[0].dateTime, sent, 'Formatting preserves the original instant');
  assert.equal(customFormat.children[0].children[0].textContent, '12.09.2026');
  assert.equal(customFormat.children[0].children[0].className, 'claude-user-message-date');
  const fresh = row('fresh');
  const live = harness([fresh], { [sid]: { fresh: '2026-09-17T13:04:05Z' } });
  live.scan(); await flush(); live.scan();
  assert.equal(fresh.children[0].dateTime, '2026-09-17T13:04:05.000Z');
  const absent = row('absent');
  const missing = harness([absent], {});
  missing.scan(); await flush(); missing.scan();
  assert.equal(absent.children.length, 0);
  assert.equal(missing.timers.length, 1, 'Retry messages not yet persisted');
  const disabled = harness([row('old')], payloads, false);
  disabled.scan();
  assert.equal(disabled.requests.length, 0);
  // A reused DOM node can still point to the previous React fiber.
  const current = row('fresh');
  const stale = current.__reactFiber$test;
  const currentRoot = {};
  stale.return.return = { stateNode: { current: currentRoot } };
  stale.return.memoizedProps.message.uuid = 'old';
  stale.alternate = { return: { memoizedProps: {
    session: { sessionId: sid }, message: { type: 'user', uuid: 'fresh' },
  } } };
  const swapped = harness([current], { [sid]: { fresh: '2026-09-17T13:04:05Z', old: sent } });
  swapped.scan(); await flush(); swapped.scan();
  assert.equal(current.children[0].dateTime, '2026-09-17T13:04:05.000Z');
  assert.equal(swapped.reveal(sid, 'old'), false, 'Navigation must use the current React message');
  assert.equal(swapped.reveal(sid, 'fresh'), true);
  assert.equal(current.scrolled.block, 'center');
  assert.equal(h.reveal(sid, 'old'), true);
  assert.equal(old.classList.contains('claude-message-jump-highlight'), true);
  assert.equal(other.scrolled, undefined, 'Same message ID in another session must not match');
  assert.equal(h.reveal(secondSid, 'old'), true);
  assert.equal(old.classList.contains('claude-message-jump-highlight'), false);
  assert.equal(other.classList.contains('claude-message-jump-highlight'), true);
  h.timers.forEach(fn => fn());
  assert.equal(other.classList.contains('claude-message-jump-highlight'), false, 'Highlight is temporary');
  assert.equal(h.reveal(sid, 'missing'), false);
  assert.equal(h.reveal(sid, 'synthetic'), false);
  assert.equal(disabled.reveal(sid, 'old'), true, 'Navigation also works with timestamps disabled');
  const busyRow = row('task');
  const busy = { value: true };
  busyRow.__reactFiber$test.return.memoizedProps.session.busy = busy;
  const taskStarted = '2020-01-01T10:00:00Z';
  const state = { tasks: { task: {} }, active_ids: ['task'] };
  const liveTask = harness([busyRow], { [sid]: { task: taskStarted } }, true, undefined, state);
  liveTask.setNow(Date.parse(taskStarted) + 5000);
  liveTask.scan(); await flush(); liveTask.scan();
  assert(liveTask.requests.some(url => new URL(url).searchParams.get('running') === '1'), 'Request intermediate costs only for a busy session');
  assert.match(busyRow.children[0].textContent, / · 5 с$/);
  // A final response from one API call must not stop a still-busy host task.
  state.tasks.task = { completed_at: '2020-01-01T10:00:04Z', source: 'transcript' };
  liveTask.advance(90000); liveTask.tick();
  assert.match(busyRow.children[0].textContent, / · 1 м 35 с$/);
  assert(!busyRow.children[0].textContent.includes('('));
  busy.value = false;
  liveTask.tick(); await flush(); liveTask.tick();
  assert.match(busyRow.children[0].textContent, /\(\d{2}:\d{2}:\d{2}\) · 1 м 35 с$/);
  const frozen = busyRow.children[0].textContent;
  liveTask.advance(60000); liveTask.tick();
  assert.equal(busyRow.children[0].textContent, frozen, 'Duration freezes at host stop');
  const restored = row('task');
  restored.__reactFiber$test.return.memoizedProps.session.busy = { value: false };
  const restoredTask = harness([restored], { [sid]: { task: taskStarted } }, true, undefined, state);
  restoredTask.scan(); await flush(); restoredTask.scan();
  assert.equal(restored.children[0].textContent, frozen, 'Stored completion survives page reload');
  // The Stop hook finishes before the webview has processed its final chunks.
  const delayedRow = row('delayed');
  const delayedBusy = { value: true };
  delayedRow.__reactFiber$test.return.memoizedProps.session.busy = delayedBusy;
  const delayedState = { tasks: { delayed: {
    completed_at: '2020-01-01T10:06:15Z', source: 'stop_hook',
  } }, active_ids: ['delayed'] };
  const delayed = harness([delayedRow], { [sid]: { delayed: taskStarted } }, true, undefined, delayedState);
  delayed.setNow(Date.parse(taskStarted) + 376000);
  delayed.scan(); await flush(); delayed.scan();
  assert.match(delayedRow.children[0].textContent, / · 6 м 16 с$/);
  // Simulate a blocked main thread: elapsed time must include the missing ticks.
  delayed.advance(98000); delayed.tick();
  assert.match(delayedRow.children[0].textContent, / · 7 м 54 с$/);
  delayedBusy.value = false;
  delayed.tick();
  assert.match(delayedRow.children[0].textContent, /\) · 7 м 54 с$/, 'Local UI stop overrides an earlier hook even before POST returns');
  await flush(); delayed.tick();
  assert.equal(delayed.posts[0].completed_at, '2020-01-01T10:07:54.000Z');
  delayed.advance(60000); delayed.tick();
  assert.equal(delayed.posts.length, 1, 'Idle ticks must not record new completion times');
  delayedBusy.value = true;
  delayed.tick();
  assert.match(delayedRow.children[0].textContent, /\) · 7 м 54 с$/, 'A new turn must not restart the old completed message');
  const overnight = row('night');
  const night = harness([overnight], { [sid]: { night: '2020-01-01T10:00:00Z' } }, true, undefined,
    { tasks: { night: { completed_at: '2020-01-02T10:00:00Z', source: 'stop_hook' } }, active_ids: ['night'] });
  night.scan(); await flush(); night.scan();
  assert.match(overnight.children[0].textContent, /\(2020-01-02 \d{2}:\d{2}:\d{2}\) · 24 ч 0 м 0 с$/);
  const billed = row('paid');
  const billingState = { tasks: { paid: { completed_at: '2020-01-01T10:01:00Z', source: 'stop_hook',
    cost: { kind: 'api', usd: 0.1234, approximate: true } } }, active_ids: [] };
  const billing = harness([billed], { [sid]: { paid: taskStarted } }, true, undefined, billingState);
  billing.scan(); await flush(); billing.scan();
  assert.match(billed.children[0].textContent, / · 1 м 0 с · ≈ \$0\.1234$/);
  assert.match(billed.children[0].children.at(-1).title, /API/);
  const subscription = row('quota');
  const quota = harness([subscription], { [sid]: { quota: taskStarted } }, true, undefined,
    { tasks: { quota: { completed_at: '2020-01-01T10:01:00Z', cost: {
      kind: 'subscription', windows: [{ label: '5 ч', percent: 2 }, { label: '7 дн', percent: 0.5 }],
    } } }, active_ids: [] });
  quota.scan(); await flush(); quota.scan();
  assert.match(subscription.children[0].textContent, / · ≈ 2% \/ ≈ 0.5%$/);
  assert.match(subscription.children[0].children.at(-1).title, /5 ч: ≈ 2%\n7 дн: ≈ 0.5%/);
  assert(!subscription.children[0].textContent.includes('$'));
  const refreshingRow = row('refreshing');
  const refreshingBusy = { value: true };
  refreshingRow.__reactFiber$test.return.memoizedProps.session.busy = refreshingBusy;
  const refreshingState = { tasks: { refreshing: {
    cost: { kind: 'subscription', windows: [{ label: '5 ч', percent: 3 }] },
    cost_refresh: { next_at: Date.parse(taskStarted) / 1000 + 30, updating: false },
  } }, active_ids: ['refreshing'] };
  const refreshing = harness([refreshingRow], { [sid]: { refreshing: taskStarted } }, true, undefined, refreshingState);
  refreshing.setNow(Date.parse(taskStarted) + 5000);
  refreshing.scan(); await flush(); refreshing.scan();
  assert.match(refreshingRow.children[0].textContent, /≈ 3% · ↻ 00:25$/);
  assert(!refreshingRow.children[0].textContent.includes('5 ч'));
  refreshing.advance(1000); refreshing.tick();
  assert.match(refreshingRow.children[0].textContent, /↻ 00:24$/);
  refreshing.advance(25000); refreshing.tick();
  assert.match(refreshingRow.children[0].textContent, /↻ …$/, 'Expired countdown waits for the actual sample');
  refreshingState.tasks.refreshing.cost_refresh.next_at += 30;
  refreshing.tick();
  assert.match(refreshingRow.children[0].textContent, /↻ 00:29$/, 'New sample resets countdown');
  refreshingBusy.value = false; refreshing.tick();
  assert(!refreshingRow.children[0].textContent.includes('↻'), 'Countdown disappears immediately when task stops');
  const unmeasured = row('unmeasured');
  const absentCost = harness([unmeasured], { [sid]: { unmeasured: taskStarted } }, true, undefined,
    { tasks: { unmeasured: { cost: { kind: 'unknown', reason: 'no_baseline' } } }, active_ids: [] });
  absentCost.scan(); await flush(); absentCost.scan();
  assert.match(unmeasured.children[0].textContent, / · —$/);
  assert(!unmeasured.children[0].textContent.includes('$0'));
  console.log('User message times: journal source, live/history, reload, sessions, React updates, retries: OK');
  console.log('Task duration: live seconds, host stop, persistence and midnight: OK');
  console.log('Message costs: API dollars, subscription windows and unavailable historical data: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
