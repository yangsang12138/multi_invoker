"""SQLite 持久层：建表、读写、JSON 列互转、连接管理。

标准库 `sqlite3` 即可，不引入第三方 ORM。

约束：
- 一份数据库文件 = `config/app.db`，所有表共用
- 嵌套结构（tags / endpoints / headers / bodyFields 等）一律存为 JSON 字符串
- 顺序敏感的列表（endpoints、bodyFields、responseFields、contextProfiles）用 ord 字段保存
- 启用 WAL + 外键，原子事务
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS nodes(
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    short_name   TEXT,
    note         TEXT,
    insecure     INTEGER DEFAULT 0,
    follow       INTEGER DEFAULT 0,
    any_scheme   INTEGER DEFAULT 0,
    module       TEXT,
    intranet_ip  TEXT,                   -- JSON 数组
    tags         TEXT,                   -- JSON 数组
    display_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS node_endpoints(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    node_code TEXT NOT NULL REFERENCES nodes(code) ON DELETE CASCADE,
    tags      TEXT,                     -- JSON 数组
    base_url  TEXT NOT NULL,
    ord       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_node_endpoints_node
    ON node_endpoints(node_code, ord);

CREATE TABLE IF NOT EXISTS services(
    code              TEXT PRIMARY KEY,
    name              TEXT,
    method            TEXT DEFAULT 'POST',
    default_module    TEXT,
    body              TEXT,            -- 旧版 raw body 字符串
    pick              TEXT,
    body_format       TEXT,
    enabled           INTEGER DEFAULT 1,
    tags              TEXT,            -- JSON 数组
    headers           TEXT,            -- JSON 对象
    module_by_platform TEXT,           -- JSON 对象（按节点覆盖应用模块）
    body_by_platform  TEXT,            -- JSON 对象
    body_fields       TEXT,            -- JSON 数组
    response_fields   TEXT             -- JSON 数组
);

CREATE TABLE IF NOT EXISTS context_profiles(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    context      TEXT,
    match_tags   TEXT,                  -- JSON 数组
    priority     INTEGER DEFAULT 0,
    url_template TEXT,
    ord          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS headers_row(
    id              INTEGER PRIMARY KEY CHECK(id = 1),
    default_headers TEXT,              -- JSON 对象
    body_format     TEXT
);
"""


