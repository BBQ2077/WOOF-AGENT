"""測試用：把 plugins/files/__init__.py 當一般模組載入。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("woof_plugins_files", ROOT / "plugins" / "files" / "__init__.py")
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
_xlsx_digest = _mod._xlsx_digest
