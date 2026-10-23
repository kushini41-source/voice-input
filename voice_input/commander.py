"""指令模式：用語音對 AI 下指令。

依「有沒有選取文字」和「指令的意思」，AI 決定要做哪一件事：
  replace  有選取文字、指令是要修改它（改短、改正式、翻譯、修錯字…）→ 直接取代選取的文字
  answer   問問題（解釋、摘要、這是什麼意思…）→ 在小卡片顯示回答，原文不動
  insert   沒選取文字、要 AI 寫一段可以直接打出來的內容（請假信、回覆…）→ 打在游標位置

AI 回傳固定格式的 JSON（{"action": ..., "text": ...}），程式照著做，不靠猜。
"""

import concurrent.futures
import logging
import time

log = logging.getLogger(__name__)

ACTIONS = ("replace", "answer", "insert")

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "text": {"type": "string"},
    },
    "required": ["action", "text"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
你是語音輸入法的「指令助理」。使用者用說的對你下指令，語音辨識把指令轉成文字（可能有同音錯字，請依意思理解）。
有時使用者會先選取一段文字，指令就是針對那段文字。

請判斷要做哪一件事，並照格式回傳 action 和 text：

1. action = "replace"：有選取文字，而且指令是要「修改」它
   （例如：改短一點、改長一點、改正式一點、改口語一點、修正錯字、翻成英文、改成條列、換個說法、加上禮貌用語）。
   text = 修改後的完整文字，會直接取代原本選取的文字。只放要替換進去的內容本身，不要加任何說明或引號。
   保留原文的格式（換行、條列）和原文中的專有名詞、英文術語、網址、數字，除非指令要求改它們。

2. action = "answer"：指令是在「問問題」，或要你說明、分析、摘要，而不是要改原文
   （例如：這段是什麼意思、幫我摘要、這樣寫對嗎、這個字怎麼念、解釋一下這個錯誤）。
   text = 你的回答，簡潔清楚，可以用短條列。這個回答只會顯示給使用者看，不會改到任何文字。

3. action = "insert"：沒有選取文字，而且指令是要你「寫一段內容」讓使用者直接用
   （例如：幫我寫一封請假信、回覆說我明天下午到、寫一句生日祝福）。
   text = 寫好的內容本身，可以直接打在游標位置。不要加「好的」「以下是」這類開頭或任何說明。

判斷原則：
- 有選取文字但不確定是要改還是要問 → 用 answer（不要冒險改掉使用者的原文）。
- 沒有選取文字、指令是問題 → 用 answer。
- 沒有選取文字時不能用 replace。

語言：一律用台灣繁體中文與台灣用語，除非指令明確要求其他語言（例如「翻成英文」「用日文寫」）。
依「場景」調整語氣：技術場景保留英文術語；聊天場景口語、不加句號；正式場景用書面語。"""


class CommandResult:
    def __init__(self, action, text, ok=True, reason="", seconds=0.0):
        self.action = action
        self.text = text
        self.ok = ok
        self.reason = reason
        self.seconds = seconds


def build_user_message(instruction, selection, scene, style_rules=()):
    parts = [f"<場景>{scene.get('name', '一般模式')}</場景>"]
    if style_rules:
        lines = "\n".join(f"- {r}" for r in style_rules)
        parts.append("<使用者的用字習慣>（幫他寫或改寫要給別人看的文字時參考；指令要求正式、或是回答問題時，"
                     f"以指令和場景為準，不要硬套）\n{lines}\n</使用者的用字習慣>")
    if selection.strip():
        parts.append(f"<選取的文字>\n{selection}\n</選取的文字>")
    else:
        parts.append("<選取的文字>（沒有選取任何文字）</選取的文字>")
    parts.append(f"<語音指令>{instruction}</語音指令>")
    return "\n\n".join(parts)


def validate(data, has_selection):
    """檢查 AI 回傳的內容能不能照做；不行就回傳原因。"""
    if not isinstance(data, dict):
        return "AI 回傳的格式不對"
    action, text = data.get("action"), data.get("text")
    if action not in ACTIONS:
        return f"AI 回傳了看不懂的動作「{action}」"
    if not isinstance(text, str) or not text.strip():
        return "AI 沒有回傳內容"
    if action == "replace" and not has_selection:
        return "沒有選取文字卻要取代"
    return None


class Commander:
    def __init__(self, cfg, backend_getter):
        """backend_getter：回傳目前的 AI 後端（跟 AI 校正共用同一個，切換來源時一起換）。

        cfg 的 model 有填（例如 claude-sonnet-5-5）時，用 Claude 的期間改用這個模型（Ollama 時照舊）。
        """
        self.timeout = float(cfg.get("timeout_seconds", 20))
        self.model = str(cfg.get("model") or "").strip()
        self.effort = str(cfg.get("effort") or "low")
        self.thinking = str(cfg.get("thinking") or "off")
        self.backend_getter = backend_getter
        self._own_backend = None
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="command")

    def _backend(self):
        shared = self.backend_getter()
        if not self.model or getattr(shared, "name", "") != "anthropic":
            return shared
        if self._own_backend is None:
            from .corrector import AnthropicBackend, load_api_key
            self._own_backend = AnthropicBackend(self.model, load_api_key(), self.effort, self.thinking)
        return self._own_backend

    def run(self, instruction, selection, scene, is_cancelled=lambda: False, style_rules=()):
        backend = self._backend()
        self.last_model = getattr(backend, "model", "") or getattr(backend, "name", "")   # 給日誌看
        if backend is None or not hasattr(backend, "complete_json"):
            return CommandResult("", "", ok=False, reason="AI 無法使用（請確認 api_key.txt 或 AI 校正來源）")
        user = build_user_message(instruction, selection, scene, style_rules)
        t0 = time.perf_counter()
        future = self._pool.submit(backend.complete_json, SYSTEM_PROMPT, user, SCHEMA, self.timeout)
        deadline = t0 + self.timeout
        while True:
            try:
                data = future.result(timeout=0.05)
                break
            except concurrent.futures.TimeoutError:
                if is_cancelled():
                    return CommandResult("", "", ok=False, reason="使用者按 Esc 取消", seconds=time.perf_counter() - t0)
                if time.perf_counter() > deadline:
                    return CommandResult("", "", ok=False, reason=f"AI 超過 {self.timeout:g} 秒沒回應",
                                         seconds=time.perf_counter() - t0)
            except Exception as e:
                return CommandResult("", "", ok=False, reason=f"AI 呼叫失敗：{e}", seconds=time.perf_counter() - t0)
        seconds = time.perf_counter() - t0
        problem = validate(data, bool(selection.strip()))
        if problem:
            log.warning("指令模式：%s（%s）", problem, data)
            return CommandResult("", "", ok=False, reason=problem, seconds=seconds)
        return CommandResult(data["action"], data["text"].strip("\n"), seconds=seconds)
