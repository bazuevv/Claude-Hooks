const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const patch = source.slice(source.indexOf('// === LAZY COLLAPSED THINKING ==='),
  source.indexOf('// === END LAZY COLLAPSED THINKING ==='));
// Optional installed bundle exercises the exact native component, without
// executing the rest of the extension or requiring its browser environment.
const bundle = process.argv[2] && fs.readFileSync(process.argv[2], 'utf8');
const native = bundle ? bundle.slice(bundle.indexOf('function rf1('), bundle.indexOf('function TL0(')) : `
function rf1({thinkingBlock: block, isExpanded, onToggle}) {
  const [expanded, setExpanded] = l(false);
  const open = isExpanded !== undefined ? isExpanded : expanded;
  const toggle = onToggle !== undefined ? onToggle : setExpanded;
  if (!block.thinking || !block.thinking.trim()) return F('div', {children:'Thinking'});
  return R('details', {open:open, onToggle:e=>toggle(e.target.open), children:[
    F('summary', {children:'Thinking'}),
    F('div', {className:TX.thinkingContent, children:F(r$, {content:block.thinking})})
  ]});
}`;
assert.match(native, /^\s*function rf1\(/);
function harness(config = {}, component = native) {
  let expanded = false, hooks = 0;
  const parsed = [];
  const jsx = (type, props, key = null) => Object.freeze({ type, props:Object.freeze(props), key });
  const ctx = vm.createContext({
    window:{__CLAUDE_CUSTOM_CONFIG__:{lazyCollapsedThinking:true, ...config}},
    F:jsx, R:jsx, l:() => { hooks++; return [expanded, value => { expanded = value; }]; },
    TX:{thinking:'thinking', thinkingV2:'v2', thinkingSummary:'summary',
      thinkingStatic:'static', thinkingContent:'content', thinkingToggle:'toggle'},
    r$:props => { parsed.push(props.content); return null; },
    TL0:() => null, tH:() => null,
  });
  vm.runInContext(component, ctx);
  const original = ctx.rf1;
  vm.runInContext(patch, ctx);
  function mount(element) {
    if (!element || typeof element !== 'object') return;
    if (typeof element.type === 'function') return mount(element.type(element.props));
    const children = element.props.children;
    (Array.isArray(children) ? children : [children]).forEach(mount);
  }
  function render(text, extra = {}) {
    const element = ctx.rf1({thinkingBlock:{thinking:text}, context:{}, durationMillis:null, ...extra});
    mount(element);
    return element;
  }
  return {ctx, original, parsed, render, hooks:()=>hooks};
}
const h = harness();
assert.equal(h.ctx.window.__claudeCollapsedThinking.state, 'installed');
let tree = h.render('a'.repeat(50000));
assert.equal(tree.props.open, false);
assert.equal(tree.props.children[0].type, 'summary');
assert.equal(h.parsed.length, 0, 'Collapsed text must not reach Markdown');
tree = h.render('new text');
assert.equal(h.parsed.length, 0);
assert.equal(h.hooks(), 2, 'Native hooks must run on every render');
tree.props.onToggle({target:{open:true}});
tree = h.render('latest text');
assert.equal(tree.props.open, true);
assert.deepEqual(h.parsed, ['latest text']);
tree.props.onToggle({target:{open:false}});
h.render('later text');
assert.deepEqual(h.parsed, ['latest text']);
tree.props.onToggle({target:{open:true}});
h.render('reopened text');
assert.deepEqual(h.parsed, ['latest text', 'reopened text']);
let controlled = null;
tree = h.render('controlled', {isExpanded:false, onToggle:value=>{controlled=value;}});
tree.props.onToggle({target:{open:true}});
assert.equal(controlled, true);
h.render('controlled current', {isExpanded:true});
assert.equal(h.parsed.at(-1), 'controlled current');
const before = h.parsed.length;
h.render('');
assert.equal(h.parsed.length, before, 'Empty native summary remains supported');
for (const config of [{lazyCollapsedThinking:false}, {safeMode:true}]) {
  const disabled = harness(config);
  assert.equal(disabled.ctx.rf1, disabled.original);
  disabled.render('native behavior');
  assert.deepEqual(disabled.parsed, ['native behavior']);
}
const unsupported = harness({}, 'function rf1(){return null}');
assert.equal(unsupported.ctx.rf1, unsupported.original);
assert.equal(unsupported.ctx.window.__claudeCollapsedThinking.state, 'unsupported-bundle');
console.log('Collapsed thinking: no hidden Markdown, latest text on reopen, native state, disabled and unsupported: OK' +
  (bundle ? ' (installed native component)' : ''));
