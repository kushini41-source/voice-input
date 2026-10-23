"""個人詞庫的測試：完全比對、拼音相近比對、門檻邊界、snippets 整句命中、熱更新。"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.dictionary import (  # noqa: E402
    DEFAULT_DICTIONARY, Dictionary, pinyin_similarity, syllable_similarity,
)

SAMPLE = """\
vocabulary:
  - Claude Code
  - CapsWriter
replacements:
  "克勞德扣的": "Claude Code"
  "克勞德": "Claude"
  "皮森": "Python"
  "吉特哈布": "GitHub"
rules:
  - pattern: "(\\\\d+)\\\\s*點\\\\s*(\\\\d+)\\\\s*版"
    replace: "v\\\\1.\\\\2"
snippets:
  "我的信箱": "you@example.com"
  "簽名檔": "祝 順心\\n（你的名字）"
"""


def _make(tmp_path, content=SAMPLE, threshold=0.85, notes=None):
    path = tmp_path / "dictionary.yaml"
    path.write_text(content, encoding="utf-8")
    notify = (lambda t, m: notes.append(m)) if notes is not None else None
    return Dictionary({"fuzzy_threshold": threshold, "fuzzy_match": True}, path=str(path), notify=notify), path


# ── 完全比對 ──

def test_exact_replacement(tmp_path):
    d, _ = _make(tmp_path)
    text, is_snippet, hits = d.process("我覺得皮森很好寫")
    assert text == "我覺得 Python 很好寫"
    assert not is_snippet
    assert hits[0][1] == "Python"


def test_longest_key_wins(tmp_path):
    d, _ = _make(tmp_path)
    assert d.process("克勞德扣的很好用")[0] == "Claude Code 很好用"
    assert d.process("克勞德很聰明")[0] == "Claude 很聰明"


def test_simplified_input_matches_traditional_key(tmp_path):
    """辨識結果是簡體也要能換。"""
    d, _ = _make(tmp_path)
    assert d.process("克劳德扣的很好用")[0] == "Claude Code 很好用"


def test_replacement_output_not_replaced_again(tmp_path):
    d, _ = _make(tmp_path, "replacements:\n  甲乙: 皮森\n  皮森: Python\n")
    assert d.process("甲乙")[0] == "皮森"


def test_no_space_added_next_to_punctuation(tmp_path):
    d, _ = _make(tmp_path)
    assert d.process("用皮森，很好")[0] == "用 Python，很好"
    assert d.process("皮森")[0] == "Python"


# ── 拼音相近比對 ──

def test_homophone_replaced(tmp_path):
    """同音不同字：克老德 = ke lao de。"""
    d, _ = _make(tmp_path)
    text, _, hits = d.process("克老德扣的很好用")
    assert text == "Claude Code 很好用"
    assert hits[0][0] == "克老德扣的"


def test_taiwan_accent_variant_replaced(tmp_path):
    """捲舌不捲舌：皮神（pi shen）≈ 皮森（pi sen）。"""
    d, _ = _make(tmp_path)
    assert d.process("我在學皮神")[0] == "我在學 Python"


def test_different_sound_not_replaced(tmp_path):
    d, _ = _make(tmp_path)
    assert d.process("皮膚很好")[0] == "皮膚很好"
    assert d.process("我們開會吧")[0] == "我們開會吧"


def test_threshold_boundary():
    # 一個音節只差口音（0.9），兩個字平均 0.95
    assert pinyin_similarity("皮森", "皮神") == pytest.approx(0.95)
    assert syllable_similarity("shen", "sen") == 0.9
    assert syllable_similarity("sen", "sen") == 1.0


def test_threshold_controls_replacement(tmp_path):
    strict, _ = _make(tmp_path, threshold=1.0)
    assert strict.process("我在學皮神")[0] == "我在學皮神"
    loose, _ = _make(tmp_path, threshold=0.95)
    assert loose.process("我在學皮神")[0] == "我在學 Python"


def test_fuzzy_can_be_disabled(tmp_path):
    path = tmp_path / "dictionary.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    d = Dictionary({"fuzzy_threshold": 0.85, "fuzzy_match": False}, path=str(path))
    assert d.process("克老德很聰明")[0] == "克老德很聰明"
    assert d.process("克勞德很聰明")[0] == "Claude 很聰明"


# ── 規則 ──

def test_regex_rule(tmp_path):
    d, _ = _make(tmp_path)
    assert d.process("升級到 2 點 5 版")[0] == "升級到 v2.5"


def test_bad_rule_is_skipped(tmp_path):
    d, _ = _make(tmp_path, "rules:\n  - pattern: \"(壞掉\"\n    replace: x\n  - pattern: 好\n    replace: 讚\n")
    assert d.process("好")[0] == "讚"


# ── 快捷片語 ──

def test_snippet_exact(tmp_path):
    d, _ = _make(tmp_path)
    text, is_snippet, _ = d.process("我的信箱。")
    assert is_snippet
    assert text == "you@example.com"


def test_snippet_simplified_and_homophone(tmp_path):
    d, _ = _make(tmp_path)
    assert d.process("我的信箱")[1]
    assert d.process("我的信相")[0] == "you@example.com"


def test_snippet_multiline(tmp_path):
    d, _ = _make(tmp_path)
    assert d.process("簽名檔")[0] == "祝 順心\n（你的名字）"


def test_snippet_only_whole_sentence(tmp_path):
    d, _ = _make(tmp_path)
    text, is_snippet, _ = d.process("請寄到我的信箱")
    assert not is_snippet
    assert text == "請寄到我的信箱"


# ── 提示詞 ──

def test_prompt_terms(tmp_path):
    d, _ = _make(tmp_path)
    terms = d.current().prompt_terms
    assert terms[:2] == ["Claude Code", "CapsWriter"]
    assert "Python" in terms and "GitHub" in terms
    assert len(terms) == len(set(terms))


# ── 熱更新 ──

def test_hot_reload(tmp_path):
    d, path = _make(tmp_path)
    assert d.process("吉特哈布")[0] == "GitHub"
    time.sleep(0.05)
    path.write_text(SAMPLE + '  "新的片語": "新內容"\n', encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert d.check_for_changes()
    assert d.process("新的片語")[0] == "新內容"


def test_bad_yaml_keeps_previous_version(tmp_path):
    notes = []
    d, path = _make(tmp_path, notes=notes)
    path.write_text("replacements: [壞掉的格式\n", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert not d.check_for_changes()
    assert d.process("皮森")[0] == "Python"
    assert notes, "格式錯誤時要跳通知"
    # 同一個壞掉的版本不會一直重複跳通知
    assert not d.check_for_changes()
    assert len(notes) == 1


def test_missing_file_creates_default(tmp_path):
    path = tmp_path / "dictionary.yaml"
    d = Dictionary({}, path=str(path))
    assert path.read_text(encoding="utf-8") == DEFAULT_DICTIONARY
    assert d.process("我的信箱")[0] == "you@example.com"


# ── 驗收句 4、5 ──

def test_acceptance_4_and_5():
    """用實際的預設詞庫檢查 PRD 驗收句 4、5。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "dictionary.yaml")
        d = Dictionary({}, path=path)
        assert d.process("克勞德扣的很好用")[0] == "Claude Code 很好用"
        assert d.process("我的信箱")[0] == "you@example.com"


