'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const Core = require('./geo-core.js');
const approx = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-8,
  `${actual} differs from ${expected}`);
const article = (id, lon, lat, extra = {}) => ({ id, title: id,
  geo: { lon, lat, country: '中国', country_id: '156', label: '北京', ...extra } });

test('longitude normalization handles antimeridian and repeated turns', () => {
  assert.equal(Core.normalizeLon(180), -180);
  assert.equal(Core.normalizeLon(-180), -180);
  assert.equal(Core.normalizeLon(540), -180);
  assert.equal(Core.normalizeLon(-540), -180);
  assert.equal(Core.normalizeLon(721), 1);
  assert.equal(Core.normalizeLon(-721), -1);
  assert.equal(Core.normalizeLon(-0), 0);
});

test('invalid numeric inputs never propagate NaN', () => {
  for (const value of [NaN, Infinity, -Infinity, undefined, null, '12']) {
    assert.equal(Core.normalizeLon(value), 0);
    assert.equal(Core.clampLat(value), 0);
  }
  assert.equal(Core.clampLat(110), 90);
  assert.equal(Core.clampLat(-110), -90);
});

test('camera target validates numeric geographic bounds without fake coordinates', () => {
  for (const geo of [null, {}, {lon: 30}, {lon: '30', lat: 20},
    {lon: 30, lat: NaN}, {lon: 181, lat: 20}, {lon: 0, lat: 91}]) {
    assert.equal(Core.cameraForLocation(geo), null);
  }
  assert.deepEqual(Core.cameraForLocation({lon: 180, lat: 90}), {lon: -180, lat: 90});
  assert.deepEqual(Core.cameraForLocation({lon: 116.4074, lat: 39.9042}),
    {lon: 116.4074, lat: 39.9042});
});

test('camera interpolation uses the short route across the antimeridian', () => {
  const start = {lon: 170, lat: 10};
  const end = {lon: -170, lat: 20};
  assert.deepEqual(Core.interpolateCamera(start, end, .5), {lon: -180, lat: 15});
  assert.deepEqual(Core.interpolateCamera(end, start, .25), {lon: -175, lat: 17.5});
});

test('interpolation does not add implicit easing and preserves normalized endpoints', () => {
  const start = {lon: 20, lat: -20};
  const end = {lon: 100, lat: 60};
  assert.deepEqual(Core.interpolateCamera(start, end, .25), {lon: 40, lat: 0});
  assert.deepEqual(Core.interpolateCamera(start, end, -1), start);
  assert.deepEqual(Core.interpolateCamera(start, end, 2), end);
  assert.deepEqual(Core.interpolateCamera(start, end, 0), start);
  assert.deepEqual(Core.interpolateCamera(start, end, 1), end);
});

test('exactly opposite longitudes use deterministic half-turn and poles stay finite', () => {
  assert.deepEqual(Core.interpolateCamera({lon: 0, lat: -90}, {lon: 180, lat: 90}, .5),
    {lon: -90, lat: 0});
  assert.deepEqual(Core.interpolateCamera({lon: 180, lat: 90}, {lon: 0, lat: -90}, .5),
    {lon: 90, lat: 0});
  for (const t of [0, .1, .5, .9, 1, NaN]) {
    const camera = Core.interpolateCamera(null, {}, t);
    assert.ok(Number.isFinite(camera.lon) && Number.isFinite(camera.lat));
  }
});

test('a location is visible on the front face, not the reverse or horizon', () => {
  const camera = {lon: 0, lat: 0};
  assert.equal(Core.visibleLocation({lon: 0, lat: 0}, camera), true);
  assert.equal(Core.visibleLocation({lon: 89, lat: 0}, camera), true);
  assert.equal(Core.visibleLocation({lon: 90, lat: 0}, camera), false);
  assert.equal(Core.visibleLocation({lon: -90, lat: 0}, camera), false);
  assert.equal(Core.visibleLocation({lon: 180, lat: 0}, camera), false);
  assert.equal(Core.visibleLocation(null, camera), false);
  assert.equal(Core.visibleLocation({lon: 0, lat: 0}, null), false);
});

