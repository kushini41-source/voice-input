"""指令模式整條流程的測試：用模擬的辨識引擎、輸出、AI，跑主程式的 _process_command。"""

import importlib.util
import os
import sys
import tempfile
import time

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from voice_input.commander import CommandResult  # noqa: E402
from voice_input.config import load_config  # noqa: E402
from voice_input.context import SceneDetector  # noqa: E402
from voice_input.converter import Converter  # noqa: E402
from voice_input.dictionary import Dictionary  # noqa: E402
from voice_input.recent import RecentRecords  # noqa: E402


def _load_main():
    spec = importlib.util.spec_from_file_location("vi_main_flow", os.path.join(BASE, "main.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeEngine:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio, prompt_terms=()):
        return self.text


class FakeOutput:
    def __init__(self, selection):
        self.selection = selection
        self.sent = []

    def copy_selection(self, app_exe=""):
        return self.selection

    def send(self, text, app_exe=""):
        self.sent.append(text)
        return "paste"


class FakeCommander:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, instruction, selection, scene, is_cancelled=lambda: False, style_rules=()):
        self.calls.append((instruction, selection))
        self.style_rules = list(style_rules)
        return self.result


class FakeStyle:
    def __init__(self, rules=()):
        self.enabled = True
        self._rules = list(rules)
        self.recorded, self.edits = [], []

    def rules(self):
        return self._rules if self.enabled else []

    def record(self, app, scene, said, final):
        self.recorded.append((app, scene, said, final))

    def record_edit(self, app, typed, edited):
        self.edits.append((app, typed, edited))


class FakeTray:
    def __init__(self):
        self.notes = []

    def set_state(self, *a):
        pass

    def notify(self, title, msg):
        self.notes.append(msg)


class FakeUI:
    def __init__(self):
        self.calls = []

    def call(self, fn):
        self.calls.append(fn)


def _app(engine_text, selection, result):
    m = _load_main()
    cfg = load_config()
    app = m.App.__new__(m.App)
    app.cfg = cfg
    app.engine = FakeEngine(engine_text)
    app.output = FakeOutput(selection)
    app.commander = FakeCommander(result)
    app.style = FakeStyle(["聊天：句尾用「喔」"])
    app.dictionary = Dictionary(cfg["dictionary"], path=os.path.join(BASE, "dictionary.yaml"))
    app.converter = Converter(cfg["converter"])
    app.scenes = SceneDetector(cfg["scenes"])
    app.recent = RecentRecords(path=os.path.join(tempfile.mkdtemp(), "recent.json"))
    app.tray = FakeTray()
    app.ui = FakeUI()
    app.overlay = None
    app._recording = False
    job = m.Job(np.zeros(16000, np.float32), 1.0, "WINWORD.EXE", "報告.docx", released_at=time.perf_counter())
    job.mode = "command"
    return app, job


def test_replace_flow():
    app, job = _app("改短一點", "原本很長的一段文字", CommandResult("replace", "短的文字"))
    app._process_command(job)
    assert app.commander.calls == [("改短一點", "原本很長的一段文字")]
    assert app.commander.style_rules == ["聊天：句尾用「喔」"], "指令模式要照使用者的用字習慣寫"
    assert app.output.sent == ["短的文字"]
    assert app.recent.last()["final"] == "短的文字"


def test_answer_flow_shows_card_without_typing():
    app, job = _app("這是什麼意思", "選取", CommandResult("answer", "意思是…"))
    app._process_command(job)
    assert app.output.sent == []
    assert app.ui.calls, "應該要顯示回答卡片"


def test_failure_notifies():
    app, job = _app("改短", "選取", CommandResult("", "", ok=False, reason="AI 超過 20 秒沒回應"))
    app._process_command(job)
    assert app.output.sent == []
    assert any("20 秒" in n for n in app.tray.notes)


def test_empty_recognition_is_logged(caplog):
    import logging
    caplog.set_level(logging.INFO)
    app, job = _app("", "選取", CommandResult("replace", "x"))
    app._process_command(job)
    assert app.commander.calls == []
    assert any("沒有辨識出" in r.getMessage() for r in caplog.records), "沒聽到聲音也要寫進日誌"
