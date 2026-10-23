"""系統匣選單能正常建立（之前曾因選單項目格式不對，程式一啟動就停住）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.ui.tray import Tray  # noqa: E402


def test_tray_menu_builds_with_toggle():
    state = {"on": True}
    tray = Tray(
        hotkey_label="capslock",
        on_quit=lambda: None,
        files_to_open=[("開啟詞庫", __file__)],
        toggles=[("AI 校正", lambda: state["on"], lambda: state.update(on=not state["on"]))],
    )
    items = list(tray.icon.menu.items)
    toggle = next(i for i in items if i.text == "AI 校正")
    assert toggle.checked is True
    toggle(tray.icon)
    assert state["on"] is False
    assert toggle.checked is False


def test_tray_radio_submenu():
    state = {"engine": "whisper"}
    tray = Tray(
        hotkey_label="capslock", on_quit=lambda: None, files_to_open=[],
        radios=[("辨識引擎", [("whisper", "faster-whisper"), ("capswriter", "CapsWriter")],
                 lambda: state["engine"], lambda v: state.update(engine=v))],
        actions=[("把上一句加入詞庫…", lambda: None)],
    )
    sub = next(i for i in tray.icon.menu.items if i.text == "辨識引擎").submenu
    whisper, caps = list(sub.items)
    assert whisper.checked and not caps.checked
    caps(tray.icon)
    assert state["engine"] == "capswriter"
    assert caps.checked and not whisper.checked
