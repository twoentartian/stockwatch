/* StockWatch 前端：纯原生 JS，无外部依赖。 */
'use strict';

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 30000;   // 页面向后端要一次最新状态的间隔
let stateTimer = null;
let lastState = null;
let quoteCurrency = {};   // 代码 -> 报价币种，记流水时自动带出

/* ------------------------------------------------------------------ 工具 */

async function api(path, options) {
  const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options));
  let data = {};
  try { data = await res.json(); } catch (e) { /* 空响应 */ }
  if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
  return data;
}

const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

function toast(text, bad) {
  const el = $('toast');
  el.textContent = text;
  el.classList.toggle('bad', !!bad);
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 3200);
}

const fmtNum = (v, digits = 2) =>
  (v === null || v === undefined || Number.isNaN(v)) ? '-' :
    Number(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });

const fmtPct = (v) => (v === null || v === undefined) ? '-' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;

const dirClass = (v) => (v === null || v === undefined || Math.abs(v) < 0.005) ? 'flat' : (v > 0 ? 'up' : 'down');

function fmtTime(ts) {
  if (!ts) return '--';
  return new Date(ts * 1000).toLocaleString(dateLocale(), { hour12: false });
}

function fmtClock(ts) {
  if (!ts) return '--';
  return new Date(ts * 1000).toLocaleTimeString(dateLocale(), { hour12: false });
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ------------------------------------------------------------------ 标签页 */

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((el) => el.classList.remove('active'));
    document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    $('panel-' + tab.dataset.tab).classList.add('active');
  });
});

/* ------------------------------------------------------------------ 状态渲染 */

async function loadState() {
  try {
    const state = await api('/api/state');
    lastState = state;
    renderStatus(state);
    renderIndexCards(state.indices);
    renderWatchCards(state.watchlist);
    renderWatchTable(state.watchlist);
    fillSymbolSelect(state.watchlist);
    quoteCurrency = {};
    state.watchlist.forEach((w) => { if (w.quote) quoteCurrency[w.symbol] = w.quote.currency; });
    renderAlerts(state.alerts);
  } catch (e) {
    console.error(e);
  }
}

function renderStatus(s) {
  const badge = $('market-badge');
  badge.textContent = t(s.market_open ? 'top.marketOpen' : 'top.marketClosed');
  badge.classList.toggle('open', !!s.market_open);
  $('last-poll').textContent = t('top.lastPoll', { time: fmtTime(s.last_poll) });
  $('next-poll').textContent =
    t('top.nextPoll', { time: fmtClock(s.next_run), n: s.poll_interval_minutes }) +
    t(s.email.enabled ? 'top.mailOn' : 'top.mailOff');
}

function quoteCard(symbol, title, quote, error, removable) {
  const box = document.createElement('div');
  box.className = 'quote-card' + (error ? ' err' : '');
  if (error) {
    box.innerHTML =
      `<div class="sym">${escapeHtml(symbol)}</div>` +
      `<div class="name">${escapeHtml(title)}</div>` +
      `<div class="price">${t('ov.fetchFailed')}</div>` +
      `<div class="meta">${escapeHtml(error)}</div>`;
  } else if (!quote) {
    box.innerHTML =
      `<div class="sym">${escapeHtml(symbol)}</div>` +
      `<div class="name">${escapeHtml(title)}</div>` +
      `<div class="price">--</div><div class="meta">${t('ov.waitingFirst')}</div>`;
  } else {
    const pct = quote.baseline_change_pct;
    const cls = dirClass(pct);
    box.innerHTML =
      `<div class="sym">${escapeHtml(symbol)}</div>` +
      `<div class="name">${escapeHtml(title || quote.name || '')}</div>` +
      `<div class="price ${cls}">${fmtNum(quote.price)}</div>` +
      `<div class="chg ${cls}">${fmtNum(quote.change)} (${fmtPct(pct)})</div>` +
      `<div class="meta">${t('ov.prevClose')} ${fmtNum(quote.prev_close)} · ${t('ov.open')} ${fmtNum(quote.open)} · ` +
      `${escapeHtml(quote.currency || '')} · ${fmtClock(quote.market_time)}</div>`;
  }
  if (removable) {
    const x = document.createElement('button');
    x.className = 'x';
    x.textContent = '×';
    x.title = t('ov.remove');
    x.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      await post(removable, { symbol });
      loadState();
    });
    box.appendChild(x);
  }
  box.addEventListener('click', () => openChart(symbol, title));
  return box;
}

function renderIndexCards(indices) {
  const wrap = $('index-cards');
  wrap.innerHTML = '';
  indices.forEach((it) => {
    const title = indexTitle(it.symbol, it.alias, it.quote && it.quote.name);
    wrap.appendChild(quoteCard(it.symbol, title, it.quote, it.error, '/api/indices/remove'));
  });
}

function renderWatchCards(watchlist) {
  const wrap = $('watch-cards');
  wrap.innerHTML = '';
  if (!watchlist.length) {
    wrap.innerHTML = `<p class="hint">${t('ov.emptyWatch')}</p>`;
    return;
  }
  watchlist.forEach((it) => {
    wrap.appendChild(quoteCard(it.symbol, it.name, it.quote, it.error, null));
  });
}

