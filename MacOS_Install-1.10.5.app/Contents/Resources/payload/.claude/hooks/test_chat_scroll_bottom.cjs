const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const start = source.indexOf('(function () {', source.indexOf(' * CHAT SCROLL BOTTOM'));
const code = source.slice(start, source.indexOf('/* ============================================================', start));
function fixture(left, right, top, height, chatRight = 1140) {
  const transcript = { scrollHeight: 8000, scrollTo(options) { this.lastScroll = options; } };
  const chat = { isConnected: true, querySelector: () => transcript,
    getBoundingClientRect: () => ({ right: chatRight, width: chatRight, height: 800 }) };
  const host = { isConnected: true, querySelector: () => ({}), closest: () => chat,
    getBoundingClientRect: () => ({ left, right, top, height, width: right - left, bottom: top + height }) };
  return { host, chat, transcript };
}
const wide = fixture(260, 870, 600, 90);
let inputs = [wide.host];
let scan;
let resizeCallback;
let reduced = false;
const mounted = [];
const watched = [];
const frames = [];
const context = vm.createContext({
  window: { innerWidth: 1150, innerHeight: 800,
    __CLAUDE_CUSTOM_CONFIG__: { chatScrollBottomButton: true },
    __claudeDomWatch: { register(name, fn) { scan = fn; fn(); } },
    addEventListener() {}, matchMedia: () => ({ matches: reduced }) },
  document: { querySelectorAll: () => inputs,
    createElement: () => ({ style: {}, attrs: {}, events: {},
      setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(k, v) { this.events[k] = v; } }),
    body: { appendChild(el) { el.isConnected = true; mounted.push(el); } } },
  ResizeObserver: class {
    constructor(callback) { resizeCallback = callback; }
    disconnect() { watched.length = 0; }
    observe(el) { watched.push(el); }
  },
  requestAnimationFrame(fn) { frames.push(fn); return frames.length; },
});
vm.runInContext(code, context);
const button = mounted[0];
assert(button);
assert.equal(button.attrs['aria-label'], 'В конец чата');
assert.equal(button.style.left, '987px', 'Center the button in the free area beside the composer');
assert.equal(button.style.top, '627px');
const click = () => button.events.click({ preventDefault() {}, stopPropagation() {} });
click();
assert.equal(wide.transcript.lastScroll.top, 8000);
assert.equal(wide.transcript.lastScroll.behavior, 'smooth');
wide.transcript.scrollHeight = 9000;
reduced = true;
click();
assert.equal(wide.transcript.lastScroll.top, 9000, 'Use the latest height, including newly streamed text');
assert.equal(wide.transcript.lastScroll.behavior, 'auto');
scan({ inputs });
assert.equal(mounted.length, 1, 'Repeated scans do not duplicate the button');
const narrow = fixture(10, 385, 600, 90, 400);
context.window.innerWidth = 400;
inputs = [narrow.host];
scan({ inputs });
assert.equal(button.style.left, '352px');
assert.equal(button.style.top, '554px', 'Narrow sidebar places the button above the composer');
click();
assert.equal(narrow.transcript.lastScroll.top, 8000, 'Switching chats changes the scroll target');
assert.equal(watched.includes(wide.host), false, 'Observers release the old chat');
resizeCallback(); resizeCallback();
assert.equal(frames.length, 1, 'Batch resize notifications');
frames.shift()();
inputs = [];
scan({ inputs });
assert.equal(button.hidden, true, 'Hide outside an active chat');
inputs = [narrow.host];
scan({ inputs });
assert.equal(button.hidden, false);
button.isConnected = false;
scan({ inputs });
assert.equal(mounted.length, 2, 'Restore button after DOM replacement');
const disabled = vm.createContext({ window: { __CLAUDE_CUSTOM_CONFIG__: { chatScrollBottomButton: false } } });
vm.runInContext(code, disabled);
assert.equal(disabled.window.__claudeChatScrollBottomInstalled, undefined);
console.log('Chat end button: placement, live height, active chat, resize, remount and reduced motion: OK');
