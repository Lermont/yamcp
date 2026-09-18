/* Client report renderer. No network requests, dependencies or tracking. */
(() => {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icons = {
    grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    chart:'<path d="M4 3v17h17M8 14l4-5 4 3 5-7"/>',
    layers:'<path d="m12 3 10 5-10 5L2 8l10-5Zm-9 10 9 5 9-5M3 18l9 5 9-5"/>',
    check:'<path d="m5 12 4 4L19 6"/>',
    list:'<path d="M9 6h12M9 12h12M9 18h12M3 6h1M3 12h1M3 18h1"/>',
    info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    link:'<path d="m10 13 4-4m-6 8-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m0 10a4 4 0 0 0 6 0l5-5a4 4 0 0 0-6-6l-1 1" transform="translate(1 1) scale(.9)"/>',
    calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18M7 15h3m4 0h3"/>',
    wallet:'<rect x="3" y="6" width="18" height="15" rx="2"/><path d="m3 7 14-4v3m4 6h-6v5h6m-3-2h.01"/>',
    pointer:'<path d="m5 3 3 17 4-6 7-2L5 3Zm7 11 5 7"/>',
    target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    trend:'<path d="m3 17 6-6 4 4L21 5m-6 0h6v6"/>',
    search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    spark:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/>',
    ad:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 13h7M7 16h10"/>',
    eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>'
  };
  const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.info}</svg>`;
  const num = (v, d = null) => v == null || !Number.isFinite(v) ? '—' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits:d ?? 2, minimumFractionDigits:d ?? 0}).format(v);
  const money = (v, d = 0) => v == null || !Number.isFinite(v) ? '—' : `${num(v, d)} ₽`;
  const divide = (a, b) => a == null || b == null || b === 0 ? null : a / b;
  const date = s => new Date(`${s}T12:00:00Z`);
  const niceDate = (s, options = {day:'numeric', month:'long'}) => date(s).toLocaleDateString('ru-RU', {...options, timeZone:'UTC'});
  const channelName = v => ({all:'Все каналы', search:'Поиск', network:'РСЯ'}[v] || v);
  const plural = (n, one, few, many) => { const v=Math.abs(n), last=v%10, tail=v%100; return !Number.isInteger(v)?few:last===1&&tail!==11?one:last>=2&&last<=4&&(tail<12||tail>14)?few:many; };
  const statusTag = (s, done = false) => `<span class="status-tag${done ? ' complete' : ''}">${esc(s)}</span>`;
  let D;
  try { D = JSON.parse($('#report-data').textContent); } catch { $('#load-error').hidden = false; return; }
  const allowedViews = D.views || ['statistics','setup'];
  const menuViews = ['setup','statistics'].filter(v => allowedViews.includes(v));
  const initial = new URLSearchParams(location.search);
  const state = {
    view: allowedViews.includes(initial.get('view')) ? initial.get('view') : allowedViews[0],
    period: D.periods.some(p => p.id === initial.get('period')) ? initial.get('period') : D.periods[0]?.id,
    channel: ['all','search','network'].includes(initial.get('channel')) ? initial.get('channel') : 'all',
    metric:'clicks', compare:true, query:'', sort:'spend', direction:-1
  };
  let toastTimer;
  const toast = message => { const t = $('#toast'); t.textContent = message; t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3500); };
  function updateURL() {
    try { const url = new URL(location.href); url.searchParams.set('view', state.view); if (state.period) url.searchParams.set('period', state.period); url.searchParams.set('channel', state.channel); history.replaceState(null, '', url); } catch { /* file:// browsers may restrict history */ }
  }
  const period = () => D.periods.find(p => p.id === state.period);
  const filteredCampaigns = () => D.campaigns.filter(c => (!period()?.campaignIds || period().campaignIds.includes(c.id)) && (state.channel === 'all' || c.channel === state.channel));
  function rowsFor(p, ids) {
    return p ? D.daily.filter(r => r.date >= p.start && r.date <= p.end && ids.includes(r.campaign)) : [];
  }
  function sum(rows) {
    if (!rows.length) return {spend:null, impressions:null, clicks:null, conversions:null, cpc:null, cpa:null, ctr:null};
    const values = {};
    for (const key of ['spend','impressions','clicks','conversions']) values[key] = rows.some(r => r[key] == null) ? null : rows.reduce((t, r) => t + r[key], 0);
    values.cpc = divide(values.spend, values.clicks); values.cpa = divide(values.spend, values.conversions);
    const ctr = divide(values.clicks, values.impressions); values.ctr = ctr == null ? null : ctr * 100;
    return values;
  }
  function totals(p, ids) {
    const rows=rowsFor(p,ids);
    const expected=p?((date(p.end)-date(p.start))/86400000+1)*ids.length:0;
    return rows.length===expected?sum(rows):sum([]);
  }
  function context() {
    const p = period(), cs = filteredCampaigns(), ids = cs.map(c => c.id), rows = rowsFor(p, ids), previousRows = rowsFor(p?.previous, ids);
    const current = totals(p,ids), previous = totals(p?.previous,ids);
    const measured = Boolean(p?.measurement?.available);
    if (!measured) { current.conversions = null; current.cpa = null; previous.conversions = null; previous.cpa = null; }
    const comparison = Boolean(p?.previous && previousRows.length);
    if (!p?.measurement?.comparable) { previous.conversions = null; previous.cpa = null; }
    return {p, cs, ids, rows, previousRows, current, previous, measured, comparison};
  }
  function delta(current, previous, lowerBetter = false, neutral = false) {
    if (current == null || previous == null) return '<span class="muted">Нет сравнения</span>';
    if (previous === 0) return `<span class="muted">${current === 0 ? 'Без изменений' : 'Ранее — 0'}</span>`;
    const change = (current / previous - 1) * 100;
    const cls = neutral || Math.abs(change) < .05 ? 'flat' : (lowerBetter ? change < 0 : change > 0) ? 'good' : 'bad';
    return `<span class="delta ${cls}">${change > 0 ? '↗ +' : change < 0 ? '↘ −' : ''}${num(Math.abs(change),1)}%</span><span>к прошлому периоду</span>`;
  }
  function metricCard(label, value, foot, glyph, featured = false) {
    return `<article class="kpi${featured?' featured':''}"><div class="kpi-title">${esc(label)}${icon(glyph)}</div><div class="kpi-value">${value}</div><div class="kpi-foot">${foot}</div></article>`;
  }
  function kpis(x) {
    const a=x.current, b=x.previous;
    return metricCard('Расход с НДС',money(a.spend),delta(a.spend,b.spend,false,true),'wallet') + metricCard('Клики по рекламе',num(a.clicks),delta(a.clicks,b.clicks),'pointer') + (x.measured ?
      metricCard('Достижения цели',num(a.conversions),delta(a.conversions,b.conversions),'target',true) + metricCard('Стоимость цели',money(a.cpa),delta(a.cpa,b.cpa,true),'trend') :
      metricCard('Показы рекламы',num(a.impressions),delta(a.impressions,b.impressions),'eye',true) + metricCard('Стоимость клика',money(a.cpc,2),delta(a.cpc,b.cpc,true),'trend'));
  }
  function summary(x) {
    const insight=x.p.insight;
    if (state.channel === 'all' && insight?.dataRevision === x.p.dataRevision && insight?.title && insight?.text) return [insight.title, insight.text];
    const a=x.current;
    if (a.spend == null) return ['Недостаточно данных для итогов', 'Статистика отсутствует или охватывает не все дни и кампании. Итоги не рассчитаны; доступные значения можно посмотреть на графике и в таблице.'];
    if (x.measured && a.conversions != null) return [`${num(a.conversions)} ${plural(a.conversions,'достижение','достижения','достижений')} цели за период`, `${channelName(state.channel)}: ${num(a.clicks)} ${plural(a.clicks,'клик','клика','кликов')} при расходе ${money(a.spend)} с НДС.${a.cpa != null ? ` Средняя стоимость достижения цели — ${money(a.cpa)}.` : ''} Цель: «${x.p.measurement.goal}».`];
    return [`${num(a.clicks)} ${plural(a.clicks,'переход','перехода','переходов')} на сайт`, `${channelName(state.channel)}: расход ${money(a.spend)} с НДС.${a.cpc != null ? ` Средняя стоимость клика — ${money(a.cpc,2)}.` : ''} ${x.p.measurement?.note || 'Данные по целям недоступны; оцениваем привлечение трафика.'}`];
  }
  const banner = (title,text) => `<div class="insight-banner"><div class="insight-symbol">${icon('spark')}</div><div><h2>${esc(title)}</h2><p>${esc(text)}</p></div><span class="arrow" aria-hidden="true">↗</span></div>`;
  function workList(items, next = false) {
    if (!items?.length) return '<p class="subheading">Нет записей за этот период.</p>';
    return `<ul class="work-list">${items.map((t,i)=>`<li><span class="work-icon${next?' next':''}" aria-hidden="true">${next?String(i+1).padStart(2,'0'):'✓'}</span><div><h3>${esc(t.title)}</h3><p>${esc(t.text)}</p>${t.meta?`<span class="task-meta">${esc(t.meta)}</span>`:''}</div></li>`).join('')}</ul>`;
  }
  function methodology(x) {
    const p=x.p;
    return `<details class="panel methodology" id="methodology"><summary>Как читать этот отчёт · источники и определения</summary><div class="methodology-content"><p><strong>Источник:</strong> ${esc(p.source)}. Период: ${niceDate(p.start)} — ${niceDate(p.end,{day:'numeric',month:'long',year:'numeric'})}. ${esc(p.sourceNote || '')}</p><p><strong>Расход:</strong> с НДС, без комиссии агентства. Все цены и стоимости в рублях.${p.previous?` Сравнение: ${niceDate(p.previous.start)} — ${niceDate(p.previous.end)}. Проценты рассчитаны по суммарным показателям периодов.`:' Период для сравнения не задан.'}</p><p><strong>Измерение результата:</strong> ${x.measured?`цель «${esc(p.measurement.goal)}»; ${esc(p.measurement.attribution)}. Достижения цели могут повторяться и не равны уникальным заявкам или продажам.${!p.measurement.comparable?' Сравнение по целям не показано: условия измерения периодов отличаются.':''}`:esc(p.measurement?.note || 'Данные по целям недоступны. Выводы о заявках и продажах не делаем.')}</p><p>Прочерк означает отсутствие данных или невозможность расчёта. Ноль означает измеренный нулевой результат. ${esc(p.completenessNote || '')}</p><div class="glossary-grid"><div><strong>Клики</strong><br>Переходы по рекламным объявлениям.</div><div><strong>Показы</strong><br>Количество показов объявления, включая повторные.</div><div><strong>Кликабельность</strong><br>Клики ÷ показы × 100%.</div><div><strong>Стоимость клика</strong><br>Расход ÷ количество кликов.</div>${x.measured?'<div><strong>Достижения цели</strong><br>Зафиксированные действия выбранного типа.</div><div><strong>Стоимость цели</strong><br>Расход ÷ число достижений выбранной цели.</div>':''}</div></div></details>`;
  }
  function statsView() {
    const x=context(), p=x.p;
    if (!p) { $('#panel-statistics').innerHTML='<section id="overview"><div class="report-heading"><div class="heading-copy"><span class="eyebrow">ОТЧЁТ О ПРОДВИЖЕНИИ</span><h1>Статистика появится здесь<span style="color:var(--accent)">.</span></h1></div></div><div class="panel empty"><h2>Всё в одном отчёте</h2><p>После начала показов добавим результаты рекламы, динамику и выполненные работы. Сейчас доступен раздел «Настройка».</p></div></section>'; return; }
    if (!x.measured && state.metric==='conversions') state.metric='clicks';
    const [headline,description]=summary(x);
    $('#panel-statistics').innerHTML=`<section id="overview"><div class="report-heading"><div class="heading-copy"><span class="eyebrow">ОТЧЁТ О ПРОДВИЖЕНИИ</span><h1>Результаты рекламы<span style="color:var(--accent)">.</span></h1><p><strong>${esc(D.client.name)}</strong> · ${esc(D.client.description)}</p></div><div class="period-control"><label for="period">Отчётный период</label><div class="select-wrap">${icon('calendar')}<select id="period" aria-label="Отчётный период">${D.periods.map(v=>`<option value="${esc(v.id)}"${v.id===state.period?' selected':''}>${esc(v.label)}</option>`).join('')}</select></div><small>${p.previous?`Сравнение: ${niceDate(p.previous.start)} — ${niceDate(p.previous.end)}`:'Без сравнения с предыдущим периодом'}</small></div></div>${banner(headline,description)}<div class="filter-row"><div class="filter-chips" aria-label="Канал рекламы">${['all','search','network'].map(c=>`<button type="button" class="chip" data-channel="${c}" aria-pressed="${c===state.channel}">${channelName(c)}</button>`).join('')}</div><span class="data-note"><span class="small-dot"></span>${esc(p.updated)}</span></div><div class="kpi-grid" id="kpis">${kpis(x)}</div></section><section class="panels-grid" id="dynamics"><div class="panel panel-pad"><div class="panel-heading"><div><h2>Динамика по дням</h2><p>Как менялись показатели в течение периода</p></div><span class="label-pill">${esc(channelName(state.channel))}</span></div><div class="chart-controls"><div class="metric-tabs" aria-label="Показатель графика">${[['clicks','Клики'],['spend','Расход'],...(x.measured?[['conversions','Цели']]:[])].map(([k,v])=>`<button type="button" data-metric="${k}" aria-pressed="${k===state.metric}">${v}</button>`).join('')}</div><label class="compare-toggle"><input id="compare" type="checkbox"${state.compare?' checked':''}${!x.comparison?' disabled':''}>Прошлый период</label></div><div id="chart-content"></div></div><div class="panel budget-panel" id="budget"></div></section><section class="section-block" id="campaigns"><div class="section-heading"><h2><span class="section-no">01 /</span>Эффективность кампаний</h2><span>Показатели выбранного канала</span></div><div class="panel"><div class="table-toolbar"><label class="search-box">${icon('search')}<input id="campaign-search" type="search" aria-label="Найти кампанию" placeholder="Найти кампанию…" value="${esc(state.query)}"></label><button class="button ghost" id="export-csv" type="button">${icon('download')}Таблица CSV</button></div><div class="table-scroll" tabindex="0" role="region" aria-label="Статистика кампаний"><table id="campaign-table"></table></div><p class="table-caption" id="table-caption"></p></div></section>${detailsView()}<section class="work-grid" id="work"><article class="panel work-panel"><h2>Что сделали</h2><p class="subheading">Работа агентства за ${esc(p.label.toLowerCase())} · все каналы</p>${workList(p.work)}</article><article class="panel work-panel"><h2>Что дальше <span style="color:var(--accent)">↗</span></h2><p class="subheading">Приоритеты на следующий период · все каналы</p>${workList(p.next,true)}</article></section>${methodology(x)}`;
    $('#period').addEventListener('change',e=>{state.period=e.target.value;state.query='';statsView();renderNav();updateURL();});
    $$('[data-channel]').forEach(b=>b.addEventListener('click',()=>{state.channel=b.dataset.channel;state.query='';statsView();renderNav();$(`[data-channel="${state.channel}"]`).focus();updateURL();}));
    $$('[data-metric]').forEach(b=>b.addEventListener('click',()=>{state.metric=b.dataset.metric;$$('[data-metric]').forEach(t=>t.setAttribute('aria-pressed',t===b));renderChart();}));
    $('#compare').addEventListener('change',e=>{state.compare=e.target.checked;renderChart();});
    $('#campaign-search').addEventListener('input',e=>{state.query=e.target.value;renderTable();});
    $('#export-csv').addEventListener('click',exportCSV);
    renderChart();renderBudget();renderTable();bindDetails();
  }
  function dailySeries(p, ids, metric, measured=true) {
    if (!p) return [];
    const out=[];
    for(let cursor=date(p.start);cursor<=date(p.end);cursor.setUTCDate(cursor.getUTCDate()+1)) {
      const day=cursor.toISOString().slice(0,10), found=D.daily.filter(r=>r.date===day&&ids.includes(r.campaign));
      const complete=found.length===ids.length && ids.length>0;
      out.push({date:day,value:complete && (metric!=='conversions'||measured) ? sum(found)[metric] : null});
    }
    return out;
  }
  function renderChart() {
    const x=context(), metric=state.metric, current=dailySeries(x.p,x.ids,metric,x.measured);
    const showPrevious=state.compare&&x.comparison&&(metric!=='conversions'||x.p.measurement.comparable);
    const previous=showPrevious?dailySeries(x.p.previous,x.ids,metric,true):[];
    const vals=[...current,...previous].map(r=>r.value).filter(v=>v!=null);
    if(!vals.length){$('#chart-content').innerHTML='<p class="empty">Для выбранного показателя нет данных.</p>';return;}
    const W=680,H=222,left=38,right=13,top=12,bottom=28, max=Math.max(1,...vals)*1.14, n=Math.max(current.length,previous.length,2);
    const px=i=>left+i*(W-left-right)/(n-1), py=v=>H-bottom-(v/max)*(H-top-bottom);
    const line=series=>series.map((v,i)=>v.value==null?'':`${i===0||series[i-1].value==null?'M':'L'}${px(i).toFixed(2)},${py(v.value).toFixed(2)}`).join(' ');
    const unit=metric==='spend'?' ₽':'', label={clicks:'Клики',spend:'Расход',conversions:'Достижения цели'}[metric];
    const tick=v=>v>=1000?`${num(v/1000,1)} тыс.`:num(v);
    const grid=Array.from({length:4},(_,i)=>{const v=max*i/3,y=py(v);return `<line x1="${left}" y1="${y}" x2="${W-right}" y2="${y}" stroke="#eeecf1" stroke-dasharray="3 4"/><text x="${left-9}" y="${y+3}" text-anchor="end">${tick(v)}</text>`;}).join('');
    const indices=[0,...Array.from({length:4},(_,i)=>Math.round((current.length-1)*(i+1)/5)),current.length-1];
    const ticks=[...new Set(indices)].map(i=>`<text x="${px(i)}" y="${H-7}" text-anchor="${i===0?'start':i===current.length-1?'end':'middle'}">${niceDate(current[i].date,{day:'2-digit',month:'short'})}</text>`).join('');
    const area=current.every(v=>v.value!=null)?`<path d="${line(current)} L${px(current.length-1)},${py(0)} L${left},${py(0)}Z" fill="url(#chart-fill)"/>`:'';
    $('#chart-content').innerHTML=`<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-labelledby="chart-title chart-desc"><title id="chart-title">${esc(label)} по дням</title><desc id="chart-desc">${esc(x.p.label)}. ${esc(channelName(state.channel))}. Точные значения доступны в таблице под графиком. ${showPrevious?'Периоды совмещены по порядковому номеру дня.':''}</desc><defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ff6600" stop-opacity=".16"/><stop offset="1" stop-color="#ff6600" stop-opacity=".01"/></linearGradient></defs>${grid}${area}${showPrevious?`<path d="${line(previous)}" fill="none" stroke="#bcafce" stroke-width="2" stroke-dasharray="5 5"/>`:''}<path d="${line(current)}" fill="none" stroke="#ff6600" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>${current.map((v,i)=>v.value==null?'':`<circle class="point" cx="${px(i)}" cy="${py(v.value)}" r="6" data-day="${i}"><title>${niceDate(v.date)}: ${num(v.value)}${unit}</title></circle>`).join('')}${ticks}</svg><div class="chart-tooltip" hidden></div></div><div class="chart-legend"><span><i class="legend-line"></i>${esc(x.p.label)}</span>${showPrevious?`<span><i class="legend-line previous"></i>Прошлый период · по дням</span>`:''}</div><details class="chart-data"><summary>Точные значения по дням</summary><div class="daily-wrap"><table><caption class="muted">${esc(label)}${unit?' · рубли с НДС':''}</caption><thead><tr><th scope="col">Дата</th><th scope="col">Текущий период</th>${showPrevious?'<th scope="col">Дата сравнения</th><th scope="col">Прошлый период</th>':''}</tr></thead><tbody>${Array.from({length:Math.max(current.length,previous.length)},(_,i)=>`<tr><td>${current[i]?niceDate(current[i].date):'—'}</td><td>${num(current[i]?.value)}</td>${showPrevious?`<td>${previous[i]?niceDate(previous[i].date):'—'}</td><td>${num(previous[i]?.value)}</td>`:''}</tr>`).join('')}</tbody></table></div></details>`;
    $$('.point').forEach(el=>{el.addEventListener('pointerenter',()=>{const v=current[Number(el.dataset.day)], t=$('.chart-tooltip');t.textContent=`${niceDate(v.date)} · ${num(v.value)}${unit}`;t.hidden=false;});el.addEventListener('pointerleave',()=>{$('.chart-tooltip').hidden=true;});});
  }
  function renderBudget() {
    const x=context(), amounts=x.cs.map(c=>x.p.budget?.[c.id]);
    const budget=amounts.length&&amounts.every(v=>v!=null)?amounts.reduce((a,b)=>a+b,0):null;
    const ratio=divide(x.current.spend,budget), pct=ratio==null?null:ratio*100, circumference=2*Math.PI*68;
    $('#budget').innerHTML=`<h2>Рекламный бюджет</h2><p>${esc(channelName(state.channel))} · ${esc(x.p.label.toLowerCase())}</p><div class="budget-ring"><svg viewBox="0 0 168 168" aria-hidden="true"><circle cx="84" cy="84" r="68" fill="none" stroke="#f1edf4" stroke-width="12"/><circle cx="84" cy="84" r="68" fill="none" stroke="${pct>100?'#b7432e':'#ff6600'}" stroke-width="12" stroke-linecap="round" stroke-dasharray="${circumference}" stroke-dashoffset="${circumference*(1-Math.min(1,Math.max(0,ratio||0)))}"/></svg><div class="ring-center"><strong>${num(pct)}${pct==null?'':'%'}</strong><span>от плана на период</span></div></div><div class="budget-rows"><div><span>План с НДС</span><strong>${money(budget)}</strong></div><div><span>Израсходовано</span><strong>${money(x.current.spend)}</strong></div><div><span>${ratio>1?'Сверх плана':'Остаток плана'}</span><strong>${budget==null||x.current.spend==null?'—':money(Math.abs(budget-x.current.spend))}</strong></div></div><p class="budget-note">${budget==null?'План бюджета не указан.':ratio>1?'Расход превысил план на выбранный период.':'Плановый бюджет на выбранный период. Остаток плана не является балансом рекламного кабинета.'}</p>`;
  }
  function campaignRows() {
    const x=context(), query=state.query.trim().toLocaleLowerCase('ru');
    return x.cs.filter(c=>`${c.name} ${channelName(c.channel)}`.toLocaleLowerCase('ru').includes(query)).map(c=>{
      const metrics=totals(x.p,[c.id]);if(!x.measured){metrics.conversions=null;metrics.cpa=null;}
      return {...c,...metrics};
    }).sort((a,b)=>{
      const av=a[state.sort],bv=b[state.sort]; if(av==null)return bv==null?0:1;if(bv==null)return -1;
      return (typeof av==='string'?av.localeCompare(bv,'ru'):av-bv)*state.direction;
    });
  }
  function renderTable() {
    const x=context(), rows=campaignRows();
    const cols=[['name','Кампания'],['spend','Расход с НДС'],['impressions','Показы'],['clicks','Клики'],['ctr','Кликабельность'],...(x.measured?[['conversions','Цели'],['cpa','Стоимость цели']]:[['cpc','Стоимость клика']])];
    const value=(r,key)=>['spend','cpa'].includes(key)?money(r[key]):key==='cpc'?money(r[key],2):key==='ctr'?(r[key]==null?'—':`${num(r[key],2)}%`):num(r[key]);
    const total=totals(x.p,rows.map(r=>r.id));
    if(!x.measured){total.conversions=null;total.cpa=null;}
    $('#campaign-table').innerHTML=`<caption class="sr-only" style="position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)">Показатели кампаний за ${esc(x.p.label)}. Кнопки в заголовках сортируют строки.</caption><thead><tr>${cols.map(([key,title])=>`<th scope="col" aria-sort="${state.sort===key?(state.direction===1?'ascending':'descending'):'none'}"><button type="button" data-sort="${key}">${esc(title)}${state.sort===key?(state.direction===1?' ↑':' ↓'):''}</button></th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(r=>`<tr><td><strong>${esc(r.name)}</strong><span class="channel-label ${r.channel}"><i class="small-dot"></i>${esc(channelName(r.channel))}</span></td>${cols.slice(1).map(([k])=>`<td>${value(r,k)}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${cols.length}" class="table-empty">Кампании не найдены. Измените поиск или канал.</td></tr>`}</tbody>${rows.length?`<tfoot><tr><td>Итого по таблице</td>${cols.slice(1).map(([k])=>`<td>${value(total,k)}</td>`).join('')}</tr></tfoot>`:''}`;
    $('#table-caption').textContent=`Найдено кампаний: ${rows.length}. Все суммы — с НДС.${state.query?' Поиск ограничивает таблицу и CSV; общие показатели выше относятся ко всему выбранному каналу.':''}${x.measured?' Цели — достижения выбранного действия, не уникальные заявки.':''}`;
    $('#export-csv').disabled=!rows.length;
    $$('[data-sort]').forEach(b=>b.addEventListener('click',()=>{state.direction=state.sort===b.dataset.sort?-state.direction:-1;state.sort=b.dataset.sort;renderTable();$(`[data-sort="${state.sort}"]`).focus();}));
  }
  function exportCSV() {
    const x=context(), rows=campaignRows();
    const cols=[['name','Кампания'],['channel','Канал'],['spend','Расход с НДС, руб.'],['impressions','Показы'],['clicks','Клики'],['ctr','Кликабельность, %'],...(x.measured?[['conversions','Достижения цели'],['cpa','Стоимость цели, руб.']]:[['cpc','Стоимость клика, руб.']])];
    const cell=v=>`"${String(v??'').replace(/^[=+@\-\t\r]/,"'$&").replace(/"/g,'""')}"`;
    const out=[cols.map(c=>c[1]),...rows.map(r=>cols.map(([k])=>k==='channel'?channelName(r[k]):typeof r[k]==='number'?num(r[k],['ctr','cpa','cpc','spend'].includes(k)?2:0).replace(/[\s\u00a0\u202f]/g,''):r[k]))].map(r=>r.map(cell).join(';')).join('\r\n');
    const url=URL.createObjectURL(new Blob(['\ufeff',out],{type:'text/csv;charset=utf-8'}));
    const a=document.createElement('a');a.href=url;a.download=`report-${state.period}-${state.channel}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Таблица подготовлена к скачиванию');
  }
  const detailDefinitions = {
    keywords:['Ключевые фразы','Условия, по которым Директ подбирал показы: фразы и автоматический подбор. Это не всегда текст запроса пользователя.'],
    queries:['Поисковые запросы','Что пользователи вводили в поиске. Одинаковые запросы объединены по группам. Директ раскрывает не все запросы: сумма может отличаться от общих итогов поиска.'],
    placements:['Площадки РСЯ','Сайты и приложения рекламной сети. Одинаковые площадки объединены по кампаниям.'],
    groups:['Группы объявлений','Сравнение направлений рекламы. Здесь группы, у которых была статистика в выбранном периоде.'],
    ads:['Объявления','Сравнение отдельных объявлений. Номер объявления сохраняется точно; историческая эффективность не подтверждает актуальность его текста.'],
    regions:['Регионы','Регион фактического местонахождения пользователя по данным Директа; может отличаться от региона таргетинга.'],
    devices:['Устройства','Сравнение компьютеров, смартфонов и планшетов. Неизвестные устройства показаны отдельно.'],
    demographics:['Пол и возраст','Совместные сегменты пола и возраста по оценке Директа. Неопределённые значения сохранены; это не подтверждённые персональные данные.']
  };
  const detailState = {};
  const detailLabel = (d,key) => {
    const unknown = v => !v || v==='--' || v==='UNKNOWN' || v.endsWith('_UNKNOWN') || v==='OTHER' ? 'Не определено' : v;
    const gender = {MALE:'Мужчины',FEMALE:'Женщины',GENDER_MALE:'Мужчины',GENDER_FEMALE:'Женщины'};
    const device = {DESKTOP:'Компьютеры',MOBILE:'Смартфоны',TABLET:'Планшеты',SMART_TV:'Смарт-ТВ'};
    const age = v => !v || v.endsWith('_UNKNOWN') ? 'Возраст не определён' : v==='AGE_55'?'55+':v?.startsWith('AGE_') ? v.slice(4).replace(/_/g,'–').replace(/–(UP|PLUS)$/,'+') : unknown(v);
    return ({keywords:()=>d.CriterionType==='AUTOTARGETING'?'Автоматический подбор':unknown(d.Criterion), queries:()=>unknown(d.Query), placements:()=>unknown(d.Placement), groups:()=>unknown(d.AdGroupName), ads:()=>`Объявление № ${d.AdId}`, regions:()=>unknown(d.LocationOfPresenceName), devices:()=>device[d.Device]||unknown(d.Device), demographics:()=>`${gender[d.Gender]||unknown(d.Gender)} · ${age(d.Age)}`}[key])();
  };
  function detailRows(key) {
    const part=period()?.breakdowns?.slices?.[key], grouped=new Map(), ids=new Set(filteredCampaigns().map(c=>c.id));
    const identity={keywords:['CriterionId','CriterionType','Criterion'],queries:['Query'],placements:['Placement'],groups:['AdGroupId'],ads:['AdId'],regions:['LocationOfPresenceId','LocationOfPresenceName'],devices:['Device'],demographics:['Gender','Age']}[key];
    for(const row of part?.rows||[]) {
      if(!ids.has(row.campaign))continue;
      const id=JSON.stringify(identity.map(f=>row.dimensions[f]));
      if(!grouped.has(id))grouped.set(id,[]);
      grouped.get(id).push(row);
    }
    const config=detailState[key] ||= {query:'',sort:'clicks',direction:-1,page:0};
    return [...grouped.values()].map(rows=>{
      const first=rows[0], groupNames=[...new Set(rows.map(r=>r.dimensions.AdGroupName).filter(Boolean))];
      const channels=[...new Set(rows.map(r=>channelName(r.channel)))].join(' + ');
      const names=[...new Set(rows.map(r=>D.campaigns.find(c=>c.id===r.campaign)?.name||r.campaign))].join(' · ');
      const group=groupNames.length>1?`${groupNames.length} групп`:groupNames[0];
      const copy=key==='ads'?part.currentCopy?.[first.dimensions.AdId]:null;
      return {name:copy?.title||detailLabel(first.dimensions,key),meta:[channels,group,copy?`№ ${first.dimensions.AdId} · Текущий текст: ${copy.text}`:null].filter(Boolean).join(' · '),campaigns:names,
        identifier:first.dimensions.AdId||first.dimensions.CriterionId||first.dimensions.AdGroupId||'',...sum(rows)};
    }).filter(r=>[r.name,r.meta,r.campaigns,r.identifier].join(' ').toLocaleLowerCase('ru').includes(config.query.toLocaleLowerCase('ru')))
      .sort((a,b)=>{const x=a[config.sort],y=b[config.sort];return x==null?(y==null?0:1):y==null?-1:typeof x==='string'?config.direction*x.localeCompare(y,'ru'):config.direction*(x-y)||a.name.localeCompare(b.name,'ru');});
  }
  function detailColumns() {
    return [['name','Сегмент'],['clicks','Клики'],['impressions','Показы'],['spend','Расход, ₽'],['ctr','CTR, %'],['cpc','Цена клика, ₽'],...(period()?.measurement.available?[['conversions','Цели'],['cpa','Цена цели, ₽']]:[])];
  }
  function detailsView() {
    return `<section class="section-block" id="breakdowns"><div class="section-heading"><h2><span class="section-no">02 /</span>Детальная статистика</h2><span>Сравнение по кликам и стоимости</span></div><p class="subheading">Восемь срезов за выбранный период. По умолчанию первыми идут строки с наибольшим числом кликов. Цели означают достижения выбранного действия, не подтверждённые продажи.</p>${Object.entries(detailDefinitions).map(([key,[title,note]])=>{
      const part=period()?.breakdowns?.slices?.[key];
      return `<details class="panel detail-panel" data-detail="${key}"${(detailState[key]?.open??key==='groups')?' open':''}><summary>${esc(title)}<span class="detail-count">${part?num(detailRows(key).length)+' строк':'Нет выгрузки'}</span></summary><div class="detail-content"><p class="subheading">${esc(note)}</p>${part?`<div class="table-toolbar"><label class="search-box">${icon('search')}<input type="search" data-detail-search="${key}" aria-label="Поиск: ${esc(title)}" placeholder="Найти в таблице…" value="${esc(detailState[key]?.query||'')}"></label><button class="button ghost" data-detail-export="${key}" type="button">${icon('download')}Все строки CSV</button></div><p class="detail-scroll-hint">Прокрутите таблицу вправо, чтобы увидеть показатели →</p><div class="table-scroll" tabindex="0" role="region" aria-label="${esc(title)}"><table id="detail-table-${key}"></table></div><div class="detail-pagination" id="detail-pagination-${key}"></div><p class="table-caption">Все суммы с НДС. Прочерк — значение недоступно. Суммы срезов могут отличаться на округление.${part.reconciliation?.some(c=>!c.trafficMatches)?' Охват этого среза отличается от общего отчёта: итоги не следует складывать с другими срезами.':''} Обновлено ${esc(new Date(period().breakdowns.collectedAt).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'}))} · Москва.</p>`:'<p class="empty">Этот срез ещё не выгружен для выбранного периода. Отсутствие выгрузки не означает нулевой результат.</p>'}</div></details>`;
    }).join('')}</section>`;
  }
  function renderDetail(key) {
    const table=$(`#detail-table-${key}`);if(!table)return;
    const rows=detailRows(key), c=detailState[key], cols=detailColumns(), size=20, pages=Math.max(1,Math.ceil(rows.length/size));
    c.page=Math.min(c.page,pages-1);
    const shown=rows.slice(c.page*size,(c.page+1)*size), format=(r,k)=>k==='name'?`<strong>${esc(r.name)}</strong><small class="detail-meta">${esc(r.meta||'')}</small>`:num(r[k],['spend','ctr','cpc','cpa','conversions'].includes(k)?2:0);
    table.innerHTML=`<caption class="sr-only">${esc(detailDefinitions[key][0])}. Сортировка и поиск применяются ко всем строкам.</caption><thead><tr>${cols.map(([k,t])=>`<th scope="col" aria-sort="${c.sort===k?(c.direction===1?'ascending':'descending'):'none'}"><button type="button" data-detail-sort="${key}" data-column="${k}">${t}${c.sort===k?(c.direction===1?' ↑':' ↓'):''}</button></th>`).join('')}</tr></thead><tbody>${shown.length?shown.map(r=>`<tr>${cols.map(([k])=>`<td>${format(r,k)}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${cols.length}" class="table-empty">Нет строк для выбранного канала и поиска.</td></tr>`}</tbody>${rows.length?`<tfoot><tr>${cols.map(([k])=>`<td>${k==='name'?'Итого по найденным строкам':format(sum(rows),k)}</td>`).join('')}</tr></tfoot>`:''}`;
    $(`#detail-pagination-${key}`).innerHTML=`<span>Строки ${rows.length?c.page*size+1:0}–${Math.min((c.page+1)*size,rows.length)} из ${num(rows.length)}</span><div><button type="button" class="button ghost" data-detail-page="${key}" data-step="-1" ${c.page===0?'disabled':''} aria-label="Предыдущая страница: ${esc(detailDefinitions[key][0])}">← Назад</button><button type="button" class="button ghost" data-detail-page="${key}" data-step="1" ${c.page===pages-1?'disabled':''} aria-label="Следующая страница: ${esc(detailDefinitions[key][0])}">Далее →</button></div>`;
    $$(`[data-detail-sort="${key}"]`).forEach(b=>b.addEventListener('click',()=>{c.direction=c.sort===b.dataset.column?-c.direction:-1;c.sort=b.dataset.column;c.page=0;renderDetail(key);$(`[data-detail-sort="${key}"][data-column="${c.sort}"]`).focus();}));
    $$(`[data-detail-page="${key}"]`).forEach(b=>b.addEventListener('click',()=>{c.page+=Number(b.dataset.step);renderDetail(key);$(`#detail-table-${key}`).parentElement.focus({preventScroll:true});}));
    $(`[data-detail-export="${key}"]`).disabled=!rows.length;
  }
  function bindDetails() {
    Object.keys(detailDefinitions).forEach(renderDetail);
    $$('[data-detail]').forEach(el=>el.addEventListener('toggle',()=>{(detailState[el.dataset.detail] ||= {query:'',sort:'clicks',direction:-1,page:0}).open=el.open;}));
    $$('[data-detail-search]').forEach(input=>input.addEventListener('input',()=>{const key=input.dataset.detailSearch;detailState[key].query=input.value;detailState[key].page=0;renderDetail(key);}));
    $$('[data-detail-export]').forEach(button=>button.addEventListener('click',()=>{
      const key=button.dataset.detailExport, rows=detailRows(key), cols=[...detailColumns(),['meta','Канал и группа'],['campaigns','Кампании'],['identifier','ID']];
      const cell=v=>`"${String(v??'').replace(/^[=+@\-\t\r]/,"'$&").replace(/"/g,'""')}"`;
      const text=[cols.map(c=>c[1]),...rows.map(r=>cols.map(([k])=>typeof r[k]==='number'?String(r[k]).replace('.',','):k==='identifier'&&r[k]?`'${r[k]}`:r[k]))].map(row=>row.map(cell).join(';')).join('\r\n');
      const url=URL.createObjectURL(new Blob(['\ufeff',text],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`${key}-${state.period}-${state.channel}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast(`В CSV: ${rows.length} строк с учётом поиска и канала`);
    }));
  }
  function commonAdExtensions(ads) {
    return Object.fromEntries(['sitelinks','callouts'].map(key=>{
      const values=ads[0]?.[key];
      const common=ads.length>1 && Array.isArray(values) && values.length>0 && ads.every(ad=>
        Array.isArray(ad[key]) && ad[key].length===values.length && ad[key].every((value,index)=>value===values[index]));
      return [key,common ? values : null];
    }));
  }
  function adExtension(label,values) {
    return Array.isArray(values) && values.length ? `<p><strong>${esc(label)}:</strong> ${values.map(esc).join(', ')}</p>` : '';
  }
  function setupView() {
    const s=D.setup;
    if(!s){$('#panel-setup').innerHTML='<div class="panel empty">Отчёт о настройке пока не добавлен.</div>';return;}
    const common=commonAdExtensions(s.adVariants || []);
    const commonSettings=common.sitelinks || common.callouts ? `<article class="panel ad-copy" id="setup-common-settings" style="margin-top:18px"><h3>Общие настройки объявлений</h3><p class="muted">Одинаковые для всех объявлений.</p>${adExtension('Быстрые ссылки',common.sitelinks)}${adExtension('Уточнения',common.callouts)}</article>` : '';
    const stats=[['Кампании',D.campaigns.length,'Поиск и рекламная сеть','layers'],['Группы объявлений',D.campaigns.reduce((t,c)=>t+c.groups,0),'Разделены по направлениям','grid'],['Объявления',D.campaigns.reduce((t,c)=>t+c.ads,0),'Подготовлены для показа','ad'],['Ключевые фразы',D.campaigns.reduce((t,c)=>t+c.keywords.length,0),'Собраны по темам','search']];
    $('#panel-setup').innerHTML=`<section id="setup-overview"><div class="report-heading"><div class="heading-copy"><span class="eyebrow">ОТЧЁТ О НАСТРОЙКЕ</span><h1>${esc(s.title)}<span style="color:var(--accent)">.</span></h1><p><strong>${esc(D.client.name)}</strong> · Настройка от ${niceDate(s.date,{day:'numeric',month:'long',year:'numeric'})}</p></div><span class="setup-status"><span class="small-dot"></span>${esc(s.status)}</span></div>${banner(s.summary.title,s.summary.text)}<div class="kpi-grid setup-kpis">${stats.map(([a,b,c,d])=>metricCard(a,num(b),esc(c),d)).join('')}</div><div class="setup-overview"><article class="panel project-brief"><h2>Задача проекта</h2><p>${esc(s.objective)}</p><dl class="brief-facts">${s.facts.map(f=>`<div><dt>${esc(f.label)}</dt><dd>${esc(f.value)}</dd></div>`).join('')}</dl></article><article class="panel delivery-panel"><span class="delivery-label">СЛЕДУЮЩИЙ ШАГ</span><h2>${esc(s.nextStep.title)}</h2><p>${esc(s.nextStep.text)}</p><div class="readiness" aria-hidden="true">${s.checks.map(c=>`<i${c.complete?' class="done"':''}></i>`).join('')}</div><small>${num(s.checks.filter(c=>c.complete).length)} из ${num(s.checks.length)} этапов выполнено</small></article></div></section><section class="section-block" id="setup-campaigns"><div class="section-heading"><h2><span class="section-no">01 /</span>Что настроили</h2><span>${D.campaigns.length} кампании</span></div><div class="setup-campaigns">${D.campaigns.map(c=>`<article class="panel campaign-card"><div class="campaign-card-head"><div><span class="channel-label ${c.channel}"><i class="small-dot"></i>${esc(channelName(c.channel))}</span><h3>${esc(c.name)}</h3></div>${statusTag(c.status)}</div><p class="campaign-purpose">${esc(c.purpose)}</p><dl class="settings-list">${c.settings.map(f=>`<div><dt>${esc(f.label)}</dt><dd>${esc(f.value)}</dd></div>`).join('')}</dl><details><summary>Ключевые фразы и исключения</summary><div class="tag-list">${c.keywords.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div><p class="muted" style="margin-top:12px">Исключения: ${esc(c.negatives.join(', '))}.</p></details></article>`).join('')}</div>${commonSettings}</section><section class="section-block" id="creatives"><div class="section-heading"><h2><span class="section-no">02 /</span>Объявления</h2><span>Пример и варианты текстов</span></div>${s.adVariants?.length ? s.adVariants.map(a=>`<article class="panel ad-copy" style="margin-bottom:16px"><span class="channel-label ${esc(a.channel)}">${esc(channelName(a.channel))}</span><h3>${esc(a.group)}</h3><p class="muted">Объявление № ${esc(a.id)} · ${esc(a.status)}</p><div class="ad-grid"><div><h3>Варианты заголовков</h3><ol>${a.titles.map(t=>`<li>${esc(t)}</li>`).join('')}</ol></div><div><h3>Варианты описаний</h3><ol>${a.texts.map(t=>`<li>${esc(t)}</li>`).join('')}</ol><p class="muted">${esc(a.note)}</p>${common.sitelinks ? '' : adExtension('Быстрые ссылки',a.sitelinks)}${common.callouts ? '' : adExtension('Уточнения',a.callouts)}</div></div></article>`).join('') : `<div class="ad-grid"><article class="panel ad-preview"><div class="ad-label"><span class="small-dot"></span>Предпросмотр · пример сочетания</div><div class="ad-domain">${esc(s.ad.domain)} · Реклама</div><h3>${esc(s.ad.titles[0])}</h3><p>${esc(s.ad.texts[0])}</p><div class="sitelinks">${s.ad.sitelinks.map(v=>`<span>${esc(v)}</span>`).join('')}</div><p class="ad-disclaimer">Схематичный пример. Вид и сочетание элементов зависят от места показа.</p></article><article class="panel ad-copy"><h3>Варианты заголовков</h3><ol>${s.ad.titles.map(t=>`<li>${esc(t)}</li>`).join('')}</ol><h3>Варианты описаний</h3><ol>${s.ad.texts.map(t=>`<li>${esc(t)}</li>`).join('')}</ol><p class="muted" style="font-size:10px">Система комбинирует заголовки и описания. Это варианты элементов одного объявления.</p></article></div>`}</section><section class="section-block" id="launch-check"><div class="section-heading"><h2><span class="section-no">03 /</span>Готовность к запуску</h2><span>Проверки и согласования</span></div><div class="panel setup-checklist">${s.checks.map(c=>`<div class="check-row"><span class="work-icon${c.complete?'':' next'}" aria-hidden="true">${c.complete?'✓':'○'}</span><div><h3>${esc(c.title)}</h3><p>${esc(c.text)}</p></div>${statusTag(c.status,c.complete)}</div>`).join('')}</div></section><section class="work-grid" id="setup-next"><article class="panel work-panel"><h2>Что сделали</h2><p class="subheading">Результат работы над настройкой</p>${workList(s.work)}</article><article class="panel work-panel"><h2>После запуска <span style="color:var(--accent)">↗</span></h2><p class="subheading">План первых шагов</p>${workList(s.next,true)}</article></section>`;
    $('#panel-setup h1').innerHTML=`${esc(s.title || 'Реклама готова к запуску')}<span style="color:var(--accent)">.</span>`;
  }
  const navItems={statistics:[['overview','Обзор','grid'],['dynamics','Динамика','chart'],['campaigns','Кампании','layers'],['breakdowns','Детальная статистика','search'],['work','Работа и планы','list'],['methodology','Как читать отчёт','info']],setup:[['setup-overview','Обзор настройки','grid'],['setup-campaigns','Кампании','layers'],['creatives','Объявления','ad'],['launch-check','Перед запуском','check'],['setup-next','Дальнейшие шаги','trend']]};
  function syncMenu(){ $('#sidebar').inert=matchMedia('(max-width:760px)').matches&&!$('#sidebar').classList.contains('is-open'); }
  function closeMenu(){ $('#sidebar').classList.remove('is-open');$('#scrim').hidden=true;$('#menu-toggle').setAttribute('aria-expanded','false');syncMenu(); }
  let activeObserver;
  function renderNav(){
    const items=navItems[state.view].filter(([id])=>$(`#${id}`));
    $('#section-nav').innerHTML=items.map(([id,label,glyph],i)=>`<a href="#${id}"${i===0?' class="active" aria-current="location"':''}>${icon(glyph)}${label}<span class="nav-number">0${i+1}</span></a>`).join('')+`<a href="#contacts">${icon('link')}Контакты</a>`;
    $$('#section-nav a').forEach(a=>a.addEventListener('click',()=>{closeMenu();const el=$(a.getAttribute('href'));if(el?.tagName==='DETAILS')el.open=true;el?.setAttribute('tabindex','-1');el?.focus({preventScroll:true});}));
    activeObserver?.disconnect();
    activeObserver=new IntersectionObserver(entries=>{const visible=entries.filter(e=>e.isIntersecting);if(!visible.length)return;const id=visible[0].target.id;$$('#section-nav a').forEach(a=>{const on=a.getAttribute('href')===`#${id}`;a.classList.toggle('active',on);if(on)a.setAttribute('aria-current','location');else a.removeAttribute('aria-current');});},{rootMargin:'-4% 0px -62% 0px',threshold:0});
    navItems[state.view].forEach(([id])=>{const el=$(`#${id}`);if(el)activeObserver.observe(el);});
  }
  function setView(view){
    state.view=view;
    allowedViews.forEach(v=>{const active=v===view; $(`#panel-${v}`).hidden=!active;$(`#tab-${v}`).setAttribute('aria-selected',active);$(`#tab-${v}`).tabIndex=active?0:-1;});
    document.title=`${view==='statistics'?'Результаты рекламы':'Настройка рекламы'} · ${D.client.name} · Media Targeting`;
    $('#report-view-label').textContent=view==='statistics'?'Статистика проекта':'Настройка рекламы';
    renderNav();updateURL();
  }
  async function copyText(value, success, title, description, label) {
    try { await navigator.clipboard.writeText(value);toast(success); }
    catch { $('#share-dialog-title').textContent=title;$('#share-dialog-description').textContent=description;$('#share-url').value=value;$('#share-url').setAttribute('aria-label',label);$('#link-dialog').showModal();$('#share-url').select(); }
  }
  function bind(){
    $$('[data-icon]').forEach(el=>{el.innerHTML=icon(el.dataset.icon);});
    $('#sidebar-client').textContent=D.client.name;$('#sidebar-description').textContent=D.client.shortDescription;$('#client-monogram').textContent=D.client.monogram || D.client.name[0];$('#breadcrumb-client').textContent=D.client.name;$('#report-number').textContent=D.reportNumber || '';
    $('#demo-badge').hidden=!D.demo;$('#demo-notice').hidden=!D.demo;
    for(const v of ['statistics','setup'])if(!allowedViews.includes(v)){$(`#tab-${v}`).hidden=true;$(`#panel-${v}`).hidden=true;}
    $$('[data-view]').forEach(b=>b.addEventListener('click',()=>{setView(b.dataset.view);if(matchMedia('(max-width:760px)').matches){closeMenu();$('#main').focus({preventScroll:true});}else b.focus();$('#main').scrollIntoView({block:'start'});}));
    $('.report-views').addEventListener('keydown',e=>{if(!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const i=menuViews.indexOf(state.view);const v=e.key==='Home'?menuViews[0]:e.key==='End'?menuViews.at(-1):menuViews[(i+(['ArrowDown','ArrowRight'].includes(e.key)?1:-1)+menuViews.length)%menuViews.length];setView(v);$(`#tab-${v}`).focus();});
    syncMenu();matchMedia('(max-width:760px)').addEventListener('change',()=>{closeMenu();});
    $('#menu-toggle').addEventListener('click',()=>{const open=$('#sidebar').classList.toggle('is-open');$('#scrim').hidden=!open;$('#menu-toggle').setAttribute('aria-expanded',open);syncMenu();if(open)$('#section-nav a').focus();});
    $('#scrim').addEventListener('click',closeMenu);
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&$('#sidebar').classList.contains('is-open')){closeMenu();$('#menu-toggle').focus();}});
    $('#print-report').addEventListener('click',()=>{toast('В окне печати выберите «Сохранить как PDF»');window.print();});
    $('#copy-link').addEventListener('click',()=>{if(location.protocol==='file:'){toast('Это локальный отчёт — передайте получателю HTML-файл');return;}void copyText(location.href,'Ссылка на текущий вид отчёта скопирована','Ссылка на отчёт','Скопируйте адрес отчёта.','Адрес отчёта');});
    $('#copy-max').addEventListener('click',()=>{void copyText('+79099994402','Номер MAX скопирован: +7 909 999-44-02','Связаться в MAX','Скопируйте номер и найдите контакт в MAX.','Номер MAX');});
    let printDetails=[];
    window.addEventListener('beforeprint',()=>{printDetails=$$('details').map(d=>[d,d.open]);$$('details:not(.chart-data)').forEach(d=>d.open=true);});
    window.addEventListener('afterprint',()=>{printDetails.forEach(([d,open])=>{d.open=open;});});
  }
  try { bind();if(allowedViews.includes('statistics'))statsView();if(allowedViews.includes('setup'))setupView();setView(state.view); }
  catch(error){$('#load-error').hidden=false;console.error('Report render failed',error);}
})();
