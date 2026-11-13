"""插件載入器與註冊表。

每個插件是 plugins/<名稱>/ 底下的資料夾：
  plugin.toml   名稱、版本、類型、預設開關、設定欄位
  __init__.py   def register(woof): ...

register 收到的 woof 物件（PluginAPI）可以：
  woof.add_tool(name, description, schema, handler)
  woof.add_provider(name, provider)         模型轉換器（由 WOOF 工具迴圈驅動）
  woof.add_engine(name, engine)             自帶工具迴圈的引擎（例如 Claude Code CLI）
  woof.on(hook, fn, order=100)              掛鉤，見 events.py
  woof.settings                             此插件目前的設定值（dict）
  woof.config                               全域設定（config.toml）
  woof.keys(pool_name)                      取得金鑰池
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import tomllib
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import caps, keypool, store
from .config import PLUGINS, load_config

VENDOR_FIELDS = ("provider", "base_url", "key_pool", "auth", "effort_style", "echo_reasoning", "max_retries", "rate_limit_wait", "headers", "api_style")


def merge_models(cfg: dict) -> list[dict]:
    """把 [[vendors]] 的共用欄位（插件、位址、金鑰池…）合併進每個模型；模型自己的值優先。"""
    vendors = {v["id"]: v for v in cfg.get("vendors", []) if isinstance(v, dict) and v.get("id")}
    out = []
    for m in cfg.get("models", []):
        if not isinstance(m, dict) or not m.get("id"):
            continue
        v = vendors.get(m.get("vendor") or "")
        merged = {k: v[k] for k in VENDOR_FIELDS if v and k in v}
        merged.update(m)
        merged["vendor_label"] = (v or {}).get("label") or (v or {}).get("id") or "其他"
        out.append(caps.apply(merged))
    return out
from .events import EventBus

log = logging.getLogger("woof.loader")


def _caps_down(model_id: str) -> str:
    from .caps import down

    return down(model_id)


def _kind(m: dict) -> str:
    from .media import kind_of

    return kind_of(m)


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[..., Awaitable[Any]]
    plugin: str


@dataclass
class PluginInfo:
    name: str
    path: Path
    manifest: dict
    enabled: bool
    loaded: bool = False
    error: str | None = None
    settings: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "name": self.name,
            "version": self.manifest.get("version", ""),
            "kinds": self.manifest.get("kinds", []),
            "description": self.manifest.get("description", ""),
            "enabled": self.enabled,
            "loaded": self.loaded,
            "error": self.error,
            "settings_schema": self.manifest.get("settings", {}),
            "settings": self.settings,
        }


class Registry:
    def __init__(self) -> None:
        self.events = EventBus()
        self.tools: dict[str, Tool] = {}
        self.providers: dict[str, Any] = {}
        self.engines: dict[str, Any] = {}
        self.plugins: dict[str, PluginInfo] = {}
        self.config: dict = load_config()
        self.config["models"] = merge_models(self.config)
        self.refresh_discovered()

    # ------------------------------------------------------------ 載入
    def discover(self) -> list[PluginInfo]:
        found = []
        if not PLUGINS.exists():
            return found
        for d in sorted(PLUGINS.iterdir()):
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            mf = d / "plugin.toml"
            if not mf.exists():
                continue
            try:
                manifest = tomllib.loads(mf.read_text(encoding="utf-8"))
            except Exception as e:
                found.append(PluginInfo(d.name, d, {}, False, error=f"plugin.toml 格式錯誤：{e}"))
                continue
            name = manifest.get("name", d.name)
            st = store.get_plugin_state(name)
            enabled = manifest.get("enabled", True) if not st or st["enabled"] is None else st["enabled"]
            defaults = {k: v.get("default") for k, v in manifest.get("settings", {}).items() if isinstance(v, dict)}
            settings = {**defaults, **((st or {}).get("settings") or {})}
            found.append(PluginInfo(name, d, manifest, enabled, settings=settings))
        return found

    def unload_all(self) -> None:
        for hook in list(self.events.hooks.get("plugin_unload", [])):
            try:
                hook.fn(hook.plugin)
            except Exception:
                log.exception("插件 %s 卸載失敗", hook.plugin)
        self.tools.clear()
        self.providers.clear()
        self.engines.clear()
        self.events.hooks.clear()
        for name in list(sys.modules):
            if name.startswith("woof_plugins."):
                del sys.modules[name]

    def load_all(self) -> None:
        self.unload_all()
        self.config = load_config()
        self.config["models"] = merge_models(self.config)
        self.refresh_discovered()
        keypool.reload_pools()
        self.plugins = {}
        self.skills_lister = None  # skills 插件註冊的 callable（設定頁 Skills 分區）
        for info in self.discover():
            self.plugins[info.name] = info
            if not info.enabled or info.error:
                continue
            self._load(info)

    def _load(self, info: PluginInfo) -> None:
        modname = f"woof_plugins.{info.name}"
        try:
            spec = importlib.util.spec_from_file_location(
                modname, info.path / "__init__.py", submodule_search_locations=[str(info.path)])
            if spec is None or spec.loader is None:
                raise RuntimeError("找不到 __init__.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            register = getattr(mod, "register", None)
            if not callable(register):
                raise RuntimeError("缺少 register(woof) 函式")
            register(PluginAPI(self, info))
            info.loaded, info.error = True, None
        except Exception as e:
            self._remove(info.name)
            info.loaded = False
            info.error = f"{type(e).__name__}: {e}"
            log.error("插件 %s 載入失敗\n%s", info.name, traceback.format_exc())

    def _remove(self, plugin: str) -> None:
        self.tools = {k: v for k, v in self.tools.items() if v.plugin != plugin}
        self.providers = {k: v for k, v in self.providers.items() if getattr(v, "_woof_plugin", None) != plugin}
        self.engines = {k: v for k, v in self.engines.items() if getattr(v, "_woof_plugin", None) != plugin}
        self.events.remove_plugin(plugin)

    # ------------------------------------------------------------ 模型
    def refresh_discovered(self) -> None:
        """重新讀 config.toml 的模型商與模型（檢索寫入後呼叫，不重載插件）。"""
        fresh = load_config()
        self.config["vendors"] = fresh.get("vendors") or []
        self.config["models"] = merge_models(fresh)

    def models(self) -> list[dict]:
        out = []
        for m in self.config.get("models", []):
            backend = m.get("provider")
            ok = backend in self.providers or backend in self.engines
            out.append({
                "id": m["id"], "label": m.get("label", m["id"]), "provider": backend,
                "efforts": m.get("efforts", []), "default_effort": m.get("default_effort"),
                "vision": bool(m.get("vision")), "vision_mode": m.get("vision_mode") or "auto",
                "context_window": m.get("context_window"),
                "effort_style": m.get("effort_style"), "learned": m.get("learned") or {},
                "auth": m.get("auth"), "available": ok, "enabled": m.get("enabled") is not False, "is_default": m["id"] == self.default_model_id(),
                "vendor": m.get("vendor") or "", "vendor_label": m.get("vendor_label") or "其他",
                "key_pool": m.get("key_pool") or "",
                "no_key": self._no_key(m),
                "down": _caps_down(m["id"]),
                "kind": _kind(m),
            })
        return out

    def vendors(self) -> list[dict]:
        return [v for v in self.config.get("vendors", []) if isinstance(v, dict) and v.get("id")]

    def default_model_id(self) -> str | None:
        """設定頁「預設模型」；沒設或設的模型不存在時，回傳 None。"""
        dflt = self.config.get("default_model")
        ids = {m.get("id") for m in self.config.get("models", [])}
        return dflt if dflt in ids else None

    def model_cfg(self, model_id: str | None) -> dict | None:
        """找模型設定。指定了 id 卻找不到（被刪或改名）回 None，不偷換別的模型；沒指定才用預設。"""
        models = self.config.get("models", [])
        for m in models:
            if m["id"] == model_id:
                return m
        if model_id:
            return None
        dflt = self.default_model_id()
        for m in models:
            if m["id"] == dflt:
                return m
        return models[0] if models else None

    def tool_specs(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "schema": t.schema} for t in self.tools.values()]

    def _no_key(self, m: dict) -> bool:
        """API 型模型、金鑰池是空的、又不是本機端點 → 缺 key。"""
        if m.get("provider") not in ("openai_compat", "anthropic") or m.get("auth"):
            return False
        base = str(m.get("base_url") or "")
        if any(h in base for h in ("://127.0.0.1", "://localhost", "://[::1]")):
            return False
        name = m.get("key_pool") or ""
        return bool(name) and not keypool.pool(name).entries


class PluginAPI:
    def __init__(self, reg: Registry, info: PluginInfo) -> None:
        self._reg = reg
        self._info = info
        self.name = info.name
        self.settings = info.settings
        self.config = reg.config
        self.path = info.path

    def add_tool(self, name: str, description: str, schema: dict, handler) -> None:
        self._reg.tools[name] = Tool(name, description, schema, handler, self.name)

    def add_provider(self, name: str, provider: Any) -> None:
        provider._woof_plugin = self.name
        self._reg.providers[name] = provider

    def add_engine(self, name: str, engine: Any) -> None:
        engine._woof_plugin = self.name
        self._reg.engines[name] = engine

    def on(self, hook: str, fn, order: int = 100) -> None:
        self._reg.events.on(hook, fn, self.name, order)

    def keys(self, pool_name: str) -> keypool.KeyPool:
        return keypool.pool(pool_name)


REG = Registry()
