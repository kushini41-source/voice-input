"""詞庫自動學習（M6 ②）：找出常被聽錯的詞，問使用者要不要加進詞庫。

兩種來源：
  1. AI 校正：同一個「聽錯 → 改對」AI 改了好幾次（預設 2 次），代表辨識引擎老是聽錯這個詞。
  2. 使用者自己改：打出來之後，使用者在輸入框裡把某個詞改掉了（由 field_watch.py 偵測）。

這裡只負責「比對兩段文字改了哪些詞」和「記錄次數」，不碰視窗介面。
次數紀錄存在 logs\\learning.json（只存詞和一句例句）。
"""

import difflib
import json
import logging
import os
import re
import threading
import time

from .paths import LOG_DIR

log = logging.getLogger(__name__)

LEARNING_PATH = os.path.join(LOG_DIR, "learning.json")

# 英文單字（含 buy+1、C#、v2.5 這類）算一個單位，空白算一個單位，其他每個字元各算一個
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#._'-]*|\s+|.", re.S)
_CJK = "㐀-鿿豈-﫿"
_CJK_CHAR = re.compile(f"[{_CJK}]")
# 標點和空白：只改這些不算「聽錯」
_PUNCT_SPACE = re.compile(r"[\s\W_]+")
# 中文和英文之間的空白是簡繁轉換自動加的，辨識引擎原本的輸出沒有，詞庫的詞也不能有
_CJK_LATIN_SPACE = re.compile(f"(?<=[{_CJK}])\\s+(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])\\s+(?=[{_CJK}])")
_NUMBERISH = re.compile(r"^[0-9０-９零〇一二兩三四五六七八九十百千萬億點.:：/年月日號時分秒半%％]+$")
_FILLERS = re.compile("呃|嗯|欸|啊|喔|那個|就是說")

# 常用的虛詞：AI 改寫句子時常把它們換掉，不是「聽錯」，不要學
_STOPWORDS = {"就是", "這個", "那個", "然後", "所以", "可是", "因為", "但是", "還是", "如果", "我們", "你們", "他們",
              "是不是", "一個", "什麼", "這樣", "那樣", "現在", "已經", "這邊", "那邊", "其實", "應該", "可以"}
ALIKE_THRESHOLD = 0.6   # 兩個中文詞唸起來多像才算「聽錯」（0～1）

MIN_WRONG_CHARS = 2     # 「聽錯的字」至少要有幾個字，只有一個字的替換太容易誤換
MAX_PAIR_CHARS = 12     # 改的範圍比這個長，算是改寫句子，不是改錯字
MIN_SIMILARITY = 0.5    # 改之前和改之後要有多像，太不像就是整句重寫
_MAX_RECORDS = 500


def normalize_field_text(text):
    """輸入框讀出來的文字和打出去的字，用同一套規則整理後再比對：
    換行統一成一個 \\n（Claude、Chrome 這類編輯器會把段落讀成不同的換行）、
    不換行空白當一般空白、連續空白只留一個、換行旁邊的空白拿掉。"""
    text = (text.replace("\r\n", "\n").replace("\r", "\n")
            .replace("\xa0", " ").replace("​", "").replace("﻿", ""))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n[ \n]*", "\n", text)
    return text


def locate(text, typed):
    """typed 在 text 裡的位置；找不到、或出現不只一次（分不出是哪一個）都回傳 -1。"""
    if not typed or text.count(typed) != 1:
        return -1
    return text.find(typed)


def edited_region(baseline, start, typed, current):
    """剛打完字時輸入框的內容是 baseline（typed 在 start 的位置），現在變成 current。

    typed 前面和後面的文字都沒變，就回傳 typed 那一段現在變成什麼；
    前後文變了（訊息送出被清空、改了別的地方、切到別的檔案）就回傳 None。
    """
    prefix, suffix = baseline[:start], baseline[start + len(typed):]
    if (len(current) < len(prefix) + len(suffix)
            or not current.startswith(prefix) or not current.endswith(suffix)):
        return None
    return current[len(prefix):len(current) - len(suffix)]


