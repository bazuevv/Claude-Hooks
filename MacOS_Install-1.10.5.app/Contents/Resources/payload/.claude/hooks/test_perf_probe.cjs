const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const code = source.slice(source.indexOf('  var PERF_ENABLED'), source.indexOf('  window.__claudePerf ='));
function harness(enabled = true, supported = ['longtask', 'long-animation-frame'], native = false) {
  let now = 0;
  const reports = [], observers = [], timers = [];
  class Observer {
    static supportedEntryTypes = supported;
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe(options) { this.type = options.type; }
    disconnect() { this.disconnected = true; }
  }
  const ctx = vm.createContext({
    cfg: { perfProbe: enabled, perfNativeMarkdown: native, locale: 'en' }, window: {},
    b41: function (props) {
      const parser = { parse: value => value, runSync: value => value };
      now += 5;
      if (props.fail) throw new Error('original error');
      return parser.runSync(parser.parse(props.children));
    },
    performance: { now: () => now, memory: { usedJSHeapSize: 10485760 } }, PerformanceObserver: Observer,
    document: { visibilityState: 'visible', hasFocus: () => true,
      getElementsByTagName: () => [], querySelectorAll: () => [] },
    location: { href: 'test' }, setInterval: callback => timers.push(callback),
    fetch(url, options) { reports.push(JSON.parse(options.body)); return Promise.resolve(); },
  });
  vm.runInContext(code + '\nperfInit();', ctx);
  return { ctx, reports, observers, timers, setNow(value) { now = value; } };
}
const h = harness();
assert.equal(h.observers.length, 2);
for (let i = 1; i <= 12; i++) {
  h.observers[1].callback({ getEntries: () => [{ startTime: 10, duration: i * 100,
    blockingDuration: 500, scripts: [{ duration: 110, sourceURL: 'app.js?private=ignored',
      sourceFunctionName: 'renderMarkdown', sourceCharPosition: 123, invokerType: 'event-listener' }] }] });
}
h.setNow(85000);
h.timers[0]();
h.ctx.perfFlush(true);
const report = h.reports[0];
assert.equal(report.interval_sec, 85, 'Blocked timers must not report an 85-second interval as ten seconds');
assert.equal(report.lag.max, 84000);
assert.equal(report.long_frames.count, 12);
assert.equal(report.long_frames.samples.length, 5, 'Attribution storage is bounded');
assert.equal(report.long_frames.max_ms, 1200);
assert.equal(report.long_frames.samples[0].scripts[0].source, 'app.js');
assert.equal(report.long_frames.samples[0].scripts[0].position, 123);
assert.equal(report.localizer_installed, false);
assert.equal(report.visibility, 'visible');
h.setNow(95000); h.ctx.perfFlush(true);
assert.equal(h.reports[1].long_frames.count, 0, 'Each report starts a fresh observation window');
assert.equal(h.reports[1].interval_sec, 10);
const fallback = harness(true, []);
fallback.setNow(10000); fallback.ctx.perfFlush(true);
assert.equal(fallback.reports[0].attribution_supported.length, 0);
const disabled = harness(false);
assert.equal(disabled.observers.length, 0);
assert.equal(disabled.timers.length, 0);
const native = harness(true, [], true);
assert.equal(native.ctx.b41({ children: 'hello' }), 'hello');
assert.throws(() => native.ctx.b41({ children: 'fail', fail: true }), /original error/);
native.ctx.perfFlush(true);
assert.equal(native.reports[0].native_markdown_state, 'installed');
assert.deepEqual(native.reports[0].native_markdown, { calls: 2, total_ms: 10, max_ms: 5, max_chars: 5 });
native.ctx.perfFlush(true);
assert.equal(native.reports[1].native_markdown.calls, 0);
for (let i = 2; i < 60; i++) native.ctx.perfFlush(true);
assert.equal(native.ctx.perfMarkdownState, 'finished', 'Restore native component after report budget');
console.log('Performance probe: real intervals, bounded browser attribution, fallback, disabled state: OK');
