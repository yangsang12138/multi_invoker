"""配置加载：节点清单、调用项清单、全局请求头（基于 SQLite）。"""

from __future__ import annotations

import json
import os

from . import runtime
from .db import Db

# 不再提供任何内置 URL 模板 —— 模板完全由用户在「URL 模板」页面配置。
# 通用编排层不假设「合包 / 分包」或任何特定部署方式。
DEFAULT_CONTEXT_PROFILES: tuple = ()

# 仅用于读取「旧版 endpoints 字典」的迁移映射（{test:{...}, prod:{...}} → 带标签的列表）。
# 迁移完成后这段只对历史数据生效；运行期不存在「环境」这一概念。
LEGACY_ENV_LABELS = {"test": "测试", "prod": "正式"}

# 全局默认请求头：headers.json 不存在时使用
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
}
DEFAULT_BODY_FORMAT = "json"  # 或 "form"

# 配置目录随运行形态变化：源码运行是仓库的 `config/`，单文件运行是 `~/.multi-invoker/config`。
# 具体规则见 runtime.py；`--config-dir` 与 `MULTI_INVOKER_HOME` 可以覆盖。
DEFAULT_CONFIG_DIR = runtime.default_config_dir()
DEFAULT_DB_PATH = os.path.join(DEFAULT_CONFIG_DIR, "app.db")


class ConfigError(Exception):
    """配置文件缺失或格式错误。"""


def _read_json(path: str) -> dict:
    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 JSON 格式错误：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是对象：{path}")
    return data


def _read_json_or(path: str, default: dict) -> dict:
    if not os.path.isfile(path):
        return dict(default)
    return _read_json(path)


def normalize_endpoints(raw) -> list:
    """把 endpoints 统一成「带标签的地址条目列表」。

    新格式：[{"tags": ["测试"], "baseUrl": "http://..."}, ...]
    旧格式：{"test": {"baseUrl": "...", "portalPath": "..."}, "prod": {...}}
            —— 用 LEGACY_ENV_LABELS 把键名翻成标签（仅历史数据兼容）。

    只保留有 baseUrl 的条目；portalPath 不再维护，直接丢弃。
    """
    out = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("baseUrl") or "").strip()
            if not url:
                continue
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [x.strip() for x in tags.split(",")]
            tags = [str(x).strip() for x in tags if str(x).strip()]
            out.append({"tags": tags, "baseUrl": url})
        return out
    if isinstance(raw, dict):
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue
            url = str(item.get("baseUrl") or "").strip()
            if not url:
                continue
            label = LEGACY_ENV_LABELS.get(str(key), str(key))
            out.append({"tags": [label] if label else [], "baseUrl": url})
    return out


