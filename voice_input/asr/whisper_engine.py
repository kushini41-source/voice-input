"""faster-whisper 辨識引擎（預設 large-v3 + 顯示卡 float16）。

CUDA 需要的 cuBLAS DLL 放在程式旁邊的 cuda_libs 資料夾；cuDNN 已經內建在 ctranslate2 套件裡。
顯示卡載入或暖機失敗時，自動改用 CPU（medium + int8）並跳通知。
"""

import logging
import os
import re
import time

from ..paths import CUDA_LIBS_DIR
from .base import ASREngine

log = logging.getLogger(__name__)

# Whisper 在沒什麼聲音時常會「腦補」出影片字幕的套話，整句等於這些就丟掉
_HALLUCINATIONS = {
    "請不吝點贊訂閱轉發打賞支持明鏡與點點欄目",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "字幕由Amara.org社區提供",
    "字幕由Amara.org社区提供",
    "謝謝觀看", "谢谢观看", "謝謝收看", "谢谢收看",
    "感謝觀看", "感谢观看", "訂閱", "订阅",
}
_PUNCT = re.compile(r"[\s，。！？、,.!?]+")

# initial_prompt 太長會被截掉，專有名詞最多放這麼多字
_PROMPT_TERMS_MAX_CHARS = 150


def _setup_cuda_dlls():
    """讓 ctranslate2 找得到 cuda_libs 裡的 DLL。

    只用 os.add_dll_directory() 不夠：ctranslate2 是執行到一半才載入 cuBLAS，
    那時 Windows 只看 PATH，所以兩個都要設。
    """
    dirs = []
    if os.path.isdir(CUDA_LIBS_DIR):
        dirs.append(CUDA_LIBS_DIR)
    # 用 pip 裝的 nvidia-cublas-cu12 也一併加入，當作備援
    try:
        import nvidia.cublas  # noqa: F401
        for p in nvidia.cublas.__path__:
            bin_dir = os.path.join(p, "bin")
            if os.path.isdir(bin_dir):
                dirs.append(bin_dir)
    except Exception:
        pass
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


_setup_cuda_dlls()


def build_initial_prompt(base_prompt, terms):
    """固定提示句 + 專有名詞（依序放，超過長度就停）。"""
    picked, total = [], 0
    for term in terms:
        term = str(term).strip()
        if not term:
            continue
        if total + len(term) + 1 > _PROMPT_TERMS_MAX_CHARS:
            break
        picked.append(term)
        total += len(term) + 1
    if not picked:
        return base_prompt
    return f"{base_prompt}常用詞：{'、'.join(picked)}。"


def is_hallucination(text, prompt=""):
    core = _PUNCT.sub("", text)
    if not core:
        return False
    if core in {_PUNCT.sub("", h) for h in _HALLUCINATIONS}:
        return True
    # 聲音太少時，Whisper 常會把提示句原封不動「背」出來（例如「請使用台灣常用的詞彙…」）
    prompt_core = _PUNCT.sub("", prompt)
    return len(core) >= 3 and core in prompt_core


class WhisperEngine(ASREngine):
    name = "whisper"

    def __init__(self, cfg, notify=None):
        self.cfg = cfg
        self.notify = notify or (lambda title, msg: None)
        self.model = None
        self.device = cfg["device"]
        self.compute_type = cfg["compute_type"]
        self.model_name = cfg["model"]

    def close(self):
        self.model = None
        import gc
        gc.collect()

    def describe(self):
        return f"whisper {self.model_name}（{self.device} / {self.compute_type}）"

    def _load_and_warmup(self, model_name, device, compute_type):
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        try:
            # 先用已經下載好的模型，不連網檢查更新（沒網路時也能馬上啟動）
            self.model = WhisperModel(model_name, device=device, compute_type=compute_type, local_files_only=True)
        except Exception as e:
            if device == "cuda" and "cuda" in str(e).lower():
                raise
            log.info("本機還沒有 %s 模型，開始下載（約 3GB，請保持網路連線）", model_name)
            self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        self.model_name, self.device, self.compute_type = model_name, device, compute_type
        t1 = time.perf_counter()
        # 顯示卡的 DLL 問題常常要到第一次推論才會爆出來，所以暖機也算在「載入」裡
        self.warmup()
        t2 = time.perf_counter()
        log.info("辨識模型就緒：%s，載入 %.1f 秒、暖機 %.1f 秒", self.describe(), t1 - t0, t2 - t1)

    def load(self):
        if self.model is not None:
            return                    # 已經載入了（例如從 Groq 切回來，沿用它的備用模型）
        try:
            self._load_and_warmup(self.cfg["model"], self.cfg["device"], self.cfg["compute_type"])
            return
        except Exception as e:
            if self.cfg["device"] != "cuda":
                raise
            log.warning("顯示卡（CUDA）模式失敗，改用 CPU：%s", e)
            self.model = None

        fallback_model = self.cfg["cpu_fallback_model"]
        self.notify(
            "語音輸入：改用 CPU 模式",
            f"顯示卡加速沒有成功，已自動改用 CPU（{fallback_model} 模型），可以照常使用，只是會比較慢。"
            "詳細原因請看 logs 資料夾裡的日誌。",
        )
        self._load_and_warmup(fallback_model, "cpu", self.cfg["cpu_fallback_compute_type"])

    def transcribe(self, audio, prompt_terms=(), warmup=False):
        if self.model is None:
            raise RuntimeError("辨識模型還沒載入完成")
        prompt = build_initial_prompt(self.cfg["initial_prompt"], prompt_terms)
        segments, _info = self.model.transcribe(
            audio,
            language=self.cfg["language"] or None,
            beam_size=int(self.cfg["beam_size"]),
            # 暖機用的是全靜音，開 VAD 會整段被濾掉、等於沒跑到模型
            vad_filter=bool(self.cfg["vad_filter"]) and not warmup,
            initial_prompt=prompt,
            condition_on_previous_text=False,
            # 聽不清楚時 Whisper 會換「溫度」重跑，預設最多重跑 5 次，短句因此偶爾要 2 秒以上；
            # 限制重跑次數，換取穩定的速度
            temperature=list(self.cfg["temperatures"]),
        )
        parts = []
        for seg in segments:
            # 模型自己覺得「這段大概沒有人在講話」而且信心很低，就丟掉
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                continue
            parts.append(seg.text.strip())
        text = "".join(parts).strip()
        if is_hallucination(text, prompt):
            log.info("丟掉疑似幻覺的辨識結果：%s", text)
            return ""
        return text
