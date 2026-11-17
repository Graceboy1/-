"use strict";

// Run the actual account/model gate with deterministic hooks and an in-memory
// transport. These are test-only values: no browser, server or provider is used.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const source = fs.readFileSync(path.join(__dirname, "../app/workspace-gate.tsx"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../app/workspace-gate.css"), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target:ts.ScriptTarget.ES2022, module:ts.ModuleKind.CommonJS, jsx:ts.JsxEmit.ReactJSX },
  fileName:"workspace-gate.tsx",
}).outputText;

const PASSWORD = "fixture-pass-123";
const accountStatus = (overrides = {}) => ({
  account_exists:true, authenticated:false, ready:false, username:null,
  ai_available:false, model:null, server_model_available:false, ...overrides,
});
const reply = (data, status = 200) => ({ok:status >= 200 && status < 300,status,json:async()=>data});
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise,resolve};
}
function elements(node, predicate, output = []) {
  if (!node || typeof node !== "object") return output;
  if (predicate(node)) output.push(node);
  React.Children.forEach(node.props?.children, child => elements(child,predicate,output));
  return output;
}
function textContent(node) {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  return React.Children.toArray(node.props?.children).map(textContent).join("");
}

function harness(initialStatus) {
  let index = 0, tree, activeForm = null, activeFormKey = null, assignedAccountRef = null;
  const slots = [], effects = [], events = new Map(), responses = [reply(initialStatus)], calls = [];
  const stats = {storage:0,intros:0,focused:0,unexpectedRequests:0};
  class Input { constructor(value = "") { this.value = value; } }
  class FormDataStub {
    constructor(form) { this.data = Object.fromEntries(Object.entries(form.inputs).map(([key,input])=>[key,input.value])); }
    get(name) { return this.data[name] ?? null; }
  }
  const window = {
    addEventListener:(name,callback)=>events.set(name,callback),
    removeEventListener(name,callback) { if(events.get(name)===callback)events.delete(name); },
  };
  for (const name of ["localStorage","sessionStorage"]) Object.defineProperty(window,name,{
    get() { stats.storage++; throw new Error("Account secrets must not use browser storage"); },
  });
  const changed = (before,after) => !before || after.some((value,i)=>!Object.is(value,before[i]));
  const mockReact = {
    ...React,
    useState(initial) {
      const i=index++;
      if(!(i in slots))slots[i]={value:typeof initial==="function"?initial():initial};
      return [slots[i].value,value=>{slots[i].value=typeof value==="function"?value(slots[i].value):value;}];
    },
    useRef(initial) { const i=index++; if(!(i in slots))slots[i]={current:initial}; return slots[i]; },
    useCallback(callback,deps) {
      const i=index++;
      if(changed(slots[i]?.deps,deps))slots[i]={value:callback,deps:[...deps]};
      return slots[i].value;
    },
    useEffect(callback,deps) {
      const i=index++;
      if(changed(slots[i]?.deps,deps)) {
        const previous=slots[i]; slots[i]={deps:[...deps],cleanup:previous?.cleanup};
        effects.push(()=>{slots[i].cleanup?.();slots[i].cleanup=callback();});
      }
    },
  };
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,module:{exports},window,
    document:{getElementById:()=>({focus(){stats.focused++;}})},
    AbortController,FormData:FormDataStub,HTMLInputElement:Input,
    async fetch(url,options) {
      assert.match(url,/^\/contest-api\/workspace\/(status|login|register|model|offline|logout)$/);
      const call={url,options,payload:options.body?JSON.parse(options.body):null}; calls.push(call);
      const next=responses.shift();
      if(!next){stats.unexpectedRequests++;throw new Error("Unexpected test request");}
      return typeof next==="function"?await next():await next;
    },
    require(name) {
      if(name==="react")return mockReact;
      if(name==="./workspace-gate.css")return {};
      if(name==="./brand")return {APP_NAME:"智慧交易助手"};
      if(name==="./startup-gate")return {default:({children})=>{stats.intros++;return children;}};
      return require(name);
    },
  },{filename:"workspace-gate.tsx"});

  function expand(node) {
    if(!node || typeof node!=="object")return node;
    if(Array.isArray(node))return node.map(expand);
    if(typeof node.type==="function")return expand(node.type(node.props));
    const children=React.Children.map(node.props?.children,expand);
    return React.cloneElement(node,{},children);
  }
  function render() {
    index=0;
    tree=expand(exports.default({children:React.createElement("main",{id:"real-workspace"},"研究工作台")}));
    const forms=elements(tree,node=>node.type==="form");
    const form= forms[0] ?? null;
    const key=form ? (form.props.ref ? `account:${form.key}` : "model") : null;
    if(key!==activeFormKey){activeFormKey=key;activeForm=null;}
    if(form && !activeForm){
      const inputs=Object.fromEntries(elements(form,node=>node.type==="input").map(node=>[node.props.name,new Input(node.props.value??"")]));
      activeForm={inputs,elements:{namedItem:name=>inputs[name]??null}};
    }
    if(assignedAccountRef && assignedAccountRef!==form?.props.ref)assignedAccountRef.current=null;
    assignedAccountRef=form?.props.ref??null;
    if(assignedAccountRef)assignedAccountRef.current=activeForm;
    for(const effect of effects.splice(0))effect();
    return renderToStaticMarkup(tree);
  }
  async function settle() { for(let i=0;i<40;i++)await Promise.resolve(); return render(); }
  function button(label) {
    const node=elements(tree,item=>item.type==="button" && textContent(item)===label)[0];
    assert.ok(node,`Missing button: ${label}`); return node;
  }
  function click(label) { button(label).props.onClick(); return render(); }
  function submit(values) {
    const form=elements(tree,node=>node.type==="form")[0]; assert.ok(form,"Missing form");
    for(const [name,value] of Object.entries(values)) {
      assert.ok(activeForm.inputs[name],`Missing input: ${name}`);activeForm.inputs[name].value=value;
    }
    const submitted=activeForm;let prevented=0;
    form.props.onSubmit({currentTarget:submitted,preventDefault(){prevented++;}});
    assert.equal(prevented,1);render();return submitted;
  }
  function dispose() {
    for(const slot of slots)if(slot?.cleanup){const cleanup=slot.cleanup;slot.cleanup=null;cleanup();}
    assert.equal(stats.storage,0);assert.equal(stats.unexpectedRequests,0);
  }
  render();
  return {render,settle,click,submit,button,dispose,slots,calls,events,stats,
    enqueue:response=>responses.push(response),
    get form(){return activeForm;},get tree(){return tree;},
    expire(){events.get("contest-session-expired")?.();render();},
  };
}

