"""證交所「基本市況報導網站」（MIS，mis.twse.com.tw）的即時行情：上市、上櫃都有，約每 5 秒更新一次。

2026-11-14 在 GitHub Actions 上實測：
  ・個股：getStockInfo.jsp?ex_ch=tse_2330.tw|otc_6488.tw&json=1&delay=0，一次查 150 檔可以、200 檔會回「參數不足」，
    每次約 0.7 秒；連續 12 次沒有被擋。欄位：c 代號、n 名稱、d 日期、t 時間、z 成交價（這 5 秒沒有成交時是「-」）、
    v 累計成交量（張）、o／h／l 開高低、y 昨收（平盤價）、a／b 五檔賣價／買價、trade.z 最近一筆成交價、
    tv 最近一筆成交的張數（盤中即時網站的大單偵測用；依開源套件 twstock 的欄位對照）。
    沒有「累計成交金額」，所以個股的金額要自己用「成交量增加的部分 × 當時價格」累計。
  ・全市場：/stock/data/mis_ohlc_TSE.txt（上市）、mis_ohlc_OTC.txt（上櫃），ohlcArray 每分鐘一筆
    （ts 時間、c 指數、s 那一分鐘的成交金額，百萬元），infoArray[0] 有日期 d、指數 z、昨收 y、累計成交金額 v（百萬元）。
    收盤後到隔天開盤前，回的是前一個交易日的資料（盤中監控用它當「昨天同一時間」的基準）。
  ・個股的分鐘走勢（getOhlc.jsp?ch=2330.tw）回空白，查不到。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

HOST = "https://mis.twse.com.tw/stock"
BATCH = 120            # 一次查幾檔（上限約 150）
PAUSE = 0.4            # 兩次請求之間停幾秒，不要打太快
TIMEOUT = 20
RETRIES = 2
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
SERIES_FILE = {"tse": "mis_ohlc_TSE.txt", "otc": "mis_ohlc_OTC.txt"}


class MisError(Exception):
    """證交所即時行情讀不到或格式不對。"""


@dataclass
class Quote:
    code: str
    day: str                    # 這筆資料的日期，YYYYMMDD（休市日會是前一個交易日）
    time: str                   # HH:MM:SS
    price: float | None         # 目前價格（沒有成交時依序用最近一筆成交、買賣價中間）
    volume: float               # 累計成交量（張）
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None    # 昨收（除權息日是調整後的參考價）
    traded: bool = False        # 價格是不是真的成交價（不是用買賣價推的）
    trade_volume: float = 0.0   # 最近一筆成交的張數（tv；大單偵測用）
    ask: float | None = None    # 最佳賣價（五檔第一檔）
    bid: float | None = None    # 最佳買價


@dataclass
class MarketSeries:
    """全市場（上市或上櫃）一天的每分鐘走勢。"""
    day: str                                # YYYYMMDD
    index: float | None                     # 目前指數
    prev_index: float | None                # 昨收指數
    minutes: list[tuple[str, float, float]] = field(default_factory=list)   # (HHMM, 指數, 累計成交金額 億元)

    def value_at(self, hhmm: str) -> float | None:
        """到 hhmm 為止的累計成交金額（億元）；那個時間還沒有資料回傳 None。"""
        out = None
        for t, _, cum in self.minutes:
            if t > hhmm:
                break
            out = cum
        return out

    @property
    def total(self) -> float | None:
        return self.minutes[-1][2] if self.minutes else None


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _first(text) -> float | None:
    """五檔報價「27.35_27.40_…」的第一檔。"""
    return _num(str(text or "").split("_")[0]) if text else None


def parse_quotes(payload: dict) -> list[Quote]:
    if not isinstance(payload, dict) or not isinstance(payload.get("msgArray"), list):
        raise MisError(f"即時行情的格式不對（{str(payload)[:80]}）")
    out = []
    for r in payload["msgArray"]:
        code = str(r.get("c") or "").strip()
        if not code:
            continue
        price, traded = _num(r.get("z")), True
        if not price:                                   # 這 5 秒沒有成交：用最近一筆成交價
            price = _num((r.get("trade") or {}).get("z")) if isinstance(r.get("trade"), dict) else None
        if not price:                                   # 今天還沒有成交：用買賣價中間（只當參考，不算成交）
            bid, ask = _first(r.get("b")), _first(r.get("a"))
            price, traded = ((bid + ask) / 2 if bid and ask else None), False
        out.append(Quote(code=code, day=str(r.get("d") or ""), time=str(r.get("t") or ""), price=price or None,
                         volume=_num(r.get("v")) or 0.0, open=_num(r.get("o")) or None, high=_num(r.get("h")) or None,
                         low=_num(r.get("l")) or None, prev_close=_num(r.get("y")) or None, traded=traded and bool(price),
                         trade_volume=_num(r.get("tv")) or 0.0, ask=_first(r.get("a")) or None, bid=_first(r.get("b")) or None))
    return out


def _get(session, url: str, params: dict | None = None):
    last = None
    for attempt in range(RETRIES + 1):
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code != 200:
                raise MisError(f"HTTP {resp.status_code}")
            if not resp.text.strip():
                raise MisError("回應是空的")
            return resp.json()
        except (requests.RequestException, ValueError, MisError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise MisError(f"證交所即時行情讀不到（{type(last).__name__}：{last}）")


def fetch_quotes(symbols: list[tuple[str, str]], session=None, pause: float = PAUSE) -> tuple[dict[str, Quote], list[str]]:
    """symbols：[(市場 tse／otc, 代號)]。回傳 {代號: Quote} 與讀取失敗的說明（某一批失敗不影響其他批）。"""
    session = session or requests.Session()
    quotes, problems = {}, []
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        ex_ch = "|".join(f"{m}_{c}.tw" for m, c in chunk)
        try:
            payload = _get(session, f"{HOST}/api/getStockInfo.jsp", {"ex_ch": ex_ch, "json": "1", "delay": "0"})
            for q in parse_quotes(payload):
                quotes[q.code] = q
        except MisError as e:
            problems.append(f"第 {i // BATCH + 1} 批（{chunk[0][1]}～{chunk[-1][1]}）：{e}")
        if pause and i + BATCH < len(symbols):
            time.sleep(pause)
    return quotes, problems


def parse_series(payload: dict) -> MarketSeries:
    if not isinstance(payload, dict) or not isinstance(payload.get("ohlcArray"), list):
        raise MisError(f"全市場走勢的格式不對（{str(payload)[:80]}）")
    info = (payload.get("infoArray") or [{}])[0]
    day = str(info.get("d") or str(payload.get("lastDatetime") or "")[:8])
    minutes, cum = [], 0.0
    for r in payload["ohlcArray"]:
        ts = str(r.get("ts") or "")
        if len(ts) < 4:
            continue
        cum += (_num(r.get("s")) or 0.0) / 100          # 百萬元 → 億元
        minutes.append((ts[:4], _num(r.get("c")) or 0.0, round(cum, 2)))
    return MarketSeries(day=day, index=_num(info.get("z")) or (minutes[-1][1] if minutes else None),
                        prev_index=_num(info.get("y")), minutes=minutes)


def fetch_series(ex: str, session=None) -> MarketSeries:
    """ex：tse（上市）或 otc（上櫃）。"""
    session = session or requests.Session()
    return parse_series(_get(session, f"{HOST}/data/{SERIES_FILE[ex]}"))
