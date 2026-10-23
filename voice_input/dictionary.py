"""個人詞庫：dictionary.yaml 的載入、存檔即自動更新、拼音相近比對與替換。

詞庫有四種內容：
  vocabulary   專有名詞：提示給辨識引擎，也保護它們不被簡繁轉換改掉
  replacements 強制替換：「聽到 A → 換成 B」，支援完全比對和「發音相近」比對
  rules        正規表示式替換（進階用）
  snippets     語音快捷片語：整句話剛好是左邊時，直接輸出右邊整段文字

處理順序：snippets（整句命中就直接輸出）→ replacements → rules。
"""

import logging
import os
import re
import threading
import time
from functools import lru_cache

import yaml

from .paths import DICTIONARY_PATH

log = logging.getLogger(__name__)

_CJK_CHAR = re.compile(r"[㐀-鿿豈-﫿]")
_IGNORABLE = re.compile(r"[\s，。！？、；：,.!?;:「」『』“”\"'（）()…—\-~～]+")
_ASCII_WORD_EDGE = re.compile(r"[A-Za-z0-9]")

DEFAULT_DICTIONARY = """\
# 個人詞庫：存檔後幾秒內自動生效，不用重開程式。
# 格式錯誤時會跳通知，並繼續使用上一次正確的版本。

# 1. 專有名詞：提供給辨識引擎當提示，也不會被簡繁轉換改掉（越常用的放越前面）
vocabulary:
  - buy+1
  - Claude Code
  - Claude
  - CapsWriter
  - faster-whisper
  - GitHub
  - Python
  - VS Code
  - RTX 4070 Ti

# 2. 強制替換：辨識結果裡出現左邊，一律換成右邊。
#    除了完全一樣，「唸起來很像」的也會換（例如左邊寫「克勞德」，辨識成「克老得」也會換）。
#    像不像的門檻在 config.yaml 的 dictionary.fuzzy_threshold 調整。
replacements:
  "克勞德扣的": "Claude Code"
  "克勞德扣": "Claude Code"
  "克勞德": "Claude"
  "皮森": "Python"
  "吉特哈布": "GitHub"
  "拜加溫": "buy+1"
  "拜加一": "buy+1"

# 3. 規則替換：正規表示式（進階用，不熟可以不用管）
rules:
  # buy+1：「buy plus one」「拜加一」「拜家萬」等各種聽法都換成 buy+1
  - pattern: '(?i)(?<![A-Za-z])(buy|bye|by|拜)\\s*(\\+|plus|加|家|交)\\s*(1|one|一|溫|温|萬|万|換|换)(?![A-Za-z0-9])'
    replace: 'buy+1'
  - pattern: "(\\\\d+)\\\\s*點\\\\s*(\\\\d+)\\\\s*版"
    replace: "v\\\\1.\\\\2"

# 4. 語音快捷片語：整句話剛好是左邊時，直接輸出右邊（可以多行，用 \\n 換行）
#    「我的信箱」請改成你自己的 Email
snippets:
  "我的信箱": "you@example.com"
  "簽名檔": "祝 順心\\n（你的名字）"
"""


# ───────────────────────── 拼音比對 ─────────────────────────

@lru_cache(maxsize=8192)
def char_readings(ch):
    """一個中文字所有可能的讀音（不帶聲調），例如「行」→ ('xing', 'hang', 'heng')。"""
    from pypinyin import Style, pinyin
    readings = pinyin(ch, style=Style.NORMAL, heteronym=True)
    if readings and readings[0]:
        return tuple(dict.fromkeys(r.lower() for r in readings[0]))
    return (ch,)


def _accent_normalize(syllable):
    """台灣口音常見的混淆：捲舌不捲舌、前後鼻音、n/l 不分。"""
    s = syllable
    for a, b in (("zh", "z"), ("ch", "c"), ("sh", "s")):
        if s.startswith(a):
            s = b + s[len(a):]
            break
    if s.startswith("l"):
        s = "n" + s[1:]
    for a, b in (("ang", "an"), ("eng", "en"), ("ing", "in")):
        if s.endswith(a):
            s = s[: -len(a)] + b
            break
    return s


