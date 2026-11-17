/* Pure geographic helpers. Synthetic news locations are not event evidence. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.GeoCore = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const RAD = Math.PI / 180;
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const bound = (value, low, high) => Math.max(low, Math.min(high, value));

  function normalizeLon(deg) {
    if (!finite(deg)) return 0;
    if (deg >= -180 && deg < 180) return Object.is(deg, -0) ? 0 : deg;
    const value = ((deg % 360) + 540) % 360 - 180;
    return Object.is(value, -0) ? 0 : value;
  }

  function clampLat(deg) {
    return finite(deg) ? bound(deg, -90, 90) : 0;
  }

  function cameraForLocation(geo) {
    if (!geo || !finite(geo.lon) || !finite(geo.lat)
        || Math.abs(geo.lon) > 180 || Math.abs(geo.lat) > 90) return null;
    return { lon: normalizeLon(geo.lon), lat: geo.lat };
  }

  function cameraValue(camera) {
    return {
      lon: normalizeLon(camera && camera.lon),
      lat: clampLat(camera && camera.lat)
    };
  }

  // Linear progress; apply the desired easing to t in the animation controller.
  // At exactly opposite longitudes, consistently travel the negative direction.
  function interpolateCamera(from, to, t) {
    const start = cameraValue(from);
    const end = cameraValue(to);
    const progress = bound(finite(t) ? t : 0, 0, 1);
    if (progress === 0) return start;
    if (progress === 1) return end;
    const delta = normalizeLon(end.lon - start.lon);
    return {
      lon: normalizeLon(start.lon + delta * progress),
      lat: start.lat + (end.lat - start.lat) * progress
    };
  }

  function visibleLocation(geo, camera) {
    const location = cameraForLocation(geo);
    if (!location || !camera || !finite(camera.lon) || !finite(camera.lat)) return false;
    const view = cameraValue(camera);
    const latitude = location.lat * RAD;
    const centreLatitude = view.lat * RAD;
    const delta = normalizeLon(location.lon - view.lon) * RAD;
    const dot = Math.sin(latitude) * Math.sin(centreLatitude)
      + Math.cos(latitude) * Math.cos(centreLatitude) * Math.cos(delta);
    // The horizon is excluded to avoid unstable clickable back-face markers.
    return dot > 1e-10;
  }

  const coordinate = value => Number(value.toFixed(5));
  const placeText = value => typeof value === 'string' ? value.trim() : '';
  function locationLabel(geo, {includeCountry = true} = {}) {
    if (!geo || typeof geo !== 'object') return '地点未指定';
    const parts = [includeCountry ? placeText(geo.country) : '',
      placeText(geo.province), placeText(geo.city) || placeText(geo.label)];
    // Municipalities can occupy both administrative levels; render their name once.
    return [...new Set(parts.filter(Boolean))].join(' · ') || '地点未指定';
  }

  function groupNews(news) {
    const groups = new Map();
    if (!Array.isArray(news)) return [];
    for (const article of news) {
      if (!article || typeof article.id !== 'string' || !article.id) continue;
      const location = cameraForLocation(article.geo);
      if (!location) continue;
      const geo = article.geo;
      const lon = coordinate(location.lon);
      const lat = coordinate(location.lat);
      const country = typeof geo.country === 'string' ? geo.country : '';
      const countryId = geo.country_id == null ? country : String(geo.country_id);
      const id = `geo:${encodeURIComponent(countryId)}:${lon}:${lat}`;
      if (!groups.has(id)) groups.set(id, {
        id,
        label: placeText(geo.city) || placeText(geo.label) || country || '已定位地点',
        country,
        province: placeText(geo.province) || null,
        city: placeText(geo.city) || null,
        country_id: countryId,
        lon,
        lat,
        newsIds: []
      });
      const group = groups.get(id);
      if (!group.newsIds.includes(article.id)) group.newsIds.push(article.id);
    }
    return Array.from(groups.values());
  }

  function filterNews(news, query) {
    if (!Array.isArray(news)) return [];
    const needle = String(query == null ? '' : query).trim().toLocaleLowerCase();
    if (!needle) return news.slice();
    const compact = value => value.replace(/[\s·•/／,，>→-]+/g, '');
    const locationNeedle = compact(needle);
    return news.filter(article => {
      if (!article || typeof article !== 'object') return false;
      const geo = article.geo || {};
      return [article.title, article.shortTitle, article.summary, article.category,
        geo.country, geo.province, geo.city, geo.label].some(value => typeof value === 'string'
          && value.toLocaleLowerCase().includes(needle))
        || Boolean(article.geo && locationNeedle
          && compact(locationLabel(geo).toLocaleLowerCase()).includes(locationNeedle));
    });
  }

  const array = value => Array.isArray(value) ? value : [];
  const record = value => value && typeof value === 'object' && !Array.isArray(value);
  const validId = value => typeof value === 'string' && value.length > 0;
  function stockAppearance(links) {
    if (!links.length) return {direction: 'unknown', impact: 'unknown'};
    const directions = new Set(links.map(link =>
      ['opportunity', 'risk', 'mixed'].includes(link.direction) ? link.direction : 'unknown'));
    const levels = ['low', 'medium', 'high'];
    const impacts = links.map(link => levels.indexOf(link.impact_level ?? link.impact));
    return {
      direction: directions.has('unknown') ? 'unknown'
        : directions.size === 1 ? [...directions][0] : 'mixed',
      impact: impacts.includes(-1) ? 'unknown' : levels[Math.max(...impacts)]
    };
  }

  /** Explicit news -> sector -> stock union, with current-seat gates only.
   * total counts unique stocks in the requested scope BEFORE seat filtering.
   * sectors retains every valid related sector, including empty sectors, so a
   * zero-result selection does not silently widen the news/sector scope.
   * Unknown impact contributors stay unknown; values are never summed.
   */
  function relatedStockRows(data, options = {}) {
    const source = record(data) ? data : {};
    const scope = record(options) ? options : {};
    const empty = sectors => ({rows: [], sectors, total: 0, excludedCount: 0});
    if (!validId(scope.newsId)
        || !array(source.news).some(item => record(item) && item.id === scope.newsId)) return empty([]);
    const sectorById = new Map(), stockById = new Map();
    for (const item of array(source.sectors)) {
      if (record(item) && validId(item.id) && !sectorById.has(item.id)) sectorById.set(item.id, item);
    }
    for (const item of array(source.stocks)) {
      if (record(item) && validId(item.id) && !stockById.has(item.id)) stockById.set(item.id, item);
    }
    const newsLinksBySector = new Map();
    for (const link of array(source.news_links)) {
      if (!record(link) || link.news_id !== scope.newsId || !sectorById.has(link.sector_id)) continue;
      if (!newsLinksBySector.has(link.sector_id)) newsLinksBySector.set(link.sector_id, []);
      newsLinksBySector.get(link.sector_id).push(link);
    }
    const sectors = [...newsLinksBySector].map(([id, links]) => ({
      id, label: sectorById.get(id).label || id, ...stockAppearance(links),
      links: links.map(link => ({...link})),
      reason: [...new Set(links.map(link => typeof link.reason === 'string' ? link.reason.trim() : '')
        .filter(Boolean))].join('；')
    }));
    const descriptors = new Map(sectors.map(sector => [sector.id, sector]));
    const singleSector = scope.sectorId != null;
    if (singleSector && !descriptors.has(scope.sectorId)) return empty(sectors);
    const memberships = new Map();
    for (const link of array(source.stock_links)) {
      if (!record(link) || !descriptors.has(link.sector_id) || !stockById.has(link.stock_id)
          || (singleSector && link.sector_id !== scope.sectorId)) continue;
      if (!memberships.has(link.stock_id)) memberships.set(link.stock_id, new Set());
      memberships.get(link.stock_id).add(link.sector_id);
    }
    const rows = [];
    for (const [id, item] of stockById) {
      if (!memberships.has(id)) continue;
      const results = record(item.rule_results) ? item.rule_results : {};
      const suppliedRule = validId(scope.seatId) && Object.prototype.hasOwnProperty.call(results, scope.seatId)
        ? results[scope.seatId] : null;
      const rule = record(suppliedRule) ? suppliedRule : null;
      if (scope.onlyEligible !== false && rule?.eligible !== true) continue;
      const rowSectors = sectors.filter(sector => memberships.get(id).has(sector.id));
      // IDs such as s1 are fixture identifiers, never a fallback security code.
      const code = [item.code, item.ticker].find(value => typeof value === 'string' && value.trim());
      rows.push({
        id, label: item.label || id, code: code ? code.trim() : null,
        item, rule, sectors: rowSectors,
        stockMatches: array(source.stock_links).filter(link => record(link)
          && link.stock_id === id && memberships.get(id).has(link.sector_id)),
        researchAvailable: typeof rule?.case_id === 'string'
          && /^[A-Za-z0-9_-]{1,64}$/.test(rule.case_id),
        ...stockAppearance(rowSectors)
      });
    }
    return {rows, sectors, total: memberships.size, excludedCount: memberships.size - rows.length};
  }

  // Readable label anchors, not precise borders or claims about sovereignty.
  // Numeric IDs match Natural Earth's ISO-3166 numeric country feature IDs.
  const countryLabels = Object.freeze([
    { country_id: '156', label: '中国', lon: 103, lat: 35 },
    { country_id: '392', label: '日本', lon: 138, lat: 38 },
    { country_id: '410', label: '韩国', lon: 128, lat: 36 },
    { country_id: '356', label: '印度', lon: 79, lat: 23 },
    { country_id: '496', label: '蒙古国', lon: 104, lat: 47 },
    { country_id: '360', label: '印度尼西亚', lon: 118, lat: -3 },
    { country_id: '764', label: '泰国', lon: 101, lat: 16 },
    { country_id: '704', label: '越南', lon: 107, lat: 16 },
    { country_id: '643', label: '俄罗斯', lon: 97, lat: 61 },
    { country_id: '036', label: '澳大利亚', lon: 134, lat: -25 },
    { country_id: '554', label: '新西兰', lon: 173, lat: -41 },
    { country_id: '840', label: '美国', lon: -102, lat: 39 },
    { country_id: '124', label: '加拿大', lon: -106, lat: 58 },
    { country_id: '484', label: '墨西哥', lon: -103, lat: 24 },
    { country_id: '076', label: '巴西', lon: -52, lat: -11 },
    { country_id: '032', label: '阿根廷', lon: -65, lat: -36 },
    { country_id: '152', label: '智利', lon: -71, lat: -30 },
    { country_id: '826', label: '英国', lon: -3, lat: 55 },
    { country_id: '250', label: '法国', lon: 2, lat: 47 },
    { country_id: '276', label: '德国', lon: 10, lat: 51 },
    { country_id: '380', label: '意大利', lon: 13, lat: 43 },
    { country_id: '724', label: '西班牙', lon: -4, lat: 40 },
    { country_id: '682', label: '沙特阿拉伯', lon: 45, lat: 24 },
    { country_id: '818', label: '埃及', lon: 30, lat: 27 },
    { country_id: '710', label: '南非', lon: 25, lat: -29 },
    { country_id: '566', label: '尼日利亚', lon: 8, lat: 9 },
    { country_id: '180', label: '刚果（金）', lon: 23, lat: -3 }
  ].map(Object.freeze));

  return Object.freeze({ normalizeLon, clampLat, cameraForLocation, interpolateCamera,
    visibleLocation, locationLabel, groupNews, filterNews, relatedStockRows, countryLabels });
});