async function open(t,status = accountStatus()) {
  const h=harness(status); t.after(()=>h.dispose()); await h.settle(); return h;
}
const registration = (overrides={}) => ({username:"测试用户",password:PASSWORD,confirm_password:PASSWORD,...overrides});

test("existing accounts start at login and can register without an SMS or setup step",async t=>{
  const h=await open(t);const markup=h.render();
  assert.match(markup,/登录智慧交易助手/);assert.match(markup,/没有账号？/);
  assert.match(markup,/aria-label="账号入口"/);assert.equal(h.button("账号登录").props["aria-pressed"],true);
  assert.ok(h.button("注册账号"));assert.doesNotMatch(markup,/确认密码|短信|手机号|绑定|发送验证码/);
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,"/contest-api/workspace/status");
  assert.doesNotMatch(source,/\/sms\b|phone_status|smsRequest|SmsForm|workspace\/setup/);
  assert.doesNotMatch(css,/workspace-sms|workspace-back/);
});

test("a fresh installation starts at registration but also allows existing-account login",async t=>{
  const h=await open(t,accountStatus({account_exists:false}));
  assert.match(h.render(),/确认密码/);assert.equal(h.button("注册账号").props["aria-pressed"],true);
  assert.equal(h.form.inputs.confirm_password.value,"");
  h.click("账号登录");assert.match(h.render(),/登录智慧交易助手/);assert.doesNotMatch(h.render(),/确认密码/);
  h.click("注册账号");assert.match(h.render(),/确认密码/);assert.equal(h.calls.length,1);
});

test("switching account mode clears password fields and previous validation errors",async t=>{
  const h=await open(t);const oldForm=h.form;
  oldForm.inputs.password.value=PASSWORD;h.click("注册账号");assert.equal(oldForm.inputs.password.value,"");
  const registerForm=h.submit(registration({confirm_password:"different-fixture"}));
  assert.match(h.render(),/两次输入的密码不一致/);
  registerForm.inputs.password.value=PASSWORD;registerForm.inputs.confirm_password.value=PASSWORD;
  h.click("账号登录");assert.equal(registerForm.inputs.password.value,"");assert.equal(registerForm.inputs.confirm_password.value,"");
  assert.doesNotMatch(h.render(),/两次输入的密码不一致/);assert.equal(h.calls.length,1);
});

