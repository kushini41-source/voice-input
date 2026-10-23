"""
voice_hotkey_tray.py

系統匣（通知區域）版本的「按鍵說話、自動打字」工具，操作方式仿照
Typeless／superwhisper 這類語音輸入軟體：按住快捷鍵說話，畫面角落會跳出
一個小浮動狀態框，放開就結束錄音，自動辨識、打字，浮動框就會消失。

快捷鍵偵測用的是 Win32 API 輪詢（GetAsyncKeyState），跟 Typeless 這類
專業工具同一種做法，而不是會攔截按鍵的「低階鍵盤鉤子」，所以不會有鉤子
卡住導致全系統當機、按鍵卡住的風險，細節見下面 _hotkey_thread() 的說明。

執行方式（不會看到黑色視窗，直接縮到右下角系統匣）：
    pyw voice_hotkey_tray.py

在系統匣的小圖示上按右鍵，可以看到目前狀態、或按「結束」離開程式。
圖示顏色（還有螢幕角落浮動框的顏色）代表狀態：
藍色/待命中 → 紅色 = 錄音中 → 黃色 = 辨識中 → 綠色 = AI 整理措辭中
（綠色這個階段是可選的，需要設定 Anthropic API Key 才會出現，見下方說明）。

發生任何問題時的紀錄會寫在同資料夾下的 voice_typer.log 裡，方便除錯。

設定項目跟 voice_hotkey_typer.py 大致相同，請看下面的 CONFIG 區塊
（這個版本多了 SHOW_OVERLAY 設定）。
"""

import os
import sys
import threading
import time
import queue

try:
    import tkinter as tk
    _HAS_TKINTER = True
except ImportError:
    _HAS_TKINTER = False

import numpy as np
import sounddevice as sd
import keyboard

# 如果同資料夾下有 cuda_libs 資料夾（放 cuBLAS／cuDNN 的 DLL，給 GPU／CUDA
# 加速用），會自動加進 Windows 的 DLL 搜尋路徑。DLL 要去哪裡下載、怎麼放，
# 請看 README「GPU 加速」那一節。沒有這個資料夾也完全沒關係，程式一樣能
# 用 CPU 正常運作（見下面 DEVICE、_load_model 的自動 CPU 備援說明）。
_CUDA_LIBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cuda_libs")
if os.path.isdir(_CUDA_LIBS_DIR) and hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(_CUDA_LIBS_DIR)
    except Exception:
        pass

from faster_whisper import WhisperModel
import pystray
from PIL import Image, ImageDraw

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

try:
    import opencc
    _HAS_OPENCC = True
except ImportError:
    _HAS_OPENCC = False

try:
    import winsound
    _HAS_WINSOUND = True
except ImportError:
    _HAS_WINSOUND = False

# 用 pyw 執行時沒有終端機，直接 print 會出錯，這裡把輸出導去記錄檔
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_typer.log")
try:
    sys.stdout = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stderr = sys.stdout
except Exception:
    pass


def log(msg):
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 只允許同時跑一份：如果同時有兩個（或以上）程式例項在監聽同一個全域快捷鍵
# （不管是這個系統匣版本、還是終端機版 voice_hotkey_typer.py），Windows
# 的按鍵處理可能會被搞亂，導致亂碼甚至整臺電腦當機、按鍵卡住。用一個繫結
# 本機 port 的 socket 當作簡單的「門鎖」，鎖不到就代表已經有一份在跑了，
# 直接結束，不會真的對外監聽任何東西。
# ---------------------------------------------------------------------------
_SINGLE_INSTANCE_PORT = 47623
_singleton_socket = None


def _ensure_single_instance():
    global _singleton_socket
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLE_INSTANCE_PORT))
    except OSError:
        return False
    _singleton_socket = s  # 故意不關閉，讓這個「門鎖」鎖到程式結束為止
    return True


if not _ensure_single_instance():
    log("偵測到已經有一份語音輸入程式在跑了，為了避免快捷鍵衝突（亂碼／當機），這次不會重複啟動。")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "語音輸入已經在背景執行中了（看系統匣的圖示）。\n"
            "不要同時開兩份，否則可能造成快捷鍵衝突、打字變亂碼，甚至系統當機。",
            "語音輸入 - 已經在執行了",
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:
        pass
    sys.exit(0)


