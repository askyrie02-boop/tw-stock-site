"""Telegram 推送。金鑰只從環境變數讀（公開 repo 的 GitHub Secrets），錯誤訊息裡不會出現金鑰。"""
from __future__ import annotations

import os
from pathlib import Path

import requests

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1000        # 圖片說明最多 1024 字，留一點空間
TIMEOUT = 30


class NotifyError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, chat_ids: list[str]):
        self.token = token
        self.chat_ids = chat_ids

    @classmethod
    def from_env(cls) -> "Telegram | None":
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        chats = [c.strip() for c in (os.getenv("TELEGRAM_CHAT_ID") or "").replace("，", ",").split(",") if c.strip()]
        return cls(token, chats) if token and chats else None

    def _post(self, method: str, **kwargs) -> None:
        try:
            r = requests.post(API.format(token=self.token, method=method), timeout=TIMEOUT, **kwargs)
        except requests.RequestException as e:                 # 例外訊息裡有網址（含金鑰），只留種類
            raise NotifyError(f"Telegram 連線失敗（{type(e).__name__}）") from None
        if not r.ok:
            try:
                why = r.json().get("description", "")
            except ValueError:
                why = ""
            raise NotifyError(f"Telegram 回應 {r.status_code}：{why}")

    def send_text(self, text: str) -> None:
        for chat in self.chat_ids:
            self._post("sendMessage", json={"chat_id": chat, "text": text, "disable_web_page_preview": True})

    def send_photo(self, path: Path, caption: str = "") -> None:
        """圖片加說明；說明太長時圖片和文字分開送。"""
        short = len(caption) <= CAPTION_LIMIT
        for chat in self.chat_ids:
            with open(path, "rb") as fh:
                self._post("sendPhoto", data={"chat_id": chat, "caption": caption if short else ""}, files={"photo": fh})
        if caption and not short:
            self.send_text(caption)
