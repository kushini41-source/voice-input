"""語音輸入：按住熱鍵說話，放開後自動辨識、轉成台灣繁體中文，直接打在游標位置。

執行方式（不會出現黑色視窗）：雙擊「启动语音输入.vbs」
測試時想看即時訊息：python main.py

處理流程：熱鍵 → 錄音 → whisper 辨識（提示詞庫裡的專有名詞）
        → 快捷片語整句命中就直接輸出 → 詞庫替換（完全＋拼音相近）→ 規則
        → AI 校正（依程式場景＋前幾句＋詞庫，逾時或出錯就略過）→ OpenCC → 貼上
"""

import ctypes
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import wave

import numpy as np

from voice_input import winapi
from voice_input.paths import BASE_DIR, CONFIG_PATH, DICTIONARY_PATH, HISTORY_DIR, LOG_DIR

APP_TITLE = "語音輸入"
# 舊版（voice_hotkey_tray.py）用這個 port 當「只能開一份」的鎖；新版也佔住它，新舊版就不會同時跑
_LEGACY_LOCK_PORT = 47623
_MUTEX_NAME = "Local\\VoiceInput_shini_main"

log = logging.getLogger("main")


# ───────────────────────── 啟動前檢查 ─────────────────────────

def _ensure_single_instance():
    handle = winapi.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", _LEGACY_LOCK_PORT))
    except OSError:
        return None
    return handle, lock


def _use_windows_certificates():
    """有些網路環境（公司 proxy、防毒軟體）的憑證只在 Windows 憑證庫裡，
    Python 預設看不到，會讓下載模型、連 API 失敗。改用 Windows 的憑證庫。"""
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass


# ───────────────────────── 主程式 ─────────────────────────

class Job:
    def __init__(self, audio, record_seconds, app_exe, app_title, released_at):
        self.audio = audio
        self.record_seconds = record_seconds
        self.app_exe = app_exe
        self.app_title = app_title
        self.released_at = released_at
        self.cancelled = False


