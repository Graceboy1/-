"use strict";

// Render the real desk JSX with deterministic hook state, without mounting
// effects, making network requests, or changing the backend fixture identity.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const CUTOFF = "2026-10-23";
const HASH = "a".repeat(64);
const SEATS = [
  { id: "livermore", label: "利弗莫尔席位", description: "低位与竞价规则", modes: ["offline"] },
  { id: "buffett", label: "巴菲特席位", description: "价值与安全边际规则", modes: ["offline"] },
  { id: "baseline", label: "基础演示席位", description: "通用预案规则", modes: ["offline", "ai"] },
];
const CASES = [
  { id: "livermore-normal", seat_id: "livermore", label: "低位涨停 · 竞价通过", symbol: "DEMO-LV01", name: "利弗莫尔合成样例01" },
  { id: "buffett-quality", seat_id: "buffett", label: "质量与安全边际通过", symbol: "DEMO-BF01", name: "价值风格合成样例01" },
  { id: "baseline-normal", seat_id: "baseline", label: "价格区间与成交确认", symbol: "DEMO001", name: "合成演示01" },
].map(sample => ({ ...sample, decision_date: CUTOFF, description: "原始情景说明，不是股票名称。" }));
const BARS = [
  { date: "2026-10-21", open: 10, high: 10.4, low: 9.8, close: 10.2, volume: 1000 },
  { date: "2026-10-22", open: 10.2, high: 10.6, low: 10, close: 10.4, volume: 1100 },
  { date: CUTOFF, open: 10.4, high: 11, low: 10.3, close: 11, volume: 1800 },
].map(bar => ({ ...bar, limit_up: 12, limit_down: 9, suspended: false }));

function frozenRun(sample, seat) {
  const evidenceId = `${sample.id}:bar:${CUTOFF}`;
  return {
    run_id: "private-run-identity", mode: "offline", model: "离线规则", seat,
    engine_version: "presentation-test",
    evidence: {
      case_id: sample.id, symbol: sample.symbol, decision_date: CUTOFF,
      bars: BARS, evidence_ids: [evidenceId],
      metrics: { data_valid: true, bar_count: 3, tradable_count: 3, last_close: 11, sma5: 10.3, sma20: 10.1, return_5d_pct: 4.5, volume_ratio_5d: 1.8 },
    },
    plan: { action: "simulate", rationale: "按冻结证据核验后生成条件预案。", evidence_ids: [evidenceId], entry_low: 10, entry_high: 11, position_pct: 5, stop_loss: 9.5, take_profit: 12 },
    execution_policy: { uses_price_thresholds: seat.id === "baseline", limit_up_deferrals: 1 },
    validation: { ok: true, checks: [{ code: "history", label: "历史时点", passed: true, detail: "仅使用决策日及以前的证据。" }] },
    trace: [{ step: "freeze", title: "预案已冻结", detail: "已保留原始证据身份。" }],
    frozen_at: `${CUTOFF}T07:00:00Z`, decision_hash: HASH,
  };
}

function completedReplay(seatId) {
  return {
    seat_id: seatId, status: "completed", reason: "按冻结预案回放完成。",
    events: [{ date: "2026-10-26", type: "buy", price: 11, detail: "按可成交价格模拟买入。" }],
    replay_bars: [{ ...BARS[2], date: "2026-10-26", close: 11.2, high: 11.4 }],
    metrics: { starting_cash: 100000, ending_cash: 100500, ending_equity: 100500, realized_pnl: 500, return_pct: 0.5, fees: 5, shares: 100, holding_sessions: 1, is_open: false },
  };
}

