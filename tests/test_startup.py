"""確認所有程式檔都能正常載入（曾經因為 main.py 有語法錯誤，程式雙擊後完全沒反應）。"""

import glob
import os
import py_compile

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = [os.path.join(BASE, "main.py")] + sorted(
    glob.glob(os.path.join(BASE, "voice_input", "**", "*.py"), recursive=True)
    + glob.glob(os.path.join(BASE, "tools", "*.py"))
)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: os.path.relpath(p, BASE))
def test_source_compiles(path):
    py_compile.compile(path, doraise=True)


def test_main_module_imports():
    """載入 main.py（不啟動程式），確認最上層的 import 都沒問題。"""
    import importlib.util
    import sys
    sys.path.insert(0, BASE)
    spec = importlib.util.spec_from_file_location("voice_input_main", os.path.join(BASE, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "App") and hasattr(module, "main")


def test_hotkey_callbacks_match_app():
    """熱鍵呼叫主程式的方式（傳幾個參數）要和主程式接收的方式一致，否則放開按鍵就會出錯。"""
    import inspect
    import importlib.util
    import sys
    sys.path.insert(0, BASE)
    spec = importlib.util.spec_from_file_location("voice_input_main2", os.path.join(BASE, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app_cls = module.App
    inspect.signature(app_cls._on_stop).bind(None, "command")      # on_stop(mode)
    inspect.signature(app_cls._on_start).bind(None)                  # on_start()
    inspect.signature(app_cls._on_abort).bind(None, reason="short")  # on_abort(reason=...)
