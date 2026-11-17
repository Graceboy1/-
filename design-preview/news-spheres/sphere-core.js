/* Pure, offline scene and sphere geometry helpers.
   Impact labels are synthetic ordinal examples, never probabilities or returns.
   No DOM, model, account, news feed, replay, or forward-result access. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SphereCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const LEVELS = ["low", "medium", "high"];
  const DIRECTIONS = ["opportunity", "risk", "mixed"];
  const rows = value => Array.isArray(value) ? value : [];
  const unique = items => [...new Map(items.map(item => [item.id, item])).values()];

  function appearance(links) {
    if (!links.length) return { direction: "unknown", impact: "unknown" };
    const directions = new Set(links.map(link => DIRECTIONS.includes(link.direction) ? link.direction : "unknown"));
    const impacts = links.map(link => LEVELS.indexOf(link.impact_level));
    return {
      direction: directions.has("unknown") ? "unknown" : directions.size === 1 ? [...directions][0] : "mixed",
      impact: impacts.includes(-1) ? "unknown" : LEVELS[Math.max(...impacts)],
    };
  }

  /**
   * Derive exactly one containing sphere, retaining ALL explicitly mapped
   * siblings, not a news/sector/stock one-to-one pairing.
   * Invalid news returns the news exterior; an invalid/unrelated sector stays
   * inside the selected news. Explicit link records control membership.
   * `onlyEligible` uses only that seat's existing decision-time eligible flag.
   */
  function deriveScene(data, options = {}) {
    const source = data || {}, news = unique(rows(source.news)), sectors = unique(rows(source.sectors));
    const seatId = options.seatId || rows(source.seats)[0]?.id || "livermore";
    const onlyEligible = options.onlyEligible !== false;
    const stocks = unique(rows(source.stocks)).filter(stock => !onlyEligible || stock.rule_results?.[seatId]?.eligible === true);
    const newsById = new Map(news.map(item => [item.id, item]));
    const sectorsById = new Map(sectors.map(item => [item.id, item]));
    const stocksById = new Map(stocks.map(item => [item.id, item]));
    const newsLinks = rows(source.news_links).filter(link => newsById.has(link.news_id) && sectorsById.has(link.sector_id));
    const stockLinks = rows(source.stock_links).filter(link => sectorsById.has(link.sector_id) && stocksById.has(link.stock_id));
    const sectorIdsByNews = new Map(), stockIdsBySector = new Map(), linksByNews = new Map();
    for (const link of newsLinks) {
      if (!sectorIdsByNews.has(link.news_id)) sectorIdsByNews.set(link.news_id, new Set());
      if (!linksByNews.has(link.news_id)) linksByNews.set(link.news_id, []);
      sectorIdsByNews.get(link.news_id).add(link.sector_id);
      linksByNews.get(link.news_id).push(link);
    }
    for (const link of stockLinks) {
      if (!stockIdsBySector.has(link.sector_id)) stockIdsBySector.set(link.sector_id, new Set());
      stockIdsBySector.get(link.sector_id).add(link.stock_id);
    }
    const parentNews = newsById.get(options.newsId) || null;
    const relatedSectorIds = parentNews ? sectorIdsByNews.get(parentNews.id) || new Set() : new Set();
    const parentSector = parentNews && relatedSectorIds.has(options.sectorId) ? sectorsById.get(options.sectorId) : null;
    const base = (item, kind, links, childCount, subtitle) => ({
      id: item.id, kind, label: item.title || item.label || item.shortTitle || String(item.id),
      subtitle, time: item.time || null, ...appearance(links), childCount,
      item, link: links[0] || null,
    });
    if (!parentNews) {
      return {
        level: "news", parentNews: null, parentSector: null,
        nodes: news.map(item => {
          const childCount = sectorIdsByNews.get(item.id)?.size || 0;
          return base(item, "news", linksByNews.get(item.id) || [], childCount, `${childCount} 个关联板块`);
        }),
      };
    }
    if (!parentSector) {
      return {
        level: "sector", parentNews, parentSector: null,
        nodes: [...relatedSectorIds].map(id => {
          const item = sectorsById.get(id), links = newsLinks.filter(link => link.news_id === parentNews.id && link.sector_id === id);
          const childCount = stockIdsBySector.get(id)?.size || 0;
          return base(item, "sector", links, childCount, `${childCount} 个${onlyEligible ? "规则候选" : "模拟标的"}`);
        }),
      };
    }
    const contextLinks = newsLinks.filter(link => link.news_id === parentNews.id && link.sector_id === parentSector.id);
    return {
      level: "stock", parentNews, parentSector,
      nodes: [...(stockIdsBySector.get(parentSector.id) || [])].map(id => {
        const item = stocksById.get(id), result = item.rule_results?.[seatId];
        const subtitle = result ? `${result.eligible === true ? "门槛通过" : "未满足门槛"} · ${result.passed ?? 0}/${result.total ?? 0}` : "当前席位暂无规则结果";
        return base(item, "stock", contextLinks, 0, subtitle);
      }),
    };
  }

  /**
   * Normalized 3D layout; positive z faces the initial camera.
   * Small layers stay around a clear central parent hub. All records retain a
   * position; large layers are deterministic and may be inspected by rotating.
   * globe = spherical halo, cluster = wider depth ellipse, panorama = two
   * curved inner-wall bands. Coordinates are not market quantities.
   */
  function layoutNodes(count, variant = "globe") {
    const numericCount = Number(count);
    if (!Number.isFinite(numericCount) || numericCount <= 0) return [];
    const size = Math.floor(numericCount), result = [];
    if (size === 1) return [{ x: variant === "panorama" ? .82 : .78, y: -.06, z: variant === "cluster" ? .22 : .56 }];
    // Two large child spheres flank the parent hub instead of sitting directly
    // above/below it; three or more retain the curved two-band panorama.
    if (size === 2 && variant === "panorama") return [{ x: -.84, y: 0, z: .45 }, { x: .84, y: 0, z: .45 }];
    for (let index = 0; index < size; index++) {
      const angle = -Math.PI / 2 + (index + (size === 2 ? .5 : 0)) * 2 * Math.PI / size;
      if (variant === "cluster") {
        const ring = size <= 8 ? 1 : Math.sqrt((index + .7) / size);
        const spin = size <= 8 ? angle : index * Math.PI * (3 - Math.sqrt(5));
        result.push({ x: Math.cos(spin) * .91 * ring, y: Math.sin(spin) * .57 * ring, z: .22 + Math.sin(spin * 2 + .5) * .18 });
      } else if (variant === "panorama") {
        const columns = Math.ceil(size / 2), row = index < columns ? 0 : 1;
        const rowSize = row === 0 ? columns : size - columns;
        const column = row === 0 ? index : index - columns;
        const longitude = rowSize === 1 ? 0 : (column / (rowSize - 1) - .5) * 2.12;
        result.push({ x: Math.sin(longitude) * .95, y: row === 0 ? -.47 : .47, z: Math.cos(longitude) * .48 });
      } else if (size <= 8) {
        const x = Math.cos(angle) * .78, y = Math.sin(angle) * .68;
        result.push({ x, y, z: Math.sqrt(Math.max(0, 1 - x * x - y * y)) });
      } else {
        const y = 1 - 2 * (index + .5) / size;
        const radial = Math.sqrt(Math.max(0, 1 - y * y));
        const longitude = index * Math.PI * (3 - Math.sqrt(5));
        result.push({ x: Math.cos(longitude) * radial, y, z: Math.sin(longitude) * radial });
      }
    }
    return result;
  }

  const finite = (value, fallback) => Number.isFinite(value) ? value : fallback;
  /** Rotate normalized xyz, yaw about Y then pitch about X (radians). */
  function rotate(point, yaw = 0, pitch = 0) {
    const x = finite(point?.x, 0), y = finite(point?.y, 0), z = finite(point?.z, 0);
    const ya = finite(yaw, 0), pa = finite(pitch, 0);
    const turnedX = x * Math.cos(ya) + z * Math.sin(ya);
    const turnedZ = z * Math.cos(ya) - x * Math.sin(ya);
    return { x: turnedX, y: y * Math.cos(pa) - turnedZ * Math.sin(pa), z: y * Math.sin(pa) + turnedZ * Math.cos(pa) };
  }
  /**
   * Project to pixel x/y, rotating internally. `radius` is in CSS pixels.
   * `depth` is rotated z (positive/front); `scale` is perspective magnification.
   * Camera/near-plane clamps keep finite output even at extreme user rotations.
   */
  function project(point, options = {}) {
    const rotated = rotate(point, options.yaw, options.pitch);
    const camera = Math.max(.4, finite(options.camera, 3));
    const scale = Math.max(.25, Math.min(4, camera / Math.max(.15, camera - rotated.z)));
    const radius = Math.max(0, finite(options.radius, 1));
    return {
      x: finite(options.width, 0) / 2 + rotated.x * radius * scale,
      y: finite(options.height, 0) / 2 + rotated.y * radius * scale,
      scale, depth: rotated.z,
    };
  }
  return Object.freeze({ deriveScene, layoutNodes, rotate, project });
});
