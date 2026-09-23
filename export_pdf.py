"""把持仓页导出成 PDF。

用 reportlab 在服务端直接生成，不走浏览器打印对话框——这样点一下就是一个文件，
而且导出的是**后端的真实数据**，不是页面上可能已经过期的那份渲染结果。

中文用 reportlab 自带的 STSong-Light（CID 字体），不需要外挂字体文件，
所以换一台机器跑也不会变成方框。

颜色沿用页面上的习惯：红涨绿跌。
"""
from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

FONT = "STSong-Light"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))

UP = colors.HexColor("#c0392b")      # 红涨
DOWN = colors.HexColor("#1e8449")    # 绿跌
MUTED = colors.HexColor("#777777")
LINE = colors.HexColor("#dddddd")
HEAD_BG = colors.HexColor("#f2f4f7")

TX_LABEL = {"BUY": "买入", "SELL": "卖出", "DIV": "股息"}
CHECK_LABEL = {"ok": "已核对", "warn": "有偏差", "bad": "差距大", "skip": "未核对"}


def _num(value, digits: int = 2) -> str:
    """德式千分位，跟页面上显示的一致（1.234,56）。"""
    if value is None:
        return "-"
    text = f"{float(value):,.{digits}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _pct(value) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def _style(name: str, size: float, color=colors.black, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=FONT, fontSize=size, leading=size * 1.35,
                          textColor=color, **kw)


def _kpi_block(totals: dict) -> Table:
    missing = (totals.get("missing_quotes") or [])
    money = lambda v: "数据不全" if v is None else f"€ {_num(v)}"
    cells = [
        ("总投入", f"€ {_num(totals['invested_eur'])}",
         f"{totals['positions']} 个持仓 · 手续费 € {_num(totals['fees_eur'])}"),
        ("当前市值", money(totals.get("market_value_eur")),
         f"{len(missing)} 只无行情未计入" if missing else ""),
        ("累计股息", f"€ {_num(totals['dividends_eur'])}", "税后到账合计"),
        ("总收益", money(totals.get("total_return_eur")),
         "" if totals.get("return_pct") is None else f"收益率 {_pct(totals['return_pct'])}"),
        ("组合年化", "-" if totals.get("annualized_pct") is None else _pct(totals["annualized_pct"]),
         "资金加权 XIRR"),
    ]

    rows = [[], [], []]
    for label, value, sub in cells:
        color = colors.black
        if label in ("总收益", "组合年化"):
            raw = totals.get("total_return_eur" if label == "总收益" else "annualized_pct")
            if raw is not None:
                color = UP if raw >= 0 else DOWN
        rows[0].append(Paragraph(label, _style("k", 8, MUTED)))
        rows[1].append(Paragraph(value, _style("v", 14, color)))
        rows[2].append(Paragraph(sub, _style("s", 7, MUTED)))

    table = Table(rows, colWidths=[52 * mm] * len(cells))
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, LINE),
    ]))
    return table