function renderDesk({ view = "research", seatId = "livermore", loaded = true, withRun = false, selectedMissing = false, historical = false, replayOverride, fundamentals } = {}) {
  const sample = historical ? {id:"history-20261113-600000",seat_id:seatId,symbol:"600000",name:"测试银行",label:"600000 测试银行 · 历史行情",decision_date:"2026-11-12",description:"真实日线条件研究"} : CASES.find(item => item.seat_id === seatId);
  const seat = SEATS.find(item => item.id === seatId);
  const catalog = loaded ? { seats: SEATS, cases: selectedMissing ? [] : historical ? [...CASES,sample] : CASES, ai_available: false } : null;
  const run = withRun ? frozenRun(sample, seat) : null;
  if (historical && run) {
    if(fundamentals)run.evidence.fundamentals=fundamentals;
    run.evidence.decision_date="2026-11-12";
    run.evidence.bars=[{...BARS[2],date:"2026-11-12"}];
    run.evidence.evidence_ids=[`${sample.id}:bar:2026-11-12`];
    run.plan.evidence_ids=[`${sample.id}:bar:2026-11-12`];
    run.frozen_at="2026-11-16T07:00:00Z";
    run.validation={ok:false,checks:[{code:"execution_data",label:"真实成交依据",passed:false,detail:"未取得执行证据"}]};
  }
  // These correspond to DecisionDesk's explicit useState declarations. An
  // added hook must deliberately extend this harness instead of silently
  // switching the meaning of a tested state slot.
  const states = [view, catalog, "", 0, loaded ? seatId : "", loaded ? sample.id : "", "offline", run, replayOverride ?? (view === "replay" && withRun && !historical ? completedReplay(seatId) : null), null, null,
    {recordId:null,context:null,archived:false,historyVersion:0,historyOpen:false,loading:false,error:""},
    {available:false,loading:false,error:"",sample:null,preview:null,endDate:"2026-11-13",decisionDate:"2026-11-12"}];
  // NewsDesk is mounted only for the fourth tab and has its own two states.
  if (view === "news") states.push("loading", 0);
  let hookIndex = 0;
  const mockReact = {
    ...React,
    useState() {
      assert.ok(hookIndex < states.length, "DecisionDesk added a state hook; update its presentation harness.");
      return [states[hookIndex++], () => {}];
    },
    useEffect() {},
    useRef(value) { return { current: value }; },
  };
  const cache = new Map();
  function loadComponent(relativeName) {
    // History owns independent, asynchronous paging state; its behavior has a
    // separate controller harness instead of sharing DecisionDesk's state slots.
    if (relativeName === "research-history" || relativeName === "historical-market") return {default:() => null};
    if (relativeName === "paper-trading-desk") return {default:({view}) => React.createElement("section",{"data-paper-view":view},"自主模拟交易账户")};
    const filename = path.join(__dirname, "../app", `${relativeName}.tsx`);
    if (cache.has(filename)) return cache.get(filename);
    const output = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, strict: true },
      fileName: filename,
    }).outputText;
    const exports = {};
    const context = {
      exports, module: { exports }, URL,
      require(name) {
        if (name === "react") return mockReact;
        if (name.startsWith("./")) return loadComponent(name.slice(2));
        return require(name);
      },
    };
    vm.runInNewContext(output, context, { filename });
    cache.set(filename, context.module.exports);
    return context.module.exports;
  }
  const Desk = loadComponent("decision-desk").default;
  const markup = renderToStaticMarkup(React.createElement(Desk));
  assert.equal(hookIndex, states.length, "DecisionDesk changed its state slots; review this fixture.");
  return { markup, sample, seat, run };
}

function assertCleanIdentity(markup, sample) {
  assert.ok(!markup.includes(sample.symbol), "The display must not expose the synthetic ticker.");
  assert.ok(!markup.includes(sample.name), "The display must not expose the generated sample name.");
  assert.doesNotMatch(markup, /DEMO(?:-|\d)|合成样例|合成演示\d/);
  assert.ok(markup.includes(sample.label), "The business scenario must remain identifiable.");
  assert.ok(markup.includes(CUTOFF), "The actual decision date must not be hidden or rewritten.");
}

for (const seat of SEATS) {
  test(`${seat.id} research renders its scenario without DEMO ticker or sample name`, () => {
    const { markup, sample } = renderDesk({ seatId: seat.id });
    assertCleanIdentity(markup, sample);
    assert.match(markup, /旧研究记录/);
    if (seat.id === "baseline") {
      assert.match(markup, /基础策略席位/);
      assert.doesNotMatch(markup, /基础演示席位/);
    } else assert.ok(markup.includes(seat.label));
    assert.doesNotMatch(markup, /合成规则演示<\/button>/);
    assert.match(markup, /生成并冻结预案/);
  });

  test(`${seat.id} replay title uses the scenario instead of DEMO identity`, () => {
    const { markup, sample } = renderDesk({ view: "replay", seatId: seat.id, withRun: true });
    assertCleanIdentity(markup, sample);
    assert.ok(markup.includes(`${sample.label} · 日线回放`));
    assert.match(markup, /回放明细/);
    assert.match(markup, /2026-10-26/);
    assert.match(markup, /初始虚拟资金/);
  });

  test(`${seat.id} audit keeps raw evidence identity and frozen hash`, () => {
    const { markup, sample } = renderDesk({ view: "audit", seatId: seat.id, withRun: true });
    assertCleanIdentity(markup, sample);
    assert.ok(markup.includes(`${sample.id}:bar:${CUTOFF}`));
    assert.ok(markup.includes(HASH));
    assert.match(markup, /引用证据/);
    assert.match(markup, /风控校验/);
    assert.match(markup, /预案已冻结/);
    if (seat.id === "baseline") {
      assert.match(markup, /基础策略席位/);
      assert.doesNotMatch(markup, /基础演示席位/);
    } else assert.ok(markup.includes(seat.label));
  });
}

