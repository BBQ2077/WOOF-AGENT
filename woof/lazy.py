"""延後載入大型套件：第一次用到屬性時才 import，沒用到就不佔記憶體與啟動時間。"""
from __future__ import annotations

import importlib
from types import ModuleType


class _Lazy(ModuleType):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._mod = None

    def _load(self):
        if self._mod is None:
            self._mod = importlib.import_module(self.__name__)
        return self._mod

    def __getattr__(self, attr: str):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return getattr(self._load(), attr)


def lazy_module(name: str) -> ModuleType:
    return _Lazy(name)
