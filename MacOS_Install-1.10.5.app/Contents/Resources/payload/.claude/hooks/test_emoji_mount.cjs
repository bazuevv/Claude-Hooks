const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const start = source.indexOf('  function mountNearMic(');
const end = source.indexOf('  function mountInFooter(', start);
let micPresent = false;
let moves = 0;
const input = { classList: { add() {} } };
const mic = { closest() { return input; } };
const wrapper = {
  classList: { add() {} },
  querySelector() { return mic; },
  insertBefore(button, next) {
    moves++;
    button.parentNode = this;
    button.nextSibling = next;
  },
};
const container = { querySelector() { return micPresent ? wrapper : null; } };
const button = { parentNode: {}, nextSibling: null };
const context = {
  BTN_CLASS: 'claude-emoji-btn',
  createButton() { throw new Error('Existing button must be reused'); },
  logInfo() {},
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
assert.equal(context.mountNearMic(container, button), false);
micPresent = true;
assert.equal(context.mountNearMic(container, button), true);
assert.equal(button.parentNode, wrapper);
for (let i = 0; i < 100; i++) context.mountNearMic(container, button);
assert.equal(moves, 1, 'Repeated scans must not mutate the DOM');
console.log('Late microphone mount and idempotent placement: OK');

let appends = 0;
let standalone = null;
const inputWrap = {
  classList: { add() {} },
  querySelector() { return standalone; },
  appendChild(el) { appends++; standalone = el; el.parentNode = this; },
};
context.document = { createElement() {
  return { appendChild(el) { appends++; el.parentNode = this; } };
} };
const noMicContainer = { querySelector() { return inputWrap; } };
const fallbackButton = { parentNode: null };
assert.equal(context.mountInInput(noMicContainer, fallbackButton), true);
assert.equal(fallbackButton.parentNode, standalone);
assert.equal(standalone.parentNode, inputWrap);
for (let i = 0; i < 100; i++) context.mountInInput(noMicContainer, fallbackButton);
assert.equal(appends, 2, 'Missing microphone must not cause repeated mounting');
console.log('Input placement without microphone: OK');
