"""盤中的計算：每檔股票的累計成交金額與均價、站上均線的比例、大型 vs 中小型、日內均價線、
60 日新高新低、族群資金（和昨天同一時間比）、異常大筆資金偵測、每天的盤中紀錄。

名詞與算法（寫給使用者看的版本在 README.md）：
  ・成交金額：證交所即時行情只有個股的「累計成交量」，沒有金額，所以每次掃描用
    「成交量增加的部分 × 這段時間的價格（前後兩次的平均）」累計。掃描間隔約 1.5 分鐘，誤差通常在 1% 以內。
  ・站上 N 日均線：現價 ≥（前 N−1 天收盤合計 ＋ 現價）÷ N，也就是現價 ≥ 前 N−1 天的平均收盤。
  ・日內均價線：今天的累計成交金額 ÷ 累計成交量；現價在它之上＝站上。只算今天有成交的股票。
  ・60 日新高／新低：今天盤中最高價 > 前 60 個交易日的最高價／最低價 < 前 60 日最低價。
  ・大型：市值前 N 名（設定檔「大型股檔數」），其餘是中小型；比較的是站上 20 日均線的比例。
  ・族群資金流入／流出：族群今天到現在的成交金額，和昨天同一時間比的增減百分比。
"""
from __future__ import annotations

import gzip
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

OPEN = time(9, 0)
CLOSE = time(13, 30)
LAST_ALERT = time(13, 24)          # 13:25 以後是收盤前的集合競價，13:30 一次撮合的量很大，不算異常
EARLY_MINUTES = 3                  # 開盤 3 分鐘內第一次看到的成交量，幾乎都是開盤那一筆，用開盤價算金額
EX_RIGHT = 0.002                   # 昨收和基準資料的收盤差超過 0.2%＝今天除權息（或基準資料不是前一個交易日的）
STALE_SHARE = 0.3                  # 超過三成的股票昨收對不上＝基準資料過期，不是除權息
MIN_INDUSTRY = 3                   # 官方產業至少要有幾檔才列入族群
MIN_GROUP = 2                      # 族群至少要有幾檔才列入資金流向
KEEP_DAYS = 20                     # 盤中紀錄保留幾天


@dataclass
class Settings:
    """設定檔 config.yaml「盤中監控」（發布網站時寫成 intraday/settings.json）。金額單位：億元；漲跌單位：%。"""
    times: list[str] = field(default_factory=lambda: ["09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30",
                                                      "13:00", "13:30"])
    interval: int = 90
    large_top: int = 150
    group_top: int = 5
    group_floor: float = 1.0
    alerts: bool = True
    window: int = 5
    warmup: int = 10
    stock_value: float = 3.0
    stock_ratio: float = 4.0
    stock_move: float = 1.0
    group_value: float = 10.0
    group_ratio: float = 3.0
    group_move: float = 0.5
    index_move: float = 0.5
    cooldown: int = 30
    # 盤中即時網站（2026-11-26 使用者要求）：金額單位見各欄位
    live: bool = True
    live_window: int = 5              # 異常拉抬／下跌：看最近幾分鐘
    live_move: float = 2.0            # 漲跌超過幾 %
    live_floor: float = 0.1           # 這幾分鐘至少成交幾億（太冷門的不算）
    big_order: float = 500.0          # 大單：一筆成交至少幾萬元
    burst_ratio: float = 5.0          # 瞬間爆量：這次掃描間的成交金額是平常的幾倍
    burst_floor: float = 0.3          # 瞬間爆量：至少幾億
    live_cooldown: int = 10           # 同一檔同一種事件，隔幾分鐘才再記一次

    @classmethod
    def load(cls, path: Path) -> "Settings":
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        return cls(**known)


@dataclass
class Stock:
    code: str
    name: str
    market: str                     # tse／otc
    industry: str
    close: float | None             # 基準資料那一天的收盤價
    s19: float | None
    s59: float | None
    s199: float | None
    h60: float | None
    l60: float | None
    cap: float | None
    big: bool = False
    scale: float = 1.0              # 今天除權息時，均線與高低點要乘上的比例
    v20: float | None = None        # 近 20 日平均成交值（億元，瞬間爆量的「平常」）


