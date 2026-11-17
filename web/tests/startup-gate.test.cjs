"use strict";

// Execute the actual gate component and effects with deterministic DOM/RAF
// substitutes. These checks neither mount an authenticated app nor call APIs.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const source = fs.readFileSync(path.join(__dirname, "../app/startup-gate.tsx"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../app/startup-gate.css"), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  fileName: "startup-gate.tsx",
}).outputText;

function harness({ seen = false, reduced = false, search = "" } = {}) {
  const slots = [], effects = [], events = new Map(), frames = new Map(), timers = new Map();
  const mediaEvents = new Map(), windowEvents = new Map(), stored = new Map();
  const stats = { focused: 0, settingsFocused: 0, created: 0, destroyed: 0, disconnected: 0, writes: 0 };
  let index = 0, next = 1, tree, clock = 100;
  if (seen) stored.set("smart-trading-intro:v1", "1");
  const root = { style: {setProperty() {}}, getBoundingClientRect: () => ({left:0,top:0,width:1280,height:720}), focus() { stats.focused++; } };
  const heading = { getBoundingClientRect: () => ({left:300,top:300,width:680,height:80}) };
  const canvas = {};
  const remove = (map, name, callback) => { if (map.get(name) === callback) map.delete(name); };
  const media = {matches:reduced,addEventListener:(name,callback)=>mediaEvents.set(name,callback),removeEventListener:(name,callback)=>remove(mediaEvents,name,callback)};
  const window = {
    innerWidth:1280, location:{search},
    sessionStorage:{getItem:key=>stored.get(key) ?? null,setItem(key,value){stats.writes++;stored.set(key,value);}},
    requestAnimationFrame(callback){const id=next++;frames.set(id,callback);return id;},
    cancelAnimationFrame:id=>frames.delete(id),
    setTimeout(callback,delay){assert.equal(delay,6000);const id=next++;timers.set(id,callback);return id;},
    clearTimeout:id=>timers.delete(id), matchMedia:()=>media,
    addEventListener:(name,callback)=>windowEvents.set(name,callback),
    removeEventListener:(name,callback)=>remove(windowEvents,name,callback),
  };
  const document = {
    hidden:false,
    addEventListener:(name,callback)=>events.set(name,callback),
    removeEventListener:(name,callback)=>remove(events,name,callback),
    getElementById(id){assert.equal(id,"workspace-settings");return {focus(){stats.settingsFocused++;}};},
  };
  function changed(before, after) { return !before || after.some((value,i)=>!Object.is(value,before[i])); }
  const mockReact = {
    ...React,
    useState(initial){const i=index++;if(!slots[i])slots[i]={value:initial};return [slots[i].value,value=>{slots[i].value=typeof value==="function"?value(slots[i].value):value;}];},
    useRef(initial){const i=index++;if(!slots[i])slots[i]={current:initial};return slots[i];},
    useCallback(callback,deps){const i=index++;if(changed(slots[i]?.deps,deps))slots[i]={value:callback,deps:[...deps]};return slots[i].value;},
    useEffect(callback,deps){const i=index++;if(changed(slots[i]?.deps,deps)){const previous=slots[i];slots[i]={deps:[...deps],cleanup:previous?.cleanup};effects.push(()=>{slots[i].cleanup?.();slots[i].cleanup=callback();});}},
  };
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,module:{exports},window,document,URLSearchParams,
    performance:{now:()=>clock},
    getComputedStyle:()=>({fontSize:"64px",fontFamily:"test-font",fontWeight:"650",letterSpacing:"1px"}),
    ResizeObserver:class {observe(target){assert.equal(target,root);}disconnect(){stats.disconnected++;}},
    require(name){
      if(name==="react")return mockReact;
      if(name==="./brand")return {APP_NAME:"智慧交易助手",APP_SUBTITLE:"多席位策略研究与交易复盘"};
      if(name==="./startup-gate.css")return {};
      if(name==="./startup-particles")return {createStartupParticles(target){assert.equal(target,canvas);stats.created++;return {resize(){},draw(){},destroy(){stats.destroyed++;}};}};
      return require(name);
    },
  },{filename:"startup-gate.tsx"});
  function assignRefs(node){
    if(!node || typeof node!=="object")return;
    if(node.props?.ref)node.props.ref.current=node.type==="section"?root:node.type==="canvas"?canvas:heading;
    React.Children.forEach(node.props?.children,assignRefs);
  }
  function render(){index=0;tree=exports.default({children:React.createElement("div",{id:"real-workspace"},"工作台")});assignRefs(tree);for(const effect of effects.splice(0))effect();return renderToStaticMarkup(tree);}
  function key(properties){let prevented=0;const event={key:"",code:"",preventDefault(){prevented++;},...properties};events.get("keydown")?.(event);return prevented;}
  function dispose(){for(const slot of slots)slot?.cleanup?.();}
  render();
  return {render,key,dispose,events,frames,timers,mediaEvents,windowEvents,stats,stored,
    get finished(){return slots[0].value;},
    flushFrame(){for(const [id,callback] of [...frames]){frames.delete(id);callback(clock);}},
  };
}

