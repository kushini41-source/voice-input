"""CapsWriter-Offline 辨識引擎：連到 CapsWriter 伺服器（ws://127.0.0.1:6016），用它的 Qwen3-ASR 模型辨識。

協定（照 CapsWriter 客戶端原始碼 core/protocol.py、core/client/audio/recorder.py）：
- WebSocket，子協定 "binary"，每則訊息是 JSON 文字
- 客戶端送 AudioMessage：data = base64(16kHz 單聲道 float32 的原始位元組)，is_final=False
- 音訊送完再送一則 data 為空字串、is_final=True 的 AudioMessage
- 伺服器回 RecognitionMessage，等到同一個 task_id 且 is_final=True 的那則，取 text 欄位
- context 欄位可以放提示詞（專有名詞），幫助模型辨識

只用 CapsWriter 的「伺服器」，不啟動它的客戶端（它的客戶端也用 CapsLock，而且是用鍵盤 hook）。
"""

import base64
import json
import logging
import os
import socket
import subprocess
import threading
import time
import uuid

import numpy as np

from .base import ASREngine

log = logging.getLogger(__name__)


class CapsWriterError(RuntimeError):
    pass


def _port_open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class CapsWriterEngine(ASREngine):
    name = "capswriter"

    def __init__(self, cfg, notify=None):
        self.host = cfg.get("host", "127.0.0.1")
        self.port = int(cfg.get("port", 6016))
        self.language = cfg.get("language", "auto")
        self.server_exe = cfg.get("server_exe", "")
        self.auto_start = bool(cfg.get("auto_start_server", True))
        self.start_timeout = float(cfg.get("server_start_timeout", 90))
        self.timeout = float(cfg.get("timeout_seconds", 15))
        self.notify = notify or (lambda title, msg: None)
        self._ws = None
        self._lock = threading.Lock()
        self._server_proc = None   # 由這個程式自動啟動的 CapsWriter 伺服器（使用者自己開的不算）

    @property
    def url(self):
        return f"ws://{self.host}:{self.port}"

    def describe(self):
        return f"CapsWriter（{self.url}）"

    # ── 連線 ──

    def _start_server(self):
        if not self.server_exe or not os.path.isfile(self.server_exe):
            raise CapsWriterError(
                f"CapsWriter 伺服器沒有在執行，也找不到它的程式（{self.server_exe or '未設定'}）。"
                "請在 config.yaml 的 asr.capswriter.server_exe 填入 start_server.exe 的完整路徑。"
            )
        log.info("CapsWriter 伺服器沒有在執行，自動在背景啟動：%s", self.server_exe)
        CREATE_NO_WINDOW = 0x08000000
        # 記住是我們開的，關程式或切換引擎時才知道要把它關掉
        self._server_proc = subprocess.Popen([self.server_exe], cwd=os.path.dirname(self.server_exe),
                                             creationflags=CREATE_NO_WINDOW, close_fds=True)
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if _port_open(self.host, self.port):
                return
            time.sleep(1)
        raise CapsWriterError(f"CapsWriter 伺服器啟動後 {self.start_timeout:.0f} 秒還連不上 {self.url}")

    def _connect(self):
        from websockets.sync.client import connect
        if self._ws is not None:
            return self._ws
        self._ws = connect(self.url, subprotocols=["binary"], max_size=None, open_timeout=5, proxy=None)
        return self._ws

    def _close(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._close()
            self._stop_own_server()

    def _stop_own_server(self):
        """只關掉「這個程式自己開的」CapsWriter 伺服器，把顯示卡記憶體還回去。
        使用者自己手動開的伺服器不會去動它。"""
        proc, self._server_proc = self._server_proc, None
        if proc is None or proc.poll() is not None:
            return
        # CapsWriter 伺服器會再開子程序，要連子程序一起關（/T）
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=0x08000000)
        log.info("已關閉由本程式啟動的 CapsWriter 伺服器")

    def load(self):
        if not _port_open(self.host, self.port):
            if not self.auto_start:
                raise CapsWriterError(f"連不上 CapsWriter 伺服器（{self.url}），請先啟動 CapsWriter 的 start_server.exe")
            self._start_server()
        self._connect()
        t0 = time.perf_counter()
        # CapsWriter 第一次辨識特別慢（實測約 12 秒），啟動時先暖機
        self.warmup()
        log.info("辨識引擎就緒：%s，暖機 %.1f 秒", self.describe(), time.perf_counter() - t0)

    # ── 辨識 ──

    def transcribe(self, audio, prompt_terms=(), warmup=False):
        audio = np.asarray(audio, dtype=np.float32)
        context = "、".join(t for t in prompt_terms if t)[:300]
        with self._lock:
            # 連線斷掉（例如 CapsWriter 伺服器重開）就重連一次再試
            for attempt in (1, 2):
                try:
                    return self._transcribe_once(audio, context, warmup)
                except CapsWriterError:
                    raise
                except Exception as e:
                    self._close()
                    if attempt == 2:
                        raise CapsWriterError(f"和 CapsWriter 伺服器的連線出問題：{e}") from e
                    log.warning("CapsWriter 連線中斷，重新連線：%s", e)
                    self._connect()

    def _transcribe_once(self, audio, context, warmup):
        ws = self._connect()
        task_id = str(uuid.uuid1())
        start = time.time()
        common = dict(task_id=task_id, source="mic", time_start=start, seg_duration=60.0,
                      seg_overlap=4.0, context=context, language=self.language)
        ws.send(json.dumps(dict(common, data=base64.b64encode(audio.tobytes()).decode("ascii"),
                                is_final=False), ensure_ascii=False))
        ws.send(json.dumps(dict(common, data="", is_final=True), ensure_ascii=False))

        timeout = 60 if warmup else self.timeout
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CapsWriterError(f"CapsWriter 超過 {timeout:.0f} 秒沒有回傳結果")
            msg = json.loads(ws.recv(timeout=remaining))
            if msg.get("task_id") == task_id and msg.get("is_final"):
                return (msg.get("text") or "").strip()