def test_speed(tmp_path):
    """詞庫替換要在 30 毫秒內（PRD 效能目標）。"""
    d, _ = _make(tmp_path)
    sentence = "我覺得克老德扣的跟皮神都很好用，但是吉特哈布上面的專案還要再整理一下才能發布"
    d.process(sentence)  # 先熱一下快取
    t = time.perf_counter()
    for _ in range(10):
        d.process(sentence)
    assert (time.perf_counter() - t) / 10 < 0.03


# ── 從程式寫入詞庫（「把上一句加入詞庫」對話框用）──

from voice_input.dictionary import add_to_dictionary  # noqa: E402


def test_add_replacement_keeps_comments_and_order(tmp_path):
    d, path = _make(tmp_path, DEFAULT_DICTIONARY)
    assert add_to_dictionary(str(path), replacements=[("酷伯耐", "Kubernetes")]) == 1
    text = path.read_text(encoding="utf-8")
    assert "# 1. 專有名詞" in text and "# 4. 語音快捷片語" in text   # 註解都還在
    # 新的一行加在 replacements 區塊最後、rules 區塊之前
    assert text.index('"酷伯耐": "Kubernetes"') < text.index("rules:")
    assert text.index('"吉特哈布"') < text.index('"酷伯耐"')
    assert d.check_for_changes()
    assert d.process("酷伯耐")[0] == "Kubernetes"


def test_add_vocabulary(tmp_path):
    d, path = _make(tmp_path, DEFAULT_DICTIONARY)
    assert add_to_dictionary(str(path), vocabulary=["Kubernetes"]) == 1
    assert d.check_for_changes()
    assert "Kubernetes" in d.current().vocabulary


def test_add_skips_duplicates(tmp_path):
    _, path = _make(tmp_path, DEFAULT_DICTIONARY)
    assert add_to_dictionary(str(path), replacements=[("皮森", "Python")], vocabulary=["GitHub"]) == 0


def test_add_special_characters(tmp_path):
    d, path = _make(tmp_path, DEFAULT_DICTIONARY)
    add_to_dictionary(str(path), replacements=[('引號"測試', "反斜線\和冒號: 都可以")])
    assert d.check_for_changes()
    assert d.current().replacements['引號"測試'] == "反斜線\和冒號: 都可以"


def test_add_to_missing_or_empty_section(tmp_path):
    d, path = _make(tmp_path, "vocabulary:\n  - A\nreplacements: {}\n")
    add_to_dictionary(str(path), replacements=[("甲乙", "丙")])
    assert d.check_for_changes()
    assert d.current().replacements == {"甲乙": "丙"}

    other = tmp_path / "other.yaml"
    other.write_text("vocabulary:\n  - A\n", encoding="utf-8")
    add_to_dictionary(str(other), replacements=[("甲乙", "丙")])
    import yaml
    assert yaml.safe_load(other.read_text(encoding="utf-8"))["replacements"] == {"甲乙": "丙"}


def test_add_refuses_broken_file(tmp_path):
    _, path = _make(tmp_path)
    path.write_text("replacements: [壞掉\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(Exception):
        add_to_dictionary(str(path), replacements=[("甲", "乙")])
    assert path.read_text(encoding="utf-8") == before
