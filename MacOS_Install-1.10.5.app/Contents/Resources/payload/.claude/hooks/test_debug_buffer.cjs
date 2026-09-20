const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const functions = source.slice(source.indexOf('  function createDebugBuffer('),
  source.indexOf('  function ensureDebugOverlay('));
const nodes = new Map();
let writes = 0;
function node(tag) {
  const values = {};
  const n = {tag, style:{}, children:[], attrs:{},
    appendChild(child) { this.children.push(child); nodes.set(child.id, child); },
    setAttribute(key, value) { this.attrs[key] = value; }};
  for (const key of ['textContent', 'max', 'value']) Object.defineProperty(n, key, {
    get:()=>values[key], set:value=>{ values[key]=value; writes++; }
  });
  return n;
}
const stats = {animate_sentences:true, animation_queue:120, animation_ready_queue:100,
  animation_waiting_sentence:20, animation_queue_peak:200, animation_rate:50,
  animation_min_rate:50, animation_buffer_target:500};
const ctx = vm.createContext({window:{__claudeMarkdownRender:stats},
  document:{createElement:node, getElementById:id=>nodes.get(id)}});
vm.runInContext(functions,ctx);
ctx.createDebugBuffer(node('overlay'));
ctx.updateDebugBuffer();
const get = suffix=>nodes.get('claude-custom-debug-buffer'+suffix);
assert.equal(get('-meter').tag,'progress');
assert.equal(get('-meter').value,120);
assert.equal(get('-meter').max,200);
assert.match(get('-value').textContent,/120/);
assert.match(get('-detail').textContent,/100.*20/);
assert.match(get('-detail').textContent,/50 симв\/с/);
assert.match(get('-detail').textContent,/500 симв/);
for(const n of nodes.values()) assert.equal(n.__claudeOwnNode,true,'Ignore diagnostic DOM mutations');
const stableWrites=writes;
ctx.updateDebugBuffer();
assert.equal(writes,stableWrites,'Do not rewrite unchanged diagnostics');
stats.animation_queue=0;stats.animation_ready_queue=0;stats.animation_waiting_sentence=0;
ctx.updateDebugBuffer();
assert.equal(get('-meter').value,0);
assert.match(get('-detail').textContent,/Скорость: 0/);
stats.animate_sentences=false;ctx.updateDebugBuffer();
assert.match(get('-detail').textContent,/отключена/);
assert.match(source,/if \(DEBUG_OVERLAY_ENABLED\) \{\s+ensureDebugOverlay\(\)/);
console.log('Debug buffer: numeric remainder, scale, empty/disabled, own nodes, unchanged DOM: OK');