test("loading state has useful labels without placeholder stock identity", () => {
  const { markup } = renderDesk({ loaded: false });
  assert.doesNotMatch(markup, /DEMO(?:-|\d)|合成样例|合成演示\d/);
  assert.match(markup, /正在读取席位/);
  assert.match(markup, /正在读取研究情景/);
});

test("autonomous accounts are the primary desk and the selected single stock is a closed auxiliary tool", () => {
  const {markup}=renderDesk({view:"research",seatId:"baseline",historical:true,withRun:true});
  const primary=markup.indexOf('data-paper-view="research"');
  const auxiliary=markup.indexOf('<details class="manual-research"');
  const identity=markup.indexOf("600000 测试银行");
  assert.ok(primary>=0 && auxiliary>primary && identity>auxiliary);
  assert.match(markup,/<details class="manual-research"><summary>单股研究工具/);
  assert.doesNotMatch(markup,/<details class="manual-research"[^>]*\bopen/);
});

for (const view of ["research","replay","audit"]) for(const seat of SEATS) {
  test(`real history ${seat.id} ${view} exposes code and separate data dates without inventing a trade`, () => {
    const {markup}=renderDesk({view,seatId:seat.id,historical:true,withRun:true});
    assert.match(markup,/600000 测试银行/);
    assert.match(markup,/档案行情截至 2026-11-13/);
    assert.match(markup,/决策证据截至 2026-11-12/);
    assert.match(markup,/避免使用未来信息，不是行情没更新/);
    assert.match(markup,/尚未回放 · 无成交记录/);
    assert.match(markup,/当前模拟持仓/);
    assert.match(markup,/未建立/);
    assert.match(markup,/不编造成交与收益/);
    assert.doesNotMatch(markup,/2026-10-23/);
    assert.doesNotMatch(markup,/本次模拟买卖账本/);
  });
}

test("synthetic filled replay shows an explicit single-position trade ledger from saved events", () => {
  const replay={...completedReplay("baseline"),events:[
    {date:"2026-10-26",type:"buy",price:11,detail:"虚拟开盘买入100股"},
    {date:"2026-10-27",type:"sell",price:11.5,detail:"虚拟收盘卖出100股"},
  ]};
  const {markup}=renderDesk({view:"replay",seatId:"baseline",withRun:true,replayOverride:replay});
  assert.match(markup,/旧合成研究记录 · 非真实股票/);
  assert.match(markup,/本次模拟买卖账本/);
  assert.match(markup,/2026-10-26 · 开盘/);
  assert.match(markup,/2026-10-27 · 收盘/);
  assert.match(markup,/1,100.00 元/);
  assert.match(markup,/1,150.00 元/);
  assert.match(markup,/成交金额（未扣费用）/);
  assert.match(markup,/已平仓/);
  assert.match(markup,/各研究资金独立，不能相加当作账户收益/);
});

test("real historical observation never displays idle cash as simulated trading performance", () => {
  const replay={...completedReplay("buffett"),status:"hold",observation_only:true,events:[{date:"2026-11-12",type:"hold",price:null,detail:"观望，不创建订单"}],metrics:{starting_cash:100000,ending_equity:100000,return_pct:0,shares:0,open_shares:0,is_open:false}};
  const {markup}=renderDesk({view:"replay",seatId:"buffett",historical:true,withRun:true,replayOverride:replay});
  assert.match(markup,/真实历史观察/);
  assert.match(markup,/没有持仓，不统计成交收益/);
  assert.match(markup,/仅观察后续价格 · 未产生买卖收益/);
  assert.doesNotMatch(markup,/期末虚拟权益|初始虚拟资金|虚拟净值 0/);
  assert.doesNotMatch(markup,/本次模拟买卖账本/);
});