test("intro has no skip button and focuses the accessible dialog", () => {
  const h=harness(); const markup=h.render();
  assert.doesNotMatch(markup,/<button|跳过开场|startup-skip|real-workspace/);
  assert.match(markup,/role="dialog"/);assert.match(markup,/tabindex="-1"/i);
  assert.match(markup,/aria-keyshortcuts="Space Escape"/);assert.match(markup,/可按空格键/);
  assert.equal(h.stats.focused,1);
  assert.doesNotMatch(css,/\.startup-skip/);
  assert.match(css,/\.startup-instruction\s*\{[^}]*clip:/);
  h.dispose();
});

for(const [label,event] of [["space key",{key:" "}],["physical Space",{code:"Space"}],["legacy Spacebar",{key:"Spacebar"}],["Escape",{key:"Escape"}]]){
  test(`${label} skips once, prevents scrolling and releases animation resources`,()=>{
    const h=harness(),originalKey=h.events.get("keydown");
    assert.equal(h.key(event),1);assert.equal(h.finished,true);
    assert.equal(h.stored.get("smart-trading-intro:v1"),"1");
    assert.equal(h.stats.writes,1);assert.equal(h.stats.destroyed,1);assert.equal(h.stats.disconnected,1);
    for(const map of [h.events,h.frames,h.timers,h.mediaEvents,h.windowEvents])assert.equal(map.size,0);
    originalKey({...event,repeat:true,preventDefault(){}});
    assert.equal(h.stats.writes,1);assert.equal(h.stats.destroyed,1);
    assert.match(h.render(),/real-workspace/);h.flushFrame();assert.equal(h.stats.settingsFocused,1);
    h.dispose();
  });
}

test("modified keys, composition and unrelated keys neither skip nor prevent defaults",()=>{
  const h=harness();
  for(const event of [{key:"Enter"},{key:"a"},{key:" ",altKey:true},{key:" ",ctrlKey:true},{code:"Space",metaKey:true},{key:" ",isComposing:true}]){
    assert.equal(h.key(event),0);assert.equal(h.finished,false);
  }
  assert.equal(h.stats.writes,0);h.dispose();
});

test("unmount detaches the key handler without writing a completion marker",()=>{
  const h=harness();h.dispose();h.dispose();
  for(const map of [h.events,h.frames,h.timers,h.mediaEvents,h.windowEvents])assert.equal(map.size,0);
  assert.equal(h.stats.destroyed,1);assert.equal(h.stats.writes,0);
  assert.equal(h.key({key:" "}),0);assert.equal(h.finished,false);
});

test("seen/reduced-motion bypass and explicit replay keep existing entry behavior",()=>{
  for(const options of [{seen:true},{reduced:true}]){
    const h=harness(options);assert.equal(h.finished,true);assert.equal(h.stats.created,0);assert.equal(h.events.size,0);h.dispose();
  }
  const replay=harness({seen:true,search:"?intro=1"});assert.equal(replay.finished,false);assert.equal(replay.stats.created,1);replay.dispose();
  const gate=fs.readFileSync(path.join(__dirname,"../app/workspace-gate.tsx"),"utf8");
  assert.match(gate,/status\?\.authenticated && status\.ready && <VerifiedApiEntry enabled=\{status\.ai_available\}>/);
  assert.match(gate,/return enabled \? <StartupGate>\{children\}<\/StartupGate> : children/);
  assert.doesNotMatch(source,/fetch\s*\(|XMLHttpRequest|contest-api|api_key/);
});
