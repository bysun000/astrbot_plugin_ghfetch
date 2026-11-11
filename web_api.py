# -*- coding: utf-8 -*-
"""GitHub Release 加速下载插件 —— 页面用 Web API

给插件自带页面（pages/settings/index.html）提供：
  GET  /astrbot_plugin_ghfetch/config        读取配置项定义 + 当前值
  POST /astrbot_plugin_ghfetch/config/save   保存配置（写盘并即时生效）
  GET  /astrbot_plugin_ghfetch/status        运行状态（aria2 / 回传端口 / 残留）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    from astrbot.api.web import error_response, json_response, request

    HAS_WEB = True
except ImportError:  # pragma: no cover - 旧版本 AstrBot 兜底
    try:
        from quart import jsonify, request  # type: ignore

        HAS_WEB = False

        def json_response(payload: Any) -> Any:  # type: ignore
            return jsonify(payload)

        def error_response(message: str, status_code: int = 400) -> Any:  # type: ignore
            resp = jsonify({"status": "error", "message": message, "data": {}})
            resp.status_code = status_code
            return resp

    except ImportError:
        HAS_WEB = False

        def _fallback(*_a, **_k):
            raise RuntimeError("Web API not available")

        json_response = error_response = _fallback  # type: ignore


PLUGIN_NAME = "astrbot_plugin_ghfetch"

# 需要重启插件/容器才真正生效的配置项
RESTART_KEYS = ("http_port", "callback_host", "aria2_path", "max_concurrent", "enabled")

INT_LIMITS = {
    "connections": (1, 64),
    "max_concurrent": (1, 16),
    "max_size_mb": (1, 100000),
    "http_port": (1, 65535),
    "clean_delay": (0, 86400),
    "search_candidates": (1, 20),
    "asset_candidates": (1, 30),
    "pending_ttl": (10, 3600),
    "repo_intro_readme_chars": (200, 20000),
    "info_cache_ttl": (0, 86400),
}


def _ok(data: Any = None, message: str = "") -> Any:
    return json_response({"status": "ok", "message": message, "data": data})


def _err(message: str, status_code: int = 400) -> Any:
    return error_response(message, status_code=status_code)


async def _get_json() -> dict:
    try:
        data = await request.json(default=None)
    except TypeError:  # pragma: no cover
        data = await request.json
    except Exception:
        data = None
    return data if isinstance(data, dict) else {}


class GhFetchConfigAPI:
    def __init__(self, plugin: Any, plugin_dir: Path):
        self.plugin = plugin
        self.plugin_dir = Path(plugin_dir)

    # ---------- 基础 ----------
    @property
    def config(self) -> dict:
        cfg = getattr(self.plugin, "config", None)
        return cfg if isinstance(cfg, dict) else {}

    def load_schema(self) -> dict:
        path = self.plugin_dir / "_conf_schema.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[ghfetch] 读取 _conf_schema.json 失败：%s" % exc)
            return {}
        return data if isinstance(data, dict) else {}

    def current_values(self, schema: dict) -> dict:
        cfg = self.config
        values = {}
        for key, spec in schema.items():
            default = spec.get("default")
            values[key] = cfg.get(key, default)
        return values

    # ---------- 校验 ----------
    def coerce(self, key: str, spec: dict, raw: Any) -> Any:
        ftype = spec.get("type", "string")
        if ftype == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in {"1", "true", "yes", "on", "是"}

        if ftype == "int":
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                raise ValueError("%s 需要填整数" % key)
            low, high = INT_LIMITS.get(key, (0, 10**9))
            if not low <= value <= high:
                raise ValueError("%s 的取值范围是 %s ~ %s" % (key, low, high))
            return value

        if ftype == "list":
            if isinstance(raw, list):
                items = [str(x).strip() for x in raw]
            else:
                items = [line.strip() for line in str(raw or "").splitlines()]
            # 去掉尾部的空项，保留中间的（mirrors 里空字符串表示 GitHub 直连）
            while items and items[-1] == "":
                items.pop()
            return items

        value = "" if raw is None else str(raw)
        return value.strip()

    # ---------- 持久化 ----------
    def persist(self, values: dict) -> str:
        cfg = self.config
        cfg.update(values)
        saver = getattr(cfg, "save_config", None)
        if callable(saver):
            try:
                saver()
                return "astrbot"
            except Exception as exc:
                logger.warning("[ghfetch] save_config 失败，改用手写文件：%s" % exc)
        target = self.plugin_dir.parent.parent / "config" / ("%s_config.json" % PLUGIN_NAME)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return str(target)
        except Exception as exc:
            logger.warning("[ghfetch] 写配置文件失败：%s" % exc)
            return "memory-only"

    # ---------- 接口 ----------
    async def get_config(self):
        schema = self.load_schema()
        if not schema:
            return _err("找不到 _conf_schema.json，无法渲染配置项", 500)
        values = self.current_values(schema)
        # 敏感项脱敏展示
        meta = {
            "plugin": PLUGIN_NAME,
            "restart_keys": list(RESTART_KEYS),
        }
        return _ok({"schema": schema, "values": values, "meta": meta})

    async def save_config(self):
        body = await _get_json()
        incoming = body.get("values")
        if not isinstance(incoming, dict):
            return _err("请求体缺少 values 字段")

        schema = self.load_schema()
        if not schema:
            return _err("找不到 _conf_schema.json，无法校验", 500)

        cleaned: dict[str, Any] = {}
        for key, raw in incoming.items():
            spec = schema.get(key)
            if not isinstance(spec, dict):
                continue
            try:
                cleaned[key] = self.coerce(key, spec, raw)
            except ValueError as exc:
                return _err(str(exc))

        if not cleaned:
            return _err("没有可保存的配置项")

        where = self.persist(cleaned)
        changed = [k for k in cleaned if k in RESTART_KEYS]
        logger.info(
            "[ghfetch] 配置页保存 %d 项（%s）" % (len(cleaned), where)
        )
        return _ok(
            {
                "saved": sorted(cleaned.keys()),
                "values": self.current_values(schema),
                "restart_needed": sorted(changed),
                "storage": where,
            },
            "已保存 %d 项" % len(cleaned),
        )

    async def get_status(self):
        plugin = self.plugin
        data: dict[str, Any] = {"plugin": PLUGIN_NAME}
        try:
            aria2 = plugin._find_aria2()
        except Exception:
            aria2 = None
        data["aria2"] = aria2 or ""
        data["aria2_ready"] = bool(aria2)
        data["http_port"] = getattr(plugin, "_http_port", None)
        data["data_dir"] = str(getattr(plugin, "data_dir", ""))
        data["pending"] = len(getattr(plugin, "_pending", {}) or {})
        data["busy"] = len(getattr(plugin, "_busy", set()) or set())

        dl_root = getattr(plugin, "dl_root", None)
        files = 0
        size = 0
        if isinstance(dl_root, Path) and dl_root.is_dir():
            for item in dl_root.rglob("*"):
                try:
                    if item.is_file():
                        files += 1
                        size += item.stat().st_size
                except OSError:
                    continue
        data["leftover_files"] = files
        data["leftover_size"] = size
        return _ok(data)


def register_routes(context: Any, plugin: Any, plugin_dir: Path) -> None:
    api = GhFetchConfigAPI(plugin, plugin_dir)
    routes = [
        ("/%s/config" % PLUGIN_NAME, api.get_config, ["GET"], "读取 ghfetch 配置项"),
        (
            "/%s/config/save" % PLUGIN_NAME,
            api.save_config,
            ["POST"],
            "保存 ghfetch 配置",
        ),
        ("/%s/status" % PLUGIN_NAME, api.get_status, ["GET"], "ghfetch 运行状态"),
    ]
    registered = 0
    for path, handler, methods, desc in routes:
        try:
            context.register_web_api(path, handler, methods, desc)
            registered += 1
        except Exception as exc:  # pragma: no cover
            logger.warning("[ghfetch] 注册 %s 失败：%s" % (path, exc))
    logger.info("[ghfetch] Web API 注册完成（%d 条路由）" % registered)
