"""Groq 雲端辨識：用假的雲端和假的本機引擎測試，不會真的連網路。"""

import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.asr import create_engine  # noqa: E402
from voice_input.asr import groq_engine as ge  # noqa: E402
from voice_input.asr.whisper_engine import WhisperEngine  # noqa: E402
from voice_input.config import load_config  # noqa: E402

WCFG = load_config()["asr"]["whisper"]


class FakeLocal:
    def __init__(self):
        self.calls = 0
        self.model = object()
        self.closed = False

    def transcribe(self, audio, prompt_terms=(), warmup=False):
        self.calls += 1
        return "本機的結果"

    def describe(self):
        return "假的本機"

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.sent = response, error, []

    def post(self, url, **kw):
        self.sent.append(kw)
        if self.error:
            raise self.error
        return self.response

    def close(self):
        pass


SPEECH = np.zeros(32000, np.float32)


def _engine(monkeypatch, client, speech=True):
    monkeypatch.setattr(ge, "speech_span", lambda a: a if speech else None)
    e = ge.GroqEngine({}, WCFG, fallback=FakeLocal())
    e.key, e._client = "gsk_test", client
    return e


def test_cloud_result_used_and_low_confidence_segments_dropped(monkeypatch):
    client = FakeClient(FakeResponse(200, {"segments": [
        {"text": "給我一個桌面版的連結", "no_speech_prob": 0.01, "avg_logprob": -0.2},
        {"text": "謝謝收看", "no_speech_prob": 0.9, "avg_logprob": -1.5},
    ]}))
    e = _engine(monkeypatch, client)
    assert e.transcribe(SPEECH, ["buy+1"]) == "給我一個桌面版的連結"
    assert e.fallback.calls == 0
    sent = client.sent[0]
    assert sent["data"]["model"] == "whisper-large-v3" and "buy+1" in sent["data"]["prompt"]
    assert sent["files"]["file"][1][:4] == b"RIFF", "要送 WAV 檔"


def test_network_error_falls_back_and_skips_cloud_for_a_while(monkeypatch):
    client = FakeClient(error=OSError("網路斷線"))
    e = _engine(monkeypatch, client)
    assert e.transcribe(SPEECH) == "本機的結果"
    assert e.transcribe(SPEECH) == "本機的結果"
    assert len(client.sent) == 1, "失敗後一段時間內不要每句都去等雲端逾時"


def test_quota_used_up_falls_back(monkeypatch):
    e = _engine(monkeypatch, FakeClient(FakeResponse(429)))
    assert e.transcribe(SPEECH) == "本機的結果"


def test_bad_key_notifies_once(monkeypatch):
    notes = []
    e = _engine(monkeypatch, FakeClient(FakeResponse(401)))
    e.notify = lambda t, m: notes.append(t)
    assert e.transcribe(SPEECH) == "本機的結果"
    e._cloud_off_until = 0
    e.transcribe(SPEECH)
    assert len(notes) == 1


def test_silence_is_not_sent(monkeypatch):
    client = FakeClient(FakeResponse(200, {"text": "謝謝觀看"}))
    e = _engine(monkeypatch, client, speech=False)
    assert e.transcribe(SPEECH) == ""
    assert client.sent == [] and e.fallback.calls == 0


def test_prompt_echo_is_dropped(monkeypatch):
    client = FakeClient(FakeResponse(200, {"text": "請使用台灣常用的詞彙與全形標點符號。"}))
    assert _engine(monkeypatch, client).transcribe(SPEECH) == ""


def test_key_file_with_placeholder_line():
    path = os.path.join(tempfile.mkdtemp(), "groq_key.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 說明 gsk_不是這個\n這裡貼金鑰\ngsk_abc123\n")
    assert ge.load_groq_key(path) == "gsk_abc123"
    assert ge.load_groq_key(path + ".none") == ""


def test_wav_bytes_and_real_vad():
    assert ge.to_wav_bytes(np.zeros(16000, np.float32))[:4] == b"RIFF"
    assert ge.speech_span(np.zeros(32000, np.float32)) is None, "全靜音要判斷成沒人說話"


def test_switching_reuses_loaded_local_model():
    cfg = load_config()["asr"]
    local = WhisperEngine(cfg["whisper"])
    local.model = object()                              # 假裝已經載入
    groq = create_engine(cfg, name="groq", reuse=local)
    assert groq.fallback is local, "切到雲端時沿用已經載入的本機模型"
    assert create_engine(cfg, name="whisper", reuse=groq) is local, "切回本機時直接用備用模型"
    fresh = create_engine(cfg, name="groq", reuse=None)
    assert isinstance(fresh.fallback, WhisperEngine) and fresh.fallback is not local