test('visibility handles the antimeridian and pole cameras', () => {
  assert.equal(Core.visibleLocation({lon: -179, lat: 0}, {lon: 179, lat: 0}), true);
  assert.equal(Core.visibleLocation({lon: 30, lat: 89}, {lon: -100, lat: 90}), true);
  assert.equal(Core.visibleLocation({lon: 30, lat: -89}, {lon: 100, lat: 90}), false);
});

test('same-place articles aggregate without dropping siblings', () => {
  const stories = [article('n1', 116.4, 39.9), article('n2', 116.4, 39.9),
    article('n3', 121.47, 31.23, {label: '上海'})];
  const groups = Core.groupNews(stories);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0].newsIds, ['n1', 'n2']);
  assert.deepEqual(groups[1].newsIds, ['n3']);
  assert.equal(groups[0].label, '北京');
  assert.equal(groups[1].label, '上海');
});

test('location labels include province and city without duplicate municipality names', () => {
  const shenzhen = Object.freeze({country: '中国', province: '广东省', city: '深圳市', label: '深圳'});
  assert.equal(Core.locationLabel(shenzhen), '中国 · 广东省 · 深圳市');
  assert.equal(Core.locationLabel(shenzhen, {includeCountry: false}), '广东省 · 深圳市');
  assert.equal(Core.locationLabel({country: ' 中国 ', province: ' 北京市 ', city: '北京市', label: '北京'}),
    '中国 · 北京市');
  assert.equal(Core.locationLabel({country: '中国', province: '上海市', city: '上海市'}, {includeCountry: false}),
    '上海市');
  assert.equal(shenzhen.label, '深圳');
});

test('location labels keep legacy and partial records without inventing administrative levels', () => {
  assert.equal(Core.locationLabel({country: '美国', label: '华盛顿'}), '美国 · 华盛顿');
  assert.equal(Core.locationLabel({country: '日本', city: '东京', province: null}), '日本 · 东京');
  assert.equal(Core.locationLabel({country: '中国', province: '广东省'}), '中国 · 广东省');
  assert.equal(Core.locationLabel({country: '中国'}), '中国');
  for (const geo of [null, undefined, '', {}, {country: ' ', province: 42, city: false}]) {
    assert.equal(Core.locationLabel(geo), '地点未指定');
  }
});

test('grouped markers carry province and city and prefer city over a legacy label', () => {
  const first = article('n1', 114.0579, 22.5431, {province: '广东省', city: '深圳市', label: '旧显示名'});
  const groups = Core.groupNews([first, {...first, id: 'n2'},
    article('n3', 113.2644, 23.1291, {province: '广东省', city: '广州市'})]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].label, '深圳市');
  assert.equal(groups[0].province, '广东省');
  assert.equal(groups[0].city, '深圳市');
  assert.equal(Core.locationLabel(groups[0]), '中国 · 广东省 · 深圳市');
  assert.deepEqual(groups[0].newsIds, ['n1', 'n2']);
  assert.deepEqual(groups[1].newsIds, ['n3']);
  assert.equal(groups[1].city, '广州市');
  const legacy = Core.groupNews([article('old', 116.4, 39.9)])[0];
  assert.equal(legacy.province, null);
  assert.equal(legacy.city, null);
  assert.equal(legacy.label, '北京');
});

test('near-identical floating point representations group but distinct places do not', () => {
  const groups = Core.groupNews([article('n1', 116.4000001, 39.9),
    article('n2', 116.4000002, 39.9), article('n3', 116.401, 39.9)]);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0].newsIds, ['n1', 'n2']);
  assert.notEqual(groups[0].id, groups[1].id);
});

