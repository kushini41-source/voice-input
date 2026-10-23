"""詞庫自動學習接進主流程：一般聽寫打完字後，有記下 AI 改的詞、有開始觀察輸入框。"""

import os
import sys
import tempfile
import time

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from test_command_flow import FakeEngine, FakeOutput, FakeStyle, FakeTray, FakeUI, _load_main  # noqa: E402
from voice_input.config import load_config  # noqa: E402
from voice_input.context import History, SceneDetector  # noqa: E402
from voice_input.converter import Converter  # noqa: E402
from voice_input.dictionary import Dictionary  # noqa: E402
from voice_input.learner import Learner  # noqa: E402
from voice_input.recent import RecentRecords  # noqa: E402


class FakeResult:
    def __init__(self, text):
        self.text, self.seconds, self.used_ai, self.reason = text, 0.5, True, ""


class FakeCorrector:
    enabled = True

    def __init__(self, text):
        self.text = text
        self.style_rules = None

    def correct(self, text, scene, history, protected, is_cancelled=lambda: False, style_rules=()):
        self.style_rules = list(style_rules)
        return FakeResult(self.text)


class FakeWatcher:
    def __init__(self):
        self.watched = []

    def watch(self, typed, app_exe=""):
        self.watched.append((typed, app_exe))


def _app(raw, corrected, ai_min_count=1):
    m = _load_main()
    cfg = load_config()
    app = m.App.__new__(m.App)
    app.cfg = cfg
    app.engine = FakeEngine(raw)
    app.output = FakeOutput("")
    app.corrector = FakeCorrector(corrected)
    app.dictionary = Dictionary(cfg["dictionary"], path=os.path.join(BASE, "dictionary.yaml"))
    app.converter = Converter(cfg["converter"])
    app.scenes = SceneDetector(cfg["scenes"])
    app.history = History(3, 300)
    app.recent = RecentRecords(path=os.path.join(tempfile.mkdtemp(), "recent.json"))
    app.learner = Learner({"enabled": True, "ai_min_count": ai_min_count},
                          path=os.path.join(tempfile.mkdtemp(), "learning.json"))
    app.edit_watcher = FakeWatcher()
    app.style = FakeStyle(["「OK」用大寫英文"])
    app.tray = FakeTray()
    app.ui = FakeUI()
    app.overlay = None
    app._recording = False
    app.suggested = []
    app._suggest_word = app.suggested.append    # 不真的開卡片，只記下來
    job = m.Job(np.zeros(16000, np.float32), 1.0, "Notepad.exe", "", released_at=time.perf_counter())
    return app, job


def test_dictation_learns_ai_fix_and_watches_field():
    app, job = _app("你沒有像ChangeGPT一樣嗎", "你沒有像ChatGPT一樣嗎？")
    app._process(job)
    assert app.output.sent == ["你沒有像 ChatGPT 一樣嗎？"]
    assert app.edit_watcher.watched == [("你沒有像 ChatGPT 一樣嗎？", "Notepad.exe")]
    assert [(s.wrong, s.right) for s in app.suggested] == [("ChangeGPT", "ChatGPT")]


def test_ai_fix_asked_only_after_enough_times():
    app, job = _app("你沒有像ChangeGPT一樣嗎", "你沒有像ChatGPT一樣嗎？", ai_min_count=2)
    app._process(job)
    assert app.suggested == []
    assert app.learner.ai_count("ChangeGPT", "ChatGPT") == 1


def test_learning_turned_off_does_nothing():
    app, job = _app("你沒有像ChangeGPT一樣嗎", "你沒有像ChatGPT一樣嗎？")
    app.learner.enabled = False
    app.style.enabled = False
    app._process(job)
    assert app.output.sent, "關掉學習照樣要打字"
    assert app.edit_watcher.watched == [] and app.suggested == []


def test_style_alone_still_watches_edits():
    app, job = _app("你沒有像ChangeGPT一樣嗎", "你沒有像ChatGPT一樣嗎？")
    app.learner.enabled = False
    app._process(job)
    assert app.edit_watcher.watched, "個人化風格也需要知道使用者自己改了什麼"
    assert app.suggested == []


def test_dictation_uses_and_records_style():
    app, job = _app("OK我知道了", "OK，我知道了。")
    app._process(job)
    assert app.corrector.style_rules == ["「OK」用大寫英文"]
    assert app.style.recorded == [("Notepad.exe", "一般模式", "OK 我知道了", "OK，我知道了。")]


def test_user_edit_goes_to_style_and_learner():
    app, job = _app("x", "x")
    app.dictionary.current()
    app._on_user_edit("我想要深圖的話", "我想要生圖的話", "Notepad.exe")
    assert app.style.edits == [("Notepad.exe", "我想要深圖的話", "我想要生圖的話")]
    assert [(s.wrong, s.right) for s in app.suggested] == [("深圖", "生圖")]
