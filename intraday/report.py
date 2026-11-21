"""盤中監控的通知文字與圖（格式依使用者 2026-11-14 貼的範例）。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .core import Alert, Point, seconds

RED, GRAY, BLUE, ORANGE = "#d1343c", "#6b7280", "#1f5fcc", "#e8801a"
TEXT, TEXT_2, GRID = "#1f2328", "#6b7280", "#e5e7eb"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0f}%"


def _yi(x: float | None) -> str:
    return "—" if x is None else f"{x:,.0f} 億"


def _change(now: float | None, before: float | None) -> str:
    if not now or not before:
        return ""
    return f"{(now / before - 1) * 100:+.0f}%"


def summary(label: str, point: Point, prev_total: float | None, flows, notes: list[str] | None = None,
            head: str = "🕐【盤中定時】", official_prev: float | None = None) -> str:
    """定時（預設每小時）的通知。prev_total：昨天同一時間的普通股成交金額；flows：core.group_flows 的結果（沒有昨天的紀錄是 None）。"""
    lines = [f"{head}{label}"]
    money = f"💰 上市櫃普通股成交 {_yi(point.total)}"
    if prev_total:
        money += f"（昨同時 {_yi(prev_total)}，{_change(point.total, prev_total)}）"
    elif point.official and official_prev:              # 還沒有昨天的盤中紀錄：用官方的全市場成交金額比
        money += f"（全市場含 ETF {_yi(point.official)}，昨同時 {_yi(official_prev)}，{_change(point.official, official_prev)}）"
    lines.append(money)
    lines.append(f"📊 站上20MA {_pct(point.ma20)}｜60MA {_pct(point.ma60)}｜200MA {_pct(point.ma200)}")
    if point.big is not None and point.small is not None:
        lines.append(f"⚖️ 大型 {_pct(point.big)} vs 中小型 {_pct(point.small)}（差 {round(point.big) - round(point.small):+d}）")
    if point.vwap is not None:
        lines.append(f"📈 站上日內均價線 {_pct(point.vwap)}｜跌破 {100 - round(point.vwap)}%")
    lines.append(f"🆕 60日淨新高 {point.hi - point.lo:+d}（新高 {point.hi}／新低 {point.lo}）")
    if flows is None:
        lines.append("🔺🔻 族群資金（vs 昨同時）：下一個交易日開始才有（要先有前一天的盤中紀錄）")
    else:
        inflow, outflow = flows
        fmt = lambda rows: "、".join(f"{g} {r * 100:+.0f}%" for g, r in rows) or "無"      # noqa: E731
        lines.append(f"🔺 族群資金流入（vs 昨同時）：{fmt(inflow)}")
        lines.append(f"🔻 族群資金流出：{fmt(outflow)}")
    lines += [f"※ {n}" for n in notes or []]
    return "\n".join(lines)


def alert_text(alerts: list[Alert], now: datetime) -> str:
    """異常大筆資金的即時通知。"""
    lines = [f"🚨【異常資金】{now:%H:%M}"]
    for a in alerts:
        mins = f"{a.minutes:.0f} 分鐘"
        times = f"，平常的 {a.ratio:.1f} 倍" if a.ratio else "，之前幾乎沒有成交"
        way = "上漲" if a.move > 0 else "下跌"
        if a.kind == "大盤":
            icon = "⚡"
            lines.append(f"{icon} 大盤：加權指數 {mins}{way} {abs(a.move):.2f}%（{a.price:,.0f}），這段時間成交 {a.value:,.0f} 億")
            continue
        icon = "🔺" if a.move > 0 else "🔻"
        flow = "流入" if a.move > 0 else "流出"
        tail = f"（{a.price:,.2f}）".replace(".00）", "）") if a.kind == "個股" and a.price else ""
        lead = f"｜{'、'.join(a.leaders)}" if a.leaders else ""
        lines.append(f"{icon} {a.kind} {a.name}：資金{flow}，{mins}成交 {a.value:,.1f} 億{times}，{way} {abs(a.move):.1f}%{tail}{lead}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 圖

def _setup_fonts(plt) -> None:
    plt.rcParams["font.sans-serif"] = ["Noto Sans CJK TC", "Noto Sans CJK JP", "Microsoft JhengHei", "PingFang TC",
                                       "Heiti TC", "WenQuanYi Zen Hei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def _hours(t: str) -> float:
    return seconds(t) / 3600


def chart(path: Path, now: datetime, points: list[Point], prev: list[Point] | None, prev_day: str | None,
          prev_total: float | None, title: str = "今日盤中") -> Path:
    """上：累計成交金額（今天 vs 前一天）；下：盤中廣度（站上 20 日均線、站上日內均價線）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    _setup_fonts(plt)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(6.4, 10.4), dpi=150, gridspec_kw={"hspace": 0.42})
    fig.subplots_adjust(top=0.86, bottom=0.085, left=0.14, right=0.955)
    fig.text(0.04, 0.965, f"{title}｜即時 {now:%H:%M}", fontsize=17, fontweight="bold", color=TEXT, va="center")
    fig.text(0.955, 0.965, f"{now:%m/%d}", fontsize=11, color=TEXT_2, va="center", ha="right")
    ticks = [9, 10, 11, 12, 13]
    for ax in (ax1, ax2):
        ax.set_xlim(9, 13.52)
        ax.set_xticks(ticks, [f"{h:02d}:00" for h in ticks])
        ax.grid(True, color=GRID, linewidth=0.8)
        ax.tick_params(colors=TEXT_2, labelsize=10, length=0)
        for side in ax.spines.values():
            side.set_visible(False)

    last = points[-1] if points else None
    x = [_hours(p.t) for p in points]
    ax1.plot(x, [p.total for p in points], color=RED, linewidth=2.4, label=f"今日 {now:%m/%d}", zorder=3)
    if prev:
        label = f"前一日 {prev_day[5:7]}/{prev_day[8:10]}" if prev_day and len(prev_day) >= 10 else "前一日"
        ax1.plot([_hours(p.t) for p in prev], [p.total for p in prev], color=GRAY, linewidth=1.6, linestyle="--", label=label)
    ax1.set_ylim(bottom=0)
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax1.legend(loc="upper left", frameon=False, fontsize=10.5, labelcolor=TEXT)
    sub = f"目前 {last.total:,.0f} 億" if last else ""
    if last and prev_total:
        sub += f"　比昨同時 {(last.total / prev_total - 1) * 100:+.0f}%"
    ax1.text(0, 1.115, "累計成交金額（億）", transform=ax1.transAxes, fontsize=13.5, fontweight="bold", color=TEXT)
    ax1.text(0, 1.045, sub, transform=ax1.transAxes, fontsize=10.5, color=TEXT_2)

    ax2.plot(x, [p.ma20 for p in points], color=ORANGE, linewidth=2.2,
             label=f"站上 20MA　現 {_pct(last.ma20) if last else '—'}")
    ax2.plot(x, [p.vwap for p in points], color=BLUE, linewidth=2.2,
             label=f"站上均價線　現 {_pct(last.vwap) if last else '—'}")
    ax2.axhline(50, color=GRAY, linewidth=1, linestyle="--")
    ax2.set_ylim(0, 100)
    ax2.set_yticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    ax2.text(0, 1.115, "盤中廣度", transform=ax2.transAxes, fontsize=13.5, fontweight="bold", color=TEXT)
    ax2.text(0, 1.045, "均價線 >70% 買盤強　<30% 賣壓重", transform=ax2.transAxes, fontsize=10.5, color=TEXT_2)
    ax2.legend(loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=2, frameon=False, fontsize=11, labelcolor=TEXT)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path
