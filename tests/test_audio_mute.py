"""錄音時自動靜音：用假的音訊工作階段測試，不會真的動到電腦的聲音。"""

import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from voice_input.audio_mute import RecordingMuter  # noqa: E402


class FakeSession:
    def __init__(self, exe, pid, muted=False, volume=1.0, is_system=False, key=None):
        self.exe, self.pid, self.muted, self.volume, self.is_system = exe, pid, muted, volume, is_system
        self.key = key or f"{exe}|{pid}"

    def get_mute(self):
        return self.muted

    def set_mute(self, on):
        self.muted = on

    def get_volume(self):
        return self.volume

    def set_volume(self, v):
        self.volume = v


def _setup(**cfg):
    sessions = [
        FakeSession("chrome.exe", 100),
        FakeSession("Spotify.exe", 200, volume=0.8),
        FakeSession("LINE.exe", 300, muted=True),            # 本來就靜音
        FakeSession("", 0, is_system=True),                   # Windows 系統音效
        FakeSession("python.exe", os.getpid()),               # 語音輸入自己
        FakeSession("Zoom.exe", 400),                         # 開會軟體
    ]
    path = os.path.join(tempfile.mkdtemp(), "audio_muted.json")
    m = RecordingMuter({"enabled": True, "skip_apps": ["Zoom.exe"], **cfg}, state_path=path,
                       list_sessions=lambda: sessions, start=False)
    return m, {s.exe or "system": s for s in sessions}, path


def test_mute_only_other_apps_and_restore():
    m, s, path = _setup()
    m._do_mute()
    assert s["chrome.exe"].muted and s["Spotify.exe"].muted
    assert not s["system"].muted and not s["python.exe"].muted and not s["Zoom.exe"].muted
    assert os.path.exists(path), "動手前要先記下來，當掉才還原得回來"
    m._do_restore()
    assert not s["chrome.exe"].muted and not s["Spotify.exe"].muted
    assert s["LINE.exe"].muted, "使用者自己靜音的不能被我們打開"
    assert not os.path.exists(path)


def test_lower_mode_restores_original_volume():
    m, s, _ = _setup(mode="lower", lower_to_percent=20)
    m._do_mute()
    assert abs(s["Spotify.exe"].volume - 0.16) < 1e-9 and not s["Spotify.exe"].muted
    m._do_restore()
    assert s["Spotify.exe"].volume == 0.8 and s["chrome.exe"].volume == 1.0


def test_mute_twice_restores_correctly():
    m, s, _ = _setup()
    m._do_mute()
    m._do_mute()          # 已經關了，不會把「靜音中」當成原本的狀態記下來
    m._do_restore()
    assert not s["chrome.exe"].muted


def test_leftover_after_crash_is_restored_on_start():
    m, s, path = _setup()
    m._do_mute()          # 錄音中當掉：沒有呼叫 restore
    assert s["chrome.exe"].muted
    sessions = list(s.values())
    m2 = RecordingMuter({}, state_path=path, list_sessions=lambda: sessions, start=False)
    m2._restore_leftover()
    assert not s["chrome.exe"].muted and not s["Spotify.exe"].muted
    assert s["LINE.exe"].muted
    assert not os.path.exists(path)


def test_leftover_lower_mode_uses_saved_volume():
    m, s, path = _setup(mode="lower")
    m._do_mute()
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["mode"] == "lower"
    sessions = list(s.values())
    RecordingMuter({}, state_path=path, list_sessions=lambda: sessions, start=False)._restore_leftover()
    assert s["Spotify.exe"].volume == 0.8


def test_disabled_does_not_queue_mute_but_restore_still_works():
    m, s, _ = _setup(enabled=False)
    m.mute()
    assert m._queue.empty()
    m.restore()
    assert not m._queue.empty()


def test_nothing_to_mute_leaves_no_state_file():
    path = os.path.join(tempfile.mkdtemp(), "audio_muted.json")
    m = RecordingMuter({}, state_path=path, list_sessions=lambda: [FakeSession("", 0, is_system=True)], start=False)
    m._do_mute()
    assert not os.path.exists(path)


def test_background_thread_runs_queue():
    m, s, _ = _setup()
    m.available = True
    import threading
    threading.Thread(target=m._run, daemon=True).start()
    m.mute()
    m.close(timeout=2)    # close = 還原並等它做完
    assert not s["chrome.exe"].muted
    assert m._changed == []
