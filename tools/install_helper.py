"""install.bat 的幫手：處理批次檔不好做的步驟，訊息一律用白話中文。

用法：python tools\\install_helper.py <步驟>
  cuda       把顯示卡加速需要的 cuBLAS DLL 複製到 cuda_libs 資料夾
  model      下載辨識模型（large-v3，約 3GB，第一次要等一陣子）
  check      檢查顯示卡、麥克風、API Key 狀態
  autostart  設定開機自動啟動
  shortcut   在桌面建立「語音輸入」捷徑
"""

import glob
import os
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)


def say(msg):
    print(msg, flush=True)


def use_windows_certificates():
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass


def step_cuda():
    target = os.path.join(BASE_DIR, "cuda_libs")
    os.makedirs(target, exist_ok=True)
    try:
        import nvidia.cublas
    except ImportError:
        say("  ！找不到 nvidia-cublas 套件，顯示卡加速可能無法使用（程式會自動改用 CPU，只是比較慢）。")
        return 0
    copied = 0
    for p in nvidia.cublas.__path__:
        for dll in glob.glob(os.path.join(p, "bin", "*.dll")):
            dest = os.path.join(target, os.path.basename(dll))
            if not os.path.exists(dest) or os.path.getsize(dest) != os.path.getsize(dll):
                shutil.copy2(dll, dest)
                copied += 1
    say(f"  ✓ 顯示卡加速檔案已就緒（cuda_libs，新複製 {copied} 個檔案）")
    return 0


def step_model():
    use_windows_certificates()
    from voice_input.config import load_config
    model = load_config()["asr"]["whisper"]["model"]
    say(f"  正在下載辨識模型 {model}（約 3GB）。已經下載過的話會直接跳過。")
    say("  網路慢的話可能要十幾分鐘，請不要關掉這個視窗……")
    try:
        from faster_whisper.utils import download_model
        path = download_model(model)
    except Exception as e:
        say(f"  ✗ 模型下載失敗：{e}")
        say("    請確認網路有通，再重新執行一次 install.bat（已下載的部分不會重來）。")
        return 1
    say(f"  ✓ 模型已就緒：{path}")
    return 0


def step_check():
    use_windows_certificates()
    problems = 0
    # 顯示卡
    try:
        sys.path.insert(0, BASE_DIR)
        from voice_input.asr import whisper_engine  # noqa: F401  （會順便設定 cuda_libs 路徑）
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        if n > 0:
            say("  ✓ 偵測到 NVIDIA 顯示卡，會用顯示卡加速")
        else:
            say("  ！沒有偵測到可用的 NVIDIA 顯示卡，會改用 CPU（可以用，但比較慢）")
    except Exception as e:
        say(f"  ！檢查顯示卡時發生問題：{e}")
    # 麥克風
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        say(f"  ✓ 麥克風：{dev['name']}")
    except Exception:
        say("  ✗ 找不到麥克風。請插上麥克風，並到 Windows「設定 → 隱私權 → 麥克風」允許應用程式使用。")
        problems += 1
    # API Key（只讀，不會修改 api_key.txt）
    from voice_input.corrector import load_api_key
    if load_api_key():
        say("  ✓ 已找到 api_key.txt 裡的 API Key（AI 校正可以使用）")
    else:
        say("  ！api_key.txt 裡沒有 API Key：語音輸入可以用，但不會有 AI 校正。")
        say("    之後想用的話，把 Anthropic API Key 貼進 api_key.txt 存檔，再重開程式即可。")
    return 1 if problems else 0


def step_autostart():
    from voice_input import autostart
    autostart.enable()
    say("  ✓ 已設定開機自動啟動（之後可以在系統匣圖示的右鍵選單取消）")
    return 0


def step_shortcut():
    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    link = os.path.join(desktop, "語音輸入.lnk")
    target = os.path.join(BASE_DIR, "启动语音输入.vbs")
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:VI_LINK); "
        "$s.TargetPath = $env:VI_TARGET; $s.WorkingDirectory = $env:VI_DIR; "
        "$s.IconLocation = \"$env:SystemRoot\\System32\\imageres.dll,109\"; "
        "$s.Description = '語音輸入：按住 CapsLock 說話'; $s.Save()"
    )
    env = dict(os.environ, VI_LINK=link, VI_TARGET=target, VI_DIR=BASE_DIR)
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       env=env, capture_output=True, text=True)
    if r.returncode == 0 and os.path.exists(link):
        say("  ✓ 已在桌面建立「語音輸入」捷徑")
        return 0
    say(f"  ！桌面捷徑建立失敗（不影響使用，可以直接雙擊資料夾裡的 启动语音输入.vbs）：{r.stderr.strip()}")
    return 0


STEPS = {"cuda": step_cuda, "model": step_model, "check": step_check,
         "autostart": step_autostart, "shortcut": step_shortcut}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in STEPS:
        say(__doc__)
        sys.exit(2)
    sys.exit(STEPS[sys.argv[1]]())
