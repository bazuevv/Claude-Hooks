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
  constructor() { this.children = []; this.listeners = {}; }
  appendChild(el) { this.children.push(el); el.parentNode = this; }
  removeChild(el) { this.children = this.children.filter(c => c !== el); el.parentNode = null; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  click() { this.listeners.click?.(); }
  focus() {}
}
function setup() {
  const pending = [];
  const first = { file: 'first', name: 'First', isActive: true };
  const second = { file: 'second', name: 'Second', isActive: false };
  const context = vm.createContext({
    modal: null, modalDecline: null, panel: {}, switching: false, restarting: false,
    accountsRevision: 0, accountsSnapshot: [first, second], activeFile: 'first', MODAL_ID: 'modal',
    API_URL: '/accounts', RESTART_URL: '/restart', Date,
    document: { body: new Element(), createElement: () => new Element(), addEventListener() {}, removeEventListener() {} },
    window: { __claudeSessionId: () => 'session' }, logInfo() {},
    closePanel() { context.panel = null; },
    rememberAccounts(accounts) { context.accountsSnapshot = accounts; context.activeFile = accounts.find(a => a.isActive)?.file; },
    waitForAck() { context.ackStarted = true; },
    fetch: (url, options) => new Promise((resolve, reject) => pending.push({ url, options, resolve, reject })),
  });
  vm.runInContext(['findAccount', 'switchTo', 'closeModal', 'onModalKeydown', 'modalRow', 'openModal', 'setStatus', 'requestRestart'].map(extract).join('\n'), context);
  context.switchTo('second', {}, {});
  const box = context.modal.children[0];
  const footer = box.children.find(c => c.className === 'claude-accs-modal-foot');
  return { context, pending, box, cancel: footer.children[0], go: footer.children[1] };
}
async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
async function respond(request, value) { request.resolve({ json: async () => value }); await flush(); }
const switched = { ok: true, accounts: [{ file: 'first', isActive: false }, { file: 'second', isActive: true }] };
(async () => {
  for (const cancelBy of ['button', 'escape', 'outside']) {
    const h = setup();
    assert.equal(h.box.children[0].textContent, 'Переключение аккаунта');
    assert.equal(h.go.textContent, 'Переключить аккаунт');
    assert.equal(h.pending.length, 0, 'Opening the warning must not write settings');
    if (cancelBy === 'button') h.cancel.click();
    if (cancelBy === 'escape') h.context.onModalKeydown({ key: 'Escape', preventDefault() {}, stopPropagation() {} });
    if (cancelBy === 'outside') h.context.modal.listeners.mousedown({ target: h.context.modal });
    assert.equal(h.context.modal, null);
    assert.equal(h.context.activeFile, 'first');
    assert.equal(h.pending.length, 0, 'Cancellation must not issue switch or rollback requests');
  }
  const h = setup();
  h.go.click(); h.go.click(); h.cancel.click();
  assert.equal(h.pending.length, 1, 'Double click sends only one switch');
  assert(h.context.modal, 'Cannot cancel an in-flight switch');
  const change = h.pending.shift();
  assert.equal(change.url, '/accounts');
  assert.equal(JSON.parse(change.options.body).file, 'second');
  assert.equal(h.context.activeFile, 'first');
  await respond(change, switched);
  assert.equal(h.context.activeFile, 'second');
  assert.equal(h.pending.length, 1);
  const restart = h.pending.shift();
  assert.equal(restart.url, '/restart', 'Restart is requested only after the successful account switch');
  assert.equal(JSON.parse(restart.options.body).sessionId, 'session');
  await respond(restart, { ok: true, token: 'restart' });
  assert.equal(h.context.ackStarted, true);

  const failed = setup();
  failed.go.click();
  await respond(failed.pending.shift(), { ok: false, error: 'test failure' });
  assert.equal(failed.pending.length, 0, 'Failed switch never restarts the extension');
  assert.equal(failed.context.activeFile, 'first');
  assert.equal(failed.go.disabled, false);
  failed.cancel.click();
  assert.equal(failed.pending.length, 0);

  const retry = setup();
  retry.go.click();
  await respond(retry.pending.shift(), switched);
  retry.pending.shift().reject(new Error('restart unavailable'));
  await flush();
  retry.go.click();
  assert.equal(retry.pending[0].url, '/restart', 'Retry does not switch the already-applied account again');
  retry.pending.shift().reject(new Error('still unavailable'));
  await flush();
  retry.cancel.click();
  const revert = retry.pending.shift();
  assert.deepEqual(JSON.parse(revert.options.body), { file: 'first', revert: true });
  await respond(revert, { ok: true, accounts: [{ file: 'first', isActive: true }] });
  assert.equal(retry.context.activeFile, 'first');
  assert.equal(retry.context.modal, null);
  console.log('Account confirmation: no early mutations, cancellation, switch/restart order, double clicks, failure and rollback: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
