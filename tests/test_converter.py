"""OpenCC 轉換、保護詞、誤轉修正表的測試：config.yaml 裡每一個保護詞和修正項目都要有案例。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.config import load_config  # noqa: E402
from voice_input.converter import Converter  # noqa: E402


@pytest.fixture(scope="module")
def converter():
    return Converter(load_config()["converter"])


# 保護詞：簡體、繁體輸入都要保持原詞，不能被 OpenCC 換成別的用語
PROTECT_CASES = [
    ("这个项目很重要", "這個項目很重要"),
    ("這個項目很重要", "這個項目很重要"),
    ("这是连接词", "這是連接詞"),
    ("前后缀都要处理", "前後綴都要處理"),
    ("这个参数要传", "這個參數要傳"),
    ("请使用台湾常用的词汇", "請使用台灣常用的詞彙"),
]

# 保護詞不能誤傷意思不同的詞：真的講「專案」就要打出「專案」
DIFFERENT_WORD_CASES = [
    ("这个专案下周结案", "這個專案下週結案"),
    ("這個專案下週結案", "這個專案下週結案"),
    ("网络连线很慢", "網路連線很慢"),
]

# 修正表：OpenCC 斷詞錯誤
FIX_CASES = [
    ("这只能这样", "這只能這樣"),
    ("那只能算了", "那只能算了"),
]

# 本來就正確、不能被改壞的用法
UNTOUCHED = [
    ("一只猫", "一隻貓"),
    ("两只手", "兩隻手"),
    ("这只是测试", "這只是測試"),
    ("我们只能等", "我們只能等"),
    ("只要你说", "只要你說"),
    ("软件", "軟體"),
    ("数据库", "資料庫"),
]


@pytest.mark.parametrize("src, expected", PROTECT_CASES + DIFFERENT_WORD_CASES + FIX_CASES + UNTOUCHED)
def test_convert(converter, src, expected):
    assert converter.convert(src) == expected


def test_every_protect_word_has_a_case(converter):
    covered = {w for w in converter.protect for _, out in PROTECT_CASES if w in out}
    missing = set(converter.protect) - covered
    assert not missing, f"這些保護詞沒有測試案例：{missing}"


def test_every_fix_entry_has_a_case(converter):
    import opencc
    s2twp = opencc.OpenCC("s2twp")
    covered = {w for w in converter.fixes for src, _ in FIX_CASES if w in s2twp.convert(src)}
    missing = set(converter.fixes) - covered
    assert not missing, f"這些修正項目沒有測試案例：{missing}"


def test_fixes_are_single_pass():
    """換完的結果不會再被別條規則換第二次。"""
    c = Converter({"opencc_config": "s2twp", "fixes": {"甲": "乙", "乙": "丙"}})
    assert c.apply_fixes("甲乙") == "乙丙"


def test_multiple_protected_words_in_one_sentence(converter):
    assert converter.convert("项目里的连接词和项目名称") == "項目裡的連接詞和項目名稱"


def test_empty_text(converter):
    assert converter.convert("") == ""


@pytest.mark.parametrize("src, expected", [
    ("現在都是測試,所以你不用管我,這一段全部都是。", "現在都是測試，所以你不用管我，這一段全部都是。"),
    ("真的嗎?好!", "真的嗎？好！"),
    ("注意:先備份", "注意：先備份"),
    ("版本是 3.14 嗎", "版本是 3.14 嗎"),
    ("參數 a, b 都要傳", "參數 a, b 都要傳"),
    ("幫我把這個 function 改成 async.", "幫我把這個 function 改成 async."),
    ("結束了.", "結束了。"),
])
def test_fullwidth_punctuation(converter, src, expected):
    assert converter.convert(src) == expected


@pytest.mark.parametrize("src, expected", [
    ("Claude Code很好用", "Claude Code 很好用"),
    ("幫我把這個function改成async", "幫我把這個 function 改成 async"),
    ("我用Python3寫", "我用 Python3 寫"),
    ("升級到v2.5版", "升級到 v2.5 版"),
    ("下午3點開會", "下午3點開會"),
    ("已經有 空格 的不會變兩個", "已經有 空格 的不會變兩個"),
    ("用C++和C#", "用 C++ 和 C#"),
])
def test_space_between_chinese_and_english(converter, src, expected):
    assert converter.convert(src) == expected