function renderWatchTable(watchlist) {
  const tbody = $('watch-tbody');
  // 正在编辑的输入框不要被刷新覆盖
  if (tbody.contains(document.activeElement)) return;
  tbody.innerHTML = '';

  watchlist.forEach((it) => {
    const tr = document.createElement('tr');
    const pct = it.quote ? it.quote.baseline_change_pct : null;
    tr.innerHTML =
      `<td><input type="checkbox" data-f="enabled" ${it.enabled ? 'checked' : ''}></td>` +
      `<td><a class="sym-link" href="/stock/${encodeURIComponent(it.symbol)}" target="_blank"` +
      ` title="${t('watch.btn.fundamentals')}"><b>${escapeHtml(it.symbol)}</b></a>` +
      `<br><span class="s-meta">${escapeHtml(it.name || '')}</span></td>` +
      `<td class="right">${it.quote ? fmtNum(it.quote.price) : (it.error ? t('ov.fetchFailed') : '--')}</td>` +
      `<td class="right ${dirClass(pct)}">${fmtPct(pct)}</td>` +
      `<td class="right"><input class="num" type="number" step="0.1" min="0" data-f="up_pct" value="${it.up_pct}"></td>` +
      `<td class="right"><input class="num" type="number" step="0.1" min="0" data-f="down_pct" value="${it.down_pct}"></td>` +
      `<td class="right"><input class="num" type="number" step="5" min="0" data-f="cooldown_minutes" value="${it.cooldown_minutes}"></td>` +
      `<td><input type="checkbox" data-f="once_per_day" ${it.once_per_day ? 'checked' : ''}></td>` +
      `<td><input class="note" type="text" data-f="note" value="${escapeHtml(it.note || '')}"></td>` +
      `<td class="nowrap"><button type="button" class="btn small" data-act="pos">${t('watch.btn.position')}</button> ` +
      fundLink(it.symbol) + `</td>` +
      `<td><button type="button" class="btn small danger" data-act="del">${t('watch.btn.delete')}</button></td>`;

    tr.querySelectorAll('[data-f]').forEach((input) => {
      input.addEventListener('change', async () => {
        const field = input.dataset.f;
        const value = input.type === 'checkbox' ? input.checked
          : (input.type === 'number' ? Number(input.value) : input.value);
        const res = await post('/api/watchlist/update', { symbol: it.symbol, [field]: value });
        if (res.ok) toast(t('watch.updated', { symbol: it.symbol }));
        else toast(res.error || t('watch.updateFailed'), true);
        loadState();
      });
    });

    tr.querySelector('[data-act=pos]').addEventListener('click', () => openHoldings(it.symbol));

    tr.querySelector('[data-act=del]').addEventListener('click', async () => {
      if (!confirm(t('watch.confirmDelete', { symbol: it.symbol }))) return;
      await post('/api/watchlist/remove', { symbol: it.symbol });
      toast(t('ov.removed', { symbol: it.symbol }));
      loadState();
    });

    tbody.appendChild(tr);
  });

  if (!watchlist.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="hint">${t('watch.empty')}</td></tr>`;
  }
}

function renderAlerts(list) {
  const tbody = $('alert-tbody');
  tbody.innerHTML = '';
  if (!list || !list.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="hint">${t('hist.empty')}</td></tr>`;
    return;
  }
  const mailText = { sent: t('hist.mailOk'), failed: t('hist.mailFail'), skipped: t('hist.mailSkip') };
  list.forEach((a) => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${fmtTime(a.ts)}</td>` +
      `<td><b>${escapeHtml(a.symbol)}</b> <span class="s-meta">${escapeHtml(a.name || '')}</span></td>` +
      `<td class="${a.direction === 'down' ? 'down' : 'up'}">` +
      `${{ up: t('hist.upLabel'), down: t('hist.downLabel'), milestone: t('hist.milestone') }[a.direction] || a.direction}</td>` +
      `<td class="right ${dirClass(a.change_pct)}">${fmtPct(a.change_pct)}</td>` +
      `<td class="right">${fmtNum(a.threshold)}%</td>` +
      `<td class="right">${fmtNum(a.price)}</td>` +
      `<td>${mailText[a.email_status] || escapeHtml(a.email_status || '')}</td>` +
      `<td class="s-meta">${escapeHtml(a.detail || '')}</td>`;
    tbody.appendChild(tr);
  });
}

/* ------------------------------------------------------------------ 添加股票 / 指数 */

let suggestTimer = null;

$('watch-symbol').addEventListener('input', (ev) => {
  const q = ev.target.value.trim();
  clearTimeout(suggestTimer);
  if (q.length < 1) { $('suggest-box').hidden = true; return; }
  suggestTimer = setTimeout(async () => {
    const data = await api('/api/search?q=' + encodeURIComponent(q));
    const box = $('suggest-box');
    const results = data.results || [];
    if (!results.length) { box.hidden = true; return; }
    box.innerHTML = '';
    results.forEach((r) => {
      const row = document.createElement('div');
      row.innerHTML = `<span class="s-sym">${escapeHtml(r.symbol)}</span>` +
        `<span>${escapeHtml(r.name)}</span><br>` +
        `<span class="s-meta">${escapeHtml(r.exchange)} · ${escapeHtml(r.type)}</span>`;
      row.addEventListener('click', () => {
        $('watch-symbol').value = r.symbol;
        $('watch-symbol').dataset.name = r.name || '';
        box.hidden = true;
      });
      box.appendChild(row);
    });
    box.hidden = false;
  }, 260);
});

document.addEventListener('click', (ev) => {
  if (!ev.target.closest('.search-wrap')) $('suggest-box').hidden = true;
});

$('watch-add-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const symbol = $('watch-symbol').value.trim();
  if (!symbol) return;
  const res = await post('/api/watchlist/add', {
    symbol,
    name: $('watch-symbol').dataset.name || '',
    up_pct: Number($('watch-up').value),
    down_pct: Number($('watch-down').value),
  });
  if (res.ok) {
    toast(t('watch.added', { symbol: res.item.symbol }));
    $('watch-symbol').value = '';
    $('watch-symbol').dataset.name = '';
    loadState();
  } else {
    toast(res.error || t('watch.addFailed'), true);
  }
});

$('index-add-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const symbol = $('index-symbol').value.trim();
  if (!symbol) return;
  const res = await post('/api/indices/add', { symbol, name: $('index-alias').value.trim() });
  if (res.ok) {
    toast(t('watch.added', { symbol }));
    $('index-symbol').value = '';
    $('index-alias').value = '';
    loadState();
  } else {
    toast(res.error || t('watch.addFailed'), true);
  }
});

$('btn-refresh').addEventListener('click', async () => {
  const btn = $('btn-refresh');
  btn.disabled = true;
  btn.textContent = t('top.refreshing');
  const res = await post('/api/refresh');
  btn.disabled = false;
  btn.textContent = t('top.refresh');
  if (res.skipped) toast(res.error || t('top.pollBusy'), true);
  else toast(t('top.polled', { n: res.fetched }) +
             (res.triggered ? t('top.polledAlerts', { n: res.triggered }) : ''));
  loadState();
});

$('btn-reset-alerts').addEventListener('click', async () => {
  await post('/api/alerts/reset', {});
  toast(t('watch.dedupReset'));
});

/* ------------------------------------------------------------------ 持仓收益 */

const eur = (v, d = 2) => (v === null || v === undefined) ? '-' :
  Number(v).toLocaleString('de-DE', { minimumFractionDigits: d, maximumFractionDigits: d });

/* 基本面入口用真正的 <a target="_blank">，不要用 window.open：
 * 后者会被当成弹窗拦截，某些浏览器拦截后会退化成在当前标签页跳转。
 * 锚点还顺带支持中键点击、Ctrl+点击、右键「在新窗口打开」。 */
function fundLink(symbol) {
  return `<a class="btn small" href="/stock/${encodeURIComponent(symbol)}"` +
    ` target="_blank" rel="noopener">${t('watch.btn.fundamentals')}</a>`;
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((el) => el.classList.toggle('active', el.dataset.tab === name));
  document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + name));
}

/* 本地日期（toISOString 给的是 UTC，欧洲时区晚上会差一天） */
function localToday() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

/* ---------------- 年/月/日 三段式日期选择 ----------------
 * 原来用 <input type="date">，但它按浏览器语言渲染（en-US 下是 MM/DD/YYYY），
 * 月和日容易看反。三个下拉顺序固定，没有歧义。 */

const FIRST_YEAR = 2000;

function daysInMonth(year, month) {
  return new Date(year, month, 0).getDate();   // month 传 1-12，第 0 天即上月末
}

function syncDayOptions() {
  const year = Number($('tx-year').value);
  const month = Number($('tx-month').value);
  const total = daysInMonth(year, month);
  const keep = Number($('tx-day').value) || 1;
  $('tx-day').innerHTML = Array.from({ length: total },
    (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join('');
  $('tx-day').value = String(Math.min(keep, total));   // 2月选31日时自动收到28/29
}

function initDatePicker() {
  const thisYear = new Date().getFullYear();
  const years = [];
  for (let y = thisYear; y >= FIRST_YEAR; y--) years.push(y);
  $('tx-year').innerHTML = years.map((y) => `<option value="${y}">${y}</option>`).join('');
  $('tx-month').innerHTML = Array.from({ length: 12 },
    (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join('');
  syncDayOptions();
  $('tx-year').addEventListener('change', syncDayOptions);
  $('tx-month').addEventListener('change', syncDayOptions);
  $('tx-today').addEventListener('click', () => setTxDate(localToday()));
}

function getTxDate() {
  const y = $('tx-year').value, m = $('tx-month').value, d = $('tx-day').value;
  if (!y || !m || !d) return '';
  return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function setTxDate(iso) {
  const [y, m, d] = String(iso || '').split('-').map(Number);
  if (!y || !m || !d) return;
  $('tx-year').value = String(y);
  $('tx-month').value = String(m);
  syncDayOptions();
  $('tx-day').value = String(d);
}

let accounts = [];

function openHoldings(symbol, accountId) {
  switchTab('holdings');
  if (symbol) setSelect($('tx-symbol'), symbol);
  if (accountId) setSelect($('tx-account'), String(accountId));
  $('tx-shares').focus();
  loadPortfolio();
}

/* 选项还没渲染出来时先记下来，等填充完再应用 */
function setSelect(select, value) {
  select.value = value;
  if (select.value !== value) select.dataset.pending = value;
}

function fillSelect(select, options, keepValue) {
  const wanted = keepValue ? (select.dataset.pending || select.value) : '';
  select.innerHTML = options.map((o) =>
    `<option value="${escapeHtml(o.value)}">${escapeHtml(o.label)}</option>`).join('');
  if (wanted) {
    select.value = wanted;
    if (select.value === wanted) delete select.dataset.pending;
  }
}

function fillAccountSelects() {
  const options = accounts.map((a) => ({ value: String(a.id), label: a.name }));
  fillSelect($('tx-account'), options, true);
  const filter = $('pf-account');
  const current = filter.value;
  filter.innerHTML = `<option value="">${t('pf.allAccountsFilter')}</option>` +
    options.map((o) => `<option value="${o.value}">${escapeHtml(o.label)}</option>`).join('');
  filter.value = current;
}

/* 代码只能从追踪列表里选，从根上杜绝写错代码（比如 INL 写成了 INL.DE 之外的东西） */
function fillSymbolSelect(watchlist) {
  const options = watchlist.map((w) => ({
    value: w.symbol,
    label: w.name ? `${w.symbol} — ${w.name}` : w.symbol,
  }));
  const fallback = [{ value: '', label: t('llm.emptyWatch') }];
  fillSelect($('tx-symbol'), options.length ? options : fallback, true);
  fillSelect($('llm-symbol'), options.length ? options : fallback, true);
}

async function loadPortfolio() {
  const accountId = $('pf-account').value;
  const query = accountId ? `?account_id=${accountId}` : '';
  const [pf, txs, all] = await Promise.all([
    api('/api/portfolio' + query),
    api('/api/transactions' + query),
    accountId ? api('/api/portfolio') : null,
  ]);

  accounts = pf.accounts || [];
  fillAccountSelects();

  renderPortfolioKpis(pf.totals, pf.fx);
  renderPositions(pf.positions, !accountId);
  renderTransactions(txs.transactions || []);
  renderAccounts((all || pf).positions || []);

  // 导出链接跟着账户筛选走：页面上显示哪个账户，导出的就是哪个账户
  const label = accountId
    ? (accounts.find((a) => String(a.id) === accountId) || {}).name
    : t('pf.allAccountsFilter');
  $('btn-export').href = '/api/portfolio/export.pdf' + query;
  $('btn-export').title = t('pf.exportTitle', { name: label || '' });
}

function renderPortfolioKpis(totals, rates) {
  const cls = (v) => dirClass(v);
  const missing = (totals.missing_quotes || []).length;
  const money = (v) => (v === null || v === undefined) ? t('pf.incomplete') : `€ ${eur(v)}`;

  $('pf-kpis').innerHTML = [
    [t('pf.kpi.invested'), `€ ${eur(totals.invested_eur)}`,
      t('pf.kpi.positionsSub', { n: totals.positions, fees: eur(totals.fees_eur) }), ''],
    [t('pf.kpi.value'), money(totals.market_value_eur),
      missing ? t('pf.kpi.missingSub', { n: missing })
              : (totals.proceeds_eur ? t('pf.kpi.proceedsSub', { v: eur(totals.proceeds_eur) }) : ''), ''],
    [t('pf.kpi.divTotal'), `€ ${eur(totals.dividends_eur)}`, t('pf.kpi.divSub'), ''],
    [t('pf.kpi.profit'), money(totals.total_return_eur),
      totals.return_pct === null || totals.return_pct === undefined ? ''
        : t('pf.kpi.retSub', { pct: fmtPct(totals.return_pct) }),
      cls(totals.total_return_eur)],
    [t('pf.kpi.portfolioAnnual'),
      (totals.annualized_pct === null || totals.annualized_pct === undefined) ? '-' : fmtPct(totals.annualized_pct),
      t('pf.kpi.xirrSub'), cls(totals.annualized_pct)],
  ].map(([label, value, sub, c]) =>
    `<div class="kpi"><div class="kpi-label">${label}</div>` +
    `<div class="kpi-value ${c}">${value}</div><div class="kpi-sub">${sub}</div></div>`).join('');

  const warn = $('pf-warning');
  if (missing) {
    warn.className = 'msg bad';
    warn.textContent = t('pf.warnMissing', { symbols: totals.missing_quotes.join('、') });
  } else {
    warn.textContent = '';
  }

  const usd = rates && rates.USD;
  $('fx-note').textContent = usd
    ? t('pf.fxLine', { rate: usd.eur_per_unit.toFixed(4), time: fmtClock(usd.ts) })
    : '';
}

/* 账户页：把持仓按账户汇总 */
function renderAccounts(positions) {
  const byAccount = new Map();
  accounts.forEach((a) => byAccount.set(a.id, {
    account: a, invested: 0, market: 0, dividends: 0, fees: 0, priced: true, count: 0,
  }));
  positions.forEach((p) => {
    const row = byAccount.get(p.account_id);
    if (!row) return;
    row.invested += p.invested_eur;
    row.dividends += p.dividends_eur;
    row.fees += p.fees_eur;
    row.count += 1;
    if (p.has_quote) row.market += p.market_value_eur;
    else row.priced = false;
  });

  const rows = [...byAccount.values()];
  $('acct-cards').innerHTML = rows.map((r) => {
    const ret = r.priced ? r.market + r.dividends - r.invested : null;
    return `<div class="kpi"><div class="kpi-label">${escapeHtml(r.account.name)}</div>` +
      `<div class="kpi-value ${dirClass(ret)}">${ret === null ? t('pf.incomplete') : '€ ' + eur(ret)}</div>` +
      `<div class="kpi-sub">${t('acct.kpiSub', { inv: eur(r.invested), mv: eur(r.market) })}</div></div>`;
  }).join('') || `<p class="hint">${t('acct.emptyCards')}</p>`;

  const tbody = $('acct-tbody');
  tbody.innerHTML = '';
  rows.forEach((r) => {
    const a = r.account;
    const ret = r.priced ? r.market + r.dividends - r.invested : null;
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td><input type="text" class="note" data-f="name" value="${escapeHtml(a.name)}"></td>` +
      `<td><input type="text" class="note" data-f="note" value="${escapeHtml(a.note || '')}"></td>` +
      `<td class="right">${a.tx_count}</td>` +
      `<td class="right">${eur(r.invested)}</td>` +
      `<td class="right">${r.priced ? eur(r.market) : '-'}</td>` +
      `<td class="right">${eur(r.dividends)}</td>` +
      `<td class="right ${dirClass(ret)}">${ret === null ? '-' : eur(ret)}</td>` +
      `<td><button class="btn small danger" data-act="del"` +
      `${a.tx_count ? ` disabled title="${t('acct.cantDelete')}"` : ''}>${t('acct.delete')}</button></td>`;

    tr.querySelectorAll('[data-f]').forEach((input) => {
      input.addEventListener('change', async () => {
        const res = await post('/api/accounts/update', {
          id: a.id,
          name: tr.querySelector('[data-f=name]').value.trim(),
          note: tr.querySelector('[data-f=note]').value.trim(),
        });
        toast(res.ok ? t('acct.updated') : (res.error || t('watch.updateFailed')), !res.ok);
        loadPortfolio();
      });
    });
    tr.querySelector('[data-act=del]').addEventListener('click', async () => {
      if (!confirm(t('acct.confirmDelete', { name: a.name }))) return;
      const res = await post('/api/accounts/remove', { id: a.id });
      toast(res.ok ? t('tx.deleted') : (res.error || t('tx.deleteFailed')), !res.ok);
      loadPortfolio();
    });
    tbody.appendChild(tr);
  });
}

