"""盤中即時網站（2026-11-26 使用者要求）：每次掃描整理「即時金流」與個股異常，推到網站 repo 的 live 分支，網頁約 1.5 分鐘讀一次。

使用者要的功能與定義（門檻在 config「盤中監控 → 即時網站」）：
  ・即時金流：全市場普通股累計成交金額（和昨天同一時間比）、最近 5 分鐘的成交金額、
    族群最近 15 分鐘的成交金額與漲跌（金額加權）、個股最近 5 分鐘成交金額排行
  ・異常拉抬／下跌：最近 live_window 分鐘漲跌超過 live_move %，而且這段時間成交至少 live_floor 億
  ・大單敲進／敲出：最近一筆成交的金額 ≥ big_order 萬元；成交價 ≥ 最佳賣價（或高於買賣價中間）＝往上敲進，
    ≤ 最佳買價（或低於中間）＝往下敲出；沒有買賣價時和上一次掃描的價格比（漲＝敲進、跌＝敲出）
  ・瞬間爆量：兩次掃描之間的成交金額 ≥ burst_floor 億，而且是平常的 burst_ratio 倍以上。
    「平常」＝近 20 日平均成交金額 × 這段時間占全天成交的比例（用前一個交易日官方的全市場每分鐘走勢算；
    開盤後半小時本來就占很多，2026-11-27 第一天用平均每分鐘算時，開盤爆量事件太多）。讀不到走勢時用平均每分鐘。
  ・13:25 以後是收盤集合競價（13:30 一次撮合），價格與成交一次跳出來，不算任何事件
    （11-27 第一天收盤那一次跳出 471 筆，把盤中的事件都洗掉了）。
限制：證交所即時行情每次只給「最近一筆成交」，兩次掃描之間（約 1 分鐘）的其他成交看不到，所以大單是抽樣；
網頁不能直接連證交所（瀏覽器會擋），資料先由 GitHub Actions 整理再推上去，網頁看到的會比盤中晚約 1～2 分鐘。

推送：live 分支只保留一個 commit（每次 --amend 後 force push），不會越存越大；網站發布只蓋 main，不會動到它。
"""
from __future__ import annotations

import json
import subprocess
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from .core import LAST_ALERT, OPEN, Session, Settings, minutes_since_open

TRADING_MINUTES = 270          # 09:00～13:30
KEEP_EVENTS = 150             # 每一類各留最近幾筆（爆量很多時不會把大單、異常漲跌洗掉）
FLOW_MINUTES = 15              # 族群金流看最近幾分鐘
STOCK_MINUTES = 5              # 個股金流排行看最近幾分鐘
WARMUP = 5                     # 開盤後幾分鐘內不判斷爆量與異常（集合競價那一筆很大）
KIND = {"up": "🚀 異常拉抬", "down": "💥 異常下跌", "buy": "🟥 大單敲進", "sell": "🟩 大單敲出", "burst": "🔥 瞬間爆量"}


def _r(x, d=2):
    return None if x is None else round(float(x), d)


