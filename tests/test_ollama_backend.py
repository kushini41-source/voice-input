"""本機 AI（Ollama）後端的測試：用一個假的 Ollama 伺服器，不需要真的安裝 Ollama。"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.context import DEFAULT_SCENE  # noqa: E402
from voice_input.corrector import Corrector, OllamaBackend, create_backend  # noqa: E402


class FakeOllama:
    def __init__(self, reply="我明天4點要開會。", models=("qwen2.5:7b",)):
        outer = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send({"models": [{"name": m} for m in models]})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(payload)
                self._send({"message": {"role": "assistant", "content": reply}, "done": True})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def ollama():
    s = FakeOllama()
    yield s
    s.close()


def _cfg(url, model="qwen2.5:7b"):
    return {"enabled": True, "backend": "ollama", "timeout_seconds": 2,
            "ollama": {"url": url, "model": model}}


def test_ollama_request_format(ollama):
    b = OllamaBackend("qwen2.5:7b", ollama.url)
    assert b.complete("系統提示", "使用者內容", timeout=2) == "我明天4點要開會。"
    req = ollama.requests[-1]
    assert req["model"] == "qwen2.5:7b"
    assert req["stream"] is False and req["think"] is False
    assert req["messages"][0] == {"role": "system", "content": "系統提示"}
    assert req["messages"][1] == {"role": "user", "content": "使用者內容"}


def test_ollama_strips_think_tags():
    s = FakeOllama(reply="<think>想一想…</think>\n我明天4點要開會。")
    try:
        assert OllamaBackend("qwen2.5:7b", s.url).complete("s", "u", timeout=2) == "我明天4點要開會。"
    finally:
        s.close()


def test_corrector_with_ollama(ollama):
    c = Corrector(_cfg(ollama.url))
    r = c.correct("呃我明天三點不對四點要開會", DEFAULT_SCENE, [], [])
    assert r.used_ai and r.text == "我明天4點要開會。"
    assert c.backend_name == "ollama"


def test_missing_model_gives_clear_error(ollama):
    with pytest.raises(RuntimeError, match="ollama pull"):
        create_backend(_cfg(ollama.url, model="不存在的模型"))


def test_ollama_not_running_gives_clear_error():
    c = Corrector(_cfg("http://127.0.0.1:1"))
    assert c.backend is None
    assert "Ollama" in c.init_error
    # 沒有 AI 也照常輸出原文
    assert c.correct("原本的句子", DEFAULT_SCENE, [], []).text == "原本的句子"


def test_switch_backend_keeps_old_on_failure(ollama):
    c = Corrector(_cfg(ollama.url))
    old = c.backend
    c.cfg["ollama"]["url"] = "http://127.0.0.1:1"
    with pytest.raises(RuntimeError):
        c.switch_backend("ollama")
    assert c.backend is old