$('acct-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const name = $('acct-name').value.trim();
  if (!name) return;
  const res = await post('/api/accounts/add', { name, note: $('acct-note').value.trim() });
  if (!res.ok) { toast(res.error || t('acct.addFailed'), true); return; }
  toast(t('acct.added', { name }));
  $('acct-name').value = '';
  $('acct-note').value = '';
  loadPortfolio();
});

$('pf-account').addEventListener('change', loadPortfolio);

function renderPositions(positions) {
  const tbody = $('pos-tbody');
  if (!positions.length) {
    tbody.innerHTML = `<tr><td colspan="13" class="hint">${t('pf.empty')}</td></tr>`;
    return;
  }
  tbody.innerHTML = '';
  positions.forEach((p) => {
    const tr = document.createElement('tr');
    const ann = (p.annualized_pct === null || p.annualized_pct === undefined) ? '-' :
      `${fmtPct(p.annualized_pct)}${p.annualized_reliable ? '' : ` <span class="s-meta" title="${t('pf.shortHoldTitle')}">?</span>`}`;
    const priceCell = p.has_quote
      ? `${fmtNum(p.price)} <span class="s-meta">${escapeHtml(p.currency)}</span>`
      : `<span class="bad-cell" title="${t('pf.noQuoteTitle')}">${t('pf.noQuote')}</span>`;
    const money = (v) => (v === null || v === undefined) ? '-' : eur(v);

    tr.innerHTML =
      `<td>${escapeHtml(p.account || '')}</td>` +
      `<td><a class="sym-link" href="/stock/${encodeURIComponent(p.symbol)}" target="_blank"><b>${escapeHtml(p.symbol)}</b></a>` +
      `<br><span class="s-meta">${escapeHtml(p.name || '')}</span></td>` +
      `<td class="right">${p.shares_held}</td>` +
      `<td class="right">${priceCell}</td>` +
      `<td class="right">${money(p.market_value_eur)}</td>` +
      `<td class="right">${eur(p.invested_eur)}</td>` +
      `<td class="right">${eur(p.dividends_eur)}</td>` +
      `<td class="right">${eur(p.breakeven_eur, p.breakeven_eur !== null && Math.abs(p.breakeven_eur) < 1 ? 4 : 2)}</td>` +
      `<td class="right ${dirClass(p.total_return_eur)}">${money(p.total_return_eur)}</td>` +
      `<td class="right ${dirClass(p.return_pct)}">${p.return_pct === null || p.return_pct === undefined ? '-' : fmtPct(p.return_pct)}</td>` +
      `<td class="right ${dirClass(p.annualized_pct)}">${ann}</td>` +
      `<td class="right">${p.holding_days ?? '-'}${p.closed ? ` <span class="s-meta" title="${t('pf.closedTitle')}">${t('pf.closed')}</span>` : ''}</td>` +
      `<td class="nowrap"><button type="button" class="btn small" data-act="add">${t('tx.add')}</button> ` +
      fundLink(p.symbol) + `</td>`;
    tr.querySelector('[data-act=add]').addEventListener('click', () => openHoldings(p.symbol, p.account_id));
    tbody.appendChild(tr);
  });
}

