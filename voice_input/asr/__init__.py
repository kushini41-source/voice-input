"""依設定檔建立語音辨識引擎。"""

ENGINES = {
    "groq": "Groq 雲端（whisper large-v3，本機當備用）",
    "whisper": "faster-whisper（本機顯示卡）",
    "capswriter": "CapsWriter（Qwen3-ASR）",
}


def create_engine(cfg, notify=None, name=None, reuse=None):
    """reuse：目前正在用的引擎。切換時可以沿用已經載入的本機 whisper，不用再載一次（省顯示卡記憶體）。"""
    from .whisper_engine import WhisperEngine
    engine = name or cfg["engine"]
    if engine == "whisper":
        local = getattr(reuse, "fallback", None)          # 從 Groq 切回來：直接用它的備用模型
        if isinstance(local, WhisperEngine) and local.model is not None:
            return local
        return WhisperEngine(cfg["whisper"], notify=notify)
    if engine == "groq":
        from .groq_engine import GroqEngine
        local = reuse if isinstance(reuse, WhisperEngine) and reuse.model is not None else None
        return GroqEngine(cfg.get("groq", {}), cfg["whisper"], notify=notify, fallback=local)
    if engine == "capswriter":
        from .capswriter_engine import CapsWriterEngine
        return CapsWriterEngine(cfg.get("capswriter", {}), notify=notify)
    from ..config import ConfigError
    raise ConfigError(f"設定檔裡的辨識引擎「{engine}」看不懂，請改成 groq、whisper 或 capswriter。")
