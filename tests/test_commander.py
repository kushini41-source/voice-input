"""指令模式的測試：用假的 AI 後端，不連網。"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.commander import SCHEMA, SYSTEM_PROMPT, Commander, build_user_message, validate  # noqa: E402
from voice_input.context import DEFAULT_SCENE  # noqa: E402


class FakeBackend:
    def __init__(self, reply=None, delay=0.0, error=None):
        self.reply, self.delay, self.error = reply, delay, error
        self.calls = []

    def complete_json(self, system, user, schema, timeout, max_tokens=4096):
        self.calls.append((system, user, schema))
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply


def _cmd(backend, timeout=1.0):
    return Commander({"timeout_seconds": timeout}, lambda: backend)


def test_replace_with_selection():
    b = FakeBackend({"action": "replace", "text": "明天請假。"})
    r = _cmd(b).run("改短一點", "我明天因為家裡有事情所以想要請假一天", DEFAULT_SCENE)
    assert r.ok and r.action == "replace" and r.text == "明天請假。"
    system, user, schema = b.calls[0]
    assert "我明天因為家裡有事情" in user and "改短一點" in user
    assert schema is SCHEMA and system is SYSTEM_PROMPT


def test_answer_and_insert():
    assert _cmd(FakeBackend({"action": "answer", "text": "意思是…"})).run("這是什麼意思", "選取", DEFAULT_SCENE).action == "answer"
    assert _cmd(FakeBackend({"action": "insert", "text": "主管您好…"})).run("幫我寫請假信", "", DEFAULT_SCENE).action == "insert"


def test_replace_without_selection_is_rejected():
    """沒選取文字卻要「取代」：不能照做（會把東西打到不對的地方）。"""
    r = _cmd(FakeBackend({"action": "replace", "text": "x"})).run("改短", "", DEFAULT_SCENE)
    assert not r.ok


@pytest.mark.parametrize("data", [
    None, "字串", {"action": "delete_all", "text": "x"}, {"action": "answer", "text": ""}, {"action": "answer"},
])
def test_invalid_replies(data):
    assert validate(data, has_selection=True) is not None


def test_timeout_and_errors():
    r = _cmd(FakeBackend({"action": "answer", "text": "x"}, delay=1.0), timeout=0.2).run("問題", "", DEFAULT_SCENE)
    assert not r.ok and "秒" in r.reason
    r = _cmd(FakeBackend(error=RuntimeError("網路斷線"))).run("問題", "", DEFAULT_SCENE)
    assert not r.ok and "網路斷線" in r.reason


def test_cancel():
    r = _cmd(FakeBackend({"action": "answer", "text": "x"}, delay=1.0), timeout=3).run(
        "問題", "", DEFAULT_SCENE, is_cancelled=lambda: True)
    assert not r.ok and "Esc" in r.reason


def test_no_backend():
    r = Commander({}, lambda: None).run("問題", "", DEFAULT_SCENE)
    assert not r.ok and "AI" in r.reason


def test_user_message_marks_missing_selection():
    assert "沒有選取任何文字" in build_user_message("幫我寫信", "", DEFAULT_SCENE)
    assert "<選取的文字>\n原文\n</選取的文字>" in build_user_message("改短", "原文", DEFAULT_SCENE)


# ── 讀取選取文字的保護 ──

from voice_input import output as out_mod  # noqa: E402


class _FakeOutput(out_mod.Output):
    def __init__(self, copied):
        self.copied = copied
        self.copy_calls = 0

    def _copy_selection(self, timeout):
        self.copy_calls += 1
        return self.copied


def test_terminal_never_gets_ctrl_c():
    """終端機的 Ctrl+C 會中斷程式，指令模式絕對不能送。"""
    o = _FakeOutput("不該被讀到")
    for exe in ("WindowsTerminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe"):
        assert o.copy_selection(exe) == ""
    assert o.copy_calls == 0


def test_vscode_empty_selection_line_copy_ignored():
    """VS Code 沒選取文字時 Ctrl+C 會複製整行，要當成沒有選取。"""
    assert _FakeOutput("const a = 1;\n").copy_selection("Code.exe") == ""
    assert _FakeOutput("const a = 1;").copy_selection("Code.exe") == "const a = 1;"
    assert _FakeOutput("第一行\n第二行\n").copy_selection("Code.exe") == "第一行\n第二行\n"
    assert _FakeOutput("記事本裡選的一行\n").copy_selection("notepad.exe") == "記事本裡選的一行\n"