const txLabel = (kind) => t('tx.kindShort.' + kind) || kind;

/* 核对结果的小徽章，鼠标悬停能看到具体比对了什么 */
function checkBadge(tx) {
  const badge = {
    ok: [t('tx.checkOk'), 'down'],
    warn: [t('tx.checkWarn'), 'up'],
    bad: [t('tx.checkBad'), 'up'],
    skip: [t('tx.checkNone'), 's-meta'],
  }[tx.check_status] || [t('tx.checkNone'), 's-meta'];
  const when = tx.checked_at ? ` (${fmtTime(tx.checked_at)})` : '';
  return `<span class="${badge[1]}" title="${escapeHtml((tx.check_detail || t('tx.checkNever')) + when)}">${badge[0]}</span>`;
}

async function runVerify(recheck) {
  const msg = $('verify-msg');
  msg.className = 'msg';
  msg.textContent = t('tx.verifying');
  const res = await post('/api/transactions/verify', { recheck });
  if (!res.ok) { msg.className = 'msg bad'; msg.textContent = res.error || t('tx.verifyFailed'); return; }

  const s = res.summary || {};
  const parts = [t('tx.verifyCount', { n: res.checked })];
  if (s.ok) parts.push(t('tx.verifyOk', { n: s.ok }));
  if (s.warn) parts.push(t('tx.verifyWarn', { n: s.warn }));
  if (s.bad) parts.push(t('tx.verifyBad', { n: s.bad }));
  if (s.skip) parts.push(t('tx.verifySkip', { n: s.skip }));
  msg.className = 'msg ' + ((s.bad || s.warn) ? 'bad' : 'ok');
  msg.textContent = parts.join(' · ');

  (res.problems || []).forEach((p) => {
    console.warn(`[核对] ${p.trade_date} ${p.symbol}: ${p.detail}`);
  });
  loadPortfolio();
}

