"""詞庫自動學習：比對改了哪些詞、次數紀錄、觀察輸入框。"""

import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from voice_input.dictionary import DictionaryData  # noqa: E402
from voice_input.field_watch import EditWatcher  # noqa: E402
from voice_input.learner import Learner, edited_region, extract_pairs, is_term, locate  # noqa: E402


# ── 比對改了哪些詞（句子取自實際日誌）──

def test_english_names():
    before = "你沒有像 ChangeGPT 或 Cloud Code 一樣有桌面版的應用程式就對了。"
    after = "你沒有像 ChatGPT 或 Claude Code 一樣有桌面版的應用程式就對了。"
    assert extract_pairs(before, after) == [("ChangeGPT", "ChatGPT"), ("Cloud Code", "Claude Code")]


def test_word_fixed_and_period_removed_together():
    # 2026-10-22 實測漏掉的：技術模式 AI 會拿掉句尾句號，剛好句尾的詞也被改對
    assert extract_pairs("或者是 Claude Code 跟 ChangeGPT。", "或者是 Claude Code 跟 ChatGPT") == \
        [("ChangeGPT", "ChatGPT")]
    assert extract_pairs("我想要深圖。", "我想要生圖") == [("深圖", "生圖")]
    assert extract_pairs("好，深圖嗎", "好深圖嗎？") == []


def test_one_wrong_english_word_takes_neighbor_word():
    assert extract_pairs("我用 VS Cold 寫程式", "我用 VS Code 寫程式") == [("VS Cold", "VS Code")]


def test_single_chinese_char_takes_neighbor():
    assert extract_pairs("我想要深圖的話", "我想要生圖的話") == [("深圖", "生圖")]


def test_spaces_added_by_converter_are_removed():
    before = "全 GVD 也順便裝，因為全 GVD 我也有買。"
    after = "Gemini 也順便裝，因為 Gemini 我也有買。"
    assert extract_pairs(before, after) == [("全GVD", "Gemini")]


def test_filler_and_punctuation_only_are_ignored():
    assert extract_pairs("嗯,再換一個。", "再換一個。") == []
    assert extract_pairs("呃我明天要開會", "我明天要開會。") == []
    assert extract_pairs("好喔，晚點再說。", "好喔晚點再說") == []


def test_number_format_is_ignored():
    assert extract_pairs("我明天三點開會", "我明天3點開會") == []


def test_added_words_are_not_a_mishearing():
    before = "可是因為我很久沒有它被盜用,以後可能貼一些色情或其他的東西導致被封鎖了。"
    after = "可是因為我很久沒有用它，被盜用的可能性很高，以後可能會貼一些色情或其他東西，導致被封鎖。"
    assert extract_pairs(before, after) == []


def test_rewritten_sentence_is_ignored():
    assert extract_pairs("今天天氣很好", "完全不同的一句話了喔") == []


def test_is_term():
    assert is_term("Claude Code") and is_term("buy+1")
    assert not is_term("生圖")


# ── 從輸入框找出使用者改了什麼 ──

def test_locate_requires_exactly_one_match():
    assert locate("前文。我要開會。", "我要開會。") == 3
    assert locate("好。好。", "好。") == -1
    assert locate("前文", "沒有") == -1


def test_edited_region():
    typed = "我用 Cloud Code 寫程式。"
    baseline = "前面的字。" + typed + "\n後面的字"
    start = locate(baseline, typed)
    current = "前面的字。我用 Claude Code 寫程式。\n後面的字"
    assert edited_region(baseline, start, typed, current) == "我用 Claude Code 寫程式。"


def test_edited_region_context_changed():
    typed = "晚點再說"
    baseline = "晚點再說"
    assert edited_region(baseline, 0, typed, "") == ""          # 整句被刪掉（觀察的一方會當成「不算」）
    assert edited_region("前文" + typed, 2, typed, "改了前文" + typed) is None


# ── 次數紀錄 ──

def _learner(tmp, **cfg):
    return Learner({"enabled": True, "ai_min_count": 2, **cfg}, path=os.path.join(tmp, "learning.json"))


