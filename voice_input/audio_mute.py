"""錄音時自動把其他程式的聲音關掉（M6 ⑤），錄完馬上還原。

- 只動「各程式自己的音量」（跟 Windows 音量混合器裡每個程式的那一條一樣），不動喇叭總音量，
  也不動語音輸入自己和 Windows 系統音效，所以開始／結束的提示音照常聽得到。
- 原本就靜音的程式不會被我們打開；「調小聲」模式會還原成原本的音量。
- 動手之前先把「改了哪些程式」記在 logs\\audio_muted.json，萬一程式當掉，下次啟動會先還原。
- 開會軟體（Teams、Zoom…）預設不動，避免開會時聽不到對方。
- 所有動作都在自己的執行緒裡做（Windows 音訊 API 是 COM，要在同一條執行緒用），熱鍵那邊只丟指令、馬上返回。
"""

import json
import logging
import os
import queue
import sys
import threading

from .paths import LOG_DIR

log = logging.getLogger(__name__)

STATE_PATH = os.path.join(LOG_DIR, "audio_muted.json")


class _Session:
    """包一層 pycaw 的音訊工作階段，只留我們要用的。"""

    def __init__(self, s):
        self._vol = s.SimpleAudioVolume
        self.pid = s.ProcessId
        self.key = s.InstanceIdentifier
        try:
            self.exe = s.Process.name() if s.Process is not None else ""
        except Exception:
            self.exe = ""
        try:
            self.is_system = s._ctl.IsSystemSoundsSession() == 0   # S_OK 代表「是」系統音效
        except Exception:
            self.is_system = self.pid == 0

    def get_mute(self):
        return bool(self._vol.GetMute())

    def set_mute(self, on):
        self._vol.SetMute(1 if on else 0, None)

    def get_volume(self):
        return float(self._vol.GetMasterVolume())

    def set_volume(self, value):
        self._vol.SetMasterVolume(max(0.0, min(1.0, float(value))), None)


def _pycaw_sessions():
    from pycaw.pycaw import AudioUtilities
    return [_Session(s) for s in AudioUtilities.GetAllSessions()]


def _com_init():
    sys.coinit_flags = 0   # 多執行緒模式（COINIT_MULTITHREADED）
    import comtypes
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass               # 這條執行緒已經初始化過了


class RecordingMuter:
    def __init__(self, cfg, state_path=STATE_PATH, list_sessions=None, start=True):
        self.enabled = bool(cfg.get("enabled", True))
        self.mode = "lower" if str(cfg.get("mode", "mute")).lower() == "lower" else "mute"
        self.lower_to = max(0.0, min(1.0, float(cfg.get("lower_to_percent", 20)) / 100))
        self.skip_apps = {str(a).lower() for a in (cfg.get("skip_apps") or [])}
        self.state_path = state_path
        self._list_sessions = list_sessions
        self._queue = queue.Queue()
        self._changed = []          # [(工作階段, 原本的狀態)]
        self.available = True
        if start:
            threading.Thread(target=self._run, name="audio-mute", daemon=True).start()

    # ── 對外（都馬上返回）──

    def mute(self):
        """開始錄音時呼叫。"""
        if self.enabled and self.available:
            self._queue.put(("mute", None))

    def restore(self):
        """結束／取消錄音時呼叫。關掉這個功能時也要呼叫，才能還原已經關掉的聲音。"""
        if self.available:
            self._queue.put(("restore", None))

    def close(self, timeout=1.5):
        """程式結束前：還原，最多等 timeout 秒。"""
        if not self.available:
            return
        done = threading.Event()
        self._queue.put(("restore", done))
        done.wait(timeout)

    # ── 背景執行緒 ──

    def _run(self):
        try:
            if self._list_sessions is None:
                _com_init()
                self._list_sessions = _pycaw_sessions
            self._restore_leftover()
        except Exception as e:
            self.available = False
            log.warning("無法控制其他程式的音量，錄音時不會自動靜音：%s", e)
            return
        while True:
            action, done = self._queue.get()
            try:
                if action == "mute":
                    self._do_mute()
                else:
                    self._do_restore()
            except Exception:
                log.exception("錄音時自動靜音發生錯誤（不影響語音輸入）")
            finally:
                if done is not None:
                    done.set()

    def _do_mute(self):
        if self._changed:
            return                  # 已經關了
        own = os.getpid()
        plan = []
        for s in self._list_sessions():
            if s.pid in (0, own) or s.is_system or (s.exe or "").lower() in self.skip_apps:
                continue
            try:
                state = {"key": s.key, "exe": s.exe, "mute": s.get_mute(), "volume": s.get_volume()}
            except Exception:
                continue            # 剛好關掉的程式
            if self.mode == "mute" and state["mute"]:
                continue            # 本來就靜音
            plan.append((s, state))
        if not plan:
            return
        # 先記下來再動手：萬一中途當掉，下次啟動還原得回來
        self._save_state([state for _, state in plan])
        for s, state in plan:
            try:
                if self.mode == "mute":
                    s.set_mute(True)
                else:
                    s.set_volume(state["volume"] * self.lower_to)
                self._changed.append((s, state))
            except Exception:
                pass
        if not self._changed:
            self._clear_state()
            return
        names = sorted({state["exe"] or "?" for _, state in self._changed})
        log.info("錄音中%s：%s", "靜音" if self.mode == "mute" else "調小聲", "、".join(names))

    def _do_restore(self):
        if not self._changed:
            return
        for s, state in self._changed:
            try:
                self._restore_one(s, state, self.mode)
            except Exception:
                pass                # 程式已經關掉了
        self._changed = []
        self._clear_state()

    @staticmethod
    def _restore_one(s, state, mode):
        if mode == "mute":
            if s.get_mute():
                s.set_mute(False)
        else:
            s.set_volume(state["volume"])

    # ── 當掉後的保險 ──

    def _save_state(self, states):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"mode": self.mode, "sessions": states}, f, ensure_ascii=False)
        except OSError as e:
            log.warning("記錄靜音狀態失敗：%s", e)

    def _clear_state(self):
        try:
            os.remove(self.state_path)
        except OSError:
            pass

    def _restore_leftover(self):
        """上次程式結束（或當掉）時還有程式被我們關掉聲音：先還原。"""
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        mode = "lower" if data.get("mode") == "lower" else "mute"
        saved = {s.get("key"): s for s in data.get("sessions", []) if isinstance(s, dict)}
        restored = 0
        for s in self._list_sessions():
            state = saved.get(s.key)
            if state is None:
                continue
            try:
                self._restore_one(s, state, mode)
                restored += 1
            except Exception:
                pass
        self._clear_state()
        if restored:
            log.info("上次結束時還有 %d 個程式被關掉聲音，已經還原", restored)
