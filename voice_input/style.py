"""個人化風格（M6 ④）：從你的紀錄歸納用字習慣，讓 AI 校正和指令模式照你的習慣寫。

資料來源（只存在本機 logs\\style_samples.json，最多 max_samples 句）：
  - 你說的話（詞庫替換後、AI 校正前）和最後打出來的字
  - 打出來之後你自己動手改成的樣子（最能看出習慣，例如把「哦」改回「喔」、把句號刪掉）
每累積 update_every 句，就在背景請 AI 整理一次「用字習慣」，存進程式資料夾的 style.yaml：
  my_rules：你自己寫的習慣（程式不會動）
  learned ：程式整理出來的（每次整理會整段換新）
第一次啟動時，會先從日誌（logs\\voice_input.log*）把以前說過的話匯入，馬上就有東西可以整理。
"""

import glob
import json
import logging
import os
import re
import threading
import time

import yaml

from .paths import BASE_DIR, LOG_DIR

log = logging.getLogger(__name__)

STYLE_PATH = os.path.join(BASE_DIR, "style.yaml")
SAMPLES_PATH = os.path.join(LOG_DIR, "style_samples.json")

MAX_RULES = 8
MAX_RULE_CHARS = 80
RETRY_AFTER_FAILURE = 30 * 60   # 整理失敗（例如金鑰失效、斷網）後，30 分鐘內不再自動重試

SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string"},
                    "examples": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["rule", "examples"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rules"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
你在幫語音輸入法整理「使用者的用字習慣」。之後 AI 校正器會照這些習慣，把使用者的語音辨識結果修成文字。

你會拿到使用者最近的紀錄，每筆有：場景、「說的」、「打出」，有的還有「他自己改成」。
- 「說的」是使用者自己說出口的話（語音辨識結果，可能有同音錯字）：看得出他自己的用詞、語氣詞、說話方式。
- 「打出」是程式（AI 校正＋簡繁轉換）產生的，不是使用者的習慣，不能當成證據；
  只用來比對程式改掉了他的什麼（例如他說了「啦」，打出來被刪掉了）。
- 「他自己改成」最重要：代表打出來的不合他的意，要照改後的方向。

請歸納 0～8 條「用字與格式習慣」，每條一句話、具體到校正器可以照做，放在 rule。
每條都要附上 examples：從紀錄的「說的」或「他自己改成」裡，逐字複製 2～4 個能證明這個習慣的片段
（每個 2～15 個字、來自不同筆紀錄，不要改字）。程式會檢查這些片段是不是真的在紀錄裡，找不到的整條丟掉。
例如：
- 聊天：句尾的「喔」「啦」「欸」要保留，不要刪
- 「OK」用大寫英文，不要改成「好」或「ok」
- 技術：「跑一下」「丟上去」這類口語照原樣保留，不要改成書面語

規則：
- 只寫有證據的習慣：「說的」裡同樣的情形出現 2 次以上，或使用者自己改過 1 次。沒把握就少寫，一條都沒有也可以，不要湊數。
- 每條簡短清楚（40 字以內），直接說要怎麼做，不要用「…以外」這類繞口的說法。
- 只寫用字、語氣詞、口語說法、格式、中英混用的習慣；不要寫內容、話題，也不要評論使用者。
- 語氣詞（啊、啦、喔、耶、欸…）和贅字只是說話習慣，不代表他想打出來：不要寫「保留語氣詞」這類習慣。
- 以下是程式本來就會做的事，不要寫成習慣：修同音錯字、刪呃嗯、補全形標點、數字用阿拉伯數字、
  英文專有名詞的大小寫和中英之間的空格、簡繁轉換（包括「台／臺」這類用字）、用繁體中文。
- 不要寫進任何個人資料（人名、電話、地址、帳號、金額、網址）。
- 每個場景都適用的習慣，開頭不加任何字；只適用某個場景的，開頭寫「聊天：」「技術：」或「正式：」其中一個。不要用「一般：」。
- <目前的習慣> 給你參考：還成立的保留，被新紀錄推翻的或違反上面規則的拿掉。
- 用台灣繁體中文。"""

_HEADER = """\
# 我的用字習慣：AI 校正和指令模式會照這些習慣幫你寫。存檔後下一句就生效，不用重開程式。
#
# my_rules：你自己寫的習慣，程式不會動。每行一條，開頭是「  - 」，例如：
#   - 「喔」不要改成「哦」
#   - 聊天：句尾不要加句號
# learned：程式從你的紀錄整理出來的（每說 {every} 句自動更新一次，系統匣選單也可以「立即整理」）。
#   覺得哪條不對可以刪掉；如果下次整理又出現，就在 my_rules 寫一條相反的規則（my_rules 優先）。
"""


def _yaml_list(items):
    if not items:
        return "\n"            # 空的就留白，使用者直接在下一行加「  - 」就好
    return "\n" + "".join(f"  - {json.dumps(str(i), ensure_ascii=False)}\n" for i in items)


def _clean_rules(rules):
    out = []
    for r in rules or []:
        r = re.sub(r"^\s*[-*•\d.、]+\s*", "", str(r)).strip()
        if r and len(r) <= MAX_RULE_CHARS and r not in out:
            out.append(r)
    return out[:MAX_RULES]


# ───────────────────────── style.yaml ─────────────────────────

class StyleProfile:
    """讀寫 style.yaml。存檔後自動重新讀取；格式寫錯就繼續用上一次正確的內容。"""

    def __init__(self, path=STYLE_PATH, every=30, notify=None):
        self.path = path
        self.every = every
        self.notify = notify or (lambda title, msg: None)
        self._lock = threading.Lock()
        self._mtime = None
        self.my_rules, self.learned = [], []

    def _reload_if_changed(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self.my_rules, self.learned, self._mtime = [], [], None
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise ValueError("內容不是正確的格式")
            self.my_rules = [str(r).strip() for r in (data.get("my_rules") or []) if str(r).strip()]
            self.learned = [str(r).strip() for r in (data.get("learned") or []) if str(r).strip()]
        except Exception as e:
            log.warning("style.yaml 格式有誤，繼續用上一次的內容：%s", e)
            self.notify("語音輸入：用字習慣檔案格式有誤",
                        f"style.yaml 有地方寫錯了，先繼續用上一次的內容。請檢查剛剛改過的地方。\n原因：{e}")

    def rules(self):
        """AI 要參考的習慣：自己寫的在前。"""
        with self._lock:
            self._reload_if_changed()
            return self.my_rules + [r for r in self.learned if r not in self.my_rules]

    def save_learned(self, learned):
        """換掉 learned 那一段（my_rules 保留原樣）。"""
        with self._lock:
            self._reload_if_changed()
            text = (_HEADER.format(every=self.every)
                    + "\nmy_rules:" + _yaml_list(self.my_rules)
                    + "\nlearned:" + _yaml_list(learned))
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
            self.learned = list(learned)
            self._mtime = os.path.getmtime(self.path)


# ───────────────────────── 紀錄 ─────────────────────────

class StyleSamples:
    """最近說過的句子（只存文字），存在 logs\\style_samples.json。"""

    def __init__(self, path=SAMPLES_PATH, max_items=300):
        self.path = path
        self.max_items = max_items
        self._lock = threading.Lock()
        self.exists = os.path.exists(path)
        data = self._load()
        self.items = data["items"]
        self.since_update = data["since_update"]

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = [i for i in data.get("items", []) if isinstance(i, dict)]
            return {"items": items[-self.max_items:], "since_update": int(data.get("since_update", 0))}
        except (OSError, ValueError, AttributeError, TypeError):
            return {"items": [], "since_update": 0}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"since_update": self.since_update, "items": self.items}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("存用字習慣紀錄失敗：%s", e)

    def add(self, app, scene, said, final):
        self.add_many([(time.strftime("%Y-%m-%d %H:%M:%S"), app, scene, said, final)])

    def add_many(self, rows):
        """rows：[(時間, 程式, 場景, 說的話, 最後打出的字), ...]"""
        with self._lock:
            for when, app, scene, said, final in rows:
                self.items.append({"time": when, "app": app or "", "scene": scene or "",
                                   "said": said, "final": final})
                self.since_update += 1
            self.items = self.items[-self.max_items:]
            self._save()

    def attach_edit(self, app, typed, edited):
        """使用者把打出來的 typed 改成 edited：記在最近那一筆同樣文字的紀錄上。"""
        with self._lock:
            for item in reversed(self.items[-20:]):
                if item.get("final") == typed and item.get("app") == (app or ""):
                    item["edited"] = edited
                    self.since_update += 1      # 自己改的字很重要，多算一次，早點整理
                    self._save()
                    return True
        return False

    def mark_updated(self):
        with self._lock:
            self.since_update = 0
            self._save()

    def recent(self, n):
        with self._lock:
            return [dict(i) for i in self.items[-n:]]


_LOG_LINE = re.compile(r"^(\S+ \S+) \[INFO\] main：原始辨識：(.*) → 詞庫替換後：(.*) → AI 校正後：(.*) → 最終輸出：(.*)$")
_LOG_TIMING = re.compile(r"程式：([^｜]+)｜場景：(\S+)\s*$")


def read_log_samples(paths):
    """從以前的日誌找出 (時間, 程式, 場景, 說的話, 最後打出的字)，給第一次啟動時匯入用。"""
    out = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            m = _LOG_LINE.match(line)
            if not m:
                continue
            when, _raw, said, _ai, final = m.groups()
            timing = _LOG_TIMING.search(lines[i + 1]) if i + 1 < len(lines) else None
            app, scene = (timing.group(1), timing.group(2)) if timing else ("", "")
            out.append((when, app, scene, said, final))
    return out


def _log_paths():
    current = os.path.join(LOG_DIR, "voice_input.log")
    return sorted(p for p in glob.glob(os.path.join(LOG_DIR, "voice_input.log.*"))) + [current]


# ───────────────────────── 整理 ─────────────────────────

def build_user_message(samples, current_rules):
    lines = []
    for s in samples:
        scene = (s.get("scene") or "一般模式").replace("模式", "")
        line = f"- [{scene}] 說的：{s.get('said', '')}｜打出：{s.get('final', '')}"
        if s.get("edited"):
            line += f"｜他自己改成：{s['edited']}"
        lines.append(line)
    current = "\n".join(f"- {r}" for r in current_rules) or "（還沒有）"
    return f"<目前的習慣>\n{current}\n</目前的習慣>\n\n<紀錄>\n" + "\n".join(lines) + "\n</紀錄>"


def _squash(s):
    return re.sub(r"\s+", "", s or "")


def has_evidence(examples, samples):
    """AI 附的例子真的出現在紀錄裡：至少 2 筆「說的」，或 1 筆使用者自己改的（改後有、程式打出的沒有）。"""
    examples = [_squash(e) for e in examples or [] if len(_squash(e)) >= 2]
    said_hits, edit_hit = set(), False
    for idx, s in enumerate(samples):
        said, final, edited = _squash(s.get("said")), _squash(s.get("final")), _squash(s.get("edited"))
        for e in examples:
            if e in said:
                said_hits.add(idx)
            if edited and e in edited and e not in final:
                edit_hit = True
    return len(said_hits) >= 2 or edit_hit


def summarize(backend, samples, current_rules, timeout=20):
    """請 AI 整理用字習慣，只留下紀錄裡找得到證據的，回傳規則清單；失敗丟出例外。"""
    data = backend.complete_json(SYSTEM_PROMPT, build_user_message(samples, current_rules), SCHEMA, timeout)
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise RuntimeError("AI 回傳的格式不對")
    kept = []
    for item in data["rules"]:
        if not isinstance(item, dict):
            continue
        rule, examples = str(item.get("rule", "")), item.get("examples") or []
        if has_evidence(examples, samples):
            kept.append(rule)
        else:
            log.info("個人化風格：丟掉沒有證據的習慣「%s」（例子：%s）", rule, "、".join(map(str, examples)))
    return _clean_rules(kept)


class StyleLearner:
    def __init__(self, cfg, backend_getter, notify=None, profile=None, samples=None, seed_from_logs=True):
        self.enabled = bool(cfg.get("enabled", True))
        self.every = max(5, int(cfg.get("update_every", 30)))
        self.min_samples = max(5, int(cfg.get("min_samples", 20)))
        self.use_samples = int(cfg.get("samples_per_update", 120))
        self.timeout = float(cfg.get("timeout_seconds", 20))
        self.backend_getter = backend_getter
        self.notify = notify or (lambda title, msg: None)
        self.profile = profile or StyleProfile(every=self.every, notify=self.notify)
        self.samples = samples or StyleSamples(max_items=int(cfg.get("max_samples", 300)))
        self._updating = threading.Lock()
        self._next_try = 0.0      # 整理失敗後，這個時間之前不再自動重試（不然每說一句就重試一次）
        if seed_from_logs and not self.samples.exists and not self.samples.items:
            self._seed_from_logs()

    def _seed_from_logs(self):
        found = read_log_samples(_log_paths())[-self.samples.max_items:]
        if found:
            self.samples.add_many(found)
            log.info("個人化風格：從以前的日誌匯入了 %d 句", len(found))

    def rules(self):
        return self.profile.rules() if self.enabled else []

    def record(self, app, scene, said, final):
        """打出一句之後呼叫。累積夠了就在背景整理一次。"""
        if not self.enabled:
            return
        self.samples.add(app, scene, said, final)
        self._maybe_update()

    def record_edit(self, app, typed, edited):
        if self.enabled and self.samples.attach_edit(app, typed, edited):
            self._maybe_update()

    def _maybe_update(self):
        if time.monotonic() < self._next_try:
            return
        if self.samples.since_update >= self.every and len(self.samples.items) >= self.min_samples:
            threading.Thread(target=self.update_now, name="style-update", daemon=True).start()

    def update_now(self, manual=False):
        """請 AI 整理一次。回傳 (成功與否, 說明, 習慣有沒有變)。同一時間只跑一次。"""
        if not self._updating.acquire(blocking=False):
            return False, "正在整理中，請稍等", False
        try:
            backend = self.backend_getter()
            if backend is None or not hasattr(backend, "complete_json"):
                return False, "AI 無法使用（請確認 api_key.txt 或 AI 校正來源）", False
            if len(self.samples.items) < (1 if manual else self.min_samples):
                return False, f"紀錄還不夠（目前 {len(self.samples.items)} 句），多說幾句再整理", False
            old = list(self.profile.learned)
            t0 = time.perf_counter()
            try:
                new = summarize(backend, self.samples.recent(self.use_samples), self.profile.rules(), self.timeout)
            except Exception as e:
                self._next_try = time.monotonic() + RETRY_AFTER_FAILURE
                log.warning("個人化風格：整理用字習慣失敗（%d 分鐘後再自動試）：%s", RETRY_AFTER_FAILURE // 60, e)
                return False, f"整理失敗：{e}", False
            self.profile.save_learned(new)
            self.samples.mark_updated()
            log.info("個人化風格：整理好了（%.1f 秒）：%s", time.perf_counter() - t0, "；".join(new) or "（沒有明顯的習慣）")
            if new != old:
                self.notify("語音輸入：更新了你的用字習慣",
                            f"整理出 {len(new)} 條，AI 校正會照著做。可以從系統匣選單「開啟用字習慣」查看或修改。")
            return True, f"整理出 {len(new)} 條", new != old
        finally:
            self._updating.release()
