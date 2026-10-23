"""
voice_hotkey_typer.py

按住(或按一下)快捷鍵說話，放開(或再按一下)後自動用本地語音辨識把你說的話
轉成文字，並直接打字到目前游標所在的位置（聊天框、Word、瀏覽器輸入框都可以）。

特色：
- 完全離線／免費：語音辨識使用本地的 faster-whisper 模型，不需要網路、不用 API key
  （只有第一次執行時需要連網下載模型，之後可以完全離線使用）
- 支援中文與其他多國語言辨識
- 可切換「按住說話」或「按一下開始/再按一下結束」兩種模式

使用方式：
1. 先安裝需要的套件：
     pip install -r requirements.txt
2. 執行本程式：
     python voice_hotkey_typer.py
   第一次執行會自動下載語音辨識模型，依模型大小可能要等幾十秒到幾分鐘。
3. 把遊標點到你想打字的地方（例如聊天視窗、記事本、瀏覽器輸入框）。
4. 按住（或按一下，視 MODE 設定而定）下面設定的 HOTKEY 開始說話，
   結束後會自動辨識並把文字打在目前游標的位置。
5. 要結束程式請按 Ctrl+C。

下面的 CONFIG 區塊可以自行調整快捷鍵、語言、模型大小等設定。

快捷鍵偵測用的是 Win32 API 輪詢（GetAsyncKeyState），跟 Typeless 這類專業
工具同一種做法，而不是會攔截按鍵的「低階鍵盤鉤子」，所以不會有鉤子卡住
導致全系統當機、按鍵卡住的風險。

⚠️ 注意：請不要跟 voice_hotkey_tray.py（系統匣版本）同時執行！程式內建了
「同時只能跑一份」的保護（見下面 _ensure_single_instance），重複執行會被
自動擋下來，但還是建議日常只開 voice_hotkey_tray.py 一個就好，這個檔案
主要用來測試、看記錄輸出。
"""

import os
import sys
import threading
import time
import queue

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

# ---------------------------------------------------------------------------
# 只允許同時跑一份：如果同時有兩個（或以上）程式例項在監聽同一個全域快捷鍵
# （不管是這個終端機版、還是系統匣版 voice_hotkey_tray.py），Windows 的
# 按鍵處理可能會被搞亂，導致亂碼甚至整臺電腦當機、按鍵卡住。用一個繫結
# 本機 port 的 socket 當作簡單的「門鎖」（兩個檔案用同一個 port，互相也會
# 擋到），鎖不到就代表已經有一份在跑了，直接結束。
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
    print("偵測到已經有一份語音輸入程式在跑了（這個版本或系統匣版本）。")
    print("為了避免快捷鍵衝突（亂碼／當機），這次不會重複啟動，請先關閉原本那一份。")
    sys.exit(0)

# ========================== CONFIG（可自行修改） ==========================

# 觸發錄音的快捷鍵，可選：right ctrl / left ctrl / caps lock / f6~f12 等
# （完整清單見下面的 _VK_MAP）。不建議用 alt：Windows／部分應用（尤其
# Chromium 系的程式，例如 Claude 桌面版）對 Alt 鍵有特殊處理，單獨按放
# 就會去聚焦選單，這是 OS 層級的行為，跟這個程式怎麼偵測按鍵無關。
HOTKEY = "right ctrl"

# 錄音模式："hold" = 按住說話，放開就辨識；"toggle" = 按一下開始，再按一下結束
MODE = "hold"

# 辨識語言："zh" = 中文，"en" = 英文，None = 自動偵測語言（會稍微慢一點）
LANGUAGE = "zh"

# 模型大小，越大越準但速度越慢、吃的資源越多：
#   tiny < base < small < medium < large-v3
# 你有 4070 Ti／3080 Ti 這種 12GB 獨顯，用 GPU 跑 large-v3 完全沒問題（大約只吃 4~5GB VRAM），
# 準確度會比 medium／small 好很多。如果哪臺電腦還沒裝好 GPU 用的 DLL（見下方 DEVICE 說明、
# README「GPU 加速」那節），程式會自動改用 CPU + medium 繼續運作，不會啟動失敗，只是比較慢。
MODEL_SIZE = "large-v3"

# 運算裝置："cuda"（用 GPU 加速，速度快很多）或 "cpu"。用 "cuda" 前請先看 README「GPU 加速」
# 那一節，把 cuBLAS／cuDNN 的 DLL 放到同資料夾的 cuda_libs 資料夾裡；沒放的話程式會自動
# 偵測失敗並改用 CPU，不影響使用。
DEVICE = "cuda"