test("a blocked replay with no fills never displays a trade ledger or fabricated position", () => {
  const {markup}=renderDesk({view:"replay",withRun:true,replayOverride:{status:"blocked",reason:"证据不足",events:[],metrics:{shares:0,is_open:false}}});
  assert.doesNotMatch(markup,/本次模拟买卖账本/);
  assert.match(markup,/无持仓/);
});

test("legacy replay without position fields is shown as unknown, not falsely closed", () => {
  const replay={...completedReplay("baseline"),status:"pending",metrics:{}};
  const {markup}=renderDesk({view:"replay",seatId:"baseline",withRun:true,replayOverride:replay});
  assert.match(markup,/状态未知 · 待核对/);
  assert.doesNotMatch(markup,/已平仓|1,100.00 元/);
});

test("real financial references show sourced figures and version gaps without becoming decision evidence",()=>{
  const fundamentals={status:"insufficient",eligible:false,issues:["缺少历史原版财报与人工业务确认"],reports:[],assessment:null,valuation:null,checks:[],
    data_status:"reference_only",data_provenance:{source:"测试公开财务源",source_url:"https://emweb.securities.eastmoney.com/PC_HSF10/FinanceAnalysis/Index?type=web&code=SH600000",retrieved_at:"2026-11-16T08:00:00Z",point_in_time_verified:false},
    raw_reports:[{period_end:"2025-12-31",available_on:"2026-03-20",operating_cashflow:1234.56,reported_capex:234.56,net_profit:800,roe_pct:12,debt_to_equity:1.5,conservative_total_capex_proxy:1000,point_in_time_verified:false}],
    limitations:["总开支不能替代维持性资本开支"]};
  const {markup}=renderDesk({seatId:"buffett",historical:true,withRun:true,fundamentals});
  assert.match(markup,/600000 测试银行/);assert.match(markup,/真实财务资料核对/);
  assert.match(markup,/原始历史版本、重述情况尚未核验/);assert.match(markup,/未作为当时已知的决策证据/);
  assert.match(markup,/1,234.56/);assert.match(markup,/234.56/);assert.match(markup,/总负债 \/ 权益/);
  assert.match(markup,/不等于有息债务/);assert.match(markup,/总开支不能替代维持性资本开支/);
  assert.match(markup,/noopener noreferrer/);assert.doesNotMatch(markup,/含安全边际买入上限/);
});