class LiveBoard:
    """當天的即時看板：每次掃描呼叫 update()（找事件），再用 snapshot() 產生網頁要的資料。"""

    def __init__(self, s: Settings):
        self.s = s
        self.events: list[dict] = []
        self.last: dict[tuple[str, str], datetime] = {}
        self.quotes: dict = {}                       # 上一次掃描的報價（大單方向、同一筆成交不重複算）
        self.prev_close: dict[str, float] = {}
        self.history: deque = deque()                # (時間, {代號: (累計金額, 價格)})，保留約 FLOW_MINUTES 分鐘

    # -------------------------------------------------------------- 事件
    def _add(self, now: datetime, kind: str, code: str, name: str, price: float | None, text: str) -> dict | None:
        key = (kind, code)
        seen = self.last.get(key)
        if seen and now - seen < timedelta(minutes=self.s.live_cooldown):
            return None
        self.last[key] = now
        pc = self.prev_close.get(code)
        ev = {"t": f"{now:%H:%M:%S}", "kind": kind, "label": KIND[kind], "code": code, "name": name,
              "price": _r(price), "pct": _r((price / pc - 1) * 100) if price and pc else None, "text": text}
        self.events.append(ev)
        same = [i for i, e in enumerate(self.events) if e["kind"] == kind]
        if len(same) > KEEP_EVENTS:
            del self.events[same[0]]
        return ev

    @staticmethod
    def _share(curve: dict[str, float] | None, t0: datetime, t1: datetime) -> float | None:
        """t0～t1 這段時間占全天成交的比例（前一個交易日官方的全市場每分鐘累計成交金額）。"""
        if not curve:
            return None

        def at(hhmm):
            past = [v for t, v in curve.items() if t <= hhmm]
            return past[-1] if past else 0.0
        total = max(curve.values())
        part = at(f"{t1:%H%M}") - at(f"{t0:%H%M}")
        return part / total if total > 0 and part > 0 else None

    def _snap_before(self, now: datetime, minutes: float):
        target = now - timedelta(minutes=minutes)
        older = [h for h in self.history if h[0] <= target + timedelta(seconds=20)]
        return older[-1] if older else (self.history[0] if self.history else None)

    def update(self, session: Session, quotes: dict, now: datetime, curve: dict[str, float] | None = None) -> list[dict]:
        """curve：前一個交易日官方的全市場每分鐘累計成交金額 {HHMM: 億}，用來算「平常」這段時間該有多少成交。"""
        s, base = self.s, session.base
        current = {c: (t.value, t.price) for c, t in session.ticks.items()}
        for c, q in quotes.items():
            if q.prev_close:
                self.prev_close[c] = q.prev_close
        new: list[dict] = []
        prev_snap = self.history[-1] if self.history else None
        self.history.append((now, current))
        while self.history and now - self.history[0][0] > timedelta(minutes=FLOW_MINUTES + 2):
            self.history.popleft()
        warm = minutes_since_open(now) >= WARMUP
        today = f"{session.day:%Y%m%d}"
        if now.time() > LAST_ALERT:                                      # 收盤集合競價：只記錄，不算事件
            self.quotes = dict(quotes)
            return new

        for code, q in quotes.items():                                   # 大單
            st = base.stocks.get(code)
            if not st or q.day != today or not q.traded or not q.price or not q.trade_volume:
                continue
            old = self.quotes.get(code)
            if old and old.time == q.time and old.volume == q.volume:      # 沒有新的成交
                continue
            amount = q.trade_volume * 1000 * q.price
            if amount < s.big_order * 1e4:
                continue
            side = None
            if q.ask and q.price >= q.ask:
                side = "buy"
            elif q.bid and q.price <= q.bid:
                side = "sell"
            elif q.ask and q.bid:
                side = "buy" if q.price >= (q.ask + q.bid) / 2 else "sell"
            elif old and old.price:
                side = "buy" if q.price > old.price else "sell" if q.price < old.price else None
            if side:
                way = "往上敲進" if side == "buy" else "往下敲出"
                ev = self._add(now, side, code, st.name, q.price,
                               f"{way} {q.trade_volume:,.0f} 張（約 {amount / 1e4:,.0f} 萬）@ {q.price:,.2f}")
                if ev:
                    new.append(ev)
        self.quotes = {c: q for c, q in quotes.items()}

        if warm and prev_snap:                                            # 瞬間爆量
            span = (now - prev_snap[0]).total_seconds() / 60
            share = self._share(curve, prev_snap[0], now) or span / TRADING_MINUTES
            for code, (value, price) in current.items():
                v0 = prev_snap[1].get(code, (None, None))[0]
                st = base.stocks.get(code)
                if v0 is None or not st or span <= 0:
                    continue
                flow = value - v0
                usual = (st.v20 or 0) * 1e8 * share
                if flow >= s.burst_floor * 1e8 and usual > 0 and flow >= s.burst_ratio * usual:
                    ev = self._add(now, "burst", code, st.name, price,
                                   f"{span:.1f} 分鐘成交 {flow / 1e8:.2f} 億，是平常的 {flow / usual:.0f} 倍")
                    if ev:
                        new.append(ev)

        then = self._snap_before(now, s.live_window)                     # 異常拉抬／下跌
        if warm and then and then[0] < now:
            mins = (now - then[0]).total_seconds() / 60
            for code, (value, price) in current.items():
                v0, p0 = then[1].get(code, (None, None))
                st = base.stocks.get(code)
                if not st or not price or not p0 or v0 is None:
                    continue
                move = (price / p0 - 1) * 100
                flow = value - v0
                if abs(move) >= s.live_move and flow >= s.live_floor * 1e8:
                    kind = "up" if move > 0 else "down"
                    ev = self._add(now, kind, code, st.name, price,
                                   f"{mins:.0f} 分鐘{'拉抬' if move > 0 else '下跌'} {move:+.2f}%（{p0:,.2f}→{price:,.2f}），成交 {flow / 1e8:.2f} 億")
                    if ev:
                        new.append(ev)
        return new

    # -------------------------------------------------------------- 網頁資料
    def snapshot(self, session: Session, now: datetime, prev_total: float | None = None, status: str = "盤中") -> dict:
        base = session.base
        point = session.points[-1] if session.points else None
        current = self.history[-1][1] if self.history else {}

        def flows(minutes):
            then = self._snap_before(now, minutes)
            out = {}
            if not then:
                return out
            for code, (value, price) in current.items():
                v0, p0 = then[1].get(code, (0.0, None))
                out[code] = (value - v0, (price / p0 - 1) * 100 if price and p0 else 0.0)
            return out

        f5, f15 = flows(STOCK_MINUTES), flows(FLOW_MINUTES)
        stocks = sorted(((c, v, m) for c, (v, m) in f5.items() if v > 0), key=lambda x: -x[1])[:20]
        groups = []
        for g in base.own_groups + [g for g in base.groups if g not in base.own_groups]:
            members = [c for c in base.groups.get(g, []) if c in f15 and f15[c][0] > 0]
            value = sum(f15[c][0] for c in members)
            if value <= 0:
                continue
            move = sum(f15[c][0] * f15[c][1] for c in members) / value
            lead = sorted(members, key=lambda c: -f15[c][0])[:3]
            groups.append({"name": g, "own": g in base.own_groups, "value": _r(value / 1e8), "move": _r(move),
                           "leaders": [base.stocks[c].name for c in lead]})
        groups.sort(key=lambda x: -x["value"])
        flow5 = sum(v for v, _ in f5.values() if v > 0)
        return {
            "date": f"{session.day:%Y-%m-%d}", "time": f"{now:%H:%M:%S}", "status": status,
            "index": _r(point.index) if point else None, "total": _r(point.total, 1) if point else None,
            "total_prev": _r(prev_total, 1), "official": _r(point.official, 1) if point else None,
            "flow5": _r(flow5 / 1e8, 1), "vwap": point.vwap if point else None, "ma20": point.ma20 if point else None,
            "hi": point.hi if point else None, "lo": point.lo if point else None,
            "groups": groups[:12],
            "stocks": [{"code": c, "name": base.stocks[c].name, "value": _r(v / 1e8), "move": _r(m),
                        "price": _r(current[c][1]), "pct": _r((current[c][1] / self.prev_close[c] - 1) * 100)
                        if current[c][1] and self.prev_close.get(c) else None} for c, v, m in stocks],
            "events": list(reversed(self.events)),
            "settings": {"window": self.s.live_window, "move": self.s.live_move, "big": self.s.big_order,
                         "burst": self.s.burst_ratio, "interval": self.s.interval},
        }


