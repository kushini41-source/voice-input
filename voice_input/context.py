"""上下文：判斷目前在哪種 App（決定校正風格），以及記住同一個 App 裡最近說過的幾句話。"""

import collections
import threading
import time

DEFAULT_SCENE = {
    "name": "一般模式",
    "instructions": "一般文字：補上合適的全形標點，句尾加句號。",
}


class SceneDetector:
    """依前景程式名稱與視窗標題，對照 config.yaml 的 scenes 表找出場景。"""

    def __init__(self, scenes):
        self.scenes = []
        for s in scenes or []:
            self.scenes.append({
                "name": str(s.get("name", "")),
                "processes": {str(p).lower() for p in (s.get("processes") or [])},
                "title_keywords": [str(k).lower() for k in (s.get("title_keywords") or [])],
                "instructions": str(s.get("instructions", "")).strip(),
                # 句尾的句號一律拿掉（聊天模式用）；由程式保證，不只靠 AI 自律
                "no_trailing_period": bool(s.get("no_trailing_period", False)),
            })

    def detect(self, exe, title):
        exe_l, title_l = (exe or "").lower(), (title or "").lower()
        # 視窗標題比程式名稱更精確（例如同一個瀏覽器，開 Gmail 就是正式模式），所以先比標題
        for s in self.scenes:
            if any(k and k in title_l for k in s["title_keywords"]):
                return s
        for s in self.scenes:
            if exe_l and exe_l in s["processes"]:
                return s
        return DEFAULT_SCENE


class History:
    """每個 App 各自記住最近幾句最終輸出，超過有效時間就不算。"""

    def __init__(self, max_items=3, ttl_seconds=300):
        self.max_items = int(max_items)
        self.ttl = float(ttl_seconds)
        self._items = collections.defaultdict(lambda: collections.deque(maxlen=self.max_items))
        self._lock = threading.Lock()

    def add(self, app, text, now=None):
        if not text:
            return
        with self._lock:
            self._items[(app or "").lower()].append((now if now is not None else time.time(), text))

    def recent(self, app, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            items = list(self._items.get((app or "").lower(), ()))
        return [text for t, text in items if now - t <= self.ttl]
