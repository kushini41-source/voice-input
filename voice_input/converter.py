"""輸出前的最後一關：OpenCC 轉成台灣繁體＋台灣用語。

OpenCC 的 s2twp 會把某些詞改成它認為的台灣用語，但不一定是你要的
（例如「項目→專案」「連接詞→連線詞」）。處理方式有兩種，都在 config.yaml：

- converter.protect：保護詞。轉換「之前」先把這些詞藏起來，OpenCC 不會動到。
  講「項目」就打「項目」，講「專案」還是打「專案」，兩個都能正常打出來。
- converter.fixes：修正表。轉換「之後」把左邊換成右邊，用在保護詞處理不了的情況
  （例如「這只能」被轉成「這隻能」，這是 OpenCC 斷詞斷錯，不是用語替換）。
"""

import logging
import re

log = logging.getLogger(__name__)

# 保護詞用 Unicode 私人使用區的字元當暫時記號，OpenCC 不會轉換它們
_PLACEHOLDER_BASE = 0xE000

# 中文字旁邊的半形標點換成全形（英文、數字、程式碼中間的不動，例如 3.14、a, b）
_CJK = r"[㐀-鿿豈-﫿]"
_FULLWIDTH = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；", ".": "。"}
_HALF_PUNCT_AFTER_CJK = re.compile(rf"(?<={_CJK})\s*([,?!:;.])\s*")
_HALF_PUNCT_BEFORE_CJK = re.compile(rf"\s*([,?!:;])\s*(?={_CJK})")
# 刪節號：「...」「。。。」「…」都統一成中文的「……」（不然第一個點會被當成句號，變成「。..」）
_ELLIPSIS = re.compile(r"(?:\.\s*){2,}\.|(?:。\s*){2,}。|…+|\.\.(?=\s|$)")


def to_fullwidth_punct(text):
    text = _ELLIPSIS.sub("……", text)
    text = _HALF_PUNCT_AFTER_CJK.sub(lambda m: _FULLWIDTH[m.group(1)], text)
    return _HALF_PUNCT_BEFORE_CJK.sub(lambda m: _FULLWIDTH[m.group(1)], text)


# 中文和英文詞之間加一個空格（「改成async」→「改成 async」）。
# 英文詞 = 英文字母開頭的一串（可含數字、. + # -），例如 v2.5、Python3、C++。
# 純數字不加空格（「下午3點」維持原樣）。
_LATIN_WORD = r"[A-Za-z][A-Za-z0-9.+#\-]*"
_CJK_BEFORE_WORD = re.compile(rf"({_CJK})(?={_LATIN_WORD})")
_WORD_BEFORE_CJK = re.compile(rf"(\b{_LATIN_WORD})(?={_CJK})")


def space_cjk_latin(text):
    text = _CJK_BEFORE_WORD.sub(r"\1 ", text)
    return _WORD_BEFORE_CJK.sub(r"\1 ", text)


def _alternation(words):
    """長的詞優先比對的正規表示式；一次掃過全文，換完的結果不會被再換一次。"""
    order = sorted(set(words), key=len, reverse=True)
    return re.compile("|".join(re.escape(w) for w in order)) if order else None


class Converter:
    def __init__(self, cfg):
        self._opencc = None
        t2s = None
        try:
            import opencc
            self._opencc = opencc.OpenCC(cfg.get("opencc_config") or "s2twp")
            t2s = opencc.OpenCC("t2s")
        except Exception as e:
            log.error("OpenCC 簡繁轉換初始化失敗，輸出可能會有簡體字：%s", e)

        # 保護詞：繁體寫法和簡體寫法都認得，輸出一律是設定檔裡寫的繁體寫法
        self.protect = [str(w) for w in (cfg.get("protect") or []) if str(w)]
        self._protect_map = {}
        for word in self.protect:
            self._protect_map[word] = word
            if t2s is not None:
                self._protect_map.setdefault(t2s.convert(word), word)
        self._protect_pattern = _alternation(self._protect_map)

        self.fixes = {str(k): str(v) for k, v in (cfg.get("fixes") or {}).items()}
        self._fix_pattern = _alternation(self.fixes)
        self.space_cjk_latin = bool(cfg.get("space_between_chinese_english", True))

    def apply_fixes(self, text):
        if self._fix_pattern is None:
            return text
        return self._fix_pattern.sub(lambda m: self.fixes[m.group(0)], text)

    def _hide_protected(self, text, keep=()):
        """keep：這一次額外要保護的詞（例如詞庫裡的專有名詞），照原樣保留。"""
        hidden = []
        mapping = self._protect_map
        pattern = self._protect_pattern
        keep = [w for w in keep if w and w in text]
        if keep:
            mapping = {**self._protect_map, **{w: w for w in keep}}
            pattern = _alternation(mapping)
        if pattern is None:
            return text, hidden

        def _swap(m):
            hidden.append(mapping[m.group(0)])
            return chr(_PLACEHOLDER_BASE + len(hidden) - 1)

        return pattern.sub(_swap, text), hidden

    @staticmethod
    def _restore_protected(text, hidden):
        for i, word in enumerate(hidden):
            text = text.replace(chr(_PLACEHOLDER_BASE + i), word)
        return text

    def convert(self, text, keep=()):
        if not text:
            return text
        if self._opencc is not None:
            text, hidden = self._hide_protected(text, keep)
            try:
                text = self._opencc.convert(text)
            except Exception as e:
                log.error("OpenCC 轉換失敗，略過這一步：%s", e)
            text = self._restore_protected(text, hidden)
        text = to_fullwidth_punct(self.apply_fixes(text))
        if self.space_cjk_latin:
            text = space_cjk_latin(text)
        return text