def _positions_table(positions: list[dict], show_account: bool) -> Table:
    headers = (["账户"] if show_account else []) + [
        "代码 / 名称", "持股", "现价", "市值 €", "总投入 €", "已收股息 €",
        "摊薄成本 €", "总收益 €", "收益率", "年化", "持有天数",
    ]
    head_style = _style("h", 7, MUTED)
    cell = _style("c", 7)
    right = _style("r", 7, alignment=2)

    data = [[Paragraph(h, head_style) for h in headers]]
    colors_by_row = []

    for index, p in enumerate(positions, start=1):
        name = f"<b>{p['symbol']}</b><br/><font size=6 color='#888888'>{p.get('name') or ''}</font>"
        price = (f"{_num(p['price'])} {p.get('currency') or ''}"
                 if p.get("has_quote") else "查不到")
        money = lambda v: "-" if v is None else _num(v)
        row = ([Paragraph(p.get("account") or "", cell)] if show_account else []) + [
            Paragraph(name, cell),
            Paragraph(f"{p['shares_held']:g}" + ("（已清仓）" if p.get("closed") else ""), right),
            Paragraph(price, right),
            Paragraph(money(p.get("market_value_eur")), right),
            Paragraph(_num(p["invested_eur"]), right),
            Paragraph(_num(p["dividends_eur"]), right),
            Paragraph(money(p.get("breakeven_eur")), right),
            Paragraph(money(p.get("total_return_eur")), right),
            Paragraph(_pct(p.get("return_pct")), right),
            Paragraph(_pct(p.get("annualized_pct")), right),
            Paragraph(str(p.get("holding_days") or "-"), right),
        ]
        data.append(row)
        colors_by_row.append((index, p.get("total_return_eur"), p.get("return_pct"),
                              p.get("annualized_pct")))

    widths = ([16 * mm] if show_account else []) + [
        36 * mm, 15 * mm, 22 * mm, 22 * mm, 22 * mm, 20 * mm,
        20 * mm, 22 * mm, 18 * mm, 18 * mm, 16 * mm,
    ]
    table = Table(data, colWidths=widths, repeatRows=1)

    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    offset = 1 if show_account else 0
    for row_index, total_return, return_pct, annual in colors_by_row:
        for col, value in ((7 + offset, total_return), (8 + offset, return_pct),
                           (9 + offset, annual)):
            if value is not None:
                style.append(("TEXTCOLOR", (col, row_index), (col, row_index),
                              UP if value >= 0 else DOWN))
    table.setStyle(TableStyle(style))
    return table


def _tx_table(txs: list[dict], show_account: bool) -> Table:
    headers = ["日期"] + (["账户"] if show_account else []) + [
        "代码", "类型", "股数", "每股价", "手续费 €", "金额 €", "核对", "备注",
    ]
    head_style = _style("h", 7, MUTED)
    cell = _style("c", 7)
    right = _style("r", 7, alignment=2)
    small = _style("s", 6, MUTED)

    data = [[Paragraph(h, head_style) for h in headers]]
    for tx in reversed(txs):
        price = "-" if tx.get("price") is None else \
            f"{_num(tx['price'], 4)} {tx.get('price_currency') or ''}"
        row = [Paragraph(tx["trade_date"], cell)]
        if show_account:
            row.append(Paragraph(tx.get("account_name") or "", cell))
        row += [
            Paragraph(f"<b>{tx['symbol']}</b>", cell),
            Paragraph(TX_LABEL.get(tx["kind"], tx["kind"]), cell),
            Paragraph("-" if tx.get("shares") is None else f"{tx['shares']:g}", right),
            Paragraph(price, right),
            Paragraph(_num(tx.get("fee_eur") or 0), right),
            Paragraph(_num(tx.get("amount_eur")), right),
            Paragraph(CHECK_LABEL.get(tx.get("check_status") or "", "未核对"), small),
            Paragraph(tx.get("note") or "", small),
        ]
        data.append(row)

    widths = [20 * mm] + ([16 * mm] if show_account else []) + [
        18 * mm, 14 * mm, 16 * mm, 26 * mm, 18 * mm, 22 * mm, 18 * mm, 30 * mm,
    ]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _markdown_flowables(text: str) -> list:
    """把点评的 Markdown 转成 reportlab 的流式元素。

    只认 ## 标题、- 列表、**加粗**——模型输出是不可信内容，其余字符一律转义，
    免得 <font> 之类的标记被 reportlab 当成富文本解析。
    """
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def inline(s: str) -> str:
        out, parts = "", esc(s).split("**")
        for index, part in enumerate(parts):
            out += f"<b>{part}</b>" if index % 2 else part
        return out

    body = _style("ib", 8)
    head = _style("ih", 8.5, colors.HexColor("#444444"), spaceBefore=4)
    bullet = _style("il", 8, leftIndent=8 * mm, bulletIndent=4 * mm)

    flowables = []
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            flowables.append(Paragraph(inline(line.lstrip("#").strip()), head))
        elif line.startswith(("- ", "* ")):
            flowables.append(Paragraph(inline(line[2:]), bullet, bulletText="·"))
        else:
            flowables.append(Paragraph(inline(line), body))
    return flowables


