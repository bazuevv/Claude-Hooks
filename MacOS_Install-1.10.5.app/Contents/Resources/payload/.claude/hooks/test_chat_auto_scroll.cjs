const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const code = source.slice(source.indexOf('// === CHAT AUTO SCROLL ==='),
  source.indexOf('// === END CHAT AUTO SCROLL ==='));
function harness(config = {chatAutoScroll:true}) {
  let scan, id=0;
  const frames=new Map(), resizes=[], mutations=[];
  class Observer {
    constructor(callback) { this.callback=callback; this.targets=[]; }
    observe(target) { this.targets.push(target); }
    disconnect() { this.targets=[]; }
  }
  const ctx=vm.createContext({
    window:{__CLAUDE_CUSTOM_CONFIG__:config, __claudeDomWatch:{register:(_, fn)=>{scan=fn;}}},
    ResizeObserver:class extends Observer { constructor(fn){super(fn);resizes.push(this);} },
    MutationObserver:class extends Observer { constructor(fn){super(fn);mutations.push(this);} },
    requestAnimationFrame:fn=>{frames.set(++id,fn);return id;},
    cancelAnimationFrame:key=>frames.delete(key),
  });
  vm.runInContext(code,ctx);
  function container(initial=800) {
    const listeners={};let top=initial;
    return {scrollHeight:1000,clientHeight:200,isConnected:true,children:Array.from({length:20},()=>({})),
      get scrollTop(){return top;},set scrollTop(value){top=Math.max(0,Math.min(value,this.scrollHeight-this.clientHeight));},
      addEventListener:(name,fn)=>{listeners[name]=fn;},removeEventListener:name=>{delete listeners[name];},
      emit:(name,event)=>listeners[name]?.(event),listeners};
  }
  return {ctx,container,resizes,mutations,frames,scan:inputs=>scan?.({inputs}),hasScan:()=>!!scan,
    tick:()=>{const batch=[...frames.values()];frames.clear();batch.forEach(fn=>fn());}};
}
const h=harness(), box=h.container();
const input={closest:()=>({querySelector:()=>box})};
h.scan([input]);h.tick();
assert.equal(h.resizes[0].targets.length,5,'Observe only container and four tail boxes');
box.scrollHeight=1500;h.resizes[0].callback();h.resizes[0].callback();
assert.equal(h.frames.size,1,'Coalesce layout notifications');
h.tick();assert.equal(box.scrollTop,1300,'Follow rendered content growth');
box.emit('wheel',{deltaY:-100});box.scrollTop=1100;box.emit('scroll');
box.scrollHeight=1700;h.resizes[0].callback();h.tick();
assert.equal(box.scrollTop,1100,'Reading earlier text must not be interrupted');
h.ctx.window.__claudeResumeChatScroll(box);h.tick();
assert.equal(box.scrollTop,1500,'End button resumes following');
box.scrollTop=1000;box.emit('scroll'); // Scrollbar/keyboard, no wheel event.
box.scrollHeight=1800;h.resizes[0].callback();h.tick();
assert.equal(box.scrollTop,1000);
box.scrollTop=1600;box.emit('scroll');
box.scrollHeight=1900;h.resizes[0].callback();h.tick();
assert.equal(box.scrollTop,1700,'Manual return to the end resumes following');
box.scrollHeight=2000;h.resizes[0].callback();h.scan([]);
assert.equal(h.frames.size,0);
assert.equal(h.resizes[0].targets.length,0);
assert.equal(Object.keys(box.listeners).length,0,'Navigation removes listeners');
const saved=h.container(100);h.ctx.claudeFollowChatEnd(saved);h.tick();
assert.equal(saved.scrollTop,100,'Do not overwrite restored history position');
assert.equal(harness({chatAutoScroll:false}).hasScan(),false);
assert.equal(harness({chatAutoScroll:true,safeMode:true}).hasScan(),false);
console.log('Chat auto scroll: growth, bounded observers, manual pause/resume, navigation, disabled: OK');
