"""Windows API 的 ctypes 包裝：按鍵狀態、SendInput、前景視窗。

這裡刻意不使用 keyboard 套件：它在打字時會偷偷裝上低階鍵盤 hook，
舊版按右 Alt 會讓整個鍵盤卡死，就是這個原因。
"""

import ctypes
import os
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56
# 沒有被任何功能使用的按鍵碼，用來「遮住」單獨按放 Alt 時觸發的選單列
VK_MENU_MASK = 0xE8

MODIFIER_VKS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

# 需要加上「延伸鍵」旗標的按鍵（右 Alt、右 Ctrl、方向鍵等）
_EXTENDED_VKS = {0xA3, 0xA5, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x5C}


def is_key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def any_modifier_down():
    return any(is_key_down(vk) for vk in MODIFIER_VKS)


# 判斷「組合鍵」時不算的按鍵：滑鼠按鍵、各種修飾鍵、CapsLock、我們自己補送的遮罩鍵
_NOT_CHORD_VKS = set(range(0x01, 0x07)) | {
    VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL, VK_LWIN, VK_RWIN, VK_MENU_MASK,
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
}


def other_key_down(hotkey_vk):
    """除了熱鍵和修飾鍵之外，有沒有其他鍵正被按著（例如右 Ctrl+C 的 C）。"""
    for vk in range(0x08, 0xFF):
        if vk != hotkey_vk and vk not in _NOT_CHORD_VKS and is_key_down(vk):
            return True
    return False


def capslock_on():
    return bool(user32.GetKeyState(VK_CAPITAL) & 1)


def _key_input(vk, up=False):
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=user32.MapVirtualKeyW(vk, 0), dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _unicode_input(code_unit, up=False):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def send_inputs(inputs):
    if not inputs:
        return True
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    return sent == len(inputs)


def tap_key(vk):
    return send_inputs([_key_input(vk), _key_input(vk, up=True)])


def send_ctrl_key(vk):
    return send_inputs([
        _key_input(VK_CONTROL),
        _key_input(vk),
        _key_input(vk, up=True),
        _key_input(VK_CONTROL, up=True),
    ])


def send_ctrl_v():
    return send_ctrl_key(VK_V)


def send_ctrl_c():
    return send_ctrl_key(0x43)   # C


def send_shift_enter():
    return send_inputs([
        _key_input(VK_SHIFT),
        _key_input(VK_RETURN),
        _key_input(VK_RETURN, up=True),
        _key_input(VK_SHIFT, up=True),
    ])


def send_unicode_text(text):
    """逐字送出 Unicode 字元；換行改送 Shift+Enter，避免聊天軟體直接把訊息送出。"""
    text = text.replace("\r\n", "\n")
    for line_index, line in enumerate(text.split("\n")):
        if line_index > 0:
            if not send_shift_enter():
                return False
        data = line.encode("utf-16-le")
        inputs = []
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")
            inputs.append(_unicode_input(unit))
            inputs.append(_unicode_input(unit, up=True))
        # 分批送，避免一次塞太多讓目標程式來不及處理
        for start in range(0, len(inputs), 200):
            if not send_inputs(inputs[start:start + 200]):
                return False
    return True


def foreground_window_info():
    """回傳 (程序名稱, 視窗標題)，例如 ("Code.exe", "main.py - Visual Studio Code")。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if handle:
        try:
            size = wintypes.DWORD(1024)
            path_buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)):
                exe = os.path.basename(path_buf.value)
        finally:
            kernel32.CloseHandle(handle)
    return exe, title


def message_box(text, title, icon=0x40):
    """跳出 Windows 對話框（0x40 = 資訊圖示，0x10 = 錯誤圖示）。"""
    try:
        user32.MessageBoxW(None, text, title, icon | 0x00010000)  # MB_SETFOREGROUND
    except Exception:
        pass