$('btn-verify').addEventListener('click', () => runVerify(false));
$('btn-reverify').addEventListener('click', () => runVerify(true));

function renderTransactions(list) {
  const tbody = $('tx-tbody');
  // 正在编辑备注时别重绘，否则定时刷新会把没保存的字冲掉
  if (tbody.contains(document.activeElement)) return;
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="hint">${t('tx.emptyRows')}</td></tr>`;
    return;
  }
  tbody.innerHTML = '';
  list.slice().reverse().forEach((tx) => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${escapeHtml(tx.trade_date)}</td>` +
      `<td>${escapeHtml(tx.account_name || '')}</td>` +
      `<td><b>${escapeHtml(tx.symbol)}</b></td>` +
      `<td class="${tx.kind === 'SELL' ? 'down' : ''}">${txLabel(tx.kind)}</td>` +
      `<td class="right">${tx.shares ?? '-'}</td>` +
      `<td class="right">${tx.price === null ? '-' : fmtNum(tx.price)} <span class="s-meta">${escapeHtml(tx.price_currency || '')}</span></td>` +
      `<td class="right">${eur(tx.fee_eur)}</td>` +
      `<td class="right">${eur(tx.amount_eur)}</td>` +
      `<td>${checkBadge(tx)}</td>` +
      `<td><input class="note" type="text" data-act="note" placeholder="${t('tx.notePh2')}"` +
      ` value="${escapeHtml(tx.note || '')}"></td>` +
      `<td><button type="button" class="btn small danger" data-act="del">${t('acct.delete')}</button></td>`;

    const noteInput = tr.querySelector('[data-act=note]');
    noteInput.addEventListener('change', async () => {
      const res = await post('/api/transactions/note', { id: tx.id, note: noteInput.value });
      toast(res.ok ? t('tx.noteSaved') : (res.error || t('tx.saveFailed')), !res.ok);
    });
    // 回车直接提交，不用点到别处去
    noteInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') noteInput.blur();
    });

    tr.querySelector('[data-act=del]').addEventListener('click', async () => {
      if (!confirm(t('tx.confirmDeleteRow', { date: tx.trade_date, symbol: tx.symbol, kind: txLabel(tx.kind) }))) return;
      const res = await post('/api/transactions/remove', { id: tx.id });
      toast(res.ok ? t('tx.deleted') : (res.error || t('tx.deleteFailed')), !res.ok);
      loadPortfolio();
    });
    tbody.appendChild(tr);
  });
}

/* 类型切换时，股息不需要股数和单价 */
function syncTxKindUI() {
  const isDiv = $('tx-kind').value === 'DIV';
  ['tx-shares', 'tx-price', 'tx-currency'].forEach((id) => { $(id).disabled = isDiv; });
  $('tx-amount').placeholder = t(isDiv ? 'tx.amountPhDiv' : 'tx.amountPh');
}
$('tx-kind').addEventListener('change', syncTxKindUI);

/* 欧元计价的交易可以直接算出实付金额，省一次手填。
 * 外币不能这么算——股数 × 美元单价不是欧元，必须以 DeGiro 账单上实际扣的欧元为准。 */
function syncTxAmount() {
  const amount = $('tx-amount');
  if (amount.dataset.touched) return;          // 手填过就不再动它
  if ($('tx-currency').value !== 'EUR') {
    // 从 EUR 切到外币时，之前自动算的那个数就不成立了，必须清掉——
    // 留在框里会被当成实付金额存进去，正好造成"忘了换汇"那类错误。
    if (amount.dataset.autofilled) {
      amount.value = '';
      delete amount.dataset.autofilled;
    }
    return;
  }
  const shares = Number($('tx-shares').value), price = Number($('tx-price').value);
  const fee = Number($('tx-fee').value) || 0;
  if (shares > 0 && price > 0) {
    const sign = $('tx-kind').value === 'SELL' ? -1 : 1;
    amount.value = (shares * price + sign * fee).toFixed(2);
    amount.dataset.autofilled = '1';
  }
}

['tx-shares', 'tx-price', 'tx-fee', 'tx-currency', 'tx-kind'].forEach((id) => {
  $(id).addEventListener('input', syncTxAmount);
  $(id).addEventListener('change', syncTxAmount);
});
$('tx-amount').addEventListener('input', () => { $('tx-amount').dataset.touched = '1'; });

/* 选了代码就把币种带出来，省得 XETRA 上市的票误填成 USD */
$('tx-symbol').addEventListener('change', () => {
  const currency = quoteCurrency[$('tx-symbol').value];
  if (currency && [...$('tx-currency').options].some((o) => o.value === currency)) {
    $('tx-currency').value = currency;
  }
});