def _summary_section(symbol: str, entry: dict) -> list:
    """一只股票的年报 + 季报点评。"""
    out = [Paragraph(symbol, _style("ss", 11))]
    for period_type, title in (("annual", "年报点评"), ("quarterly", "季报点评")):
        record = entry.get(period_type)
        if not record:
            continue
        if period_type == "quarterly":
            period = f"截至 {record.get('period_key') or record.get('as_of')} 的季度"
        else:
            period = f"{record.get('period_key') or record.get('fiscal_year')} 财年"
        stamp = datetime.fromtimestamp(record["created_at"]).strftime("%Y-%m-%d") \
            if record.get("created_at") else ""
        out.append(Paragraph(
            f"{title} · {period}　<font color='#999999'>"
            f"{record.get('model') or ''} 生成于 {stamp}</font>",
            _style("sh", 9, colors.HexColor("#555555"), spaceBefore=3)))
        out += _markdown_flowables(record.get("summary") or "")
        out.append(Spacer(1, 3 * mm))
    return out


def build(portfolio_data: dict, txs: list[dict], account_name: str | None,
          summaries: dict | None = None) -> bytes:
    """生成 PDF，返回字节。account_name 为空表示「全部账户」。"""
    buffer = io.BytesIO()
    scope = account_name or "全部账户"
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"StockWatch 持仓报告 - {scope}", author="StockWatch",
    )

    positions = portfolio_data.get("positions") or []
    totals = portfolio_data.get("totals") or {}
    show_account = account_name is None

    story = [
        Paragraph(f"StockWatch 持仓报告 · {scope}", _style("t", 16)),
        Paragraph(
            f"生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}　·　"
            f"金额单位为欧元，成本按记录的实际扣款额计，市值按当前汇率折算",
            _style("sub", 8, MUTED)),
        Spacer(1, 6 * mm),
        _kpi_block(totals),
        Spacer(1, 7 * mm),
    ]

    if positions:
        story.append(Paragraph("持仓明细", _style("h2", 11)))
        story.append(Spacer(1, 2 * mm))
        story.append(_positions_table(positions, show_account))
    else:
        story.append(Paragraph("（这个账户下没有持仓）", _style("empty", 9, MUTED)))

    missing = totals.get("missing_quotes") or []
    if missing:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(
            f"注意：{'、'.join(missing)} 取不到行情，市值与总收益未计入这些标的。",
            _style("warn", 8, UP)))

    if txs:
        story.append(Spacer(1, 8 * mm))
        story.append(KeepTogether([
            Paragraph("交易流水", _style("h2", 11)),
            Spacer(1, 2 * mm),
        ]))
        story.append(_tx_table(txs, show_account))

    # 财报点评附在持仓后面，一只股票一段，按持仓表的顺序排
    if summaries:
        story.append(PageBreak())
        story.append(Paragraph("财报点评附录", _style("h2", 13)))
        story.append(Paragraph(
            "以下内容由大语言模型根据公开财报数据生成，仅为信息整理，不构成投资建议。",
            _style("note", 8, MUTED)))
        story.append(Spacer(1, 4 * mm))
        seen = set()
        for p in positions:
            symbol = p["symbol"]
            if symbol in seen or symbol not in summaries:
                continue
            seen.add(symbol)
            section = _summary_section(symbol, summaries[symbol])
            # 开头几行绑在一起，免得"标题在上一页、正文在下一页"
            story.append(KeepTogether(section[:3]))
            story += section[3:]
            story.append(Spacer(1, 5 * mm))

    rates = portfolio_data.get("fx") or {}
    usd = rates.get("USD")
    footer = "年化为资金加权收益率（XIRR），与 Excel 的 XIRR 函数一致。"
    if usd:
        footer = f"折算汇率 1 USD = {usd['eur_per_unit']:.4f} EUR。" + footer
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(footer, _style("f", 7, MUTED)))

    doc.build(story)
    return buffer.getvalue()
