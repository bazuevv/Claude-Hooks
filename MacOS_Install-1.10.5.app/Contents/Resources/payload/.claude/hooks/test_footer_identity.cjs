const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
function extract(name, from = 0) {
  const start = source.indexOf('  function ' + name + '(', from);
  assert(start >= 0, name);
  const end = source.indexOf('\n  }', start) + 4;
  return source.slice(start, end);
}
class Element {
  constructor(className = '') { this.className = className; this.children = []; }
  appendChild(el) { this.children.push(el); el.parentNode = this; }
  addEventListener() {}
  querySelector(sel) { return this.children.find(el => el.className.split(/\s+/).includes(sel.slice(1))) || null; }
  compareDocumentPosition(other) {
    if (this === other) return 0;
    return this.parentNode.children.indexOf(this) < this.parentNode.children.indexOf(other) ? 4 : 2;
  }
  insertBefore(el, right) {
    this.moves = (this.moves || 0) + 1;
    if (el === right) return;
    this.children.splice(this.children.indexOf(el), 1);
    this.children.splice(this.children.indexOf(right), 0, el);
  }
}
const context = { document: { createElement: () => new Element() },
  ROOT_CLASS: 'claude-mood', createSvg: () => new Element(), applyTo() {}, logInfo() {} };
vm.createContext(context);
const helperStart = source.indexOf('function claudeNativeButtonClasses(');
if (helperStart >= 0) vm.runInContext(source.slice(helperStart, source.indexOf('\n}', helperStart) + 2), context);
vm.runInContext(extract('createGauge'), context);
const moodStart = source.indexOf('  function createGauge(');
vm.runInContext(extract('leftmostNeighbour', moodStart) + '\n' + extract('ensureOrder', moodStart), context);
for (const donorClasses of ['footerButton_hash', 'footerButton_hash claude-accs-btn',
                            'footerButton_hash claude-usage-btn claude-bypass-btn']) {
  const gauge = context.createGauge(new Element(donorClasses));
  assert.equal(gauge.className, 'footerButton_hash claude-mood', 'Mood must not impersonate its style donor');
  const footer = new Element();
  footer.appendChild(gauge);
  footer.appendChild(new Element('footerButton_hash claude-accs-btn'));
  for (let i = 0; i < 100; i++) context.ensureOrder(footer);
  assert.equal(footer.moves || 0, 0, 'Stable footer must not generate more mutations');
}
console.log('Footer identity and stable repeated scans: OK');
