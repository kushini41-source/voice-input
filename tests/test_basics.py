"""M1 其他基礎元件的測試：設定檔、熱鍵名稱、重新取樣、辨識提示句、幻覺過濾。"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.asr.whisper_engine import build_initial_prompt, is_hallucination  # noqa: E402
from voice_input.config import DEFAULTS, _merge, load_config  # noqa: E402
from voice_input.hotkey import resolve_key  # noqa: E402
from voice_input.recorder import resample, to_mono  # noqa: E402


def test_config_file_loads():
    cfg = load_config()
    assert cfg["hotkey"]["key"]
    assert cfg["asr"]["whisper"]["model"]


def test_config_merge_keeps_defaults():
    merged = _merge(DEFAULTS, {"hotkey": {"key": "f13"}})
    assert merged["hotkey"]["key"] == "f13"
    assert merged["hotkey"]["mode"] == "toggle"


def test_resolve_key():
    assert resolve_key("capslock") == 0x14
    assert resolve_key("Right_Alt") == 0xA5
    assert resolve_key("f13") == 0x7C
    assert resolve_key("mouse_x2") == 0x06
    assert resolve_key("0x14") == 0x14
    assert resolve_key("不存在的鍵") is None


def test_resample_48k_stereo_to_16k_mono():
    sr = 48000
    t = np.arange(sr) / sr
    tone = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    stereo = np.stack([tone, tone], axis=1)
    out = resample(to_mono(stereo), sr)
    assert out.dtype == np.float32
    assert len(out) == 16000
    # 440Hz 的波形經過重新取樣後，能量應該差不多
    assert abs(np.sqrt(np.mean(out ** 2)) - np.sqrt(0.5)) < 0.02


def test_resample_44100():
    out = resample(np.zeros(44100, dtype=np.float32), 44100)
    assert len(out) == 16000


def test_initial_prompt_with_terms():
    p = build_initial_prompt("基本提示。", ["Claude Code", "faster-whisper"])
    assert p.startswith("基本提示。")
    assert "Claude Code" in p and "faster-whisper" in p
    assert build_initial_prompt("基本提示。", []) == "基本提示。"


def test_initial_prompt_length_limit():
    p = build_initial_prompt("基本提示。", ["很長的專有名詞" * 3] * 100)
    assert len(p) < 220


def test_hallucination_filter():
    assert is_hallucination("謝謝觀看。")
    assert is_hallucination("请不吝点赞订阅转发打赏支持明镜与点点栏目")
    assert not is_hallucination("謝謝觀看這部影片的大家")


def test_prompt_echo_is_hallucination():
    prompt = "以下是台灣人說的繁體中文，請使用台灣常用的詞彙與全形標點符號。常用詞：buy+1。"
    assert is_hallucination("請使用台灣常用的詞彙與全形標點符號。", prompt)
    assert is_hallucination("請使用。", prompt)
    assert not is_hallucination("我今天要去台灣大學", prompt)
    assert not is_hallucination("好", prompt)
