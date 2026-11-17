"use strict";

// Controller/lifecycle checks on the real component, without credentials,
// browser calls or model access. Visual integration is checked separately.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const source = fs.readFileSync(path.join(__dirname, "../app/news-desk.tsx"), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target:ts.ScriptTarget.ES2022, module:ts.ModuleKind.CommonJS, jsx:ts.JsxEmit.ReactJSX, strict:true },
  fileName:"news-desk.tsx",
}).outputText;

function findElement(node, type) {
  if (!node || typeof node !== "object") return null;
  if (node.type === type) return node;
  for (const child of React.Children.toArray(node.props?.children)) {
    const found = findElement(child, type);
    if (found) return found;
  }
  return null;
}

function harness(initialSeat = "livermore") {
  let hookIndex = 0, refIndex = 0, nextTimer = 1, pendingEffect, previousDeps, cleanup, tree;
  const states = [], events = new Map(), timers = new Map(), researchCalls = [], notices = [];
  const origin = "http://127.0.0.1:3001";
  const contentWindow = {};
  const frameRef = {current:{contentWindow}};
  const refs = [frameRef];
  const onResearch = value => researchCalls.push(value);
  const window = {
    location:{origin},
    dispatchEvent(event) {notices.push(event.type);},
    addEventListener(name, handler) { assert.equal(name, "message"); events.set(name, handler); },
    removeEventListener(name, handler) { if (events.get(name) === handler) events.delete(name); },
    setTimeout(callback, ms) { assert.equal(ms, 15000); const id = nextTimer++; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const mockReact = {
    ...React,
    useRef(initial) {
      const index = refIndex++;
      if (!(index in refs)) refs[index] = {current:initial};
      return refs[index];
    },
    useState(initial) {
      const index = hookIndex++;
      if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial;
      return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }];
    },
    useEffect(callback, deps) {
      if (!previousDeps || deps.some((value, index) => !Object.is(value, previousDeps[index]))) {
        pendingEffect = callback;
        previousDeps = [...deps];
      }
    },
  };
  const exports = {};
  vm.runInNewContext(compiled, {
    exports, module:{exports}, window, Event,
    require(name) { return name === "react" ? mockReact : require(name); },
  }, {filename:"news-desk.tsx"});
  const Component = exports.default;
  let seat = initialSeat, researchHandler = onResearch;
  function render(nextSeat = seat, nextHandler = researchHandler) {
    seat = nextSeat;
    researchHandler = nextHandler;
    hookIndex = 0;
    refIndex = 0;
    tree = Component({seatId:seat,onResearch:researchHandler});
    assert.equal(hookIndex, 2, "Review harness if the news component adds state");
    if (pendingEffect) {
      cleanup?.();
      cleanup = pendingEffect();
      pendingEffect = null;
    }
    return renderToStaticMarkup(tree);
  }
  function send(data = {channel:"news-earth",type:"ready"}, overrides = {}) {
    events.get("message")?.({origin,source:contentWindow,data,...overrides});
  }
  render();
  return {
    render,send,states,events,timers,frameRef,researchCalls,notices,
    get tree() { return tree; },
    get status() { return states[0]; },
    timeout() { for (const callback of [...timers.values()]) callback(); },
    dispose() { cleanup?.(); cleanup = null; },
  };
}

test("news frame uses bundled same-origin assets and a descriptive title", () => {
  const h = harness("buffett");
  const markup = h.render();
  assert.match(markup, /src="\/news-earth\/index\.html\?view=orbit&amp;embed=1&amp;mode=live&amp;seat=buffett"/);
  assert.match(markup, /title="新闻页：地球定位、板块关联与股票列表"/);
  assert.match(markup, /referrerPolicy="no-referrer"/i);
  assert.match(markup, /aria-busy="true"/);
  assert.doesNotMatch(markup, /3008|allow-top-navigation/);
  h.dispose();
});

const researchMessage = (overrides = {}) => ({
  channel:"news-earth", type:"research", newsId:"g14", stockId:"s1", seatId:"livermore", sectorId:"grid", ...overrides,
});