# ========================== CONFIG（可自行修改） ==========================
HOTKEY = "right ctrl"     # 觸發錄音的快捷鍵，可選：right ctrl / left ctrl / caps lock / f6~f12 等（不建議用 alt，見 _hotkey_thread 說明）
MODE = "hold"             # "hold" = 按住說話放開辨識；"toggle" = 按一下開始、再按一下結束
SHOW_OVERLAY = True       # 是否在螢幕角落顯示一個像 Typeless 那樣的小浮動狀態圖示
LANGUAGE = "zh"           # "zh" 中文／"en" 英文／None 自動偵測
MODEL_SIZE = "large-v3"   # tiny / base / small / medium / large-v3，越大越準。你有 4070 Ti／3080 Ti 這種 12GB 獨顯，
                          # 用 GPU 跑 large-v3 完全沒問題（大約只吃 4~5GB VRAM）。如果哪臺電腦還沒裝好 GPU 用的
                          # DLL（見下方 DEVICE 說明、README「GPU 加速」），程式會自動改用 CPU + medium 繼續運作，
                          # 不會啟動失敗，只是速度會比較慢。
DEVICE = "cuda"           # "cuda"（用 GPU 加速，速度快很多）或 "cpu"。用 "cuda" 前請先看 README「GPU 加速」
                          # 那一節，把 cuBLAS／cuDNN 的 DLL 放到同資料夾的 cuda_libs 資料夾裡；沒放的話程式會
                          # 自動偵測失敗並改用 CPU，不影響使用。
COMPUTE_TYPE = "float16"  # GPU（cuda）建議 float16；如果手動把 DEVICE 改回 "cpu"，請把這裡也改成 "int8"
# 給辨識引擎一個繁體中文的「風格提示」，幫助辨識結果的用字、標點更貼近臺灣用語習慣
# （不會強迫內容，只是給個參考方向；就算不給這個提示，FORCE_TRADITIONAL 也會轉換簡繁）
INITIAL_PROMPT = "以下是繁體中文的語音內容，請用臺灣常用的詞彙與標點符號記錄。"
SAMPLE_RATE = 16000
TYPE_DELAY = 0.01
BEEP_ON_ACTION = True     # 錄音開始/結束時是否發出提示音（因為看不到視窗，建議開著）
MIC_STARTUP_DELAY = 3     # 秒，啟動後延遲多久才開麥克風（避開開機時跟音效服務撞在一起）

# Whisper 辨識中文時常常會輸出簡體字（即使你講的是臺灣腔的中文），
# 開啟這個選項會在辨識完之後自動轉換成繁體中文（臺灣用語），完全本地處理、免費、不用網路
FORCE_TRADITIONAL = True

# --- AI 整理措辭（可選）---
# 開啟後，語音辨識完的原始文字會再送給 Claude 整理成通順、有標點的句子
# （去掉「那個」「然後」之類的口頭禪、修掉重複詞、補標點），才打字出來。
# 需要連網，且需要在同資料夾的 api_key.txt 裡貼上你自己的 Anthropic API Key，
# 否則會自動略過整理、直接用辨識出來的原始文字（不會造成程式無法使用）。
ENABLE_AI_CLEANUP = True
AI_MODEL = "claude-haiku-4-5"     # 便宜、夠快，短句整理、長段落整理都適合
AI_CLEANUP_TIMEOUT = 15           # 秒，超過就放棄整理、直接用原始辨識結果（長段落整理需要多一點時間）
API_KEY_FILE = "api_key.txt"      # 放 Anthropic API Key 的檔案（跟本程式同資料夾）
# ===========================================================================

log("程式啟動，開始載入語音辨識模型…")

_recording = False
_stream = None
_audio_q = queue.Queue()
_lock = threading.Lock()
_status = "載入模型中…"
_icon = None

