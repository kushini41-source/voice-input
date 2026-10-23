"""個人化風格：用字習慣檔（style.yaml）、紀錄、整理、提示詞。"""

import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from voice_input import commander, corrector  # noqa: E402
from voice_input.style import (StyleLearner, StyleProfile, StyleSamples, build_user_message,  # noqa: E402
                               read_log_samples, summarize)


class FakeBackend:
    """rules：[規則, ...] 或 [(規則, [例子, ...]), ...]；只給規則時，例子用「好喔」（測試紀錄裡都有）。"""

    def __init__(self, rules=None, error=None):
        self.rules, self.error = rules, error
        self.calls = []

    def complete_json(self, system, user, schema, timeout):
        self.calls.append(user)
        if self.error:
            raise self.error
        items = [r if isinstance(r, tuple) else (r, ["好喔"]) for r in self.rules or []]
        return {"rules": [{"rule": rule, "examples": ex} for rule, ex in items]}


SAMPLES = [{"scene": "聊天模式", "said": f"好喔{i}", "final": f"好喔{i}"} for i in range(3)]


def _tmp(name):
    return os.path.join(tempfile.mkdtemp(), name)


# ── style.yaml ──

def test_profile_missing_file_has_no_rules():
    assert StyleProfile(path=_tmp("style.yaml")).rules() == []