@dataclass
class Base:
    date: str
    stocks: dict[str, Stock]
    groups: dict[str, list[str]]    # 族群 → 代號（使用者的族群在前面，官方產業在後面）
    own_groups: list[str]           # 使用者自己的族群名稱
    stale: bool = False

    def symbols(self) -> list[tuple[str, str]]:
        return [(s.market, s.code) for s in self.stocks.values()]


def load_base(raw: dict, large_top: int) -> Base:
    stocks = {}
    for code, r in (raw.get("stocks") or {}).items():
        stocks[code] = Stock(code=code, name=r.get("n") or code, market=r.get("m") or "tse", industry=r.get("i") or "",
                             close=r.get("c"), s19=r.get("s19"), s59=r.get("s59"), s199=r.get("s199"),
                             h60=r.get("h60"), l60=r.get("l60"), cap=r.get("cap"), v20=r.get("v20"))
    if not stocks:
        raise ValueError("基準資料裡沒有任何股票")
    ranked = sorted((s for s in stocks.values() if s.cap), key=lambda s: -s.cap)
    for s in ranked[:large_top]:
        s.big = True
    groups = {name: [c for c in codes if c in stocks] for name, codes in (raw.get("groups") or {}).items()}
    groups = {name: codes for name, codes in groups.items() if len(codes) >= MIN_GROUP}
    own = list(groups)
    short = raw.get("short") or {}
    by_industry: dict[str, list[str]] = {}
    for s in stocks.values():
        if s.industry:
            by_industry.setdefault(s.industry, []).append(s.code)
    for ind, codes in sorted(by_industry.items()):
        name = short.get(ind, ind)
        if name in groups:                              # 和使用者的族群同名（例如「網通」）：官方產業用全名
            name = ind
        if len(codes) >= MIN_INDUSTRY and name not in groups:
            groups[name] = codes
    return Base(date=str(raw.get("date") or ""), stocks=stocks, groups=groups, own_groups=own)


@dataclass
class Tick:
    """一檔股票今天到目前為止的狀態。"""
    price: float | None = None
    volume: float = 0.0             # 累計成交量（張）
    value: float = 0.0              # 累計成交金額（元，估算）
    high: float | None = None
    low: float | None = None


