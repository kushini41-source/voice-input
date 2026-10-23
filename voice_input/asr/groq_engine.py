"""Groq 雲端辨識（M6 ⑥）：把錄音送到 Groq 的伺服器，用同一個 whisper large-v3 模型辨識。

一句通常 0.4 ~ 0.9 秒，不吃自己的顯示卡，兩台電腦（4070 Ti / 2060）速度一樣。
- 金鑰放在程式資料夾的 groq_key.txt（找 gsk_ 開頭的那一串）。
- 送出前先在本機偵測有沒有人在說話：沒聲音就不送（省額度、也避免 whisper 腦補字幕），
  頭尾的空白會剪掉。
- 網路斷線、Groq 逾時、額度用完時，這一句自動改用本機 whisper（本機模型一直載入著當備用），
  之後 60 秒內直接用本機，不會每句都等逾時。
"""

import io
import logging
import os
import re
import time
import wave

import numpy as np

from ..paths import BASE_DIR
from .base import ASREngine
from .whisper_engine import WhisperEngine, build_initial_prompt, is_hallucination

log = logging.getLogger(__name__)

GROQ_KEY_PATH = os.path.join(BASE_DIR, "groq_key.txt")
_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_PAD = int(0.25 * 16000)       # 剪掉頭尾空白時，前後各留 0.25 秒
_RETRY_AFTER_FAILURE = 60      # 雲端失敗後，這麼多秒內直接用本機


def load_groq_key(path=GROQ_KEY_PATH):
    """讀 groq_key.txt 裡 gsk_ 開頭的金鑰（只讀，不修改檔案）；找不到回傳空字串。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                m = re.search(r"gsk_\S+", line)
                if m and not line.lstrip().startswith("#"):
                    return m.group(0)
    except OSError:
        pass
    return ""


def to_wav_bytes(audio):
    """16kHz 單聲道 float32 → WAV 檔內容（在記憶體裡，不寫暫存檔）。"""
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def speech_span(audio):
    """用 Silero VAD 找出有人在說話的範圍，回傳剪好的音訊；完全沒人說話回傳 None。"""
    from faster_whisper.vad import get_speech_timestamps
    stamps = get_speech_timestamps(audio)
    if not stamps:
        return None
    start = max(0, stamps[0]["start"] - _PAD)
    end = min(len(audio), stamps[-1]["end"] + _PAD)
    return audio[start:end]


class GroqEngine(ASREngine):
    name = "groq"

    def __init__(self, cfg, whisper_cfg, notify=None, fallback=None, key_path=GROQ_KEY_PATH):
        self.cfg = cfg
        self.whisper_cfg = whisper_cfg
        self.notify = notify or (lambda title, msg: None)
        self.model_name = cfg.get("model", "whisper-large-v3")
        self.timeout = float(cfg.get("timeout_seconds", 5))
        self.key_path = key_path
        self.key = ""
        # 本機 whisper 當備用（切換引擎時可以沿用已經載入的那一個，不用再載一次）
        self.fallback = fallback if fallback is not None else WhisperEngine(whisper_cfg, notify=notify)
        self._client = None
        self._cloud_off_until = 0.0
        self._warned_key = False

    def describe(self):
        return f"Groq 雲端 {self.model_name}（備用：{self.fallback.describe()}）"

    def load(self):
        self.key = load_groq_key(self.key_path)
        if not self.key:
            raise RuntimeError("groq_key.txt 裡沒有 Groq 的金鑰（gsk_ 開頭）")
        import httpx2
        self._client = httpx2.Client(timeout=httpx2.Timeout(self.timeout, connect=2.0))
        if getattr(self.fallback, "model", None) is None:
            self.fallback.load()      # 本機模型也載好，斷網時馬上可以用
        log.info("辨識引擎就緒：%s", self.describe())

    def warmup(self):
        pass                          # 雲端不用暖機；本機備用在 load() 時已經暖過

    def close(self, keep_fallback=False):
        """keep_fallback=True：只關雲端連線，本機 whisper 留著（切回本機引擎時會直接沿用它）。"""
        if self._client is not None:
            self._client.close()
            self._client = None
        if not keep_fallback:
            self.fallback.close()

    def transcribe(self, audio, prompt_terms=(), warmup=False):
        clip = speech_span(audio)
        if clip is None:
            return ""                 # 沒有人在說話：不送雲端
        if time.monotonic() < self._cloud_off_until:
            return self.fallback.transcribe(audio, prompt_terms)
        prompt = build_initial_prompt(self.whisper_cfg["initial_prompt"], prompt_terms)
        try:
            text = self._cloud(clip, prompt)
        except Exception as e:
            self._cloud_off_until = time.monotonic() + _RETRY_AFTER_FAILURE
            log.warning("Groq 雲端辨識失敗，這句改用本機 whisper（%d 秒內都先用本機）：%s", _RETRY_AFTER_FAILURE, e)
            return self.fallback.transcribe(audio, prompt_terms)
        if is_hallucination(text, prompt):
            log.info("丟掉疑似幻覺的辨識結果：%s", text)
            return ""
        return text

    def _cloud(self, clip, prompt):
        r = self._client.post(
            _URL,
            headers={"Authorization": f"Bearer {self.key}"},
            files={"file": ("audio.wav", to_wav_bytes(clip), "audio/wav")},
            data={"model": self.model_name, "language": self.whisper_cfg.get("language") or "zh",
                  "prompt": prompt, "response_format": "verbose_json", "temperature": "0"},
        )
        if r.status_code == 401 and not self._warned_key:
            self._warned_key = True
            self.notify("語音輸入：Groq 金鑰無效", "groq_key.txt 裡的金鑰不能用，先改用本機辨識。請到 Groq 重新建立金鑰。")
        if r.status_code == 429:
            raise RuntimeError("Groq 額度用完了（今天或這一小時）")
        r.raise_for_status()
        data = r.json()
        segments = data.get("segments")
        if not segments:
            return (data.get("text") or "").strip()
        # 跟本機一樣：模型自己覺得「這段大概沒人在講話」而且信心很低的，丟掉
        parts = [s.get("text", "").strip() for s in segments
                 if not (s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0)]
        return "".join(parts).strip()
