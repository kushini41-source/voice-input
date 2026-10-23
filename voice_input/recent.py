"""最近 20 筆輸入紀錄：給「查看最近紀錄」和「把上一句加入詞庫」用。

只存文字（不存錄音），放在 logs\\recent.json，重開程式後還看得到。
"""

import json
import logging
import os
import threading
import time

from .paths import LOG_DIR

log = logging.getLogger(__name__)

RECENT_PATH = os.path.join(LOG_DIR, "recent.json")


class RecentRecords:
    def __init__(self, path=RECENT_PATH, max_items=20):
        self.path = path
        self.max_items = max_items
        self._lock = threading.Lock()
        self._items = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                items = json.load(f)
            return [i for i in items if isinstance(i, dict)][-self.max_items:]
        except (OSError, ValueError):
            return []

    def _save(self, items):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("存最近紀錄失敗：%s", e)

    def add(self, app, scene, raw, after_dict, after_ai, final, used_ai):
        item = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "app": app or "",
            "scene": scene or "",
            "raw": raw,
            "after_dict": after_dict,
            "after_ai": after_ai,
            "final": final,
            "used_ai": bool(used_ai),
        }
        with self._lock:
            self._items.append(item)
            self._items = self._items[-self.max_items:]
            items = list(self._items)
        self._save(items)

    def all(self):
        """由新到舊。"""
        with self._lock:
            return list(reversed(self._items))

    def last(self):
        with self._lock:
            return dict(self._items[-1]) if self._items else None
