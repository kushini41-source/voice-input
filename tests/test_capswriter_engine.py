"""CapsWriter 引擎的協定測試：用一個假的 CapsWriter 伺服器，檢查送出的格式和讀結果的方式。"""

import base64
import json
import os
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.asr.capswriter_engine import CapsWriterEngine, CapsWriterError  # noqa: E402


class FakeServer:
    """照 CapsWriter 的協定回應：收齊音訊後，先回一則中間結果，再回最終結果。"""

    def __init__(self, reply_text="測試結果", send_final=True):
        from websockets.sync.server import serve
        self.reply_text = reply_text
        self.send_final = send_final
        self.received = []
        self.server = serve(self._handler, "127.0.0.1", 0, subprotocols=["binary"], max_size=None)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self, ws):
        assert ws.subprotocol == "binary"
        chunks = []
        for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            if msg["data"]:
                chunks.append(np.frombuffer(base64.b64decode(msg["data"]), dtype=np.float32))
            if msg["is_final"]:
                audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
                base = dict(task_id=msg["task_id"], duration=len(audio) / 16000, time_start=msg["time_start"],
                            time_submit=0, time_complete=0)
                # 別的任務的結果、以及同任務的中間結果，都不能被當成答案
                ws.send(json.dumps(dict(base, task_id="別的任務", is_final=True, text="錯的")))
                ws.send(json.dumps(dict(base, is_final=False, text="中間結果")))
                if self.send_final:
                    ws.send(json.dumps(dict(base, is_final=True, text=f"{self.reply_text}:{len(audio)}")))
                chunks = []

    def close(self):
        self.server.shutdown()


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


def _engine(port, **cfg):
    base = {"port": port, "auto_start_server": False, "timeout_seconds": 2}
    base.update(cfg)
    return CapsWriterEngine(base)


def test_protocol_roundtrip(server):
    eng = _engine(server.port)
    eng.load()   # 會先暖機一次
    audio = np.linspace(-0.5, 0.5, 16000 * 2, dtype=np.float32)
    text = eng.transcribe(audio, prompt_terms=["buy+1", "Claude Code"])
    assert text == "測試結果:32000"

    first, last = server.received[-2], server.received[-1]
    # 音訊訊息：16kHz float32 原始位元組的 base64，source=mic
    assert first["is_final"] is False and first["source"] == "mic"
    decoded = np.frombuffer(base64.b64decode(first["data"]), dtype=np.float32)
    assert np.allclose(decoded, audio)
    # 結束訊息：data 是空字串、is_final=True、同一個 task_id
    assert last["is_final"] is True and last["data"] == "" and last["task_id"] == first["task_id"]
    # 詞庫的專有名詞放在 context 給模型參考
    assert "buy+1" in first["context"] and "Claude Code" in first["context"]
    for key in ("time_start", "seg_duration", "seg_overlap", "language"):
        assert key in first
    eng.close()


def test_reconnects_after_disconnect(server):
    eng = _engine(server.port)
    eng.load()
    eng._ws.close()          # 模擬連線被切斷
    assert eng.transcribe(np.zeros(1600, np.float32)).startswith("測試結果")


def test_timeout_when_no_final():
    s = FakeServer(send_final=False)
    try:
        eng = _engine(s.port, timeout_seconds=0.5)
        eng._connect()
        with pytest.raises(CapsWriterError):
            eng.transcribe(np.zeros(1600, np.float32))
    finally:
        s.close()


def test_server_not_running_gives_clear_error():
    eng = _engine(1, auto_start_server=False)
    with pytest.raises(CapsWriterError, match="CapsWriter"):
        eng.load()


def test_missing_server_exe_gives_clear_error():
    eng = _engine(1, auto_start_server=True, server_exe=r"C:\不存在\start_server.exe")
    with pytest.raises(CapsWriterError, match="server_exe"):
        eng.load()


def test_close_stops_only_own_server(monkeypatch):
    """關閉引擎時，只關掉自己開的伺服器；使用者自己開的不動。"""
    import voice_input.asr.capswriter_engine as mod
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda args, **kw: calls.append(args))

    class FakeProc:
        pid = 12345
        def poll(self):
            return None

    eng = _engine(1)
    eng.close()                      # 不是自己開的：不會呼叫 taskkill
    assert calls == []
    eng._server_proc = FakeProc()
    eng.close()                      # 自己開的：連子程序一起關
    assert calls and calls[0][:3] == ["taskkill", "/PID", "12345"] and "/T" in calls[0]
    eng.close()                      # 關過了就不會再關一次
    assert len(calls) == 1