def _align_similarity(a, b):
    """兩段中文（可以不等長）唸起來有多像：逐字找最像的對齊方式，0～1。"""
    from .dictionary import char_similarity
    n, m = len(a), len(b)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = max(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1] + char_similarity(a[i - 1], b[j - 1]))
    return dp[n][m] / max(n, m)


def sounds_alike(wrong, right):
    """「聽錯」的兩個詞通常唸起來很像。回傳 True（像）、False（不像：大概是改寫或改主意）、
    None（中英混在一起，例如「拜加溫→buy+1」，判斷不了）。"""
    w, r = _meaningful(wrong), _meaningful(right)
    has_cjk = lambda s: bool(_CJK_CHAR.search(s))           # noqa: E731
    has_latin = lambda s: bool(re.search(r"[A-Za-z]", s))   # noqa: E731
    if has_cjk(w) and has_cjk(r) and not has_latin(w) and not has_latin(r):
        return _align_similarity(w, r) >= ALIKE_THRESHOLD
    if not has_cjk(w) and not has_cjk(r):
        if w.lower() == r.lower():
            return False                  # 只改大小寫：不是聽錯
        return difflib.SequenceMatcher(None, w.lower(), r.lower()).ratio() >= 0.5
    return None


def is_term(word):
    """看起來像專有名詞（有英文字母，例如 ChatGPT、Claude Code）：加進詞庫時也一起加進專有名詞。"""
    return bool(re.search(r"[A-Za-z]", word or ""))


def _meaningful(s):
    return _PUNCT_SPACE.sub("", s)


def _is_cjk_token(tok):
    return len(tok) == 1 and bool(_CJK_CHAR.match(tok))


def _is_word_token(tok):
    return tok[:1].isascii() and tok[:1].isalnum()


def _widen(a, b, i1, i2, j1, j2):
    """改的範圍太短時，把前後「沒改的字」一起帶上，讓詞庫的詞比較明確。

    - 只改了一個中文字（「深」→「生」）：帶上旁邊的中文字（「深圖」→「生圖」），先右後左
    - 只改了一個英文字（「Cloud」→「Claude」）：帶上旁邊的英文字（「Cloud Code」→「Claude Code」）
    """
    def same(x, y):
        return 0 <= x < len(a) and 0 <= y < len(b) and a[x] == b[y]

    if i2 - i1 == 1 and j2 - j1 == 1 and _is_word_token(a[i1]) and _is_word_token(b[j1]):
        if same(i2, j2) and same(i2 + 1, j2 + 1) and a[i2].isspace() and _is_word_token(a[i2 + 1]):
            return i1, i2 + 2, j1, j2 + 2
        if same(i1 - 1, j1 - 1) and same(i1 - 2, j1 - 2) and a[i1 - 1].isspace() and _is_word_token(a[i1 - 2]):
            return i1 - 2, i2, j1 - 2, j2
        return i1, i2, j1, j2

    for _ in range(2):
        if len(_meaningful("".join(a[i1:i2]))) >= MIN_WRONG_CHARS:
            break
        if same(i2, j2) and _is_cjk_token(a[i2]):
            i2, j2 = i2 + 1, j2 + 1
        elif same(i1 - 1, j1 - 1) and _is_cjk_token(a[i1 - 1]):
            i1, j1 = i1 - 1, j1 - 1
        else:
            break
    return i1, i2, j1, j2


def _keep_pair(wrong, right):
    mw, mr = _meaningful(wrong), _meaningful(right)
    if not mw or not mr or mw == mr:
        return False                      # 只是刪字、加字、改標點或空白
    if len(mw) < MIN_WRONG_CHARS:
        return False
    if len(wrong) > MAX_PAIR_CHARS or len(right) > MAX_PAIR_CHARS:
        return False
    if max(len(mw), len(mr)) > 3 * min(len(mw), len(mr)):
        return False                      # 長度差太多，是補字或刪句，不是聽錯
    if _NUMBERISH.match(mw) and _NUMBERISH.match(mr):
        return False                      # 「三點」→「3點」這種數字寫法
    if _FILLERS.sub("", mw) == mr:
        return False                      # 刪掉贅字
    return True


