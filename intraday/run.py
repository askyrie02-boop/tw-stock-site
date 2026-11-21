"""盤中監控主程式：python -m intraday.run（在公開的網站 repo 由 GitHub Actions 執行）。

流程：開盤前確認今天有沒有開盤 → 09:00 起每隔一段時間把全部上市櫃普通股掃一次 →
到了設定的時間推送一則盤中狀況（附圖）→ 每次掃描檢查有沒有異常的大筆資金，有就馬上通知 → 13:30 收盤後結束。

  python -m intraday.run             正式執行（盤中時間以外會直接結束）
  python -m intraday.run --test      測試：馬上掃一次、推送一則測試通知（收盤後或假日也可以跑，用最近一個交易日的資料）
  python -m intraday.run --dry-run   不推送，只把訊息印出來、把圖存到 intraday_output/
"""
from __future__ import annotations

import argparse
import json
import sys
import time as clock
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import mis, report
from .core import CLOSE, OPEN, Detector, Session, Settings, group_flows, load_base, previous_history, save_history, value_at
from .notify import NotifyError, Telegram

TZ = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent.parent
BASE_FILE = ROOT / "assets" / "intraday_base.json"      # 每檔股票的均線、60 日高低點（私人 repo 每天收盤後更新）
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"
HISTORY = ROOT / "intraday_data"                        # 每天的盤中紀錄（GitHub Actions 的快取保留）
OUTPUT = ROOT / "intraday_output"
HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
FIRST_SCAN = time(9, 0, 20)         # 開盤後 20 秒做第一次掃描
GIVE_UP = time(9, 20)               # 到這個時間證交所還沒有今天的報價＝今天沒有開盤（颱風等臨時休市）
FINAL_DELAY = 100                   # 收盤後幾秒做最後一次掃描（等 13:30 的收盤撮合出來）
MAX_FAILS = 15                      # 連續幾次完全讀不到報價就放棄


def log(text: str) -> None:
    print(f"[{datetime.now(TZ):%H:%M:%S}] {text}", flush=True)


def is_holiday(day: date, http) -> bool | None:
    """證交所公告的休市日。查不到回傳 None（之後再用「有沒有今天的報價」判斷）。"""
    try:
        rows = http.get(HOLIDAY_URL, timeout=20, headers=mis.HEADERS).json()
        roc = f"{day.year - 1911}{day:%m%d}"
        # 清單裡也有「開始交易日」「最後交易日」這類有開盤的日子，要排除
        return any(str(r.get("Date")) == roc and "交易日" not in str(r.get("Name", "")) for r in rows)
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None


def official_curve(http, day: str | None = None) -> tuple[dict[str, float], float | None, str] | None:
    """官方的全市場（上市＋上櫃）每分鐘累計成交金額 {HHMM: 億元}、加權指數、資料日期。讀不到回傳 None。"""
    try:
        tse, otc = mis.fetch_series("tse", http), mis.fetch_series("otc", http)
    except mis.MisError as e:
        log(f"全市場走勢讀不到：{e}")
        return None
    if day and tse.day != day:
        return None
    curve = {}
    for hhmm, _, cum in tse.minutes:
        curve[hhmm] = round(cum + (otc.value_at(hhmm) or 0.0), 2)
    return curve, tse.index, tse.day


def curve_at(curve: dict[str, float], hhmm: str) -> float | None:
    past = [v for t, v in curve.items() if t <= hhmm]
    return past[-1] if past else None


