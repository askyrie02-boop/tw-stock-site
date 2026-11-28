"""盤中監控的接力啟動：python -m intraday.relay（在公開的網站 repo 由 GitHub Actions 執行）。

為什麼要接力？GitHub 自己的排程在這個 repo 常常延遲 7～8 小時（2026-11-18～11-23 的 08:37 排程都到 15:40 以後才跑），
Claude 的排程任務又可能被停用，所以改成不靠排程：一個 GitHub 工作先「等」，時間到了用 workflow_dispatch 啟動盤中監控
（workflow_dispatch 是馬上執行，不會像排程那樣延遲）。

GitHub 一個工作最多跑 6 小時，所以每一棒最多等 MAX_SLEEP，還沒到就再啟動下一棒（自己）繼續等：
  晚上網站更新（push）→ 第一棒等 5.5 小時 → 第二棒等到 08:40 → 啟動盤中監控，同時啟動下一棒等明天。
同一時間只留一條接力（intraday-relay.yml 的 concurrency 會取消舊的），網站每次更新都重新開始一條也沒關係。
週末會多接幾棒（約 10 棒）；休市日照樣啟動，盤中監控自己會看證交所的休市日並馬上結束。

金鑰：只用 GitHub 自動提供的 GITHUB_TOKEN（只能操作這個 repo 的 Actions），不印在 log。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time as clock
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
START_AT = time(8, 40)              # 每個交易日幾點啟動盤中監控（它會自己等到 09:00 開盤）
WINDOW = (time(8, 30), time(13, 20))  # 這段時間內被叫醒、盤中監控又沒有在跑，就馬上啟動
MAX_SLEEP = timedelta(hours=5, minutes=30)   # 一棒最多等多久（GitHub 一個工作最多 6 小時）
MONITOR = "intraday.yml"
RELAY = "intraday-relay.yml"
API = "https://api.github.com"


def log(text: str) -> None:
    print(f"[{datetime.now(TZ):%m/%d %H:%M:%S}] {text}", flush=True)


def next_start(now: datetime, handled: date | None) -> datetime:
    """下一次要啟動盤中監控的時間：今天還沒處理過、還沒到 08:40 就是今天，否則是下一個週一到週五。"""
    day = now.date()
    if day.weekday() < 5 and day != handled and now.time() < START_AT:
        return datetime.combine(day, START_AT, TZ)
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, START_AT, TZ)


def in_window(now: datetime) -> bool:
    return now.weekday() < 5 and WINDOW[0] <= now.time() < WINDOW[1]


def plan(now: datetime, handled: date | None, running: bool) -> dict:
    """這一棒要做什麼：
    start_now  現在就啟動盤中監控（盤中時間被叫醒、今天還沒處理、監控沒有在跑）
    sleep      要等幾秒
    then       等完之後：monitor＝啟動盤中監控再交棒給明天；relay＝只交棒（還太早）
    handled    交棒時告訴下一棒「這一天已經處理過」
    """
    start_now = False
    if in_window(now) and now.date() != handled:
        start_now = not running
        handled = now.date()                     # 正在跑或現在啟動，今天都算處理過
    target = next_start(now, handled)
    wait = target - now
    if wait <= MAX_SLEEP:
        return {"start_now": start_now, "sleep": max(0, wait.total_seconds()), "then": "monitor", "handled": target.date()}
    return {"start_now": start_now, "sleep": MAX_SLEEP.total_seconds(), "then": "relay", "handled": handled}


class GitHub:
    def __init__(self, repo: str, token: str, opener=urllib.request.urlopen):
        self.repo, self.token, self.opener = repo, token, opener

    def _call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(f"{API}/repos/{self.repo}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        for attempt in range(3):
            try:
                with self.opener(req, timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:      # 只印狀態碼，不印網址與金鑰
                if attempt == 2 or e.code < 500:
                    raise RuntimeError(f"GitHub API 回應 {e.code}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 2:
                    raise RuntimeError(f"GitHub API 連不上（{type(e).__name__}）") from None
            clock.sleep(10 * (attempt + 1))

    def monitor_running(self) -> bool:
        for status in ("in_progress", "queued"):
            runs = self._call("GET", f"/actions/workflows/{MONITOR}/runs?status={status}&per_page=5") or {}
            if runs.get("total_count"):
                return True
        return False

    def dispatch(self, workflow: str, inputs: dict | None = None) -> None:
        self._call("POST", f"/actions/workflows/{workflow}/dispatches", {"ref": "main", "inputs": inputs or {}})


def start_monitor(gh: GitHub) -> None:
    if gh.monitor_running():
        log("盤中監控已經在執行，不用再啟動")
        return
    gh.dispatch(MONITOR)
    log("已啟動盤中監控")


def main(argv: list[str] | None = None, now_fn=lambda: datetime.now(TZ), sleep=clock.sleep, gh: GitHub | None = None) -> int:
    p = argparse.ArgumentParser(description="盤中監控的接力啟動")
    p.add_argument("--handled", default="", help="已經處理過的日期（YYYY-MM-DD），上一棒傳過來的")
    args = p.parse_args(argv)
    handled = date.fromisoformat(args.handled) if args.handled else None
    if gh is None:
        repo, token = os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", "")
        if not repo or not token:
            print("::error::沒有 GITHUB_REPOSITORY 或 GITHUB_TOKEN（這個程式要在 GitHub Actions 上執行）")
            return 1
        gh = GitHub(repo, token)
    now = now_fn()
    step = plan(now, handled, gh.monitor_running() if in_window(now) else False)
    if step["start_now"]:
        start_monitor(gh)
    wake = now + timedelta(seconds=step["sleep"])
    what = "啟動盤中監控" if step["then"] == "monitor" else "交棒給下一棒繼續等"
    log(f"等到 {wake:%m/%d %H:%M} 再{what}")
    sleep(step["sleep"])
    if step["then"] == "monitor":
        start_monitor(gh)
    nxt = step["handled"].isoformat() if step["handled"] else ""
    gh.dispatch(RELAY, {"handled": nxt})            # 最後才交棒：新的一棒會取消這一棒（同一個 concurrency）
    log(f"已交棒（下一棒從 {nxt or '今天'} 之後開始算）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