def extract_pairs(before, after):
    """比對兩段文字，找出「把哪個詞換成哪個詞」：[(原本的, 改成的), ...]。

    只算「替換」：單純刪字（贅字）、加字、只改標點空白、數字寫法都不算；
    整句幾乎重寫的也不算。
    """
    # 先拿掉中英之間自動加的空白，不然對齊時空白會亂配對
    a = _TOKEN.findall(_CJK_LATIN_SPACE.sub("", before or ""))
    b = _TOKEN.findall(_CJK_LATIN_SPACE.sub("", after or ""))
    if not a or not b:
        return []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < MIN_SIMILARITY:
        return []
    pairs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        # 頭尾的標點、空白另外算（例如「ChangeGPT。」→「ChatGPT」：改詞＋拿掉句號，只看改詞的部分）
        i1, i2 = _trim(a, i1, i2)
        j1, j2 = _trim(b, j1, j2)
        if i1 == i2 or j1 == j2:
            continue                      # 只是改標點，或原本只有標點的地方補了字
        i1, i2, j1, j2 = _widen(a, b, i1, i2, j1, j2)
        if not all(_is_plain(t) for t in a[i1:i2] + b[j1:j2]):
            continue                      # 跨過標點、換行，是改句子不是改詞
        wrong, right = "".join(a[i1:i2]).strip(), "".join(b[j1:j2]).strip()
        if _keep_pair(wrong, right):
            pairs.append((wrong, right))
    return list(dict.fromkeys(pairs))


def _is_plain(tok):
    """中文字、英文字或一般空白（不是標點、不是換行）。"""
    return _is_cjk_token(tok) or _is_word_token(tok) or (tok.isspace() and "\n" not in tok)


def _trim(tokens, start, end):
    """去掉範圍頭尾的標點和空白。"""
    while start < end and not (_is_cjk_token(tokens[start]) or _is_word_token(tokens[start])):
        start += 1
    while end > start and not (_is_cjk_token(tokens[end - 1]) or _is_word_token(tokens[end - 1])):
        end -= 1
    return start, end


class Suggestion:
    """一筆詞庫學習。source："ai"（AI 改了好幾次）或 "edit"（使用者自己改的）。
    auto=True：有把握，直接加進詞庫（事後可以復原）；False：跳卡片問使用者。"""

    def __init__(self, wrong, right, source, count=1, before="", after="", auto=False):
        self.wrong = wrong
        self.right = right
        self.source = source
        self.count = count
        self.before = before      # 例句：改之前
        self.after = after        # 例句：改之後
        self.auto = auto

    @property
    def key(self):
        return (self.wrong, self.right)

    def __repr__(self):
        return f"Suggestion({self.wrong!r} → {self.right!r}, {self.source}, {self.count})"


