"""M4 的測試：開機自動啟動、最近紀錄。（用暫存資料夾，不會動到真的開機設定）"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input import autostart  # noqa: E402
from voice_input.recent import RecentRecords  # noqa: E402


def test_autostart_toggle(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert not autostart.is_enabled()
    assert autostart.toggle() is True
    path = autostart.startup_file()
    assert path.startswith(str(tmp_path))
    content = open(path, encoding="utf-16", newline="").read()
    assert "启动语音输入.vbs" in content
    assert "\r\r\n" not in content          # 換行不能重複
    assert autostart.toggle() is False
    assert not os.path.exists(path)


def test_recent_records_keep_last_20(tmp_path):
    path = tmp_path / "recent.json"
    r = RecentRecords(path=str(path), max_items=20)
    assert r.last() is None and r.all() == []
    for i in range(25):
        r.add("LINE.exe", "聊天模式", f"原始{i}", f"詞庫{i}", f"AI{i}", f"最終{i}", used_ai=True)
    items = r.all()
    assert len(items) == 20
    assert items[0]["final"] == "最終24"       # 由新到舊
    assert items[-1]["final"] == "最終5"
    assert r.last()["raw"] == "原始24"
    # 重開程式後還在
    again = RecentRecords(path=str(path), max_items=20)
    assert again.last()["final"] == "最終24"


def test_recent_records_survive_broken_file(tmp_path):
    path = tmp_path / "recent.json"
    path.write_text("這不是 JSON", encoding="utf-8")
    r = RecentRecords(path=str(path))
    assert r.all() == []
    r.add("", "", "a", "b", "c", "d", used_ai=False)
    assert r.last()["final"] == "d"