test('country identity is retained even when demonstration coordinates coincide', () => {
  const groups = Core.groupNews([article('n1', 0, 0),
    article('n2', 0, 0, {country_id: '840', country: '美国'})]);
  assert.equal(groups.length, 2);
  assert.equal(groups[1].country, '美国');
});

test('unlocated and malformed news are omitted from globe grouping but not invented', () => {
  assert.deepEqual(Core.groupNews([{id: 'global', geo: null}, article('bad', 0, 100),
    {id: 'invalid'}, null, {geo: {lon: 0, lat: 0}}]), []);
  assert.deepEqual(Core.groupNews(null), []);
});

test('duplicate article IDs within a group are not double-counted', () => {
  const item = article('n1', 116, 39);
  const groups = Core.groupNews([item, item]);
  assert.deepEqual(groups[0].newsIds, ['n1']);
});

test('groups and queries do not mutate frozen source records or arrays', () => {
  const item = article('n1', 116, 39);
  Object.freeze(item.geo);
  Object.freeze(item);
  const source = Object.freeze([item]);
  assert.equal(Core.groupNews(source).length, 1);
  assert.deepEqual(Core.filterNews(source, ''), source);
  assert.notEqual(Core.filterNews(source, ''), source);
});

test('filter searches title, short title, summary, country and city case-insensitively', () => {
  const stories = [{...article('n1', 0, 0), title: '电网升级', summary: '高效设备'},
    {...article('n2', 0, 0, {country: '美国', label: 'New York'}), title: 'AI Infrastructure'},
    {id: 'n3', title: '其他', shortTitle: '全球能效', geo: null}];
  assert.deepEqual(Core.filterNews(stories, '电网').map(x => x.id), ['n1']);
  assert.deepEqual(Core.filterNews(stories, '高效').map(x => x.id), ['n1']);
  assert.deepEqual(Core.filterNews(stories, '  ai  ').map(x => x.id), ['n2']);
  assert.deepEqual(Core.filterNews(stories, '美国').map(x => x.id), ['n2']);
  assert.deepEqual(Core.filterNews(stories, 'new york').map(x => x.id), ['n2']);
  assert.deepEqual(Core.filterNews(stories, '全球').map(x => x.id), ['n3']);
});

test('province and city search also matches full locations with common separators', () => {
  const stories = [article('sz', 114.0579, 22.5431, {province: '广东省', city: '深圳市'}),
    article('hz', 120.1551, 30.2741, {province: '浙江省', city: '杭州市'}),
    {id: 'global', title: '全球议题', geo: null}];
  for (const query of ['广东', '深圳', '广东省 · 深圳市', '广东省 深圳市', '广东省/深圳市',
    '中国 · 广东省 · 深圳市', '广东省深圳市']) {
    assert.deepEqual(Core.filterNews(stories, query).map(item => item.id), ['sz'], query);
  }
  assert.deepEqual(Core.filterNews(stories, '浙江省 · 杭州市').map(item => item.id), ['hz']);
  assert.deepEqual(Core.filterNews(stories, '江苏省'), []);
  assert.deepEqual(Core.filterNews(stories, '广东省 · 杭州市'), []);
});

test('separator-only location queries do not match every located story', () => {
  const stories = [article('sz', 114.0579, 22.5431, {province: '广东省', city: '深圳市'}),
    {id: 'global', title: '全球议题', geo: null}];
  for (const query of ['·', '/', ' · / ']) assert.deepEqual(Core.filterNews(stories, query), []);
  assert.deepEqual(Core.filterNews(stories, '地点未指定'), []);
});

test('province/city formatting, grouping and search leave source metadata intact', () => {
  const geo = Object.freeze({lon: 114.0579, lat: 22.5431, country: '中国', country_id: '156',
    province: '广东省', city: '深圳市', label: '深圳'});
  const source = Object.freeze([Object.freeze({id: 'sz', title: '设备采购', geo, synthetic: true})]);
  const before = JSON.stringify(source);
  assert.equal(Core.locationLabel(geo), '中国 · 广东省 · 深圳市');
  assert.equal(Core.groupNews(source)[0].city, '深圳市');
  assert.deepEqual(Core.filterNews(source, '广东省 深圳市'), source);
  assert.equal(JSON.stringify(source), before);
});

