"""翻譯模式：AI 呼叫、失敗處理、主流程。"""

import os
import sys
import tempfile
import time

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from test_command_flow import FakeEngine, FakeOutput, FakeTray, FakeUI, _load_main  # noqa: E402
from voice_input.config import load_config  # noqa: E402
from voice_input.context import SceneDetector  # noqa: E402
from voice_input.converter import Converter  # noqa: E402
from voice_input.dictionary import Dictionary  # noqa: E402
from voice_input.recent import RecentRecords  # noqa: E402
from voice_input.translator import Translator, build_user_message, is_chinese_target  # noqa: E402


class FakeBackend:
    def __init__(self, reply=None, error=None, delay=0.0):
        self.reply, self.error, self.delay = reply, error, delay
        self.calls = []

    def complete_json(self, system, user, schema, timeout):
        self.calls.append(user)
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply


def _translator(backend, **cfg):
    return Translator({"languages": ["英文", "日文"], "target": "英文", "timeout_seconds": 2, **cfg},
                      lambda: backend)


SCENE = {"name": "聊天模式"}


def test_translate_ok():
    backend = FakeBackend({"translation": "  See you at 3 PM tomorrow.  "})
    r = _translator(backend).run("明天三點見", SCENE, ["buy+1"])
    assert r.ok and r.text == "See you at 3 PM tomorrow."
    assert "<目標語言>英文</目標語言>" in backend.calls[0] and "buy+1" in backend.calls[0]


def test_empty_translation_is_failure():
    r = _translator(FakeBackend({"translation": "  "})).run("明天見", SCENE)
    assert not r.ok and "沒有回傳" in r.reason


def test_ai_error_is_failure():
    r = _translator(FakeBackend(error=RuntimeError("網路斷線"))).run("明天見", SCENE)
    assert not r.ok and "網路斷線" in r.reason


def test_timeout_is_failure():
    r = _translator(FakeBackend({"translation": "x"}, delay=0.5), timeout_seconds=0.1).run("明天見", SCENE)
    assert not r.ok and "沒回應" in r.reason


def test_no_backend_is_failure():
    r = _translator(None).run("明天見", SCENE)
    assert not r.ok


def test_unknown_target_falls_back_to_first_language():
    t = _translator(FakeBackend(), target="火星文")
    assert t.target == "英文"
    assert Translator({}, lambda: None).languages == ["英文"]


def test_user_message_and_chinese_target():
    msg = build_user_message("你好", "日文", {"name": "正式模式"}, ["Claude Code"])
    assert "<目標語言>日文</目標語言>" in msg and "<場景>正式模式</場景>" in msg and "Claude Code" in msg
    assert is_chinese_target("繁體中文") and is_chinese_target("中文")
    assert not is_chinese_target("英文") and not is_chinese_target("日文")


# ── 主流程 ──

def _app(raw, backend):
    m = _load_main()
    cfg = load_config()
    app = m.App.__new__(m.App)
    app.cfg = cfg
    app.engine = FakeEngine(raw)
    app.output = FakeOutput("")
    app.translator = _translator(backend)
    app.dictionary = Dictionary(cfg["dictionary"], path=os.path.join(BASE, "dictionary.yaml"))
    app.converter = Converter(cfg["converter"])
    app.scenes = SceneDetector(cfg["scenes"])
    app.recent = RecentRecords(path=os.path.join(tempfile.mkdtemp(), "recent.json"))
    app.tray = FakeTray()
    app.ui = FakeUI()
    app.overlay = None
    app._recording = False
    job = m.Job(np.zeros(16000, np.float32), 1.0, "LINE.exe", "", released_at=time.perf_counter())
    job.mode = "translate"
    return app, job


def test_translate_flow_types_translation():
    backend = FakeBackend({"translation": "I'm using buy+1 today."})
    app, job = _app("我今天在用拜加溫", backend)
    app._process(job)
    assert app.output.sent == ["I'm using buy+1 today."]
    assert "buy+1" in backend.calls[0], "詞庫要先把專有名詞改對再交給 AI 翻譯"
    assert app.recent.last()["scene"] == "翻譯模式（英文）"


def test_translate_failure_types_chinese_and_notifies():
    app, job = _app("我明天三點到", FakeBackend(error=RuntimeError("網路斷線")))
    app._process(job)
    assert app.output.sent == ["我明天三點到"]
    assert any("網路斷線" in n for n in app.tray.notes)


def test_translate_snippet_is_not_translated():
    backend = FakeBackend({"translation": "should not be used"})
    app, job = _app("我的信箱", backend)
    app._process(job)
    assert app.output.sent == [app.dictionary.current().snippets["我的信箱"]]
    assert backend.calls == []