def _edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@lru_cache(maxsize=65536)
def syllable_similarity(a, b):
    """兩個音節有多像：一樣 = 1，只差台灣口音的混淆 = 0.9，其他依拼寫差異遞減。"""
    if a == b:
        return 1.0
    if _accent_normalize(a) == _accent_normalize(b):
        return 0.9
    dist = _edit_distance(a, b)
    return max(0.0, 1.0 - dist / max(len(a), len(b))) * 0.7


def char_similarity(c1, c2):
    if c1 == c2:
        return 1.0
    return max(syllable_similarity(a, b) for a in char_readings(c1) for b in char_readings(c2))


def pinyin_similarity(s1, s2):
    """兩段等長中文唸起來有多像（0~1）。"""
    if len(s1) != len(s2) or not s1:
        return 0.0
    return sum(char_similarity(a, b) for a, b in zip(s1, s2)) / len(s1)


def _is_all_cjk(s):
    return bool(s) and all(_CJK_CHAR.match(c) for c in s)


def normalize_sentence(text):
    """比對整句用：去掉標點和空白、英文轉小寫。"""
    return _IGNORABLE.sub("", text).lower()


# ───────────────────────── 詞庫內容 ─────────────────────────

class DictionaryData:
    def __init__(self, data, t2s=None, s2t=None):
        data = data or {}
        self.vocabulary = [str(v).strip() for v in (data.get("vocabulary") or []) if str(v).strip()]
        self.replacements = {
            str(k).strip(): str(v) for k, v in (data.get("replacements") or {}).items() if str(k).strip()
        }
        self.snippets = {
            str(k).strip(): str(v).replace("\\n", "\n") for k, v in (data.get("snippets") or {}).items()
            if str(k).strip()
        }
        self.rules = []
        for rule in data.get("rules") or []:
            try:
                self.rules.append((re.compile(str(rule["pattern"])), str(rule.get("replace", ""))))
            except (KeyError, TypeError, re.error) as e:
                log.warning("詞庫裡有一條規則格式錯誤，已略過：%s（%s）", rule, e)

        # 辨識結果可能是簡體也可能是繁體，所以左邊的詞兩種寫法都認
        def variants(word):
            out = {word}
            for conv in (t2s, s2t):
                if conv is not None:
                    try:
                        out.add(conv.convert(word))
                    except Exception:
                        pass
            return out

        self._exact = {}
        for key, value in self.replacements.items():
            for v in variants(key):
                self._exact.setdefault(v, value)
        order = sorted(self._exact, key=len, reverse=True)
        self._exact_pattern = re.compile("|".join(map(re.escape, order))) if order else None

        # 拼音相近比對只用全中文、至少兩個字的詞（單一個字太容易誤換）
        self._fuzzy_keys = sorted(
            {k for k in self._exact if _is_all_cjk(k) and len(k) >= 2}, key=len, reverse=True
        )

        self._snippets_norm = {}
        for key, value in self.snippets.items():
            for v in variants(key):
                self._snippets_norm.setdefault(normalize_sentence(v), value)

    @property
    def prompt_terms(self):
        """要提示給辨識引擎的詞：專有名詞 + 替換後的詞（去重、保持順序）。"""
        terms = list(self.vocabulary) + list(self.replacements.values())
        return list(dict.fromkeys(t for t in terms if t.strip()))

    @property
    def protected_terms(self):
        """不能被簡繁轉換改掉的詞。"""
        return self.prompt_terms

    # ── snippets ──

    def match_snippet(self, text, threshold):
        norm = normalize_sentence(text)
        if not norm:
            return None
        if norm in self._snippets_norm:
            return self._snippets_norm[norm]
        # 唸起來幾乎一樣也算（例如「我的信相」）
        best, best_score = None, 0.0
        for key, value in self._snippets_norm.items():
            if len(key) == len(norm) and _is_all_cjk(key) and _is_all_cjk(norm):
                score = pinyin_similarity(key, norm)
                if score > best_score:
                    best, best_score = value, score
        return best if best_score >= threshold else None

    # ── replacements ──

    def apply_replacements(self, text, threshold, fuzzy=True):
        """回傳 (替換後文字, 命中紀錄)。替換進去的文字不會被再換第二次。"""
        hits = []
        # 切成 [(文字, 是否已鎖定)]，已經替換過的片段鎖起來
        segments = [(text, False)]

        if self._exact_pattern is not None:
            segments = self._split_exact(segments, hits)

        if fuzzy and self._fuzzy_keys:
            segments = self._split_fuzzy(segments, threshold, hits)

        return _join_with_spacing(segments), hits

    def _split_exact(self, segments, hits):
        out = []
        for seg, locked in segments:
            if locked:
                out.append((seg, True))
                continue
            pos = 0
            for m in self._exact_pattern.finditer(seg):
                if m.start() > pos:
                    out.append((seg[pos:m.start()], False))
                value = self._exact[m.group(0)]
                out.append((value, True))
                hits.append((m.group(0), value, 1.0))
                pos = m.end()
            if pos < len(seg):
                out.append((seg[pos:], False))
        return out

    def _split_fuzzy(self, segments, threshold, hits):
        out = []
        for seg, locked in segments:
            if locked or not _CJK_CHAR.search(seg):
                out.append((seg, locked))
                continue
            out.extend(self._fuzzy_one(seg, threshold, hits))
        return out

    def _fuzzy_one(self, seg, threshold, hits):
        """在一段文字裡找唸起來像詞庫左邊的片段；長的詞優先、分數高的優先、不重疊。"""
        candidates = []
        for key in self._fuzzy_keys:
            n = len(key)
            for i in range(0, len(seg) - n + 1):
                window = seg[i:i + n]
                if not _is_all_cjk(window):
                    continue
                score = pinyin_similarity(key, window)
                if score >= threshold:
                    candidates.append((n, score, i, key, window))
        if not candidates:
            return [(seg, False)]

        candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
        taken = [False] * len(seg)
        chosen = []
        for n, score, i, key, window in candidates:
            if any(taken[i:i + n]):
                continue
            for k in range(i, i + n):
                taken[k] = True
            chosen.append((i, n, key, window, score))
        chosen.sort()

        out, pos = [], 0
        for i, n, key, window, score in chosen:
            if i > pos:
                out.append((seg[pos:i], False))
            value = self._exact[key]
            out.append((value, True))
            hits.append((window, value, round(score, 3)))
            pos = i + n
        if pos < len(seg):
            out.append((seg[pos:], False))
        return out

    # ── rules ──

    def apply_rules(self, text):
        for pattern, replace in self.rules:
            try:
                text = pattern.sub(replace, text)
            except re.error as e:
                log.warning("規則 %s 執行失敗：%s", pattern.pattern, e)
        return text


