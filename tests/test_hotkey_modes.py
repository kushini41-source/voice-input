"""熱鍵模式的測試：用模擬的按鍵狀態跑輪詢邏輯，不需要真的按鍵盤。"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input import hotkey as hk  # noqa: E402

F13 = 0x7C   # 用 F13 測試，避開 CapsLock 的大小寫還原邏輯


class FakeKeyboard:
    def __init__(self):
        self.down = set()

    def is_key_down(self, vk):
        return vk in self.down


@pytest.fixture
def run_poller(monkeypatch):
    kb = FakeKeyboard()
    monkeypatch.setattr(hk.winapi, "is_key_down", kb.is_key_down)
    monkeypatch.setattr(hk.winapi, "tap_key", lambda vk: True)
    events = []
    pollers = []

    def make(mode, **extra):
        cfg = {"key": "f13", "mode": mode, "min_hold_seconds": 0.05, "poll_interval_ms": 5, "esc_cancels": True,
               **extra}
        p = hk.HotkeyPoller(
            cfg,
            on_start=lambda: events.append("start"),
            on_stop=lambda m="dictate": events.append(f"stop:{m}"),
            on_abort=lambda reason="": events.append(f"abort:{reason}"),
            on_escape=lambda: events.append("esc"),
            is_busy=lambda: True,
            on_mode=lambda m: events.append(f"mode:{m}"),
        )
        p.start()
        pollers.append(p)
        return p

    def press(vk=F13, hold=0.1, gap=0.05):
        kb.down.add(vk)
        time.sleep(hold)
        kb.down.discard(vk)
        time.sleep(gap)

    yield make, kb, press, events
    for p in pollers:
        p.stop()


def test_toggle_press_once_start_press_again_stop(run_poller):
    make, kb, press, events = run_poller
    make("toggle")
    press()                      # 按一下：開始
    assert events == ["start"]
    time.sleep(0.2)              # 手放開、繼續講話：不會結束
    assert events == ["start"]
    press()                      # 再按一下：結束
    assert events == ["start", "stop:dictate"]


def test_toggle_short_tap_still_counts(run_poller):
    """按一下模式不需要按住，輕輕點一下也算。"""
    make, kb, press, events = run_poller
    make("toggle")
    press(hold=0.02)
    press(hold=0.02)
    assert events == ["start", "stop:dictate"]


def test_toggle_shift_during_recording_switches_to_command(run_poller):
    make, kb, press, events = run_poller
    make("toggle")
    press()
    press(vk=hk.winapi.VK_SHIFT, hold=0.05)    # 錄音中按一下 Shift
    press()
    assert events == ["start", "mode:command", "stop:command"]


def test_shift_twice_is_translate_when_ctrl_is_hotkey(run_poller):
    """熱鍵是右 Ctrl 時 Ctrl 不能當翻譯鍵：Shift 按第一下＝指令、第二下＝翻譯。"""
    make, kb, press, events = run_poller
    make("toggle", translate_modifier="shift")      # 翻譯鍵跟指令鍵一樣＝輪流切換
    press()
    press(vk=hk.winapi.VK_SHIFT, hold=0.03)
    press(vk=hk.winapi.VK_SHIFT, hold=0.03)
    press()
    assert events == ["start", "mode:command", "mode:translate", "stop:translate"]


def test_shift_three_times_back_to_dictate(run_poller):
    make, kb, press, events = run_poller
    make("toggle", translate_modifier="shift")
    press()
    for _ in range(3):
        press(vk=hk.winapi.VK_SHIFT, hold=0.03)
    press()
    assert events == ["start", "mode:command", "mode:translate", "mode:dictate", "stop:dictate"]


def test_holding_shift_counts_once(run_poller):
    make, kb, press, events = run_poller
    make("toggle", translate_modifier="shift")
    press()
    press(vk=hk.winapi.VK_SHIFT, hold=0.3)          # 按著不放：只算一下
    press()
    assert events == ["start", "mode:command", "stop:command"]


def test_next_recording_starts_as_dictate(run_poller):
    make, kb, press, events = run_poller
    make("toggle", translate_modifier="shift")
    press()
    press(vk=hk.winapi.VK_SHIFT, hold=0.03)
    press()
    press()
    press()
    assert events == ["start", "mode:command", "stop:command", "start", "stop:dictate"]


def test_ctrl_is_translate_when_hotkey_is_not_ctrl(run_poller):
    make, kb, press, events = run_poller
    make("toggle")                                  # F13 當熱鍵：Ctrl 可以直接當翻譯鍵
    press()
    press(vk=hk.winapi.VK_CONTROL, hold=0.03)
    press()
    assert events == ["start", "mode:translate", "stop:translate"]


def test_right_ctrl_hotkey_uses_shift_cycle():
    cfg = {"key": "right_ctrl", "mode": "toggle", "min_hold_seconds": 0.3, "poll_interval_ms": 15,
           "esc_cancels": True}
    p = hk.HotkeyPoller(cfg, on_start=None, on_stop=None, on_abort=None, on_escape=None, is_busy=None)
    assert p.cycle_vk == hk.winapi.VK_SHIFT
    assert p.translate_hint == "錄音中按兩下 Shift"


def test_toggle_esc_cancels(run_poller):
    make, kb, press, events = run_poller
    make("toggle")
    press()
    press(vk=hk.winapi.VK_ESCAPE)
    assert events[-1] == "esc"
    press()                      # Esc 之後再按一下是「重新開始」，不是結束
    assert events[-1] == "start"


def test_hold_mode_still_works(run_poller):
    make, kb, press, events = run_poller
    make("hold")
    press(hold=0.15)
    assert events == ["start", "stop:dictate"]
    press(hold=0.01)             # 太短：視為一般按鍵
    assert events[-1] == "abort:short"


def test_toggle_auto_stops_after_max_seconds(run_poller):
    make, kb, press, events = run_poller
    p = make("toggle")
    p.max_record = 0.3
    press()
    time.sleep(0.5)              # 忘了按結束
    assert events == ["start", "stop:dictate"]
    press()                      # 下一次按鍵是「重新開始」
    assert events[-1] == "start"


def _chord_press(kb, other, hold=0.1):
    kb.down.add(F13)
    time.sleep(0.03)
    kb.down.add(other)          # 例如右 Ctrl+C 的 C
    time.sleep(hold)
    kb.down.discard(other)
    kb.down.discard(F13)
    time.sleep(0.05)


def test_toggle_chord_is_not_recording(run_poller):
    """熱鍵跟別的鍵一起按（右 Ctrl+C）是一般快捷鍵，不能開始錄音。"""
    make, kb, press, events = run_poller
    make("toggle")
    _chord_press(kb, ord("C"))
    assert events == []
    press()                      # 之後單獨按一下，照常開始
    assert events == ["start"]


def test_toggle_with_mouse_click_is_not_recording(run_poller):
    make, kb, press, events = run_poller
    make("toggle")
    _chord_press(kb, 0x01)       # Ctrl+滑鼠左鍵
    assert events == []


def test_toggle_long_hold_is_not_a_tap(run_poller):
    make, kb, press, events = run_poller
    p = make("toggle")
    p.max_tap = 0.2
    press(hold=0.4)
    assert events == []


def test_toggle_chord_while_recording_does_not_stop(run_poller):
    make, kb, press, events = run_poller
    make("toggle")
    press()
    _chord_press(kb, ord("V"))   # 錄音中按了 Ctrl+V：不算結束
    assert events == ["start"]
    press()
    assert events == ["start", "stop:dictate"]


def test_hold_chord_aborts(run_poller):
    make, kb, press, events = run_poller
    make("hold")
    _chord_press(kb, ord("C"))
    assert events == ["start", "abort:chord"]