for(const [label,values,message] of [
  ["short username",{username:"x"},/账号须为/],
  ["unsupported username",{username:"user name"},/账号须为/],
  ["long username",{username:"x".repeat(33)},/账号须为/],
  ["short password",{password:"short",confirm_password:"short"},/密码须为 10–128 位/],
  ["long password",{password:"x".repeat(129),confirm_password:"x".repeat(129)},/密码须为 10–128 位/],
  ["confirmation mismatch",{confirm_password:"different-fixture"},/两次输入的密码不一致/],
])test(`registration validates ${label} locally and clears both secrets`,async t=>{
  const h=await open(t,accountStatus({account_exists:false}));const form=h.submit(registration(values));
  assert.match(h.render(),message);assert.equal(h.calls.length,1);
  assert.equal(form.inputs.password.value,"");assert.equal(form.inputs.confirm_password.value,"");
});

test("successful registration posts confirmation and opens model configuration, not the workspace",async t=>{
  const h=await open(t);h.click("注册账号");
  h.enqueue(reply(accountStatus({authenticated:true,username:"测试用户"})));
  const form=h.submit(registration({username:" 测试用户 "}));
  assert.equal(form.inputs.password.value,"");assert.equal(form.inputs.confirm_password.value,"");
  const markup=await h.settle();
  assert.equal(h.calls[1].url,"/contest-api/workspace/register");
  assert.deepEqual(h.calls[1].payload,registration());
  assert.match(markup,/连接模型 API/);assert.match(markup,/先用离线模式/);
  assert.doesNotMatch(markup,/real-workspace|确认密码/);assert.equal(h.stats.intros,0);
  assert.equal(h.stats.storage,0);
});

test("duplicate registration remains on registration and preserves the login option",async t=>{
  const h=await open(t);h.click("注册账号");h.enqueue(reply({detail:"该账号已存在，请直接登录或更换账号。"},409));
  h.submit(registration());const markup=await h.settle();
  assert.match(markup,/该账号已存在/);assert.match(markup,/确认密码/);
  assert.equal(h.button("注册账号").props["aria-pressed"],true);assert.ok(h.button("账号登录"));
  assert.equal(h.calls.length,2);h.click("账号登录");assert.doesNotMatch(h.render(),/该账号已存在/);
});

test("an in-flight registration disables switches and does not submit twice",async t=>{
  const h=await open(t,accountStatus({account_exists:false}));const pending=deferred();h.enqueue(pending.promise);
  h.submit(registration());assert.equal(h.button("账号登录").props.disabled,true);
  h.submit(registration());h.click("账号登录");
  assert.equal(h.calls.length,2);assert.match(h.render(),/确认密码/);assert.match(h.render(),/正在处理/);
  pending.resolve(reply(accountStatus({authenticated:true,username:"测试用户"})));await h.settle();
  assert.match(h.render(),/连接模型 API/);
});

test("legacy login remains username/password only and clears its password",async t=>{
  const h=await open(t);h.enqueue(reply(accountStatus({authenticated:true,username:"legacy"})));
  const form=h.submit({username:"legacy",password:"old-fixture"});await h.settle();
  assert.deepEqual(h.calls[1].payload,{username:"legacy",password:"old-fixture"});
  assert.equal(h.calls[1].url,"/contest-api/workspace/login");assert.equal(form.inputs.password.value,"");
  assert.match(h.render(),/连接模型 API/);
});

test("invalid login 401 is shown locally without an expiry refresh or secret echo",async t=>{
  const h=await open(t);h.enqueue(reply({detail:`incorrect password ${PASSWORD}`},401));
  h.submit({username:"legacy",password:PASSWORD});const markup=await h.settle();
  assert.match(markup,/工作区请求未完成（401）/);assert.doesNotMatch(markup,new RegExp(PASSWORD));
  assert.doesNotMatch(markup,/登录已过期/);assert.equal(h.calls.length,2);
});

test("registration errors never echo a submitted confirmation or store either password",async t=>{
  const h=await open(t,accountStatus({account_exists:false}));
  h.enqueue(reply({detail:`refused confirm_password=${PASSWORD}`},400));h.submit(registration());
  const markup=await h.settle();assert.match(markup,/工作区请求未完成（400）/);
  assert.doesNotMatch(markup,new RegExp(PASSWORD));assert.equal(h.stats.storage,0);
  for(const slot of h.slots)assert.notEqual(slot?.value,PASSWORD);
});

