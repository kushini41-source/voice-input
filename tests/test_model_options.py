"""不同 Claude 模型的呼叫方式（關掉思考的寫法、安全備援），以及指令模式自己指定模型。不會真的連網路。"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.commander import Commander  # noqa: E402
from voice_input.corrector import AnthropicBackend  # noqa: E402


class FakeMessages:
    def __init__(self, log, kind):
        self.log, self.kind = log, kind

    def create(self, **kwargs):
        self.log.append((self.kind, kwargs))
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text='{"action": "answer", "text": "好"}')])


class FakeClient:
    def __init__(self):
        self.log = []
        self.messages = FakeMessages(self.log, "messages")
        self.beta = SimpleNamespace(messages=FakeMessages(self.log, "beta"))

    def with_options(self, **_):
        return self


def _backend(model, thinking="off"):
    b = AnthropicBackend(model, "test-key", "low", thinking)
    b.client = FakeClient()
    return b


def _sent(b):
    b.complete_json("系統", "使用者", {"type": "object"}, timeout=5)
    return b.client.log[-1]


def test_haiku_unchanged():
    kind, kw = _sent(_backend("claude-haiku-5-5"))
    assert kind == "messages" and kw["thinking"] == {"type": "disabled"} and "fallbacks" not in kw
    assert kw["max_tokens"] == 4096


def test_sonnet_55_off_uses_between_tools_and_fallback():
    kind, kw = _sent(_backend("claude-sonnet-5-5"))
    assert kw["thinking"] == {"type": "between_tools"}
    assert kind == "beta" and kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]


def test_sonnet_adaptive_omits_thinking():
    kind, kw = _sent(_backend("claude-sonnet-5-5", "adaptive"))
    assert "thinking" not in kw and kw["max_tokens"] >= 16000


def test_fable_never_sends_disabled():
    kind, kw = _sent(_backend("claude-fable-5-1"))
    assert "thinking" not in kw, "Fable 不能關掉思考，送 disabled 會被拒絕"
    assert kind == "beta" and kw["max_tokens"] >= 16000


def test_reads_text_block_after_empty_thinking_block():
    assert _backend("claude-fable-5-1").complete_json("s", "u", {}, timeout=5) == {"action": "answer", "text": "好"}


def test_commander_own_model_only_when_using_claude():
    shared_claude = SimpleNamespace(name="anthropic")
    shared_ollama = SimpleNamespace(name="ollama")
    cmd = Commander({"model": "claude-sonnet-5-5", "effort": "low"}, lambda: shared_claude)
    own = cmd._backend()
    assert own is not shared_claude and own.model == "claude-sonnet-5-5"
    assert cmd._backend() is own, "同一個模型只建立一次"
    assert Commander({"model": "claude-sonnet-5-5"}, lambda: shared_ollama)._backend() is shared_ollama
    assert Commander({}, lambda: shared_claude)._backend() is shared_claude, "沒設定就跟 AI 校正用同一個"
