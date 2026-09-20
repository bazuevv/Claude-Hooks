const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const moduleSource = source.slice(source.indexOf(' * IMAGE ANNOTATION EDITOR'));
function extract(name) {
  const start = moduleSource.indexOf('  function ' + name + '(');
  assert(start >= 0, name);
  return moduleSource.slice(start, moduleSource.indexOf('\n  }', start) + 4);
}
const documentEvents = new Map();
const overlay = {};
let setZoom;
let attachments = 1;
const preview = {
  children: [],
  getBoundingClientRect: () => ({ left: 200, top: 100 }),
  querySelector: () => null,
  appendChild(el) { this.children.push(el); },
};
const image = {
  draggable: true, style: {}, dataset: {}, events: new Map(),
  closest: () => overlay,
  addEventListener(type, listener) {
    if (!this.events.has(type)) this.events.set(type, []);
    this.events.get(type).push(listener);
  },
  getBoundingClientRect() {
    const state = preview.__claudeImageZoom || { value: 1, panX: 0, panY: 0 };
    const left = 200 + state.panX;
    const top = 100 + state.panY;
    return { left, top, right: left + 300 * state.value, bottom: top + 240 * state.value,
      width: 300 * state.value, height: 240 * state.value };
  },
  setPointerCapture() {}, releasePointerCapture() {},
};
const context = vm.createContext({
  EDIT_CLASS: 'edit', MIN_ZOOM: 0.25, MAX_ZOOM: 4,
  window: { innerWidth: 1000, innerHeight: 700, __claudeDomWatch: { register() {} } },
  document: { documentElement: {}, addEventListener(type, listener, capture) {
    assert.equal(capture, true, 'Intercept preview drops before the native React handler');
    documentEvents.set(type, listener);
  } },
  scan() {},
  zoomControls(cls, getZoom, setter) { setZoom = setter; return { style: {}, update() {} }; },
});
vm.runInContext(['clampZoom', 'wheelZoom', 'touchDistance', 'touchCenter', 'installPreviewZoom',
  'blockPreviewAttachmentDrag', 'init'].map(extract).join('\n'), context);
function event(type, extra = {}) {
  return { type, target: image, defaultPrevented: false, stopped: false,
    pointerType: 'mouse', pointerId: 1, button: 0, clientX: 250, clientY: 150,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.stopped = true; },
    stopImmediatePropagation() { this.stopped = true; }, ...extra };
}
function dispatch(type, extra) {
  const ev = event(type, extra);
  if (documentEvents.has(type)) documentEvents.get(type)(ev);
  if (!ev.stopped) for (const handler of image.events.get(type) || []) handler(ev);
  // The native session onDrop appends every image received through the portal.
  if (type === 'drop' && !ev.stopped) attachments++;
  return ev;
}

dispatch('drop');
assert.equal(attachments, 2, 'An unguarded preview drop reaches the attachment handler');
attachments = 1;
context.init();
assert.equal(dispatch('dragstart').defaultPrevented, true, 'Protection is active even before the first preview scan');
context.installPreviewZoom(preview, image);
assert.equal(image.draggable, false);
context.installPreviewZoom(preview, image);
assert.equal(preview.children.length, 1, 'Repeated scans keep one set of zoom controls');
for (const zoom of [1, 1.2, 2, 4]) {
  setZoom(zoom);
  for (let i = 0; i < 5; i++) {
    dispatch('pointerdown');
    assert.equal(dispatch('dragstart').defaultPrevented, true);
    dispatch('pointermove', { clientX: 275, clientY: 180 });
    dispatch('pointerup');
    assert.equal(dispatch('dragover').stopped, true);
    assert.equal(dispatch('drop').stopped, true);
  }
  assert.equal(attachments, 1, `Zoom ${zoom} must not duplicate the image`);
}
assert(preview.__claudeImageZoom.panX !== 0, 'Custom panning still works when the enlarged image overflows');
dispatch('wheel', { deltaY: 300 });
assert(preview.__claudeImageZoom.value < 4, 'Wheel zoom still works');
const beforePinch = preview.__claudeImageZoom.value;
dispatch('pointerdown', { pointerType: 'touch', pointerId: 2, clientX: 100 });
dispatch('pointerdown', { pointerType: 'touch', pointerId: 3, clientX: 200 });
dispatch('pointermove', { pointerType: 'touch', pointerId: 3, clientX: 250 });
assert(preview.__claudeImageZoom.value > beforePinch, 'Touch pinch still works');
dispatch('pointerup', { pointerType: 'touch', pointerId: 2 });
dispatch('pointerup', { pointerType: 'touch', pointerId: 3 });
for (const target of [
  { closest: () => overlay }, // backdrop, close button, toolbar
  { nodeType: 3, parentElement: { closest: () => overlay } },
]) assert.equal(dispatch('drop', { target }).stopped, true);
assert.equal(attachments, 1);
const normalDrop = dispatch('drop', { target: { closest: () => null } });
assert.equal(normalDrop.defaultPrevented, false);
assert.equal(attachments, 2, 'Dropping files into the regular chat remains available');
console.log('Preview: native drag/drop cannot duplicate attachments at any zoom; pan, pinch and normal chat drops preserved: OK');