def _load_model():
    """載入語音辨識模型；如果 GPU（cuda）載入失敗（例如還沒放 cuBLAS／cuDNN
    的 DLL），會自動改用 CPU 繼續運作，不會讓程式整個啟動失敗。"""
    try:
        m = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        log(f"模型載入完成（{DEVICE} / {COMPUTE_TYPE} / {MODEL_SIZE}）。快捷鍵：{HOTKEY.upper()}，模式：{MODE}")
        return m
    except Exception as e:
        if DEVICE != "cuda":
            log(f"模型載入失敗，程式無法啟動：{e}")
            raise
        log(f"GPU（CUDA）模型載入失敗，自動改用 CPU 繼續執行，不影響使用：{e}")
        log("如果想用 GPU 加速，請參考 README「GPU 加速」那一節，把 cuBLAS／cuDNN 的 DLL 放到 cuda_libs 資料夾。")
        fallback_size = "medium" if MODEL_SIZE == "large-v3" else MODEL_SIZE
        try:
            m = WhisperModel(fallback_size, device="cpu", compute_type="int8")
            log(f"已改用 CPU 模式載入模型（{fallback_size}）。快捷鍵：{HOTKEY.upper()}，模式：{MODE}")
            return m
        except Exception as e2:
            log(f"CPU 模式也載入失敗，程式無法啟動：{e2}")
            raise


try:
    model = _load_model()
except Exception:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "語音辨識模型載入失敗，程式無法啟動。\n請看同資料夾的 voice_typer.log 瞭解詳細錯誤內容。",
            "語音輸入 - 啟動失敗",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass
    sys.exit(1)

_status = f"待命中 (按 {HOTKEY.upper()} 開始)"


def _load_api_key():
    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), API_KEY_FILE)
    try:
        with open(key_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except Exception:
        pass
    return ""


_api_key = _load_api_key()
_ai_client = None
if ENABLE_AI_CLEANUP and _HAS_ANTHROPIC and _api_key:
    try:
        _ai_client = anthropic.Anthropic(api_key=_api_key, timeout=AI_CLEANUP_TIMEOUT)
        log("AI 整理措辭功能已啟用")
    except Exception as e:
        log(f"AI 整理措辭初始化失敗，將只使用原始辨識結果: {e}")
        _ai_client = None
else:
    if ENABLE_AI_CLEANUP:
        log("尚未設定 Anthropic API Key（或未安裝 anthropic 套件），AI 整理措辭功能暫停，只會打出原始辨識結果")

_s2t_converter = None
if FORCE_TRADITIONAL and _HAS_OPENCC:
    try:
        _s2t_converter = opencc.OpenCC("s2twp")
        log("簡體轉繁體功能已啟用")
    except Exception as e:
        log(f"簡體轉繁體初始化失敗: {e}")
        _s2t_converter = None
elif FORCE_TRADITIONAL:
    log("未安裝 opencc 套件，無法自動把簡體轉成繁體，請執行 pip install -r requirements.txt")


def _to_traditional(text):
    if _s2t_converter is None:
        return text
    try:
        return _s2t_converter.convert(text)
    except Exception as e:
        log(f"簡體轉繁體失敗: {e}")
        return text


_CLEANUP_SYSTEM_PROMPT = (
    "你是語音輸入的文字整理助手。使用者會給你一段語音辨識出來的原始文字（逐字稿），"
    "這段文字是語音辨識引擎自動轉出來的，裡面除了口頭禪、重複詞、缺少標點、語句不通順之外，"
    "很常見的情況是某些字詞被辨識成「音相近但意思不對」的同音異字或錯字"
    "（例如把「辨識」聽成「變形」、把「設定」聽成「設訂」、把「你好」聽成「泥壕」這類明顯不合語意的錯誤）。\n"
    "請一律先檢查並修正這類明顯的語音辨識錯字／同音字錯誤："
    "只要從上下文可以清楚判斷原本應該是哪個詞，就直接修正成最合理的字詞，不用保留錯字。"
    "這個修正規則不分句子長短，簡短的一兩句話也要修正。\n"
    "接著請依照內容長短，用不同程度處理：\n"
    "- 如果只是簡短的一兩句話（例如指令、訊息、簡單的句子），"
    "在修正好辨識錯字的基礎上，只需要輕度整理：去掉口頭禪、補標點、讓語句通順，"
    "不改變原本想表達的意思，也不要增加使用者沒說過的新內容。\n"
    "- 如果是較長的一段口述內容（例如說明一件事、表達一個想法、口述一段文字），"
    "請整理成有條理、適當分段的書面文字：讓語句完整通順、補上必要的連接詞，"
    "讀起來像整理過的文章，但絕對不可以編造使用者沒有表達過的事實、數字、立場，"
    "也不要自己加上新的論點或結論，只能重新組織、順過使用者本來就說過的內容。\n"
    "請注意：修正同音字錯字是為了還原使用者「原本想說的話」，不是改寫或延伸使用者的意思，"
    "如果不確定原意是什麼，寧可保留原文，不要用猜的硬改。\n"
    "請一律使用繁體中文（臺灣用語習慣），絕對不要輸出簡體字。"
    "段落之間用換行分隔，不要用 Markdown 標題或項目符號。"
    "只回傳整理後的文字本身，不要加任何說明、引號或前後綴。"
)


def _ai_cleanup(text):
    if _ai_client is None:
        return text
    try:
        resp = _ai_client.messages.create(
            model=AI_MODEL,
            max_tokens=4096,
            system=_CLEANUP_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        cleaned = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        ).strip()
        return cleaned if cleaned else text
    except Exception as e:
        log(f"AI 整理失敗，改用原始辨識結果: {e}")
        return text


def _beep(freq, dur=120):
    # winsound.Beep() 本身會阻塞，為了不卡到全域快捷鍵的 hook 執行緒，
    # 一律丟到獨立的執行緒去播放
    if BEEP_ON_ACTION and _HAS_WINSOUND:
        def _play():
            try:
                winsound.Beep(freq, dur)
            except Exception:
                pass
        threading.Thread(target=_play, daemon=True).start()


def _make_icon_image(color):
    img = Image.new("RGB", (64, 64), "white")
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=color)
    return img