# 運算精度：GPU（cuda）建議 "float16"；如果手動把 DEVICE 改回 "cpu"，請把這裡也改成 "int8"
COMPUTE_TYPE = "float16"

# 給辨識引擎一個繁體中文的「風格提示」，幫助辨識結果的用字、標點更貼近臺灣用語習慣
# （不會強迫內容，只是給個參考方向；就算不給這個提示，FORCE_TRADITIONAL 也會轉換簡繁）
INITIAL_PROMPT = "以下是繁體中文的語音內容，請用臺灣常用的詞彙與標點符號記錄。"

SAMPLE_RATE = 16000

# 模擬打字的速度，數字越小打字越快（單位：秒／字元）
TYPE_DELAY = 0.01

# Whisper 辨識中文時常常會輸出簡體字（即使你講的是臺灣腔的中文），
# 開啟這個選項會在辨識完之後自動轉換成繁體中文（臺灣用語），完全本地處理、免費、不用網路
FORCE_TRADITIONAL = True

# --- AI 整理措辭（可選）---
# 開啟後，語音辨識完的原始文字會再送給 Claude 整理成通順、有標點的句子
# （去掉「那個」「然後」之類的口頭禪、修掉重複詞、補標點），才打字出來。
# 需要連網，且需要在同資料夾的 api_key.txt 裡貼上你自己的 Anthropic API Key，
# 否則會自動略過整理、直接用辨識出來的原始文字（不會造成程式無法使用）。
ENABLE_AI_CLEANUP = True
AI_MODEL = "claude-haiku-4-5"
AI_CLEANUP_TIMEOUT = 15   # 秒，長段落整理需要多一點時間
API_KEY_FILE = "api_key.txt"

# ===========================================================================


def _load_model():
    """載入語音辨識模型；如果 GPU（cuda）載入失敗（例如還沒放 cuBLAS／cuDNN
    的 DLL），會自動改用 CPU 繼續運作，不會讓程式整個啟動失敗。"""
    print(f"正在載入語音辨識模型 ({MODEL_SIZE} / {DEVICE})，第一次執行會自動下載模型，請稍等…")
    try:
        return WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
    except Exception as e:
        if DEVICE != "cuda":
            raise
        print(f"GPU（CUDA）模型載入失敗，自動改用 CPU 繼續執行，不影響使用：{e}")
        print("如果想用 GPU 加速，請參考 README「GPU 加速」那一節，把 cuBLAS／cuDNN 的 DLL 放到 cuda_libs 資料夾。")
        fallback_size = "medium" if MODEL_SIZE == "large-v3" else MODEL_SIZE
        print(f"改用 CPU 模式載入模型（{fallback_size}）…")
        return WhisperModel(fallback_size, device="cpu", compute_type="int8")


model = _load_model()

mode_desc = "按住不放來說話，放開就辨識打字" if MODE == "hold" else "按一下開始錄音，再按一下結束並辨識打字"
print(f"模型載入完成！快捷鍵：[{HOTKEY.upper()}]，模式：{mode_desc}")
print("請把游標點到你要打字的地方，然後就可以開始使用了。按 Ctrl+C 結束程式。\n")


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
        print("AI 整理措辭功能已啟用。")
    except Exception as e:
        print(f"AI 整理措辭初始化失敗，將只使用原始辨識結果: {e}")
        _ai_client = None
else:
    if ENABLE_AI_CLEANUP:
        print("尚未設定 Anthropic API Key，AI 整理措辭功能暫停，只會打出原始辨識結果。")

_s2t_converter = None
if FORCE_TRADITIONAL and _HAS_OPENCC:
    try:
        _s2t_converter = opencc.OpenCC("s2twp")
        print("簡體轉繁體功能已啟用。")
    except Exception as e:
        print(f"簡體轉繁體初始化失敗: {e}")
        _s2t_converter = None
elif FORCE_TRADITIONAL:
    print("未安裝 opencc 套件，無法自動把簡體轉成繁體，請執行 pip install -r requirements.txt")


def _to_traditional(text):
    if _s2t_converter is None:
        return text
    try:
        return _s2t_converter.convert(text)
    except Exception as e:
        print(f"簡體轉繁體失敗: {e}")
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
        print(f"AI 整理失敗，改用原始辨識結果: {e}")
        return text

