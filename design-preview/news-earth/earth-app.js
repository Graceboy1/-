/* Geographic news. Preview fixtures stay offline; formal news reads bounded
 * same-origin pages and pauses its single refresh timer while hidden. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const variants = {
    atlas: {description:'地图资讯台 · 大幅裁切地球，新闻与地理位置直接对照。',number:'01'},
    orbit: {description:'轨道观测台 · 环轨构图，定位点与独立新闻面板。',number:'02'},
    horizon: {description:'地平线视窗 · 放大地表弧面，让地理位置成为新闻背景。',number:'03'}
  };
  const parameters = new URLSearchParams(location.search);
  const liveMode = parameters.get('mode') === 'live';
  const key = liveMode ? 'news-earth-live:v1' : 'news-earth-preview:v1';
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(key) || '{}'); } catch (_) { /* optional */ }
  const compare = parameters.get('compare') === '1';
  const embedded = parameters.get('embed') === '1' && window.parent !== window;
  const queryView = parameters.get('view');
  const state = {variant: compare && variants[queryView] ? queryView : 'orbit',selected:saved.selected || 'g1',seat:parameters.get('seat') || saved.seat || 'livermore',eligible:liveMode ? false : saved.eligible !== false,level:'earth',sector:null,stock:null,query:'',offset:0};
  let data, renderer, flight = 0, flightTarget = null, pointer = null;
  let requestController = null, pollTimer = null, searchTimer = null, disposed = false, requesting = false, refreshChecks = 0;
  let aiController = null, aiNewsId = null, aiError = null;
  const stage = $('globe-stage');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  function persist() {
    try { sessionStorage.setItem(key, JSON.stringify({variant:state.variant,selected:state.selected,seat:state.seat,eligible:state.eligible})); } catch (_) { /* no persistence required */ }
  }
  function announce(message) { $('announcer').textContent = message; }
  function notifyHost(type) {
    if (embedded) window.parent.postMessage({channel:'news-earth',type},location.origin);
  }
  function newsItem() { return data?.news.find(item => item.id === state.selected); }
  function place(item) { return item?.geo ? GeoCore.locationLabel(item.geo) : '全球议题 · 地点未指定'; }
  function links(item) { return item ? data.news_links.filter(link => link.news_id === item.id) : []; }
  function sectorNames(item) { return links(item).map(link => data.sectors.find(sector => sector.id === link.sector_id)?.label).filter(Boolean); }
  function dateText(value) {
    const time = typeof value === 'string' ? Date.parse(value) : NaN;
    return Number.isFinite(time) ? new Date(time).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '时间未提供';
  }
  function sourceLink(item) {
    try {
      const url = new URL(item?.url);
      if (url.protocol !== 'https:' || url.username || url.password) return '';
      return `<a class="news-original" href="${escapeHTML(url.href)}" target="_blank" rel="noopener noreferrer">${escapeHTML(item.source || '新闻来源')} · 阅读原文 ↗</a>`;
    } catch (_) { return ''; }
  }
  function businessLink(item) {
    try {
      const url=new URL(item?.source_url);
      if(url.protocol!=='https:' || url.username || url.password)return '';
      const checked=item.source_checked_at || item.source_checked_date;
      return `<p><a class="news-original" href="${escapeHTML(url.href)}" target="_blank" rel="noopener noreferrer">${escapeHTML(item.source_title || '官方业务资料')} ↗</a>${checked?`<br>资料核对日期 ${escapeHTML(checked)}`:''}</p>`;
    } catch (_) {return '';}
  }
  function validateAiAnalysis(value, item) {
    const text = (value, maximum) => typeof value === 'string' && value.length <= maximum;
    if (!item || !value || value.method !== 'llm' || !text(value.model,120) || !value.model
      || !text(value.analyzed_at,80) || !Number.isFinite(Date.parse(value.analyzed_at)) || !/^[a-f0-9]{64}$/i.test(value.source_hash || '')
      || !text(value.summary,600) || value.summary.length<5 || !Array.isArray(value.limitations) || value.limitations.length<1 || value.limitations.length>5
      || value.limitations.some(note=>!text(note,240) || note.length<3) || !Array.isArray(value.relations) || value.relations.length>8
      || !value.evidence || ['title','summary','published_at','source','url'].some(key=>String(value.evidence[key] || '')!==String(item[key] || ''))) {
      throw new Error('AI结果与当前新闻证据不匹配，未替换原关联。');
    }
    const ids=new Set(), corpus=`${item.title} ${item.summary || ''}`.replace(/\s+/g,' ');
    for (const relation of value.relations) {
      if (!relation || !data.sectors.some(sector=>sector.id===relation.sector_id) || ids.has(relation.sector_id)
        || !['opportunity','risk','mixed','unknown'].includes(relation.direction)
        || !['high','medium','low','unknown'].includes(relation.impact_level)
        || !['direct','indirect'].includes(relation.relation_type)
        || !text(relation.label,100) || !text(relation.reason,500) || relation.reason.length<5
        || !text(relation.evidence_excerpt,200) || relation.evidence_excerpt.length<2
        || !text(relation.uncertainty,300) || relation.uncertainty.length<5 || !corpus.includes(relation.evidence_excerpt.replace(/\s+/g,' '))) {
        throw new Error('AI关联板块或引用证据不完整，未替换原关联。');
      }
      ids.add(relation.sector_id);
    }
    return value;
  }
  function analysisFor(item = newsItem()) {
    if (!liveMode || !item) return null;
    try {return validateAiAnalysis(data?.ai_analyses?.[item.id],item);} catch (_) {return null;}
  }
  function applyAiRelations(item, analysis) {
    data.news_links=[...data.news_links.filter(link=>link.news_id!==item.id),...analysis.relations.map(relation=>({
      ...relation,news_id:item.id,kind:'ai_inference',method:'llm',verified:false
    }))];
  }
  function cancelAi() {
    const wasPending=aiController!==null;
    aiController?.abort();aiController=null;aiNewsId=null;
    if(wasPending && !disposed && liveMode && data && renderer){renderLiveStatus();scheduleRefresh();}
  }
  function aiPanel(compact = false) {
    const item=newsItem();
    if (!liveMode || !item) return '';
    const analysis=analysisFor(item), active=aiController!==null && aiNewsId===item.id;
    const available=data?.ai_status?.available===true;
    const unavailableNote=typeof data?.ai_status?.error==='string' ? data.ai_status.error : '尚未连接模型，请在主界面“API设置”完成验证。';
    const directions={opportunity:'潜在利好',risk:'潜在不利',mixed:'影响混合',unknown:'方向待确认'};
    const impacts={high:'较强',medium:'中等',low:'较弱',unknown:'强度待确认'};
    const reason=aiError?.newsId===item.id ? `<p class="news-ai-error" role="alert">AI分析失败：${escapeHTML(aiError.message)} 原有关联未改写，不能当作AI分析结果。</p>` : '';
    const relations=analysis?.relations.map(relation=>`<article class="news-ai-relation ${relation.direction} impact-${relation.impact_level}"><div class="news-ai-relation-title"><strong>${escapeHTML(relation.label)}</strong><span>${directions[relation.direction]} · ${impacts[relation.impact_level]}</span></div><p>${relation.relation_type==='direct'?'直接关联假设':'间接传导假设'} · ${escapeHTML(relation.reason)}</p>${relation.evidence_excerpt?`<blockquote>新闻依据：${escapeHTML(relation.evidence_excerpt)}</blockquote>`:''}<p class="news-ai-uncertainty">待核实：${escapeHTML(relation.uncertainty || '仍需核验实际产业、收入或订单敞口。')}</p></article>`).join('') || '';
    const body=analysis?`<p class="news-ai-summary">${escapeHTML(analysis.summary)}</p>${compact?`<details class="news-ai-details"><summary>查看 ${analysis.relations.length} 个关联板块与依据</summary>`:''}${relations || '<p>本次AI未找到有足够依据的关联板块，不补入无关板块。</p>'}<ul class="news-ai-limitations">${analysis.limitations.map(note=>`<li>${escapeHTML(note)}</li>`).join('')}</ul>${compact?'</details>':''}`:'';
    return `<section class="news-ai-panel${compact?' compact':''}" aria-label="AI板块关联分析"><div class="news-ai-heading"><strong>AI板块关联</strong><span>推测 · 非事实核验</span></div><p class="news-ai-model">${analysis?`${escapeHTML(analysis.model)} · 分析于 ${escapeHTML(dateText(analysis.analyzed_at))} · 已保存缓存`:available?`当前模型：${escapeHTML(data.ai_status.model || '已验证模型')}`:escapeHTML(unavailableNote)}</p><button type="button" class="news-ai-analyze" data-ai-analyze="${escapeHTML(item.id)}"${!available || analysis || aiController || requesting?' disabled':''}>${active?'AI正在分析…':analysis?'AI分析已保存':'AI分析关联板块'}</button><p class="news-ai-cost">点击会将本条标题、短摘要发送至当前模型，可能计费；不自动批量调用，读取已保存结果不重复分析。已提交请求即使停止等待也可能计费。</p>${reason}${body}${analysis?'<p class="news-ai-legend">红色为潜在利好，绿色为潜在不利；明暗仅表示AI评估的影响程度，不是发生概率或收益承诺。</p>':'<p class="news-ai-fallback">尚无AI结果时，仅展示关键词产业假设。</p>'}</section>`;
  }
  async function analyzeNews() {
    const item=newsItem();
    if (!liveMode || disposed || document.hidden || !item || requesting || aiController || data?.ai_status?.available!==true || analysisFor(item)) return;
    const id=item.id, controller=new AbortController();aiController=controller;aiNewsId=id;aiError=null;
    if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=null;
    renderFocus();if(state.level!=='earth')renderInner();renderLiveStatus();
    try {
      const response=await fetch('/contest-api/news/analyze',{method:'POST',credentials:'same-origin',redirect:'error',cache:'no-store',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({news_id:id}),signal:controller.signal});
      const result=await response.json().catch(()=>null);
      if(controller.signal.aborted || disposed || state.selected!==id)return;
      if(response.status===401)notifyHost('session-expired');
      if(!response.ok)throw new Error(typeof result?.detail==='string'?result.detail.slice(0,400):'模型未返回可用结果，请稍后重试。');
      const current=newsItem();
      if(result?.news_id!==id || typeof result.cached!=='boolean')throw new Error('AI响应未对应当前新闻，已拒绝展示。');
      const analysis=validateAiAnalysis(result.analysis,current);
      data.ai_analyses={...(data.ai_analyses || {}),[id]:analysis};applyAiRelations(current,analysis);
      announce(result.cached?'已读取此新闻的AI关联缓存，未重复发起模型分析。':'AI关联分析已保存；仅为新闻传导假设。');
    } catch(error) {
      if(!controller.signal.aborted && !disposed && state.selected===id)aiError={newsId:id,message:error instanceof Error?error.message:'模型请求未完成。'};
    } finally {
      if(aiController===controller){
        aiController=null;aiNewsId=null;
        if(!disposed){renderList();renderFocus();if(state.level!=='earth')renderInner();renderLiveStatus();scheduleRefresh();}
      }
    }
  }
  async function fetchJSON(path, options = {}) {
    const response = await fetch(path,{credentials:'same-origin',redirect:'error',cache:'no-store',...options});
    if (response.status === 401) notifyHost('session-expired');
    if (!response.ok) throw new Error(response.status === 401 ? '登录已失效，请重新登录。' : '新闻缓存读取失败，请稍后重试。');
    return response.json();
  }
  function validateLive(payload) {
    if (payload?.news_mode !== 'live' || payload.preview_only !== false || payload.model_called !== false
      || payload.impact_mode !== 'keyword_hypothesis' || payload.geo_mode !== 'reported_mentions'
      || !['news','sectors','stocks','news_links','stock_links','seats'].every(key=>Array.isArray(payload[key]))
      || payload.news.length > 40 || !payload.meta || !Number.isSafeInteger(payload.meta.total) || payload.meta.total < 0
      || payload.news.some(item=>!item || typeof item.id!=='string' || !/^[A-Za-z0-9_-]{1,64}$/.test(item.id) || typeof item.title!=='string')) {
      throw new Error('新闻数据标识或分页格式不正确，未改用模拟新闻。');
    }
    return payload;
  }
  function renderLiveStatus(error = '') {
    if (!liveMode) return;
    const meta = data?.meta;
    const failures=(meta?.sources || []).filter(source=>source.error || ['error','blocked'].includes(source.status)).length;
    $('live-controls').hidden = false; $('news-pagination').hidden = false;
    const current = meta?.status === 'live' ? '真实新闻' : meta?.status === 'stale' ? '历史缓存 · 更新受阻' : '暂无可用新闻';
    $('data-status').textContent = `数据状态：${current}`;
    $('data-status').title = '新闻为来源原文；地理位置来自明确地名。关键词或AI关联均为产业假设，不是事实核验或买卖结论。';
    $('live-controls').dataset.state = error || failures || meta?.status !== 'live' ? 'warning' : 'live';
    $('live-status').textContent = error || `${current}${meta?.refreshing?' · 正在抓取来源，暂展示缓存':''}${failures?` · ${failures}个来源失败`:''} · 检查 ${dateText(meta?.checked_at)} · 更新 ${dateText(meta?.updated_at)}（北京时间）`;
    const sourceState={pending:'待检查',ok:'正常',error:'失败',blocked:'已停用'};
    $('source-status').innerHTML = (meta?.sources || []).slice(0,12).map(source=>`<p><strong>${escapeHTML(source.label || source.id)}</strong> · ${sourceState[source.status] || '状态待核验'}${source.last_success?`<br>成功 ${escapeHTML(dateText(source.last_success))}`:''}${source.error?`<br><span class="source-failure">${escapeHTML(source.error)}</span>`:''}</p>`).join('') || '<p>来源尚未返回状态。</p>';
    $('refresh-news').disabled = requesting || aiController!==null;
    $('previous-news').disabled = requesting || aiController!==null || state.offset === 0;
    $('next-news').disabled = requesting || aiController!==null || !meta?.has_more;
    $('page-number').textContent = `第 ${Math.floor(state.offset/40)+1} 页 · ${meta?.total ?? 0} 条`;
  }
  function scheduleRefresh(delay = 300000) {
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
    if (!liveMode || disposed || document.hidden) return;
    pollTimer = setTimeout(()=>{pollTimer=null;void refreshLive();},delay);
  }
  function disposeFrame() {
    disposed=true;requestController?.abort();requestController=null;
    cancelAi();
    if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=null;
    if(searchTimer!==null)clearTimeout(searchTimer);searchTimer=null;
    stopFlight();renderer?.destroy();
  }
  function adoptLive(payload) {
    const previous = state.selected, level = state.level;
    data = validateLive(payload);
    if (!data.news.some(item=>item.id===state.selected)) {
      cancelAi();aiError=null;
      state.selected = data.news[0]?.id || null; state.level='earth';state.sector=null;state.stock=null;
    }
    for(const item of data.news){const analysis=analysisFor(item);if(analysis)applyAiRelations(item,analysis);}
    if (!data.seats.some(seat=>seat.id===state.seat)) state.seat=data.seats[0]?.id || 'baseline';
    $('seat').innerHTML=data.seats.map(seat=>`<option value="${escapeHTML(seat.id)}">${escapeHTML(seat.name||seat.label||seat.id)}</option>`).join('');
    $('seat').value=state.seat;$('eligible').checked=state.eligible;
    stopFlight(); renderer.setNews(data.news); renderList(); renderFocus(); renderLiveStatus();
    if (state.selected !== previous) {stage.hidden=false;$('inner-space').hidden=true;document.body.dataset.level='earth';if(newsItem())flyTo(newsItem().geo,true);}
    else if (level !== 'earth' && newsItem()) renderInner();
    persist();
  }
  async function refreshLive({refresh=false} = {}) {
    if (!liveMode || disposed || document.hidden) return;
    if (aiController) {scheduleRefresh();return;}
    if (pollTimer !== null) clearTimeout(pollTimer); pollTimer=null;
    requestController?.abort(); const controller = new AbortController();requestController=controller;requesting=true;
    renderLiveStatus();
    try {
      if (refresh) {await fetchJSON('/contest-api/news/refresh',{method:'POST',signal:controller.signal});refreshChecks=6;}
      const payload = await fetchJSON(`/contest-api/news/feed?limit=40&offset=${state.offset}&q=${encodeURIComponent(state.query)}`,{signal:controller.signal});
      if (!controller.signal.aborted && !disposed) adoptLive(payload);
    } catch (error) {
      if (!controller.signal.aborted && !disposed) renderLiveStatus(error instanceof Error ? error.message : '更新失败，保留上次缓存。');
    } finally {
      if (requestController === controller) {
        requestController=null;requesting=false;
        if(!disposed){
          if(data && renderer){renderFocus();if(state.level!=='earth' && newsItem())renderInner();}
          $('refresh-news').disabled=false;$('previous-news').disabled=state.offset===0;$('next-news').disabled=!data?.meta?.has_more;
          const followUp = refreshChecks>0 && (refresh || data?.meta?.refreshing);
          if (followUp) refreshChecks--; else refreshChecks=0;
          scheduleRefresh(followUp?5000:300000);
        }
      }
    }
  }
  function applyVariant() {
    document.body.dataset.view = state.variant;
    document.body.dataset.compare = String(compare);
    document.body.dataset.embedded = String(embedded);
    document.querySelectorAll('.variants button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === state.variant)));
    const url = new URL(location.href); url.searchParams.set('view',state.variant); history.replaceState(null,'',url);
    renderer?.setVariant(state.variant);
    if (state.level !== 'earth') renderInner();
    persist();
  }
  function renderList() {
    const items = liveMode ? data.news : GeoCore.filterNews(data.news,state.query);
    $('news-count').textContent = liveMode ? `${items.length} / ${data.meta?.total ?? items.length} 条` : state.query ? `${items.length} / ${data.news.length} 条` : `${data.news.length} 条新闻`;
    $('news-list').innerHTML = items.length ? items.map(item => `<button class="news-item" type="button" data-news="${escapeHTML(item.id)}" aria-pressed="${item.id === state.selected}" aria-label="定位新闻：${escapeHTML(item.title)}"><span class="news-meta"><span>${escapeHTML(place(item))}</span><time>${escapeHTML(liveMode ? dateText(item.published_at) : String(item.time || '').replace('（演示）',''))}</time></span><strong>${escapeHTML(item.title)}</strong><span class="news-tags"><span>${escapeHTML(liveMode ? item.source : item.category)}</span><span>${links(item).length} 个关联板块</span>${item.geo ? '' : '<span>地点未明确</span>'}</span></button>`).join('') : `<p class="empty-list">${liveMode ? '暂无匹配的真实新闻。可清空搜索、检查来源状态或稍后更新；不会用模拟新闻补齐。' : '没有匹配的新闻。清空搜索可恢复全部新闻。'}</p>`;
  }
  function revealListSelection() {
    const list = $('news-list');
    const item = [...list.querySelectorAll('[data-news]')].find(button => button.dataset.news === state.selected);
    if (!item) return;
    const a = list.getBoundingClientRect(), b = item.getBoundingClientRect();
    if (b.top < a.top || b.bottom > a.bottom) list.scrollTop += b.top - a.top - Math.max(0,(a.height - b.height)/2);
  }
  function renderFocus() {
    const item = newsItem();
    if (!item) {
      $('focus-card').innerHTML='<h3>等待真实新闻</h3><p class="summary">地图可继续旋转。暂无新闻时不会补入模拟内容，请检查左侧来源状态。</p>';
      $('locate').disabled=true;renderer.setSelected(null);return;
    }
    // Provenance stays in the payload and the persistent page-level status.
    const summary = liveMode ? String(item.summary || '') : String(item.summary || '').replace(/^假想新闻[：:]\s*/,'').replace(/^虚构/,'');
    const provenance = liveMode ? `<div class="news-provenance">${sourceLink(item)}<p>发布 ${escapeHTML(dateText(item.published_at))} · 北京时间${item.language && !/^zh(?:-|$)/i.test(item.language) ? ' · 外文原文' : ''}</p><p>${escapeHTML(item.location_basis || '仅在来源明确提及地名时定位，未定位不代表没有地域影响。')}</p></div>` : '';
    $('focus-card').innerHTML = `<div class="place"><span>${escapeHTML(place(item))}</span></div><h3>${escapeHTML(item.title)}</h3>${provenance}<p class="summary">${escapeHTML(summary)}</p><div class="sector-tags">${sectorNames(item).map(label=>`<span>${escapeHTML(label)}</span>`).join('')}</div>${aiPanel(true)}${item.geo ? '' : '<p class="geo-note">暂无明确地点，未设置地图定位。</p>'}<button class="enter" id="enter-sectors">进入全部 ${links(item).length} 个关联板块 <span>↗</span></button>`;
    $('enter-sectors').addEventListener('click',()=>{stopFlight();state.level='sector';state.sector=null;state.stock=null;renderInner();$('back').focus({preventScroll:true});});
    $('locate').disabled = !item.geo;
    renderer.setSelected(item.id);
  }
  function cameraStatus(moving = false) {
    const {lon,lat} = renderer.camera;
    stage.dataset.cameraLon = lon.toFixed(3); stage.dataset.cameraLat = lat.toFixed(3);
    stage.dataset.selectedNews = state.selected;
    $('coordinate').textContent = `${Math.abs(lat).toFixed(1)}°${lat >= 0 ? 'N' : 'S'} / ${Math.abs(lon).toFixed(1)}°${lon >= 0 ? 'E' : 'W'}`;
    $('camera-status').textContent = moving ? `正在转向 ${place(newsItem())}` : newsItem()?.geo ? `已选中 ${place(newsItem())} · 可自由转动` : '地点未指定 · 保留当前视角';
    stage.dataset.locating = String(moving);
  }
  function stopFlight() { if (flight) cancelAnimationFrame(flight); flight = 0; flightTarget = null; if(renderer && data) cameraStatus(false); }
  function flyTo(geo,immediate = false) {
    stopFlight();
    const target = GeoCore.cameraForLocation(geo);
    if (!target) { cameraStatus(); announce('该新闻没有地图坐标，已显示新闻内容并保留当前视角。'); return; }
    if (immediate || reduced.matches || document.hidden) { renderer.setCamera(target);cameraStatus();return; }
    const origin = {...renderer.camera}, start = performance.now();
    flightTarget = target; cameraStatus(true);
    const tick = now => {
      const t = Math.min(1,(now-start)/430), eased = 1 - Math.pow(1-t,3);
      renderer.setCamera(GeoCore.interpolateCamera(origin,target,eased));cameraStatus(t<1);
      if (t<1) flight=requestAnimationFrame(tick); else {flight=0;flightTarget=null;announce(`已定位到${place(newsItem())}`);}
    };
    flight=requestAnimationFrame(tick);
  }
  function selectNews(id,{locate=true,fromMap=false}={}) {
    if (!data.news.some(item=>item.id===id)) return;
    if(state.selected!==id){cancelAi();aiError=null;}
    state.selected=id;state.level='earth';state.sector=null;state.stock=null;
    document.body.dataset.level='earth';$('inner-space').hidden=true;stage.hidden=false;
    $('location-group').hidden=true;renderList();renderFocus();persist();
    if (locate) flyTo(newsItem().geo); else cameraStatus();
    if (fromMap) { if(state.query && !liveMode){state.query='';$('search').value='';renderList();}revealListSelection(); }
  }
  function pickGroup(group) {
    const id = group.newsIds.includes(state.selected) ? state.selected : group.newsIds[0];
    selectNews(id,{fromMap:true});
    if(group.newsIds.length>1){
      $('location-group').innerHTML=`<h3>${escapeHTML(GeoCore.locationLabel(group))} · ${group.newsIds.length} 条新闻</h3>${group.newsIds.map(newsId=>{const item=data.news.find(n=>n.id===newsId);return `<button data-group-news="${escapeHTML(newsId)}">${escapeHTML(item.title)}</button>`;}).join('')}`;
      $('location-group').hidden=false;
    }
  }
  function renderStockList() {
    const result = GeoCore.relatedStockRows(data,{newsId:state.selected,sectorId:state.sector,seatId:state.seat,onlyEligible:state.eligible});
    const seat = data.seats?.find(item=>item.id===state.seat);
    const seatLabel = seat?.label || seat?.name || '当前席位';
    const directionText = liveMode ? {opportunity:'潜在利好',risk:'潜在不利',mixed:'方向混合',unknown:'未评级'} : {opportunity:'利好',risk:'不利',mixed:'混合',unknown:'待确认'};
    const impactText = {high:'较强',medium:'中等',low:'较弱',unknown:'未评级'};
    if(state.stock && !result.rows.some(row=>row.id===state.stock))state.stock=null;
    const rows = result.rows.map(row=>{
      const rule=row.rule, expanded=state.stock===row.id;
      const status=liveMode ? '待核验' : rule ? rule.eligible===true?'门槛通过':'未通过' : '缺少规则结果';
      const reasons=row.sectors.map(sector=>`<section><h5><span class="direction ${escapeHTML(sector.direction)}">${directionText[sector.direction]||'待确认'}</span> ${escapeHTML(sector.label)}</h5>${(sector.links||[]).map(link=>`<p><b>${link.kind==='ai_inference'?'AI产业传导假设':link.kind==='mentioned'?'文本直接涉及':'产业传导假设'}</b> · ${escapeHTML(link.reason||'暂无具体关联说明。')}</p>${link.evidence_excerpt?`<blockquote>${escapeHTML(link.evidence_excerpt)}</blockquote>`:''}${link.uncertainty?`<p>待核实：${escapeHTML(link.uncertainty)}</p>`:''}`).join('')}</section>`).join('');
      const matches=(row.stockMatches||[]).map(link=>`<p>${escapeHTML(link.business_profile||row.item.business_profile||'人工模拟映射，尚未提供现实公司业务证据。')}</p><p>${escapeHTML(link.reason||'该板块与股票的关联仅来自当前人工映射。')}</p>${businessLink(link)}${link.verification_needed?`<p>${escapeHTML(link.verification_needed)}</p>`:''}`).join('');
      const checks=(rule?.checks||[]).map(check=>`<span class="${check.passed?'check-pass':'check-fail'}">${check.passed?'✓ 通过':'× 未通过'} · ${escapeHTML(check.label||check.name||check.code||'规则检查')}${check.detail?`<small>${escapeHTML(check.detail)}</small>`:''}</span>`).join('');
      const version=typeof rule?.snapshot_version==='string'?rule.snapshot_version.slice(0,12):null;
      const researchButton=embedded?`<button class="stock-research-button" data-stock-research="${escapeHTML(row.id)}" aria-label="研究${escapeHTML(row.label)}"${row.researchAvailable && !liveMode?'':' disabled title="当前新闻时点缺少已核验的股票规则与研究证据"'}>进入研究</button>`:'';
      return `<tr class="stock-row${expanded?' is-selected':''}" data-stock-row="${escapeHTML(row.id)}"><td><strong>${escapeHTML(row.label)}</strong>${row.code?`<small>${escapeHTML(row.code)}</small>`:''}</td><td><div class="stock-sectors">${row.sectors.map(sector=>`<span>${escapeHTML(sector.label)}</span>`).join('')}</div></td><td><span class="direction ${escapeHTML(row.direction)}">${directionText[row.direction]||'待确认'}</span><small>${impactText[row.impact]||'未评级'}</small></td><td><span class="gate ${rule?.eligible===true?'pass':'unpassed'}">${status}</span></td><td><div class="stock-actions">${researchButton}<button class="stock-reason-button" data-stock-detail="${escapeHTML(row.id)}" aria-expanded="${expanded}" aria-label="${expanded?'收起':'查看'}${escapeHTML(row.label)}的关联与筛选原因">${expanded?'收起':'查看原因'} <span>${expanded?'−':'＋'}</span></button></div></td></tr>${expanded?`<tr class="stock-expanded"><td colspan="5"><div class="stock-evidence"><div><h4>01 · 新闻事实</h4><p>${escapeHTML(newsItem().title)}</p><blockquote>${escapeHTML(newsItem().summary||'未提供新闻原文摘录。')}</blockquote><p>${escapeHTML(newsItem().source||'来源未提供')} · ${escapeHTML(newsItem().time||'发布时间未提供')}</p></div><div><h4>02 · 板块关联</h4>${reasons}</div><div><h4>03 · 股票关联</h4>${matches}<p>关联不等于实际收入、订单或价格表现。</p></div><div><h4>04 · 席位判断</h4><h5>${escapeHTML(seatLabel)} · 筛选依据</h5><p>${escapeHTML(rule?.rationale||'暂无当前席位的筛选说明。')}</p>${rule?.decision_date?`<p>规则时点 ${escapeHTML(rule.decision_date)}${version?` · 结果版本 ${escapeHTML(version)}`:''}</p>`:''}<div class="rule-list">${checks}</div></div></div></td></tr>`:''}`;
    }).join('');
    $('relation-detail').innerHTML=`${aiPanel()}<div class="stock-list-heading"><h3>关联股票 <span>${result.rows.length}</span></h3></div>${liveMode?`<p class="live-association-note">${analysisFor()?'AI产业关联假设 · 未经事实核验':'关键词产业假设 · 未验证'}。关联不等于实际订单、收入、涨跌或席位门槛通过；缺少新闻时点的行情与财报，不生成买卖结论。</p>`:''}<div class="stock-scope" aria-label="股票板块范围"><button data-stock-sector="all" aria-pressed="${!state.sector}">全部关联板块</button>${result.sectors.map(sector=>`<button data-stock-sector="${escapeHTML(sector.id)}" aria-pressed="${state.sector===sector.id}">${escapeHTML(sector.label)}</button>`).join('')}</div>${result.rows.length?`<div class="stock-table-wrap"><table class="stock-table"><caption class="sr-only">${escapeHTML(newsItem().title)}的关联股票，使用${escapeHTML(seatLabel)}规则</caption><thead><tr><th scope="col">股票</th><th scope="col">关联板块</th><th scope="col">新闻影响</th><th scope="col">席位筛选</th><th scope="col">依据</th></tr></thead><tbody>${rows}</tbody></table></div>`:`<div class="stock-empty"><strong>${result.total?'当前席位暂无通过的股票':'当前范围暂无关联股票'}</strong><p>${liveMode ? '当前关联范围缺少已核验的业务候选或当时席位结果，不补入合成标的；可查看上方独立的板块关联说明。' : result.total?'可取消“仅看门槛通过”查看未通过项及原因。':'不会补入无关股票，可切换其他板块或新闻。'}</p></div>`}<p class="stock-list-foot sr-only">${result.excludedCount?`另有 ${result.excludedCount} 只未通过当前席位或缺少结果，已隐藏。`:'按明确关联汇总，同一股票去重显示。'}</p>`;
  }
  function enterResearch(stockId) {
    if(!embedded || liveMode)return;
    const result=GeoCore.relatedStockRows(data,{newsId:state.selected,sectorId:state.sector,seatId:state.seat,onlyEligible:state.eligible});
    const row=result.rows.find(item=>item.id===stockId);
    const ids=[state.selected,stockId,state.seat,...(state.sector?[state.sector]:[])];
    if(!row?.researchAvailable||ids.some(id=>typeof id!=='string'||!/^[A-Za-z0-9_-]{1,64}$/.test(id)))return;
    // The parent resolves all names, rules and mappings again on its server.
    // Never transmit arbitrary fixture text, model parameters or a frozen plan.
    window.parent.postMessage({channel:'news-earth',type:'research',newsId:state.selected,stockId,seatId:state.seat,sectorId:state.sector},location.origin);
    announce(`正在为${row.label}新建研究，已有冻结预案不会被覆盖。`);
  }
  function renderInner() {
    if (!newsItem()) return;
    const parentSector = data.sectors.find(sector=>sector.id===state.sector);
    state.level=state.sector?'stock':'sector';document.body.dataset.level=state.level;
    stage.hidden=true;$('inner-space').hidden=false;
    $('back').textContent=state.sector?'← 返回板块':'← 返回新闻页';
    $('breadcrumbs').innerHTML=`<button data-crumb="earth">新闻页</button><span>/</span><button data-crumb="news">${escapeHTML(newsItem().shortTitle || newsItem().title)}</button>${parentSector?`<span>/</span><span>${escapeHTML(parentSector.label)}</span>`:''}`;
    renderStockList();
  }
  function back(){if(state.sector){state.sector=null;state.stock=null;renderInner();}else{state.level='earth';document.body.dataset.level='earth';$('inner-space').hidden=true;stage.hidden=false;renderer.draw();stage.focus({preventScroll:true});}}
  function rotate(dx,dy){stopFlight();renderer.setCamera({lon:GeoCore.normalizeLon(renderer.camera.lon+dx),lat:Math.max(-80,Math.min(80,renderer.camera.lat+dy))});cameraStatus();$('location-group').hidden=true;}
  function wire() {
    document.querySelectorAll('.variants button').forEach(button=>button.addEventListener('click',()=>{state.variant=button.dataset.view;applyVariant();}));
    $('news-list').addEventListener('click',event=>{const button=event.target.closest('[data-news]');if(button)selectNews(button.dataset.news);});
    $('search').addEventListener('input',event=>{
      state.query=String(event.target.value).slice(0,80);state.offset=0;
      if (!liveMode) {renderList();return;}
      cancelAi();aiError=null;
      requestController?.abort();
      if (searchTimer !== null) clearTimeout(searchTimer);
      searchTimer=setTimeout(()=>{searchTimer=null;void refreshLive();},250);
    });
    if (liveMode) {
      $('focus-card').addEventListener('click',event=>{const button=event.target.closest('[data-ai-analyze]');if(button?.dataset.aiAnalyze===state.selected)void analyzeNews();});
      $('refresh-news').addEventListener('click',()=>void refreshLive({refresh:true}));
      $('previous-news').addEventListener('click',()=>{if(!requesting && !aiController && state.offset>0){state.offset=Math.max(0,state.offset-40);void refreshLive();}});
      $('next-news').addEventListener('click',()=>{if(!requesting && !aiController && data?.meta?.has_more){state.offset+=40;void refreshLive();}});
    }
    $('location-group').addEventListener('click',event=>{const button=event.target.closest('[data-group-news]');if(button)selectNews(button.dataset.groupNews,{fromMap:true});});
    $('locate').addEventListener('click',()=>flyTo(newsItem()?.geo));
    document.querySelectorAll('[data-turn]').forEach(button=>button.addEventListener('click',()=>rotate(button.dataset.turn==='left'?-25:25,0)));
    $('back').addEventListener('click',back);
    $('breadcrumbs').addEventListener('click',event=>{const button=event.target.closest('[data-crumb]');if(!button)return;if(button.dataset.crumb==='earth'){state.sector=null;back();}else{state.sector=null;state.stock=null;renderInner();}});
    $('relation-detail').addEventListener('click',event=>{
      const analyze=event.target.closest('[data-ai-analyze]');
      if(analyze){if(analyze.dataset.aiAnalyze===state.selected)void analyzeNews();return;}
      const research=event.target.closest('[data-stock-research]');
      if(research){enterResearch(research.dataset.stockResearch);return;}
      const scope=event.target.closest('[data-stock-sector]');
      if(scope){state.sector=scope.dataset.stockSector==='all'?null:scope.dataset.stockSector;state.stock=null;renderInner();return;}
      const detail=event.target.closest('[data-stock-detail]');
      if(detail){state.stock=state.stock===detail.dataset.stockDetail?null:detail.dataset.stockDetail;renderInner();}
    });
    $('seat').addEventListener('change',event=>{state.seat=event.target.value;state.stock=null;persist();renderInner();});
    $('eligible').addEventListener('change',event=>{state.eligible=event.target.checked;state.stock=null;persist();renderInner();});
    stage.addEventListener('pointerdown',event=>{
      if(event.button!==0||event.target.closest('button,article,.location-group'))return;
      stopFlight();pointer={id:event.pointerId,x:event.clientX,y:event.clientY,lastX:event.clientX,lastY:event.clientY,drag:false};
    });
    stage.addEventListener('pointermove',event=>{
      if(!pointer||pointer.id!==event.pointerId)return;
      if(!pointer.drag&&Math.hypot(event.clientX-pointer.x,event.clientY-pointer.y)>5){pointer.drag=true;stage.setPointerCapture(event.pointerId);stage.classList.add('is-dragging');}
      if(pointer.drag){rotate(-(event.clientX-pointer.lastX)*.25,(event.clientY-pointer.lastY)*.2);renderer.suppressClickUntil=Date.now()+250;}
      pointer.lastX=event.clientX;pointer.lastY=event.clientY;
    });
    const endPointer=event=>{if(!pointer||pointer.id!==event.pointerId)return;if(pointer.drag){renderer.suppressClickUntil=Date.now()+250;if(stage.hasPointerCapture(event.pointerId))stage.releasePointerCapture(event.pointerId);}pointer=null;stage.classList.remove('is-dragging');};
    stage.addEventListener('pointerup',endPointer);stage.addEventListener('pointercancel',endPointer);stage.addEventListener('lostpointercapture',()=>{pointer=null;stage.classList.remove('is-dragging');});
    stage.addEventListener('wheel',event=>{if(event.ctrlKey||event.target.closest('article,.location-group'))return;event.preventDefault();const scale=event.deltaMode===1?16:1;rotate((event.deltaX+(event.shiftKey?0:event.deltaY))*scale*.09,event.shiftKey?event.deltaY*scale*.06:0);},{passive:false});
    stage.addEventListener('keydown',event=>{if(event.target!==stage)return;const turns={ArrowLeft:[-12,0],ArrowRight:[12,0],ArrowUp:[0,8],ArrowDown:[0,-8]};if(turns[event.key]){event.preventDefault();rotate(...turns[event.key]);}else if(event.key==='Home'){event.preventDefault();flyTo(newsItem()?.geo);}});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!event.target.closest('input,select')){if(state.level!=='earth')back();else $('location-group').hidden=true;}});
    document.addEventListener('visibilitychange',()=>{
      if(document.hidden&&flightTarget){const target=flightTarget;stopFlight();renderer.setCamera(target);cameraStatus();}
      if (liveMode) {
        if(document.hidden){
          if(pollTimer!==null)clearTimeout(pollTimer);pollTimer=null;
          if(searchTimer!==null)clearTimeout(searchTimer);searchTimer=null;
          requestController?.abort();
          cancelAi();
        } else {void refreshLive();}
      }
    });
    window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
  }
  async function init(){
    applyVariant();
    if(liveMode){
      document.body.dataset.newsMode='live';$('live-controls').hidden=false;
      $('data-status').textContent='数据状态：读取真实新闻';
      $('data-status').title='真实新闻由后端定时聚合；关键词或AI关联均为待核验假设。';
    }
    try{
      const controller=new AbortController();requestController=controller;
      const [payload,world]=await Promise.all([fetchJSON(liveMode?'/contest-api/news/feed?limit=40&offset=0&q=':'news-data.json',{signal:controller.signal}),fetchJSON('vendor/countries-110m.json',{signal:controller.signal})]);
      if(controller.signal.aborted || disposed)return;
      if(liveMode)validateLive(payload);
      else if(payload.news_mode!=='synthetic'||payload.preview_only!==true||payload.model_called!==false)throw new Error('fixture');
      data=payload;
      if(!data.news.some(n=>n.id===state.selected))state.selected=data.news[0]?.id || null;
      if(!data.seats.some(seat=>seat.id===state.seat))state.seat=data.seats[0]?.id || 'baseline';
      $('seat').innerHTML=data.seats.map(seat=>`<option value="${escapeHTML(seat.id)}">${escapeHTML(seat.name||seat.label||seat.id)}</option>`).join('');
      $('seat').value=state.seat;$('eligible').checked=state.eligible;
      renderer=new EarthRenderer(stage,{onPick:pickGroup});renderer.setWorld(world);renderer.setNews(data.news);renderer.setVariant(state.variant);
      renderList();renderFocus();if(newsItem())flyTo(newsItem().geo,true);wire();stage.dataset.ready='true';
      requestController=null;
      if(liveMode){renderLiveStatus();if(data.meta?.refreshing){refreshChecks=6;scheduleRefresh(5000);}else scheduleRefresh();}
      announce(liveMode?'真实新闻页已就绪，地图只显示当前40条以内的新闻。':'地图和全部新闻已就绪。点击左侧新闻可直接定位。');notifyHost('ready');
    }catch(error){
      if(disposed)return;
      requestController?.abort();requestController=null;
      $('load-error').hidden=false;$('load-error').innerHTML=`<h2>地理页面暂未载入</h2><p>${liveMode?'真实新闻接口或地图暂不可用；未切换到模拟新闻。':'本地地图或新闻数据不可用，请确认服务已启动。'}</p><button id="retry">重新加载</button>`;$('retry').addEventListener('click',()=>location.reload());console.error('Earth news failed:',error);notifyHost('error');
    }
  }
  window.addEventListener('pagehide',disposeFrame);
  init();
})();