def _join_with_spacing(segments):
    """英文詞夾在中文中間時，前後補一個空格（例如「Claude Code 很好用」）。"""
    text = ""
    for seg, _locked in segments:
        if not seg:
            continue
        if text and _needs_space(text[-1], seg[0]):
            text += " "
        text += seg
    return re.sub(r" {2,}", " ", text)


def _needs_space(left, right):
    """一邊是英數、另一邊是中文時需要空格。"""
    l_ascii, r_ascii = bool(_ASCII_WORD_EDGE.match(left)), bool(_ASCII_WORD_EDGE.match(right))
    l_cjk, r_cjk = bool(_CJK_CHAR.match(left)), bool(_CJK_CHAR.match(right))
    return (l_ascii and r_cjk) or (l_cjk and r_ascii)


# ───────────────────────── 從程式寫入詞庫 ─────────────────────────

def _yaml_str(s):
    """JSON 字串剛好也是合法的 YAML 雙引號字串，用它來處理引號、反斜線等特殊字元。"""
    import json
    return json.dumps(str(s), ensure_ascii=False)


def _insert_into_section(text, section, line):
    """在 YAML 文字的某個最上層區塊最後面插入一行，保留檔案裡原本的註解和排版。"""
    lines = text.split("\n")
    header = re.compile(rf"^{re.escape(section)}\s*:\s*(#.*)?$")
    start = next((i for i, l in enumerate(lines) if header.match(l)), None)
    if start is None:
        # 沒有這個區塊就加在檔案最後
        body = text.rstrip("\n")
        return f"{body}\n\n{section}:\n{line}\n"
    # 區塊內容 = 後面連續的「有縮排的行」；空白行和註解行先跳過，遇到下一個最上層項目就停
    last = start
    for i in range(start + 1, len(lines)):
        l = lines[i]
        if l.startswith((" ", "\t")) and l.strip() and not l.strip().startswith("#"):
            last = i
        elif l.strip() == "" or l.lstrip().startswith("#"):
            continue
        else:
            break
    lines.insert(last + 1, line)
    return "\n".join(lines)