class Monitor:
    def __init__(self, settings: Settings, notifier: Telegram | None, dry_run: bool = False):
        self.s = settings
        self.notifier = notifier
        self.dry_run = dry_run
        self.http = requests.Session()
        self.prev_day: str | None = None            # 前一天盤中紀錄的日期
        self.prev: list = []                        # 前一天的盤中紀錄（core.Point）
        self.prev_official: dict[str, float] = {}   # 前一個交易日官方的全市場每分鐘累計成交金額
        self.today_official: dict[str, float] = {}

    # -------------------------------------------------------------- 推送
    def send(self, text: str, image: Path | None = None) -> None:
        print(text, flush=True)
        if self.dry_run or self.notifier is None:
            return
        try:
            if image:
                self.notifier.send_photo(image, text)
            else:
                self.notifier.send_text(text)
        except NotifyError as e:
            log(f"❌ 推送失敗：{e}")

    # -------------------------------------------------------------- 一次掃描
    def scan(self, session: Session, now: datetime, any_day: bool = False):
        quotes, problems = mis.fetch_quotes(session.base.symbols(), self.http)
        if any_day and quotes:                       # 測試：用報價裡最多的那個日期（假日是前一個交易日）
            common = Counter(q.day for q in quotes.values()).most_common(1)[0][0]
            session.day = datetime.strptime(common, "%Y%m%d").date()
        n = session.update(quotes, now)
        for p in problems[:3]:
            log(f"讀不到報價：{p}")
        return n, len(problems)

    def notes(self, session: Session, first: bool) -> list[str]:
        out = []
        if session.late:
            out.append(f"{session.late} 才開始監控，之前的成交金額是估計值")
        if first and session.base.stale:
            d = session.base.date
            out.append(f"均線基準是 {d[5:7]}/{d[8:10]} 的資料（不是前一個交易日），站上均線的比例可能不準")
        return out

    def summary(self, session: Session, now: datetime, label: str, head: str = "🕐【盤中定時】",
                extra: list[str] | None = None, first: bool = False, title: str = "今日盤中") -> None:
        point = session.points[-1]
        prev_total = value_at(self.prev, point.t)
        flows = group_flows(point, self.prev, self.s.group_floor, self.s.group_top)
        text = report.summary(label, point, prev_total, flows, self.notes(session, first) + (extra or []), head=head,
                              official_prev=curve_at(self.prev_official, f"{now:%H%M}"))
        image = None
        try:
            image = report.chart(OUTPUT / f"intraday_{now:%H%M}.png", now, session.points, self.prev, self.prev_day,
                                 prev_total, title=title)
        except Exception as e:                       # 圖畫不出來不影響文字通知
            log(f"圖沒有產生（{type(e).__name__}：{e}）")
        self.send(text, image)

    # -------------------------------------------------------------- 正式執行
    def load_previous(self, today: date) -> None:
        got = previous_history(HISTORY, today)
        if got:
            self.prev_day, self.prev, self.prev_official = got
            log(f"前一天的盤中紀錄：{self.prev_day}（{len(self.prev)} 次掃描）")
        else:
            log("還沒有前一天的盤中紀錄：族群資金（vs 昨同時）下一個交易日才有")

    def run(self, base) -> int:
        s = self.s
        now = datetime.now(TZ)
        today = now.date()
        self.load_previous(today)
        if now.time() < OPEN:                        # 開盤前，官方的全市場走勢還是前一個交易日的：留下來當基準
            got = official_curve(self.http)
            if got and got[2] != f"{today:%Y%m%d}":
                self.prev_official = got[0]
                log(f"官方的全市場走勢（前一個交易日 {got[2]}）：收盤累計 {curve_at(got[0], '9999'):,.0f} 億")
        session = Session(base, today, s.window, s.interval)
        detector = Detector(s)
        due = [t for t in s.times if datetime.combine(today, time.fromisoformat(t), TZ) > now - timedelta(minutes=5)]
        final = datetime.combine(today, CLOSE, TZ) + timedelta(seconds=FINAL_DELAY)
        next_scan = max(now, datetime.combine(today, FIRST_SCAN, TZ))
        fails, sent = 0, 0
        while True:
            now = datetime.now(TZ)
            if now < next_scan:
                clock.sleep(min(30, (next_scan - now).total_seconds()))
                continue
            n, bad = self.scan(session, now)
            if n == 0:
                fails += 1
                if not session.ticks and now.time() >= GIVE_UP:
                    log("證交所沒有今天的報價，今天沒有開盤")
                    self.send(f"ℹ️ 今天（{today:%m/%d}）證交所沒有開盤的報價（臨時休市？），盤中監控不執行")
                    return 0
                if fails >= MAX_FAILS:
                    raise mis.MisError(f"連續 {fails} 次讀不到證交所即時行情")
            else:
                fails = 0
                got = official_curve(self.http, f"{today:%Y%m%d}")
                index = None
                if got:
                    self.today_official, index = got[0], got[1]
                point = session.measure(now, index, curve_at(self.today_official, "9999"))
                save_history(HISTORY, today, base.date, session.points, self.today_official)
                log(f"掃描 {n} 檔（{bad} 批失敗）成交 {point.total:,.0f} 億｜20MA {point.ma20}%｜均價線 {point.vwap}%"
                    f"｜新高 {point.hi} 新低 {point.lo}")
                alerts = detector.check(session, self.prev, now)
                if alerts:
                    self.send(report.alert_text(alerts, now))
                while due and now >= self._due_time(today, due[0], final):
                    label = due.pop(0)
                    closing = time.fromisoformat(label) >= CLOSE
                    self.summary(session, now, label + ("（收盤）" if closing else ""), first=sent == 0)
                    sent += 1
            if now >= final and not due:
                log("收盤，盤中監控結束")
                return 0
            upcoming = [self._due_time(today, t, final) for t in due] + [final]
            next_scan = min([now + timedelta(seconds=s.interval)] + [t for t in upcoming if t > now])

    @staticmethod
    def _due_time(today: date, hhmm: str, final: datetime) -> datetime:
        """通知時間；收盤那一次要等收盤撮合出來。"""
        t = datetime.combine(today, time.fromisoformat(hhmm), TZ)
        return final if t.time() >= CLOSE else t

    # -------------------------------------------------------------- 測試
    def test(self, base) -> int:
        """馬上掃一次並推送（不管現在幾點）。假日或收盤後用最近一個交易日的資料，成交金額只是粗估。"""
        now = datetime.now(TZ)
        session = Session(base, now.date(), self.s.window, self.s.interval)
        n, bad = self.scan(session, now, any_day=True)
        if n == 0:
            raise mis.MisError("測試時讀不到任何報價")
        got = official_curve(self.http)
        point = session.measure(now, got[1] if got else None, curve_at(got[0], "9999") if got else None)
        live = session.day == now.date() and OPEN <= now.time() <= CLOSE
        note = (f"這是測試通知：掃到 {n} 檔（{bad} 批失敗），" +
                ("盤中資料。" if live else f"用的是 {session.day:%m/%d} 收盤的資料，成交金額是用開高低收粗估的。"))
        if got:
            note += f"官方的全市場成交金額（含 ETF）{curve_at(got[0], '9999'):,.0f} 億。"
        session.late = None                          # 測試不和前一天比（時間對不上），也不用提醒「中途才開始」
        self.summary(session, now, f"{now:%H:%M}", head="🧪【測試】", extra=[note], title="測試")
        log(f"測試完成：{point.total:,.0f} 億、有報價 {point.n} 檔")
        return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="台股盤中監控")
    p.add_argument("--test", action="store_true", help="馬上掃一次並推送一則測試通知（不管現在幾點）")
    p.add_argument("--dry-run", action="store_true", help="不推送，只印出訊息、把圖存到 intraday_output/")
    args = p.parse_args(argv)
    now = datetime.now(TZ)
    notifier = Telegram.from_env()
    if notifier is None and not args.dry_run:
        print("::error::還沒有設定 Telegram 金鑰：請到這個 repo 的 Settings → Secrets and variables → Actions，"
              "新增 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID（和私人 repo 的值一樣）")
        return 1
    settings = Settings.load(SETTINGS_FILE) if SETTINGS_FILE.exists() else Settings()
    monitor = Monitor(settings, notifier, args.dry_run)
    try:
        if not args.test:
            if now.weekday() >= 5:
                log("週末不開盤")
                return 0
            if now.time() > CLOSE:
                log("已經收盤")
                return 0
            if is_holiday(now.date(), monitor.http):
                log("今天是證交所公告的休市日")
                return 0
        base = load_base(json.loads(BASE_FILE.read_text(encoding="utf-8")), settings.large_top)
        log(f"基準資料 {base.date}：{len(base.stocks)} 檔普通股、{len(base.groups)} 個族群（其中 {len(base.own_groups)} 個是自己的分類）")
        return monitor.test(base) if args.test else monitor.run(base)
    except Exception as e:                           # 任何步驟失敗都要通知，不可以默默結束
        log(f"❌ 盤中監控執行失敗：{type(e).__name__}：{e}")
        monitor.send(f"❌ 盤中監控執行失敗 {datetime.now(TZ):%m/%d %H:%M}\n原因：{type(e).__name__}：{e}\n影響：今天之後的盤中通知不會再送")
        return 1


if __name__ == "__main__":
    sys.exit(main())