$('tx-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const tradeDate = getTxDate();
  // 未来日期基本都是手滑（比如把 06-12 打成 12-06），先拦一下
  if (tradeDate > localToday() &&
      !confirm(t('tx.futureDate', { date: tradeDate }))) return;

  const body = {
    symbol: $('tx-symbol').value.trim(),
    account_id: $('tx-account').value,
    kind: $('tx-kind').value,
    trade_date: tradeDate,
    shares: $('tx-shares').value,
    price: $('tx-price').value,
    price_currency: $('tx-currency').value,
    fee_eur: $('tx-fee').value,
    amount_eur: $('tx-amount').value,
    note: $('tx-note').value,
  };
  const res = await post('/api/transactions/add', body);
  if (!res.ok) { toast(res.error || t('tx.addFailed'), true); return; }
  toast(t('tx.added'));
  ['tx-shares', 'tx-price', 'tx-amount', 'tx-note'].forEach((id) => { $(id).value = ''; });
  delete $('tx-amount').dataset.touched;
  delete $('tx-amount').dataset.autofilled;
  loadPortfolio();
  loadState();
});

/* ------------------------------------------------------------------ 年报点评 */

/* 只渲染模型会用到的那几种 Markdown，其余一律转义：
 * 模型输出是不可信内容，不能直接塞 innerHTML。 */
function miniMarkdown(text) {
  const lines = escapeHtml(text).split('\n');
  const out = [];
  let inList = false;
  const bold = (s) => s.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^#{1,6}\s/.test(line)) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<h4>${bold(line.replace(/^#{1,6}\s*/, ''))}</h4>`);
    } else if (/^[-*]\s/.test(line.trimStart())) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${bold(line.trimStart().slice(2))}</li>`);
    } else if (!line.trim()) {
      if (inList) { out.push('</ul>'); inList = false; }
    } else {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<p>${bold(line)}</p>`);
    }
  }
  if (inList) out.push('</ul>');
  return out.join('');
}

function periodLabel(r) {
  return (r.period_type === 'quarterly')
    ? t('rep.periodQuarter', { date: r.period_key || r.as_of })
    : t('rep.periodAnnual', { year: r.period_key || r.fiscal_year });
}

async function loadReports() {
  const data = await api('/api/insights');
  const box = $('rep-list');
  const rows = data.summaries || [];
  if (!rows.length) {
    box.innerHTML = `<p class="hint">${t('rep.empty')}</p>`;
    return;
  }
  box.innerHTML = '';
  rows.forEach((r) => {
    const tokens = (r.prompt_tokens || 0) + (r.completion_tokens || 0);
    const card = document.createElement('section');
    card.className = 'card-box insight';
    card.innerHTML =
      `<div class="row">` +
      `<h2 style="margin:0"><a class="sym-link" href="/stock/${encodeURIComponent(r.symbol)}"` +
      ` target="_blank" rel="noopener">${escapeHtml(r.symbol)}</a> ` +
      `<span class="badge-period">${t(r.period_type === 'quarterly' ? 'rep.quarterly' : 'rep.annual')}</span> ` +
      `· ${periodLabel(r)}</h2>` +
      `<span class="s-meta">${escapeHtml(r.model || '')} · ${fmtTime(r.created_at)}` +
      `${tokens ? ' · ' + tokens + ' tokens' : ''}</span>` +
      `<span style="flex:1"></span>` +
      `<button type="button" class="btn small" data-act="regen">${t('rep.regen')}</button>` +
      `<button type="button" class="btn small" data-act="mail">${t('rep.email')}</button>` +
      `</div><div class="insight-body">${miniMarkdown(r.summary || '')}</div>`;

    // 就地重生成这一期：改了提示词或想让它再联网查一次时最常用
    card.querySelector('[data-act=regen]').addEventListener('click', async (ev) => {
      const btn = ev.target;
      btn.disabled = true;
      btn.textContent = t('rep.generating');
      const res = await post('/api/insights/generate',
        { symbol: r.symbol, period_type: r.period_type, force: true });
      btn.disabled = false;
      btn.textContent = t('rep.regen');
      if (!res.ok) { toast(res.error || t('rep.genFailed'), true); return; }
      // 出网的查询词每次都摆出来，能核
      const queries = (res.web_queries || []).length
        ? t('rep.webQueries', { queries: res.web_queries.map((q) => `“${q}”`).join('、') }) : '';
      toast(t('rep.regenerated', { symbol: r.symbol, period: periodLabel(res) }) + queries, false);
      loadReports();
    });

    card.querySelector('[data-act=mail]').addEventListener('click', async (ev) => {
      const btn = ev.target;
      btn.disabled = true;
      const res = await post('/api/insights/email',
        { symbol: r.symbol, period_type: r.period_type, period_key: r.period_key });
      btn.disabled = false;
      toast(res.message || t(res.ok ? 'rep.sent' : 'rep.sendFailed'), !res.ok);
    });
    box.appendChild(card);
  });
}

$('btn-rep-email').addEventListener('click', async () => {
  showMsg('msg-rep', t('set.sending'), false);
  const res = await post('/api/insights/email', { limit: 5 });
  showMsg('msg-rep', res.message || t(res.ok ? 'rep.sent' : 'rep.sendFailed'), !res.ok);
});

/* ------------------------------------------------------------------ 设置 */

async function loadConfig() {
  const cfg = await api('/api/config');
  $('cfg-interval').value = cfg.poll_interval_minutes;
  $('cfg-baseline').value = cfg.alert.baseline;
  $('cfg-market-only').checked = !!cfg.alert.market_hours_only;
  $('cfg-position-return').value = cfg.alert.position_return_pct ?? 100;
  $('cfg-keep-days').value = cfg.history_keep_days;

  const l = cfg.llm || {};
  $('llm-enabled').checked = !!l.enabled;
  $('llm-auto').checked = !!l.auto_summary;
  $('llm-position').checked = !!l.include_position;
  $('llm-email').checked = !!l.email_summary;
  $('llm-quarterly').checked = !!l.include_quarterly;
  $('llm-tools').checked = !!l.use_tools;
  $('llm-base').value = l.base_url || '';
  $('llm-model').value = l.model || '';
  $('llm-key').value = l.api_key || '';      // 占位符或空，明文永远拿不到
  $('llm-temp').value = l.temperature;
  $('llm-maxtok').value = l.max_tokens;
  $('llm-timeout').value = l.timeout_s;
  $('llm-interval').value = l.check_interval_hours;

  const w = cfg.websearch || {};
  $('web-enabled').checked = !!w.enabled;
  $('web-key').value = w.api_key || '';      // 占位符或空
  $('web-results').value = w.max_results;
  $('web-searches').value = w.max_searches;
  $('web-depth').value = w.search_depth || 'basic';
  $('web-days').value = w.days;
  $('web-domains').value = (w.include_domains || []).join('\n');

  const m = cfg.email;
  $('mail-enabled').checked = !!m.enabled;
  $('mail-host').value = m.smtp_host || '';
  $('mail-port').value = m.smtp_port || 465;
  $('mail-security').value = m.security || 'ssl';
  $('mail-username').value = m.username || '';
  $('mail-password').value = m.password || '';     // 是占位符 __UNCHANGED__ 或空
  $('mail-from').value = m.from_addr || '';
  $('mail-to').value = (m.to_addrs || []).join(', ');
  $('mail-prefix').value = m.subject_prefix || '';
}

