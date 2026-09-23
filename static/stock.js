/* 个股基本面页：年报、利润率、分红、现金流、长期股价。 */
'use strict';

const SYMBOL = document.body.dataset.symbol;
const $$ = (id) => document.getElementById(id);

const num = (v, d = 2) => (v === null || v === undefined) ? '-' : Number(v).toFixed(d);
const pct = (v, d = 2) => (v === null || v === undefined) ? '-' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`;

function tile(label, value, sub, cls) {
  return `<div class="kpi">
    <div class="kpi-label">${label}</div>
    <div class="kpi-value ${cls || ''}">${value}</div>
    <div class="kpi-sub">${sub || ''}</div>
  </div>`;
}

let lastData = null;   // 最近一次抓到的基本面数据，切语言时原地重画用

async function load(force) {
  const box = $$('charts');
  box.style.opacity = '0.5';   // 重新抓取时保持旧内容，不闪骨架屏
  const res = await fetch(`/api/fundamentals?symbol=${encodeURIComponent(SYMBOL)}${force ? '&force=1' : ''}`);
  const d = await res.json();
  box.style.opacity = '1';

  if (d.error) {
    box.innerHTML = `<p class="chart-empty">${t('sp.fetchFailed', { error: d.error })}</p>`;
    return;
  }
  lastData = d;
  render(d);
}

function render(d) {
  if (!d) return;
  document.title = t('sp.title', { symbol: `${d.symbol} ${d.name}` });
  $$('hd-name').textContent = `${d.symbol} · ${d.name}`;
  $$('hd-exchange').textContent = d.exchange || '';
  $$('hd-cached').textContent = d.cached
    ? t('sp.cachedAt', { time: new Date(d.fetched_at * 1000).toLocaleString(dateLocale(), { hour12: false }) })
    : t('sp.justNow');

  const annual = d.annual || [];
  const latest = annual[annual.length - 1] || {};
  const years = annual.map((r) => String(r.year));
  const lastPayout = (d.payout_ratio || [])[d.payout_ratio.length - 1];

  /* ---------------------------------------------------------- 关键指标 */
  $$('kpis').innerHTML = [
    tile(t('sp.price'), `${num(d.price)} ${d.currency}`, pct(d.change_pct), d.change_pct >= 0 ? 'up' : 'down'),
    tile(t('sp.divYield'), d.dividend_yield ? `${d.dividend_yield.toFixed(2)}%` : t('sp.noDiv'),
      d.ttm_dividend ? t('sp.perShare', { v: num(d.ttm_dividend), cur: d.currency }) : ''),
    tile(t('sp.revenueOf', { year: latest.year || '' }), Charts.compact(latest.revenue),
      `${d.report_currency}${latest.as_of ? ' · ' + t('sp.asOf', { date: latest.as_of }) : ''}`),
    tile(t('sp.netMarginOf', { year: latest.year || '' }),
      latest.net_margin === undefined ? '-' : `${latest.net_margin.toFixed(1)}%`,
      t('sp.netIncome', { v: Charts.compact(latest.net_income) }),
      latest.net_margin < 0 ? 'down' : ''),
    tile(t('sp.payout'), lastPayout ? `${lastPayout.ratio.toFixed(0)}%` : '-',
      lastPayout ? t('sp.payoutSub', { year: lastPayout.year, div: num(lastPayout.dividend), eps: num(lastPayout.eps) })
                 : t('sp.noComparable')),
  ].join('');

  const notes = [];
  if (d.report_currency && d.currency && d.report_currency !== d.currency) {
    notes.push(t('sp.currencyNote', { quote: d.currency, report: d.report_currency }));
  }
  if (!annual.length) notes.push(t('sp.noAnnual'));
  $$('meta-note').textContent = notes.join(' ');

  /* ---------------------------------------------------------- 图表 */
  const box = $$('charts');
  box.innerHTML = '';
  const cur = d.report_currency || '';

  if (annual.length) {
    Charts.bar(box, {
      title: t('ch.revenueNet'),
      subtitle: t('ch.annualCur', { cur }),
      categories: years,
      series: [
        { name: t('ch.revenue'), values: annual.map((r) => r.revenue ?? null) },
        { name: t('ch.netIncome'), values: annual.map((r) => r.net_income ?? null) },
      ],
    });

    const marginSeries = [
      { name: t('ch.grossMargin'), values: annual.map((r) => r.gross_margin ?? null), suffix: '' },
      { name: t('ch.operatingMargin'), values: annual.map((r) => r.operating_margin ?? null) },
      { name: t('ch.netMargin'), values: annual.map((r) => r.net_margin ?? null) },
    ].filter((s) => s.values.some((v) => v !== null));

    if (marginSeries.length) {
      Charts.line(box, {
        // 只有一条线时不画图例，标题就得点明具体是哪个指标
        title: marginSeries.length === 1 ? marginSeries[0].name : t('ch.margins'),
        subtitle: t('ch.annualPct'),
        categories: years,
        series: marginSeries,
        format: (v) => Charts.fmtPct(v, 0),
        zeroBased: true,
      });
    }

    Charts.bar(box, {
      title: t('ch.eps'),
      subtitle: t('ch.annualCur', { cur }),
      categories: years,
      series: [{ name: 'EPS', values: annual.map((r) => r.eps ?? null) }],
      format: (v) => Charts.fmtMoney(v, 2),
    });

    const cashSeries = [
      { name: t('ch.opCash'), values: annual.map((r) => r.operating_cash_flow ?? null) },
      { name: t('ch.freeCash'), values: annual.map((r) => r.free_cash_flow ?? null) },
    ].filter((s) => s.values.some((v) => v !== null));
    if (cashSeries.length) {
      Charts.bar(box, {
        title: t('ch.cashflow'),
        subtitle: t('ch.annualCur', { cur }),
        categories: years,
        series: cashSeries,
      });
    }
  }

  const quarters = (d.quarterly || []).slice(-8);
  if (quarters.length >= 2) {
    const labels = quarters.map((q) => q.as_of.slice(2));   // 26-06-30
    Charts.bar(box, {
      title: t('ch.qRevenueNet'),
      subtitle: t('ch.qSub', { n: quarters.length, cur }),
      categories: labels,
      series: [
        { name: t('ch.revenue'), values: quarters.map((q) => q.revenue ?? null) },
        { name: t('ch.netIncome'), values: quarters.map((q) => q.net_income ?? null) },
      ],
      note: t('ch.qNote'),
    });

    const qMargin = [
      { name: t('ch.grossMargin'), values: quarters.map((q) => q.gross_margin ?? null) },
      { name: t('ch.netMargin'), values: quarters.map((q) => q.net_margin ?? null) },
    ].filter((s) => s.values.some((v) => v !== null));
    if (qMargin.length) {
      Charts.line(box, {
        title: qMargin.length === 1 ? t('ch.qMarginOne', { name: qMargin[0].name }) : t('ch.qMargins'),
        subtitle: '%',
        categories: labels,
        series: qMargin,
        format: (v) => Charts.fmtPct(v, 0),
        zeroBased: true,
      });
    }
  }

  const divYears = d.dividends_by_year || [];
  if (divYears.length) {
    const recent = divYears.slice(-16);
    Charts.bar(box, {
      title: t('ch.dividends'),
      subtitle: `${recent[0].year}–${recent[recent.length - 1].year} · ${d.currency}`,
      categories: recent.map((r) => String(r.year)),
      series: [{
        name: t('ch.dividendPerShare'),
        values: recent.map((r) => r.amount),
        faded: recent.map((r) => !!r.partial),
        hint: recent.map((r) => r.partial ? t('ch.partialHint') : t('ch.payments', { n: r.count })),
      }],
      format: (v) => Charts.fmtMoney(v, 2),
      note: recent.some((r) => r.partial) ? t('ch.partialNote') : '',
    });
  }

  if ((d.payout_ratio || []).length) {
    Charts.bar(box, {
      title: t('ch.payoutTitle'),
      subtitle: t('ch.annualPct'),
      categories: d.payout_ratio.map((p) => String(p.year)),
      series: [{
        name: t('sp.payout'),
        values: d.payout_ratio.map((p) => p.ratio),
        hint: d.payout_ratio.map((p) => t('ch.payoutHint', { div: num(p.dividend), eps: num(p.eps) })),
      }],
      format: (v) => Charts.fmtPct(v, 0),
      note: t('ch.payoutNote'),
    });
  }

  const prices = d.prices || [];
  if (prices.length > 2) {
    // 标题按实际覆盖的时间写：新上市的标的没有五年数据，写死「近 5 年」会误导
    const spanDays = Math.round((prices[prices.length - 1].ts - prices[0].ts) / 86400);
    const spanLabel = spanDays >= 700 ? t('ch.spanYears', { n: Math.round(spanDays / 365) })
      : spanDays >= 330 ? t('ch.spanYear')
      : t('ch.spanSince', { n: spanDays });
    Charts.line(box, {
      title: t('ch.priceTitle', { span: spanLabel }),
      subtitle: d.currency,
      categories: prices.map((p) => new Date(p.ts * 1000).toLocaleDateString('zh-CN', { year: '2-digit', month: '2-digit' })),
      series: [{ name: t('ch.close'), values: prices.map((p) => p.close) }],
      format: (v) => Charts.fmtMoney(v, 2),
      area: true,
      height: 320,
    });
  }

  /* ---------------------------------------------------------- 年报明细表 */
  const cols = [
    [t('ch.revenue'), 'revenue', Charts.compact],
    [t('ch.grossProfit'), 'gross_profit', Charts.compact],
    [t('ch.operatingIncome'), 'operating_income', Charts.compact],
    [t('ch.netIncome'), 'net_income', Charts.compact],
    [t('ch.netMargin'), 'net_margin', (v) => Charts.fmtPct(v, 1)],
    ['EPS', 'eps', (v) => Charts.fmtMoney(v, 2)],
    [t('ch.opCash'), 'operating_cash_flow', Charts.compact],
    [t('ch.freeCash'), 'free_cash_flow', Charts.compact],
    [t('ch.equity'), 'equity', Charts.compact],
    [t('ch.totalDebt'), 'total_debt', Charts.compact],
  ];
  $$('annual-table').innerHTML = annual.length
    ? `<table><thead><tr><th>${t('sp.item', { cur })}</th>${annual.map((r) => `<th class="right">${r.year}</th>`).join('')}</tr></thead>
       <tbody>${cols.map(([label, key, fmt]) =>
        `<tr><th>${label}</th>${annual.map((r) => `<td class="right">${r[key] === undefined ? '-' : fmt(r[key])}</td>`).join('')}</tr>`).join('')}
       </tbody></table>`
    : `<p class="chart-empty">${t('sp.noAnnualData')}</p>`;
}

/* ------------------------------------------------------------ 年报点评 */

/* 只渲染模型会用到的那几种 Markdown（标题、加粗、列表），
 * 其余一律转义——模型输出是不可信内容，不能直接塞 innerHTML。 */
function miniMarkdown(text) {
  const esc = (s) => s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const lines = esc(text).split('\n');
  const out = [];
  let inList = false;
  for (const raw of lines) {
    const line = raw.trimEnd();
    const bold = (s) => s.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    if (/^#{1,6}\s/.test(line)) {
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(`<h4>${bold(line.replace(/^#{1,6}\s*/, ''))}</h4>`);
    } else if (/^[-*]\s/.test(line)) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${bold(line.replace(/^[-*]\s*/, ''))}</li>`);
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

const PERIODS = {
  annual: { label: () => t('rep.annual'), empty: () => t('ins.emptyAnnual') },
  quarterly: { label: () => t('rep.quarterly'), empty: () => t('ins.emptyQuarterly') },
};

function periodLabel(record) {
  return record.period_type === 'quarterly'
    ? t('rep.periodQuarter', { date: record.period_key || record.as_of })
    : t('rep.periodAnnual', { year: record.period_key || record.fiscal_year });
}

function insightBox(periodType) {
  return document.querySelector(`.insight[data-period="${periodType}"]`);
}

function renderInsight(periodType, record) {
  const box = insightBox(periodType);
  const body = box.querySelector('[data-el=body]');
  const meta = box.querySelector('[data-el=meta]');
  const gen = box.querySelector('[data-el=gen]');

  if (!record || !record.summary) {
    body.innerHTML = `<p class="chart-empty">${PERIODS[periodType].empty()}</p>`;
    meta.textContent = '';
    gen.textContent = t('ins.gen');
    return;
  }
  body.innerHTML = miniMarkdown(record.summary);
  const tokens = (record.prompt_tokens || 0) + (record.completion_tokens || 0);
  meta.textContent = `${periodLabel(record)} · ${record.model || ''} · ` +
    `${new Date(record.created_at * 1000).toLocaleString('zh-CN', { hour12: false })}` +
    (tokens ? ` · ${tokens} tokens` : '');
  gen.textContent = t('ins.regen');
}

async function loadInsight() {
  const data = await fetch(`/api/insights?symbol=${encodeURIComponent(SYMBOL)}`).then((r) => r.json());
  if (!data.configured) {
    Object.keys(PERIODS).forEach((periodType) => {
      const box = insightBox(periodType);
      box.querySelector('[data-el=body]').innerHTML =
        `<p class="chart-empty">${t('ins.notConfigured')}</p>`;
      box.querySelector('[data-el=gen]').hidden = true;
    });
    return;
  }
  // 一个代码可能有多期，各类型取最新的一份
  const rows = data.summaries || [];
  Object.keys(PERIODS).forEach((periodType) => {
    const latest = rows.filter((r) => (r.period_type || 'annual') === periodType)[0];
    renderInsight(periodType, latest);
  });
}

async function generateInsight(periodType) {
  const box = insightBox(periodType);
  const gen = box.querySelector('[data-el=gen]');
  const body = box.querySelector('[data-el=body]');
  gen.disabled = true;
  body.innerHTML = `<p class="chart-empty">${t('ins.generating')}</p>`;
  const res = await fetch('/api/insights/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol: SYMBOL, force: true, period_type: periodType }),
  }).then((r) => r.json());
  gen.disabled = false;
  if (!res.ok) {
    body.innerHTML = `<p class="chart-empty">${t('ins.genFailed', { error: res.error || t('ins.unknownError') })}</p>`;
    return;
  }
  renderInsight(periodType, res);
}

Object.keys(PERIODS).forEach((periodType) => {
  insightBox(periodType).querySelector('[data-el=gen]')
    .addEventListener('click', () => generateInsight(periodType));
});

/* 这个页面总是新标签页打开的。先试着直接关掉标签页（浏览器多半不允许，
 * 因为不是脚本开的窗口），关不掉就退回监控页，免得留下两个一样的标签页。 */
$$('btn-back').addEventListener('click', () => {
  window.close();
  setTimeout(() => { window.location.href = '/'; }, 120);
});

$$('btn-reload').addEventListener('click', async () => {
  const btn = $$('btn-reload');
  btn.disabled = true;
  btn.textContent = t('sp.reloading');
  await load(true);
  btn.disabled = false;
  btn.textContent = t('sp.reload');
});

/* 切语言：整页重画一遍。图表标题、系列名、表格表头全是 JS 生成的，
 * 没有「只改文字」的捷径——重新走一遍渲染最省心也最不容易漏。 */
initI18n(() => {
  document.title = t('sp.title', { symbol: document.body.dataset.symbol });
  render(lastData);     // 图表、KPI、年报表格
  loadInsight();        // 两块点评
});
document.title = t('sp.title', { symbol: document.body.dataset.symbol });

load(false);
loadInsight();
