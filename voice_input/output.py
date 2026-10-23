"""把文字送進目前游標所在的位置。

預設：先備份剪貼簿 → 放入文字 → 模擬 Ctrl+V → 稍等一下再把剪貼簿還原。
剪貼簿打不開、或程式被列在 type_apps 裡時，改成用 SendInput 逐字輸入。
"""

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes

from . import winapi

log = logging.getLogger(__name__)

user32 = winapi.user32
kernel32 = winapi.kernel32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = wintypes.HWND(-3)

# 這些格式存的是 GDI 物件而不是記憶體區塊，沒辦法照位元組備份（Windows 會從其他格式自動補回來）
_UNCOPYABLE_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
_MAX_FORMAT_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
PM_REMOVE = 0x0001


# 這些程式按 Ctrl+C 是「中斷正在執行的程式」，不是複製
_NO_COPY_APPS = {
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "openconsole.exe",
    "wezterm-gui.exe", "mintty.exe", "alacritty.exe", "putty.exe", "kitty.exe", "mstsc.exe",
}
# 這些編輯器沒選取文字時按 Ctrl+C 會複製整行
_LINE_COPY_APPS = {"code.exe", "cursor.exe", "windsurf.exe", "code - insiders.exe"}


class ClipboardBusy(Exception):
    pass


class _ClipboardThread:
    """所有剪貼簿操作都在這條執行緒做。

    剪貼簿的擁有者視窗必須一直處理訊息：別的程式清空剪貼簿時，Windows 會「同步」
    送訊息給擁有者，擁有者如果不回應，對方程式就會卡住。所以這條執行緒在沒事時
    也持續處理視窗訊息。
    """

    def __init__(self):
        self._tasks = queue.Queue()
        self._ready = threading.Event()
        self.hwnd = None
        threading.Thread(target=self._run, name="clipboard", daemon=True).start()
        self._ready.wait(5)

    def _run(self):
        self.hwnd = user32.CreateWindowExW(0, "STATIC", "voice_input_clipboard", 0, 0, 0, 0, 0,
                                           HWND_MESSAGE, None, None, None)
        self._ready.set()
        msg = wintypes.MSG()
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                fn, holder, done = self._tasks.get(timeout=0.02)
            except queue.Empty:
                continue
            try:
                holder["result"] = fn()
            except BaseException as e:
                holder["error"] = e
            done.set()

    def call(self, fn, timeout=5.0):
        holder, done = {}, threading.Event()
        self._tasks.put((fn, holder, done))
        if not done.wait(timeout):
            raise ClipboardBusy("剪貼簿操作逾時")
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")