function mailFormValues() {
  return {
    enabled: $('mail-enabled').checked,
    smtp_host: $('mail-host').value.trim(),
    smtp_port: Number($('mail-port').value) || 465,
    security: $('mail-security').value,
    username: $('mail-username').value.trim(),
    password: $('mail-password').value,
    from_addr: $('mail-from').value.trim(),
    to_addrs: $('mail-to').value.split(/[,;]/).map((s) => s.trim()).filter(Boolean),
    subject_prefix: $('mail-prefix').value.trim(),
  };
}

function showMsg(id, text, bad) {
  const el = $(id);
  el.textContent = text;
  el.className = 'msg ' + (bad ? 'bad' : 'ok');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.textContent = ''; }, 6000);
}

$('btn-save-general').addEventListener('click', async () => {
  const res = await post('/api/config', {
    poll_interval_minutes: Number($('cfg-interval').value),
    history_keep_days: Number($('cfg-keep-days').value),
    alert: {
      baseline: $('cfg-baseline').value,
      market_hours_only: $('cfg-market-only').checked,
      position_return_pct: Number($('cfg-position-return').value),
    },
  });
  showMsg('msg-general', res.ok ? t('set.saved') : (res.error || t('set.saveFailed')), !res.ok);
  loadState();
});

$('btn-save-mail').addEventListener('click', async () => {
  const res = await post('/api/config', { email: mailFormValues() });
  showMsg('msg-mail', res.ok ? t('set.saved') : (res.error || t('set.saveFailed')), !res.ok);
  if (res.ok) loadConfig();
  loadState();
});

function llmFormValues() {
  return {
    enabled: $('llm-enabled').checked,
    auto_summary: $('llm-auto').checked,
    include_position: $('llm-position').checked,
    email_summary: $('llm-email').checked,
    include_quarterly: $('llm-quarterly').checked,
    use_tools: $('llm-tools').checked,
    base_url: $('llm-base').value.trim(),
    model: $('llm-model').value.trim(),
    api_key: $('llm-key').value,
    temperature: Number($('llm-temp').value),
    max_tokens: Number($('llm-maxtok').value),
    timeout_s: Number($('llm-timeout').value),
    check_interval_hours: Number($('llm-interval').value),
  };
}

$('btn-save-llm').addEventListener('click', async () => {
  const res = await post('/api/config', { llm: llmFormValues() });
  showMsg('msg-llm', res.ok ? t('set.saved') : (res.error || t('set.saveFailed')), !res.ok);
  if (res.ok) loadConfig();
});

$('btn-test-llm').addEventListener('click', async () => {
  showMsg('msg-llm', t('llm.testing'), false);
  const res = await post('/api/llm/test', { llm: llmFormValues() });
  showMsg('msg-llm', res.message || t(res.ok ? 'llm.testOk' : 'llm.testFailed'), !res.ok);
});

function webFormValues() {
  return {
    enabled: $('web-enabled').checked,
    api_key: $('web-key').value,
    max_results: Number($('web-results').value),
    max_searches: Number($('web-searches').value),
    search_depth: $('web-depth').value,
    days: Number($('web-days').value),
    // 后端会做规整（去协议、去 www、去重），这里原样送过去
    include_domains: $('web-domains').value.split('\n').map(s => s.trim()).filter(Boolean),
  };
}

$('btn-save-web').addEventListener('click', async () => {
  const res = await post('/api/config', { websearch: webFormValues() });
  showMsg('msg-web', res.ok ? t('set.saved') : (res.error || t('set.saveFailed')), !res.ok);
  if (res.ok) loadConfig();
});

$('btn-test-web').addEventListener('click', async () => {
  showMsg('msg-web', t('web.testing'), false);
  const res = await post('/api/websearch/test', { websearch: webFormValues() });
  showMsg('msg-web', res.message || t(res.ok ? 'web.ok' : 'web.failed'), !res.ok);
});

/* 手动给某一只生成点评——测试提示词和模型效果时最常用的入口 */
$('btn-gen-one').addEventListener('click', async () => {
  const symbol = $('llm-symbol').value;
  if (!symbol) { showMsg('msg-llm', t('llm.emptyWatch'), true); return; }
  const btn = $('btn-gen-one');
  btn.disabled = true;
  showMsg('msg-llm', t('llm.generating', { symbol }), false);
  const res = await post('/api/insights/generate',
    { symbol, force: $('llm-force').checked, period_type: $('llm-period').value });
  btn.disabled = false;
  if (!res.ok) { showMsg('msg-llm', res.error || t('rep.genFailed'), true); return; }
  const tokens = (res.prompt_tokens || 0) + (res.completion_tokens || 0);
  const used = (res.tool_calls || []).length ? t('llm.toolCalls', { n: res.tool_calls.length }) : '';
  // 联网查询词是唯一出网的内容，每次都摆出来让人能核
  const queries = (res.web_queries || []).length
    ? t('llm.queries', { queries: res.web_queries.map((q) => `“${q}”`).join('、') }) : '';
  showMsg('msg-llm',
    res.cached
      ? t('llm.cached', { symbol, period: periodLabel(res) })
      : t('llm.generated', { symbol, period: periodLabel(res), tokens, tools: used, queries }),
    false);
});

$('btn-check-reports').addEventListener('click', async () => {
  showMsg('msg-llm', t('llm.scanning'), false);
  const res = await post('/api/insights/check', {});
  if (!res.ok) { showMsg('msg-llm', res.error || t('llm.scanFailed'), true); return; }
  if (res.skipped) { showMsg('msg-llm', res.skipped, true); return; }
  const created = (res.created || []).length;
  const failed = Object.keys(res.failed || {}).length;
  showMsg('msg-llm',
    created ? t('llm.scanCreated', { n: created, list: res.created.join('、') }) : t('llm.scanNothing'),
    failed > 0);
});

$('btn-test-mail').addEventListener('click', async () => {
  showMsg('msg-mail', t('set.sending'), false);
  const res = await post('/api/email/test', { email: mailFormValues() });
  showMsg('msg-mail', res.message || t(res.ok ? 'rep.sent' : 'rep.sendFailed'), !res.ok);
});