_ICON_IDLE = _make_icon_image((60, 120, 220))
_ICON_RECORDING = _make_icon_image((220, 60, 60))
_ICON_BUSY = _make_icon_image((230, 180, 40))
_ICON_CLEANUP = _make_icon_image((60, 180, 100))


# ---------------------------------------------------------------------------
# 螢幕角落的浮動狀態圖示（像 Typeless／superwhisper 那種小提示框）：
# 按快捷鍵開始錄音時出現，處理完（或再按一次取消）就消失。
# tkinter 需要有自己獨立的執行緒跑 mainloop，跟系統匣（pystray）分開，
# 兩邊用一個 queue 溝通，避免跨執行緒直接操作 tkinter 元件。
# ---------------------------------------------------------------------------
_overlay_q = queue.Queue()

_OVERLAY_COLORS = {
    "recording": "#dc3c3c",
    "busy": "#e6b428",
    "cleanup": "#3cb464",
}
_OVERLAY_LABELS = {
    "recording": "🎙️ 錄音中",
    "busy": "⏳ 辨識中…",
    "cleanup": "✨ AI 整理中…",
}


_overlay_enabled = SHOW_OVERLAY and _HAS_TKINTER
if SHOW_OVERLAY and not _HAS_TKINTER:
    log("未偵測到 tkinter，浮動狀態圖示功能停用（不影響其他功能）")


def _overlay_show(state):
    if _overlay_enabled:
        _overlay_q.put(("show", state))


def _overlay_hide():
    if _overlay_enabled:
        _overlay_q.put(("hide", None))


def _overlay_thread():
    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.92)
        except Exception:
            pass

        width, height = 170, 46
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = screen_w - width - 24
        y = screen_h - height - 80
        root.geometry(f"{width}x{height}+{x}+{y}")

        label = tk.Label(
            root, text="", fg="white", bg="#dc3c3c",
            font=("Microsoft JhengHei UI", 11, "bold"),
        )
        label.pack(expand=True, fill="both")
        root.withdraw()

        def poll():
            try:
                while True:
                    cmd, state = _overlay_q.get_nowait()
                    if cmd == "show":
                        color = _OVERLAY_COLORS.get(state, "#3c78dc")
                        text = _OVERLAY_LABELS.get(state, state)
                        label.configure(text=text, bg=color)
                        root.configure(bg=color)
                        root.deiconify()
                    elif cmd == "hide":
                        root.withdraw()
            except queue.Empty:
                pass
            root.after(50, poll)

        root.after(50, poll)
        root.mainloop()
    except Exception as e:
        log(f"浮動狀態圖示發生錯誤（不影響其他功能）: {e}")


def _set_status(text, image=None):
    global _status
    _status = text
    if _icon is not None:
        try:
            _icon.title = f"語音輸入 - {text}"
            if image is not None:
                _icon.icon = image
        except Exception:
            pass


def _audio_callback(indata, frames_count, time_info, status):
    if _recording:
        _audio_q.put(indata.copy())


