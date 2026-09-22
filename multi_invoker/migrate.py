"""首次启动迁移：把 config/*.json 导入 SQLite 并改名 *.imported。

原则：
- 一次跑完：标记位写入 meta('imported_from_json', 1)，下次启动跳过
- 原 JSON 文件不会被删除，改名为 *.imported 留一份以防回退
- 任一文件损坏不阻断其它文件；错误信息汇总返回
"""

from __future__ import annotations

import json
import os

from .db import Db

JSON_FILES = ("platforms.json", "services.json", "headers.json")


def ensure_migrated(config_dir: str, db_path: str) -> None:
    """幂等地把 config/*.json 导入 SQLite。"""
    if os.path.isfile(db_path):
        db = Db(db_path)
        try:
            if db.get_meta("imported_from_json"):
                db.close()
                return
        finally:
            # 已迁过；表/数据已经在
            pass

    db = Db(db_path)
    try:
        any_found = False
        for name in JSON_FILES:
            path = os.path.join(config_dir, name)
            if not os.path.isfile(path):
                continue
            any_found = True
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[migrate] 跳过 {name}：{exc}", flush=True)
                continue
            _import_one(db, name, doc)
            # 改名 *.imported，保留原文件以便回退
            new_path = path + ".imported"
            try:
                os.replace(path, new_path)
            except OSError as exc:
                print(f"[migrate] {name} 改名失败：{exc}", flush=True)
        if any_found:
            db.set_meta("imported_from_json", 1)
    finally:
        db.close()


def _import_one(db: Db, name: str, doc: dict):
    if name == "platforms.json":
        meta = doc.get("meta") or {}
        if meta.get("contextProfiles"):
            db.replace_context_profiles(meta["contextProfiles"])
        for n in (doc.get("platforms") or []):
            if isinstance(n, dict) and n.get("code"):
                db.upsert_node(n)
    elif name == "services.json":
        for s in (doc.get("services") or []):
            if isinstance(s, dict) and s.get("code"):
                db.upsert_service(s)
    elif name == "headers.json":
        db.set_headers(doc.get("defaultHeaders") or {},
                        (doc.get("bodyFormat") or "json").lower())