/* ------------------------------------------------------------------ 数据导出 / 导入 */

function fmtBytes(n) {
  if (!n) return '0 MB';
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`;
}

async function loadDataStats() {
  const res = await api('/api/data/stats');
  if (!res.ok) return;
  const c = res.counts || {};
  $('data-stats').textContent = t('data.stats', {
    size: fmtBytes(res.size_bytes),
    tx: c.transactions ?? '?',
    acct: c.accounts ?? '?',
    rep: c.report_summaries ?? '?',
  });
}

$('btn-import').addEventListener('click', () => $('import-file').click());

$('import-file').addEventListener('change', async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = '';                       // 清掉，否则选同一个文件不会再触发 change
  if (!file) return;

  // 按文件头判断要不要弹"会覆盖"的警告。后端也会再判一次（那次才算数），
  // 这里读前 16 字节只是为了把确认文案说对。
  const head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
  const isDb = String.fromCharCode(...head) === 'SQLite format 3' + String.fromCharCode(0);
  if (!confirm(t(isDb ? 'data.confirmDb' : 'data.confirmJson'))) return;

  const btn = $('btn-import');
  btn.disabled = true;
  showMsg('msg-data', t('data.importing'), false);

  const form = new FormData();
  form.append('file', file);
  // 不要设 Content-Type：浏览器要自己加 multipart 的 boundary
  const res = await api('/api/data/import', { method: 'POST', body: form, headers: {} });
  btn.disabled = false;

  if (!res.ok) { showMsg('msg-data', res.error || t('data.failed'), true); return; }

  if (res.mode === 'db') {
    const c = res.counts || {};
    showMsg('msg-data', t('data.doneDb', {
      tx: c.transactions, acct: c.accounts, rep: c.report_summaries, backup: res.backup,
    }), false);
  } else {
    let text = t('data.doneJson', {
      tx: res.transactions, acct: res.accounts, rep: res.summaries,
      watch: res.watchlist, skipped: res.skipped,
    });
    if ((res.errors || []).length) {
      console.warn('[import]', res.errors);
      text += t('data.someErrors', { n: res.errors.length });
    }
    showMsg('msg-data', text, false);
  }

  // 库可能整个换了，页面上所有东西都得重新拉
  loadState();
  loadPortfolio();
  loadReports();
  loadConfig();
  loadDataStats();
});

/* ------------------------------------------------------------------ 走势图 */

let chartSymbol = null;

async function openChart(symbol, title) {
  chartSymbol = symbol;
  $('chart-title').textContent = `${symbol} ${title || ''}`;
  $('chart-modal').hidden = false;
  await drawChart();
}

$('chart-close').addEventListener('click', () => { $('chart-modal').hidden = true; });
$('chart-modal').addEventListener('click', (ev) => {
  if (ev.target === $('chart-modal')) $('chart-modal').hidden = true;
});
$('chart-range').addEventListener('change', drawChart);

async function drawChart() {
  const hours = Number($('chart-range').value);
  const data = await api(`/api/history?symbol=${encodeURIComponent(chartSymbol)}&hours=${hours}`);
  const points = data.points || [];
  const canvas = $('chart-canvas');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height, pad = { l: 58, r: 14, t: 14, b: 26 };
  const css = getComputedStyle(document.body);
  const text = css.getPropertyValue('--text').trim();
  const muted = css.getPropertyValue('--muted').trim();
  const border = css.getPropertyValue('--border').trim();

  ctx.clearRect(0, 0, W, H);
  if (points.length < 2) {
    $('chart-hint').textContent = t('chart.notEnough');
    ctx.fillStyle = muted;
    ctx.font = '14px sans-serif';
    ctx.fillText(t('chart.noData'), W / 2 - 40, H / 2);
    return;
  }

  const prices = points.map((p) => p.price);
  let min = Math.min(...prices), max = Math.max(...prices);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.08; max += span * 0.08;

  const t0 = points[0].ts, t1 = points[points.length - 1].ts;
  const x = (ts) => pad.l + (ts - t0) / Math.max(1, t1 - t0) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (max - v) / (max - min) * (H - pad.t - pad.b);

  // 网格与刻度
  ctx.strokeStyle = border; ctx.fillStyle = muted; ctx.lineWidth = 1;
  ctx.font = '11px sans-serif';
  for (let i = 0; i <= 4; i++) {
    const v = max - (max - min) * i / 4;
    const yy = Math.round(y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(W - pad.r, yy); ctx.stroke();
    ctx.fillText(v.toFixed(2), 6, yy + 4);
  }
  for (let i = 0; i <= 3; i++) {
    const ts = t0 + (t1 - t0) * i / 3;
    const label = new Date(ts * 1000).toLocaleString('zh-CN',
      { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    ctx.fillText(label, x(ts) - 28, H - 8);
  }

  const rising = prices[prices.length - 1] >= prices[0];
  const color = css.getPropertyValue(rising ? '--up' : '--down').trim();

  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(x(p.ts), y(p.price)) : ctx.moveTo(x(p.ts), y(p.price))));
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();

  ctx.lineTo(x(t1), H - pad.b); ctx.lineTo(x(t0), H - pad.b); ctx.closePath();
  ctx.fillStyle = color + '22';
  ctx.fill();

  ctx.fillStyle = text;
  ctx.font = '13px sans-serif';
  const change = (prices[prices.length - 1] - prices[0]) / prices[0] * 100;
  $('chart-hint').textContent =
    t('chart.summary', {
      n: points.length,
      change: (change >= 0 ? '+' : '') + change.toFixed(2),
      last: prices[prices.length - 1].toFixed(2),
    });
}

/* ------------------------------------------------------------------ 启动 */

/* 切语言时：静态文案由 applyStatic 刷（i18n.js 里做），动态内容得重新渲染一遍。
 * 直接复用各自的 load 函数，不另写一套「只改文字」的逻辑——那种逻辑一定会和
 * 渲染函数慢慢走偏。 */
initI18n(() => {
  loadState();        // 顶栏、指数卡、追踪表、告警表
  loadPortfolio();    // KPI、持仓表、账户卡、流水表
  loadReports();      // 年报点评列表
  loadDataStats();    // 「当前库 X MB，N 笔流水」那一行
  syncTxKindUI();     // 「实付/到账」的 placeholder 是 JS 写的，applyStatic 管不到
  fillAccountSelects();
});

initDatePicker();
setTxDate(localToday());

loadReports();
loadConfig();
loadState();
loadPortfolio();
loadDataStats();
stateTimer = setInterval(() => { loadState(); loadPortfolio(); }, REFRESH_MS);