test('all-news listing remains intact and ordered including unlocated stories', () => {
  const stories = Array.from({length: 400}, (_, index) => ({id: `n${index}`, title: '演示新闻', geo: null}));
  assert.equal(Core.filterNews(stories, '').length, 400);
  assert.deepEqual(Core.filterNews(stories, '演示新闻'), stories);
  assert.equal(Core.filterNews(stories, '无匹配').length, 0);
  assert.deepEqual(Core.filterNews(null, ''), []);
});

test('country label anchors are unique, immutable and finite geographic points', () => {
  assert.ok(Core.countryLabels.length >= 25);
  assert.equal(new Set(Core.countryLabels.map(item => item.country_id)).size, Core.countryLabels.length);
  assert.ok(Object.isFrozen(Core.countryLabels));
  for (const item of Core.countryLabels) {
    assert.ok(Core.cameraForLocation(item));
    assert.ok(item.label.length > 0);
    assert.ok(Object.isFrozen(item));
  }
});

test('normalized large finite longitude values remain in the canonical interval', () => {
  for (const input of [1e15, -1e15, Number.MAX_VALUE, -Number.MAX_VALUE, .125]) {
    const value = Core.normalizeLon(input);
    assert.ok(value >= -180 && value < 180);
    assert.ok(Number.isFinite(value));
  }
  approx(Core.normalizeLon(.125), .125);
});

function stockFixture() {
  return {
    news: [{id: 'n1'}, {id: 'n2'}],
    sectors: [{id: 'grid', label: '电网'}, {id: 'storage', label: '储能'},
      {id: 'empty', label: '空板块'}, {id: 'compute', label: '算力'}],
    news_links: [
      {news_id: 'n1', sector_id: 'grid', direction: 'opportunity', impact_level: 'high', reason: '电网理由'},
      {news_id: 'n1', sector_id: 'storage', direction: 'risk', impact_level: 'low', reason: '储能理由'},
      {news_id: 'n1', sector_id: 'empty', direction: 'mixed', impact_level: 'medium', reason: '空板块理由'},
      {news_id: 'n2', sector_id: 'compute', direction: 'opportunity', impact_level: 'high', reason: '其他新闻'}
    ],
    stock_links: [
      {sector_id: 'grid', stock_id: 'a'}, {sector_id: 'storage', stock_id: 'b'},
      {sector_id: 'grid', stock_id: 'c'}, {sector_id: 'storage', stock_id: 'c'},
      {sector_id: 'compute', stock_id: 'e'}, {sector_id: 'grid', stock_id: 'f'}
    ],
    stocks: [
      {id: 'a', label: '甲', code: '001234', rule_results: {livermore: {eligible: true}, buffett: {eligible: false}}},
      {id: 'b', label: '乙', ticker: '600123', rule_results: {livermore: {eligible: false}, buffett: {eligible: true}}},
      {id: 'c', label: '丙', rule_results: {livermore: {eligible: true}, buffett: {eligible: true}}},
      {id: 'd', label: '无显式边', sector_id: 'grid', rule_results: {livermore: {eligible: true}}},
      {id: 'e', label: '无关', rule_results: {livermore: {eligible: true}}},
      {id: 'f', label: '缺少结果', rule_results: {baseline: {eligible: true}}}
    ]
  };
}
const stockScope = {newsId: 'n1', sectorId: null, seatId: 'livermore', onlyEligible: true};
const rowIds = result => result.rows.map(row => row.id);

test('stock union includes all explicitly mapped news sectors, never implicit or foreign stocks', () => {
  const result = Core.relatedStockRows(stockFixture(), {...stockScope, onlyEligible: false});
  assert.deepEqual(rowIds(result), ['a', 'b', 'c', 'f']);
  assert.deepEqual(result.sectors.map(sector => sector.id), ['grid', 'storage', 'empty']);
  assert.equal(result.total, 4);
  assert.equal(result.excludedCount, 0);
});