test("offline entry has no startup animation; only verified AI entry enables it",async t=>{
  const h=await open(t,accountStatus({authenticated:true,username:"fixture"}));
  h.enqueue(reply(accountStatus({authenticated:true,ready:true,username:"fixture"})));
  h.click("先用离线模式");const offline=await h.settle();
  assert.match(offline,/real-workspace/);assert.match(offline,/离线模式/);assert.equal(h.stats.intros,0);
  assert.equal(h.calls[1].url,"/contest-api/workspace/offline");
  assert.doesNotMatch(offline,/手机号|绑定|短信/);
  h.click("API 设置");h.enqueue(reply(accountStatus({authenticated:true,ready:true,username:"fixture",ai_available:true,model:"fixture-model"})));
  const form=h.submit({base_url:"https://api.deepseek.com",model:"fixture-model",api_key:"test-only-key"});
  assert.equal(form.inputs.api_key.value,"");const verified=await h.settle();
  assert.match(verified,/API 已验证/);assert.ok(h.stats.intros>0);assert.match(verified,/real-workspace/);
  assert.deepEqual(h.calls[2].payload,{base_url:"https://api.deepseek.com",model:"fixture-model",api_key:"test-only-key"});
});

test("canceling API settings preserves the workspace revision and performs no request",async t=>{
  const h=await open(t,accountStatus({authenticated:true,ready:true,username:"fixture"}));const revision=h.slots[5].value;
  h.click("API 设置");assert.match(h.render(),/返回工作台/);assert.match(h.render(),/class="workspace-content" hidden/);
  h.click("返回工作台");assert.match(h.render(),/real-workspace/);assert.doesNotMatch(h.render(),/连接模型 API/);
  assert.equal(h.slots[5].value,revision);assert.equal(h.calls.length,1);
});

test("protected 401 responses refresh status and return to account login",async t=>{
  const h=await open(t,accountStatus({authenticated:true,username:"fixture"}));
  h.enqueue(reply({detail:"expired"},401));h.enqueue(reply(accountStatus()));
  h.click("先用离线模式");const markup=await h.settle();
  assert.match(markup,/登录已过期，请重新登录/);assert.match(markup,/没有账号？/);
  assert.deepEqual(h.calls.map(call=>call.url),["/contest-api/workspace/status","/contest-api/workspace/offline","/contest-api/workspace/status"]);
});

test("session expiry clears account secrets, aborts pending registration and ignores its late response",async t=>{
  const h=await open(t,accountStatus({account_exists:false}));const pending=deferred();h.enqueue(pending.promise);
  const form=h.submit(registration());form.inputs.password.value=PASSWORD;form.inputs.confirm_password.value=PASSWORD;
  h.enqueue(reply(accountStatus()));h.expire();
  assert.equal(form.inputs.password.value,"");assert.equal(form.inputs.confirm_password.value,"");
  assert.equal(h.calls[1].options.signal.aborted,true);await h.settle();
  pending.resolve(reply(accountStatus({authenticated:true,ready:true,username:"stale"})));const markup=await h.settle();
  assert.match(markup,/登录已过期/);assert.doesNotMatch(markup,/real-workspace|stale/);
});

test("unmount clears entered secrets and aborts account transport without browser persistence",async t=>{
  const h=await open(t,accountStatus({account_exists:false}));const pending=deferred();h.enqueue(pending.promise);
  const form=h.submit(registration());form.inputs.password.value=PASSWORD;form.inputs.confirm_password.value=PASSWORD;
  h.dispose();assert.equal(form.inputs.password.value,"");assert.equal(form.inputs.confirm_password.value,"");
  assert.equal(h.calls[1].options.signal.aborted,true);assert.equal(h.events.size,0);
  pending.resolve(reply(accountStatus({authenticated:true,username:"late"})));await h.settle();
  assert.equal(h.slots[0].value.authenticated,false);assert.equal(h.stats.storage,0);
});

test("all account requests stay same-origin, reject redirects and avoid every SMS endpoint",async t=>{
  const h=await open(t);h.click("注册账号");h.enqueue(reply({detail:"账号不可用"},409));h.submit(registration());await h.settle();
  h.click("账号登录");h.enqueue(reply(accountStatus({authenticated:true,username:"fixture"})));h.submit({username:"fixture",password:PASSWORD});await h.settle();
  for(const {url,options} of h.calls){
    assert.match(url,/^\/contest-api\/workspace\//);assert.doesNotMatch(url,/sms|phone|setup/);
    assert.equal(options.credentials,"same-origin");assert.equal(options.redirect,"error");assert.equal(options.cache,"no-store");
  }
  assert.equal(h.calls[0].options.method,"GET");
  for(const call of h.calls.slice(1))assert.equal(call.options.method,"POST");
});