def add_to_dictionary(path, replacements=(), vocabulary=(), comment=""):
    """把新詞寫進 dictionary.yaml。

    replacements：[(錯的字, 對的字), ...]；vocabulary：[專有名詞, ...]。
    comment：加在每一行後面的註解（例如「自動學的 2026-10-23」），方便使用者看出哪些是自動加的。
    已經存在的會略過。寫完會重新解析一次，格式有問題就還原成原本的檔案並丟出例外。
    回傳實際新增的項目數。
    """
    note = f"  # {comment}" if comment else ""
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()
    current = yaml.safe_load(original) or {}
    existing_rep = {str(k) for k in (current.get("replacements") or {})}
    existing_voc = {str(v) for v in (current.get("vocabulary") or [])}

    text, added = original, 0
    if (current.get("replacements") is not None and not isinstance(current.get("replacements"), dict)) or \
       (current.get("vocabulary") is not None and not isinstance(current.get("vocabulary"), list)):
        raise ValueError("dictionary.yaml 的 replacements 或 vocabulary 格式不對，請先手動修正")
    # 原本寫成 replacements: {} 這種空的行內格式時，換成區塊格式才能往下加
    text = re.sub(r"^replacements\s*:\s*\{\s*\}\s*$", "replacements:", text, flags=re.M)
    text = re.sub(r"^vocabulary\s*:\s*\[\s*\]\s*$", "vocabulary:", text, flags=re.M)

    for wrong, right in replacements:
        wrong, right = str(wrong).strip(), str(right).strip()
        if not wrong or not right or wrong in existing_rep:
            continue
        text = _insert_into_section(text, "replacements", f"  {_yaml_str(wrong)}: {_yaml_str(right)}{note}")
        existing_rep.add(wrong)
        added += 1
    for term in vocabulary:
        term = str(term).strip()
        if not term or term in existing_voc:
            continue
        text = _insert_into_section(text, "vocabulary", f"  - {_yaml_str(term)}{note}")
        existing_voc.add(term)
        added += 1

    if added == 0:
        return 0
    check = yaml.safe_load(text) or {}
    for wrong, right in replacements:
        if str(wrong).strip() and (check.get("replacements") or {}).get(str(wrong).strip()) is None:
            raise ValueError("寫入詞庫後格式檢查沒過，已放棄這次修改")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    log.info("從程式新增了 %d 個詞到詞庫：替換 %s、專有名詞 %s", added, list(replacements), list(vocabulary))
    return added


def remove_from_dictionary(path, wrong, vocabulary_term=None):
    """把 replacements 裡左邊是 wrong 的那一行（和可選的一個專有名詞）從 dictionary.yaml 拿掉，
    其他內容和註解原樣保留（給「復原自動學的詞」用）。回傳拿掉了幾行。"""
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()
    section, keep, removed = None, [], 0
    for line in original.split("\n"):
        top = re.match(r"^([A-Za-z_]\w*)\s*:", line)
        if top:
            section = top.group(1)
        stripped = line.strip()
        if line.startswith((" ", "\t")) and stripped and not stripped.startswith("#"):
            try:
                entry = yaml.safe_load(stripped)
            except yaml.YAMLError:
                entry = None
            if section == "replacements" and isinstance(entry, dict) and len(entry) == 1 \
                    and str(next(iter(entry))).strip() == wrong:
                removed += 1
                continue
            if section == "vocabulary" and vocabulary_term and isinstance(entry, list) and len(entry) == 1 \
                    and str(entry[0]).strip() == vocabulary_term:
                removed += 1
                continue
        keep.append(line)
    if removed == 0:
        return 0
    text = "\n".join(keep)
    check = yaml.safe_load(text) or {}
    if wrong in (check.get("replacements") or {}):
        raise ValueError("從詞庫拿掉後格式檢查沒過，已放棄這次修改")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    log.info("從詞庫拿掉了「%s」%s", wrong, f"和專有名詞「{vocabulary_term}」" if vocabulary_term else "")
    return removed


