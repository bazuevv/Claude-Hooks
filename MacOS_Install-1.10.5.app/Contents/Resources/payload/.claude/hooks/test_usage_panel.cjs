const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const moduleSource = source.slice(source.indexOf(' * CACHE USAGE BUTTON'));
function extract(name) {
  const start = moduleSource.indexOf('  function ' + name + '(');
  assert(start >= 0, name);
  return moduleSource.slice(start, moduleSource.indexOf('\n  }', start) + 4);
}
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.attributes = {}; this.style = {};
    this.scrollTop = 0; this.open = false;
  }
  set textContent(value) { this.text = value; this.children = []; }
  appendChild(el) { this.children.push(el); el.parent = this; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(el => el !== this); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  querySelectorAll(selector) {
    const found = [];
    for (const child of this.children) {
      if ((selector === '[data-usage-key]' && child.attributes['data-usage-key']) ||
          (selector === 'details' && child.tag === 'details') ||
          (selector === '[aria-expanded]' && child.attributes['aria-expanded'] !== undefined) ||
          (selector === '[aria-expanded="false"]' && child.attributes['aria-expanded'] === 'false')) found.push(child);
      found.push(...child.querySelectorAll(selector));
    }
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getBoundingClientRect() {
    let owner = this;
    while (owner.parent && owner.id !== 'panel') owner = owner.parent;
    const top = this.offset === undefined ? 0 : this.offset - owner.scrollTop;
    return { top, bottom: top + 300, height: 300, right: 360 };
  }
}
let session = 'one';
const pending = [];
let renders = 0;
let disposed = 0;
const context = vm.createContext({
  panel: null, panelState: null, anchorBtn: null, historyCleanup: null,
  usageViewReady: false, usageSessions: new Map(), historyTab: 'cache',
  PANEL_ID: 'panel', API_URL: 'http://127.0.0.1:18923/cache-usage',
  document: { body: new Element(), createElement: tag => new Element(tag), addEventListener() {}, removeEventListener() {} },
  window: { innerHeight: 800, innerWidth: 1000 },
  findSessionId: () => session, logInfo() {}, onOutside() {}, onKeydown() {},
  fetch: url => new Promise((resolve, reject) => pending.push({ url, resolve, reject })),
  renderStats(body, data) {
    renders++;
    const row = new Element();
    row.offset = data.offset || 1000;
    row.setAttribute('data-usage-key', 'chat:old');
    const details = new Element('details');
    row.appendChild(details);
    row.__usageExpand = () => { row.fullText = true; };
    body.appendChild(row);
    context.historyCleanup = () => { disposed++; };
  },
  renderError(body, text) { body.textContent = text; }, renderHistoryTabs() {},
});
vm.runInContext(['usageSession', 'captureUsageView', 'restoreUsageView', 'closePanel', 'openPanel'].map(extract).join('\n'), context);
const button = new Element('button');
async function response(data) {
  pending.shift().resolve({ json: async () => data });
  for (let i = 0; i < 10; i++) await Promise.resolve();
}
(async () => {
  context.openPanel(button);
  assert.equal(renders, 0);
  await response({ ok: true, session });
  const row = context.panel.querySelector('[data-usage-key]');
  row.querySelector('details').open = true;
  context.panel.scrollTop = 1005;
  context.historyTab = 'chat';
  context.closePanel();
  assert.equal(disposed, 1);
  assert.equal(context.panel, null);
  context.openPanel(button);
  assert.equal(renders, 2, 'Reopen renders synchronously, before fetch resolves');
  assert.equal(context.historyTab, 'chat');
  assert.equal(context.panel.scrollTop, 1005);
  assert.equal(context.panel.querySelector('details').open, true);
  assert.equal(context.panel.querySelector('[data-usage-key]').fullText, true, 'Restore full text before measuring scroll');
  await response({ ok: true, session, offset: 1200 });
  assert.equal(context.panel.scrollTop, 1205, 'New rows above do not move the message being read');
  assert.equal(context.panel.querySelector('details').open, true);
  context.panel.querySelector('details').open = false;
  context.closePanel();
  context.openPanel(button);
  assert.equal(context.panel.querySelector('details').open, false, 'Closing a previously expanded message is preserved');
  const cachedPanel = context.panel;
  pending.shift().reject(new Error('offline'));
  for (let i = 0; i < 10; i++) await Promise.resolve();
  assert.equal(context.panel, cachedPanel);
  assert(context.panel.querySelector('[data-usage-key]'), 'Network failure keeps cached content readable');
  context.closePanel();
  session = 'two';
  context.openPanel(button);
  assert.equal(context.historyTab, 'cache');
  assert.equal(context.panel.scrollTop, 0);
  assert.equal(context.panel.querySelector('[data-usage-key]'), null, 'Never render another session cached data');
  context.closePanel();
  session = 'one';
  context.openPanel(button);
  const currentPanel = context.panel;
  await response({ ok: true, session: 'two' });
  assert.equal(context.panel, currentPanel, 'Late response from closed session cannot change the current panel');
  assert.equal(context.panel.scrollTop, 1205);
  await response({ ok: false });
  assert(context.panel.querySelector('[data-usage-key]'));
  // A cache group uses the same stable-key restoration path as a chat entry.
  const group = new Element();
  group.setAttribute('data-usage-key', 'cache:hit:date');
  const toggle = new Element('button');
  toggle.setAttribute('aria-expanded', 'false');
  toggle.click = () => toggle.setAttribute('aria-expanded', 'true');
  group.appendChild(toggle);
  context.panel.appendChild(group);
  context.panelState.expanded['cache:hit:date'] = true;
  context.restoreUsageView();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  context.closePanel();
  for (let i = 0; i < 12; i++) context.usageSession('session-' + i);
  assert.equal(context.usageSessions.size, 8, 'Bound memory used by retained histories');
  console.log('Usage panel: immediate cached reopen, scroll anchor, expanded rows, session isolation, stale responses and offline: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
