/* 轻量 SVG 图表：柱状图 / 折线图。无外部依赖。
 *
 * 遵循的几条硬规则：
 *   - 单一 Y 轴，绝不双轴（两个量纲就画两张图）
 *   - 柱子从 0 基线起算，负值向下画
 *   - 网格线是实线细发丝线，不用虚线
 *   - 两个以上系列必配图例；不在每个点上打数字
 *   - 每张图都带「表格」切换（浅色模式下青色对比度不足 3:1，规则要求必须有表格兜底）
 *   - 悬停显示数值，命中区域比图形本身大
 */
'use strict';

const Charts = (() => {

  const NS = 'http://www.w3.org/2000/svg';
  const PALETTE = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)'];

  const el = (name, attrs = {}) => {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) {
      if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    return node;
  };

  /* ---------------------------------------------------------------- 数值格式 */

  function compact(value, digits = 1) {
    if (value === null || value === undefined || Number.isNaN(value)) return '-';
    if (value === 0) return '0';
    const abs = Math.abs(value);
    const sign = value < 0 ? '-' : '';
    if (abs >= 1e12) return `${sign}${(abs / 1e12).toFixed(digits)}T`;
    if (abs >= 1e9) return `${sign}${(abs / 1e9).toFixed(digits)}B`;
    if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(digits)}M`;
    if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(digits)}K`;
    return `${sign}${abs.toFixed(abs < 10 ? 2 : digits)}`;
  }

  const fmtPct = (v, d = 1) => (v === null || v === undefined) ? '-' : `${v.toFixed(d)}%`;
  const fmtMoney = (v, d = 2) => (v === null || v === undefined) ? '-' : v.toFixed(d);

  /* 坐标轴刻度：取“好看”的步长（1/2/2.5/5 × 10^n） */
  function niceTicks(min, max, count = 4) {
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;
    const rawStep = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
    const norm = rawStep / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    const start = Math.floor(min / step) * step;
    const end = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = start; v <= end + step * 1e-6; v += step) ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    return ticks;
  }

  /* ---------------------------------------------------------------- 骨架 */

  function shell(spec) {
    const wrap = document.createElement('figure');
    wrap.className = 'chart';

    const head = document.createElement('figcaption');
    head.className = 'chart-head';
    head.innerHTML = `<span class="chart-title">${spec.title || ''}</span>` +
      (spec.subtitle ? `<span class="chart-sub">${spec.subtitle}</span>` : '');

    const toggle = document.createElement('button');
    toggle.className = 'btn small chart-toggle';
    toggle.type = 'button';
    toggle.textContent = t('ch.table');
    head.appendChild(toggle);
    wrap.appendChild(head);

    const plot = document.createElement('div');
    plot.className = 'chart-plot';
    wrap.appendChild(plot);

    const table = document.createElement('div');
    table.className = 'chart-table';
    table.hidden = true;
    wrap.appendChild(table);

    toggle.addEventListener('click', () => {
      const showTable = table.hidden;
      table.hidden = !showTable;
      plot.hidden = showTable;
      toggle.textContent = t(showTable ? 'ch.chart' : 'ch.table');
    });

    if (spec.note) {
      const note = document.createElement('p');
      note.className = 'chart-note';
      note.textContent = spec.note;
      wrap.appendChild(note);
    }
    return { wrap, plot, table };
  }

  function legend(series) {
    const box = document.createElement('div');
    box.className = 'chart-legend';
    series.forEach((s, i) => {
      const item = document.createElement('span');
      item.innerHTML = `<i style="background:${s.color || PALETTE[i % PALETTE.length]}"></i>${s.name}`;
      box.appendChild(item);
    });
    return box;
  }

  function tableView(categories, series, format) {
    const rows = categories.map((cat, i) =>
      `<tr><th>${cat}</th>${series.map((s) => `<td class="right">${format(s.values[i])}</td>`).join('')}</tr>`);
    return `<table><thead><tr><th>${''}</th>${series.map((s) => `<th class="right">${s.name}</th>`).join('')}</tr></thead>` +
      `<tbody>${rows.join('')}</tbody></table>`;
  }

  /* 悬停提示：跟着鼠标走，值同时也在表格视图里能读到 */
  function attachTip(plot, target, html) {
    target.addEventListener('mouseenter', () => {
      let tip = plot.querySelector('.chart-tip');
      if (!tip) {
        tip = document.createElement('div');
        tip.className = 'chart-tip';
        plot.appendChild(tip);
      }
      tip.innerHTML = html;
      tip.hidden = false;
      const box = target.getBoundingClientRect();
      const host = plot.getBoundingClientRect();
      tip.style.left = `${Math.min(Math.max(box.left - host.left + box.width / 2, 60), host.width - 60)}px`;
      tip.style.top = `${Math.max(box.top - host.top - 10, 0)}px`;
    });
    target.addEventListener('mouseleave', () => {
      const tip = plot.querySelector('.chart-tip');
      if (tip) tip.hidden = true;
    });
  }

  /* ---------------------------------------------------------------- 柱状图 */

  function bar(container, spec) {
    const { wrap, plot, table } = shell(spec);
    const series = spec.series.filter((s) => s.values.some((v) => v !== null && v !== undefined));
    const categories = spec.categories;
    const format = spec.format || compact;

    if (!series.length || !categories.length) {
      plot.innerHTML = `<p class="chart-empty">${t('ch.noSeries')}</p>`;
      container.appendChild(wrap);
      return wrap;
    }

    const W = 760, H = spec.height || 300;
    const pad = { l: 64, r: 16, t: 14, b: 38 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

    const all = series.flatMap((s) => s.values).filter((v) => v !== null && v !== undefined);
    const ticks = niceTicks(Math.min(0, ...all), Math.max(0, ...all), 4);
    const vMin = ticks[0], vMax = ticks[ticks.length - 1];
    const y = (v) => pad.t + (vMax - v) / (vMax - vMin) * ih;
    const zeroY = y(0);

    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart-svg', role: 'img' });

    ticks.forEach((t) => {
      svg.appendChild(el('line', {
        x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t),
        class: t === 0 ? 'axis-zero' : 'grid',
      }));
      const label = el('text', { x: pad.l - 8, y: y(t) + 4, class: 'tick', 'text-anchor': 'end' });
      label.textContent = format(t);
      svg.appendChild(label);
    });

    const bandW = iw / categories.length;
    const groupPad = Math.min(14, bandW * 0.18);
    const slotW = (bandW - groupPad * 2) / series.length;
    // 相邻柱之间留 2px 背景缝隙；再封一个上限，类别少的时候柱子不至于粗成色块
    const barW = Math.max(3, Math.min(slotW - 2, 46 / series.length));
    // 类别多了就隔几个标一次，否则 x 轴文字会叠在一起
    const labelStep = Math.max(1, Math.ceil(categories.length / 10));

    categories.forEach((cat, ci) => {
      const bandX = pad.l + bandW * ci;

      if (ci % labelStep === 0 || ci === categories.length - 1) {
        const label = el('text', { x: bandX + bandW / 2, y: H - pad.b + 20, class: 'tick', 'text-anchor': 'middle' });
        label.textContent = cat;
        svg.appendChild(label);
      }

      series.forEach((s, si) => {
        const value = s.values[ci];
        if (value === null || value === undefined) return;
        const color = s.color || PALETTE[si % PALETTE.length];
        const top = Math.min(y(value), zeroY);
        const height = Math.max(1, Math.abs(y(value) - zeroY));
        const x = bandX + groupPad + slotW * si + (slotW - barW) / 2;

        const rect = el('rect', {
          x, y: top, width: barW, height,
          rx: Math.min(4, barW / 2), fill: color,
          class: s.faded && s.faded[ci] ? 'bar faded' : 'bar',
        });
        svg.appendChild(rect);

        // 命中区域盖满整个条带高度，不用精确点到柱子上
        const hit = el('rect', {
          x: x - 2, y: pad.t, width: barW + 4, height: ih, fill: 'transparent', class: 'hit',
        });
        svg.appendChild(hit);
        attachTip(plot, hit,
          `<b>${cat}</b><br>${s.name}${t('ch.sep')}${format(value)}${s.suffix || ''}` +
          (s.hint && s.hint[ci] ? `<br><span class="tip-hint">${s.hint[ci]}</span>` : ''));
      });
    });

    plot.appendChild(svg);
    if (series.length > 1) wrap.insertBefore(legend(series), plot);
    table.innerHTML = tableView(categories, series, format);
    container.appendChild(wrap);
    return wrap;
  }

  /* ---------------------------------------------------------------- 折线图 */

  function line(container, spec) {
    const { wrap, plot, table } = shell(spec);
    const series = spec.series.filter((s) => s.values.some((v) => v !== null && v !== undefined));
    const categories = spec.categories;
    const format = spec.format || compact;

    if (!series.length || categories.length < 2) {
      plot.innerHTML = `<p class="chart-empty">${t('ch.tooFewPoints')}</p>`;
      container.appendChild(wrap);
      return wrap;
    }

    const W = 760, H = spec.height || 300;
    const pad = { l: 64, r: 16, t: 14, b: 38 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

    const all = series.flatMap((s) => s.values).filter((v) => v !== null && v !== undefined);
    const lo = Math.min(...all), hi = Math.max(...all);
    const ticks = niceTicks(spec.zeroBased ? Math.min(0, lo) : lo, hi, 4);
    const vMin = ticks[0], vMax = ticks[ticks.length - 1];
    const x = (i) => pad.l + (categories.length === 1 ? iw / 2 : iw * i / (categories.length - 1));
    const y = (v) => pad.t + (vMax - v) / (vMax - vMin) * ih;

    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart-svg', role: 'img' });

    ticks.forEach((t) => {
      svg.appendChild(el('line', {
        x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t),
        class: t === 0 && spec.zeroBased ? 'axis-zero' : 'grid',
      }));
      const label = el('text', { x: pad.l - 8, y: y(t) + 4, class: 'tick', 'text-anchor': 'end' });
      label.textContent = format(t);
      svg.appendChild(label);
    });

    const step = Math.max(1, Math.ceil(categories.length / 6));
    categories.forEach((cat, i) => {
      if (i % step && i !== categories.length - 1) return;
      const label = el('text', { x: x(i), y: H - pad.b + 20, class: 'tick', 'text-anchor': 'middle' });
      label.textContent = cat;
      svg.appendChild(label);
    });

    series.forEach((s, si) => {
      const color = s.color || PALETTE[si % PALETTE.length];
      const points = s.values
        .map((v, i) => (v === null || v === undefined ? null : `${x(i)},${y(v)}`))
        .filter(Boolean);
      if (!points.length) return;

      if (spec.area && series.length === 1) {
        const base = y(Math.max(vMin, spec.zeroBased ? 0 : vMin));
        svg.appendChild(el('polygon', {
          points: `${x(0)},${base} ${points.join(' ')} ${x(s.values.length - 1)},${base}`,
          fill: color, class: 'area',
        }));
      }
      svg.appendChild(el('polyline', { points: points.join(' '), fill: 'none', stroke: color, class: 'line' }));

      // 点少的时候画出标记点，点多了只留命中区，避免糊成一片
      if (categories.length <= 14) {
        s.values.forEach((v, i) => {
          if (v === null || v === undefined) return;
          svg.appendChild(el('circle', { cx: x(i), cy: y(v), r: 4, fill: color, class: 'dot' }));
        });
      }
    });

    // 每个 x 位置一条透明命中带，鼠标靠近就出提示
    const bandW = iw / Math.max(1, categories.length - 1);
    categories.forEach((cat, i) => {
      const hit = el('rect', {
        x: x(i) - bandW / 2, y: pad.t, width: bandW, height: ih, fill: 'transparent', class: 'hit',
      });
      svg.appendChild(hit);
      const rows = series
        .map((s) => (s.values[i] === null || s.values[i] === undefined)
          ? null : `${s.name}${t('ch.sep')}${format(s.values[i])}${s.suffix || ''}`)
        .filter(Boolean);
      attachTip(plot, hit, `<b>${cat}</b><br>${rows.join('<br>')}`);
    });

    plot.appendChild(svg);
    if (series.length > 1) wrap.insertBefore(legend(series), plot);
    table.innerHTML = tableView(categories, series, format);
    container.appendChild(wrap);
    return wrap;
  }

  return { bar, line, compact, fmtPct, fmtMoney, niceTicks };
})();