class Config:
    """平台 + 服务 + 全局请求头配置，供 CLI 与本地网页共用。"""

    def __init__(self, config_dir: str = DEFAULT_CONFIG_DIR):
        self.dir = os.path.abspath(config_dir)
        self.db_path = os.path.join(self.dir, "app.db")

        # 首次启动迁移：把老的 JSON 文件导入 SQLite（如果存在）
        from . import migrate
        migrate.ensure_migrated(self.dir, self.db_path)
        self.db = Db(self.db_path)

        nodes = self.db.list_nodes()
        services = self.db.list_services()
        headers = self.db.get_headers()

        # 兼容字段：旧 Config 暴露 platforms / services / context_profiles / default_headers / body_format
        self.platforms = nodes
        self.services = services
        self.platform_meta = {"contextProfiles": self.db.list_context_profiles()}
        self.service_meta = {}
        self.header_meta = {}

        # 「contextProfile」按标签匹配 → 上下文 → URL 模板：先固化默认 profile，
        # 给所有现有平台一条兜底路径（让重构不破坏老调用）。
        # 真正的优先级解析在 runner.py：service.contextProfile > 平台 tag 命中 > 默认 profile。
        self.context_profiles = self._load_context_profiles(
            {"meta": {"contextProfiles": self.platform_meta["contextProfiles"]}})

        # 全局默认请求头 / body 格式
        default_headers = headers.get("defaultHeaders") or DEFAULT_HEADERS
        if not isinstance(default_headers, dict):
            default_headers = dict(DEFAULT_HEADERS)
        self.default_headers = {str(k): str(v) for k, v in default_headers.items()}
        body_format = (headers.get("bodyFormat") or DEFAULT_BODY_FORMAT).lower()
        self.body_format = "form" if body_format == "form" else "json"

        # 默认要求至少 1 个平台 + 1 条服务，避免误用空配置发起请求。
        # 当环境变量 MULTI_INVOKER_ALLOW_EMPTY_CONFIG=1 时放宽，便于初次启动 / 配置丢失后
        # 仍然能让网页起来、由用户在 UI 上补齐数据。
        _allow_empty = os.environ.get("MULTI_INVOKER_ALLOW_EMPTY_CONFIG") == "1"
        if not self.platforms and not _allow_empty:
            raise ConfigError(f"platforms.json 中没有平台数据：{self.dir}")
        if not self.services and not _allow_empty:
            raise ConfigError(f"services.json 中没有服务数据：{self.dir}")

        self.platform_by_code = {}
        for item in self.platforms:
            code = item.get("code")
            if not code:
                raise ConfigError("platforms.json 中存在缺少 code 的平台")
            if code in self.platform_by_code:
                raise ConfigError(f"平台编码重复：{code}")
            item.setdefault("endpoints", {})
            item.setdefault("shortName", "")
            item.setdefault("insecure", False)
            item.setdefault("follow", False)
            item["endpoints"] = normalize_endpoints(item.get("endpoints"))
            self.platform_by_code[code] = item

        self.service_by_code = {}
        for item in self.services:
            code = item.get("code")
            if not code:
                raise ConfigError("services.json 中存在缺少 code 的服务")
            if code in self.service_by_code:
                raise ConfigError(f"服务编码重复：{code}")
            item.setdefault("method", "POST")
            item.setdefault("defaultModule", None)
            item.setdefault("moduleByPlatform", {})
            item.setdefault("bodyByPlatform", {})
            item.setdefault("headers", {})
            item.setdefault("bodyFields", [])
            item.setdefault("tags", [])
            item.setdefault("enabled", True)
            item.setdefault("pick", "")
            item.setdefault("bodyFormat", "")
            item.setdefault("contextProfile", "")
            self.service_by_code[code] = item

    # -- 基础查询 ---------------------------------------------------------
    # 通用编排层不再提供「合包上下文」默认值；URL 完全由模板决定。
    # 模板表为空时所有请求都标记为「配置缺失」。

    # -- contextProfiles：按平台标签匹配 → 上下文 + URL 模板 --------------
    def _load_context_profiles(self, platform_doc: dict) -> list:
        """加载 meta.contextProfiles；空就空，没有任何内置兜底。"""
        profiles = platform_doc.get("meta", {}).get("contextProfiles") or []
        cleaned = []
        for p in profiles:
            if not isinstance(p, dict):
                continue
            name = (p.get("name") or "").strip()
            if not name:
                continue
            url_tpl = (p.get("urlTemplate") or "").strip()
            if not url_tpl:
                continue
            cleaned.append({
                "name": name,
                "context": (p.get("context") or "").strip(),
                "matchTags": [str(t).strip() for t in (p.get("matchTags") or []) if str(t).strip()],
                "priority": int(p.get("priority") or 0),
                "urlTemplate": url_tpl,
            })
        return cleaned

    def context_profile_by_name(self, name: str) -> dict | None:
        """按 name 精确取 profile（用于 service.contextProfile 覆写）。"""
        if not name:
            return None
        for p in self.context_profiles:
            if p["name"] == name:
                return p
        return None

    def effective_tags(self, platform: dict, endpoint: dict | None = None) -> set:
        """当前上下文里「生效的标签集合」= 平台自身标签 ∪ 地址条目自己的标签。

        地址条目（endpoints 列表里的一项）自带标签，比如 ["测试"] / ["正式"] / ["预发"]。
        代码不知道也不关心这些词是什么意思——「环境」只是标签的一种常见用法。
        """
        tags = set(platform.get("tags") or [])
        if endpoint:
            tags |= set(endpoint.get("tags") or [])
        return tags

    def resolve_context_profile(self, service: dict, platform: dict,
                                endpoint: dict | None = None) -> dict | None:
        """按优先级解析当前服务在该平台 + 该地址条目上的 contextProfile。

        唯一规则：模板的 matchTags **全部**出现在「生效标签集合」里即命中
        （子集判定，纯标签语义，无任何保留词）。全部命中者中取 priority 最高；
        同优先级命中多条视为配置冲突，返回 None 由上层报配置缺失。
        """
        # 1) 服务级覆写
        forced = self.context_profile_by_name((service.get("contextProfile") or "").strip())
        if forced:
            return forced
        # 2) 纯标签匹配：matchTags ⊆ 生效标签
        effective = self.effective_tags(platform, endpoint)
        matches = [p for p in self.context_profiles
                   if set(p["matchTags"]).issubset(effective)]
        if not matches:
            return None
        matches.sort(key=lambda p: p["priority"], reverse=True)
        if len(matches) > 1 and matches[0]["priority"] == matches[1]["priority"]:
            return None
        return matches[0]

    def platform(self, code: str) -> dict | None:
        return self.platform_by_code.get(code)

    def service(self, code: str) -> dict | None:
        return self.service_by_code.get(code)

    def platform_codes(self, only_enabled: bool = True) -> list:
        return [
            p["code"]
            for p in self.platforms
            if not only_enabled or p.get("enabled", True)
        ]

    def service_codes(self, only_enabled: bool = True) -> list:
        return [
            s["code"]
            for s in self.services
            if not only_enabled or s.get("enabled", True)
        ]

    # -- 对外快照（本地网页用） -------------------------------------------
    def snapshot(self) -> dict:
        return {
            "configDir": self.dir,
            "platforms": [
                {
                    "code": p["code"],
                    "name": p.get("name", p["code"]),
                    "shortName": p.get("shortName") or "",
                    "displayName": (p.get("shortName") or "").strip() or p.get("name", p["code"]),
                    "intranetIp": p.get("intranetIp") or [],
                    "note": p.get("note"),
                    "insecure": bool(p.get("insecure")),
                    "follow": bool(p.get("follow")),
                    "tags": p.get("tags") or [],
                    "endpoints": p.get("endpoints") or {},
                }
                for p in self.platforms
            ],
            "services": [
                {
                    "code": s["code"],
                    "name": s.get("name", s["code"]),
                    "method": s.get("method", "POST"),
                    "defaultModule": s.get("defaultModule"),
                    "moduleByPlatform": s.get("moduleByPlatform") or {},
                    "tags": s.get("tags") or [],
                    "body": s.get("body"),
                    "bodyFields": s.get("bodyFields") or [],
                    "responseFields": s.get("responseFields") or [],
                    "headers": s.get("headers") or {},
                    "enabled": bool(s.get("enabled", True)),
                    "pick": (s.get("pick") or "").strip(),
                    "bodyFormat": (s.get("bodyFormat") or "").strip().lower() or "",
                }
                for s in self.services
            ],
            "contextProfiles": list(self.context_profiles),
            "defaultHeaders": self.default_headers,
            "bodyFormat": self.body_format,
        }