test("missing financial reference values are not converted to zeros and unsafe sources cannot become links",()=>{
  const fundamentals={status:"insufficient",eligible:false,issues:[],reports:[],assessment:null,valuation:null,checks:[],data_status:"reference_only",
    raw_reports:[{period_end:"2025-12-31",available_on:"2026-03-20",operating_cashflow:null,reported_capex:null,net_profit:null}],data_provenance:{source:"<img src=x onerror=alert(1)>",source_url:"javascript:alert(1)"}};
  const {markup}=renderDesk({seatId:"buffett",historical:true,withRun:true,fundamentals});
  assert.match(markup,/&lt;img/);assert.doesNotMatch(markup,/href="javascript:|<img src=x/);
  assert.match(markup,/<td>—<\/td>/);assert.doesNotMatch(markup,/<td>0.00<\/td>/);
});

test("Buffett data checklist distinguishes downloaded references, original-version checks and human confirmation",()=>{
  const fundamentals={status:"insufficient",eligible:false,issues:["缺少人工确认"],reports:[],assessment:null,valuation:null,checks:[],data_status:"reference_only",raw_reports:[],
    data_coverage:{expected_years:["2023-12-31","2024-12-31","2025-12-31"],reference_year_count:3,items:[
      {key:"annual_reports",label:"三年公开财报参考",status:"reference_only",available_count:3,required_count:3,detail:"只是参考，不是交易放行。"},
      {key:"point_in_time",label:"原始披露版本",status:"unverified",available_count:0,required_count:3,detail:"核对原始年报。"},
      {key:"maintenance_capex",label:"维持性资本开支",status:"missing",available_count:0,required_count:3,detail:"总开支不替代维持开支。"},
      {key:"moat_supported",label:"护城河证据",status:"manual_required",available_count:0,required_count:1,detail:"<script>不能自动勾选</script>"},
    ]}};
  const {markup}=renderDesk({seatId:"buffett",historical:true,withRun:true,fundamentals});
  assert.match(markup,/巴菲特数据补齐清单/);assert.match(markup,/参考完整年度 3 \/ 3/);
  for(const label of ["已下载参考","待原文核验","仍缺失","待人工确认","人工判断","下载完成不等于买入条件通过"])assert.ok(markup.includes(label));
  assert.match(markup,/&lt;script/);assert.doesNotMatch(markup,/<script>不能自动勾选/);
  assert.doesNotMatch(markup,/含安全边际买入上限/);
});

test("original-value evidence can show a passed objective check while preserving missing PIT and human gates",()=>{
  const fundamentals={status:"insufficient",eligible:false,financial_valid:false,assessment_valid:false,annual_structure_valid:true,
    issues:["历史更正版本仍待核验","缺少维持性开支与人工确认"],assessment:null,valuation:null,
    reports:[{period_end:"2025-12-31",available_on:"2026-03-31",operating_cashflow:1234.56,maintenance_capex:null,net_profit:800,roe_pct:18,debt_to_equity:.2,shares_outstanding:null,point_in_time_verified:false,
      field_evidence:{net_profit:{page:8,unit:"千元",quote:"归属于上市公司股东的净利润 800,000",source_url:"https://static.cninfo.com.cn/finalpage/2026-03-31/1225065145.PDF",document_sha256:HASH,verification_status:"matched_original"}}}],
    checks:[{code:"profit",label:"持续正利润",passed:true,detail:"已与原始年报核对"},{code:"history",label:"年度财务证据",passed:false,detail:"仍缺严格版本核验"}]};
  const {markup}=renderDesk({seatId:"buffett",historical:true,withRun:true,fundamentals});
  assert.match(markup,/原文已核对事实，仍待历史更正版本与人工评估/);
  assert.match(markup,/最终买入门槛：未通过，保持观望/);assert.match(markup,/满足<\/span><div><strong>持续正利润/);
  assert.match(markup,/原文逐字段核对依据（1项）/);assert.match(markup,/归母净利润/);assert.match(markup,/<td>8<\/td>/);
  assert.match(markup,/href="https:\/\/static\.cninfo\.com\.cn\/finalpage\//);assert.match(markup,/此字段已与原文数值核对，历史版本另行核验/);
  assert.match(markup,/原文财报事实与估值假设/);assert.doesNotMatch(markup,/含安全边际买入上限/);
});

test("field evidence accepts only fixed official disclosure hosts and does not leak markup from excerpts",()=>{
  const urls=["https://www.sse.com.cn/disclosure/report.pdf","https://disc.static.szse.cn/download/report.pdf","https://static.cninfo.com.cn/finalpage/2026-03-31/1225065145.PDF",
    "javascript:alert(1)","http://static.cninfo.com.cn/report.pdf","https://static.cninfo.com.cn.evil.test/report.pdf","https://user:secret@www.sse.com.cn/report.pdf","https://www.sse.com.cn:8080/report.pdf","https://unknown.szse.cn/report.pdf"];
  for(let index=0;index<urls.length;index++){
    const source_url=urls[index],fundamentals={status:"insufficient",eligible:false,issues:[],assessment:null,valuation:null,checks:[],reports:[{period_end:"2025-12-31",available_on:"2026-03-31",point_in_time_verified:false,field_evidence:{net_profit:{page:4,unit:"元",quote:'<img src=x onerror="bad">',source_url,document_sha256:HASH,verification_status:"matched_original"}}}]};
    const {markup}=renderDesk({seatId:"buffett",historical:true,withRun:true,fundamentals});
    assert.match(markup,/&lt;img/);assert.doesNotMatch(markup,/<img src/);
    if(index<3)assert.ok(markup.includes(`href="${source_url}"`));else assert.doesNotMatch(markup,/查看披露原文<\/a>/);
  }
});

test("a pending original without field proofs is not described as numerically verified",()=>{
  const fundamentals={status:"insufficient",eligible:false,issues:[],assessment:null,valuation:null,checks:[],reports:[{period_end:"2025-12-31",available_on:"2026-03-31",point_in_time_verified:false,field_evidence:{}}]};
  const {markup}=renderDesk({seatId:"buffett",historical:true,withRun:true,fundamentals});
  assert.match(markup,/原始报告已接入，字段与历史版本仍待核验/);assert.doesNotMatch(markup,/原文已核对事实，仍待/);
});

test("replay with a missing selected item does not fall back to the raw synthetic symbol", () => {
  const { markup, sample } = renderDesk({ view: "replay", seatId: "buffett", withRun: true, selectedMissing: true });
  assert.ok(!markup.includes(sample.symbol));
  assert.ok(!markup.includes(sample.name));
  assert.match(markup, /当前研究情景 · 日线回放/);
  assert.match(markup, /2026-10-23/);
});

test("news follows audit as the fourth accessible workbench tab", () => {
  const { markup } = renderDesk();
  const ids = [...markup.matchAll(/id="desk-tab-([^"]+)"/g)].map(match => match[1]);
  assert.deepEqual(ids, ["research", "replay", "audit", "news"]);
  const tabs = [...markup.matchAll(/<button\b[^>]*\bid="desk-tab-([^"]+)"[^>]*>([\s\S]*?)<\/button>/g)]
    .map(match => ({id: match[1], content: match[2]}));
  assert.deepEqual(tabs, [
    {id: "research", content: "研究台"},
    {id: "replay", content: "复盘台"},
    {id: "audit", content: "审计台"},
    {id: "news", content: "新闻页"},
  ], "Workbench navigation shows only its Chinese labels, without A/B/C/D prefixes or decorative spans");
  assert.match(markup, /id="desk-tab-news" role="tab" aria-selected="false" aria-controls="desk-panel-news"/);
  assert.match(markup, /id="desk-panel-news" role="tabpanel" aria-labelledby="desk-tab-news" hidden=""/);
});

for (const view of ["research", "replay", "audit"]) {
  test(`${view} does not mount or load the news iframe`, () => {
    const { markup } = renderDesk({view, withRun:true});
    assert.doesNotMatch(markup, /<iframe|class="news-desk"|\/news-earth\/index\.html/);
  });
}

for (const seat of SEATS) {
  test(`${seat.id} news uses a same-origin globe and only news context`, () => {
    const { markup, sample, run } = renderDesk({view:"news", seatId:seat.id, withRun:true});
    assert.match(markup, /id="desk-tab-news" role="tab" aria-selected="true"/);
    assert.match(markup, /id="desk-panel-news" role="tabpanel" aria-labelledby="desk-tab-news" tabindex="0"/);
    assert.match(markup, /title="新闻页：地球定位、板块关联与股票列表"/);
    assert.ok(markup.includes(`src="/news-earth/index.html?view=orbit&amp;embed=1&amp;mode=live&amp;seat=${seat.id}"`));
    assert.match(markup, /真实新闻聚合 · 关联待核验/);
    assert.match(markup, /<strong>全球新闻<\/strong>/);
    assert.doesNotMatch(markup, /全球新闻 · 轨道观测台/);
    assert.doesNotMatch(markup, /3008|决策日|2026-10-23|DEMO-|<h2[^>]*>冻结预案/);
    assert.ok(!markup.includes(sample.label), "Unrelated research case must not label the news page");
    assert.match(markup, /class="bottom-strip" role="status" hidden=""/);
    assert.equal(run.decision_hash, HASH, "Rendering news must not alter the frozen decision");
    assert.equal(run.evidence.case_id, sample.id);
  });
}

test("news selection shares the existing desk lifecycle without resetting the run or calling a model", () => {
  const source = fs.readFileSync(path.join(__dirname, "../app/decision-desk.tsx"), "utf8");
  assert.match(source, /onClick=\{\(\) => openDesk\(id\)\}/);
  const navigate = source.match(/function openDesk\(next: DeskView\) \{([\s\S]*?)\n  \}/)?.[1];
  assert.ok(navigate);
  assert.match(navigate, /setView\(next\)/);
  assert.doesNotMatch(navigate, /reset\(|setRun\(|setReplay\(|request\(|fetch\(|analyze\(/);
  assert.match(source, /\{view === "news" && <NewsDesk seatId=\{seatId\} onResearch=\{[^}]+\}/);
  const page = fs.readFileSync(path.join(__dirname, "../app/page.tsx"), "utf8");
  assert.match(page, /<WorkspaceGate><DecisionDesk \/><\/WorkspaceGate>/);
});