# ───────────────────────── 載入與熱更新 ─────────────────────────

class Dictionary:
    """持有目前的詞庫內容，並在 dictionary.yaml 被存檔後自動重新載入。"""

    def __init__(self, cfg, path=DICTIONARY_PATH, notify=None):
        self.path = path
        self.threshold = float(cfg.get("fuzzy_threshold", 0.85))
        self.fuzzy = bool(cfg.get("fuzzy_match", True))
        self.notify = notify or (lambda title, msg: None)
        self._t2s = self._s2t = None
        try:
            import opencc
            self._t2s = opencc.OpenCC("t2s")
            self._s2t = opencc.OpenCC("s2t")
        except Exception:
            pass
        self.data = DictionaryData({})
        self._mtime = None
        self._lock = threading.Lock()
        self._ensure_file()
        self.reload(initial=True)

    def _ensure_file(self):
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(DEFAULT_DICTIONARY)
            log.info("建立了預設詞庫：%s", self.path)

    def reload(self, initial=False):
        try:
            mtime = os.path.getmtime(self.path)
            with open(self.path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            if not isinstance(raw, dict):
                raise ValueError("最外層必須是 vocabulary／replacements／rules／snippets 這幾個項目")
            data = DictionaryData(raw, self._t2s, self._s2t)
        except FileNotFoundError:
            self._mtime = None
            return False
        except Exception as e:
            self._mtime = os.path.getmtime(self.path) if os.path.exists(self.path) else None
            log.error("詞庫 dictionary.yaml 格式有誤，繼續使用上一個版本：%s", e)
            self.notify(
                "語音輸入：詞庫格式有誤",
                f"dictionary.yaml 有地方寫錯了，這次的修改沒有套用（繼續用上一個版本）。\n{e}",
            )
            return False
        with self._lock:
            self.data = data
            self._mtime = mtime
        log.info(
            "詞庫已%s：專有名詞 %d、替換 %d、規則 %d、快捷片語 %d",
            "載入" if initial else "自動更新",
            len(data.vocabulary), len(data.replacements), len(data.rules), len(data.snippets),
        )
        return True

    def check_for_changes(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if mtime != self._mtime:
            # 編輯器存檔可能分好幾次寫入，稍等一下再讀
            time.sleep(0.2)
            return self.reload()
        return False

    def start_watching(self, interval=1.0):
        def _watch():
            while True:
                time.sleep(interval)
                try:
                    self.check_for_changes()
                except Exception:
                    log.exception("檢查詞庫更新時發生錯誤")
        threading.Thread(target=_watch, name="dictionary-watch", daemon=True).start()

    def warmup(self):
        """拼音字典第一次用時要花約 0.2 秒載入，啟動時先跑一次。"""
        self.process("詞庫暖機用的一句話")

    # ── 給主流程用 ──

    def current(self):
        with self._lock:
            return self.data

    def process(self, text):
        """回傳 (結果, 是否為快捷片語, 命中紀錄)。快捷片語命中時，後面的步驟全部跳過。"""
        data = self.current()
        snippet = data.match_snippet(text, self.threshold)
        if snippet is not None:
            return snippet, True, [("快捷片語", snippet, 1.0)]
        text, hits = data.apply_replacements(text, self.threshold, fuzzy=self.fuzzy)
        text = data.apply_rules(text)
        return text, False, hits