@dataclass
class Point:
    """一次掃描的結果（存進盤中紀錄，隔天當成「昨天同一時間」的基準）。"""
    t: str                          # HH:MM:SS
    total: float                    # 上市櫃普通股累計成交金額（億元）
    ma20: float | None = None       # 站上 20 日均線的比例（%）
    ma60: float | None = None
    ma200: float | None = None
    big: float | None = None        # 大型股站上 20 日均線的比例
    small: float | None = None
    vwap: float | None = None       # 站上日內均價線的比例
    hi: int = 0                     # 60 日新高檔數
    lo: int = 0
    n: int = 0                      # 有報價的檔數
    index: float | None = None      # 加權指數
    official: float | None = None   # 全市場（上市＋上櫃，含 ETF）累計成交金額，官方數字
    groups: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "groups"}
        d["groups"] = {k: round(v, 3) for k, v in self.groups.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Point":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def seconds(t: str) -> int:
    """HH:MM 或 HH:MM:SS → 從午夜起算的秒數。"""
    parts = [int(x) for x in t.split(":")] + [0]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def minutes_since_open(t: time | datetime) -> float:
    return (t.hour - OPEN.hour) * 60 + (t.minute - OPEN.minute) + t.second / 60


class Session:
    """今天的盤中狀態。每次掃描呼叫 update()，再用 measure() 算出一個 Point。"""

    def __init__(self, base: Base, day: date, window: int = 5, interval: int = 90):
        self.base = base
        self.day = day
        self.ticks: dict[str, Tick] = {}
        self.points: list[Point] = []
        self.snaps: deque = deque(maxlen=max(3, window * 60 // max(interval, 1) + 3))   # (時間, {代號: (金額, 價格)})
        self.late: str | None = None        # 不是從開盤就開始監控：第一次掃描的時間
        self.checked = False                # 除權息／基準資料過期判斷過了沒
        # 逐筆紀錄（當沖回測用，2026-11-26）：使用者族群的股票每次掃描的價格、累計張數、累計金額、當天高低
        self.tape_codes = {c for g in base.own_groups for c in base.groups.get(g, [])}
        self.tape: dict[str, list] = {}

    def _calibrate(self, quotes: dict) -> None:
        """第一次拿到今天的報價時：昨收和基準收盤對不上的股票，今天除權息，均線與高低點依比例調整。"""
        off = {}
        for code, q in quotes.items():
            s = self.base.stocks.get(code)
            if s and s.close and q.prev_close and abs(q.prev_close / s.close - 1) > EX_RIGHT:
                off[code] = q.prev_close / s.close
        known = sum(1 for c, q in quotes.items() if c in self.base.stocks and q.prev_close)
        if known and len(off) / known > STALE_SHARE:
            self.base.stale = True                      # 大部分都對不上：基準資料不是前一個交易日的，不調整
        else:
            for code, ratio in off.items():
                self.base.stocks[code].scale = ratio
        self.checked = True

    def update(self, quotes: dict, now: datetime) -> int:
        """quotes：{代號: mis.Quote}。只收今天的報價；回傳今天有報價的檔數。"""
        today = f"{self.day:%Y%m%d}"
        fresh = {c: q for c, q in quotes.items() if q.day == today and c in self.base.stocks}
        if not fresh:
            return 0
        if not self.checked:
            self._calibrate(fresh)
            if minutes_since_open(now) > EARLY_MINUTES + 2:
                self.late = f"{now:%H:%M}"
        elapsed = minutes_since_open(now)
        for code, q in fresh.items():
            t = self.ticks.get(code)
            first = t is None
            if first:
                t = self.ticks[code] = Tick()
            price = q.price or t.price or q.prev_close
            added = q.volume - t.volume
            if added > 0 and price:
                if not first and t.price:
                    ref = (t.price + price) / 2             # 這段時間的價格：前後兩次的平均
                elif elapsed <= EARLY_MINUTES:
                    ref = q.open or price                   # 剛開盤：幾乎都是開盤那一筆
                else:                                       # 中途才開始監控：用開高低收的平均估計
                    known = [x for x in (q.open, q.high, q.low, price) if x]
                    ref = sum(known) / len(known)
                t.value += added * 1000 * ref
                t.volume = q.volume
            t.price = price
            t.high = q.high or t.high
            t.low = q.low or t.low
            if code in self.tape_codes and q.traded and price:
                self.tape.setdefault(code, []).append([f"{now:%H:%M:%S}", price, q.volume, round(t.value), t.high, t.low])
        self.snaps.append((now, {c: (t.value, t.price) for c, t in self.ticks.items()}))
        return len(fresh)

    def measure(self, now: datetime, index: float | None = None, official: float | None = None) -> Point:
        above = {20: 0, 60: 0, 200: 0}
        count = {20: 0, 60: 0, 200: 0}
        size = {True: [0, 0], False: [0, 0]}             # 大型／中小型：[站上 20 日線, 有資料]
        vw_up = vw_n = hi = lo = 0
        total = 0.0
        for code, t in self.ticks.items():
            s = self.base.stocks[code]
            total += t.value
            p = t.price
            if not p:
                continue
            for n, total_prev in ((20, s.s19), (60, s.s59), (200, s.s199)):
                if total_prev:
                    count[n] += 1
                    ok = p >= total_prev * s.scale / (n - 1)
                    above[n] += ok
                    if n == 20:
                        size[s.big][1] += 1
                        size[s.big][0] += ok
            if t.volume > 0 and t.value > 0:
                vw_n += 1
                vw_up += p >= t.value / (t.volume * 1000) * (1 - 1e-6)
            if s.h60 and t.high and t.high > s.h60 * s.scale:
                hi += 1
            if s.l60 and t.low and t.low < s.l60 * s.scale:
                lo += 1
        pct = lambda a, b: round(100 * a / b, 1) if b else None       # noqa: E731
        groups = {g: sum(self.ticks[c].value for c in codes if c in self.ticks) / 1e8 for g, codes in self.base.groups.items()}
        point = Point(t=f"{now:%H:%M:%S}", total=round(total / 1e8, 2), ma20=pct(above[20], count[20]),
                      ma60=pct(above[60], count[60]), ma200=pct(above[200], count[200]),
                      big=pct(*size[True]), small=pct(*size[False]), vwap=pct(vw_up, vw_n), hi=hi, lo=lo,
                      n=len(self.ticks), index=index, official=official, groups=groups)
        self.points.append(point)
        return point


# ------------------------------------------------------------------ 昨天同一時間

def value_at(points: list[Point], t: str, group: str | None = None) -> float | None:
    """盤中紀錄在時間 t 的累計成交金額（線性內插）。group 沒給＝全部普通股。還沒有紀錄的時間回傳 None。"""
    if not points:
        return None
    get = (lambda p: p.groups.get(group)) if group else (lambda p: p.total)
    target = seconds(t)
    prev = None
    for p in points:
        cur = seconds(p.t)
        v = get(p)
        if v is None:
            continue
        if cur >= target:
            if cur == target or prev is None:
                return v if cur == target else None
            (t0, v0) = prev
            return v0 + (v - v0) * (target - t0) / (cur - t0)
        prev = (cur, v)
    return prev[1] if prev else None


def group_flows(point: Point, prev: list[Point], floor: float, top: int) -> tuple[list, list] | None:
    """族群今天到現在的成交金額，和昨天同一時間比。回傳（流入, 流出），每筆是（族群, 增減比例）；沒有昨天的紀錄回傳 None。"""
    if not prev:
        return None
    rows = []
    for g, today in point.groups.items():
        before = value_at(prev, point.t, g)
        if before is None or before < floor:            # 基期太小，百分比沒有意義
            continue
        rows.append((g, today / before - 1))
    rows.sort(key=lambda r: -r[1])
    return [r for r in rows if r[1] > 0][:top], [r for r in rows[::-1] if r[1] < 0][:top]


# ------------------------------------------------------------------ 異常大筆資金

@dataclass
class Alert:
    kind: str                       # 個股／族群／大盤
    key: str
    name: str
    value: float                    # 這幾分鐘的成交金額（億元）
    ratio: float | None             # 是平常速度的幾倍（None＝之前幾乎沒有成交）
    move: float                     # 這幾分鐘的漲跌（%）
    minutes: float
    price: float | None = None
    leaders: list[str] = field(default_factory=list)


class Detector:
    """每次掃描檢查最近幾分鐘的成交金額有沒有突然放大。同一檔通知過後，隔一段時間才會再通知。"""

    def __init__(self, s: Settings):
        self.s = s
        self.last: dict[str, datetime] = {}

    def _allowed(self, key: str, now: datetime) -> bool:
        seen = self.last.get(key)
        return seen is None or now - seen >= timedelta(minutes=self.s.cooldown)

    def check(self, session: Session, prev: list[Point], now: datetime) -> list[Alert]:
        s = self.s
        if not s.alerts or len(session.snaps) < 2 or now.time() > LAST_ALERT:
            return []
        target = now - timedelta(minutes=s.window)
        older = [snap for snap in session.snaps if snap[0] <= target + timedelta(seconds=s.interval / 2)]
        if not older:
            return []
        then, before = older[-1]
        elapsed = minutes_since_open(then)
        span = (now - then).total_seconds() / 60
        if elapsed < s.warmup or span <= 0:
            return []
        current = session.snaps[-1][1]
        moves, flows = {}, {}
        for code, (value, price) in current.items():
            v0, p0 = before.get(code, (0.0, None))
            flows[code] = value - v0
            moves[code] = (price / p0 - 1) * 100 if price and p0 else 0.0
        alerts = []
        for code, flow in flows.items():                    # 個股
            v0 = before.get(code, (0.0, None))[0]
            usual = v0 / elapsed * span
            if flow < s.stock_value * 1e8 or abs(moves[code]) < s.stock_move or flow < s.stock_ratio * usual:
                continue
            st = session.base.stocks[code]
            alerts.append(Alert("個股", code, f"{code} {st.name}", flow / 1e8, flow / usual if usual > 0 else None,
                                moves[code], span, current[code][1]))
        for g, codes in session.base.groups.items():        # 族群
            members = [c for c in codes if flows.get(c, 0) > 0]
            flow = sum(flows[c] for c in members)
            if flow < s.group_value * 1e8:
                continue
            usual = sum(before.get(c, (0.0, None))[0] for c in codes) / elapsed * span
            t_now, t_then = f"{now:%H:%M:%S}", f"{then:%H:%M:%S}"
            y_now, y_then = value_at(prev, t_now, g), value_at(prev, t_then, g)
            if y_now is not None and y_then is not None:        # 有昨天的紀錄：也要比昨天同一時段多
                usual = max(usual, (y_now - y_then) * 1e8)
            move = sum(flows[c] * moves[c] for c in members) / flow
            if abs(move) < s.group_move or flow < s.group_ratio * usual:
                continue
            lead = sorted(members, key=lambda c: -flows[c])[:3]
            alerts.append(Alert("族群", f"族群:{g}", g, flow / 1e8, flow / usual if usual > 0 else None, move, span,
                                leaders=[session.base.stocks[c].name for c in lead]))
        idx = [(p.t, p.index) for p in session.points if p.index]
        if len(idx) >= 2:                                   # 大盤：加權指數這幾分鐘的漲跌
            t_then = f"{then:%H:%M:%S}"
            past = [v for t, v in idx if t <= t_then]
            if past and abs(idx[-1][1] / past[-1] - 1) * 100 >= s.index_move:
                tot = [p.total for p in session.points if p.t <= t_then]
                flow = session.points[-1].total - (tot[-1] if tot else 0.0)
                alerts.append(Alert("大盤", "大盤", "加權指數", flow, None, (idx[-1][1] / past[-1] - 1) * 100, span, idx[-1][1]))
        out = []
        for kind, limit in (("大盤", 1), ("族群", 5), ("個股", 5)):
            picked = sorted((a for a in alerts if a.kind == kind and self._allowed(a.key, now)), key=lambda a: -a.value)
            for a in picked[:limit]:
                self.last[a.key] = now
                out.append(a)
        return out


# ------------------------------------------------------------------ 盤中紀錄（隔天當基準）

def save_history(folder: Path, day: date, base_date: str, points: list[Point], series: dict | None = None) -> Path:
    """每次掃描後存檔（中途當掉也留得住）。series：官方的全市場每分鐘累計成交金額 {HHMM: 億元}。"""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day:%Y-%m-%d}.json"
    payload = {"date": f"{day:%Y-%m-%d}", "base": base_date, "points": [p.to_dict() for p in points], "official": series or {}}
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for old in sorted(folder.glob("20??-??-??.json"))[:-KEEP_DAYS]:
        old.unlink()
    return path


def save_tape(folder: Path, day: date, tape: dict[str, list]) -> Path | None:
    """當沖回測用的逐筆紀錄存成 YYYY-MM-DD.json.gz（收盤後由 workflow 推到網站 repo 的 intraday-records 分支）。"""
    if not tape:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day:%Y-%m-%d}.json.gz"
    tmp = path.with_suffix(".tmp")
    payload = {"date": f"{day:%Y-%m-%d}", "fields": ["時間", "價格", "累計張數", "累計金額", "當天最高", "當天最低"], "stocks": tape}
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)
    return path


def load_history(path: Path) -> tuple[str, list[Point], dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return d.get("date", path.stem), [Point.from_dict(p) for p in d.get("points", [])], d.get("official") or {}


def previous_history(folder: Path, day: date) -> tuple[str, list[Point], dict] | None:
    """今天以前最近一天的盤中紀錄。"""
    files = [f for f in sorted(folder.glob("20??-??-??.json")) if f.stem < f"{day:%Y-%m-%d}"] if folder.is_dir() else []
    for f in reversed(files):
        try:
            got = load_history(f)
            if got[1]:
                return got
        except (ValueError, KeyError, TypeError):
            continue
    return None
