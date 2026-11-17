'use strict';
// Static DOM/dependency contracts plus controller-only VM tests.
// These do not replace browser/canvas visual or native-pointer verification.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const GeoCore = require('./geo-core.js');
const appSource = fs.readFileSync(path.join(__dirname, 'earth-app.js'), 'utf8');
const rendererSource = fs.readFileSync(path.join(__dirname, 'earth-renderer.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, 'earth.css'), 'utf8');
const serverSource = fs.readFileSync(path.join(__dirname, 'serve_earth.py'), 'utf8');
const htmlIds = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
const generatedIds = ['retry', 'enter-sectors'];

test('static contract: every literal app ID exists in HTML or a generated template', () => {
  assert.equal(new Set(htmlIds).size, htmlIds.length, 'HTML IDs must be unique');
  const referenced = [...appSource.matchAll(/\$\('([^']+)'\)/g)].map(match => match[1]);
  for (const id of referenced) {
    assert.ok(htmlIds.includes(id) || generatedIds.includes(id), `Missing #${id}`);
  }
  for (const id of generatedIds) assert.ok(appSource.includes(`id="${id}"`));
  assert.ok(htmlIds.includes('scene-shell'), 'Globe and stock-list workspace must exist');
});

test('static contract: all scripts are deferred, local, ordered and available', () => {
  const scripts = [...html.matchAll(/<script\b([^>]+)>/g)].map(match => match[1]);
  const expected = ['vendor/d3-array.min.js', 'vendor/d3-geo.min.js',
    'vendor/topojson-client.min.js', 'geo-core.js', 'sphere-core.js',
    'earth-renderer.js', 'earth-app.js'];
  assert.deepEqual(scripts.map(attrs => attrs.match(/src="([^"]+)"/)?.[1]), expected);
  for (const attrs of scripts) assert.match(attrs, /\bdefer\b/);
  for (const src of expected) {
    assert.doesNotMatch(src, /^(https?:|\/\/|\/)/);
    const localPath = src === 'sphere-core.js'
      ? path.join(__dirname, '../news-spheres/sphere-core.js') : path.join(__dirname, src);
    assert.ok(fs.statSync(localPath).isFile(), `${src} must be vendored or a fixed server alias`);
  }
});

test('static contract: all three variants and renderer hooks are provided', () => {
  const variants = [...html.matchAll(/<button data-view="([^"]+)"/g)].map(match => match[1]);
  assert.deepEqual(variants, ['atlas', 'orbit', 'horizon']);
  for (const variant of variants) assert.ok(appSource.includes(`${variant}: {`));
  const context = {window: {}};
  vm.runInNewContext(rendererSource, context);
  const proto = context.window.EarthRenderer.prototype;
  for (const name of ['setWorld', 'setNews', 'setSelected', 'setVariant', 'setCamera', 'draw', 'destroy']) {
    assert.equal(typeof proto[name], 'function', `Renderer missing ${name}`);
  }
  assert.match(appSource, /new EarthRenderer\(stage,\{onPick:pickGroup\}\)/);
  assert.match(rendererSource, /this\.options\.onPick\(nearest\.group\)/);
});

test('static contract: no permanent interval or runtime remote dependency', () => {
  assert.doesNotMatch(appSource + rendererSource, /\bsetInterval\s*\(/);
  assert.doesNotMatch(appSource, /fetch\(\s*['"]https?:/);
  assert.match(html, /数据状态：模拟/);
  assert.match(html, /地理示意，非标准地图/);
});

test('static contract: one persistent header status replaces repeated demo badges', () => {
  const badge = html.match(/<header\b[^>]*>[\s\S]*?<span\b([^>]*class="demo"[^>]*)>数据状态：模拟<\/span>[\s\S]*?<\/header>/);
  assert.ok(badge, 'Simulation status must be visible in the shared header, not a tooltip-only notice');
  assert.equal([...html.matchAll(/>数据状态：模拟</g)].length, 1);
  assert.match(badge[1], /title="[^"]*新闻、地点及股票均为模拟数据[^"]*未接入实时新闻或行情[^"]*"/);
  assert.doesNotMatch(badge[1], /\bhidden\b|aria-hidden="true"|onclick=|role="button"|style=/);
  const badgeRules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter(match => match[1].includes('.demo'));
  assert.ok(badgeRules.length > 0);
  for (const rule of badgeRules) {
    assert.doesNotMatch(rule[2], /display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0\s*(?:;|$)/);
  }
  assert.doesNotMatch(html, /演示新闻 \/ 非实时|正在读取演示新闻|人工演示影响等级/);
});

test('static contract: raw fixture provenance and client synthetic guard are retained', () => {
  assert.match(serverSource, /"synthetic": True/);
  assert.match(serverSource, /"source": "本地虚构情景 · 非真实新闻"/);
  assert.match(serverSource, /"summary": f"假想新闻：\{summary\}"/);
  assert.match(serverSource, /"geo_mode": "synthetic_locations"/);
  assert.match(appSource, /payload\.news_mode!=='synthetic'/);
  assert.match(appSource, /payload\.preview_only!==true/);
  assert.match(appSource, /payload\.model_called!==false/);
});

function createHarness(options = {}) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      id, hidden: false, dataset: {}, style: {}, textContent: '', innerHTML: '',
      value: '', checked: true, disabled: false, clientWidth: 900, clientHeight: 330,
      scrollTop: 0, handlers: {}, classList: {add() {}, remove() {}},
      setAttribute() {}, addEventListener(type, handler) { this.handlers[type] = handler; },
      querySelectorAll() { return []; }, focus() {},
      getBoundingClientRect() { return {top: 0, bottom: 300, height: 300}; },
      setPointerCapture() {}, hasPointerCapture() { return false; }
    });
    return elements.get(id);
  }
  let now = 0, next = 1;
  const frames = new Map(), events = {}, windowEvents = {}, hostMessages = [], timers = new Map(), requests = [], replies = [];
  const document = {
    hidden: false, body: {dataset: {}},
    getElementById(id) { return htmlIds.includes(id) || generatedIds.includes(id) ? element(id) : null; },
    querySelectorAll() { return []; },
    addEventListener(type, handler) { events[type] = handler; }
  };
  let replacedURL = null;
  const context = {
    console, document, window: {addEventListener(type, handler) { windowEvents[type] = handler; }},
    sessionStorage: {getItem() { return options.saved ? JSON.stringify(options.saved) : null; }, setItem() {}},
    location: {search: options.search || '', href: `http://127.0.0.1:3008/${options.search || ''}`, origin:'http://127.0.0.1:3008', reload() {}},
    history: {replaceState(_state, _title, url) { replacedURL = String(url); }}, URL, URLSearchParams, GeoCore, AbortController,
    setTimeout(handler,ms){const id=next++;timers.set(id,{handler,ms});return id;},
    clearTimeout(id){timers.delete(id);},
    async fetch(url,options={}){
      requests.push({url,options});assert.ok(replies.length,`Unexpected request ${url}`);
      const reply=await replies.shift();
      return {status:reply.status??200,ok:(reply.status??200)<400,json:async()=>reply.data};
    },
    matchMedia() { return {matches: false}; }, performance: {now: () => now},
    requestAnimationFrame(handler) { const id = next++; frames.set(id, handler); return id; },
    cancelAnimationFrame(id) { frames.delete(id); }
  };
  context.window.parent=options.embedded?{postMessage(message,origin){hostMessages.push({message:JSON.parse(JSON.stringify(message)),origin});}}:context.window;
  // Expose private controller functions in test memory only; do not run fetch/init.
  const entry = '  init();';
  assert.equal(appSource.split(entry).length, 2, 'Expected one app entry point');
  const instrumented = appSource.replace(entry,
    '  window.testController={setup:(d,r)=>{data=d;renderer=r;},selectNews,pickGroup,rotate,back,renderInner,renderStockList,applyVariant,wire,state,stopFlight,refreshLive,adoptLive,renderLiveStatus,validateLive,init,analyzeNews,analysisFor,validateAiAnalysis};');
  vm.runInNewContext(instrumented, context);
  const app = context.window.testController;
  const item = (id, title, geo) => ({id, title, summary: '假想新闻：内容', time: '09:40（演示）', category: '演示', geo});
  const beijing = {lon: 116.4, lat: 39.9, country: '中国', label: '北京'};
  const rule = (eligible, rationale, code = 'gate') => ({eligible, rationale,
    passed: eligible ? 1 : 0, total: 1, checks: [{passed: eligible, code, label: `${code}规则`}]});
  const data = {
    news: [item('g1', '北京一', beijing), item('g2', '北京二', {...beijing}),
      item('g3', '美国', {lon: -77, lat: 39, country: '美国', label: '华盛顿'}), item('g4', '无坐标', null)],
    seats: [{id: 'livermore', label: '利弗莫尔席位'}, {id: 'buffett', label: '巴菲特席位'}],
    news_links: [
      {news_id: 'g1', sector_id: 'grid', direction: 'opportunity', impact_level: 'high', reason: '电网项目需求关联'},
      {news_id: 'g1', sector_id: 'storage', direction: 'risk', impact_level: 'medium', reason: '储能成本约束关联'},
      {news_id: 'g2', sector_id: 'grid', direction: 'opportunity', impact_level: 'low', reason: '另一新闻的独立关联'},
      {news_id: 'g3', sector_id: 'compute', direction: 'opportunity', impact_level: 'medium', reason: '海外计算设施关联'}
    ],
    sectors: [{id: 'grid', label: '电网'}, {id: 'storage', label: '储能'}, {id: 'compute', label: '算力'}],
    stock_links: [{sector_id: 'grid', stock_id: 's1'}, {sector_id: 'storage', stock_id: 's1'},
      {sector_id: 'grid', stock_id: 's2'}, {sector_id: 'storage', stock_id: 's3'}, {sector_id: 'compute', stock_id: 's4'}],
    stocks: [
      {id: 's1', label: '甲', rule_results: {livermore: rule(true, '利弗莫尔趋势依据'), buffett: rule(false, '巴菲特安全边际不足')}},
      {id: 's2', label: '乙', rule_results: {livermore: rule(false, '利弗莫尔条件未达'), buffett: rule(true, '巴菲特质量与价值依据')}},
      {id: 's3', label: '丙', rule_results: {livermore: rule(true, '利弗莫尔储能依据'), buffett: rule(true, '巴菲特储能依据')}},
      {id: 's4', label: '无关标的', rule_results: {livermore: rule(true, '其他新闻依据'), buffett: rule(true, '其他新闻依据')}}
    ]
  };
  const renderer = {
    camera: {lon: 0, lat: 0}, destroyed: false,
    setSelected(id) { this.selected = id; }, setCamera(camera) { this.camera = {...camera}; },
    setNews(news) {this.news=news;this.newsUpdates=(this.newsUpdates||0)+1;},
    setVariant(variant) { this.variant = variant; }, setWorld(world){this.world=world;},draw() {}, destroy() { this.destroyed = true; }
  };
  context.EarthRenderer=function(){return renderer;};
  app.setup(data, renderer); app.wire();
  function flush(time) {
    now = time; const pending = [...frames.values()]; frames.clear();
    for (const handler of pending) handler(time);
  }
  return {app, renderer, element, frames, flush, document, events, windowEvents, data, hostMessages, requests, replies, timers,
    reply(payload,status=200){replies.push({data:payload,status});},
    async timer(ms){const entry=[...timers].find(([,value])=>value.ms===ms);assert.ok(entry,`Missing ${ms}ms timer`);timers.delete(entry[0]);entry[1].handler();await new Promise(resolve=>setImmediate(resolve));},
    getReplacedURL: () => replacedURL};
}

function clickStockControl(element, kind, value) {
  const selector = kind === 'scope' ? '[data-stock-sector]' : kind==='research'?'[data-stock-research]':'[data-stock-detail]';
  const dataset = kind === 'scope' ? {stockSector: value} : kind==='research'?{stockResearch:value}:{stockDetail: value};
  element('relation-detail').handlers.click({target: {closest(query) {
    return query === selector ? {dataset} : null;
  }}});
}

function renderedStockIds(element) {
  return [...element('relation-detail').innerHTML.matchAll(/data-stock-row="([^"]+)"/g)].map(match => match[1]);
}

test('mocked controller: rapid selection cancels old flight and targets the newest news', () => {
  const {app, renderer, frames, flush, element} = createHarness();
  app.selectNews('g1'); assert.equal(frames.size, 1); flush(100);
  app.selectNews('g3'); assert.equal(frames.size, 1); flush(600);
  assert.equal(renderer.camera.lon, -77); assert.equal(renderer.camera.lat, 39);
  assert.equal(app.state.selected, 'g3'); assert.equal(frames.size, 0);
  assert.equal(element('globe-stage').dataset.locating, 'false');
});

test('mocked controller: selecting unlocated news cancels flight and preserves current view', () => {
  const {app, renderer, frames, flush, element} = createHarness();
  app.selectNews('g1'); flush(100); const previous = {...renderer.camera};
  app.selectNews('g4'); assert.equal(frames.size, 0);
  assert.deepEqual(renderer.camera, previous);
  assert.equal(renderer.selected, 'g4'); assert.equal(element('locate').disabled, true);
  assert.equal(element('globe-stage').dataset.locating, 'false');
});

test('mocked controller: province/city labels and camera target remain consistent across news views', () => {
  const {app, renderer, element, data, flush} = createHarness();
  data.news[0].geo = {lon: 114.0579, lat: 22.5431, country: '中国', country_id: '156',
    province: '广东省', city: '深圳市', label: '深圳'};
  const before = JSON.stringify(data.news[0].geo);
  app.selectNews('g1'); flush(500);
  for (const id of ['news-list', 'focus-card']) {
    assert.match(element(id).innerHTML, /中国 · 广东省 · 深圳市/);
  }
  assert.match(element('camera-status').textContent, /中国 · 广东省 · 深圳市/);
  assert.equal(renderer.camera.lon, 114.0579);
  assert.equal(renderer.camera.lat, 22.5431);
  const group = {...data.news[0].geo, newsIds: ['g1', 'g2']};
  app.pickGroup(group);
  assert.match(element('location-group').innerHTML, /广东省 · 深圳市/);
  assert.equal(JSON.stringify(data.news[0].geo), before);
});

test('mocked controller: province/city search filters the sidebar without changing active news', () => {
  const {app, element, data} = createHarness();
  data.news[0].geo = {lon: 114.0579, lat: 22.5431, country: '中国', province: '广东省', city: '深圳市'};
  app.selectNews('g3');
  element('search').handlers.input({target: {value: '广东省 · 深圳市'}});
  assert.equal(app.state.selected, 'g3');
  assert.equal(element('news-count').textContent, '1 / 4 条');
  assert.match(element('news-list').innerHTML, /data-news="g1"/);
  assert.doesNotMatch(element('news-list').innerHTML, /data-news="g[234]"/);
  element('search').handlers.input({target: {value: ''}});
  assert.equal(element('news-count').textContent, '4 条新闻');
  assert.match(element('news-list').innerHTML, /data-news="g4"/);
});

test('mocked controller: geographic metadata is escaped before insertion into markup', () => {
  const {app, element, data} = createHarness();
  data.news[0].geo = {...data.news[0].geo, province: '<省&>', city: '"市<script>'};
  app.selectNews('g1');
  for (const id of ['news-list', 'focus-card']) {
    const markup = element(id).innerHTML;
    assert.match(markup, /&lt;省&amp;&gt;/);
    assert.match(markup, /&quot;市&lt;script&gt;/);
    assert.doesNotMatch(markup, /<省&>|<script>/);
  }
});

test('mocked controller: return labels match actual stock-to-sector-to-news navigation', () => {
  const {app, element, document} = createHarness();
  app.selectNews('g1'); app.renderInner();
  assert.equal(element('back').textContent, '← 返回新闻页');
  assert.match(element('breadcrumbs').innerHTML, /data-crumb="earth">新闻页<\/button>/);
  assert.equal(app.state.level, 'sector');
  clickStockControl(element, 'scope', 'grid');
  clickStockControl(element, 'detail', 's1');
  assert.equal(element('back').textContent, '← 返回板块');
  element('back').handlers.click();
  assert.equal(app.state.level, 'sector');
  assert.equal(app.state.sector, null); assert.equal(app.state.stock, null);
  assert.equal(element('back').textContent, '← 返回新闻页');
  assert.equal(element('inner-space').hidden, false);
  element('back').handlers.click();
  assert.equal(app.state.level, 'earth');
  assert.equal(document.body.dataset.level, 'earth');
  assert.equal(element('inner-space').hidden, true);
  assert.equal(element('globe-stage').hidden, false);
  assert.equal(app.state.selected, 'g1');
});

test('mocked controller: news-page breadcrumb goes directly home from a selected sector', () => {
  const {app, element} = createHarness();
  app.selectNews('g1'); app.renderInner();
  clickStockControl(element, 'scope', 'grid');
  assert.match(element('breadcrumbs').innerHTML, /data-crumb="earth">新闻页<\/button>/);
  element('breadcrumbs').handlers.click({target: {closest(selector) {
    return selector === '[data-crumb]' ? {dataset: {crumb: 'earth'}} : null;
  }}});
  assert.equal(app.state.level, 'earth');
  assert.equal(app.state.sector, null);
  assert.equal(element('inner-space').hidden, true);
  assert.equal(element('globe-stage').hidden, false);
  assert.equal(app.state.selected, 'g1');
});

test('mocked controller: cancellation alone clears the locating status', () => {
  const {app, frames, element} = createHarness();
  app.selectNews('g1'); assert.equal(element('globe-stage').dataset.locating, 'true');
  app.stopFlight(); assert.equal(frames.size, 0);
  assert.equal(element('globe-stage').dataset.locating, 'false');
  assert.ok(!element('camera-status').textContent.includes('正在转向'));
});

test('mocked controller: blank pointer-down cancels flight without requiring a drag', () => {
  const {app, frames, element} = createHarness(); app.selectNews('g1');
  element('globe-stage').handlers.pointerdown({button: 0, pointerId: 1,
    clientX: 10, clientY: 20, target: {closest() { return null; }}});
  assert.equal(frames.size, 0); assert.equal(element('globe-stage').dataset.locating, 'false');
});

test('mocked controller: grouped markers retain all stories and clear an excluding search', () => {
  const {app, renderer, element} = createHarness();
  app.state.query = '不存在'; element('search').value = '不存在';
  app.pickGroup({label: '北京', newsIds: ['g1', 'g2']});
  assert.equal(app.state.query, ''); assert.equal(element('search').value, '');
  assert.equal(element('location-group').hidden, false);
  assert.match(element('location-group').innerHTML, /北京一/);
  assert.match(element('location-group').innerHTML, /北京二/);
  app.selectNews('g2'); assert.equal(renderer.selected, 'g2');
  assert.equal(element('location-group').hidden, true);
});

test('mocked controller: changing seats clears stock selection and never mixes seat eligibility', () => {
  const {app, element} = createHarness(); app.selectNews('g1');
  app.state.sector = 'grid'; app.state.stock = 's1';
  element('seat').handlers.change({target: {value: 'buffett'}});
  assert.equal(app.state.stock, null); assert.equal(app.state.seat, 'buffett');
  assert.deepEqual(renderedStockIds(element), ['s2']);
  element('eligible').handlers.change({target: {checked: false}});
  assert.deepEqual(renderedStockIds(element), ['s1', 's2']);
});

test('mocked controller: hidden page finishes pending target; pagehide destroys renderer', () => {
  const {app, renderer, frames, document, events, windowEvents} = createHarness();
  app.selectNews('g3'); document.hidden = true; events.visibilitychange();
  assert.equal(frames.size, 0); assert.equal(renderer.camera.lon, -77);
  windowEvents.pagehide(); assert.equal(renderer.destroyed, true);
});

test('mocked controller: orbit is adopted even with a legacy variant or old URL', () => {
  for (const options of [{}, {saved: {variant: 'atlas'}}, {search: '?view=horizon'},
    {saved: {variant: 'horizon'}, search: '?view=atlas'}, {search: '?compare=0&view=atlas'}]) {
    const {app, document, renderer, getReplacedURL} = createHarness(options);
    assert.equal(app.state.variant, 'orbit'); app.applyVariant();
    assert.equal(document.body.dataset.view, 'orbit');
    assert.equal(document.body.dataset.compare, 'false');
    assert.equal(renderer.variant, 'orbit');
    assert.equal(new URL(getReplacedURL()).searchParams.get('view'), 'orbit');
  }
});

test('mocked controller: explicit comparison mode still exposes the archived variants', () => {
  for (const variant of ['atlas', 'orbit', 'horizon']) {
    const {app, document} = createHarness({search: `?compare=1&view=${variant}`});
    assert.equal(app.state.variant, variant); app.applyVariant();
    assert.equal(document.body.dataset.compare, 'true');
    assert.equal(document.body.dataset.view, variant);
  }
  assert.equal(createHarness({search: '?compare=1&view=invalid'}).app.state.variant, 'orbit');
});

test('mocked controller: news-level stock list is a deduplicated union of all linked sectors', () => {
  const {app, element} = createHarness(); app.selectNews('g1'); app.renderInner();
  assert.equal(app.state.sector, null);
  assert.deepEqual(renderedStockIds(element), ['s1', 's3']);
  const markup = element('relation-detail').innerHTML;
  assert.match(markup, /data-stock-sector="all" aria-pressed="true"/);
  assert.match(markup, /data-stock-sector="grid"/);
  assert.match(markup, /data-stock-sector="storage"/);
  assert.doesNotMatch(markup, /data-stock-sector="compute"/);
  assert.doesNotMatch(markup, /无关标的/);
  assert.match(markup, /direction mixed/);
  assert.doesNotMatch(markup, /暂无证券代码/);
  assert.match(markup, /利弗莫尔席位/);
});

test('mocked controller: sector chips and all-sectors control update list and navigation together', () => {
  const {app, element} = createHarness(); app.selectNews('g1'); app.renderInner();
  clickStockControl(element, 'scope', 'grid');
  assert.equal(app.state.sector, 'grid'); assert.equal(app.state.level, 'stock');
  assert.deepEqual(renderedStockIds(element), ['s1']);
  assert.match(element('relation-detail').innerHTML, /data-stock-sector="grid" aria-pressed="true"/);
  assert.match(element('relation-detail').innerHTML, /data-stock-sector="storage"/);
  assert.match(element('breadcrumbs').innerHTML, /电网/);
  assert.equal(element('back').textContent, '← 返回板块');
  clickStockControl(element, 'scope', 'storage');
  assert.equal(app.state.sector, 'storage');
  assert.deepEqual(renderedStockIds(element), ['s1', 's3']);
  assert.match(element('breadcrumbs').innerHTML, /储能/);
  clickStockControl(element, 'scope', 'all');
  assert.equal(app.state.sector, null); assert.equal(app.state.level, 'sector');
  assert.deepEqual(renderedStockIds(element), ['s1', 's3']);
  assert.equal(element('back').textContent, '← 返回新闻页');
});

test('mocked controller: reason button expands and collapses selected-seat evidence only', () => {
  const {app, element} = createHarness(); app.selectNews('g1'); app.renderInner();
  clickStockControl(element, 'detail', 's1');
  assert.equal(app.state.stock, 's1');
  let markup = element('relation-detail').innerHTML;
  assert.match(markup, /data-stock-detail="s1" aria-expanded="true"/);
  assert.match(markup, /电网项目需求关联/); assert.match(markup, /储能成本约束关联/);
  assert.match(markup, /利弗莫尔趋势依据/); assert.match(markup, /gate规则/);
  assert.doesNotMatch(markup, /巴菲特安全边际不足/);
  clickStockControl(element, 'detail', 's1');
  assert.equal(app.state.stock, null); markup = element('relation-detail').innerHTML;
  assert.match(markup, /data-stock-detail="s1" aria-expanded="false"/);
  assert.doesNotMatch(markup, /class="stock-expanded"/);
});

test('mocked controller: expanded reasons show four separate evidence layers and escape source text', () => {
  const {app,element,data}=createHarness();
  data.news[0].source='<img src=x onerror=alert(1)>';
  data.news[0].summary='事实摘录 <script>bad()</script>';
  data.news_links[0].kind='mentioned'; data.news_links[1].kind='inferred';
  data.news_links[0].evidence_excerpt='摘录 <svg onload=bad()>';
  data.stock_links[0].reason='人工业务映射';
  data.stock_links[0].verification_needed='供货关系尚未核验';
  data.stocks[0].rule_results.livermore.decision_date='2026-10-23';
  data.stocks[0].rule_results.livermore.snapshot_version='abcd1234567890';
  data.stocks[0].rule_results.livermore.checks[0].detail='实际席位检查解释';
  app.selectNews('g1');app.renderInner();clickStockControl(element,'detail','s1');
  const markup=element('relation-detail').innerHTML;
  for(const text of ['01 · 新闻事实','02 · 板块关联','03 · 股票关联','04 · 席位判断',
    '文本直接涉及','产业传导假设','人工业务映射','供货关系尚未核验','实际席位检查解释','2026-10-23','abcd12345678'])assert.ok(markup.includes(text),text);
  assert.match(markup,/&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(markup,/<script>|<svg onload|<img src=x/);
  assert.equal(data.news[0].summary,'事实摘录 <script>bad()</script>');
});

test('mocked controller: embedded research posts only exact mapped identifiers to same origin', () => {
  const {app,element,data,hostMessages}=createHarness({embedded:true,search:'?embed=1'});
  data.stocks[0].rule_results.livermore.case_id='livermore-normal';
  app.selectNews('g1');app.renderInner();
  assert.match(element('relation-detail').innerHTML,/data-stock-research="s1"/);
  clickStockControl(element,'research','s1');
  assert.deepEqual(hostMessages,[{origin:'http://127.0.0.1:3008',message:{channel:'news-earth',type:'research',newsId:'g1',stockId:'s1',seatId:'livermore',sectorId:null}}]);
  clickStockControl(element,'scope','grid');clickStockControl(element,'research','s1');
  assert.equal(hostMessages[1].message.sectorId,'grid');
  for(const stockId of ['s2','s4','unknown','<script>'])clickStockControl(element,'research',stockId);
  assert.equal(hostMessages.length,2,'Unmapped, hidden or malformed targets cannot request research');
  delete data.stocks[0].rule_results.livermore.case_id;
  clickStockControl(element,'research','s1');assert.equal(hostMessages.length,2);
});

test('mocked controller: standalone research cannot pretend to open a workbench', () => {
  const {app,element,data,hostMessages}=createHarness();
  data.stocks[0].rule_results.livermore.case_id='livermore-normal';
  app.selectNews('g1');app.renderInner();
  assert.doesNotMatch(element('relation-detail').innerHTML,/data-stock-research=/);
  clickStockControl(element,'research','s1');assert.equal(hostMessages.length,0);
});

test('mocked controller: seat and eligibility changes clear expanded hidden stock', () => {
  const {app, element} = createHarness(); app.selectNews('g1'); app.renderInner();
  clickStockControl(element, 'detail', 's1');
  element('seat').handlers.change({target: {value: 'buffett'}});
  assert.equal(app.state.stock, null); assert.deepEqual(renderedStockIds(element), ['s2', 's3']);
  assert.match(element('relation-detail').innerHTML, /巴菲特席位/);
  assert.doesNotMatch(element('relation-detail').innerHTML, /利弗莫尔趋势依据/);
  element('eligible').handlers.change({target: {checked: false}});
  assert.deepEqual(renderedStockIds(element), ['s1', 's2', 's3']);
  clickStockControl(element, 'detail', 's1');
  assert.match(element('relation-detail').innerHTML, /巴菲特安全边际不足/);
  element('eligible').handlers.change({target: {checked: true}});
  assert.equal(app.state.stock, null); assert.deepEqual(renderedStockIds(element), ['s2', 's3']);
});

test('mocked controller: stale stock selection and new-news lists cannot retain unrelated rows', () => {
  const {app, element} = createHarness(); app.selectNews('g1');
  app.state.stock = 's4'; app.renderStockList(); assert.equal(app.state.stock, null);
  app.selectNews('g3'); app.renderInner();
  assert.deepEqual(renderedStockIds(element), ['s4']);
  assert.doesNotMatch(element('relation-detail').innerHTML, /data-stock-row="s1"/);
  app.selectNews('g4'); app.renderInner(); assert.deepEqual(renderedStockIds(element), []);
  assert.match(element('relation-detail').innerHTML, /当前范围暂无关联股票/);
});

test('mocked controller: clean stock list keeps no repeated simulation annotations', () => {
  const {app, element} = createHarness(); app.selectNews('g1'); app.renderInner();
  assert.doesNotMatch(element('relation-detail').innerHTML, /演示|模拟标的|模拟候选|可见模拟标的/);
  assert.doesNotMatch(element('relation-detail').innerHTML, /暂无证券代码/);
  assert.doesNotMatch(element('relation-detail').innerHTML, /<small>s[1-4]<\/small>/,
    'Internal sample IDs must not become security codes');
  clickStockControl(element, 'scope', 'grid');
  assert.doesNotMatch(element('relation-detail').innerHTML,
    /演示|模拟标的|模拟候选|可见模拟标的/);
});

test('static contract: helper captions are removed while navigation remains usable', () => {
  assert.doesNotMatch(html, /点击新闻即定位|拖动地球自由浏览|地球旋转不改变列表|点击一条新闻，直接转向其地图位置/);
  assert.doesNotMatch(html, /拖动旋转 · 滚轮左右转动|地理入口 \/|全球新闻 · 地理关联空间/);
  assert.doesNotMatch(html, /class="mode-line"|class="list-caption"|class="list-foot"/);
  assert.ok(!htmlIds.includes('view-description'));
  assert.ok(!htmlIds.includes('scene-eyebrow'));
  const status = html.match(/<p\b[^>]*id="camera-status"[^>]*>/)?.[0];
  assert.ok(status, 'Camera status remains available for assistive technology and state checks');
  assert.match(status, /class="[^"]*\bsr-only\b/);
  for (const id of ['locate', 'back', 'seat', 'eligible', 'search']) assert.ok(htmlIds.includes(id));
  assert.match(html, /data-turn="left"/);
  assert.match(html, /data-turn="right"/);
  assert.match(html, /数据状态：模拟/);
});

test('static contract: globe heading and inner relation diagram are removed, not hidden', () => {
  for (const id of ['scene-title', 'relation-field', 'relation-lines', 'parent-orb', 'child-orbs']) {
    assert.ok(!htmlIds.includes(id), `Removed #${id} must not remain in the DOM`);
    assert.ok(!appSource.includes(`$('${id}')`), `Controller must not access removed #${id}`);
  }
  assert.doesNotMatch(html + appSource, /全球事件，进入视野/);
  assert.doesNotMatch(appSource, /SphereCore|\bderiveScene\b|\[data-node\]|\bnew ResizeObserver\s*\(/);
  assert.match(rendererSource, /\bnew ResizeObserver\s*\(/,
    'The geographic globe retains its own responsive renderer');
  for (const id of ['relation-detail', 'back', 'breadcrumbs', 'seat', 'eligible']) {
    assert.ok(htmlIds.includes(id), `Retained list control #${id} must exist`);
  }
});

test('mocked controller: inner view needs no orb layout dependency and keeps list details usable', () => {
  const {app, element} = createHarness(); app.selectNews('g1'); app.renderInner();
  assert.equal(element('globe-stage').hidden, true);
  assert.equal(element('inner-space').hidden, false);
  assert.deepEqual(renderedStockIds(element), ['s1', 's3']);
  clickStockControl(element, 'scope', 'grid');
  assert.equal(app.state.level, 'stock');
  assert.deepEqual(renderedStockIds(element), ['s1']);
  clickStockControl(element, 'detail', 's1');
  assert.match(element('relation-detail').innerHTML, /利弗莫尔趋势依据/);
  app.back();
  assert.equal(app.state.sector, null);
  assert.deepEqual(renderedStockIds(element), ['s1', 's3']);
  app.back();
  assert.equal(element('globe-stage').hidden, false);
  assert.equal(element('inner-space').hidden, true);
});

test('mocked controller: compact stock rows omit filler but retain supplied codes and results', () => {
  const {app, element, data} = createHarness();
  data.stocks[0].code = 'TEST-CODE-01';
  app.selectNews('g1'); app.renderInner();
  const markup = element('relation-detail').innerHTML;
  assert.doesNotMatch(markup, /暂无证券代码|符合席位条件|暂不入选/);
  assert.match(markup, /TEST-CODE-01/);
  assert.match(markup, /门槛通过/);
  assert.match(markup, /direction mixed/);
  assert.match(markup, /data-stock-detail="s1"/);
  assert.doesNotMatch(markup.match(/<div class="stock-list-heading">([\s\S]*?)<\/div>/)?.[1] || '', /<p\b/);
  assert.match(markup, /stock-list-foot[^"\n]*\bsr-only\b|sr-only[^"\n]*\bstock-list-foot\b/,
    'Optional filtering notes should not add visible helper text');
  assert.deepEqual(renderedStockIds(element), ['s1', 's3']);
});

test('mocked controller: summary presentation is trimmed without rewriting underlying evidence', () => {
  const {app, element, data} = createHarness();
  const summary = '假想新闻：虚构规划提出设备更新。';
  const reason = '人工演示推测关联；未核验产业传导。';
  const rationale = '仅用于模拟交易；下一演示交易日检查条件。';
  data.news[0].summary = summary;
  data.news[0].source = '本地虚构情景 · 非真实新闻';
  data.news[0].synthetic = true;
  data.news_links[0].reason = reason;
  data.stocks[0].rule_results.livermore.rationale = rationale;
  app.selectNews('g1');
  assert.match(element('focus-card').innerHTML, /规划提出设备更新。/);
  assert.doesNotMatch(element('focus-card').innerHTML, /假想新闻|虚构规划|>演示<|位置为演示锚点/);
  assert.equal(data.news[0].summary, summary);
  assert.equal(data.news[0].source, '本地虚构情景 · 非真实新闻');
  assert.equal(data.news[0].synthetic, true);
  app.renderInner(); clickStockControl(element, 'detail', 's1');
  assert.ok(element('relation-detail').innerHTML.includes(reason));
  assert.ok(element('relation-detail').innerHTML.includes(rationale));
  assert.equal(data.news_links[0].reason, reason);
  assert.equal(data.stocks[0].rule_results.livermore.rationale, rationale);
});

function liveNews(id='real-1',changes={}) {
  return {id,title:'中国电网投资消息',summary:'来源短摘要',published_at:'2026-11-16T06:00:00Z',source:'官方来源',language:'zh',
    url:'https://news.example.org/story',location_basis:'标题明确提及中国',category:'全球新闻',
    geo:{lon:103,lat:35,country:'中国',country_id:'156'},...changes};
}
function livePayload(news=[liveNews()],changes={}) {
  return {news_mode:'live',preview_only:false,model_called:false,impact_mode:'keyword_hypothesis',geo_mode:'reported_mentions',
    news,sectors:[],stocks:[],news_links:[],stock_links:[],seats:[{id:'baseline',label:'基础策略席位'}],
    meta:{status:'live',checked_at:'2026-11-16T06:01:00Z',updated_at:'2026-11-16T06:00:00Z',refresh_interval_seconds:300,
      total:news.length,limit:40,offset:0,has_more:false,sources:[{id:'fixture',label:'官方来源',status:'ok',last_success:'2026-11-16T06:01:00Z',error:null}]},...changes};
}
const flushLive=()=>new Promise(resolve=>setImmediate(resolve));

test('live controller: empty real-news cache leaves the globe usable without synthetic fallback',()=>{
  const h=createHarness({search:'?mode=live'});
  h.app.adoptLive(livePayload([]));
  assert.equal(h.app.state.selected,null);assert.deepEqual(h.renderer.news,[]);
  assert.match(h.element('focus-card').innerHTML,/等待真实新闻/);
  assert.match(h.element('news-list').innerHTML,/不会用模拟新闻补齐/);
  assert.equal(h.element('locate').disabled,true);
  assert.equal(h.element('live-controls').hidden,false);
  assert.equal(h.element('news-pagination').hidden,false);
  assert.equal(h.requests.length,0);h.windowEvents.pagehide();
});

test('live controller: requests bounded server-side search and a single periodic timer',async()=>{
  const h=createHarness({search:'?mode=live'});
  h.app.state.query='电网 & oil';h.app.state.offset=40;
  h.reply(livePayload());await h.app.refreshLive();
  const url=new URL(h.requests[0].url,'http://127.0.0.1:3001');
  assert.equal(url.pathname,'/contest-api/news/feed');assert.equal(url.searchParams.get('q'),'电网 & oil');
  assert.equal(url.searchParams.get('limit'),'40');assert.equal(url.searchParams.get('offset'),'40');
  assert.equal(h.requests[0].options.credentials,'same-origin');assert.equal(h.requests[0].options.redirect,'error');
  assert.equal(h.timers.size,1);assert.equal([...h.timers.values()][0].ms,300000);
  assert.match(h.element('news-list').innerHTML,/中国电网投资消息/,'Server matches are not filtered away again in the browser');
  h.windowEvents.pagehide();assert.equal(h.timers.size,0);
});

test('live controller: failed refresh keeps last real articles and exposes the failure',async()=>{
  const h=createHarness({search:'?mode=live'});h.app.adoptLive(livePayload());
  h.reply({},503);await h.app.refreshLive();
  assert.equal(h.renderer.news[0].id,'real-1');assert.match(h.element('live-status').textContent,/读取失败/);
  assert.equal(h.element('live-controls').dataset.state,'warning');
  assert.ok(h.requests.every(request=>!request.url.includes('news-data.json')));
  h.windowEvents.pagehide();
});

test('live controller: source errors and cache times are explicit and escaped',()=>{
  const h=createHarness({search:'?mode=live'}),payload=livePayload();
  payload.meta.status='stale';payload.meta.sources[0].label='<img onerror=bad()>';
  payload.meta.sources[0].status='error';
  payload.meta.sources[0].error='<script>blocked</script>';
  h.app.adoptLive(payload);
  assert.match(h.element('live-status').textContent,/历史缓存 · 更新受阻/);
  assert.match(h.element('live-status').textContent,/北京时间/);
  assert.match(h.element('source-status').innerHTML,/&lt;script&gt;blocked/);
  assert.doesNotMatch(h.element('source-status').innerHTML,/<script>|<img/);
  assert.equal(h.element('live-controls').dataset.state,'warning');
  assert.match(h.element('source-status').innerHTML,/失败/);assert.match(h.element('live-status').textContent,/1个来源失败/);
});

test('live controller: superseded responses cannot overwrite a later query or add timers',async()=>{
  const h=createHarness({search:'?mode=live'});let release;
  h.replies.push(new Promise(resolve=>release=resolve));
  const first=h.app.refreshLive();h.app.state.query='later';
  h.reply(livePayload([liveNews('later')]));await h.app.refreshLive();
  release({data:livePayload([liveNews('older')])});await first;
  assert.equal(h.requests[0].options.signal.aborted,true);assert.equal(h.renderer.news[0].id,'later');
  assert.equal(h.timers.size,1);h.windowEvents.pagehide();
});

test('live controller: background pauses refresh, foreground reloads and pagehide aborts pending work',async()=>{
  const h=createHarness({search:'?mode=live'});h.reply(livePayload());await h.app.refreshLive();
  h.document.hidden=true;h.events.visibilitychange();assert.equal(h.timers.size,0);
  await h.app.refreshLive();assert.equal(h.requests.length,1);
  h.reply(livePayload());h.document.hidden=false;h.events.visibilitychange();await flushLive();
  assert.equal(h.requests.length,2);assert.equal(h.timers.size,1);
  let release;h.replies.push(new Promise(resolve=>release=resolve));const pending=h.app.refreshLive();
  h.windowEvents.pagehide();assert.equal(h.requests[2].options.signal.aborted,true);assert.equal(h.timers.size,0);
  release({data:livePayload([liveNews('too-late')])});await pending;
  assert.equal(h.renderer.destroyed,true);assert.notEqual(h.renderer.news[0].id,'too-late');
});

test('live controller: source links are https-only and published facts are not rewritten',()=>{
  const h=createHarness({search:'?mode=live'});
  h.app.adoptLive(livePayload([liveNews('x',{url:'javascript:alert(1)',summary:'虚构内容受到调查',source:'<script>source</script>'})]));
  assert.doesNotMatch(h.element('focus-card').innerHTML,/javascript:|<script>/);
  assert.match(h.element('focus-card').innerHTML,/虚构内容受到调查/,'Do not trim real-source wording as if it were a fixture prefix');
  h.app.adoptLive(livePayload());
  assert.match(h.element('focus-card').innerHTML,/target="_blank" rel="noopener noreferrer"/);
  assert.match(h.element('focus-card').innerHTML,/https:\/\/news.example.org\/story/);
  assert.match(h.element('focus-card').innerHTML,/发布 .*北京时间/);
});

test('live controller: unknown seat results remain unverified and cannot open synthetic research',()=>{
  const h=createHarness({search:'?mode=live&embed=1',embedded:true}),payload=livePayload();
  payload.sectors=h.data.sectors;payload.stocks=h.data.stocks;payload.stock_links=h.data.stock_links;
  payload.news_links=h.data.news_links.map(link=>({...link,news_id:'real-1'}));
  payload.stocks[0].rule_results.baseline={eligible:true,case_id:'normal-fill'};
  h.app.adoptLive(payload);h.app.renderInner();
  assert.equal(h.app.state.eligible,false);assert.match(h.element('relation-detail').innerHTML,/关键词产业假设 · 未验证/);
  assert.match(h.element('relation-detail').innerHTML,/待核验/);
  assert.match(h.element('relation-detail').innerHTML,/disabled title="当前新闻时点缺少已核验/);
  clickStockControl(h.element,'research','s1');assert.equal(h.hostMessages.length,0);
});

test('live controller: official business sources are clickable, dated and https-only',()=>{
  const h=createHarness({search:'?mode=live&embed=1',embedded:true}),payload=livePayload();
  payload.sectors=h.data.sectors;payload.stocks=h.data.stocks;payload.stock_links=h.data.stock_links;
  payload.news_links=h.data.news_links.map(link=>({...link,news_id:'real-1'}));
  payload.stock_links[0].source_url='https://official.example.org/business';
  payload.stock_links[0].source_title='公司官方业务资料';payload.stock_links[0].source_checked_at='2026-11-16';
  payload.stock_links[1].source_url='javascript:bad()';
  h.app.adoptLive(payload);h.app.renderInner();clickStockControl(h.element,'detail','s1');
  assert.match(h.element('relation-detail').innerHTML,/https:\/\/official.example.org\/business/);
  assert.match(h.element('relation-detail').innerHTML,/公司官方业务资料/);assert.match(h.element('relation-detail').innerHTML,/资料核对日期 2026-11-16/);
  assert.doesNotMatch(h.element('relation-detail').innerHTML,/javascript:/);
});

test('live controller: synthetic payloads or oversized pages are refused, never substituted',()=>{
  const h=createHarness({search:'?mode=live'});h.app.adoptLive(livePayload());
  assert.throws(()=>h.app.adoptLive({...livePayload(),news_mode:'synthetic'}),/未改用模拟新闻/);
  assert.throws(()=>h.app.adoptLive(livePayload(Array.from({length:41},(_,i)=>liveNews(`real-${i}`)))),/分页格式不正确/);
  assert.equal(h.renderer.news[0].id,'real-1');
});

test('live controller: paging and debounced searches use the cache, not a full-market client array',async()=>{
  const h=createHarness({search:'?mode=live'}),payload=livePayload();payload.meta.has_more=true;payload.meta.total=90;
  h.app.adoptLive(payload);h.reply(payload);h.element('next-news').handlers.click();await flushLive();
  assert.equal(h.app.state.offset,40);assert.match(h.requests[0].url,/offset=40/);
  h.reply(payload);h.element('previous-news').handlers.click();await flushLive();assert.equal(h.app.state.offset,0);
  h.element('search').handlers.input({target:{value:'first'}});
  h.element('search').handlers.input({target:{value:'second'}});
  assert.equal([...h.timers.values()].filter(timer=>timer.ms===250).length,1);
  h.reply(livePayload());await h.timer(250);assert.match(h.requests.at(-1).url,/q=second/);
  h.windowEvents.pagehide();assert.equal(h.timers.size,0);
});

test('live controller: refresh tracking has a bounded single timer then returns to five-minute checks',async()=>{
  const h=createHarness({search:'?mode=live'}),payload=livePayload();payload.meta.refreshing=true;
  h.reply({accepted:true});h.reply(payload);await h.app.refreshLive({refresh:true});
  assert.equal(h.requests[0].options.method,'POST');assert.equal(h.requests[0].url,'/contest-api/news/refresh');
  for(let i=0;i<6;i++){h.reply(payload);await h.timer(5000);assert.equal(h.timers.size,1);}
  assert.equal([...h.timers.values()][0].ms,300000);h.windowEvents.pagehide();
});

test('live controller: initial real mode loads a bounded page and world map without the fixture JSON',async()=>{
  const h=createHarness({search:'?mode=live&embed=1',embedded:true});h.reply(livePayload([]));h.reply({type:'Topology'});
  await h.app.init();
  assert.equal(h.requests.length,2);assert.match(h.requests[0].url,/\/contest-api\/news\/feed\?limit=40/);
  assert.equal(h.requests[1].url,'vendor/countries-110m.json');assert.equal(h.element('globe-stage').dataset.ready,'true');
  assert.ok(h.hostMessages.some(item=>item.message.type==='ready'));assert.equal(h.renderer.news.length,0);
  assert.equal(h.timers.size,1);h.windowEvents.pagehide();
});

test('live controller: removing the iframe during initial load cancels requests and never creates a renderer',async()=>{
  const h=createHarness({search:'?mode=live&embed=1',embedded:true});let release;
  h.replies.push(new Promise(resolve=>release=resolve));h.reply({type:'Topology'});
  const pending=h.app.init();h.windowEvents.pagehide();assert.ok(h.requests.every(request=>request.options.signal.aborted));
  release({data:livePayload()});await pending;
  assert.notEqual(h.element('globe-stage').dataset.ready,'true');assert.equal(h.timers.size,0);
});

function aiPayload(news=[liveNews()],changes={}) {
  return livePayload(news,{sectors:[{id:'grid',label:'电网设备'},{id:'bank',label:'银行'}],
    ai_status:{available:true,model:'test-model'},ai_analyses:{},
    news_links:news.map(item=>({news_id:item.id,sector_id:'grid',direction:'unknown',impact_level:'unknown',kind:'inferred',reason:'关键词原关联'})),...changes});
}
function aiResult(item=liveNews(),changes={}) {
  return {method:'llm',model:'test-model',analyzed_at:'2026-11-16T07:00:00Z',source_hash:'a'.repeat(64),
    evidence:Object.fromEntries(['title','summary','published_at','source','url'].map(key=>[key,item[key]])),
    summary:'仅基于标题和短摘要的产业观察。',limitations:['未核验新闻全文与实际业务敞口。'],
    relations:[{sector_id:'grid',label:'电网设备',direction:'opportunity',impact_level:'high',relation_type:'indirect',
      reason:'电网投资或形成相关设备需求，仍需核实采购安排。',evidence_excerpt:'电网投资',uncertainty:'实际采购时间与受益主体尚未确认。'}],...changes};
}
function clickAi(h,id='real-1',inner=false) {
  h.element(inner?'relation-detail':'focus-card').handlers.click({target:{closest(selector){
    return selector==='[data-ai-analyze]'?{dataset:{aiAnalyze:id}}:null;
  }}});
}

test('news AI: reading, selecting and polling never submit paid model requests',async()=>{
  const h=createHarness({search:'?mode=live'}),payload=aiPayload([liveNews(),liveNews('real-2')]);
  h.app.adoptLive(payload);h.app.selectNews('real-2');h.app.renderInner();
  assert.equal(h.requests.length,0);assert.match(h.element('focus-card').innerHTML,/AI分析关联板块/);
  assert.match(h.element('focus-card').innerHTML,/可能计费/);assert.match(h.element('focus-card').innerHTML,/不自动批量调用/);
  h.reply(payload);await h.app.refreshLive();assert.equal(h.requests.length,1);assert.ok(h.requests[0].url.startsWith('/contest-api/news/feed?'));
  assert.match(h.element('focus-card').innerHTML,/data-ai-analyze="real-2">AI分析关联板块/,'A completed cache refresh must re-enable the paid action without submitting it');
  assert.ok(h.requests.every(request=>request.options.method!=='POST'));h.windowEvents.pagehide();
});

test('news AI: an explicit click submits only the current ID and adopts validated relations',async()=>{
  const h=createHarness({search:'?mode=live'}),item=liveNews();h.app.adoptLive(aiPayload());
  h.reply({news_id:item.id,analysis:aiResult(item),cached:false});clickAi(h);await flushLive();
  assert.equal(h.requests.length,1);const request=h.requests[0];assert.equal(request.url,'/contest-api/news/analyze');
  assert.equal(request.options.method,'POST');assert.equal(request.options.credentials,'same-origin');
  assert.equal(request.options.redirect,'error');assert.deepEqual(JSON.parse(request.options.body),{news_id:item.id});
  assert.equal(h.app.analysisFor().method,'llm');assert.equal(h.app.state.selected,item.id);
  assert.match(h.element('focus-card').innerHTML,/test-model/);assert.match(h.element('focus-card').innerHTML,/已保存缓存/);
  assert.match(h.element('focus-card').innerHTML,/潜在利好/);assert.match(h.element('focus-card').innerHTML,/新闻依据：电网投资/);
  h.app.renderInner();assert.match(h.element('relation-detail').innerHTML,/AI产业关联假设/);
  assert.match(h.element('relation-detail').innerHTML,/实际采购时间与受益主体尚未确认/);
  assert.equal(h.timers.size,1);h.windowEvents.pagehide();
});

test('news AI: cached analysis is readable without repeating the POST',async()=>{
  const h=createHarness({search:'?mode=live'}),payload=aiPayload();payload.ai_analyses['real-1']=aiResult();
  h.app.adoptLive(payload);assert.match(h.element('focus-card').innerHTML,/AI分析已保存/);
  clickAi(h);await flushLive();await h.app.analyzeNews();assert.equal(h.requests.length,0);
  assert.equal(h.app.analysisFor().relations[0].sector_id,'grid');
  h.windowEvents.pagehide();
});

test('news AI: duplicate clicks share one pending UI request',async()=>{
  const h=createHarness({search:'?mode=live'});h.app.adoptLive(aiPayload());let release;
  h.replies.push(new Promise(resolve=>release=resolve));const first=h.app.analyzeNews();
  await h.app.analyzeNews();clickAi(h);assert.equal(h.requests.length,1);
  assert.match(h.element('focus-card').innerHTML,/AI正在分析/);
  release({data:{news_id:'real-1',analysis:aiResult(),cached:false}});await first;
  assert.equal(h.requests.length,1);h.windowEvents.pagehide();
});

test('news AI: unavailable models disable analysis without blocking real news',async()=>{
  const h=createHarness({search:'?mode=live'});h.app.adoptLive(aiPayload([liveNews()],{ai_status:{available:false,model:null}}));
  assert.match(h.element('focus-card').innerHTML,/尚未连接模型/);
  assert.match(h.element('focus-card').innerHTML,/data-ai-analyze="real-1" disabled/);
  clickAi(h);await flushLive();await h.app.analyzeNews();assert.equal(h.requests.length,0);
  assert.match(h.element('news-list').innerHTML,/中国电网投资消息/);h.windowEvents.pagehide();
});

test('news AI: provider errors remain explicit and preserve keyword relations',async()=>{
  const h=createHarness({search:'?mode=live'});h.app.adoptLive(aiPayload());
  h.reply({detail:'模型服务返回 HTTP 400；未自动重试。'},502);await h.app.analyzeNews();
  assert.match(h.element('focus-card').innerHTML,/AI分析失败：模型服务返回 HTTP 400/);
  assert.match(h.element('focus-card').innerHTML,/不能当作AI分析结果/);
  assert.equal(h.app.analysisFor(),null);assert.equal(h.requests.length,1);
  h.app.renderInner();assert.match(h.element('relation-detail').innerHTML,/关键词产业假设 · 未验证/);
  assert.match(h.element('relation-detail').innerHTML,/电网设备/);h.windowEvents.pagehide();
});

test('news AI: unavailable cache is not mislabeled as missing API settings',async()=>{
  const h=createHarness({search:'?mode=live'});
  h.app.adoptLive(aiPayload([liveNews()],{ai_status:{available:false,model:'test-model',error:'AI分析缓存暂不可用，未发起模型请求。'}}));
  assert.match(h.element('focus-card').innerHTML,/AI分析缓存暂不可用/);
  assert.doesNotMatch(h.element('focus-card').innerHTML,/尚未连接模型/);
  await h.app.analyzeNews();assert.equal(h.requests.length,0);h.windowEvents.pagehide();
});

test('news AI: mismatched article, invented citations and unknown sectors are rejected',async()=>{
  for(const change of [
    {responseId:'other-news'},
    {evidence:{...aiResult().evidence,title:'另一条新闻'}},
    {relations:[{...aiResult().relations[0],evidence_excerpt:'原文没有的订单金额'}]},
    {relations:[{...aiResult().relations[0],sector_id:'unregistered'}]},
    {relations:[{...aiResult().relations[0],evidence_excerpt:''}]},
    {relations:Array.from({length:9},()=>aiResult().relations[0])},
  ]){
    const h=createHarness({search:'?mode=live'});h.app.adoptLive(aiPayload());
    const {responseId,...changes}=change;
    h.reply({news_id:responseId || 'real-1',analysis:aiResult(liveNews(),changes),cached:false});await h.app.analyzeNews();
    assert.equal(h.app.analysisFor(),null);assert.match(h.element('focus-card').innerHTML,/AI分析失败/);
    assert.doesNotMatch(h.element('focus-card').innerHTML,/AI分析已保存/);h.windowEvents.pagehide();
  }
});

test('news AI: AI text is escaped and no quote or summary becomes executable HTML',async()=>{
  const h=createHarness({search:'?mode=live'}),item=liveNews('real-1',{summary:'来源提示：<img onerror=bad()>电网投资'});
  h.app.adoptLive(aiPayload([item]));const analysis=aiResult(item,{summary:'<script>run()</script>是模型文字'});
  analysis.relations[0].reason='<img src=x onerror=bad()>仅为模型推测';
  analysis.relations[0].uncertainty='<iframe src=x>仍需核验';
  analysis.limitations=['<svg onload=bad()>不得执行'];
  h.reply({news_id:item.id,analysis,cached:false});await h.app.analyzeNews();h.app.renderInner();
  for(const id of ['focus-card','relation-detail']){
    const markup=h.element(id).innerHTML;assert.doesNotMatch(markup,/<script>|<img |<iframe |<svg /);
    assert.match(markup,/&lt;script&gt;/);assert.match(markup,/&lt;img/);
  }
  h.windowEvents.pagehide();
});

test('news AI: switching news aborts UI wait and a late response cannot relabel another article',async()=>{
  const h=createHarness({search:'?mode=live'});h.app.adoptLive(aiPayload([liveNews(),liveNews('real-2')]));let release;
  h.replies.push(new Promise(resolve=>release=resolve));const pending=h.app.analyzeNews();
  h.app.selectNews('real-2');assert.equal(h.requests[0].options.signal.aborted,true);
  release({data:{news_id:'real-1',analysis:aiResult(),cached:false}});await pending;
  assert.equal(h.app.state.selected,'real-2');assert.equal(h.app.analysisFor(),null);
  assert.doesNotMatch(h.element('focus-card').innerHTML,/AI分析已保存/);assert.equal(h.timers.size,1);h.windowEvents.pagehide();
});

test('news AI: hiding or unloading the frame cancels waiting without automatic reanalysis',async()=>{
  for(const event of ['hidden','pagehide']){
    const h=createHarness({search:'?mode=live'});h.app.adoptLive(aiPayload());let release;
    h.replies.push(new Promise(resolve=>release=resolve));const pending=h.app.analyzeNews();
    if(event==='hidden'){h.document.hidden=true;h.events.visibilitychange();}else h.windowEvents.pagehide();
    assert.equal(h.requests[0].options.signal.aborted,true);assert.equal(h.timers.size,0);
    release({data:{news_id:'real-1',analysis:aiResult(),cached:false}});await pending;
    assert.equal(h.app.analysisFor(),null);assert.equal(h.requests.length,1);
    if(event==='hidden'){
      h.reply(aiPayload());h.document.hidden=false;h.events.visibilitychange();await flushLive();
      assert.equal(h.requests.length,2);assert.ok(h.requests[1].url.startsWith('/contest-api/news/feed?'));
      h.windowEvents.pagehide();
    }
  }
});

test('news AI: empty valid relations keep an honest no-association conclusion',async()=>{
  const h=createHarness({search:'?mode=live'});h.app.adoptLive(aiPayload());
  h.reply({news_id:'real-1',analysis:aiResult(liveNews(),{relations:[]}),cached:false});await h.app.analyzeNews();h.app.renderInner();
  assert.match(h.element('relation-detail').innerHTML,/本次AI未找到有足够依据的关联板块/);
  assert.doesNotMatch(h.element('relation-detail').innerHTML,/data-stock-sector="grid"/);
  assert.match(h.element('focus-card').innerHTML,/进入全部 0 个关联板块/);h.windowEvents.pagehide();
});

test('news AI: preview mode never exposes or executes model analysis',async()=>{
  const h=createHarness();h.app.selectNews('g1');h.app.renderInner();
  assert.doesNotMatch(h.element('focus-card').innerHTML,/data-ai-analyze|AI板块关联/);
  assert.doesNotMatch(h.element('relation-detail').innerHTML,/data-ai-analyze/);
  await h.app.analyzeNews();assert.equal(h.requests.length,0);h.windowEvents.pagehide();
});