test('current-seat stock gate is independent and total is before filtering', () => {
  const data = stockFixture();
  const livermore = Core.relatedStockRows(data, stockScope);
  const buffett = Core.relatedStockRows(data, {...stockScope, seatId: 'buffett'});
  assert.deepEqual(rowIds(livermore), ['a', 'c']);
  assert.deepEqual(rowIds(buffett), ['b', 'c']);
  assert.equal(livermore.rows[0].rule, data.stocks[0].rule_results.livermore);
  assert.equal(livermore.total, 4);
  assert.equal(livermore.excludedCount, 2);
  assert.equal(buffett.excludedCount, 2);
});

test('stock rows require eligible === true and do not borrow missing-seat rules', () => {
  const data = stockFixture();
  data.stocks[0].rule_results.livermore.eligible = 'true';
  assert.deepEqual(rowIds(Core.relatedStockRows(data, stockScope)), ['c']);
  const missingSeat = Core.relatedStockRows(data, {...stockScope, seatId: 'missing'});
  assert.deepEqual(missingSeat.rows, []);
  assert.equal(missingSeat.total, 4);
  const all = Core.relatedStockRows(data, {...stockScope, seatId: 'missing', onlyEligible: false});
  assert.ok(all.rows.every(row => row.rule === null));
});

test('single-sector stock scope narrows rows without losing all related sector options', () => {
  const result = Core.relatedStockRows(stockFixture(), {...stockScope, sectorId: 'storage'});
  assert.deepEqual(rowIds(result), ['c']);
  assert.equal(result.total, 2);
  assert.equal(result.excludedCount, 1);
  assert.deepEqual(result.rows[0].sectors.map(sector => sector.id), ['storage']);
  assert.equal(result.rows[0].direction, 'risk');
  assert.equal(result.rows[0].impact, 'low');
  assert.deepEqual(result.sectors.map(sector => sector.id), ['grid', 'storage', 'empty']);
});

test('unrelated, invalid and empty sectors never widen the stock scope', () => {
  for (const sectorId of ['compute', 'missing', '', 'empty']) {
    const result = Core.relatedStockRows(stockFixture(), {...stockScope, sectorId});
    assert.deepEqual(result.rows, []);
    assert.equal(result.total, 0);
    assert.equal(result.excludedCount, 0);
    assert.equal(result.sectors.length, 3);
  }
});

test('duplicate links and multi-sector stocks produce one row with all unique reasons', () => {
  const data = stockFixture();
  data.news_links.push({...data.news_links[0]}, {...data.news_links[0], reason: '另一理由'});
  data.stock_links.push({...data.stock_links[0]}, {...data.stock_links[2]});
  const result = Core.relatedStockRows(data, stockScope);
  assert.deepEqual(rowIds(result), ['a', 'c']);
  assert.equal(result.total, 4);
  const multi = result.rows.find(row => row.id === 'c');
  assert.deepEqual(multi.sectors.map(sector => sector.id), ['grid', 'storage']);
  assert.equal(multi.sectors[0].reason, '电网理由；另一理由');
  assert.equal(multi.direction, 'mixed');
  assert.equal(multi.impact, 'high');
});

test('unknown direction or impact contributions remain conservative', () => {
  const data = stockFixture();
  data.news_links[1].direction = 'unverified';
  delete data.news_links[1].impact_level;
  const row = Core.relatedStockRows(data, stockScope).rows.find(item => item.id === 'c');
  assert.equal(row.direction, 'unknown');
  assert.equal(row.impact, 'unknown');
  assert.equal(row.sectors[0].direction, 'opportunity');
  assert.equal(row.sectors[1].direction, 'unknown');
});

