"""讀取 config.yaml。檔案裡沒寫到的項目一律用這裡的預設值，所以設定檔少寫幾行也不會壞。"""

import copy
import logging

import yaml

from .paths import CONFIG_PATH

log = logging.getLogger(__name__)

DEFAULTS = {
    "hotkey": {
        "key": "capslock",
        "mode": "toggle",
        "max_record_seconds": 120,
        "min_hold_seconds": 0.3,
        "poll_interval_ms": 15,
        "esc_cancels": True,
    },
    "audio": {
        "device": "",
        "startup_delay_seconds": 2,
        "min_duration_seconds": 0.3,
        "beep": True,
    },
    "asr": {
        "engine": "whisper",
        "whisper": {
            "model": "large-v3",
            "device": "cuda",
            "compute_type": "float16",
            "beam_size": 5,
            "temperatures": [0.0, 0.2, 0.4],
            "vad_filter": True,
            "language": "zh",
            "initial_prompt": "以下是台灣人說的繁體中文，夾雜一些英文技術名詞，請使用台灣常用的詞彙與全形標點符號。",
            "cpu_fallback_model": "medium",
            "cpu_fallback_compute_type": "int8",
        },
        "capswriter": {
            "host": "127.0.0.1",
            "port": 6016,
            "language": "auto",
            "server_exe": "",
            "auto_start_server": True,
            "server_start_timeout": 90,
            "timeout_seconds": 15,
        },
        "groq": {
            "model": "whisper-large-v3",
            "timeout_seconds": 5,
        },
        "fallback_to_whisper": True,
    },
    "dictionary": {
        "fuzzy_match": True,
        "fuzzy_threshold": 0.85,
    },
    "corrector": {
        "enabled": True,
        "backend": "anthropic",
        "model": "claude-haiku-5-5",
        "effort": "low",
        "timeout_seconds": 3.0,
        "max_length_ratio": 1.5,
        "remove_particles": True,
        "history_items": 3,
        "history_seconds": 300,
        "ollama": {
            "url": "http://127.0.0.1:11434",
            "model": "qwen2.5:7b",
        },
    },
    "command": {
        "timeout_seconds": 20,
        "model": "",
        "effort": "low",
        "thinking": "off",
    },
    "mute": {
        "enabled": True,
        "mode": "mute",
        "lower_to_percent": 20,
        "skip_apps": ["ms-teams.exe", "Teams.exe", "Zoom.exe", "CptHost.exe", "Webex.exe", "CiscoCollabHost.exe"],
    },
    "translate": {
        "languages": ["英文", "日文", "韓文"],
        "target": "英文",
        "timeout_seconds": 8,
    },
    "style": {
        "enabled": True,
        "update_every": 30,
        "min_samples": 20,
        "samples_per_update": 120,
        "max_samples": 300,
        "timeout_seconds": 20,
    },
    "learning": {
        "enabled": True,
        "ai_min_count": 2,
        "ai_auto_count": 3,
        "auto_from_edits": True,
        "watch_edits": True,
        "watch_seconds": 90,
        "poll_seconds": 0.7,
        "skip_apps": ["WindowsTerminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
                      "OpenConsole.exe", "wezterm-gui.exe", "mintty.exe", "alacritty.exe", "mstsc.exe"],
    },
    "scenes": [],
    "converter": {
        "opencc_config": "s2twp",
        "space_between_chinese_english": True,
        "protect": [],
        "fixes": {},
    },
    "output": {
        "method": "paste",
        "type_apps": [],
        "restore_clipboard_delay": 0.3,
        "wait_modifiers_timeout": 1.5,
    },
    "ui": {
        "overlay": True,
    },
    "debug": {
        "save_history": False,
    },
}


def _merge(base, override):
    """把 override 疊到 base 上；字典往下遞迴，其他型別直接覆蓋。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigError(Exception):
    """設定檔格式錯誤，訊息會直接顯示給使用者看。"""


def load_config(path=CONFIG_PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("找不到設定檔 %s，全部使用預設值", path)
        data = {}
    except yaml.YAMLError as e:
        raise ConfigError(f"設定檔 config.yaml 格式有誤，請檢查剛剛改過的地方：\n{e}") from e
    if not isinstance(data, dict):
        raise ConfigError("設定檔 config.yaml 的內容不是正確的格式。")
    return _merge(DEFAULTS, data)
