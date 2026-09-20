const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../patches/claude-custom.js'), 'utf8');
const patch = source.slice(source.indexOf('// === COALESCED MARKDOWN ==='),
  source.indexOf('// === END COALESCED MARKDOWN ==='));
const bundle = process.argv[2] && fs.readFileSync(process.argv[2], 'utf8');
const native = bundle ? bundle.slice(bundle.indexOf('function r$('), bundle.indexOf('function jL0(')) :
  'function r$({content, isPartialText:partial}) { return {remarkPlugins:[], content}; }';
assert.match(native, /^function r\$\(/);
function harness(config = {}, component = native, scheduling = {}) {
  let clock = 0, cursor = 0, dirty = false, props, tree, timerId = 0;
  const slots = [], effects = [], timers = new Map(), committed = [];
  const equal = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const ctx = vm.createContext({
    window:{__CLAUDE_CUSTOM_CONFIG__:{markdownRenderIntervalMs:500, ...config}},
    performance:{now:()=>clock},
    F:(type, props, key)=>({type, props, key}),
    jK:{type:function Nn0(){}, compare:(a,b)=>a.content.key===b.content.key && a.context===b.context},
    l:function(value) { // useState
      const i = cursor++;
      if (!slots[i]) slots[i] = {value};
      return [slots[i].value, next=>{
        const value=typeof next==='function'?next(slots[i].value):next;
        if(!Object.is(value,slots[i].value)) { slots[i].value=value; dirty=true; }
      }];
    },
    U1:function(value) { // useRef
      const i = cursor++;
      if (!slots[i]) slots[i] = {current:value};
      return slots[i];
    },
    Q9:function(effect, deps) { // useLayoutEffect
      const i = cursor++;
      if (!slots[i] || !equal(slots[i].deps, deps)) {
        effects.push(()=>{
          if (slots[i]?.cleanup) slots[i].cleanup();
          slots[i] = {deps, cleanup:effect()};
        });
      }
    },
    b5:function(make, deps) { // useMemo
      const i = cursor++;
      if (!slots[i] || !equal(slots[i].deps, deps)) slots[i] = {deps, value:make()};
      return slots[i].value;
    },
    setTimeout:(fn, ms)=>{const id=++timerId; timers.set(id,{fn,at:clock+Math.max(ms,scheduling.timeoutFloor||0)}); return id;},
    clearTimeout:id=>timers.delete(id),
  });
  if(scheduling.frames) {
    ctx.document={visibilityState:'visible'};
    ctx.requestAnimationFrame=fn=>{
      const id=++timerId;timers.set(id,{fn:()=>fn(clock),at:clock+16,frame:true});return id;
    };
    ctx.cancelAnimationFrame=id=>timers.delete(id);
  }
  vm.runInContext(component, ctx);
  vm.runInContext(`function rf1(props) {
    return F('details', {children:[F('summary',{}),F('div',{
      children:F(r$, {content:props.thinkingBlock.thinking,context:props.context})})]});
  }`,ctx);
  ctx.yQ = function () {
    this.busy={value:true};this.messages={value:[]};this.claudeChannelId='test';
    this.sendTurnIdx=1;
    this.interruptCalls=0;
    this.connection={value:{interruptClaude:()=>this.interruptCalls++}};
  };
  const endTurn=bundle ? bundle.match(/endTurn\(\)\{this\.busy\.value=[^}]+\}/)[0]
    : 'endTurn(){this.busy.value=false}';
  const interrupt=bundle ? bundle.match(/async interrupt\(\)\{[^}]+\}/)[0]
    : 'async interrupt(){this.connection.value.interruptClaude(this.claudeChannelId)}';
  vm.runInContext('Object.assign(yQ.prototype, {'+endTurn+','+interrupt+'})',ctx);
  // Real resetPerProcessState invokes endTurn during process replacement.
  if(bundle) assert.match(bundle,/resetPerProcessState\(\)\{if\(this\.endTurn\(\)/);
  vm.runInContext('yQ.prototype.resetPerProcessState=function(){this.endTurn();this.claudeChannelId=undefined}',ctx);
  if (bundle) {
    ctx.zH = (type,compare)=>({type,compare});
    vm.runInContext(bundle.slice(bundle.indexOf('var jK=zH('), bundle.indexOf(';function Nn0(')), ctx);
    ctx.R=ctx.F;ctx.TX={};ctx.tH=function(){};ctx.TL0=function(){};
    vm.runInContext(bundle.slice(bundle.indexOf('function rf1('),bundle.indexOf('function TL0(')),ctx);
    ctx.Hv=()=>false;ctx.HX0=null;
    vm.runInContext(bundle.slice(bundle.indexOf('function Nn0('),bundle.indexOf('function BX0(')),ctx);
  } else {
    ctx.Nn0 = function Nn0(p) {
      if(p.content.content.type==='thinking') return ctx.F(ctx.rf1, {
        thinkingBlock:p.content.content,context:p.context,isCurrentlyThinking:p.content.isPartial,isExpanded:true});
      return ctx.F(ctx.r$, {content:p.content.content.text,context:p.context,isPartialText:p.content.isPartial});
    };
  }
  const original = ctx.r$;
  vm.runInContext(patch, ctx);
  function render(next = props) {
    props = next;
    let loops = 0;
    do {
      dirty=false; cursor=0;
      const nextTree = ctx.r$(props);
      if (tree !== nextTree) committed.push(nextTree);
      tree = nextTree;
      while (effects.length) effects.shift()();
      assert.ok(++loops < 10, 'No render loop');
    } while(dirty);
    return tree;
  }
  function advance(ms) {
    if (dirty) render();
    const target = clock+ms;
    while (true) {
      const next = [...timers].filter(([,t])=>t.at<=target &&
        !(t.frame && ctx.document.visibilityState==='hidden')).sort((a,b)=>a[1].at-b[1].at)[0];
      if (!next) break;
      clock=next[1].at; timers.delete(next[0]); next[1].fn();
      if (dirty) render();
    }
    clock=target;
  }
  return {ctx, original, render, advance, committed, timers,
    visibility:value=>{
      ctx.document.visibilityState=value;
      if(value==='visible') for(const t of timers.values()) if(t.frame) t.at=Math.max(t.at,clock+16);
    },
    stall:ms=>{clock+=ms;for(const t of timers.values()) t.at=Math.max(t.at,clock);},
    unmount:()=>slots.forEach(slot=>slot?.cleanup?.()), tree:()=>tree};
}
const context = {fileOpener:{}};
const props = (content, isPartialText)=>({content, context, isPartialText});
const h = harness();
assert.equal(h.ctx.window.__claudeMarkdownRender.state, 'installed');
const block = {content:{type:'text'}, key:1};
const first = h.ctx.F(h.ctx.jK, {content:block, context}, block.key);
block.key = 2;
const second = h.ctx.F(h.ctx.jK, {content:block, context}, block.key);
assert.equal(first.key, second.key, 'Streaming revision must not remount the component');
assert.equal(h.ctx.jK.compare(first.props, second.props), false, 'Mutable content must still invalidate memo');
const unchanged = h.ctx.F(h.ctx.jK, {content:block, context}, block.key);
assert.equal(h.ctx.jK.compare(second.props, unchanged.props), true);
const other = h.ctx.F(h.ctx.jK, {content:{content:{type:'text'},key:2},context}, 2);
assert.notEqual(first.key, other.key, 'Distinct blocks have distinct identities');
const tool = {content:{type:'tool_use'},key:7};
assert.equal(h.ctx.F(h.ctx.jK, {content:tool},7).key, 7);
assert.equal(h.ctx.F(h.ctx.jK, {content:block},'att-0').key, 'att-0');
assert.equal(h.render(props('start', true)).props.content, 'start');
// Sustained high-rate streaming must flush on schedule, not debounce forever.
let text = 'start';
for(let i=0;i<1500;i++) {
  h.advance(1); text+='a'; h.render(props(text, true));
}
assert.equal(h.committed.length, 4, '1500 chunks: initial render plus three timed renders');
h.advance(500);
assert.equal(h.tree().props.content, text, 'Trailing chunk flushes without any new input');
assert.equal(h.committed.length, 5);
const retained = h.tree();
for(let i=0;i<100;i++) h.render(props(text, true));
assert.equal(h.tree(), retained, 'Unchanged props reuse the native React subtree');
h.render(props(text+' final', false));
assert.equal(h.tree().props.content, text+' final', 'Completion flushes immediately');
assert.equal(h.timers.size, 0);
assert.equal(h.render(props('different message', false)).props.content, 'different message');
const thought = harness();
const faster = harness({markdownRenderIntervalMs:50, markdownShowPartialParagraph:true});
faster.render(props('start',true));
faster.render(props('start next',true));
faster.advance(49);
assert.equal(faster.tree().props.content,'start');
faster.advance(1);
assert.equal(faster.tree().props.content,'start next','Configured 50 ms interval is respected');
assert.equal(faster.tree().props.isPartialText,false,'Bypass only the native paragraph filter');
const streaming = props('first paragraph\n\nunfinished second paragraph',true);
faster.render(streaming);
assert.equal(streaming.isPartialText,true,'Do not change received streaming state');
assert.equal(faster.tree().props.content,streaming.content);
faster.render(props(streaming.content+' final',false));
assert.equal(faster.tree().props.content,streaming.content+' final','Completion still flushes immediately');
assert.equal(faster.timers.size,0);
const words = harness({markdownRenderIntervalMs:50,markdownShowPartialParagraph:true,markdownWholeWords:true});
const partial = props('Регистрация читателя (реб',true);
assert.equal(words.render(partial).props.content,'Регистрация читателя ');
const wordTree = words.tree();
words.render(props('Регистрация читателя (ребён',true));words.advance(50);
assert.equal(words.tree(),wordTree,'Growing hidden word must not reparse Markdown');
words.render(props('Регистрация читателя (ребёнок) ',true));words.advance(50);
assert.equal(words.tree().props.content,'Регистрация читателя (ребёнок) ');
assert.equal(partial.content,'Регистрация читателя (реб','Source text is not truncated');
assert.equal(words.render(props('ОдноСло',true)).props.content,'');
assert.equal(words.render(props('ОдноСлово',false)).props.content,'ОдноСлово','Final word without whitespace must appear immediately');
assert.equal(words.render(props('номер 12.3',true)).props.content,'номер ');
assert.equal(words.render(props('строка\nнов',true)).props.content,'строка\n');
assert.equal(words.render(props('emoji 😀сло',true)).props.content,'emoji ');
assert.equal(words.render(props('готово\u00a0',true)).props.content,'готово\u00a0');
const thinkingTree = words.ctx.rf1({thinkingBlock:{thinking:'думаю дал'},context,isCurrentlyThinking:true,isExpanded:true});
const thinkingProps = thinkingTree.props.children[1].props.children.props;
assert.equal(thinkingProps.__claudeStreaming,true);
assert.equal(words.render(thinkingProps).props.content,'думаю ');
const doneThinking = words.ctx.rf1({thinkingBlock:{thinking:'думаю далее'},context,isCurrentlyThinking:false,isExpanded:true});
words.render(doneThinking.props.children[1].props.children.props);
assert.equal(words.tree().props.content,'думаю далее','Thinking completion releases final word without waiting');
assert.equal(words.timers.size,0);
const sentences = harness({markdownRenderIntervalMs:50,markdownShowPartialParagraph:true,
  markdownWholeWords:true,markdownWholeSentences:true});
function sentenceChunk(text) {
  sentences.render(props(text,true));sentences.advance(50);
  return sentences.tree().props.content;
}
assert.equal(sentenceChunk('Первое предложение ещё пишется'),'','Sentence mode takes priority over words');
assert.equal(sentenceChunk('Первое предложение готово. Следующее ещё пишется'),'Первое предложение готово. ');
const sentenceTree=sentences.tree();
sentenceChunk('Первое предложение готово. Следующее ещё пишется дальше');
assert.equal(sentences.tree(),sentenceTree,'Incomplete sentence does not rebuild Markdown');
assert.equal(sentenceChunk('Первое предложение готово. Следующее готово! Третье'),'Первое предложение готово. Следующее готово! ');
assert.equal(sentenceChunk('Число 3.14 и сайт example.com ещё проверяются'),'');
assert.equal(sentenceChunk('То есть т. е. пока без конца'),'');
assert.equal(sentenceChunk('1. '),'','List number is not a complete sentence');
assert.equal(sentenceChunk('Он сказал: «Готово!» Далее'),'Он сказал: «Готово!» ');
assert.equal(sentenceChunk('Подождём… Потом'),'Подождём… ');
assert.equal(sentenceChunk('Вызов `first. Second'),'','Do not cut inline code');
assert.equal(sentenceChunk('```js\nfirst. Second\n'),'','Do not cut fenced code');
sentences.render(props('Финальный текст без точки',false));
assert.equal(sentences.tree().props.content,'Финальный текст без точки');
assert.equal(sentences.timers.size,0);
const sentenceThinking = harness({markdownRenderIntervalMs:50,markdownShowPartialParagraph:true,markdownWholeSentences:true});
let thinking = sentenceThinking.ctx.rf1({thinkingBlock:{thinking:'Проверено. Теперь дума'},context,isCurrentlyThinking:true,isExpanded:true});
sentenceThinking.render(thinking.props.children[1].props.children.props);
assert.equal(sentenceThinking.tree().props.content,'Проверено. ');
thinking = sentenceThinking.ctx.rf1({thinkingBlock:{thinking:'Проверено. Теперь готово'},context,isCurrentlyThinking:false,isExpanded:true});
sentenceThinking.render(thinking.props.children[1].props.children.props);
assert.equal(sentenceThinking.tree().props.content,'Проверено. Теперь готово');
const animationConfig={markdownRenderIntervalMs:50,markdownShowPartialParagraph:true,
  markdownWholeSentences:true,markdownAnimateSentences:true};
const framed=harness(animationConfig,native,{frames:true,timeoutFloor:1000});
const throttled=harness(animationConfig,native,{timeoutFloor:1000});
const frameText='слово '.repeat(70)+'. ';
for(const h of [framed,throttled]) {
  h.render(props('',true));h.render(props(frameText,true));h.advance(200);
  if(h===framed) assert.ok(h.tree().props.content.length>0,'Coalescing must also bypass throttled timeouts');
  h.advance(3800);
}
assert.equal(framed.ctx.window.__claudeMarkdownRender.animation_scheduler,'animation-frame');
assert.ok(framed.ctx.window.__claudeMarkdownRender.animation_ticks>40 &&
  framed.ctx.window.__claudeMarkdownRender.animation_ticks>throttled.ctx.window.__claudeMarkdownRender.animation_ticks*5,
  'One-second timeout throttling must not throttle visual animation or coalescing');
assert.ok(framed.tree().props.content.length>=170 && framed.tree().props.content.length<frameText.length);
assert.ok(framed.ctx.window.__claudeMarkdownRender.animation_frame_max_gap_ms<=80);
const beforeHidden=framed.tree().props.content;
framed.visibility('hidden');framed.render(props(frameText+'Следующее предложение. ',true));framed.advance(10000);
assert.equal(framed.tree().props.content,beforeHidden,'Hidden tab preserves the queue instead of flushing it instantly');
framed.visibility('visible');framed.advance(16);
assert.ok(framed.tree().props.content.length-beforeHidden.length<=6,'Resume must not spend ten seconds of typing credit in one frame');
framed.advance(200);
const beforeStall=framed.tree().props.content.length;
framed.stall(5000);framed.advance(0);
assert.ok(framed.tree().props.content.length-beforeStall<=6,'A delayed frame must not dump a large word fragment');
assert.ok(framed.ctx.window.__claudeMarkdownRender.animation_late_frames>0);
framed.render(props(frameText+'Следующее предложение. Конец',false));framed.advance(3100);
assert.equal(framed.tree().props.content,frameText+'Следующее предложение. Конец');
assert.equal(framed.timers.size,0,'Completed animation leaves no RAF loop');
throttled.unmount();
const cancelFrame=harness(animationConfig,native,{frames:true});
cancelFrame.render(props('',true));cancelFrame.render(props(frameText,true));cancelFrame.advance(150);
assert.ok(cancelFrame.timers.size>0);cancelFrame.unmount();
assert.equal(cancelFrame.timers.size,0,'Unmount cancels pending animation frames');
const animated=harness(animationConfig);
animated.render(props('',true));
animated.render(props('Готово. Дальше ещё думаем',true));
animated.advance(50);
assert.equal(animated.tree().props.content,'','Completed sentence is queued before gradual reveal');
assert.equal(animated.ctx.window.__claudeMarkdownRender.animation_queue,'Готово. Дальше ещё думаем'.length);
assert.equal(animated.ctx.window.__claudeMarkdownRender.animation_ready_queue,'Готово. '.length);
assert.equal(animated.ctx.window.__claudeMarkdownRender.animation_waiting_sentence,'Дальше ещё думаем'.length);
animated.advance(100);
assert.ok(animated.tree().props.content.length>0 && animated.tree().props.content.length<'Готово. '.length);
animated.advance(2000);
assert.equal(animated.tree().props.content,'Готово. ','Do not animate an incomplete next sentence');
assert.equal(animated.timers.size,0,'No animation timer while waiting for a sentence');
assert.equal(animated.ctx.window.__claudeMarkdownRender.animation_ready_queue,0);
assert.equal(animated.ctx.window.__claudeMarkdownRender.animation_queue,'Дальше ещё думаем'.length);
animated.render(props('Готово. Финальный остаток без точки',false));
assert.notEqual(animated.tree().props.content,'Готово. Финальный остаток без точки');
animated.advance(3000);
assert.equal(animated.tree().props.content,'Готово. Финальный остаток без точки');
assert.equal(animated.timers.size,0);
assert.equal(animated.ctx.window.__claudeMarkdownRender.animation_queue,0,'Completion empties telemetry');
const history=harness(animationConfig);
history.render(props('Готовая история целиком',false));
assert.equal(history.tree().props.content,'Готовая история целиком');
assert.equal(history.timers.size,0);
const burst=harness(animationConfig);
burst.render(props('',true));
const large='Большой фрагмент '+('текст '.repeat(500))+'. ';
burst.render(props(large,true));burst.advance(1200);
assert.ok(burst.tree().props.content.length>large.length/2 && burst.tree().props.content.length<large.length,
  'Live burst drains the excess quickly while retaining a modest reserve');
const earlyRate=burst.ctx.window.__claudeMarkdownRender.animation_rate;
const earlyReceiptRate=burst.ctx.window.__claudeMarkdownRender.animation_receive_rate;
const beforePause=burst.tree().props.content.length;
burst.advance(5000);
assert.ok(burst.tree().props.content.length>beforePause && burst.tree().props.content.length<large.length,
  'Continue printing through a five-second server pause using the buffered text');
assert.ok(burst.ctx.window.__claudeMarkdownRender.animation_rate<earlyRate &&
  burst.ctx.window.__claudeMarkdownRender.animation_receive_rate<earlyReceiptRate,
  'Both estimated input rate and printing speed decay even without new server messages');
const afterPause=burst.tree().props.content.length;
burst.render(props(large+large,true));burst.advance(100);
assert.ok(burst.ctx.window.__claudeMarkdownRender.animation_reserve_ms>=7500,
  'Remember the observed gap after data resumes');
assert.ok(burst.ctx.window.__claudeMarkdownRender.animation_rate>60 &&
  burst.tree().props.content.length-afterPause>12,
  'A new large burst accelerates beyond the former ceiling');
burst.render(props(large+large+'Конец без точки',false));burst.advance(3000);
assert.equal(burst.tree().props.content,large+large+'Конец без точки',
  'Only the final server message enables fast catch-up, without losing buffered text');
assert.equal(burst.timers.size,0);
const bigQueue=harness(animationConfig),biggerQueue=harness(animationConfig);
const sevenThousand='текст '.repeat(1166)+'. ';
for(const [h,text] of [[bigQueue,sevenThousand],[biggerQueue,sevenThousand+sevenThousand]]) {
  h.render(props('',true));h.render(props(text,true));h.advance(100);
}
assert.ok(bigQueue.ctx.window.__claudeMarkdownRender.animation_rate>1000,
  'A roughly 7000-character queue must not remain capped at 60 characters per second');
assert.ok(biggerQueue.ctx.window.__claudeMarkdownRender.animation_rate>
  bigQueue.ctx.window.__claudeMarkdownRender.animation_rate*1.8,
  'Larger queues raise the automatic speed without a fixed upper limit');
bigQueue.advance(3000);
assert.ok(bigQueue.ctx.window.__claudeMarkdownRender.animation_queue>0 &&
  bigQueue.ctx.window.__claudeMarkdownRender.animation_queue<=550,
  'Drain a 7000-character burst towards the 500-character reserve within a few seconds');
bigQueue.unmount();biggerQueue.unmount();
const sustained=harness(animationConfig);
sustained.render(props('',true));
const packet='слово '.repeat(32)+'. ';
for(let i=1;i<=40;i++) {
  sustained.render(props(packet.repeat(i),true));sustained.advance(250);
  if(i>=12) assert.ok(sustained.ctx.window.__claudeMarkdownRender.animation_queue<1100,
    'Sustained fast input must not accumulate thousands of buffered characters');
}
sustained.unmount();
const pendingSentence=harness(animationConfig);
pendingSentence.render(props('',true));pendingSentence.render(props(large,true));
for(let i=1;i<=24;i++) {
  pendingSentence.advance(250);
  pendingSentence.render(props(large+'а'.repeat(i*5),true));
}
pendingSentence.advance(50);
assert.ok(pendingSentence.ctx.window.__claudeMarkdownRender.animation_receipt_pause_ms<=50 &&
  pendingSentence.ctx.window.__claudeMarkdownRender.animation_reserve_ms>=5900,
  'Retain reserve while packets arrive regularly but the next sentence is still incomplete');
assert.ok(large.startsWith(pendingSentence.tree().props.content));
pendingSentence.unmount();
assert.equal(pendingSentence.ctx.window.__claudeMarkdownRender.animation_queue,0,'Unmount removes held sentence counters');
const paused=harness(animationConfig);
const smallReserve='текст '.repeat(80)+'. ';
paused.render(props('',true));paused.advance(60000);
paused.render(props(smallReserve,true));paused.advance(50);
const pauseStart=paused.tree().props.content.length;
paused.advance(5000);
assert.ok(paused.tree().props.content.length-pauseStart>=249,
  'After a long pause, even a small reserve prints at least fifty characters per second');
assert.equal(paused.ctx.window.__claudeMarkdownRender.animation_queue,
  smallReserve.length-paused.tree().props.content.length,'Queue reports the current remainder, not its historical peak');
paused.render(props(smallReserve+'Остановлено без точки',false));paused.advance(3000);
assert.equal(paused.tree().props.content,smallReserve+'Остановлено без точки',
  'Stopping a paused response releases the incomplete tail and drains the buffer');
assert.equal(paused.ctx.window.__claudeMarkdownRender.animation_queue,0);
assert.equal(paused.timers.size,0);
// The real interruption path leaves the block's partial flag set. Completion
// must come from the owning session, including when there is no animation timer.
for(const method of ['interrupt','endTurn']) {
  const stopped=harness(animationConfig),session=new stopped.ctx.yQ();
  const block={content:{type:'text',text:''},isPartial:true,key:1};
  const input=()=>stopped.ctx.Nn0({content:block,context}).props;
  session.messages.value=[{content:[block]}];
  stopped.render(input());
  const tail='Незавершённое предложение '+('а'.repeat(188));
  block.content.text=tail;stopped.render(input());stopped.advance(50);
  assert.equal(stopped.ctx.window.__claudeMarkdownRender.animation_waiting_sentence,tail.length);
  assert.equal(stopped.ctx.window.__claudeMarkdownRender.animation_ready_queue,0);
  assert.equal(stopped.timers.size,0);
  const foreign=new stopped.ctx.yQ();
  foreign.messages.value=[{content:[{content:{type:'text',text:tail},isPartial:true}]}];
  foreign[method]();stopped.advance(50);
  assert.equal(stopped.tree().props.content,'','Stopping another session must not flush identical text here');
  const result=session[method]();
  if(method==='interrupt') {
    assert.equal(typeof result.then,'function','Preserve the native interrupt return value');
    assert.equal(session.interruptCalls,1,'Forward the native stop request exactly once');
  } else assert.equal(session.busy.value,false,'Preserve native endTurn behavior');
  stopped.advance(50);
  assert.equal(block.isPartial,true,'Do not mutate native content state to fix display');
  assert.equal(stopped.ctx.window.__claudeMarkdownRender.animation_waiting_sentence,0);
  assert.ok(stopped.ctx.window.__claudeMarkdownRender.animation_ready_queue>0);
  stopped.advance(950);session.endTurn(); // A repeated notification must not restart the deadline.
  stopped.advance(2000);
  assert.equal(stopped.tree().props.content,tail,'Flush all text without final punctuation within three seconds');
  assert.equal(stopped.ctx.window.__claudeMarkdownRender.animation_queue,0);
  assert.equal(stopped.timers.size,0);
}
const fastStop=harness(animationConfig),fastSession=new fastStop.ctx.yQ();
const fastBlock={content:{type:'text',text:''},isPartial:true,key:1};
fastSession.messages.value=[{content:[fastBlock]}];
const fastInput=()=>fastStop.ctx.Nn0({content:fastBlock,context}).props;
fastStop.render(fastInput());fastBlock.content.text=sevenThousand;
fastStop.render(fastInput());fastStop.advance(300);
let finishRate=fastStop.ctx.window.__claudeMarkdownRender.animation_rate;
assert.ok(finishRate>150);
fastSession.interrupt();
for(let i=0;i<60;i++) {
  fastStop.advance(50);
  const rate=fastStop.ctx.window.__claudeMarkdownRender.animation_rate;
  assert.ok(rate>=finishRate,'Final draining must never slow down below the preceding live rate');
  finishRate=rate;
}
assert.equal(fastStop.tree().props.content,sevenThousand);
const stopThinking=harness(animationConfig),thinkingSession=new stopThinking.ctx.yQ();
const thinkingBlock={content:{type:'thinking',thinking:''},isPartial:true,key:1};
thinkingSession.messages.value=[{content:[thinkingBlock]}];
stopThinking.render({...props('',undefined),__claudeStreaming:true,__claudeBlock:thinkingBlock});
thinkingBlock.content.thinking='Размышление без завершающей точки';
const thinkingElement=stopThinking.ctx.Nn0({content:thinkingBlock,context,areThinkingBlocksExpanded:true});
assert.equal(thinkingElement.props.__claudeBlock,thinkingBlock);
const stoppedThinkingTree=thinkingElement.type(thinkingElement.props);
stopThinking.render(stoppedThinkingTree.props.children[1].props.children.props);stopThinking.advance(50);
thinkingSession.interrupt();stopThinking.advance(3000);
assert.equal(stopThinking.tree().props.content,thinkingBlock.content.thinking,
  'Interrupted thinking releases its unpunctuated tail too');
const quietHistory=harness(animationConfig),historySession=new quietHistory.ctx.yQ();
const historyBlock={content:{type:'text',text:'Готовая история'},isPartial:false};
historySession.messages.value=[{content:[historyBlock]}];
quietHistory.render(quietHistory.ctx.Nn0({content:historyBlock,context}).props);
const historyRenders=quietHistory.ctx.window.__claudeMarkdownRender.renders;
historySession.endTurn();quietHistory.advance(3000);
assert.equal(quietHistory.ctx.window.__claudeMarkdownRender.renders,historyRenders,
  'Ending a turn must not rerender completed history');
for(const route of ['reset','resume','new-turn']) {
  const resumed=harness(animationConfig),session=new resumed.ctx.yQ();
  const block={content:{type:'text',text:''},isPartial:true,key:1};
  session.messages.value=[{content:[block]}];
  const input=()=>resumed.ctx.Nn0({content:block,context}).props;
  resumed.render(input());block.content.text='Начало предложения';
  resumed.render(input());resumed.advance(50);
  if(route==='reset') {
    session.resetPerProcessState();
    resumed.ctx.window.__claudeFinishMarkdownSession(session); // Busy-false fallback after reset.
    resumed.advance(100);
    assert.equal(resumed.tree().props.content,'','Process reset must not release unfinished sentences');
    assert.equal(resumed.ctx.window.__claudeMarkdownRender.animation_waiting_sentence,block.content.text.length);
  } else {
    if(route==='new-turn') session.interrupt();else session.endTurn();
    resumed.advance(200);
    assert.ok(resumed.tree().props.content.length>0);
  }
  const prefix=resumed.tree().props.content;
  if(route==='new-turn') session.sendTurnIdx++;
  session.busy.value=true;
  block.content.text+=' продолжает поступать';resumed.render(input());resumed.advance(200);
  assert.equal(resumed.tree().props.content,prefix,
    'Resumed partial text must wait for a sentence without exposing chunks or retracting printed characters');
  block.content.text+=' целиком. Следующее';resumed.render(input());resumed.advance(100);
  const first=resumed.tree().props.content;
  assert.ok(first.length>prefix.length && first.length<block.content.text.indexOf('Следующее'),
    'A completed sentence must animate again in the same tab');
  const stable=first;
  resumed.advance(100);
  assert.ok(resumed.tree().props.content.length>stable.length,'Typing proceeds between network packets');
  if(route!=='reset') assert.equal(resumed.ctx.window.__claudeMarkdownRender.animation_resumed,1);
  session.endTurn();resumed.advance(3000);
  assert.equal(resumed.tree().props.content,block.content.text,'Subsequent genuine completion still drains in three seconds');
}
const late=harness(animationConfig),lateSession=new late.ctx.yQ();
const lateBlock={content:{type:'text',text:''},isPartial:true,key:1};
lateSession.messages.value=[{content:[lateBlock]}];
const lateInput=()=>late.ctx.Nn0({content:lateBlock,context}).props;
late.render(lateInput());lateBlock.content.text='Незавершённый ответ';late.render(lateInput());late.advance(50);
lateSession.interrupt();late.advance(100);
lateBlock.content.text+=' и последний пакет после остановки';late.render(lateInput());
assert.notEqual(late.tree().props.content,lateBlock.content.text,'Late packets must not bypass final animation');
late.advance(2900);
assert.equal(late.tree().props.content,lateBlock.content.text);
assert.equal(late.ctx.window.__claudeMarkdownRender.animation_resumed,0,'Late packet in the same cancelled turn is not a new stream');
const unicode=harness(animationConfig);
unicode.render(props('',true));
const unicodeText='👩‍👩‍👧‍👦 e\u0301 готово. ';
const boundaries=new Set([0,...[...new Intl.Segmenter('ru',{granularity:'grapheme'}).segment(unicodeText)]
  .map(p=>p.index+p.segment.length)]);
unicode.render(props(unicodeText,true));
let unicodeLength=0;
for(let i=0;i<24;i++) {
  unicode.advance(50);
  assert.ok(boundaries.has(unicode.tree().props.content.length),'Do not split emoji or combining characters');
  assert.ok(unicode.tree().props.content.length>=unicodeLength,'Grapheme credit must never retract painted text');
  unicodeLength=unicode.tree().props.content.length;
}
unicode.render(props(unicodeText,false));
for(let i=0;i<60;i++) {
  unicode.advance(50);
  assert.ok(boundaries.has(unicode.tree().props.content.length),'Final catch-up preserves graphemes');
}
assert.equal(unicode.tree().props.content,unicodeText);
const slow=harness(animationConfig),fast=harness(animationConfig);
slow.render(props('',true));fast.render(props('',true));
for(let i=1;i<=4;i++) {
  slow.advance(250);fast.advance(250);
  slow.render(props('а'.repeat(i),true));fast.render(props('а'.repeat(i*100),true));
}
assert.ok(fast.ctx.window.__claudeMarkdownRender.animation_receive_rate>
  slow.ctx.window.__claudeMarkdownRender.animation_receive_rate,'Estimate adapts to incoming rate');
const cancelAnimation=harness(animationConfig);
cancelAnimation.render(props('',true));cancelAnimation.render(props(large,true));cancelAnimation.advance(100);
assert.ok(cancelAnimation.timers.size>0);cancelAnimation.unmount();
assert.equal(cancelAnimation.timers.size,0,'Unmount cancels both coalescing and animation');
assert.equal(cancelAnimation.ctx.window.__claudeMarkdownRender.animation_queue,0);
const noAnimation=harness({...animationConfig,markdownAnimateSentences:false});
noAnimation.render(props('',true));noAnimation.render(props('Готово. ',true));noAnimation.advance(50);
assert.equal(noAnimation.tree().props.content,'Готово. ','Disabled restores whole sentence at once');
const animatedThinking=harness(animationConfig);
const thinkingInput=(text,live)=>animatedThinking.ctx.rf1({thinkingBlock:{thinking:text},context,
  isCurrentlyThinking:live,isExpanded:true}).props.children[1].props.children.props;
animatedThinking.render({...props('Д',undefined),__claudeStreaming:true});
animatedThinking.render(thinkingInput('Думаю. Следующее',true));animatedThinking.advance(100);
assert.ok(animatedThinking.tree().props.content.length<'Думаю. '.length);
animatedThinking.render(thinkingInput('Думаю. Итог',false));animatedThinking.advance(3000);
assert.equal(animatedThinking.tree().props.content,'Думаю. Итог');
const replacement=harness(animationConfig);
replacement.render(props('',true));replacement.render(props(large,true));replacement.advance(100);
replacement.render(props('Другая готовая запись',false));
assert.equal(replacement.tree().props.content,'Другая готовая запись');
assert.equal(replacement.timers.size,0,'Replacement must not receive pending text from the previous message');
assert.equal(replacement.ctx.window.__claudeMarkdownRender.animation_queue,0);
const nativeParagraph = harness({markdownShowPartialParagraph:false});
assert.equal(nativeParagraph.render(streaming).props.isPartialText,true,'Disabled keeps native behavior');
if (bundle) {
  const jsx = (type, props)=>({type,props});
  const renderer = vm.createContext({F:jsx,R:jsx,l:()=>[null,()=>{}],H0:fn=>fn,
    PM:{root:'markdown'},GS:{},PL0:{},b41:function(){}});
  vm.runInContext(native + bundle.slice(bundle.indexOf('function wL0('),bundle.indexOf('var PL0;')),renderer);
  const paragraphText = p=>renderer.r$(p).props.children[0].props.children;
  assert.equal(paragraphText(nativeParagraph.tree().props),'first paragraph', 'Reproduce actual native paragraph batching');
  faster.render(streaming);
  assert.equal(paragraphText(faster.tree().props),streaming.content,'Actual native renderer now receives the full growing paragraph');
}
thought.render(props('thinking'));
thought.render(props('thinking more'));
assert.equal(thought.tree().props.content, 'thinking');
thought.advance(500);
assert.equal(thought.tree().props.content, 'thinking more', 'Thinking has no partial flag');
thought.render(props('thinking more ending'));
assert.equal(thought.timers.size, 1);
thought.unmount();
assert.equal(thought.timers.size, 0, 'Collapse/unmount cancels pending work');
const independent = harness();
independent.render(props('separate chat'));
assert.equal(independent.tree().props.content, 'separate chat');
const changedContext = {fileOpener:{newOpener:true}};
h.render({content:'different message', context:changedContext, isPartialText:false});
h.advance(500);
assert.equal(h.tree().props.context, changedContext, 'File/link handlers receive updated context');
for(const cfg of [{markdownRenderIntervalMs:0},{safeMode:true}]) {
  const disabled = harness(cfg);
  assert.equal(disabled.ctx.r$, disabled.original);
}
const unknown = harness({}, 'function r$(){return null}');
assert.equal(unknown.ctx.r$, unknown.original);
assert.equal(unknown.ctx.window.__claudeMarkdownRender.state, 'unsupported-bundle');
console.log('Markdown coalescing: sustained stream, trailing flush, memoization, completion, unmount, disabled: OK' +
  (bundle ? ' (installed renderer binding)' : ''));
