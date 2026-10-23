"""詞庫自動學習：打完字後盯著那個輸入框一陣子，看使用者有沒有手動改掉我們打的字。

用 Windows 的 UI Automation（螢幕報讀軟體也用它）讀輸入框裡的文字：
只讀不改、不裝鍵盤 hook、不動剪貼簿；讀到的文字只在記憶體裡比對，不存檔、不上傳。
密碼欄一律不讀。讀不到的程式（終端機、部分特殊程式）就自動略過。

什麼時候算「改完了」：開始錄下一句時、輸入框的前後文變了（例如訊息送出後被清空）、
或過了 watch_seconds 秒。
"""

import logging
import queue
import sys
import threading
import time

from .learner import edited_region, locate, normalize_field_text

log = logging.getLogger(__name__)

UIA_VALUE_PATTERN = 10002
UIA_TEXT_PATTERN = 10014
_MAX_CHARS = 200_000     # 輸入框裡的字比這個多（很長的文件）就不看
_SLOW_READ = 0.3         # 讀一次超過這麼久（例如很大的 Word 檔），就不繼續盯，免得拖慢電腦


class FieldReader:
    """讀「目前游標所在輸入框」的文字。COM 的規定：只能在建立它的那條執行緒裡用。"""

    def __init__(self):
        sys.coinit_flags = 0   # 多執行緒模式（COINIT_MULTITHREADED），UI Automation 建議用這個
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass               # 這條執行緒已經初始化過了
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia
        self._mod = uia
        self._uia = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)

    def focused(self):
        """目前有焦點的元件；沒有、或是密碼欄，就回傳 None。"""
        try:
            el = self._uia.GetFocusedElement()
            if not el or el.CurrentIsPassword:
                return None
            return el
        except Exception:
            return None

    def read(self, el):
        """讀元件裡的全部文字；讀不到（元件不見了、不支援、太長）回傳 None。"""
        text = None
        try:
            pattern = el.GetCurrentPattern(UIA_TEXT_PATTERN)
            if pattern:
                text = pattern.QueryInterface(self._mod.IUIAutomationTextPattern).DocumentRange.GetText(_MAX_CHARS)
        except Exception:
            text = None
        if text is None:
            try:
                pattern = el.GetCurrentPattern(UIA_VALUE_PATTERN)
                if pattern:
                    text = pattern.QueryInterface(self._mod.IUIAutomationValuePattern).CurrentValue
            except Exception:
                text = None
        if text is None or len(text) >= _MAX_CHARS:
            return None
        return normalize_field_text(text)


class EditWatcher:
    """在自己的執行緒裡一次盯一句。on_edit(打出去的字, 被改成的字, 程式名稱) 只在真的被改過時呼叫。"""

    # 貼上後目標程式要一點時間才會顯示出來，分幾次讀（Claude、Chrome 這類程式比較慢，最多等約 3 秒）
    BASELINE_DELAYS = (0.25, 0.35, 0.6, 0.8, 1.0)

    def __init__(self, cfg, on_edit, reader_factory=FieldReader, start=True):
        self.on_edit = on_edit
        self.poll = max(0.01, float(cfg.get("poll_seconds", 0.7)))
        self.max_seconds = float(cfg.get("watch_seconds", 90))
        self.skip_apps = {str(a).lower() for a in (cfg.get("skip_apps") or [])}
        self._reader_factory = reader_factory
        self._reader = None
        self._queue = queue.Queue()
        self._gen = 0              # 每次 finish() 加 1；盯的那一句記得自己開始時的號碼，號碼變了就收尾
        self.available = True
        if start:
            threading.Thread(target=self._run, name="edit-watch", daemon=True).start()

    def watch(self, typed, app_exe=""):
        """剛打完一句時呼叫（馬上返回）。"""
        if not self.available or not typed or (app_exe or "").lower() in self.skip_apps:
            return
        self._queue.put((typed, app_exe, self._gen))

    def finish(self):
        """使用者要打下一句了（開始錄音、插入 AI 回答）：目前盯的那句讀最後一次就收尾。馬上返回。"""
        self._gen += 1

    def _run(self):
        try:
            self._reader = self._reader_factory()
        except Exception as e:
            self.available = False
            log.warning("無法讀取輸入框的文字，詞庫自動學習只會從 AI 校正學：%s", e)
            return
        while True:
            typed, app_exe, gen = self._queue.get()
            try:
                self._watch_one(typed, app_exe, gen)
            except Exception:
                log.exception("觀察輸入框時發生錯誤（不影響語音輸入）")

    def _wait(self, seconds, gen):
        """等 seconds 秒；中途 finish() 了就提早回來，回傳 True。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._gen != gen:
                return True
            time.sleep(min(0.05, seconds))
        return self._gen != gen

    def _watch_one(self, typed, app_exe, gen):
        typed = normalize_field_text(typed)
        el, baseline, start, last_text = None, None, -1, None
        for delay in self.BASELINE_DELAYS:
            if self._wait(delay, gen):
                return
            el = self._reader.focused()
            text = self._reader.read(el) if el is not None else None
            if text is not None:
                last_text = text
                start = locate(text, typed)
                if start >= 0:
                    baseline = text
                    break
        if baseline is None:
            # 只記長度和有沒有找到開頭幾個字，不記內容，方便之後查原因
            why = ("讀不到輸入框" if last_text is None else
                   f"輸入框 {len(last_text)} 字，找不到剛打的 {len(typed)} 字"
                   f"（開頭{'有' if typed[:6] in last_text else '也沒有'}找到、出現 {last_text.count(typed)} 次）")
            log.info("詞庫學習：%s 這句不觀察你有沒有改字：%s", app_exe or "這個程式", why)
            return

        last = typed
        deadline = time.monotonic() + self.max_seconds
        while True:
            finished = self._wait(self.poll, gen)
            t0 = time.monotonic()
            current = self._reader.read(el)
            slow = time.monotonic() - t0 > _SLOW_READ
            region = edited_region(baseline, start, typed, current) if current is not None else None
            if not region:
                break          # 輸入框不見了、訊息送出被清空、整句刪掉、或改了別的地方
            last = region
            if finished or slow or time.monotonic() > deadline:
                if slow:
                    log.info("%s 的輸入框讀取太慢，這句不再繼續觀察", app_exe or "這個程式")
                break
        if last != typed:
            self.on_edit(typed, last, app_exe)
