"""語音辨識引擎的共同介面，之後可以換成 CapsWriter 或其他引擎。"""

import numpy as np


class ASREngine:
    name = "base"

    def load(self):
        """載入模型。失敗請丟出例外。"""
        raise NotImplementedError

    def warmup(self):
        """用一秒靜音先跑一次，讓第一次真正辨識不會特別慢。"""
        self.transcribe(np.zeros(16000, dtype=np.float32), warmup=True)

    def transcribe(self, audio, prompt_terms=(), warmup=False):
        """audio：16kHz 單聲道 float32。prompt_terms：要提示給引擎的專有名詞。回傳辨識文字。"""
        raise NotImplementedError

    def close(self):
        """不再使用這個引擎時呼叫，釋放顯示卡記憶體或網路連線。"""

    def describe(self):
        """給日誌和系統匣看的一行說明，例如「whisper large-v3（cuda / float16）」。"""
        return self.name