def test_ai_needs_two_times_and_is_saved():
    tmp = tempfile.mkdtemp()
    learner = _learner(tmp)
    before, after = "我在用 Cloud Code 寫", "我在用 Claude Code 寫"
    assert learner.observe_ai(before, after) == []
    # 重開程式後次數還在
    learner = _learner(tmp)
    got = learner.observe_ai(before, after)
    assert [(s.wrong, s.right, s.source, s.count) for s in got] == [("Cloud Code", "Claude Code", "ai", 2)]


def test_declined_is_never_asked_again():
    tmp = tempfile.mkdtemp()
    learner = _learner(tmp, ai_min_count=1)
    assert learner.observe_ai("我想要深圖的話", "我想要生圖的話")
    learner.decline("深圖", "生圖")
    assert learner.observe_ai("我想要深圖的話", "我想要生圖的話") == []
    assert _learner(tmp, ai_min_count=1).observe_edit("要深圖嗎", "要生圖嗎") == []


def test_already_in_dictionary_is_skipped():
    tmp = tempfile.mkdtemp()
    learner = _learner(tmp, ai_min_count=1)
    words = DictionaryData({"replacements": {"Cloud Code": "Claude Code"}})
    assert learner.observe_ai("用 Cloud Code 寫", "用 Claude Code 寫", words) == []


def test_disabled_learns_nothing():
    tmp = tempfile.mkdtemp()
    learner = _learner(tmp, ai_min_count=1)
    learner.enabled = False
    assert learner.observe_ai("我想要深圖的話", "我想要生圖的話") == []
    assert learner.observe_edit("我想要深圖的話", "我想要生圖的話") == []


def test_forget_resets_count():
    tmp = tempfile.mkdtemp()
    learner = _learner(tmp)
    learner.observe_ai("我想要深圖的話", "我想要生圖的話")
    learner.forget("深圖", "生圖")
    assert learner.ai_count("深圖", "生圖") == 0


# ── 觀察輸入框（用假的讀取器）──

class FakeReader:
    """依序回傳 texts 裡的內容，用完就一直回傳最後一個。"""

    def __init__(self, texts):
        self.texts = list(texts)

    def focused(self):
        return "el"

    def read(self, el):
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]


def _watcher(texts, max_seconds=0.3):
    edits = []
    w = EditWatcher({"poll_seconds": 0.01, "watch_seconds": max_seconds},
                    on_edit=lambda typed, edited, app: edits.append((typed, edited)), start=False)
    w.BASELINE_DELAYS = (0.0, 0.0)
    w._reader = FakeReader(texts)
    return w, edits


def test_watch_reports_user_edit():
    typed = "我想要深圖的話"
    w, edits = _watcher(["前文。" + typed, "前文。" + typed, "前文。我想要生圖的話"])
    w._watch_one(typed, "Notepad.exe", w._gen)
    assert edits == [(typed, "我想要生圖的話")]


def test_watch_keeps_edit_made_before_message_sent():
    typed = "晚點再說"
    # 改字 → 按 Enter 送出後輸入框被清空：要記得清空之前的樣子
    w, edits = _watcher([typed, "晚一點再說", ""])
    w._watch_one(typed, "LINE.exe", w._gen)
    assert edits == [(typed, "晚一點再說")]


def test_watch_no_edit_reports_nothing():
    typed = "我想要生圖的話"
    w, edits = _watcher([typed])
    w._watch_one(typed, "Notepad.exe", w._gen)
    assert edits == []


def test_watch_stops_when_next_sentence_starts():
    typed = "我想要深圖的話"
    w, edits = _watcher([typed, "我想要生圖的話"], max_seconds=30)
    gen = w._gen
    w.finish()   # 使用者馬上開始錄下一句
    t0 = time.monotonic()
    w._watch_one(typed, "Notepad.exe", gen)
    assert time.monotonic() - t0 < 1.0
    assert edits == []


def test_watch_skips_terminal_and_unreadable():
    w, _ = _watcher(["x"])
    w.skip_apps = {"windowsterminal.exe"}
    w.watch("打的字", "WindowsTerminal.exe")
    assert w._queue.empty()
    w2, edits = _watcher([None])
    w2._watch_one("打的字", "Game.exe", w2._gen)
    assert edits == []