# ---------------------------------------------------------------------------
# 麥克風串流的開法：只在程式啟動時開啟「一次」，之後一直開著不關，錄音／
# 結束只是切換「要不要把音訊放進 queue」的旗標，而不是每次都重新開關
# 麥克風裝置。
#
# 這樣做主要是為了穩定性：有些電腦上的音效強化服務（例如部分主機板／
# 音效卡內建的 Nahimic 之類的服務）在麥克風被頻繁開關、或是在開機開機
# 階段被搶先存取時容易出問題、甚至跳出錯誤視窗，跟這個程式本身無關，
# 但讓麥克風只在啟動時開一次、並延遲幾秒再開，可以大幅降低這類衝突。
# ---------------------------------------------------------------------------
def _start_persistent_stream():
    global _stream
    try:
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=_audio_callback)
        stream.start()
        _stream = stream
        log("麥克風已就緒（開機／啟動時只會開啟一次）")
    except Exception as e:
        _stream = None
        log(f"開啟麥克風失敗，錄音功能可能無法使用（可以試著重新執行程式）: {e}")


def _stop_persistent_stream():
    global _stream
    s = _stream
    _stream = None
    if s is not None:
        try:
            s.stop()
            s.close()
        except Exception:
            pass


def _start_recording():
    """
    這個函式是被 _hotkey_thread() 的輪詢迴圈直接呼叫的，最好保持快速返回
    （拖太久會拖慢迴圈偵測放開按鍵的反應速度），麥克風已經在程式啟動時
    開好、持續開著，這裡只需要清空緩衝、翻旗標。
    """
    global _recording
    if _stream is None:
        log("麥克風尚未就緒，無法開始錄音")
        return

    with _lock:
        if _recording:
            return
        _recording = True

    while not _audio_q.empty():
        try:
            _audio_q.get_nowait()
        except queue.Empty:
            break

    _set_status("錄音中…", _ICON_RECORDING)
    _overlay_show("recording")
    _beep(880)
    log("開始錄音")


def _stop_recording_and_transcribe():
    """
    這個函式是被 _hotkey_thread() 的輪詢迴圈直接呼叫的（偵測到按鍵放開）。
    這裡只做「翻個旗標、丟給背景執行緒」這種瞬間完成的事，真正耗時的辨識／
    AI 整理／打字都丟到 _finish_recording() 背景執行緒去做，這樣輪詢迴圈
    才能馬上繼續偵測下一次按鍵，不會卡住反應速度。麥克風本身持續開著，
    不在這裡關閉（見上面 _start_persistent_stream 的說明）。
    """
    global _recording
    with _lock:
        if not _recording:
            return
        _recording = False

    threading.Thread(target=_finish_recording, daemon=True).start()


def _finish_recording():
    chunks = []
    while not _audio_q.empty():
        try:
            chunks.append(_audio_q.get_nowait())
        except queue.Empty:
            break

    _set_status("辨識中…", _ICON_BUSY)
    _overlay_show("busy")
    _beep(440)

    _process_audio(chunks)


def _process_audio(chunks):
    idle_text = f"待命中 (按 {HOTKEY.upper()} 開始)"

    if not chunks:
        log("沒有錄到聲音")
        _set_status(idle_text, _ICON_IDLE)
        _overlay_hide()
        return

    audio = np.concatenate(chunks, axis=0).flatten()
    duration = len(audio) / SAMPLE_RATE
    if duration < 0.3:
        log("錄音太短，略過")
        _set_status(idle_text, _ICON_IDLE)
        _overlay_hide()
        return

    text = ""
    try:
        segments, _info = model.transcribe(
            audio,
            language=LANGUAGE,
            vad_filter=True,
            beam_size=5,
            initial_prompt=INITIAL_PROMPT,
        )
        text = "".join(seg.text for seg in segments).strip()
        text = _to_traditional(text)
    except Exception as e:
        log(f"辨識失敗: {e}")

    if text:
        log(f"辨識結果: {text}")
        if _ai_client is not None:
            _set_status("AI 整理措辭中…", _ICON_CLEANUP)
            _overlay_show("cleanup")
            cleaned = _ai_cleanup(text)
            if cleaned != text:
                log(f"整理後: {cleaned}")
            text = cleaned
        time.sleep(0.05)
        try:
            keyboard.write(text, delay=TYPE_DELAY)
        except Exception as e:
            log(f"打字失敗: {e}")
    else:
        log("沒有辨識出文字")

    _set_status(idle_text, _ICON_IDLE)
    _overlay_hide()


