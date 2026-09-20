const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
function extract(name) {
  const start = source.indexOf('  function ' + name + '(');
  return source.slice(start, source.indexOf('\n  }', start) + 4);
}
class Element {
  constructor() { this.children = []; this.events = {}; this.attrs = {}; }
  appendChild(el) { this.children.push(el); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(key, fn) { this.events[key] = fn; }
  querySelector(selector) { return this.children.find(el => (el.className || '').split(' ').includes(selector.slice(1))) || null; }
}
const preview = new Element();
const original = { src: 'data:image/jpeg;base64,b3JpZ2luYWw=', alt: 'photo.jpg', naturalWidth: 1600, naturalHeight: 900 };
let canvas;
let reply = { ok: true };
const requests = [];
const timers = new Map();
let nextTimer = 0;
const context = vm.createContext({
  own: el => el, previewImageOf: () => original,
  document: { createElement(tag) {
    if (tag !== 'canvas') return new Element();
    canvas = { getContext: () => ({ drawImage(image, x, y) { assert.equal(image, original); assert.equal(x, 0); assert.equal(y, 0); } }),
      toDataURL: type => { assert.equal(type, 'image/png'); return 'data:image/png;base64,cGl4ZWxz'; } };
    return canvas;
  } },
  fetch: async (url, options) => { requests.push(JSON.parse(options.body)); return { json: async () => reply }; },
  setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer; },
  clearTimeout(id) { timers.delete(id); },
});
vm.runInContext(extract('button') + extract('exportPreviewImage') + extract('addImageExportButtons'), context);
const event = { detail: 0, preventDefault() {}, stopPropagation() {} };
async function flush() { for (let i = 0; i < 10; i++) await Promise.resolve(); }
(async () => {
  context.addImageExportButtons(preview);
  context.addImageExportButtons(preview);
  assert.equal(preview.children.length, 2, 'No duplicate controls after scan');
  const [copy, save] = preview.children;
  copy.events.click({ ...event, detail: 1 });
  assert.equal(requests.length, 0, 'Opening the preview must not trigger an export');
  copy.events.click(event);
  assert.equal(copy.disabled, true);
  await flush();
  assert.equal(canvas.width, 1600);
  assert.equal(canvas.height, 900);
  assert.equal(requests[0].data_url, 'data:image/png;base64,cGl4ZWxz');
  assert.equal(copy.disabled, false);
  assert.equal(copy.textContent, '✓');
  save.events.click(event);
  await flush();
  assert.equal(requests[1].data_url, original.src, 'Save retains the original encoding');
  assert.equal(requests[1].name, 'photo.jpg');
  assert.equal(save.attrs['data-saved'], 'true');
  for (const fn of timers.values()) fn();
  timers.clear();
  assert.equal(save.attrs['data-saved'], 'true', 'Saved color survives the temporary status message');
  const reopened = new Element();
  context.addImageExportButtons(reopened);
  assert.equal(reopened.children[1].attrs['data-saved'], undefined, 'Reopening resets the saved color');
  reply = { ok: true, cancelled: true };
  reopened.children[1].events.click(event); await flush();
  assert.equal(reopened.children[1].attrs['data-saved'], undefined, 'Cancel must not mark the image saved');
  save.events.click(event); await flush();
  assert.equal(save.textContent, '↓', 'Cancelling is not reported as a successful save');
  reply = { ok: false, error: 'Clipboard busy' };
  copy.events.click(event); await flush();
  assert.equal(copy.disabled, false);
  assert.equal(copy.textContent, '!');
  assert.match(copy.title, /Clipboard busy/);
  console.log('Preview export: explicit click, full-resolution copy, original save, cancellation and errors: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