class Publisher:
    """把即時資料推到這個 repo 的 live 分支（只保留一個 commit）。在背景執行，推送中又有新資料時跳過這一次。"""

    def __init__(self, root: Path, branch: str = "live", log=print):
        self.root, self.branch, self.log = root, branch, log
        self.dir = root / "live_wt"
        self.lock = threading.Lock()
        self.ready = False
        self.has_commit = False
        self.failed = 0

    def _git(self, *args, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-c", "user.name=github-actions[bot]",
                               "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", *args],
                              cwd=cwd or self.dir, capture_output=True, text=True, timeout=60)

    def _setup(self) -> None:
        if self.ready:
            return
        if not self.dir.exists():
            r = self._git("worktree", "add", "-q", "--detach", str(self.dir), cwd=self.root)
            if r.returncode:
                raise RuntimeError("git worktree 建立失敗")
            self._git("checkout", "-q", "--orphan", "live-page")
            self._git("rm", "-rq", "--cached", ".")
            self._git("clean", "-fdq")
        self.ready = True

    def _push(self, text: str) -> None:
        try:
            self._setup()
            (self.dir / "live.json").write_text(text, encoding="utf-8")
            self._git("add", "live.json")
            r = self._git("commit", "-q", "--amend" if self.has_commit else "--allow-empty", "-m", "盤中即時資料")
            if r.returncode == 0:
                self.has_commit = True
            r = self._git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{self.branch}")
            if r.returncode:
                raise RuntimeError("git push 失敗")
            self.failed = 0
        except Exception as e:                                         # 推不上去只影響網頁，不影響監控與通知
            self.failed += 1
            if self.failed in (1, 10):
                self.log(f"盤中即時網站的資料推送失敗（{type(e).__name__}：{e}），監控照常")
        finally:
            self.lock.release()

    def publish(self, data: dict, wait: bool = False) -> bool:
        if not self.lock.acquire(blocking=wait):
            return False                                               # 上一次還在推：跳過，下一次掃描再推
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if wait:
            self._push(text)
        else:
            threading.Thread(target=self._push, args=(text,), daemon=True).start()
        return True


def write_local(path: Path, data: dict) -> Path:
    """測試或本機執行：只寫檔，不推送。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path