def _jloads(value):
    if value is None or value == "":
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _jdumps(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class Db:
    """SQLite 持久层封装。"""

    def __init__(self, db_path: str):
        self.path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # ThreadingHTTPServer 下每个请求一个线程；sqlite3 连接默认线程独占，
        # 必须加 check_same_thread=False + 进程级 RLock 才能多线程安全使用。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    # --------------------------------------------------------- 初始化 --
    def _init_schema(self):
        self._executescript(SCHEMA)
        # 默认 headers 单行表：插入占位行
        self._execute(
            "INSERT OR IGNORE INTO headers_row(id, default_headers, body_format) "
            "VALUES (1, ?, ?)",
            (_jdumps({"Content-Type": "application/json", "Accept": "*/*"}), "json"),
        )

    def close(self):
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # --------------------------------------------------------- 锁 + 事务 --
    def _execute(self, sql, params=()):
        """加锁执行单条 SQL。"""
        with self._lock:
            return self._conn.execute(sql, params)

    def _executemany(self, sql, seq):
        with self._lock:
            return self._conn.executemany(sql, seq)

    def _executescript(self, sql):
        with self._lock:
            return self._conn.executescript(sql)

    def transaction(self):
        """返回 sqlite3 上下文管理器；用法：`with db.transaction(): ...`"""
        return self._lock

    # ============================================================ meta
    def get_meta(self, key: str, default=None):
        row = self._execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return _jloads(row["value"]) if row and row["value"] else default

    def set_meta(self, key: str, value: Any):
        self._execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, _jdumps(value)),
        )

    # ============================================================ nodes
    def list_nodes(self) -> list:
        rows = self._execute(
            "SELECT * FROM nodes ORDER BY display_order, code"
        ).fetchall()
        ep_rows = self._execute(
            "SELECT * FROM node_endpoints ORDER BY node_code, ord, id"
        ).fetchall()
        eps_by_code: dict[str, list] = {}
        for ep in ep_rows:
            eps_by_code.setdefault(ep["node_code"], []).append({
                "tags": _jloads(ep["tags"]) or [],
                "baseUrl": ep["base_url"],
            })
        out = []
        for r in rows:
            out.append({
                "code": r["code"],
                "name": r["name"],
                "shortName": r["short_name"] or "",
                "note": r["note"],
                "insecure": bool(r["insecure"]),
                "follow": bool(r["follow"]),
                "anyScheme": bool(r["any_scheme"]),
                "module": r["module"],
                "intranetIp": _jloads(r["intranet_ip"]) or [],
                "tags": _jloads(r["tags"]) or [],
                "endpoints": eps_by_code.get(r["code"], []),
            })
        return out

    def upsert_node(self, node: dict):
        """整体替换一个节点及其 endpoints。"""
        code = node["code"]
        with self._lock:
            self._execute(
                "INSERT INTO nodes("
                " code, name, short_name, note,"
                " insecure, follow, any_scheme, module, intranet_ip, tags, display_order"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET "
                "  name=excluded.name, short_name=excluded.short_name,"
                "  note=excluded.note,"
                "  insecure=excluded.insecure, follow=excluded.follow,"
                "  any_scheme=excluded.any_scheme, module=excluded.module,"
                "  intranet_ip=excluded.intranet_ip, tags=excluded.tags",
                (
                    code,
                    node.get("name") or code,
                    node.get("shortName") or "",
                    node.get("note"),
                    1 if node.get("insecure") else 0,
                    1 if node.get("follow") else 0,
                    1 if node.get("anyScheme") else 0,
                    node.get("module"),
                    _jdumps(node.get("intranetIp") or []),
                    _jdumps(node.get("tags") or []),
                    0,
                ),
            )
            # endpoints：先删后插，保证顺序与提交一致
            self._execute("DELETE FROM node_endpoints WHERE node_code = ?", (code,))
            for i, ep in enumerate(node.get("endpoints") or []):
                if not isinstance(ep, dict):
                    continue
                base = (ep.get("baseUrl") or "").strip()
                if not base:
                    continue
                self._execute(
                    "INSERT INTO node_endpoints(node_code, tags, base_url, ord) "
                    "VALUES(?,?,?,?)",
                    (code, _jdumps(ep.get("tags") or []), base, i),
                )

    def delete_node(self, code: str):
        with self._lock:
            self._execute("DELETE FROM nodes WHERE code = ?", (code,))

    def get_node(self, code: str) -> dict | None:
        for n in self.list_nodes():
            if n["code"] == code:
                return n
        return None

    # ============================================================ services
    def list_services(self) -> list:
        rows = self._execute(
            "SELECT * FROM services ORDER BY code"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "code": r["code"],
                "name": r["name"],
                "method": r["method"] or "POST",
                "defaultModule": r["default_module"],
                "body": r["body"],
                "pick": r["pick"] or "",
                "bodyFormat": r["body_format"] or "",
                "enabled": bool(r["enabled"]),
                "tags": _jloads(r["tags"]) or [],
                "headers": _jloads(r["headers"]) or {},
                "moduleByPlatform": _jloads(r["module_by_platform"]) or {},
                "bodyByPlatform": _jloads(r["body_by_platform"]) or {},
                "bodyFields": _jloads(r["body_fields"]) or [],
                "responseFields": _jloads(r["response_fields"]) or [],
            })
        return out

    def upsert_service(self, svc: dict):
        code = svc["code"]
        with self._lock:
            self._execute(
                "INSERT INTO services("
                " code, name, method, default_module, body, pick, body_format,"
                " enabled, tags, headers, module_by_platform, body_by_platform,"
                " body_fields, response_fields"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET "
                "  name=excluded.name, method=excluded.method,"
                "  default_module=excluded.default_module, body=excluded.body,"
                "  pick=excluded.pick, body_format=excluded.body_format,"
                "  enabled=excluded.enabled, tags=excluded.tags,"
                "  headers=excluded.headers,"
                "  module_by_platform=excluded.module_by_platform,"
                "  body_by_platform=excluded.body_by_platform,"
                "  body_fields=excluded.body_fields,"
                "  response_fields=excluded.response_fields",
                (
                    code,
                    svc.get("name") or "",
                    svc.get("method") or "POST",
                    svc.get("defaultModule"),
                    svc.get("body"),
                    svc.get("pick") or "",
                    svc.get("bodyFormat") or "",
                    1 if svc.get("enabled", True) else 0,
                    _jdumps(svc.get("tags") or []),
                    _jdumps(svc.get("headers") or {}),
                    _jdumps(svc.get("moduleByPlatform") or {}),
                    _jdumps(svc.get("bodyByPlatform") or {}),
                    _jdumps(svc.get("bodyFields") or []),
                    _jdumps(svc.get("responseFields") or []),
                ),
            )

    def delete_service(self, code: str):
        with self._lock:
            self._execute("DELETE FROM services WHERE code = ?", (code,))

    # ============================================================ context_profiles
    def list_context_profiles(self) -> list:
        rows = self._execute(
            "SELECT * FROM context_profiles ORDER BY ord, id"
        ).fetchall()
        return [
            {
                "name": r["name"],
                "context": r["context"] or "",
                "matchTags": _jloads(r["match_tags"]) or [],
                "priority": int(r["priority"] or 0),
                "urlTemplate": r["url_template"] or "",
            }
            for r in rows
        ]

    def replace_context_profiles(self, profiles: list):
        with self._lock:
            self._execute("DELETE FROM context_profiles")
            for i, p in enumerate(profiles or []):
                if not isinstance(p, dict):
                    continue
                self._execute(
                    "INSERT INTO context_profiles"
                    "(name, context, match_tags, priority, url_template, ord) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        p.get("name") or "",
                        p.get("context") or "",
                        _jdumps(p.get("matchTags") or []),
                        int(p.get("priority") or 0),
                        p.get("urlTemplate") or "",
                        i,
                    ),
                )

    # ============================================================ headers
    def get_headers(self) -> dict:
        row = self._execute(
            "SELECT default_headers, body_format FROM headers_row WHERE id = 1"
        ).fetchone()
        if not row:
            return {"defaultHeaders": {}, "bodyFormat": "json"}
        return {
            "defaultHeaders": _jloads(row["default_headers"]) or {},
            "bodyFormat": (row["body_format"] or "json").lower(),
        }

    def set_headers(self, default_headers: dict, body_format: str):
        with self._lock:
            self._execute(
                "UPDATE headers_row SET default_headers = ?, body_format = ? "
                "WHERE id = 1",
                (_jdumps(default_headers or {}), (body_format or "json").lower()),
            )