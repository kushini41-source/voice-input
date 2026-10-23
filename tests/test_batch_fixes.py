"""2026-10-23 一起修的 8 項：語助詞、分段、刪節號、改字自動學＋復原、讀 Claude/Chrome 輸入框、金鑰失效通知、風格整理重試。"""

import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import yaml  # noqa: E402

from test_command_flow import FakeTray, _load_main  # noqa: E402
from voice_input import corrector  # noqa: E402
from voice_input.config import load_config  # noqa: E402
from voice_input.converter import Converter  # noqa: E402
from voice_input.dictionary import add_to_dictionary, remove_from_dictionary  # noqa: E402
from voice_input.learner import Learner, edited_region, locate, normalize_field_text, sounds_alike  # noqa: E402
from voice_input.style import StyleLearner, StyleProfile, StyleSamples  # noqa: E402


# ── 刪節號 ──

def test_ellipsis_is_not_turned_into_period():
    c = Converter(load_config()["converter"])
    assert c.convert("你們不是有連結...額...是不是") == "你們不是有連結……額……是不是"
    assert c.convert("我有做一個...") == "我有做一個……"
    assert c.convert("版本 v2.5 跟 3.14") == "版本 v2.5 跟 3.14"
    assert c.convert("網址 www.abc.com") == "網址 www.abc.com"


# ── AI 校正規則 ──

def test_particle_rule_switch():
    with_rule = corrector.build_system_prompt(True)
    assert "使用者不要語助詞" in with_rule and with_rule.index("使用者不要語助詞") < with_rule.index("\n絕對禁止：")
    assert "使用者不要語助詞" not in corrector.build_system_prompt(False)
    assert "換話題的地方分段" in corrector.SYSTEM_PROMPT


def test_long_text_gets_more_time():
    seen = []

    class SlowBackend(corrector.LLMBackend):
        def complete(self, system, user, timeout):
            seen.append(timeout)
            return "好。"

    c = corrector.Corrector({"timeout_seconds": 4}, backend=SlowBackend())
    c.correct("短句", {"name": "一般模式", "instructions": ""}, [], [])
    c.correct("長" * 380, {"name": "一般模式", "instructions": ""}, [], [])
    assert seen[0] == 4 and seen[1] == 9


# ── 讀 Claude／Chrome 輸入框：換行、空白不同也要對得上 ──

def test_paragraph_breaks_are_normalized():
    typed = normalize_field_text("今天先把發文排好。\n\n明天再改深圖的部分。")
    field = normalize_field_text("前面的字。\n今天先把發文排好。\n明天再改深圖的部分。\n")
    start = locate(field, typed)
    assert start >= 0
    edited = normalize_field_text("前面的字。\n今天先把發文排好。\n明天再改生圖的部分。\n")
    assert edited_region(field, start, typed, edited) == "今天先把發文排好。\n明天再改生圖的部分。"
    assert normalize_field_text("a\xa0 b  c") == "a b c"


# ── 唸起來像不像 ──

def test_sounds_alike():
    assert sounds_alike("深圖", "生圖") and sounds_alike("桌面板", "桌面版") and sounds_alike("注射", "註冊")
    assert sounds_alike("明天", "後天") is False and sounds_alike("開會", "吃飯") is False
    assert sounds_alike("ChangeGPT", "ChatGPT") and sounds_alike("google", "Google") is False
    assert sounds_alike("拜加溫", "buy+1") is None


# ── 自動學 ──

def _learner(**cfg):
    return Learner({"enabled": True, "ai_min_count": 2, "ai_auto_count": 3, **cfg},
                   path=os.path.join(tempfile.mkdtemp(), "learning.json"))


def test_user_edit_that_sounds_alike_is_learned_automatically():
    got = _learner().observe_edit("我想要深圖的話", "我想要生圖的話")
    assert [(s.wrong, s.right, s.auto) for s in got] == [("深圖", "生圖", True)]


def test_user_change_of_mind_is_asked_not_learned():
    got = _learner().observe_edit("我們明天開會", "我們後天開會")
    assert [(s.wrong, s.right, s.auto) for s in got] == [("明天", "後天", False)]