def test_save_learned_keeps_my_rules():
    path = _tmp("style.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write("my_rules:\n  - 「喔」不要改成「哦」\nlearned: []\n")
    p = StyleProfile(path=path)
    p.save_learned(["聊天：句尾不加句號", "「OK」用大寫"])
    again = StyleProfile(path=path)
    assert again.rules() == ["「喔」不要改成「哦」", "聊天：句尾不加句號", "「OK」用大寫"]
    with open(path, encoding="utf-8") as f:
        assert "# 我的用字習慣" in f.read(), "存檔後要有中文說明"


def test_profile_reloads_after_user_edit():
    path = _tmp("style.yaml")
    p = StyleProfile(path=path)
    p.save_learned(["A"])
    time.sleep(0.05)
    with open(path, "w", encoding="utf-8") as f:
        f.write("my_rules:\n  - 我自己加的\nlearned:\n  - A\n")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert p.rules() == ["我自己加的", "A"]


def test_broken_file_keeps_last_good_and_notifies():
    path = _tmp("style.yaml")
    notes = []
    p = StyleProfile(path=path, notify=lambda t, m: notes.append(t))
    p.save_learned(["A"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("my_rules: [沒有關起來\n")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert p.rules() == ["A"]
    assert notes


# ── 紀錄 ──

def test_samples_add_edit_and_persist():
    path = _tmp("samples.json")
    s = StyleSamples(path=path, max_items=3)
    for i in range(5):
        s.add("LINE.exe", "聊天模式", f"說{i}", f"打{i}")
    assert [i["final"] for i in s.items] == ["打2", "打3", "打4"]
    assert s.attach_edit("LINE.exe", "打4", "改4")
    assert not s.attach_edit("LINE.exe", "不存在", "x")
    again = StyleSamples(path=path, max_items=3)
    assert again.items[-1]["edited"] == "改4"
    assert again.since_update == 6
    again.mark_updated()
    assert StyleSamples(path=path).since_update == 0


def test_read_log_samples():
    path = _tmp("voice_input.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "2026-10-19 23:24:14 [INFO] main：原始辨識：嗯,再換一個。 → 詞庫替換後：嗯,再換一個。 → "
            "AI 校正後：再換一個。 → 最終輸出：再換一個。\n"
            "2026-10-19 23:24:14 [INFO] main：耗時｜錄音 2.2 秒｜辨識 0.46 秒｜AI 0.81 秒｜放開到打出 1.28 秒"
            "｜程式：chrome.exe｜場景：一般模式\n"
            "2026-10-19 23:25:00 [INFO] main：準備完成\n"
        )
    assert read_log_samples([path]) == [("2026-10-19 23:24:14", "chrome.exe", "一般模式", "嗯,再換一個。", "再換一個。")]


# ── 整理 ──

def test_summarize_cleans_rules():
    rules = ["- 聊天：句尾用「喔」", "1. 「OK」用大寫", "", "聊天：句尾用「喔」", "x" * 200] + [f"規則{i}" for i in range(10)]
    out = summarize(FakeBackend(rules), SAMPLES, [])
    assert out[:2] == ["聊天：句尾用「喔」", "「OK」用大寫"]
    assert len(out) == 8 and all(len(r) <= 80 for r in out)


def test_rules_without_evidence_are_dropped():
    samples = SAMPLES + [{"scene": "聊天模式", "said": "好哦", "final": "好哦", "edited": "好喔～"}]
    backend = FakeBackend([
        ("聊天：句尾用「喔」", ["好喔0", "好喔1"]),          # 2 筆「說的」裡有：留下
        ("用「～」結尾", ["好喔～"]),                         # 使用者自己改出來的：1 筆就夠
        ("喜歡用「臺」", ["臺灣"]),                           # 紀錄裡沒有：丟掉
        ("只出現一次", ["好喔2"]),                             # 只有 1 筆：丟掉
    ])
    assert summarize(backend, samples, []) == ["聊天：句尾用「喔」", "用「～」結尾"]


def test_build_message_shows_user_edits():
    msg = build_user_message([{"scene": "聊天模式", "said": "好哦", "final": "好哦", "edited": "好喔"}], ["舊規則"])
    assert "他自己改成：好喔" in msg and "- 舊規則" in msg


def _learner(backend, **cfg):
    notes = []
    learner = StyleLearner({"update_every": 5, "min_samples": 5, **cfg}, lambda: backend,
                           notify=lambda t, m: notes.append(t),
                           profile=StyleProfile(path=_tmp("style.yaml")),
                           samples=StyleSamples(path=_tmp("samples.json")), seed_from_logs=False)
    return learner, notes


def test_updates_in_background_after_enough_sentences():
    backend = FakeBackend(["聊天：句尾用「喔」"])
    learner, notes = _learner(backend)
    for i in range(4):
        learner.record("LINE.exe", "聊天模式", f"好喔{i}", f"好喔{i}")
    time.sleep(0.2)
    assert backend.calls == []
    learner.record("LINE.exe", "聊天模式", "好喔", "好喔")   # 第 5 句：開始在背景整理
    for _ in range(50):          # 背景整理完會跳通知
        if notes:
            break
        time.sleep(0.05)
    assert learner.rules() == ["聊天：句尾用「喔」"]
    assert notes and learner.samples.since_update == 0


def test_update_failure_keeps_old_rules():
    learner, notes = _learner(FakeBackend(error=RuntimeError("網路斷線")))
    learner.profile.save_learned(["舊的"])
    for i in range(5):
        learner.samples.add("", "", f"好喔{i}", f"好喔{i}")
    ok, msg, changed = learner.update_now()
    assert not ok and "網路斷線" in msg and not changed
    assert learner.rules() == ["舊的"]


def test_manual_update_needs_some_sentences_and_ai():
    learner, _ = _learner(FakeBackend(["A"]))
    ok, msg, _ = learner.update_now(manual=True)
    assert not ok and "不夠" in msg
    learner2, _ = _learner(None)
    learner2.samples.add("", "", "a", "a")
    assert not learner2.update_now(manual=True)[0]


def test_disabled_returns_no_rules_and_records_nothing():
    learner, _ = _learner(FakeBackend(["A"]))
    learner.profile.save_learned(["A"])
    learner.enabled = False
    learner.record("", "", "a", "a")
    assert learner.rules() == [] and learner.samples.items == []


# ── 提示詞 ──

def test_corrector_and_commander_messages_include_style():
    scene = {"name": "聊天模式", "instructions": "口語"}
    msg = corrector.build_user_message("好喔", scene, [], [], ["聊天：句尾用「喔」"])
    assert "<使用者的用字習慣>\n- 聊天：句尾用「喔」\n</使用者的用字習慣>" in msg
    assert "使用者的用字習慣" not in corrector.build_user_message("好喔", scene, [], [])
    assert "- 「OK」用大寫" in commander.build_user_message("回覆好", "", scene, ["「OK」用大寫"])