class _Clipboard:
    """剪貼簿操作（實際動作都交給 _ClipboardThread 執行）。"""

    def __init__(self):
        self._worker = _ClipboardThread()
        self._hwnd = self._worker.hwnd
        # 告訴 Windows「剪貼簿紀錄（Win+V）」和雲端同步不要收錄我們暫放的文字
        self._fmt_exclude = user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
        self._fmt_no_history = user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
        self._fmt_no_cloud = user32.RegisterClipboardFormatW("CanUploadToCloudClipboard")

    def _open(self):
        for _ in range(25):
            if user32.OpenClipboard(self._hwnd):
                return
            time.sleep(0.02)
        raise ClipboardBusy("剪貼簿被其他程式占用中")

    @staticmethod
    def _bytes_to_hglobal(data):
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
        if not handle:
            raise MemoryError("GlobalAlloc 失敗")
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        return handle

    def _set(self, fmt, data):
        handle = self._bytes_to_hglobal(data)
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)
            return False
        return True

    def backup(self):
        return self._worker.call(self._backup)

    def set_text(self, text):
        return self._worker.call(lambda: self._set_text(text))

    def restore(self, saved):
        return self._worker.call(lambda: self._restore(saved))

    def _backup(self):
        """把剪貼簿裡能備份的格式全部複製一份：[(格式, bytes), ...]。"""
        self._open()
        saved, total = [], 0
        try:
            fmt = user32.EnumClipboardFormats(0)
            while fmt:
                if fmt not in _UNCOPYABLE_FORMATS and not (0x300 <= fmt <= 0x3FF):
                    handle = user32.GetClipboardData(fmt)
                    if handle:
                        size = kernel32.GlobalSize(handle)
                        if 0 < size <= _MAX_FORMAT_BYTES and total + size <= _MAX_TOTAL_BYTES:
                            ptr = kernel32.GlobalLock(handle)
                            if ptr:
                                try:
                                    saved.append((fmt, ctypes.string_at(ptr, size)))
                                    total += size
                                finally:
                                    kernel32.GlobalUnlock(handle)
                fmt = user32.EnumClipboardFormats(fmt)
        finally:
            user32.CloseClipboard()
        return saved

    def _set_text(self, text):
        self._open()
        try:
            user32.EmptyClipboard()
            ok = self._set(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
            zero = (0).to_bytes(4, "little")
            self._set(self._fmt_exclude, b"\0")
            self._set(self._fmt_no_history, zero)
            self._set(self._fmt_no_cloud, zero)
        finally:
            user32.CloseClipboard()
        if not ok:
            raise ClipboardBusy("無法把文字放進剪貼簿")
        return user32.GetClipboardSequenceNumber()

    def _restore(self, saved):
        self._open()
        try:
            user32.EmptyClipboard()
            for fmt, data in saved:
                self._set(fmt, data)
        finally:
            user32.CloseClipboard()

    def clear(self):
        def _do():
            self._open()
            try:
                user32.EmptyClipboard()
            finally:
                user32.CloseClipboard()
            return user32.GetClipboardSequenceNumber()
        return self._worker.call(_do)

    def get_text(self):
        """讀剪貼簿裡的文字；沒有文字就回傳空字串。"""
        def _do():
            self._open()
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ""
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        return self._worker.call(_do)

    @staticmethod
    def sequence():
        return user32.GetClipboardSequenceNumber()


class Output:
    def __init__(self, cfg):
        self.method = cfg["method"]
        self.type_apps = {str(a).lower() for a in (cfg.get("type_apps") or [])}
        self.restore_delay = float(cfg["restore_clipboard_delay"])
        self.wait_modifiers_timeout = float(cfg["wait_modifiers_timeout"])
        self._clipboard = _Clipboard()
        self._restore_done = threading.Event()
        self._restore_done.set()

    def _wait_modifiers_released(self):
        """使用者手還壓著 Ctrl／Alt／Shift 時貼上，會變成別的快捷鍵（例如 Ctrl+Alt+V），所以先等放開。"""
        deadline = time.monotonic() + self.wait_modifiers_timeout
        while winapi.any_modifier_down():
            if time.monotonic() > deadline:
                log.warning("等不到修飾鍵放開，照樣輸出")
                return
            time.sleep(0.01)

    def send(self, text, app_exe=""):
        """輸出文字，回傳實際使用的方式（"paste" 或 "type"）。失敗會丟出例外。"""
        if not text:
            return None
        self._wait_modifiers_released()
        use_type = self.method == "type" or app_exe.lower() in self.type_apps
        if not use_type:
            try:
                self._paste(text)
                return "paste"
            except Exception as e:
                log.warning("剪貼簿貼上失敗，改用逐字輸入：%s", e)
        if not winapi.send_unicode_text(text):
            raise RuntimeError("逐字輸入失敗（目標程式可能是用系統管理員身分執行的）")
        return "type"

    def copy_selection(self, app_exe="", timeout=0.6):
        """讀取目前選取的文字（模擬 Ctrl+C），讀完把剪貼簿還原。沒有選取任何文字就回傳空字串。"""
        exe = (app_exe or "").lower()
        if exe in _NO_COPY_APPS:
            # 終端機裡的 Ctrl+C 是「中斷程式」，絕對不能送
            log.info("%s 是終端機，指令模式不讀取選取的文字", app_exe)
            return ""
        text = self._copy_selection(timeout)
        if exe in _LINE_COPY_APPS and text.endswith("\n") and text.count("\n") == 1:
            # VS Code 類的編輯器沒選取文字時，Ctrl+C 會複製「整行」，當作沒有選取
            return ""
        return text

    def _copy_selection(self, timeout):
        self._wait_modifiers_released()
        self._restore_done.wait(timeout=2.0)
        try:
            saved = self._clipboard.backup()
        except Exception as e:
            log.warning("備份剪貼簿失敗，不讀取選取的文字：%s", e)
            return ""
        try:
            # 先清空，才分得出「Ctrl+C 有複製到東西」還是「剪貼簿本來就有東西」
            seq = self._clipboard.clear()
            if not winapi.send_ctrl_c():
                return ""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._clipboard.sequence() != seq:
                    time.sleep(0.03)   # 有些程式會分幾次寫入剪貼簿
                    return self._clipboard.get_text()
                time.sleep(0.02)
            return ""
        except Exception as e:
            log.warning("讀取選取的文字失敗：%s", e)
            return ""
        finally:
            try:
                self._clipboard.restore(saved)
            except Exception as e:
                log.warning("還原剪貼簿失敗：%s", e)

    def _paste(self, text):
        # 上一句的剪貼簿還沒還原完，先等它，免得把上一句的文字當成「原本的剪貼簿」備份
        self._restore_done.wait(timeout=2.0)
        try:
            saved = self._clipboard.backup()
        except Exception as e:
            log.warning("備份剪貼簿失敗，這次貼上後不會還原：%s", e)
            saved = None
        seq = self._clipboard.set_text(text)
        if not winapi.send_ctrl_v():
            raise RuntimeError("模擬 Ctrl+V 失敗")
        if saved is None:
            return
        # 目標程式要一點時間才會讀剪貼簿，所以稍後才還原；在背景做，文字可以先出現
        self._restore_done.clear()
        threading.Thread(target=self._restore_later, args=(saved, seq), daemon=True).start()

    def _restore_later(self, saved, seq):
        try:
            time.sleep(self.restore_delay)
            # 如果這段時間剪貼簿又被別人改過（例如使用者自己複製了東西），就不要蓋掉
            if self._clipboard.sequence() != seq:
                log.info("剪貼簿在貼上後被其他程式改過，不還原")
                return
            self._clipboard.restore(saved)
        except Exception as e:
            log.warning("還原剪貼簿失敗：%s", e)
        finally:
            self._restore_done.set()
