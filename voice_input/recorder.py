"""錄音：麥克風用它原本的格式收音（例如 MU5000 是 48kHz 或 44.1kHz 雙聲道），
結束後再轉成辨識引擎要的 16kHz 單聲道。錄音全程存在記憶體，不寫暫存檔。

麥克風只在啟動時開一次、之後一直開著，錄音只是切換「要不要收進來」的旗標。
頻繁開關麥克風容易跟 Nahimic 之類的音效服務打架，這是舊版就驗證過的做法。
"""

import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

TARGET_RATE = 16000


def to_mono(audio):
    if audio.ndim == 2:
        return audio.mean(axis=1)
    return audio


def resample(audio, src_rate, dst_rate=TARGET_RATE):
    """用 FFT 重新取樣，會自動濾掉高於新取樣率一半的頻率，不會產生疊頻雜音。"""
    if src_rate == dst_rate or len(audio) == 0:
        return audio.astype(np.float32)
    n_out = int(round(len(audio) * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    spectrum = np.fft.rfft(audio)
    keep = n_out // 2 + 1
    if keep <= len(spectrum):
        spectrum = spectrum[:keep]
    else:
        spectrum = np.pad(spectrum, (0, keep - len(spectrum)))
    out = np.fft.irfft(spectrum, n_out) * (n_out / len(audio))
    return out.astype(np.float32)


def _find_device(name):
    """依名稱的一部分找錄音裝置；找不到就用預設裝置（回傳 None）。"""
    if not name:
        return None
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and name.lower() in dev["name"].lower():
            return index
    log.warning("找不到名稱含有「%s」的麥克風，改用 Windows 預設錄音裝置", name)
    return None


class Recorder:
    def __init__(self, cfg):
        self.device_name = cfg.get("device") or ""
        self._stream = None
        self._lock = threading.Lock()
        self._chunks = []
        self._recording = False
        self.sample_rate = None
        self.channels = None
        self.level = 0.0   # 目前音量（0~1），之後浮動視窗的音量條會用到

    @property
    def ready(self):
        return self._stream is not None and self._stream.active

    def open(self):
        """開啟麥克風；失敗會丟出例外，由呼叫端通知使用者。"""
        self.close()
        device = _find_device(self.device_name)
        info = sd.query_devices(device, kind="input")
        self.sample_rate = int(info["default_samplerate"])
        self.channels = max(1, min(2, int(info["max_input_channels"])))
        stream = sd.InputStream(
            device=device,
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            callback=self._callback,
        )
        stream.start()
        self._stream = stream
        log.info("麥克風已就緒：%s（%d Hz，%d 聲道）", info["name"], self.sample_rate, self.channels)

    def close(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _callback(self, indata, frames, time_info, status):
        if not self._recording:
            return
        chunk = indata.copy()
        with self._lock:
            if self._recording:
                self._chunks.append(chunk)
        self.level = float(min(1.0, np.sqrt(np.mean(chunk ** 2)) * 8))

    def start(self):
        if not self.ready:
            # 麥克風可能被拔掉又插回來，試著重新開一次
            self.open()
        with self._lock:
            self._chunks = []
            self._recording = True

    def stop(self):
        """結束錄音，回傳 16kHz 單聲道 float32 的音訊（可能是空陣列）。"""
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        self.level = 0.0
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = to_mono(np.concatenate(chunks, axis=0))
        return resample(audio, self.sample_rate)

    def discard(self):
        with self._lock:
            self._recording = False
            self._chunks = []
        self.level = 0.0