_recording = False
_stream = None
_audio_q = queue.Queue()
_lock = threading.Lock()


def _audio_callback(indata, frames_count, time_info, status):
    if _recording:
        _audio_q.put(indata.copy())


# 麥克風只在程式啟動時開一次、之後一直開著，錄音/結束只是切換旗標，
# 不是每次重新開關麥克風裝置。主要是為了穩定性：有些電腦上的音效強化
# 服務（例如部分主機板/音效卡內建的 Nahimic 之類的服務）在麥克風被
# 頻繁開關、或開機階段被搶先存取時容易出問題，讓麥克風只開一次可以
# 大幅降低這類衝突。
def _start_persistent_stream():
    global _stream
    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=_audio_callback,
        )
        stream.start()
        _stream = stream
        print("麥克風已就緒。")
    except Exception as e:
        _stream = None
        print(f"開啟麥克風失敗，錄音功能可能無法使用: {e}")


def _start_recording():
    """
    這個函式是被 main() 裡的輪詢迴圈直接呼叫的，最好保持快速返回（拖太
    久會拖慢迴圈偵測放開按鍵的反應速度）。麥克風已經在程式啟動時開好、
    持續開著，這裡只需要清空緩衝、翻旗標。
    """
    global _recording
    if _stream is None:
        print("麥克風尚未就緒，無法開始錄音。")
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

    print("🎙️  錄音中…")


def _stop_recording_and_transcribe():
    """
    這個函式是被 main() 裡的輪詢迴圈直接呼叫的（偵測到按鍵放開）。這裡只
    做「翻個旗標、丟給背景執行緒」這種瞬間完成的事，真正耗時的辨識／AI
    整理／打字都丟到 _finish_recording() 背景執行緒去做，讓輪詢迴圈可以
    馬上繼續偵測下一次按鍵。麥克風本身持續開著，不在這裡關閉。
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

    if not chunks:
        print("沒有錄到聲音，略過。\n")
        return

    audio = np.concatenate(chunks, axis=0).flatten()
    duration = len(audio) / SAMPLE_RATE
    if duration < 0.3:
        print("錄音時間太短，略過。\n")
        return

    print("辨識中…")
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
        print(f"辨識失敗: {e}")
        return

    if not text:
        print("沒有辨識出文字。\n")
        return

    print(f"辨識結果：{text}")
    if _ai_client is not None:
        print("AI 整理措辭中…")
        cleaned = _ai_cleanup(text)
        if cleaned != text:
            print(f"整理後：{cleaned}")
        text = cleaned
    print()
    # 稍微停頓，避免快捷鍵放開的瞬間被一起打進去
    time.sleep(0.05)
    try:
        keyboard.write(text, delay=TYPE_DELAY)
    except Exception as e:
        print(f"打字失敗: {e}")


def _toggle_recording():
    if _recording:
        _stop_recording_and_transcribe()
    else:
        _start_recording()


# ---------------------------------------------------------------------------
# 快捷鍵偵測：用輪詢 GetAsyncKeyState 的方式，不裝「全域低階鍵盤鉤子」。
#
# 之前的版本用 keyboard 套件裝了一個會攔截（suppress）按鍵的低階鍵盤鉤子
# （WH_KEYBOARD_LL）。這種做法的問題是：Windows 要求這個鉤子的回呼必須
# 瞬間返回，一旦處理卡住、或是同時有兩份程式的鉤子互相打架，就可能讓
# Windows 認為鍵盤輸入卡死，導致全系統按鍵失靈（當機、按鍵卡住放不開）。
# 這也是專業的語音輸入工具（包含 Typeless 這類軟體）不會用這種方式的
# 原因。改成用 Win32 API 的 GetAsyncKeyState() 每 20 毫秒輪詢一次按鍵
# 狀態，純粹讀取、不裝鉤子、不攔截任何按鍵，不可能造成這種全系統當機。
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


def main():
    _start_persistent_stream()

    import ctypes

    vk = _resolve_vk(HOTKEY)
    if vk is None:
        supported = "、".join(sorted(_VK_MAP.keys()))
        print(f"不支援的 HOTKEY 名稱：\"{HOTKEY}\"，請改用下列其中一個：{supported}")
        return

    user32 = ctypes.windll.user32
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

    pressed = False
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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已結束程式。")
        sys.exit(0)