def _toggle_recording():
    if _recording:
        _stop_recording_and_transcribe()
    else:
        _start_recording()


# ---------------------------------------------------------------------------
# 快捷鍵偵測：用輪詢 GetAsyncKeyState 的方式，不裝「全域低階鍵盤鉤子」。
#
# 之前的版本用 Python 的 keyboard 套件裝了一個會攔截（suppress）按鍵的
# 低階鍵盤鉤子（WH_KEYBOARD_LL）。這種做法的問題是：Windows 要求這個鉤子
# 的回呼必須瞬間返回，一旦處理卡住，或是同時有兩份程式的鉤子互相打架，
# 就可能讓 Windows 認為鍵盤輸入卡死，導致全系統按鍵失靈（就是你遇到的
# 當機、按鍵卡住放不開）。這也是很多專業的語音輸入工具（包含 Typeless
# 這類軟體）不會用這種方式的原因。
#
# 改成用 Win32 API 的 GetAsyncKeyState() 每 20 毫秒「問」一次「這個鍵現在
# 是不是按著」，純粹是讀取狀態、不裝任何鉤子、不攔截任何按鍵，所以：
# - 不會卡住 Windows 的鍵盤輸入管線，不可能造成這種全系統當機
# - 按鍵還是會正常傳給你目前在用的視窗（這也是為什麼用 Ctrl 比用 Alt 好：
#   Ctrl 自己放開不會觸發任何應用的選單，Alt 會）
# ---------------------------------------------------------------------------
_VK_MAP = {
    "right ctrl": 0xA3, "left ctrl": 0xA2, "ctrl": 0xA3,
    "right alt": 0xA5, "left alt": 0xA4, "alt": 0xA5,
    "right shift": 0xA1, "left shift": 0xA0, "shift": 0xA1,
    "caps lock": 0x14,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}

_POLL_INTERVAL = 0.02  # 20 毫秒，反應夠快、CPU 負擔極低


def _resolve_vk(hotkey_name):
    return _VK_MAP.get(hotkey_name.strip().lower())


def _hotkey_thread():
    import ctypes

    vk = _resolve_vk(HOTKEY)
    if vk is None:
        supported = "、".join(sorted(_VK_MAP.keys()))
        log(f"不支援的 HOTKEY 名稱：\"{HOTKEY}\"，請改用下列其中一個：{supported}")
        return

    user32 = ctypes.windll.user32
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

    pressed = False
    try:
        while True:
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not pressed:
                pressed = True
                if MODE == "hold":
                    _start_recording()
                else:
                    _toggle_recording()
            elif not down and pressed:
                pressed = False
                if MODE == "hold":
                    _stop_recording_and_transcribe()
                # toggle 模式放開不用做事，等下一次按下才會再觸發
            time.sleep(_POLL_INTERVAL)
    except Exception as e:
        log(f"快捷鍵監聽發生錯誤: {e}")


def _on_quit(icon, item):
    log("使用者結束程式")
    _stop_persistent_stream()
    icon.stop()
    os._exit(0)


def _init_audio_after_delay():
    # 延遲幾秒再開麥克風：開機自動啟動時，Windows 跟一些音效強化服務
    # （例如 Nahimic 之類的）本身也還在初始化，太早去搶麥克風容易撞上
    # 這類服務、甚至讓它們跳出錯誤視窗。手動執行時這幾秒延遲感覺不到，
    # 系統匣圖示會先出現，快捷鍵要等麥克風就緒後才能開始錄音。
    if MIC_STARTUP_DELAY > 0:
        time.sleep(MIC_STARTUP_DELAY)
    _start_persistent_stream()


def main():
    global _icon

    threading.Thread(target=_init_audio_after_delay, daemon=True).start()

    t = threading.Thread(target=_hotkey_thread, daemon=True)
    t.start()

    if _overlay_enabled:
        threading.Thread(target=_overlay_thread, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: _status, None, enabled=False),
        pystray.MenuItem("結束", _on_quit),
    )
    _icon = pystray.Icon("voice_hotkey_typer", _ICON_IDLE, f"語音輸入 - {_status}", menu)
    _icon.run()


if __name__ == "__main__":
    main()
