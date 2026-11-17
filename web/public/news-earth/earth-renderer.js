/* Geographic preview renderer. Actual vector geometry is the locally vendored
 * Natural Earth-derived world-atlas dataset; articles/locations are synthetic.
 * Draws only in response to input, data, or resize. No idle render loop. */
(function (root) {
  'use strict';

  const DEG = Math.PI / 180;
  const PALETTE = {
    water: '#131a24', land: '#273442', border: '#687c90',
    grid: '#71859b', text: '#b4c3d4', active: '#6686aa',
    point: '#d5e7fa', mute: '#8298af', glow: '#a7c9ef'
  };
  const fallbackLabels = [
    ['中国', 103, 35, '156'], ['蒙古', 103, 47, '496'],
    ['俄罗斯', 96, 61, '643'], ['日本', 138, 37, '392'],
    ['韩国', 128, 36, '410'], ['印度', 79, 22, '356'],
    ['印度尼西亚', 117, -3, '360'], ['澳大利亚', 134, -25, '036'],
    ['哈萨克斯坦', 68, 48, '398'], ['沙特阿拉伯', 45, 24, '682'],
    ['伊朗', 54, 32, '364'], ['埃及', 30, 27, '818'],
    ['南非', 25, -29, '710'], ['德国', 10, 51, '276'],
    ['法国', 2, 46, '250'], ['英国', -3, 55, '826'],
    ['西班牙', -4, 40, '724'], ['加拿大', -106, 59, '124'],
    ['美国', -101, 39, '840'], ['墨西哥', -102, 24, '484'],
    ['巴西', -53, -13, '076'], ['阿根廷', -65, -36, '032']
  ].map(([label, lon, lat, id]) => ({ label, lon, lat, id }));

  function validGeo(geo) {
    return geo && Number.isFinite(geo.lon) && Number.isFinite(geo.lat)
      && geo.lat >= -90 && geo.lat <= 90;
  }
  function frontDepth(lon, lat, camera) {
    const p = lat * DEG, cp = camera.lat * DEG;
    return Math.sin(p) * Math.sin(cp)
      + Math.cos(p) * Math.cos(cp) * Math.cos((lon - camera.lon) * DEG);
  }
  function intersects(a, b, gap = 5) {
    return a.x < b.x + b.w + gap && a.x + a.w + gap > b.x
      && a.y < b.y + b.h + gap && a.y + a.h + gap > b.y;
  }
  function pathCircle(ctx, x, y, radius) {
    ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2);
  }

  class EarthRenderer {
    constructor(container, options = {}) {
      if (!container || !root.d3 || !root.topojson) {
        throw new Error('地球绘制器缺少本地地图依赖。');
      }
      this.container = container;
      this.options = options;
      this.camera = { lon: 105, lat: 30 };
      this.viewCamera = { ...this.camera };
      this.variant = 'atlas';
      this.news = [];
      this.groups = [];
      this.selectedId = null;
      this.suppressClickUntil = 0;
      this.countries = [];
      this.hits = [];
      this.destroyed = false;
      this.frame = 0;
      this.canvas = document.createElement('canvas');
      this.canvas.className = 'earth-canvas';
      this.canvas.setAttribute('aria-hidden', 'true');
      Object.assign(this.canvas.style, {
        position: 'absolute', inset: '0', width: '100%', height: '100%'
      });
      container.prepend(this.canvas);
      this.context = this.canvas.getContext('2d', { alpha: false });
      this.projection = root.d3.geoOrthographic().clipAngle(90).precision(0.6);
      this.path = root.d3.geoPath(this.projection, this.context);
      this.grid = root.d3.geoGraticule().step([15, 15])();
      this.clickHandler = (event) => {
        if (Date.now() < this.suppressClickUntil) return;
        const rect = this.canvas.getBoundingClientRect();
        const x = event.clientX - rect.left, y = event.clientY - rect.top;
        let nearest = null, distance = Infinity;
        for (const hit of this.hits) {
          const d = Math.hypot(hit.x - x, hit.y - y);
          if ((d < 22 || (hit.labelRect && intersects({x, y, w: 0, h: 0}, hit.labelRect, 0)))
            && d < distance) {
            nearest = hit; distance = d;
          }
        }
        if (nearest && typeof this.options.onPick === 'function') {
          this.options.onPick(nearest.group);
        }
      };
      this.canvas.addEventListener('click', this.clickHandler);
      this.resizeObserver = new ResizeObserver(() => this.draw());
      this.resizeObserver.observe(container);
      this.visibilityHandler = () => { if (!document.hidden) this.draw(); };
      document.addEventListener('visibilitychange', this.visibilityHandler);
      this.draw();
    }

    setWorld(topology) {
      const object = topology && topology.objects && topology.objects.countries;
      if (!object) throw new Error('地图缺少国家边界数据。');
      this.countries = root.topojson.feature(topology, object).features;
      this.boundaries = root.topojson.mesh(topology, object, (a, b) => a !== b);
      this.land = topology.objects.land
        ? root.topojson.feature(topology, topology.objects.land)
        : { type: 'FeatureCollection', features: this.countries };
      this.draw();
    }

    setNews(news) {
      this.news = Array.isArray(news) ? news : [];
      this.groups = root.GeoCore.groupNews(this.news);
      this.draw();
    }

    setSelected(newsId) {
      this.selectedId = newsId || null;
      this.draw();
    }

    setVariant(variant) {
      this.variant = ['atlas', 'orbit', 'horizon'].includes(variant) ? variant : 'atlas';
      this.draw();
    }

    setCamera(camera) {
      if (!validGeo(camera)) return;
      this.camera = {
        lon: ((camera.lon + 180) % 360 + 360) % 360 - 180,
        lat: Math.max(-89.8, Math.min(89.8, camera.lat))
      };
      if (typeof this.options.onCameraChange === 'function') {
        this.options.onCameraChange({ ...this.camera });
      }
      this.draw();
    }

    draw() {
      if (this.destroyed || this.frame || document.hidden) return;
      this.frame = requestAnimationFrame(() => {
        this.frame = 0;
        if (!this.destroyed) this.paint();
      });
    }

    layout(width, height) {
      const small = width < 600;
      if (this.variant === 'orbit') return {
        cx: width * (small ? .48 : .4), cy: height * (small ? .43 : .59),
        radius: Math.min(width * .52, height * .62)
      };
      if (this.variant === 'horizon') {
        const radius = Math.max(width * .60, height * .91);
        // Move the physical sphere centre below the view, then tilt toward the
        // selected location. Its projected y remains at the visible focus row:
        // cy - radius * sin(30 degrees) = height * focusRow.
        const focusRow = small ? .40 : .60;
        return {
          cx: width * (small ? .47 : .40),
          cy: height * focusRow + radius * .5,
          radius,
          tilt: 30
        };
      }
      return {
        cx: width * (small ? .46 : .37), cy: height * (small ? .43 : .62),
        radius: Math.max(Math.min(width * .60, height * .87), 190)
      };
    }

    paint() {
      const width = this.container.clientWidth, height = this.container.clientHeight;
      if (width < 1 || height < 1 || !this.context) return;
      const ratio = Math.min(root.devicePixelRatio || 1, 1.5);
      const pixelWidth = Math.round(width * ratio), pixelHeight = Math.round(height * ratio);
      if (this.canvas.width !== pixelWidth || this.canvas.height !== pixelHeight) {
        this.canvas.width = pixelWidth; this.canvas.height = pixelHeight;
      }
      const ctx = this.context;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.fillStyle = '#121418'; ctx.fillRect(0, 0, width, height);
      const { cx, cy, radius, tilt = 0 } = this.layout(width, height);
      this.viewCamera = { lon: this.camera.lon, lat: this.camera.lat - tilt };
      this.projection.translate([cx, cy]).scale(radius)
        .rotate([-this.viewCamera.lon, -this.viewCamera.lat, 0]);
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';

      // These rings belong to the globe rather than an animated page background.
      if (this.variant === 'orbit') this.drawOrbitalGuides(ctx, cx, cy, radius);
      ctx.save();
      ctx.shadowColor = 'rgba(137,179,223,.16)'; ctx.shadowBlur = 30;
      pathCircle(ctx, cx, cy, radius + .5);
      ctx.fillStyle = PALETTE.water; ctx.fill();
      ctx.shadowBlur = 0;
      const waterLight = ctx.createRadialGradient(cx - radius * .33, cy - radius * .42,
        radius * .1, cx, cy, radius);
      waterLight.addColorStop(0, '#1c2a3a');
      waterLight.addColorStop(.73, '#141e2b');
      waterLight.addColorStop(1, '#10161f');
      ctx.fillStyle = waterLight; ctx.fill();
      ctx.restore();

      ctx.beginPath(); this.path(this.grid);
      ctx.strokeStyle = 'rgba(113,133,155,.22)'; ctx.lineWidth = .65; ctx.stroke();
      const selected = this.news.find(item => item.id === this.selectedId);
      const selectedCountry = selected && selected.geo && selected.geo.country_id;
      for (const feature of this.countries) {
        const active = selectedCountry != null && String(feature.id).padStart(3, '0')
          === String(selectedCountry).padStart(3, '0');
        ctx.beginPath(); this.path(feature);
        ctx.fillStyle = active ? '#3c526a' : PALETTE.land;
        ctx.fill();
        ctx.lineWidth = active ? 1.1 : .7;
        ctx.strokeStyle = active ? '#a5c0df' : 'rgba(104,124,144,.68)';
        ctx.stroke();
      }
      // A fixed limb shade gives depth without an idle render or texture request.
      ctx.save(); pathCircle(ctx, cx, cy, radius); ctx.clip();
      const shade = ctx.createRadialGradient(cx, cy, radius * .52, cx, cy, radius);
      shade.addColorStop(0, 'rgba(3,7,12,0)');
      shade.addColorStop(.86, 'rgba(3,7,12,.09)');
      shade.addColorStop(1, 'rgba(3,7,12,.46)');
      ctx.fillStyle = shade; ctx.fillRect(cx - radius, cy - radius, radius * 2, radius * 2);
      ctx.restore();
      pathCircle(ctx, cx, cy, radius);
      ctx.strokeStyle = 'rgba(153,190,228,.49)'; ctx.lineWidth = 1; ctx.stroke();
      pathCircle(ctx, cx, cy, radius + 3);
      ctx.strokeStyle = 'rgba(153,190,228,.12)'; ctx.lineWidth = 1; ctx.stroke();

      const card = this.container.querySelector('.focus-card');
      let cardRect = null;
      if (card && !card.hidden) {
        const cr = card.getBoundingClientRect(), sr = this.container.getBoundingClientRect();
        cardRect = { x: cr.left - sr.left, y: cr.top - sr.top, w: cr.width, h: cr.height };
      }
      const reserved = cardRect ? [cardRect] : [];
      const markerPoints = this.markerPoints(width, height);
      this.drawCountryLabels(ctx, width, height, markerPoints, reserved);
      this.drawMarkers(ctx, width, height, markerPoints, reserved);
      const activeHit = this.hits.find(hit => hit.group.newsIds.includes(this.selectedId));
      if (activeHit && cardRect && width > 600 && !intersects({x: activeHit.x - 20,
        y: activeHit.y - 20, w: 40, h: 40}, cardRect, 0)) {
        this.drawLeader(ctx, activeHit, cardRect);
      }
      // Numeric position is surfaced by the root's accessible controls, not painted.
      this.canvas.dataset.cameraLon = this.camera.lon.toFixed(4);
      this.canvas.dataset.cameraLat = this.camera.lat.toFixed(4);
      this.canvas.dataset.visibleGroups = String(this.hits.length);
    }

    drawOrbitalGuides(ctx, cx, cy, radius) {
      ctx.save(); ctx.translate(cx, cy); ctx.rotate(-.28);
      ctx.beginPath(); ctx.ellipse(0, 0, radius * 1.23, radius * .44, 0, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(157,187,219,.16)'; ctx.lineWidth = .8;
      ctx.setLineDash([3, 7]); ctx.stroke(); ctx.setLineDash([]);
      ctx.beginPath(); ctx.ellipse(0, 0, radius * 1.12, radius * 1.12, 0, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(157,187,219,.12)'; ctx.stroke();
      for (let i = 0; i < 60; i++) {
        const a = i * Math.PI / 30, r1 = radius * 1.105;
        const r2 = radius * (i % 5 ? 1.12 : 1.135);
        ctx.beginPath(); ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1);
        ctx.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
        ctx.strokeStyle = 'rgba(157,187,219,.25)'; ctx.stroke();
      }
      ctx.restore();
    }

    markerPoints(width, height) {
      return this.groups.map(group => {
        if (!validGeo(group)) return null;
        const depth = frontDepth(group.lon, group.lat, this.viewCamera);
        if (depth <= .035) return null;
        const point = this.projection([group.lon, group.lat]);
        if (!point || point[0] < 15 || point[0] > width - 15
          || point[1] < 65 || point[1] > height - 50) return null;
        return { group, x: point[0], y: point[1], depth,
          selected: group.newsIds.includes(this.selectedId) };
      }).filter(Boolean).sort((a, b) => Number(b.selected) - Number(a.selected));
    }

    drawCountryLabels(ctx, width, height, markers, reserved) {
      const labels = root.GeoCore && Array.isArray(root.GeoCore.countryLabels)
        ? root.GeoCore.countryLabels : fallbackLabels;
      const occupied = reserved.concat(markers.map(p => ({x: p.x - 30, y: p.y - 25, w: 100, h: 50})));
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      const font = '"Source Han Sans SC","Noto Sans CJK SC","Microsoft YaHei UI",sans-serif';
      const sorted = [...labels].sort((a, b) =>
        frontDepth(b.lon, b.lat, this.viewCamera) - frontDepth(a.lon, a.lat, this.viewCamera));
      for (const label of sorted) {
        if (!validGeo(label)) continue;
        const depth = frontDepth(label.lon, label.lat, this.viewCamera);
        if (depth < .25) continue;
        const [x, y] = this.projection([label.lon, label.lat]);
        if (x < 25 || x > width - 25 || y < 82 || y > height - 58) continue;
        const text = label.label || label.name;
        if (!text) continue;
        ctx.font = `${depth > .8 ? 13 : 12}px ${font}`;
        const tw = ctx.measureText(text).width;
        const box = {x: x - tw / 2, y: y - 8, w: tw, h: 16};
        if (occupied.some(other => intersects(box, other, 9))) continue;
        occupied.push(box);
        ctx.globalAlpha = .62 + depth * .28;
        ctx.lineWidth = 3; ctx.strokeStyle = '#1b2937'; ctx.strokeText(text, x, y);
        ctx.fillStyle = PALETTE.text; ctx.fillText(text, x, y);
      }
      ctx.globalAlpha = 1;
    }

    drawMarkers(ctx, width, height, points, reserved) {
      this.hits = [];
      const occupied = [...reserved];
      for (const point of points) {
        const { x, y, selected, group } = point;
        const underCard = reserved.some(box => intersects({x: x - 9, y: y - 9, w: 18, h: 18}, box, 0));
        if (underCard) continue;
        ctx.save();
        if (selected) {
          pathCircle(ctx, x, y, 19); ctx.fillStyle = 'rgba(178,211,247,.08)'; ctx.fill();
          ctx.strokeStyle = 'rgba(178,211,247,.4)'; ctx.lineWidth = .8; ctx.stroke();
          ctx.shadowColor = PALETTE.glow; ctx.shadowBlur = 18;
        }
        pathCircle(ctx, x, y, selected ? 7 : 5);
        ctx.fillStyle = selected ? '#e4f0ff' : '#b5cce6'; ctx.fill();
        ctx.shadowBlur = 0;
        pathCircle(ctx, x, y, selected ? 11 : 9);
        ctx.strokeStyle = selected ? '#9ebcdf' : 'rgba(158,188,223,.49)';
        ctx.lineWidth = .8; ctx.stroke();
        ctx.restore();

        const count = group.newsIds.length;
        const location = root.GeoCore.locationLabel(group, {includeCountry:false});
        const text = `${location}${count > 1 ? ` · ${count} 条` : ''}`;
        ctx.font = `${selected ? '550 ' : ''}12px "Source Han Sans SC","Microsoft YaHei UI",sans-serif`;
        const tw = ctx.measureText(text).width;
        const candidates = [
          {x: x + 17, y: y - 11, w: tw + 14, h: 24},
          {x: x - tw - 31, y: y - 11, w: tw + 14, h: 24},
          {x: x - tw / 2 - 7, y: y + 20, w: tw + 14, h: 24},
          {x: x - tw / 2 - 7, y: y - 42, w: tw + 14, h: 24}
        ];
        const labelRect = candidates.find(box => box.x > 8 && box.x + box.w < width - 8
          && box.y > 67 && box.y + box.h < height - 44
          && !occupied.some(other => intersects(box, other)));
        if (labelRect) {
          occupied.push(labelRect);
          ctx.fillStyle = selected ? 'rgba(25,37,52,.96)' : 'rgba(18,26,37,.86)';
          ctx.fillRect(labelRect.x, labelRect.y, labelRect.w, labelRect.h);
          ctx.strokeStyle = selected ? 'rgba(178,211,247,.55)' : 'rgba(134,164,196,.25)';
          ctx.lineWidth = .5; ctx.strokeRect(labelRect.x, labelRect.y, labelRect.w, labelRect.h);
          ctx.fillStyle = selected ? '#e1edfb' : '#becfe2';
          ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
          ctx.fillText(text, labelRect.x + 7, labelRect.y + 12);
        }
        occupied.push({x: x - 13, y: y - 13, w: 26, h: 26});
        this.hits.push({...point, labelRect});
      }
    }

    drawLeader(ctx, hit, box) {
      const x2 = box.x - 5, y2 = box.y + Math.min(box.h * .48, 90);
      ctx.save();
      ctx.beginPath(); ctx.moveTo(hit.x + 15, hit.y - 7);
      ctx.bezierCurveTo(hit.x + 75, hit.y - 50, x2 - 80, y2 + 10, x2, y2);
      ctx.strokeStyle = 'rgba(176,206,238,.65)'; ctx.lineWidth = .85; ctx.stroke();
      pathCircle(ctx, x2, y2, 2); ctx.fillStyle = '#b4ceed'; ctx.fill();
      ctx.restore();
    }

    destroy() {
      this.destroyed = true;
      if (this.frame) cancelAnimationFrame(this.frame);
      this.frame = 0;
      this.resizeObserver.disconnect();
      this.canvas.removeEventListener('click', this.clickHandler);
      document.removeEventListener('visibilitychange', this.visibilityHandler);
      this.canvas.remove();
      this.hits = []; this.groups = []; this.countries = [];
    }
  }

  root.EarthRenderer = EarthRenderer;
})(typeof window !== 'undefined' ? window : globalThis);