test('conflicting duplicate sector directions combine to mixed without summing impact', () => {
  const data = stockFixture();
  data.news_links.push({...data.news_links[0], direction: 'risk', impact_level: 'medium'});
  const row = Core.relatedStockRows(data, stockScope).rows[0];
  assert.equal(row.direction, 'mixed');
  assert.equal(row.impact, 'high');
  assert.equal(row.sectors.length, 1);
});

test('security codes only come from explicit string code or ticker, not sample IDs', () => {
  const data = stockFixture();
  const result = Core.relatedStockRows(data, {...stockScope, onlyEligible: false});
  assert.equal(result.rows[0].code, '001234');
  assert.equal(result.rows[1].code, '600123');
  assert.equal(result.rows[2].code, null);
  data.stocks[0].code = 1234;
  assert.equal(Core.relatedStockRows(data, stockScope).rows[0].code, null);
  data.stocks[0].code = '  ';
  data.stocks[0].ticker = '  001234  ';
  assert.equal(Core.relatedStockRows(data, stockScope).rows[0].code, '001234');
});

test('invalid news, malformed collections and dangling edges do not fabricate stocks', () => {
  const empty = {rows: [], sectors: [], total: 0, excludedCount: 0};
  assert.deepEqual(Core.relatedStockRows(null), empty);
  assert.deepEqual(Core.relatedStockRows({}, null), empty);
  assert.deepEqual(Core.relatedStockRows(stockFixture(), {...stockScope, newsId: 'absent'}), empty);
  const data = stockFixture();
  data.news_links.push(null, {news_id: 'n1', sector_id: 'absent'});
  data.stock_links.push(null, {sector_id: 'grid', stock_id: 'absent'}, {sector_id: 'absent', stock_id: 'a'});
  data.stocks.push(null);
  data.sectors.push(null);
  assert.deepEqual(rowIds(Core.relatedStockRows(data, stockScope)), ['a', 'c']);
});

test('stock union does not mutate frozen input data or rule objects', () => {
  const data = stockFixture();
  const freeze = value => {
    if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
    return value;
  };
  const before = JSON.stringify(data);
  freeze(data);
  const scope = Object.freeze({...stockScope});
  const result = Core.relatedStockRows(data, scope);
  assert.deepEqual(rowIds(result), ['a', 'c']);
  assert.equal(result.rows[0].item, data.stocks[0]);
  assert.equal(JSON.stringify(data), before);
});

test('all related sector options survive when no current-seat stock passes', () => {
  const result = Core.relatedStockRows(stockFixture(), {...stockScope, seatId: 'none'});
  assert.equal(result.rows.length, 0);
  assert.equal(result.total, 4);
  assert.equal(result.excludedCount, 4);
  assert.deepEqual(result.sectors.map(sector => sector.id), ['grid', 'storage', 'empty']);
});

test('research availability uses exact current-seat case and preserves chain evidence', () => {
  const data=stockFixture();
  data.stocks[0].rule_results.livermore.case_id='livermore-normal';
  data.stocks[0].rule_results.buffett.case_id='buffett-value';
  data.news_links[0].kind='mentioned';data.news_links[0].evidence_excerpt='原文摘录';
  data.stock_links[0].reason='人工业务匹配';
  let row=Core.relatedStockRows(data,stockScope).rows[0];
  assert.equal(row.researchAvailable,true);
  assert.equal(row.sectors[0].links[0].kind,'mentioned');
  assert.equal(row.sectors[0].links[0].evidence_excerpt,'原文摘录');
  assert.equal(row.stockMatches[0].reason,'人工业务匹配');
  delete data.stocks[0].rule_results.livermore.case_id;
  row=Core.relatedStockRows(data,stockScope).rows[0];
  assert.equal(row.researchAvailable,false,'No borrowing from buffett case');
  data.stocks[0].rule_results.livermore.case_id='../foreign';
  assert.equal(Core.relatedStockRows(data,stockScope).rows[0].researchAvailable,false);
});