class App:
    def __init__(self, cfg):
        from voice_input.audio_mute import RecordingMuter
        from voice_input.context import History, SceneDetector
        from voice_input.converter import Converter
        from voice_input.corrector import Corrector
        from voice_input.commander import Commander
        from voice_input.dictionary import Dictionary
        from voice_input.field_watch import EditWatcher
        from voice_input.learner import Learner
        from voice_input.output import Output
        from voice_input.recorder import Recorder
        from voice_input.hotkey import HotkeyPoller
        from voice_input.recent import RecentRecords
        from voice_input.style import STYLE_PATH, StyleLearner
        from voice_input.translator import Translator
        from voice_input.ui.tkui import UIThread
        from voice_input.ui.tray import Tray
        from voice_input import autostart
        from voice_input.asr import ENGINES
        from voice_input.corrector import BACKENDS

        self.cfg = cfg
        self.recorder = Recorder(cfg["audio"])
        # 錄音時把其他程式的聲音關掉，錄完還原（上次當掉沒還原的，啟動時會先還原）
        self.muter = RecordingMuter(cfg["mute"])
        self.converter = Converter(cfg["converter"])
        self.output = Output(cfg["output"])
        self.scenes = SceneDetector(cfg["scenes"])
        self.history = History(cfg["corrector"]["history_items"], cfg["corrector"]["history_seconds"])
        self.corrector = Corrector(cfg["corrector"])
        # 指令模式跟 AI 校正共用同一個 AI（切換「AI 校正來源」時一起換）
        self.commander = Commander(cfg["command"], lambda: self.corrector.backend)
        self.translator = Translator(cfg["translate"], lambda: self.corrector.backend)
        # 個人化風格：從紀錄整理用字習慣（style.yaml），AI 校正和指令模式照著做
        self.style = StyleLearner(cfg["style"], lambda: self.corrector.backend,
                                  notify=lambda title, msg: self.tray.notify(title, msg))
        self.recent = RecentRecords()
        # 詞庫自動學習：AI 常改的詞、使用者自己改的詞，都問要不要加進詞庫
        self.learner = Learner(cfg["learning"])
        self.edit_watcher = (EditWatcher(cfg["learning"], on_edit=self._on_user_edit)
                             if cfg["learning"]["watch_edits"] else None)
        self.engine = None
        self.engine_ready = False
        self._switching_engine = False
        self._autostart = autostart

        self._jobs = queue.Queue()
        self._current_job = None
        self._recording = False
        self._record_start = 0.0
        self._record_app = ("", "")
        self._lock = threading.Lock()
        self._last_not_ready_notice = 0.0
        self._ai_warned = {}       # AI 帳號問題的通知：哪一類、上次通知的時間
        self._session = 0          # 每次按下熱鍵加 1，用來判斷浮動視窗的延遲顯示是否還有效

        # 浮動視窗和對話框（tkinter）在自己的執行緒裡跑
        self.ui = UIThread()
        self.overlay = None
        if cfg["ui"]["overlay"]:
            self.ui.call(self._create_overlay)

        self.hotkey = HotkeyPoller(
            cfg["hotkey"],
            on_start=self._on_start,
            on_stop=self._on_stop,
            on_abort=self._on_abort,
            on_escape=self._on_escape,
            is_busy=self._is_busy,
            on_mode=self._on_mode,
        )
        self.tray = Tray(
            hotkey_label=self.hotkey.key_name,
            on_quit=self.quit,
            files_to_open=[
                ("開啟詞庫（dictionary.yaml）", DICTIONARY_PATH),
                ("開啟用字習慣（style.yaml）", STYLE_PATH),
                ("開啟設定檔（config.yaml）", CONFIG_PATH),
                ("開啟日誌資料夾", LOG_DIR),
                ("開啟程式資料夾", BASE_DIR),
            ],
            toggles=[
                ("AI 校正", lambda: self.corrector.enabled, self._toggle_ai),
                ("詞庫自動學習", lambda: self.learner.enabled, self._toggle_learning),
                ("個人化風格", lambda: self.style.enabled, self._toggle_style),
                ("錄音時自動靜音", lambda: self.muter.enabled, self._toggle_mute),
                ("開機自動啟動", autostart.is_enabled, self._toggle_autostart),
            ],
            radios=[
                ("辨識引擎", list(ENGINES.items()), lambda: self.engine_name, self._switch_engine),
                ("AI 校正來源", list(BACKENDS.items()), lambda: self.corrector.backend_name, self._switch_ai_backend),
                ("翻譯成", [(lang, lang) for lang in self.translator.languages],
                 lambda: self.translator.target, self._set_translate_target),
            ],
            actions=[
                ("把上一句加入詞庫…", self._open_add_word),
                ("查看最近 20 筆紀錄", self._open_history),
                ("立即整理我的用字習慣", self._update_style_now),
                ("復原最近自動學的詞", self._undo_auto_word),
            ],
        )
        # 詞庫格式錯誤時要跳通知，所以在系統匣之後建立
        self.dictionary = Dictionary(cfg["dictionary"], notify=lambda t, m: self.tray.notify(t, m))

    # ── 啟動 ──

    def run(self):
        self.tray.run(setup=self._after_tray_ready)

    def _after_tray_ready(self):
        threading.Thread(target=self._load_engine, name="asr-load", daemon=True).start()
        threading.Thread(target=self._open_mic_later, name="mic-open", daemon=True).start()
        threading.Thread(target=self._worker, name="worker", daemon=True).start()
        self.hotkey.start()
        self.dictionary.start_watching()

    def _load_engine(self):
        try:
            self._activate_engine(self.cfg["asr"]["engine"])
            self.dictionary.warmup()
            if self.corrector.backend is not None and hasattr(self.corrector.backend, "warmup"):
                try:
                    self.corrector.backend.warmup()   # 本機 AI 先把模型載入顯示卡
                except Exception as e:
                    log.warning("本機 AI 暖機失敗：%s", e)
            self.engine_ready = True
            self._refresh_tray()
            log.info("準備完成，%s", self._idle_hint())
            if self.corrector.enabled and self.corrector.backend is None:
                self.tray.notify(
                    "語音輸入：AI 校正沒有啟用",
                    f"可以照常語音輸入，只是不會有 AI 校正。原因：{self.corrector.init_error}",
                )
        except Exception as e:
            log.exception("辨識模型載入失敗")
            self.tray.set_state("error", "辨識模型載入失敗")
            self.tray.notify(
                "語音輸入：無法啟動辨識",
                f"辨識模型載入失敗，暫時不能使用。常見原因是第一次使用時網路不通、模型沒下載成功。\n原因：{e}",
            )

    def _activate_engine(self, name):
        """載入指定的辨識引擎；載入成功才換掉舊的（換的過程中舊引擎照常可用）。
        非 whisper 的引擎失敗時，依設定自動改用 whisper。"""
        from voice_input.asr import ENGINES, create_engine
        try:
            new = create_engine(self.cfg["asr"], notify=self.tray.notify, name=name, reuse=self.engine)
            new.load()
        except Exception as e:
            if name == "whisper" or not self.cfg["asr"]["fallback_to_whisper"]:
                raise
            log.exception("辨識引擎 %s 啟動失敗，改用 whisper", name)
            self.tray.notify(
                "語音輸入：改用 whisper",
                f"{ENGINES.get(name, name)} 沒有啟動成功，已自動改用 whisper，可以照常使用。\n原因：{e}",
            )
            if self.engine is not None and self.engine.name == "whisper":
                return
            new = create_engine(self.cfg["asr"], notify=self.tray.notify, name="whisper")
            new.load()
        old, self.engine = self.engine, new
        if old is not None and old is not new:
            if getattr(new, "fallback", None) is old:
                pass                                # 舊的本機 whisper 留著，給雲端引擎當備用
            elif getattr(old, "fallback", None) is new:
                old.close(keep_fallback=True)       # 從雲端切回本機：只關雲端，本機模型繼續用
            else:
                old.close()
        log.info("目前的辨識引擎：%s", new.describe())

    def _switch_ai_backend(self, name):
        if name == self.corrector.backend_name and self.corrector.backend is not None:
            return

        def _work():
            from voice_input.corrector import BACKENDS
            self.tray.notify("語音輸入", f"正在切換 AI 校正到 {BACKENDS.get(name, name)}…")
            try:
                self.corrector.switch_backend(name)
                self.tray.notify("語音輸入", f"AI 校正已改用 {BACKENDS.get(name, name)}。")
            except Exception as e:
                log.exception("切換 AI 校正來源失敗")
                self.tray.notify("語音輸入：切換失敗", f"沒有切換成功，繼續用原本的 AI。\n原因：{e}")
            finally:
                self.tray.update_menu()

        threading.Thread(target=_work, name="ai-switch", daemon=True).start()

    @property
    def engine_name(self):
        return self.engine.name if self.engine is not None else self.cfg["asr"]["engine"]

    def _switch_engine(self, name):
        if name == self.engine_name or self._switching_engine:
            return
        self._switching_engine = True

        def _work():
            from voice_input.asr import ENGINES
            self.tray.notify("語音輸入", f"正在切換到 {ENGINES.get(name, name)}，切換好之前會繼續用目前的引擎。")
            try:
                self._activate_engine(name)
                if self.engine_name == name:
                    self.tray.notify("語音輸入", f"已切換到 {ENGINES.get(name, name)}。")
            except Exception as e:
                log.exception("切換辨識引擎失敗")
                self.tray.notify("語音輸入：切換失敗", f"沒有切換成功，繼續用原本的引擎。\n原因：{e}")
            finally:
                self._switching_engine = False
                self.tray.update_menu()

        threading.Thread(target=_work, name="engine-switch", daemon=True).start()

    def _open_mic_later(self):
        time.sleep(max(0.0, float(self.cfg["audio"]["startup_delay_seconds"])))
        try:
            self.recorder.open()
        except Exception as e:
            log.exception("開啟麥克風失敗")
            self.tray.notify(
                "語音輸入：找不到麥克風",
                "麥克風開啟失敗。請確認麥克風有插好，以及 Windows「設定 → 隱私權 → 麥克風」有允許應用程式使用。"
                f"\n原因：{e}",
            )

    def _toggle_ai(self):
        self.corrector.enabled = not self.corrector.enabled
        state = "開啟" if self.corrector.enabled else "關閉（純本機模式）"
        log.info("使用者從選單把 AI 校正切換為：%s", state)
        if self.corrector.enabled and self.corrector.backend is None:
            self.tray.notify("語音輸入：AI 校正無法使用", f"AI 校正已開啟，但無法連線：{self.corrector.init_error}")

    def _set_translate_target(self, lang):
        self.translator.target = lang
        log.info("使用者從選單把翻譯目標改成：%s", lang)
        self.tray.update_menu()

    def _toggle_mute(self):
        self.muter.enabled = not self.muter.enabled
        log.info("使用者從選單把錄音時自動靜音切換為：%s", "開啟" if self.muter.enabled else "關閉")
        if not self.muter.enabled:
            self.muter.restore()   # 正在錄音時關掉：馬上把聲音還回去

    def _toggle_style(self):
        self.style.enabled = not self.style.enabled
        log.info("使用者從選單把個人化風格切換為：%s", "開啟" if self.style.enabled else "關閉")

    def _update_style_now(self):
        def _work():
            self.tray.notify("語音輸入", "正在整理你的用字習慣，大約要 10 ~ 20 秒…")
            ok, message, changed = self.style.update_now(manual=True)
            if not ok:
                self.tray.notify("語音輸入：沒有整理成功", message)
            elif not changed:   # 有變的話 update_now 自己會跳通知
                self.tray.notify("語音輸入：用字習慣整理好了", f"{message}，跟原本一樣。")

        threading.Thread(target=_work, name="style-manual", daemon=True).start()

    def _toggle_learning(self):
        self.learner.enabled = not self.learner.enabled
        log.info("使用者從選單把詞庫自動學習切換為：%s", "開啟" if self.learner.enabled else "關閉")

    def _toggle_autostart(self):
        try:
            on = self._autostart.toggle()
        except Exception as e:
            log.exception("切換開機自動啟動失敗")
            self.tray.notify("語音輸入：設定失敗", f"開機自動啟動沒有設定成功。\n原因：{e}")
            return
        self.tray.notify("語音輸入", "已設定為開機自動啟動。" if on else "已取消開機自動啟動。")

    def _open_add_word(self):
        from voice_input.ui.add_word_dialog import open_add_word_dialog
        record = self.recent.last()
        self.ui.call(lambda: open_add_word_dialog(self.ui, record, on_saved=self._on_word_saved))

    def _open_history(self):
        from voice_input.ui.history_window import open_history_window
        records = self.recent.all()
        self.ui.call(lambda: open_history_window(self.ui, records, on_saved=self._on_word_saved))

    def _on_word_saved(self, wrong, right):
        # 不等每秒一次的檢查，馬上重新載入詞庫
        threading.Thread(target=self.dictionary.check_for_changes, daemon=True).start()

    # ── 詞庫自動學習 ──

    def _learn(self, job, after_dict, final, words, used_ai):
        """打出一句之後：開始觀察使用者有沒有自己改字（詞庫學習和個人化風格都會用到）；記下 AI 這次改了哪些詞。"""
        try:
            if self.edit_watcher is not None and (self.learner.enabled or self.style.enabled):
                self.edit_watcher.watch(final, job.app_exe)
            if used_ai and self.learner.enabled:
                before = self.converter.convert(after_dict, keep=words.protected_terms)
                for suggestion in self.learner.observe_ai(before, final, words):
                    self._suggest_word(suggestion)
        except Exception:
            # 字已經打出去了，學習失敗不能讓使用者以為這句沒成功
            log.exception("詞庫自動學習發生錯誤（不影響語音輸入）")

    def _on_user_edit(self, typed, edited, app_exe):
        """（觀察輸入框的執行緒呼叫）使用者把打出來的字改掉了。"""
        self.style.record_edit(app_exe, typed, edited)
        for suggestion in self.learner.observe_edit(typed, edited, self.dictionary.current()):
            self._suggest_word(suggestion)

    def _suggest_word(self, suggestion):
        from voice_input.ui.learn_card import suggest
        if suggestion.auto:
            # 有把握的直接加進詞庫，卡片只告訴你一聲，按「復原」可以拿掉
            if not self._auto_learn(suggestion):
                return
            self.ui.call(lambda: suggest(self.ui, suggestion, self._accept_word, self._modify_word,
                                         self._undo_auto_word))
            return
        self.ui.call(lambda: suggest(self.ui, suggestion, self._accept_word, self._modify_word, self._decline_word))

    def _auto_learn(self, s):
        """自動把「聽錯 → 改對」加進詞庫（標註是自動學的），成功回傳 True。"""
        from voice_input.dictionary import add_to_dictionary
        from voice_input.learner import is_term
        words = self.dictionary.current()
        add_vocab = is_term(s.right) and s.right not in words.vocabulary
        try:
            added = add_to_dictionary(DICTIONARY_PATH, replacements=[(s.wrong, s.right)],
                                      vocabulary=[s.right] if add_vocab else [],
                                      comment=f"自動學的 {time.strftime('%Y-%m-%d')}")
        except Exception:
            log.exception("自動加入詞庫失敗")
            return False
        if not added:
            return False
        self.learner.mark_auto(s.wrong, s.right, add_vocab)
        self._on_word_saved(s.wrong, s.right)
        log.info("詞庫自動學會：「%s」→「%s」（%s）", s.wrong, s.right, "使用者改的" if s.source == "edit" else "AI 改的")
        return True

    def _undo_auto_word(self, s=None):
        """復原自動學的詞（卡片上的「復原」、或系統匣「復原最近自動學的詞」）。沒給就復原最近一筆。"""
        from voice_input.dictionary import remove_from_dictionary
        if s is None:
            last = self.learner.last_auto()
            if last is None:
                self.tray.notify("語音輸入", "目前沒有自動學的詞可以復原。")
                return
            wrong, right = last["wrong"], last["right"]
        else:
            wrong, right = s.wrong, s.right
        record = self.learner.undo_auto(wrong, right)
        try:
            remove_from_dictionary(DICTIONARY_PATH, wrong,
                                   vocabulary_term=right if record and record.get("vocabulary") else None)
        except Exception as e:
            log.exception("復原自動學的詞失敗")
            self.tray.notify("語音輸入：沒有復原成功", f"請打開詞庫手動刪掉「{wrong}」那一行。\n原因：{e}")
            return
        self._on_word_saved(wrong, right)
        log.info("使用者復原了自動學的詞：「%s」→「%s」", wrong, right)
        self.tray.notify("語音輸入", f"已復原：「{wrong} → {right}」從詞庫拿掉了，以後也不會再自動學。")

    def _accept_word(self, s):
        """（介面執行緒）卡片上按了「加入詞庫」。"""
        from voice_input.dictionary import add_to_dictionary
        from voice_input.learner import is_term
        try:
            add_to_dictionary(DICTIONARY_PATH, replacements=[(s.wrong, s.right)],
                              vocabulary=[s.right] if is_term(s.right) else [])
        except Exception as e:
            log.exception("加入詞庫失敗")
            self.tray.notify("語音輸入：沒有加入詞庫", f"「{s.wrong} → {s.right}」沒有加成功。\n原因：{e}")
            return
        self.learner.forget(s.wrong, s.right)
        self._on_word_saved(s.wrong, s.right)
        self._ov_idle("show_result", "✓ 已加入詞庫")

    def _modify_word(self, s):
        """（介面執行緒）卡片上按了「修改…」：打開加入詞庫的對話框，先填好。"""
        from voice_input.learner import is_term
        from voice_input.ui.add_word_dialog import open_add_word_dialog
        labels = (("AI 改之前：", "AI 改之後：") if s.source == "ai"
                  else ("語音輸入打出來的：", "你改成的："))

        def saved(wrong, right):
            self.learner.forget(s.wrong, s.right)
            self._on_word_saved(wrong, right)

        open_add_word_dialog(self.ui, {"raw": s.before, "final": s.after}, on_saved=saved,
                             wrong=s.wrong, right=s.right, labels=labels, add_vocabulary=is_term(s.right))

    def _decline_word(self, s):
        self.learner.decline(s.wrong, s.right)

    # ── 浮動視窗 ──

    def _create_overlay(self):
        from voice_input.ui.overlay import Overlay
        try:
            self.overlay = Overlay(self.ui, level_fn=lambda: self.recorder.level)
        except Exception:
            log.exception("浮動視窗建立失敗（不影響語音輸入）")

    def _ov(self, method, *args):
        """在介面執行緒裡呼叫浮動視窗的方法；浮動視窗關閉或建立失敗時什麼都不做。"""
        def _do():
            if self.overlay is not None:
                getattr(self.overlay, method)(*args)
        self.ui.call(_do)

    def _ov_idle(self, method, *args):
        """處理流程更新浮動視窗；如果使用者已經在錄下一句，就不要蓋掉「錄音中」。"""
        if not self._recording:
            self._ov(method, *args)

    def _show_recording_if_still_held(self, session):
        # 按住超過「短按」門檻才顯示、才靜音，平常短按 CapsLock 切換大小寫時不會閃一下、聲音不會斷一下
        with self._lock:   # 跟結束錄音互斥：保證「靜音」一定排在「還原」前面
            if self._recording and self._session == session:
                self._ov("show_recording")
                self.muter.mute()        # 其他程式的聲音先關掉，錄完還原

    # ── 狀態 ──

    def _is_busy(self):
        return self._recording or self._current_job is not None or not self._jobs.empty()

    def _refresh_tray(self):
        if not self.engine_ready:
            return
        if self._recording:
            self.tray.set_state("recording", "再按一下結束" if self.hotkey.mode == "toggle" else "放開按鍵結束")
        elif self._current_job is not None or not self._jobs.empty():
            self.tray.set_state("processing", "辨識中")
        else:
            self.tray.set_state("idle", self._idle_hint())

    def _idle_hint(self):
        key = self.hotkey.display_name
        if self.hotkey.mode == "toggle":
            return f"按一下 {key} 開始說話，再按一下結束"
        return f"按住 {key} 說話"

    def _beep(self, freq, ms=80):
        if not self.cfg["audio"]["beep"]:
            return
        def _play():
            try:
                import winsound
                winsound.Beep(freq, ms)
            except Exception:
                pass
        threading.Thread(target=_play, daemon=True).start()

    # ── 熱鍵回呼（在熱鍵執行緒裡執行，必須立刻返回）──

    def _on_start(self):
        if not self.engine_ready:
            now = time.monotonic()
            if now - self._last_not_ready_notice > 10:
                self._last_not_ready_notice = now
                self.tray.notify(APP_TITLE, "辨識模型還在載入中，請稍等一下再試。")
            return
        with self._lock:
            if self._recording:
                return
            try:
                self.recorder.start()
            except Exception as e:
                log.exception("開始錄音失敗")
                self.tray.notify("語音輸入：無法錄音", f"麥克風無法使用，請確認有插好。\n原因：{e}")
                return
            self._recording = True
            self._record_start = time.monotonic()
            self._session += 1
            session = self._session
        if self.edit_watcher is not None:
            self.edit_watcher.finish()   # 上一句改完了，接下來要打新的字
        # 記下開始說話時的前景程式（M3 的「依 App 調整語氣」會用到）
        try:
            self._record_app = winapi.foreground_window_info()
            from voice_input.ui.answer_card import close_card
            self.ui.call(close_card)
        except Exception:
            self._record_app = ("", "")
        self._beep(880)
        self._refresh_tray()
        # 按住模式要等過了「短按」門檻才顯示（短按 CapsLock 切換大小寫時不會閃一下）；按一下模式馬上顯示
        delay = float(self.cfg["hotkey"]["min_hold_seconds"]) if self.hotkey.mode == "hold" else 0.0
        threading.Timer(delay, self._show_recording_if_still_held, args=(session,)).start()

    def _on_stop(self, mode="dictate"):
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            audio = self.recorder.stop()
            seconds = time.monotonic() - self._record_start
        self.muter.restore()
        self._beep(520)
        self._ov("show_processing", "辨識中…")
        exe, title = self._record_app
        job = Job(audio, seconds, exe, title, released_at=time.perf_counter())
        job.mode = mode   # dictate＝一般聽寫、command＝指令模式、translate＝翻譯模式
        self._jobs.put(job)
        self._refresh_tray()

    def _on_abort(self, reason=""):
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            self.recorder.discard()
        self.muter.restore()
        if reason in ("short", "chord"):
            log.info("短按或組合鍵，不錄音")
            self._ov("hide")
        self._refresh_tray()

    def _on_escape(self):
        if self._recording:
            self._on_abort()
            log.info("按 Esc 取消了這次錄音")
            self._ov("show_result", "已取消", "#9aa0a6")
            self._beep(300, 150)
            return
        # 取消正在處理和排隊中的每一句
        job = self._current_job
        if job is not None:
            job.cancelled = True
        while True:
            try:
                self._jobs.get_nowait().cancelled = True
            except queue.Empty:
                break
        log.info("按 Esc 中斷了正在處理的句子")
        self._ov("show_result", "已取消", "#9aa0a6")
        self._beep(300, 150)
        self._refresh_tray()

    # ── 處理流程 ──

    def _worker(self):
        while True:
            job = self._jobs.get()
            if job.cancelled:
                continue
            self._current_job = job
            self._refresh_tray()
            try:
                self._process(job)
            except Exception as e:
                log.exception("處理這一句時發生錯誤")
                self._ov("show_result", "這一句沒有成功", "#ff4d4f")
                self.tray.notify("語音輸入：這一句沒有成功", f"處理過程發生錯誤，這一句沒有打出來。\n原因：{e}")
            finally:
                self._current_job = None
                self._refresh_tray()

    def _process(self, job):
        duration = len(job.audio) / 16000
        if duration < float(self.cfg["audio"]["min_duration_seconds"]):
            log.info("錄音太短（%.2f 秒），略過", duration)
            self._ov_idle("hide")
            return

        if getattr(job, "mode", "dictate") == "command":
            self._process_command(job)
            return
        if getattr(job, "mode", "dictate") == "translate":
            self._process_translate(job)
            return

        words = self.dictionary.current()
        t0 = time.perf_counter()
        raw = self.engine.transcribe(job.audio, prompt_terms=words.prompt_terms)
        t_asr = time.perf_counter() - t0
        if not raw:
            log.info("沒有辨識出文字（錄音 %.1f 秒，辨識 %.2f 秒）", duration, t_asr)
            self._ov_idle("show_result", "沒有聽到聲音", "#9aa0a6")
            return

        t1 = time.perf_counter()
        after_dict, is_snippet, hits = self.dictionary.process(raw)
        t_dict = time.perf_counter() - t1
        if hits:
            log.info("詞庫命中：%s", "；".join(f"{a} → {b}（{s}）" for a, b, s in hits))

        scene = self.scenes.detect(job.app_exe, job.app_title)
        ai_note, t_ai = "", 0.0
        if is_snippet:
            # 快捷片語整句命中：直接輸出設定好的內容，後面的步驟全部跳過
            after_ai = final = after_dict
            ai_note = "快捷片語，略過"
        else:
            if self.corrector.enabled and not self._recording:
                self.tray.set_state("processing", "AI 校正中")
                self._ov_idle("show_processing", "整理中…")
            result = self.corrector.correct(
                after_dict, scene, self.history.recent(job.app_exe), words.protected_terms,
                is_cancelled=lambda: job.cancelled, style_rules=self.style.rules(),
            )
            after_ai, t_ai = result.text, result.seconds
            if not result.used_ai:
                ai_note = result.reason
                if self.corrector.enabled and result.reason and "Esc" not in result.reason:
                    log.warning("這句沒有用到 AI 校正：%s", result.reason)
                    self._warn_ai_account(result.reason)
            t2 = time.perf_counter()
            final = self.converter.convert(after_ai, keep=words.protected_terms)
            t_dict += time.perf_counter() - t2

        if job.cancelled:
            log.info("已取消，不輸出：%s", final)
            return

        t3 = time.perf_counter()
        method = self.output.send(final, app_exe=job.app_exe)
        t_out = time.perf_counter() - t3
        total = time.perf_counter() - job.released_at
        self._ov_idle("show_result", "✓ 已輸入")
        self.recent.add(job.app_exe, scene["name"], raw, after_dict, after_ai, final,
                        used_ai=(not is_snippet and not ai_note))

        log.info(
            "原始辨識：%s → 詞庫替換後：%s → AI 校正後：%s%s → 最終輸出：%s",
            raw, after_dict, after_ai, f"（{ai_note}）" if ai_note else "", final,
        )
        log.info(
            "耗時｜錄音 %.1f 秒｜辨識 %.2f 秒｜詞庫＋轉換 %.0f 毫秒｜AI %.2f 秒｜輸出 %.2f 秒（%s）"
            "｜放開到打出 %.2f 秒｜程式：%s｜場景：%s",
            duration, t_asr, t_dict * 1000, t_ai, t_out, method, total, job.app_exe or "未知", scene["name"],
        )

        if not is_snippet:
            self.history.add(job.app_exe, final)
            self._learn(job, after_dict, final, words, used_ai=not ai_note)
            try:
                said = self.converter.convert(after_dict, keep=words.protected_terms)
                self.style.record(job.app_exe, scene["name"], said, final)
            except Exception:
                log.exception("個人化風格記錄失敗（不影響語音輸入）")

        if self.cfg["debug"]["save_history"]:
            self._save_history(job, raw, final)

    def _warn_ai_account(self, reason):
        """金鑰失效、額度用完這類「不會自己好」的問題要跳通知（同一類 30 分鐘內只通知一次），
        不然使用者只會覺得標點和錯字突然變差，卻不知道為什麼。"""
        r = reason.lower()
        if "credit balance" in r or "billing" in r:
            title, msg = "Claude 的 API 額度用完了", "請到 Claude Console 的帳單頁儲值，或在 claude.ai 帳單設定領取 Max 方案的每月額度。"
        elif "401" in r or "x-api-key" in r or "authentication" in r:
            title, msg = "Claude 的 API 金鑰失效了", "可能是到期了。請到 Claude Console 建一把新金鑰（工作區選 Default），貼進 api_key.txt。"
        elif "workspace" in r or "403" in r or "permission" in r:
            title, msg = "Claude 的 API 金鑰設定有問題", "請到 Claude Console 重建一把金鑰，工作區選 Default，再貼進 api_key.txt。"
        else:
            return
        now = time.monotonic()
        if now - self._ai_warned.get(title, -1e9) < 1800:
            return
        self._ai_warned[title] = now
        self.tray.notify(f"語音輸入：{title}", f"這段期間打字不會有 AI 校正（標點、錯字不會修），字還是打得出來。\n{msg}")

    def _process_command(self, job):
        """指令模式：讀選取的文字 → 辨識指令 → AI 決定改寫／回答／插入 → 照做。"""
        from voice_input.ui.answer_card import show_answer_card

        # 先讀選取的文字（越早讀越好，免得使用者等的時候點到別的地方）
        t0 = time.perf_counter()
        selection = self.output.copy_selection(job.app_exe)
        t_sel = time.perf_counter() - t0

        log.info("指令模式開始｜程式：%s｜讀到選取的文字 %d 字（%.2f 秒）", job.app_exe or "未知", len(selection), t_sel)

        words = self.dictionary.current()
        raw = self.engine.transcribe(job.audio, prompt_terms=words.prompt_terms)
        t_asr = time.perf_counter() - t0 - t_sel
        if not raw:
            log.info("指令模式：沒有辨識出文字（錄音 %.1f 秒，辨識 %.2f 秒）", len(job.audio) / 16000, t_asr)
            self._ov_idle("show_result", "沒有聽到聲音", "#9aa0a6")
            return
        # 指令本身也套用詞庫替換（例如「拜加溫」→ buy+1），轉成繁體方便閱讀
        instruction, _ = words.apply_replacements(raw, self.dictionary.threshold, fuzzy=self.dictionary.fuzzy)
        instruction = self.converter.convert(words.apply_rules(instruction), keep=words.protected_terms)

        scene = self.scenes.detect(job.app_exe, job.app_title)
        self._ov_idle("show_processing", "AI 思考中…")
        if not self._recording:
            self.tray.set_state("processing", "AI 思考中")
        result = self.commander.run(instruction, selection, scene, is_cancelled=lambda: job.cancelled,
                                    style_rules=self.style.rules())
        log.info("指令模式｜選取 %d 字｜指令：%s｜AI：%s（%s，%.2f 秒）%s",
                 len(selection), instruction, result.action or "失敗",
                 getattr(self.commander, "last_model", "") or "未知模型", result.seconds,
                 f"｜{result.reason}" if result.reason else "")

        if job.cancelled:
            return
        if not result.ok:
            self._ov_idle("show_result", "指令沒有完成", "#ff4d4f")
            if "Esc" not in result.reason:
                self.tray.notify("語音輸入：指令沒有完成", result.reason)
            return

        if result.action == "answer":
            self._ov_idle("hide")
            app_exe = job.app_exe

            def insert(text):
                if self.edit_watcher is not None:
                    self.edit_watcher.finish()
                threading.Thread(target=self.output.send, args=(text, app_exe), daemon=True).start()

            self.ui.call(lambda: show_answer_card(self.ui, instruction, result.text, on_insert=insert))
        else:
            self.output.send(result.text, app_exe=job.app_exe)
            self._ov_idle("show_result", "✓ 已改寫" if result.action == "replace" else "✓ 已插入")

        total = time.perf_counter() - job.released_at
        log.info("指令模式耗時｜讀選取 %.2f 秒｜辨識 %.2f 秒｜AI %.2f 秒｜放開到完成 %.2f 秒｜結果：%s",
                 t_sel, t_asr, result.seconds, total, result.text[:200].replace("\n", " ⏎ "))
        self.recent.add(job.app_exe, f"指令模式（{result.action}）", raw, instruction, result.text, result.text,
                        used_ai=True)

    def _process_translate(self, job):
        """翻譯模式：辨識 → 詞庫替換 → AI 翻成目標語言 → 打出來。翻譯失敗就打出中文原文，不會讓話白講。"""
        from voice_input.translator import is_chinese_target
        words = self.dictionary.current()
        t0 = time.perf_counter()
        raw = self.engine.transcribe(job.audio, prompt_terms=words.prompt_terms)
        t_asr = time.perf_counter() - t0
        if not raw:
            log.info("翻譯模式：沒有辨識出文字（錄音 %.1f 秒，辨識 %.2f 秒）", len(job.audio) / 16000, t_asr)
            self._ov_idle("show_result", "沒有聽到聲音", "#9aa0a6")
            return
        after_dict, is_snippet, _ = self.dictionary.process(raw)
        original = self.converter.convert(after_dict, keep=words.protected_terms)
        target = self.translator.target
        scene = self.scenes.detect(job.app_exe, job.app_title)

        if is_snippet:
            # 快捷片語（例如信箱）照原樣打出，不翻譯
            final, result = after_dict, None
        else:
            self._ov_idle("show_processing", f"翻譯成{target}…")
            if not self._recording:
                self.tray.set_state("processing", f"翻譯成{target}")
            result = self.translator.run(after_dict, scene, words.protected_terms, is_cancelled=lambda: job.cancelled)
            if result.ok:
                final = result.text
                if is_chinese_target(target):
                    final = self.converter.convert(final, keep=words.protected_terms)
            else:
                final = original
        if job.cancelled:
            log.info("已取消，不輸出：%s", final)
            return

        method = self.output.send(final, app_exe=job.app_exe)
        total = time.perf_counter() - job.released_at
        if result is not None and not result.ok:
            self._ov_idle("show_result", "翻譯失敗，已打出中文", "#f5b041")
            if "Esc" not in result.reason:
                self.tray.notify("語音輸入：翻譯沒有完成", f"已經改打出中文原文。\n原因：{result.reason}")
        else:
            self._ov_idle("show_result", "✓ 已翻譯")
        log.info("翻譯模式｜目標：%s｜原始辨識：%s → 中文：%s → 翻譯：%s%s",
                 target, raw, original, final if result is None or result.ok else "（失敗，打出中文）",
                 f"｜{result.reason}" if result is not None and result.reason else "")
        log.info("翻譯模式耗時｜錄音 %.1f 秒｜辨識 %.2f 秒｜AI %.2f 秒｜放開到打出 %.2f 秒（%s）｜程式：%s｜場景：%s",
                 len(job.audio) / 16000, t_asr, result.seconds if result is not None else 0.0, total, method,
                 job.app_exe or "未知", scene["name"])
        self.recent.add(job.app_exe, f"翻譯模式（{target}）", raw, original, final, final,
                        used_ai=result is not None and result.ok)

    def _on_mode(self, mode):
        """錄音中按了 Shift／Ctrl 切換模式（熱鍵執行緒呼叫，要馬上返回）。"""
        label = None
        if mode == "translate":
            target = self.translator.target
            label = f"翻成{target}" if len(target) <= 3 else "翻譯模式"
        log.info("錄音中切換為：%s", {"command": "指令模式", "translate": f"翻譯模式（{self.translator.target}）",
                                      "dictate": "一般聽寫"}.get(mode, mode))
        self._ov("set_record_kind", mode, label)

    def _save_history(self, job, raw, final):
        try:
            os.makedirs(HISTORY_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            wav_path = os.path.join(HISTORY_DIR, f"{stamp}.wav")
            pcm = (np.clip(job.audio, -1, 1) * 32767).astype(np.int16)
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm.tobytes())
            with open(os.path.join(HISTORY_DIR, "history.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "time": stamp, "audio": os.path.basename(wav_path), "app": job.app_exe,
                    "raw": raw, "final": final,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("存除錯紀錄失敗：%s", e)

    # ── 結束 ──

    def quit(self):
        log.info("使用者結束程式")
        self.hotkey.stop()
        self.muter.close()      # 正在錄音時按結束：先把其他程式的聲音還回去
        self.recorder.close()
        if self.engine is not None:
            try:
                self.engine.close()   # 例如關掉由本程式啟動的 CapsWriter 伺服器
            except Exception:
                log.exception("關閉辨識引擎時發生錯誤")
        self.tray.stop()
        os._exit(0)


def _enable_dpi_awareness():
    """讓視窗在 125%、150% 縮放的螢幕上也清楚，不會糊糊的。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 每個螢幕各自的 DPI
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    from voice_input.logger import setup_logging
    setup_logging()
    _enable_dpi_awareness()

    lock = _ensure_single_instance()
    if lock is None:
        log.info("已經有一份語音輸入在執行，這次不重複啟動")
        winapi.message_box(
            "語音輸入已經在執行了（請看右下角系統匣的麥克風圖示）。\n"
            "如果找不到，可能是舊版還開著，請先從舊版的系統匣圖示按「結束」。",
            f"{APP_TITLE} - 已在執行",
        )
        return

    _use_windows_certificates()

    from voice_input.config import ConfigError, load_config
    try:
        cfg = load_config()
    except ConfigError as e:
        winapi.message_box(str(e), f"{APP_TITLE} - 設定檔有問題", 0x10)
        return

    log.info("程式啟動（資料夾：%s，Python %s）", BASE_DIR, sys.version.split()[0])
    try:
        app = App(cfg)
    except ConfigError as e:
        winapi.message_box(str(e), f"{APP_TITLE} - 設定檔有問題", 0x10)
        return
    except Exception as e:
        log.exception("程式啟動失敗")
        winapi.message_box(
            f"程式啟動時發生錯誤，這是程式本身的問題，不是你的設定造成的。\n"
            f"請把 logs 資料夾裡 voice_input.log 的最後幾行提供給開發者。\n\n原因：{e}",
            f"{APP_TITLE} - 啟動失敗", 0x10,
        )
        return
    app.run()


if __name__ == "__main__":
    main()