class Learner:
    def __init__(self, cfg, path=LEARNING_PATH):
        self.enabled = bool(cfg.get("enabled", True))
        self.ai_min_count = max(1, int(cfg.get("ai_min_count", 2)))
        self.ai_auto_count = max(self.ai_min_count, int(cfg.get("ai_auto_count", 3)))
        self.auto_from_edits = bool(cfg.get("auto_from_edits", True))
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    # ── 存檔 ──

    def _load(self):
        data = {"ai": {}, "declined": [], "auto": []}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded.get("ai"), dict):
                data["ai"] = {k: v for k, v in loaded["ai"].items() if isinstance(v, dict)}
            if isinstance(loaded.get("declined"), list):
                data["declined"] = [str(k) for k in loaded["declined"]]
            if isinstance(loaded.get("auto"), list):
                data["auto"] = [a for a in loaded["auto"] if isinstance(a, dict)]
        except (OSError, ValueError, AttributeError):
            pass
        return data

    def _save(self):
        ai = self._data["ai"]
        if len(ai) > _MAX_RECORDS:
            # 只出現過一次、很久以前的先丟掉
            for key in sorted(ai, key=lambda k: (ai[k].get("count", 0), ai[k].get("time", "")))[:len(ai) - _MAX_RECORDS]:
                del ai[key]
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("存詞庫學習紀錄失敗：%s", e)

    @staticmethod
    def _key(wrong, right):
        return f"{wrong}\t{right}"

    def _skip(self, wrong, right, words):
        """詞庫已經有了、或使用者說過不要（或復原過）的，就不要再問。"""
        if words is not None and wrong in words.replacements:
            return True
        return self._key(wrong, right) in self._data["declined"]

    # ── 學習 ──

    def observe_ai(self, before, after, words=None):
        """AI 校正前後比對（兩邊都要是簡繁轉換後的文字）。回傳這次要處理的學習：
        唸起來像、AI 改了 ai_auto_count 次 → 自動加；唸起來像或判斷不了、改了 ai_min_count 次 → 問使用者；
        唸起來不像（AI 在改寫句子）或是常用虛詞 → 不學也不問。"""
        if not self.enabled:
            return []
        out = []
        with self._lock:
            changed = False
            for wrong, right in extract_pairs(before, after):
                if self._skip(wrong, right, words) or _meaningful(wrong) in _STOPWORDS:
                    continue
                alike = sounds_alike(wrong, right)
                if alike is False:
                    continue
                item = self._data["ai"].setdefault(self._key(wrong, right), {"count": 0})
                item["count"] += 1
                item["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                item["before"], item["after"] = before[:120], after[:120]
                changed = True
                log.info("詞庫學習：AI 把「%s」改成「%s」（第 %d 次）", wrong, right, item["count"])
                auto = bool(alike) and item["count"] >= self.ai_auto_count
                if auto or item["count"] >= self.ai_min_count:
                    out.append(Suggestion(wrong, right, "ai", item["count"], before, after, auto=auto))
            if changed:
                self._save()
        return out

    def observe_edit(self, typed, edited, words=None):
        """使用者把打出來的 typed 改成了 edited。唸起來像（聽錯）或中英混合 → 自動學；
        唸起來不像（可能是改主意，例如「明天→後天」）→ 跳卡片問。"""
        if not self.enabled:
            return []
        out = []
        with self._lock:
            for wrong, right in extract_pairs(typed, edited):
                if self._skip(wrong, right, words):
                    continue
                auto = self.auto_from_edits and sounds_alike(wrong, right) is not False
                log.info("詞庫學習：使用者把「%s」改成「%s」（%s）", wrong, right, "自動學" if auto else "問使用者")
                out.append(Suggestion(wrong, right, "edit", 1, typed, edited, auto=auto))
        return out

    # ── 自動學的紀錄（給「復原」用）──

    def mark_auto(self, wrong, right, added_vocabulary):
        with self._lock:
            self._data["ai"].pop(self._key(wrong, right), None)
            self._data["auto"].append({"wrong": wrong, "right": right, "vocabulary": bool(added_vocabulary),
                                       "time": time.strftime("%Y-%m-%d %H:%M:%S")})
            self._data["auto"] = self._data["auto"][-50:]
            self._save()

    def last_auto(self):
        with self._lock:
            return dict(self._data["auto"][-1]) if self._data["auto"] else None

    def undo_auto(self, wrong, right):
        """復原一筆自動學的：從紀錄拿掉，並記成「不要」，以後不會再學回來。回傳那筆紀錄（找不到回傳 None）。"""
        with self._lock:
            found = None
            for i in range(len(self._data["auto"]) - 1, -1, -1):
                a = self._data["auto"][i]
                if a.get("wrong") == wrong and a.get("right") == right:
                    found = self._data["auto"].pop(i)
                    break
            key = self._key(wrong, right)
            if key not in self._data["declined"]:
                self._data["declined"].append(key)
            self._save()
        return found

    def decline(self, wrong, right):
        """使用者按了「不要」：以後不再問這一組。"""
        with self._lock:
            key = self._key(wrong, right)
            self._data["ai"].pop(key, None)
            if key not in self._data["declined"]:
                self._data["declined"].append(key)
            self._save()
        log.info("詞庫學習：使用者不要把「%s」→「%s」加入詞庫", wrong, right)

    def forget(self, wrong, right):
        """已經加進詞庫了：清掉次數紀錄。"""
        with self._lock:
            if self._data["ai"].pop(self._key(wrong, right), None) is not None:
                self._save()

    def ai_count(self, wrong, right):
        with self._lock:
            return self._data["ai"].get(self._key(wrong, right), {}).get("count", 0)
