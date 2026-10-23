"""AI 校正的測試：防呆（擴寫／回答問題／前綴廢話要被捨棄）、逾時退回、取消、場景判斷、對話歷史。

這些測試都不會真的連網，用假的 LLM 後端模擬各種回應。
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.config import load_config  # noqa: E402
from voice_input.context import DEFAULT_SCENE, History, SceneDetector  # noqa: E402
from voice_input.corrector import (  # noqa: E402
    SYSTEM_PROMPT, Corrector, LLMBackend, build_user_message, check_output, clean_output,
)

KEEP = ["Claude Code", "buy+1", "GitHub"]


class FakeBackend(LLMBackend):
    def __init__(self, reply="", delay=0.0, error=None):
        self.reply, self.delay, self.error = reply, delay, error
        self.calls = []

    def complete(self, system, user, timeout):
        self.calls.append((system, user))
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply


def _corrector(backend, **cfg):
    base = {"enabled": True, "timeout_seconds": 0.5, "max_length_ratio": 1.5}
    base.update(cfg)
    return Corrector(base, backend=backend)


# ── 防呆：要被捨棄的 AI 輸出 ──

@pytest.mark.parametrize("original, ai_output", [
    # 擴寫
    ("明天開會", "明天上午十點在三樓大會議室召開季度業績檢討會議，請全體同仁準時出席並準備報告。"),
    # 回答問題
    ("你可以幫我查一下明天台北的天氣嗎", "好的，明天台北天氣晴，氣溫約 25 到 30 度，降雨機率 10%。"),
    ("什麼是遞迴", "遞迴是一種函式呼叫自己的程式設計技巧，常用於樹狀結構走訪、分治演算法等情境之中。"),
    # 前綴廢話
    ("現在開始測試", "以下是校正後的文字：現在開始測試。"),
    ("現在開始測試", "好的，現在開始測試。"),
    ("現在開始測試", "校正後：現在開始測試。"),
    ("現在開始測試", "Sure! 現在開始測試。"),
    ("我在寫程式", "當然，我在寫程式。"),
    # 改掉詞庫裡要保留的詞
    ("我在用 buy+1 寫東西", "我在用拜加一寫東西。"),
    ("Claude Code 很好用", "克勞德扣很好用。"),
])
def test_bad_outputs_are_rejected(original, ai_output):
    assert check_output(original, ai_output, KEEP) is not None


@pytest.mark.parametrize("original, ai_output", [
    ("呃我明天三點不對四點要開會", "我明天4點要開會。"),
    ("好喔晚點再說", "好喔晚點再說"),
    ("第一點要先備份第二點再更新", "1. 先備份\n2. 再更新"),
    ("幫我把這個 function 改成 async", "幫我把這個 function 改成 async"),
    ("好", "好。"),                                  # 短句只補標點不算擴寫
    ("呃好的我知道了", "好的，我知道了。"),            # 原文本來就有「好的」
    ("以下是我的想法第一要先做測試", "以下是我的想法：1. 要先做測試。"),
    ("我在用 buy+1", "我在用 buy+1。"),
    ("好,我要晚點再說。", "好，我要晚點再說。"),       # 原文是半形逗號，AI 改成全形，仍是同一個「好」
    ("好的 我知道了", "好的，我知道了。"),
])
def test_good_outputs_are_accepted(original, ai_output):
    assert check_output(original, ai_output, KEEP) is None


def test_corrector_falls_back_on_bad_output():
    c = _corrector(FakeBackend("好的，以下是校正結果：今天天氣很好。"))
    r = c.correct("今天天氣很好", DEFAULT_SCENE, [], KEEP)
    assert r.text == "今天天氣很好"
    assert not r.used_ai
    assert r.reason  # 可能被「太長」或「前言」任一項攔下


def test_corrector_uses_good_output():
    c = _corrector(FakeBackend("我明天4點要開會。"))
    r = c.correct("呃我明天三點不對四點要開會", DEFAULT_SCENE, [], KEEP)
    assert r.used_ai
    assert r.text == "我明天4點要開會。"


# ── 逾時、錯誤、取消、關閉 ──

def test_timeout_falls_back_quickly():
    c = _corrector(FakeBackend("太慢了", delay=2.0), timeout_seconds=0.3)
    t = time.perf_counter()
    r = c.correct("原本的句子", DEFAULT_SCENE, [], KEEP)
    assert time.perf_counter() - t < 0.8
    assert r.text == "原本的句子" and not r.used_ai
    assert "秒" in r.reason


def test_api_error_falls_back():
    c = _corrector(FakeBackend(error=RuntimeError("網路斷線")))
    r = c.correct("原本的句子", DEFAULT_SCENE, [], KEEP)
    assert r.text == "原本的句子" and not r.used_ai
    assert "網路斷線" in r.reason


def test_cancel_returns_immediately():
    c = _corrector(FakeBackend("結果", delay=2.0), timeout_seconds=3)
    t = time.perf_counter()
    r = c.correct("原本的句子", DEFAULT_SCENE, [], KEEP, is_cancelled=lambda: True)
    assert time.perf_counter() - t < 0.3
    assert not r.used_ai and "Esc" in r.reason


def test_disabled_skips_backend():
    backend = FakeBackend("不應該被呼叫")
    c = _corrector(backend, enabled=False)
    r = c.correct("原本的句子", DEFAULT_SCENE, [], KEEP)
    assert r.text == "原本的句子" and not backend.calls


def test_missing_api_key_disables_gracefully(monkeypatch):
    import voice_input.corrector as corrector_mod
    monkeypatch.setattr(corrector_mod, "load_api_key", lambda path=None: "")
    c = Corrector({"enabled": True, "backend": "anthropic", "model": "x"})
    assert c.backend is None and "API Key" in c.init_error
    assert c.correct("句子", DEFAULT_SCENE, [], KEEP).text == "句子"


# ── 提示詞內容 ──

def test_system_prompt_has_required_rules():
    for must in ("同音", "贅字", "口誤", "全形標點", "阿拉伯數字", "台灣繁體中文", "不可以回答", "擴寫", "必須保留的詞"):
        assert must in SYSTEM_PROMPT, must


def test_user_message_contains_context():
    scene = {"name": "技術模式", "instructions": "保留英文"}
    msg = build_user_message("改成 async", scene, ["上一句", "上上句"], KEEP)
    assert "技術模式" in msg and "保留英文" in msg
    assert "上一句" in msg and "上上句" in msg
    assert "Claude Code" in msg and "buy+1" in msg
    assert msg.rstrip().endswith("</語音辨識結果>")


def test_user_message_without_history_or_terms():
    msg = build_user_message("你好", DEFAULT_SCENE, [], [])
    assert "<前文>" not in msg and "<必須保留的詞>" not in msg


@pytest.mark.parametrize("raw, expected", [
    ("  今天很好。 ", "今天很好。"),
    ("「今天很好。」", "今天很好。"),
    ("```\n今天很好。\n```", "今天很好。"),
    ("```text\n今天很好。\n```", "今天很好。"),
    ("<語音辨識結果>今天很好。</語音辨識結果>", "今天很好。"),
    ("他說「好」然後走了", "他說「好」然後走了"),
])
def test_clean_output(raw, expected):
    assert clean_output(raw) == expected


# ── 場景判斷 ──

@pytest.fixture(scope="module")
def scenes():
    return SceneDetector(load_config()["scenes"])


@pytest.mark.parametrize("exe, title, expected", [
    ("Code.exe", "main.py - Visual Studio Code", "技術模式"),
    ("code.exe", "pipeline.py - Visual Studio Code", "技術模式"),   # 程式名稱不分大小寫；標題裡的 line 不會誤判成聊天
    ("claude.exe", "Claude", "技術模式"),
    ("WindowsTerminal.exe", "PowerShell", "技術模式"),
    ("LINE.exe", "LINE", "聊天模式"),
    ("Telegram.exe", "", "聊天模式"),
    ("WINWORD.EXE", "報告.docx - Word", "正式模式"),
    ("chrome.exe", "收件匣 - Gmail - Google Chrome", "正式模式"),
    ("chrome.exe", "YouTube - Google Chrome", "一般模式"),
    ("notepad.exe", "未命名 - 記事本", "一般模式"),
    ("", "", "一般模式"),
])
def test_scene_detection(scenes, exe, title, expected):
    assert scenes.detect(exe, title)["name"] == expected


# ── 對話歷史 ──

def test_history_keeps_last_three_per_app():
    h = History(max_items=3, ttl_seconds=300)
    for i in range(5):
        h.add("LINE.exe", f"第{i}句", now=1000 + i)
    h.add("Code.exe", "別的程式", now=1000)
    assert h.recent("line.exe", now=1010) == ["第2句", "第3句", "第4句"]
    assert h.recent("Code.exe", now=1010) == ["別的程式"]


def test_history_expires():
    h = History(max_items=3, ttl_seconds=300)
    h.add("LINE.exe", "很久以前", now=1000)
    h.add("LINE.exe", "剛剛", now=1290)
    assert h.recent("LINE.exe", now=1350) == ["剛剛"]


# ── 程式保證的格式整理 ──

from voice_input.corrector import tidy_output  # noqa: E402


@pytest.mark.parametrize("text, scene, expected", [
    ("1. 先備份。\n2. 再更新。", DEFAULT_SCENE, "1. 先備份\n2. 再更新"),
    ("1. 先備份，\n2. 再更新", DEFAULT_SCENE, "1. 先備份\n2. 再更新"),
    ("第一行。\n第二行。", DEFAULT_SCENE, "第一行。\n第二行。"),        # 不是條列，不動
    ("我用 3.14 版。", DEFAULT_SCENE, "我用 3.14 版。"),                # 一般模式句號保留
    ("好喔晚點再說。", {"no_trailing_period": True}, "好喔晚點再說"),
    ("你在哪裡？", {"no_trailing_period": True}, "你在哪裡？"),          # 問號保留
    ("版本是 v2.5", {"no_trailing_period": True}, "版本是 v2.5"),
])
def test_tidy_output(text, scene, expected):
    assert tidy_output(text, scene) == expected


def test_email_and_url_must_stay_exact():
    # 2026-10-22 實測：Sonnet 曾把信箱的點拿掉
    assert "信箱" in check_output("you.name@example.com", "youname@example.com", [])
    assert check_output("you.name@example.com", "you.name@example.com", []) is None
    assert check_output("網址是https://example.com/a?b=1", "網址是 https://example.com/a?b=1。", []) is None
    assert check_output("網址是 www.abc.com.", "網址是 www.abc.com。", []) is None
    assert "網址" in check_output("看 www.abc.com 喔", "看 www.abd.com 喔", [])