test("valid research entry forwards only the bounded canonical IDs without a financial API call", () => {
  const h = harness();
  h.send(researchMessage());
  assert.equal(h.researchCalls.length,1);
  assert.deepEqual(JSON.parse(JSON.stringify(h.researchCalls[0])), {
    newsId:"g14",stockId:"s1",seatId:"livermore",sectorId:"grid",
  });
  assert.equal(h.status,"loading","A research message must not masquerade as the ready handshake");
  assert.doesNotMatch(source,/fetch\s*\(|XMLHttpRequest|api_key/);
  h.dispose();
});

test("research entry rejects a spoofed origin, different frame and wrong channel", () => {
  const h = harness();
  h.send(researchMessage(),{origin:"https://elsewhere.invalid"});
  h.send(researchMessage(),{source:{}});
  h.send(researchMessage({channel:"other-channel"}));
  h.send({channel:"news-earth",type:"research"});
  assert.equal(h.researchCalls.length,0);
  h.dispose();
});

test("malformed research IDs, unsupported seats and non-string values are not forwarded", () => {
  const h = harness();
  for (const field of ["newsId","stockId"]) {
    for (const value of [null,1,{},[],"", "a".repeat(65),"<script>","../other"]) {
      h.send(researchMessage({[field]:value}));
    }
  }
  for (const seatId of [null, "", "other-seat", "../buffett", 1]) h.send(researchMessage({seatId}));
  for (const sectorId of [1, {}, [], "a".repeat(65), "../grid"]) h.send(researchMessage({sectorId}));
  assert.equal(h.researchCalls.length,0);
  h.dispose();
});

test("a page-local seat choice and all-sector scope can enter their exact research without substitution", () => {
  const h = harness("livermore");
  h.send(researchMessage({seatId:"buffett",sectorId:null}));
  assert.equal(h.researchCalls.length,1);
  assert.equal(h.researchCalls[0].seatId,"buffett");
  assert.equal(h.researchCalls[0].sectorId,null);
  h.dispose();
});

test("a refreshed parent callback does not reset an already ready iframe and receives subsequent entries", () => {
  const h = harness(), received = [];
  h.send();
  assert.equal(h.status,"ready");
  h.render("livermore", value => received.push(value));
  assert.equal(h.status,"ready","Parent state updates must not require a second iframe ready message");
  h.send(researchMessage());
  assert.equal(received.length,1);
  assert.equal(h.researchCalls.length,0,"Do not call a stale parent closure");
  assert.equal(h.events.size,1);
  h.dispose();
});

test("unsupported seat IDs cannot inject an iframe origin or query parameter", () => {
  const h = harness('https://other.invalid/?token=private&seat=buffett');
  assert.match(h.render(), /seat=livermore"/);
  assert.doesNotMatch(h.render(), /other\.invalid|token=private/);
  h.dispose();
});

test("only the same-origin mounted iframe can confirm readiness", () => {
  const h = harness();
  h.send(undefined, {origin:"http://127.0.0.1:3008"});
  h.send(undefined, {source:{}});
  h.send({channel:"wrong",type:"ready"});
  h.send(null);
  h.send({channel:"news-earth",type:"unknown"});
  assert.equal(h.status,"loading");
  h.send();
  assert.equal(h.status,"ready");
  assert.doesNotMatch(h.render(), /news-loading|正在载入新闻地图/);
  assert.match(h.render(), /aria-busy="false"/);
  h.dispose();
});

test("verified error and iframe load failure expose retry, not a false ready state", () => {
  const h = harness();
  h.send({channel:"news-earth",type:"error"});
  assert.equal(h.status,"error");
  assert.match(h.render(), /role="alert"/);
  assert.match(h.render(), /重新加载新闻页/);
  h.send();
  assert.equal(h.status,"ready");
  findElement(h.tree, "iframe").props.onError();
  assert.equal(h.status,"error");
  h.dispose();
});

test("readiness timeout fails pending loads but does not overwrite ready", () => {
  const pending = harness();
  pending.timeout();
  assert.equal(pending.status,"error");
  pending.dispose();
  const loaded = harness();
  loaded.send();
  loaded.timeout();
  assert.equal(loaded.status,"ready");
  loaded.dispose();
});

test("retry replaces the iframe and releases the preceding listener and timeout", () => {
  const h = harness();
  const firstKey = findElement(h.tree,"iframe").key;
  h.timeout(); h.render();
  findElement(h.tree,"button").props.onClick();
  assert.equal(h.status,"loading");
  h.render();
  assert.notEqual(findElement(h.tree,"iframe").key,firstKey);
  assert.equal(h.events.size,1);
  assert.equal(h.timers.size,1);
  h.send(); assert.equal(h.status,"ready");
  h.dispose();
});

test("seat changes reset readiness and leave just one active listener/timer", () => {
  const h = harness();
  h.send(); assert.equal(h.status,"ready");
  h.render("buffett");
  assert.equal(h.status,"loading");
  assert.match(h.render(),/seat=buffett"/);
  assert.equal(h.events.size,1);
  assert.equal(h.timers.size,1);
  h.timeout(); assert.equal(h.status,"error");
  h.dispose();
});

test("unmount clears listeners and timers without invoking financial APIs", () => {
  const h = harness();
  h.dispose();
  assert.equal(h.events.size,0);
  assert.equal(h.timers.size,0);
  h.send(); assert.equal(h.status,"loading");
  assert.doesNotMatch(source, /fetch\s*\(|contest-api|XMLHttpRequest|setInterval\s*\(|localStorage|api_key/);
});

test("only a mounted same-origin news frame may report an expired session",()=>{
  const h=harness(),message={channel:"news-earth",type:"session-expired"};
  h.send(message,{origin:"https://untrusted.example"});h.send(message,{source:{}});
  assert.deepEqual(h.notices,[]);h.send(message);
  assert.deepEqual(h.notices,["contest-session-expired"]);h.dispose();
});
