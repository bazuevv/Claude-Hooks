const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const patch = source.slice(source.indexOf('// === USER MESSAGE OUTSIDE COLLAPSE ==='),
  source.indexOf('// === END USER MESSAGE OUTSIDE COLLAPSE ==='));
function setup(config = {}) {
  const handlers = {mousedown:[],click:[],dblclick:[]}, attrs = {}, styles = {}, messages = [];
  const sizes={userMessageInitialHeightPx:60,userMessageMediumHeightPx:200,userMessageMaxHeightPx:400,...config};
  function makeSelection(node) {
    return {isCollapsed:!node,anchorNode:node,focusNode:node,removed:0,
      removeAllRanges(){this.isCollapsed=true;this.removed++;}};
  }
  let selection=makeSelection(null);
  let tick=0;
  const document = {
    documentElement:{setAttribute:(k,v)=>{attrs[k]=v;},style:{setProperty:(k,v)=>{styles[k]=v;}}},
    addEventListener:(name,fn,capture)=>{assert.ok(handlers[name]);assert.equal(capture,true);handlers[name].push(fn);},
    querySelectorAll:()=>messages.map(m=>m.less).filter(b=>b.isConnected),
  };
  const ctx = vm.createContext({window:{__CLAUDE_CUSTOM_CONFIG__:{userMessageOutsideCollapse:true,...sizes},
    getSelection:()=>selection},document});
  vm.runInContext(patch,ctx);
  function dispatch(name,target, options={}) {
    const event={target,button:0,detail:1,clientX:10,timeStamp:++tick,defaultPrevented:false,...options,
      preventDefault(){this.defaultPrevented=true;},stopPropagation:()=>assert.fail('Do not block clicked controls')};
    handlers[name].forEach(fn=>fn(event));
    return event;
  }
  const click=(target,options={})=>{
    const event=dispatch('click',target,options);
    assert.equal(event.defaultPrevented,false,'Ordinary clicks retain their native action');
    return event;
  };
  const mouseDown=(target,options={})=>dispatch('mousedown',target,options);
  const doubleClick=(target,options={})=>{
    const firstDown=mouseDown(target,{detail:1,...options});
    click(target,options);
    const secondDown=mouseDown(target,{detail:2,...options});
    click(target,{detail:2,...options});
    const doubleEvent=dispatch('dblclick',target,{detail:2,...options});
    return {firstDown,secondDown,doubleEvent};
  };
  function message(height=800, initiallyOpen=false) {
    let expanded=initiallyOpen;
    const data={},inside=new Set(),root={};
    const bubble={contains:n=>inside.has(n),closest:()=>root,querySelector:()=>frame};
    const node=(interactive=false)=>{
      const n={nodeType:1,closest:s=>s.startsWith('a,')?(interactive?n:null):bubble};inside.add(n);return n;
    };
    const text=node(),scrollbar=node(),padding=node(),link=node(true);
    const content={scrollHeight:height,clientLeft:0,clientWidth:94,
      get clientHeight(){return Math.min(height,expanded?(data['data-claude-message-height']==='max'?sizes.userMessageMaxHeightPx:sizes.userMessageMediumHeightPx):sizes.userMessageInitialHeightPx);},
      getBoundingClientRect:()=>({left:0,right:100}),contains:n=>n===text||n===scrollbar||n===link};
    const frame={setAttribute:(k,v)=>{data[k]=v;},removeAttribute:k=>{delete data[k];},getAttribute:k=>data[k]||null,
      querySelector:s=>s.startsWith(':scope')?content:s.includes('expandButton_')?(more.isConnected?more:null):(less.isConnected?less:null)};
    function button(open) {
      const b={calls:0,get isConnected(){return height>60 && expanded!==open;},
        closest:s=>s.includes('expandableContainer_')?frame:bubble,
        click(){this.calls++;click(this,{detail:0});expanded=open;}};
      inside.add(b);return b;
    }
    const more=button(true),less=button(false);
    const m={text,scrollbar,padding,link,more,less,content,
      get stage(){return expanded?(data['data-claude-message-height']==='max'?sizes.userMessageMaxHeightPx:sizes.userMessageMediumHeightPx):sizes.userMessageInitialHeightPx;}};
    messages.push(m);return m;
  }
  return {ctx,attrs,styles,handlers,click,mouseDown,doubleClick,message,
    getSelection:()=>selection,select:n=>{selection=makeSelection(n);}};
}
const h=setup(),long=h.message();
assert.equal(h.attrs['data-claude-user-message-collapse'],'outside');
assert.equal(h.styles['--claude-user-message-initial-height'],'60px');
assert.equal(h.styles['--claude-user-message-medium-height'],'200px');
assert.equal(h.styles['--claude-user-message-max-height'],'400px');
h.click(long.text);assert.equal(long.stage,200);assert.equal(long.more.calls,1);
h.click(long.scrollbar,{clientX:97});assert.equal(long.stage,200,'Scrollbar does not advance the stage');
h.select(long.text);h.click(long.text);assert.equal(long.stage,200,'Selecting text does not resize');h.select(null);
h.click(long.link);h.click(long.text,{button:2});h.click(long.text,{detail:2});
assert.equal(long.stage,200,'Links, right click and second click event do not resize');
h.click(long.padding);assert.equal(long.stage,400);
h.click(long.text);assert.equal(long.stage,60);assert.equal(long.less.calls,1);
h.click(long.text);assert.equal(long.stage,200,'New cycle starts at 200');
h.click(long.text);assert.equal(long.stage,400);
h.click({});assert.equal(long.stage,60,'Outside click collapses the 400px stage');
h.click(long.text);assert.equal(long.stage,200,'Outside click clears the large-height marker');
const otherText={};
h.select(long.text);
h.mouseDown(otherText);
h.select(null); // Native mousedown clears the selection before click.
h.click(otherText);
assert.equal(long.stage,200,'Clicking other text after a selection does not collapse the message');
h.click(otherText);
assert.equal(long.stage,60,'A later outside click still collapses it');
h.click(long.text);
const medium=h.message(150);h.click(medium.text);
assert.equal(long.stage,60);assert.equal(medium.stage,200);
h.select(medium.text);
h.mouseDown(long.text);
h.select(null);
h.click(long.text);
assert.equal(medium.stage,200,'Selection also protects an expanded message when another user message is clicked');
assert.equal(long.stage,60,'Clearing a selection does not expand the clicked message');
h.click(long.text);
assert.equal(medium.stage,60,'The next click resumes normal outside collapse');
h.click(medium.text);
h.click(medium.text);assert.equal(medium.stage,60,'Skip 400 when the text fits at 200');
const short=h.message(40);h.click(short.text);
assert.equal(short.stage,60);assert.equal(short.more.calls,0,'Text fitting the original size stays unchanged');
const dbl=h.message(800);
const firstDouble=h.doubleClick(dbl.text);
assert.equal(dbl.stage,400,'Double click opens the maximum height from initial');
assert.equal(firstDouble.firstDown.defaultPrevented,false,'First mouse down allows ordinary selection');
assert.equal(firstDouble.secondDown.defaultPrevented,true,'Second mouse down prevents word selection');
assert.equal(firstDouble.doubleEvent.defaultPrevented,true,'Double click prevents native word selection');
h.doubleClick(dbl.text);assert.equal(dbl.stage,400,'Double click retains maximum height when already open');
h.click(dbl.text);assert.equal(dbl.stage,60,'A single click after double click collapses');
h.click(dbl.text);assert.equal(dbl.stage,200);
h.doubleClick(dbl.text);assert.equal(dbl.stage,400,'Double click opens maximum height from medium');
const linkDouble=h.doubleClick(dbl.link);
assert.equal(dbl.stage,400,'Double click on a link keeps its native action');
assert.equal(linkDouble.secondDown.defaultPrevented,false);
assert.equal(linkDouble.doubleEvent.defaultPrevented,false);
const scrollbarDouble=h.doubleClick(dbl.scrollbar,{clientX:97});
assert.equal(dbl.stage,400,'Double click on scrollbar leaves size unchanged');
assert.equal(scrollbarDouble.secondDown.defaultPrevented,false);
assert.equal(scrollbarDouble.doubleEvent.defaultPrevented,false);
h.select(dbl.text);
h.doubleClick(dbl.text);
assert.equal(h.getSelection().isCollapsed,true,'Any selected word inside the expanded message is cleared');
assert.equal(h.getSelection().removed,1);
const a=h.message(800,true),b=h.message(800,true);h.click({});
assert.equal(a.less.calls,1);assert.equal(b.less.calls,1,'Multiple messages close once without recursive toggles');
vm.runInContext(patch,h.ctx);
assert.equal(h.handlers.mousedown.length,1,'Install only one delegated mouse down listener');
assert.equal(h.handlers.click.length,1,'Install only one delegated click listener');
assert.equal(h.handlers.dblclick.length,1,'Install only one delegated double click listener');
const custom=setup({userMessageInitialHeightPx:100,userMessageMediumHeightPx:260,userMessageMaxHeightPx:550});
const customMessage=custom.message(800);
custom.click(customMessage.text);assert.equal(customMessage.stage,260);
custom.doubleClick(customMessage.text);assert.equal(customMessage.stage,550);
custom.click(customMessage.text);assert.equal(customMessage.stage,100);
assert.equal(custom.styles['--claude-user-message-initial-height'],'100px');
const fitsCustomInitial=custom.message(80);
custom.click(fitsCustomInitial.text);
assert.equal(fitsCustomInitial.stage,100,'Text fitting the configured initial height stays unchanged');
assert.equal(fitsCustomInitial.more.calls,0);
const invalid=setup({userMessageInitialHeightPx:40,userMessageMediumHeightPx:20,userMessageMaxHeightPx:10});
assert.equal(invalid.styles['--claude-user-message-initial-height'],'60px','Invalid heights fall back together');
for(const cfg of [{safeMode:true},{userMessageOutsideCollapse:false}]) {
  const off=setup(cfg);
  assert.equal(off.handlers.mousedown.length+off.handlers.click.length+off.handlers.dblclick.length,0);
  assert.equal(off.attrs['data-claude-user-message-collapse'],undefined,'Keep native Show less when disabled');
}
console.log('User message height: configurable cycle, double click without selection, outside reset, scrollbar/links, disabled: OK');
