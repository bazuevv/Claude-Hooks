const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const moduleSource = source.slice(source.indexOf(' * ACCOUNT SWITCHER BUTTON'));
function extract(name) {
  const start = moduleSource.indexOf('  function ' + name + '(');
  assert(start >= 0, name);
  return moduleSource.slice(start, moduleSource.indexOf('\n  }', start) + 4);
}
class Element {
  constructor() { this.children = []; this.style = {}; this.scrollTop = 0; }
  set textContent(value) { this.text = value; this.children = []; }
  appendChild(el) { this.children.push(el); el.parentNode = this; }
  removeChild(el) { this.children = this.children.filter(child => child !== el); el.parentNode = null; }
  querySelector(selector) { return selector === '.claude-accs-env' ? this.editor : selector === '.claude-accs-details' ? this.details : null; }
}
const pending = [];
let refreshes = 0;
let modals = 0;
const context = vm.createContext({
  panel: null, accountsSnapshot: null, accountsRevision: 0, activeFile: null,
  messageUsageByFile: Object.create(null), showUsage: true,
  switching: false, restarting: false, PANEL_ID: 'accounts', API_URL: 'http://127.0.0.1:18923/accounts',
  document: { body: new Element(), createElement: () => new Element(), addEventListener() {}, removeEventListener() {} },
  positionPanel() {}, onOutside() {}, onKeydown() {}, logInfo() {},
  accountRow: acc => Object.assign(new Element(), { account: acc }),
  refreshUsage() { refreshes++; }, openModal() { modals++; },
  fetch: (url, options) => new Promise((resolve, reject) => pending.push({ url, options, resolve, reject })),
});
vm.runInContext(['rememberAccounts', 'withMessageUsage', 'receiveMessageUsage', 'renderAccounts', 'renderError', 'closePanel', 'openPanel', 'findAccount', 'switchTo'].map(extract).join('\n'), context);
const old = [{ file: 'first', isActive: true, usage: { percent: 10 } }];
const fresh = [{ file: 'first', isActive: true, usage: { percent: 20 } }];
const body = () => context.panel.children[0];
const row = () => body().children.find(el => el.account);
async function flush() { for (let i = 0; i < 10; i++) await Promise.resolve(); }
async function respond(request, accounts) { request.resolve({ json: async () => ({ ok: true, accounts }) }); await flush(); }
(async () => {
  context.openPanel({});
  assert.equal(row(), undefined);
  assert(pending[0].url.endsWith('/accounts?refresh_usage=1'), 'Opening Accs requests fresh provider quotas');
  await respond(pending.shift(), old);
  assert.equal(row().account.usage.percent, 10);
  assert.equal(refreshes, 1);
  context.closePanel();
  context.openPanel({});
  assert.equal(row().account.usage.percent, 10, 'Saved accounts render synchronously');
  assert.equal(refreshes, 1, 'Cached rendering does not start a second usage request');
  assert.equal(pending.length, 1, 'Exactly one list refresh for each opening');
  context.panel.scrollTop = 48;
  await respond(pending.shift(), fresh);
  assert.equal(row().account.usage.percent, 20);
  assert.equal(context.panel.scrollTop, 48);
  assert.equal(refreshes, 2);
  context.closePanel();
  context.openPanel({});
  pending.shift().reject(new Error('offline'));
  await flush();
  assert.equal(row().account.usage.percent, 20, 'Offline keeps old data');
  context.closePanel();
  context.openPanel({});
  const stale = pending.shift();
  context.closePanel();
  context.openPanel({});
  await respond(stale, old);
  assert.equal(row().account.usage.percent, 20);
  assert.equal(context.accountsSnapshot, fresh, 'Late responses cannot replace the newer cache');
  const editor = {};
  body().editor = editor;
  await respond(pending.shift(), old);
  assert.equal(body().editor, editor, 'Refresh must not destroy unsaved settings');
  assert.equal(row().account.usage.percent, 20);
  assert.equal(context.accountsSnapshot, old, 'Updated data remains available for the next opening');
  context.closePanel();
  context.openPanel({});
  const details = {};
  body().details = details;
  await respond(pending.shift(), fresh);
  assert.equal(body().details, details, 'List refresh must preserve the account details view');
  context.closePanel();
  context.openPanel({});
  assert.equal(row().account.usage.percent, 20);
  const beforeSwitch = pending.shift();
  context.switchTo('second', {}, body());
  assert.equal(pending.length, 0, 'Opening confirmation does not send a switch request');
  assert.equal(modals, 1);
  assert.equal(context.panel, null);
  assert.equal(context.accountsSnapshot, fresh, 'Opening confirmation preserves the active account');
  await respond(beforeSwitch, old);
  assert.equal(context.accountsSnapshot, fresh, 'Stale GET cannot replace the confirmation snapshot');
  context.openPanel({});
  assert.equal(context.activeFile, 'first');
  context.closePanel();
  await respond(pending.shift(), fresh);
  assert.equal(context.panel, null);
  assert.equal(context.accountsSnapshot, fresh, 'An opening request may finish caching after closing');
  assert.equal(pending.length, 0, 'No periodic background polling');

  const zai = (percent, observedAt) => ({ file: 'zai', usageZai: true,
    usage: { observedAt, windows: [{ key: 'five_hour', percent }] } });
  context.rememberAccounts([zai(6, 100), { file: 'other', usageZai: true }]);
  context.receiveMessageUsage({ file: 'zai', usage: zai(8, 200).usage });
  assert.equal(context.accountsSnapshot[0].usage.windows[0].percent, 8, 'Final message sample updates the closed panel snapshot');
  assert.equal(context.accountsSnapshot[1].usage, undefined, 'A different account is untouched');
  context.openPanel({});
  assert.equal(row().account.usage.windows[0].percent, 8, 'Reopening immediately shows the final sample');
  await respond(pending.shift(), [zai(6, 150)]);
  assert.equal(row().account.usage.windows[0].percent, 8, 'An older in-flight list cannot restore stale usage');
  const host = {
    usage: {}, getAttribute: () => 'zai', querySelector() { return this.usage; },
    replaceChild(fresh) { this.usage = fresh; },
  };
  context.panel.querySelectorAll = () => [host];
  context.usageBlock = value => value;
  context.receiveMessageUsage({ file: 'zai', usage: zai(10, 300).usage });
  assert.equal(host.usage.windows[0].percent, 10, 'The open panel updates without reopening or replacing the editor');
  context.receiveMessageUsage({ file: 'zai', usage: zai(6, 100).usage });
  assert.equal(host.usage.windows[0].percent, 10, 'Historical messages cannot overwrite fresh usage');
  context.closePanel();
  assert.equal(pending.length, 0, 'Reusing message samples sends no provider requests');

  for (const profile of [{ file: 'openai', provider: 'openai' }, { file: 'anthropic', oauth: true }]) {
    const usage = (percent, observedAt) => ({ observedAt, windows: [{ key: 'primary', percent }] });
    context.rememberAccounts([{ ...profile, usage: usage(60, 100) }]);
    context.receiveMessageUsage({ file: profile.file, usage: usage(61, 200) });
    context.openPanel({});
    assert.equal(row().account.usage.windows[0].percent, 61, profile.file + ' immediately shows final task sample');
    await respond(pending.shift(), [{ ...profile, usage: usage(60, 150) }]);
    assert.equal(row().account.usage.windows[0].percent, 61, 'Late stale response cannot undo task sample');
    const liveHost = {
      usage: {}, getAttribute: () => profile.file, querySelector() { return this.usage; },
      replaceChild(fresh) { this.usage = fresh; },
    };
    context.panel.querySelectorAll = selector => {
      assert.equal(selector, '[data-claude-account-usage-file]');
      return [liveHost];
    };
    context.receiveMessageUsage({ file: profile.file, usage: usage(62, 300) });
    assert.equal(liveHost.usage.windows[0].percent, 62, 'Open panel updates immediately for ' + profile.file);
    context.rememberAccounts([{ ...profile, usage: usage(63, 400) }]);
    context.receiveMessageUsage({ file: profile.file, usage: usage(62, 350) });
    assert.equal(liveHost.usage.windows[0].percent, 63, 'Newer provider sample takes precedence over a task sample');
    context.closePanel();
  }

  // Fresh native usage is remembered for the next opening as well.
  let resolveUsage;
  const usageContext = vm.createContext({
    showUsage: true, USAGE_HOST_ATTR: 'host', panel: {}, accountsRevision: 1,
    accountsSnapshot: [{ oauth: true, file: 'oauth' }, { provider: 'openai', file: 'openai' }],
    findUsageHolder: () => ({ getUsage: () => new Promise(resolve => { resolveUsage = resolve; }) }),
    markUsageBusy() {}, windowsFromRateLimits: value => value, applyUsage() {}, logInfo() {},
  });
  vm.runInContext(extract('refreshUsage'), usageContext);
  usageContext.refreshUsage({ querySelector: () => true, isConnected: true });
  resolveUsage({ usage: { rate_limits: { windows: [{ percent: 30 }] } } });
  await flush();
  assert.equal(usageContext.accountsSnapshot[0].usage.windows[0].percent, 30);
  assert.equal(usageContext.accountsSnapshot[1].usage, undefined, 'Native OAuth limits must not overwrite OpenAI limits');
  console.log('Accs: immediate cached display, refresh on opening, offline, stale replies, editor protection, switching and usage cache: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