def test_auto_from_edits_off_always_asks():
    assert _learner(auto_from_edits=False).observe_edit("我想要深圖的話", "我想要生圖的話")[0].auto is False


def test_ai_fix_asks_at_two_and_learns_at_three():
    l = _learner()
    before, after = "我想要深圖的話", "我想要生圖的話"
    assert l.observe_ai(before, after) == []
    second = l.observe_ai(before, after)
    assert [(s.auto, s.count) for s in second] == [(False, 2)]
    third = l.observe_ai(before, after)
    assert [(s.auto, s.count) for s in third] == [(True, 3)]


def test_ai_rewrites_and_function_words_are_ignored():
    l = _learner(ai_min_count=1)
    assert l.observe_ai("我們明天開會", "我們後天開會") == []
    assert l.observe_ai("就是我現在的用量", "顯示我現在的用量") == []


def test_undo_auto_blocks_relearning():
    l = _learner()
    l.mark_auto("深圖", "生圖", False)
    assert l.last_auto()["wrong"] == "深圖"
    assert l.undo_auto("深圖", "生圖")["right"] == "生圖"
    assert l.last_auto() is None
    assert l.observe_edit("我想要深圖的話", "我想要生圖的話") == []


# ── 詞庫：自動加（有標註）、復原拿掉 ──

def test_add_with_comment_and_remove():
    from voice_input.dictionary import DEFAULT_DICTIONARY
    path = os.path.join(tempfile.mkdtemp(), "dictionary.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(DEFAULT_DICTIONARY)
    original_keys = set((yaml.safe_load(open(path, encoding="utf-8")) or {}).get("replacements") or {})
    assert add_to_dictionary(path, [("深圖", "生圖")], ["ChatGPT"], comment="自動學的 2026-10-23") == 2
    text = open(path, encoding="utf-8").read()
    assert '"深圖": "生圖"  # 自動學的 2026-10-23' in text
    assert remove_from_dictionary(path, "深圖", vocabulary_term="ChatGPT") == 2
    data = yaml.safe_load(open(path, encoding="utf-8"))
    assert "深圖" not in data["replacements"] and "ChatGPT" not in data["vocabulary"]
    assert set(data["replacements"]) == original_keys, "其他詞不能被動到"
    assert remove_from_dictionary(path, "不存在的詞") == 0


# ── 風格整理失敗不要每句重試 ──

def test_style_failure_waits_before_retry():
    class Broken:
        def complete_json(self, *a, **k):
            raise RuntimeError("401 invalid x-api-key")

    calls = []
    learner = StyleLearner({"update_every": 5, "min_samples": 5}, lambda: Broken(),
                           profile=StyleProfile(path=os.path.join(tempfile.mkdtemp(), "style.yaml")),
                           samples=StyleSamples(path=os.path.join(tempfile.mkdtemp(), "s.json")), seed_from_logs=False)
    for i in range(5):
        learner.samples.add("", "", f"好{i}", f"好{i}")
    assert learner.update_now()[0] is False
    learner.update_now = lambda manual=False: calls.append(1)
    learner.record("", "", "再一句", "再一句")
    time.sleep(0.1)
    assert calls == [], "失敗後 30 分鐘內不要再自動重試"


# ── 金鑰失效要通知（同一類只通知一次）──

def test_key_problem_notifies_once():
    m = _load_main()
    app = m.App.__new__(m.App)
    app.tray, app._ai_warned = FakeTray(), {}
    app._warn_ai_account("AI 呼叫失敗：Error code: 401 - invalid x-api-key")
    app._warn_ai_account("AI 呼叫失敗：Error code: 401 - invalid x-api-key")
    app._warn_ai_account("AI 超過 4 秒沒回應")
    assert len(app.tray.notes) == 1 and "api_key.txt" in app.tray.notes[0]
    app._warn_ai_account("Your credit balance is too low to access the Anthropic API")
    assert len(app.tray.notes) == 2
