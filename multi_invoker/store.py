"""配置文件读写：服务定义 + 全局请求头 + 平台简称（基于 SQLite）。"""

from __future__ import annotations

import csv
import io
import json
import os
import re

from .db import Db

CODE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")  # 用于请求体字段名
PICKPATH_RE = re.compile(r"^[A-Za-z0-9_.\[\]]+$")       # 结果取值路径（点号 / 下标）

# 服务导入导出（CSV）列定义：列名 → 说明
CSV_COLUMNS = [
    ("code", "服务编码（必填）"),
    ("name", "服务名称"),
    ("tags", "分类标签，多个用 ; 分隔"),
    ("method", "请求方法，如 POST"),
    ("defaultModule", "默认应用模块（URL 模板里 {module} 时使用）"),
    ("requestFields", "请求体字段：name=值;name2=值2（值可省略）"),
    ("responseFields", "回参字段：路径=说明;路径2=说明2"),
    ("pick", "默认取值路径"),
    ("bodyFormat", "json / form，留空=全局默认"),
    ("headers", "专属请求头 JSON，如 {\"K\":\"V\"}"),
    ("moduleByPlatform", "按节点的应用模块：节点编码=模块;节点2=模块2"),
    ("enabled", "true / false，留空=true"),
]
METHODS = ("POST", "GET", "PUT", "DELETE", "PATCH", "HEAD")
BODY_FORMATS = ("json", "form")


class StoreError(Exception):
    """配置不合法或写入失败。"""


class _DbStore:
    """SQLite 持久层父类：把表数据装/拆为 `{meta, <集合>}` 形态以兼容旧 load_doc/save_doc 调用。

    子类只重写 _row_to_doc / _doc_to_row 把行字段映射到 doc 字段。
    写入使用 Db 的事务，**不再生成 .bak 文件**。
    """

    def __init__(self, db: Db, kind: str):
        # kind ∈ {"services", "nodes", "headers"}，决定 load_doc 装什么 key
        self.db = db
        self.kind = kind

    # 子类需要实现：把一行 SQLite 行映射成字典
    def _row_to_doc_row(self, row) -> dict:
        raise NotImplementedError

    # 子类需要实现：把一个字典（来自 doc）写回 SQLite
    def _write_doc(self, doc: dict) -> None:
        raise NotImplementedError

    def load_doc(self) -> dict:
        """读出 `{meta, <kind>}` 字典，与旧 JSON 形态一致。"""
        if self.kind == "services":
            return {"meta": {}, "services": [self._row_to_doc_row(r) for r in self.db._conn.execute(
                "SELECT * FROM services ORDER BY code").fetchall()]}
        if self.kind == "nodes":
            return {"meta": {}, "platforms": [self._row_to_doc_row(r) for r in self.db._conn.execute(
                "SELECT * FROM nodes ORDER BY display_order, code").fetchall()]}
        if self.kind == "headers":
            h = self.db.get_headers()
            return {"defaultHeaders": h["defaultHeaders"], "bodyFormat": h["bodyFormat"]}
        raise StoreError(f"未知 kind: {self.kind}")

    def save_doc(self, doc: dict) -> str | None:
        """整体写回；不再生成 .bak，返回值固定为 None 以保持接口兼容。"""
        self._write_doc(doc)
        return None


class ServiceStore(_DbStore):
    def __init__(self, db: Db):
        super().__init__(db, "services")
        self.db = db

    def _row_to_doc_row(self, r) -> dict:
        # r 是 sqlite3.Row；这里直接用 dict 类取字段名，但旧契约需要的是 dict。
        # 真正的转换在 load_doc 里通过 db.list_services 完成——见下方覆盖。
        raise NotImplementedError

    def load_doc(self) -> dict:
        # override：直接用 db.list_services() 拿到完整 dict（含 JSON 反序列化）
        return {"meta": {}, "services": self.db.list_services()}

    def save_doc(self, doc: dict) -> str | None:
        services = list(doc.get("services") or [])
        with self.db._lock:
            self.db._execute("DELETE FROM services")
            for svc in services:
                self.db.upsert_service(svc)
        return None

    def list_services(self) -> list:
        return list(self.load_doc().get("services") or [])

    # ------------------------------------------------------------- 校验 --
    @staticmethod
    def validate_pick(pick: str) -> list:
        errs = []
        if not pick:
            return errs
        # 点号路径只允许字母数字下划线和数字下标
        if not PICKPATH_RE.match(pick):
            errs.append("结果取值路径（pick）只能包含字母、数字、下划线、点、方括号下标")
        return errs

    @staticmethod
    def validate_response_fields(fields) -> tuple:
        """回参配置：{name, path, note, pick} 列表。返回 (errors, cleaned)。"""
        errors, cleaned = [], []
        if fields is None:
            return errors, cleaned
        if not isinstance(fields, list):
            return ["回参配置（responseFields）必须是数组"], cleaned
        seen = set()
        for i, item in enumerate(fields):
            if not isinstance(item, dict):
                errors.append(f"回参配置 #{i + 1} 不是对象")
                continue
            path = (item.get("path") or "").strip()
            name = (item.get("name") or "").strip() or path
            if not path:
                errors.append(f"回参配置 #{i + 1} 的取值路径（path）不能为空")
                continue
            if not PICKPATH_RE.match(path):
                errors.append(f"回参取值路径 {path!r} 不合法（字母/数字/下划线/点/下标）")
                continue
            if path in seen:
                errors.append(f"回参取值路径重复：{path}")
                continue
            seen.add(path)
            entry = {"name": name, "path": path}
            # 是否取值路径（出现在调用页「取值」下拉）：缺省按 true 处理（兼容旧数据）
            if item.get("pick") is not None:
                entry["pick"] = bool(item.get("pick"))
            note = (item.get("note") or "").strip()
            if note:
                entry["note"] = note
            cleaned.append(entry)
        return errors, cleaned

    @staticmethod
    def validate_body_format(bf: str) -> list:
        errs = []
        if bf and bf not in ("json", "form"):
            errs.append(f"请求体格式不支持：{bf}（仅 json / form 或留空）")
        return errs

    @staticmethod
    def validate(service: dict, original_code: str | None,
                 existing: list, platforms: list) -> tuple:
        errors, warnings = [], []
        code = (service.get("code") or "").strip()
        if not code:
            errors.append("服务编码（code）不能为空")
        elif not CODE_RE.match(code):
            errors.append("服务编码只能包含字母、数字、下划线、中划线、点，长度 1-64")

        others = {s.get("code") for s in existing}
        if original_code:
            others.discard(original_code)
        if code and code in others:
            errors.append(f"服务编码已存在：{code}（新建请换一个编码，"
                          "或先在左侧列表里选中该服务再修改）")

        method = (service.get("method") or "POST").upper()
        if method not in METHODS:
            errors.append(f"请求方法不支持：{method}（可用 {'/'.join(METHODS)}）")

        headers = service.get("headers") or {}
        if not isinstance(headers, dict):
            errors.append("请求头（headers）必须是对象")
        else:
            for key, value in headers.items():
                if not isinstance(value, str):
                    errors.append(f"请求头 {key} 的值必须是字符串")

        body_fields = service.get("bodyFields")
        has_fields = isinstance(body_fields, list) and len(body_fields) > 0
        if body_fields is not None and not isinstance(body_fields, list):
            errors.append("请求体字段（bodyFields）必须是数组")
        elif isinstance(body_fields, list):
            seen_names = set()
            for i, field in enumerate(body_fields):
                if not isinstance(field, dict):
                    errors.append(f"请求体字段 #{i + 1} 不是对象")
                    continue
                name = (field.get("name") or "").strip()
                if not name:
                    errors.append(f"请求体字段 #{i + 1} 的 name 不能为空")
                elif not NAME_RE.match(name):
                    errors.append(f"请求体字段名 {name!r} 不合法（字母/数字/下划线，不能以数字开头）")
                elif name in seen_names:
                    errors.append(f"请求体字段名重复：{name}")
                else:
                    seen_names.add(name)
                for key in ("default", "value"):
                    if key in field and not isinstance(field[key], (str, int, float, bool, type(None))):
                        errors.append(f"请求体字段 {name or '#' + str(i + 1)} 的 {key} 只支持字符串/数字/布尔/null")

        body = service.get("body")
        if isinstance(body, str) and body.strip().startswith(("{", "[")):
            try:
                json.loads(body)
            except ValueError as exc:
                errors.append(f"请求体看起来是 JSON 但格式有误：{exc}")
        if has_fields and body is not None:
            warnings.append("请求体字段（bodyFields）和 raw body 同时设置，以 bodyFields 为准")

        module_map = service.get("moduleByPlatform") or {}
        if not isinstance(module_map, dict):
            errors.append("按节点的应用模块配置（moduleByPlatform）必须是对象")
            module_map = {}
        body_map = service.get("bodyByPlatform") or {}
        if not isinstance(body_map, dict):
            errors.append("按平台请求体（bodyByPlatform）必须是对象")
            body_map = {}

        known = {p["code"] for p in platforms}
        for key in module_map:
            if key not in known:
                warnings.append(f"moduleByPlatform 里的平台编码不存在：{key}")
        for key in body_map:
            if key not in known:
                warnings.append(f"bodyByPlatform 里的平台编码不存在：{key}")

        # 注：旧版曾按 deployment=="split" 预检「缺应用模块」；现在交给 runner 按需精确报错。
        # 「是否需要应用模块」现在由 URL 模板决定（模板里含 {module} 才需要），
        # 属于配置层的事，不在 store 里按标签硬编码判断。
        # 真正的缺失会在「试算请求」时由 runner 逐平台精确报出。

        # pick / bodyFormat / 回参配置
        pick = (service.get("pick") or "").strip()
        errors.extend(ServiceStore.validate_pick(pick))
        body_fmt = (service.get("bodyFormat") or "").strip().lower()
        errors.extend(ServiceStore.validate_body_format(body_fmt))
        resp_errors, _ = ServiceStore.validate_response_fields(service.get("responseFields"))
        errors.extend(resp_errors)

        if not (service.get("name") or "").strip():
            warnings.append("服务名称为空，列表里只会显示编码")
        return errors, warnings

    # ------------------------------------------------------------- 保存 --
    def save(self, service: dict, original_code: str | None,
             platforms: list) -> dict:
        doc = self.load_doc()
        services = list(doc.get("services") or [])
        clean = self._clean(service)
        errors, warnings = self.validate(clean, original_code, services, platforms)
        if errors:
            raise StoreError("；".join(errors))

        index = None
        if original_code:
            for i, item in enumerate(services):
                if item.get("code") == original_code:
                    index = i
                    break
        if index is None:
            services.append(clean)
            action = "新增"
        else:
            services[index] = clean
            action = "更新"

        doc["services"] = services
        backup = self.save_doc(doc)
        return {"action": action, "backup": backup, "warnings": warnings,
                "services": services}

    def delete(self, code: str) -> dict:
        doc = self.load_doc()
        services = list(doc.get("services") or [])
        remaining = [s for s in services if s.get("code") != code]
        if len(remaining) == len(services):
            raise StoreError(f"没有找到服务：{code}")
        doc["services"] = remaining
        backup = self.save_doc(doc)
        return {"action": "删除", "backup": backup, "services": remaining,
                "warnings": []}

    # ------------------------------------------------------- 导入 / 导出 --
    @staticmethod
    def _split_pairs(text: str) -> list:
        """`a=1;b=2` / `a:1` → [(a, 1), (b, 2)]；允许换行分隔。"""
        items = []
        for chunk in re.split(r"[;\n\r]+", text or ""):
            chunk = chunk.strip()
            if not chunk:
                continue
            for sep in ("=", ":"):
                if sep in chunk:
                    key, value = chunk.split(sep, 1)
                    items.append((key.strip(), value.strip()))
                    break
            else:
                items.append((chunk, ""))
        return items

    @staticmethod
    def service_to_row(service: dict) -> dict:
        """服务对象 → 一行 CSV（与模板列一一对应）。"""
        field_pairs = []
        for f in service.get("bodyFields") or []:
            name = f.get("name") or ""
            value = f.get("value") if f.get("value") not in (None, "") else f.get("default")
            field_pairs.append((name, "" if value is None else value))
        if not field_pairs:
            # 没有 bodyFields 时，用扁平 body 里的键值对兜底导出
            body = service.get("body")
            if isinstance(body, dict):
                field_pairs = [(str(k), v) for k, v in body.items()
                               if isinstance(v, (str, int, float, bool))]
        fields = [f"{name}={value}" if value not in ("", None) else name
                  for name, value in field_pairs]
        responses = []
        for r in service.get("responseFields") or []:
            path = r.get("path") or ""
            note = r.get("note") or ""
            responses.append(f"{path}={note}" if note else path)
        modules = ";".join(f"{k}={v}" for k, v in (service.get("moduleByPlatform") or {}).items())
        headers = service.get("headers") or {}
        return {
            "code": service.get("code", ""),
            "name": service.get("name", ""),
            "tags": ";".join(service.get("tags") or []),
            "method": service.get("method") or "POST",
            "defaultModule": service.get("defaultModule") or "",
            "requestFields": ";".join(fields),
            "responseFields": ";".join(responses),
            "pick": service.get("pick") or "",
            "bodyFormat": service.get("bodyFormat") or "",
            "headers": json.dumps(headers, ensure_ascii=False) if headers else "",
            "moduleByPlatform": modules,
            "enabled": "true" if service.get("enabled", True) else "false",
        }

    def export_csv(self) -> str:
        """导出全部服务为 CSV 文本（含模板列说明行）。"""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow([name for name, _ in CSV_COLUMNS])
        writer.writerow([note for _, note in CSV_COLUMNS])
        for service in self.list_services():
            row = self.service_to_row(service)
            writer.writerow([row.get(name, "") for name, _ in CSV_COLUMNS])
        return buf.getvalue()

    def export_json(self) -> dict:
        return self.load_doc()

    @staticmethod
    def template_csv() -> str:
        """导入模板：表头 + 中文说明行 + 一行示例（导入时说明行会被自动跳过）。"""
        sample = {
            "code": "S_XX_XX_01",
            "name": "示例调用项",
            "tags": "示例;演示",
            "method": "POST",
            "defaultModule": "v1",
            "requestFields": "appCode=demo;pageSize=10",
            "responseFields": "msg=返回消息;data.total=总条数",
            "pick": "msg",
            "bodyFormat": "json",
            "headers": '{"X-Trace-Id":"{platformCode}-{env}"}',
            "moduleByPlatform": "node01=v1",
            "enabled": "true",
        }
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow([name for name, _ in CSV_COLUMNS])
        writer.writerow([note for _, note in CSV_COLUMNS])
        writer.writerow([sample.get(name, "") for name, _ in CSV_COLUMNS])
        return buf.getvalue()

    @staticmethod
    def csv_to_services(text: str) -> tuple:
        """CSV 文本 → (services, warnings)。表头按列名匹配，第二行若是说明行会自动跳过。"""
        text = (text or "").lstrip("\ufeff")
        rows = list(csv.reader(io.StringIO(text)))
        rows = [r for r in rows if any((c or "").strip() for c in r)]
        if not rows:
            raise StoreError("CSV 内容为空")
        header = [(c or "").strip() for c in rows[0]]
        known = [name.lower() for name, _ in CSV_COLUMNS]
        index = {}
        for i, col in enumerate(header):
            key = col.lower()
            if key in known:
                index[key] = i
        if "code" not in index:
            raise StoreError("CSV 缺少 code 列（请先下载模板）")
        body = rows[1:]
        # 模板第二行是中文说明，靠「code 列不是编码」判断并跳过
        if body and not CODE_RE.match((body[0][index["code"]] or "").strip() or ""):
            body = body[1:]

        services, warnings = [], []
        for line_no, row in enumerate(body, start=2):
            def cell(key: str) -> str:
                i = index.get(key.lower())
                if i is None or i >= len(row):
                    return ""
                return (row[i] or "").strip()

            code = cell("code")
            if not code:
                continue
            if not CODE_RE.match(code):
                warnings.append(f"第 {line_no} 行编码不合法，已跳过：{code}")
                continue

            service = {"code": code, "name": cell("name"), "method": cell("method") or "POST"}

            tags = [t.strip() for t in re.split(r"[;,，、]", cell("tags")) if t.strip()]
            if tags:
                service["tags"] = tags

            if cell("defaultModule"):
                service["defaultModule"] = cell("defaultModule")
            if cell("pick"):
                service["pick"] = cell("pick")
            body_format = cell("bodyFormat").lower()
            if body_format in ("json", "form"):
                service["bodyFormat"] = body_format

            body_fields = []
            for name, value in ServiceStore._split_pairs(cell("requestFields")):
                if not NAME_RE.match(name):
                    warnings.append(f"第 {line_no} 行请求体字段名不合法，已跳过：{name}")
                    continue
                field = {"name": name, "value": None}
                if value != "":
                    field["default"] = value
                body_fields.append(field)
            if body_fields:
                service["bodyFields"] = body_fields

            response_fields = []
            for path, note in ServiceStore._split_pairs(cell("responseFields")):
                if not PICKPATH_RE.match(path):
                    warnings.append(f"第 {line_no} 行回参路径不合法，已跳过：{path}")
                    continue
                entry = {"name": path.split(".")[-1].strip("[]") or path, "path": path}
                if note:
                    entry["note"] = note
                response_fields.append(entry)
            if response_fields:
                service["responseFields"] = response_fields

            headers_text = cell("headers")
            if headers_text:
                try:
                    parsed = json.loads(headers_text)
                    if isinstance(parsed, dict):
                        service["headers"] = {str(k): str(v) for k, v in parsed.items()}
                    else:
                        warnings.append(f"第 {line_no} 行 headers 不是 JSON 对象，已忽略")
                except ValueError:
                    warnings.append(f"第 {line_no} 行 headers 不是合法 JSON，已忽略")

            modules = {k: v for k, v in ServiceStore._split_pairs(cell("moduleByPlatform")) if v}
            if modules:
                service["moduleByPlatform"] = modules

            if cell("enabled").lower() in ("false", "0", "否", "no"):
                service["enabled"] = False
            services.append(service)

        if not services:
            raise StoreError("CSV 里没有可导入的服务行")
        return services, warnings

    def import_services(self, services: list, platforms: list,
                        mode: str = "merge", dry_run: bool = False) -> dict:
        """批量导入。mode=replace 时先清空，merge 时同编码覆盖（同编码不报「已存在」）。"""
        doc = self.load_doc()
        existing = list(doc.get("services") or [])
        if mode == "replace":
            existing = []
        by_code = {s.get("code"): s for s in existing}
        warnings, added, updated = [], 0, 0
        untouched = list(existing)

        for raw in services or []:
            if not isinstance(raw, dict):
                continue
            code = (raw.get("code") or "").strip()
            if not code:
                continue
            clean = self._clean(raw)
            errors, warns = self.validate(clean, None, list(by_code.values()), platforms)
            # 同编码覆盖时，把「重名」错误降级为覆盖
            errors = [e for e in errors if "服务编码已存在" not in e]
            if errors:
                warnings.append(f"[{code}] 未导入：" + "；".join(errors))
                continue
            warnings.extend(f"[{code}] {w}" for w in warns)
            if code in by_code:
                updated += 1
            else:
                added += 1
            by_code[code] = clean

        merged = list(by_code.values())
        if added == 0 and updated == 0:
            raise StoreError("没有导入任何服务：" + "；".join(warnings[:3]) or "内容为空")
        if dry_run:
            return {"services": untouched, "added": added, "updated": updated,
                    "backup": None, "warnings": warnings, "dryRun": True}
        doc["services"] = merged
        backup = self.save_doc(doc)
        return {"services": merged, "added": added, "updated": updated,
                "backup": backup, "warnings": warnings, "dryRun": False}

    @staticmethod
    def _clean(service: dict) -> dict:
        clean = {
            "code": (service.get("code") or "").strip(),
            "name": (service.get("name") or "").strip(),
            "method": (service.get("method") or "POST").upper(),
            "headers": {str(k): str(v) for k, v in (service.get("headers") or {}).items()},
            "defaultModule": (service.get("defaultModule") or "").strip() or None,
            "moduleByPlatform": {
                str(k): str(v).strip()
                for k, v in (service.get("moduleByPlatform") or {}).items() if str(v).strip()},
            "bodyByPlatform": {
                k: v for k, v in (service.get("bodyByPlatform") or {}).items() if v not in (None, "")},
            "enabled": bool(service.get("enabled", True)),
            "pick": (service.get("pick") or "").strip(),
            "bodyFormat": (service.get("bodyFormat") or "").strip().lower(),
        }
        body_fields = service.get("bodyFields")
        if isinstance(body_fields, list):
            cleaned_fields = []
            for field in body_fields:
                if not isinstance(field, dict):
                    continue
                name = (field.get("name") or "").strip()
                if not name:
                    continue
                item = {"name": name}
                # 只保留显式给过的键；运行时值（value）界面已不再维护，不再补空占位
                for key in ("default", "value"):
                    if key in field and field[key] not in (None, ""):
                        item[key] = field[key]
                cleaned_fields.append(item)
            if cleaned_fields:
                clean["bodyFields"] = cleaned_fields
        elif body_fields is not None:
            raise StoreError("请求体字段（bodyFields）格式不对")
        body = service.get("body")
        if isinstance(body, str):
            body = body.strip()
            if body == "":
                body = None
        if body is not None and "bodyFields" not in clean:
            clean["body"] = body
        tags = service.get("tags")
        if tags:
            clean["tags"] = [str(t) for t in tags if str(t).strip()]
        _, response_fields = ServiceStore.validate_response_fields(service.get("responseFields"))
        if response_fields:
            clean["responseFields"] = response_fields
        url_template = service.get("urlTemplate")
        if url_template:
            clean["urlTemplate"] = url_template
        return clean


class PlatformStore(_DbStore):
    """节点的完整 CRUD：增删改查、地址、内网 IP、平台级参数（基于 SQLite）。"""

    # 占位符白名单：URL 模板只能引用这些变量，其它花括号（如 JSON 字面量）原样保留
    _CONTEXT_PROFILE_PLACEHOLDERS = re.compile(
        r"\{(baseUrl|context|platformCode|platformName|env|endpointTags|module|serviceCode)\}")

    def __init__(self, db: Db):
        super().__init__(db, "nodes")
        self.db = db

    def load_doc(self) -> dict:
        # 保留旧契约：{meta: {contextProfiles: [...]}, platforms: [...]}
        return {"meta": {"contextProfiles": self.db.list_context_profiles()},
                "platforms": self.db.list_nodes()}

    def save_doc(self, doc: dict) -> str | None:
        meta = doc.get("meta") or {}
        platforms = list(doc.get("platforms") or [])
        with self.db._lock:
            if "contextProfiles" in meta:
                self.db.replace_context_profiles(meta["contextProfiles"] or [])
            self.db._execute("DELETE FROM nodes")
            for n in platforms:
                self.db.upsert_node(n)
        return None

    def list_platforms(self) -> list:
        return list(self.load_doc().get("platforms") or [])

    # ------------------------------------------------------------- meta.contextProfiles --
    @staticmethod
    def _clean_context_profile(p: dict) -> dict:
        name = (p.get("name") or "").strip()
        context = (p.get("context") or "").strip()
        url_template = (p.get("urlTemplate") or "").strip()
        match_tags = p.get("matchTags") or []
        if isinstance(match_tags, str):
            match_tags = [part.strip() for part in match_tags.split(",") if part.strip()]
        try:
            priority = int(p.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        return {
            "name": name,
            "context": context,
            "matchTags": [str(t).strip() for t in match_tags if str(t).strip()],
            "priority": priority,
            "urlTemplate": url_template,
        }

    @classmethod
    def validate_context_profiles(cls, profiles: list) -> tuple:
        """校验 contextProfiles 列表：name 唯一 / priority 是数字 / urlTemplate 占位符合法。"""
        errors = []
        cleaned = []
        seen_names = set()
        for i, p in enumerate(profiles or []):
            if not isinstance(p, dict):
                errors.append(f"contextProfiles[{i}] 不是对象")
                continue
            clean = cls._clean_context_profile(p)
            if not clean["name"]:
                errors.append(f"contextProfiles[{i}] 缺少 name")
                continue
            if clean["name"] in seen_names:
                errors.append(f"contextProfiles 中 name 重复：{clean['name']}")
                continue
            seen_names.add(clean["name"])
            if not clean["urlTemplate"]:
                errors.append(f"contextProfile [{clean['name']}] 缺少 urlTemplate")
                continue
            # 占位符白名单：模板里出现的 {xxx} 必须是我们认得的；其它报错
            for m in re.finditer(r"\{[^}]+\}", clean["urlTemplate"]):
                if not cls._CONTEXT_PROFILE_PLACEHOLDERS.fullmatch(m.group(0)):
                    errors.append(
                        f"contextProfile [{clean['name']}] 的 urlTemplate 含非法占位符：{m.group(0)}")
                    break
            cleaned.append(clean)
        return errors, cleaned

    def save_context_profiles(self, profiles: list) -> dict:
        """整体保存 meta.contextProfiles（来自 templates.html）。"""
        errs, cleaned = self.validate_context_profiles(profiles)
        if errs:
            raise StoreError("；".join(errs))
        doc = self.load_doc()
        meta = doc.setdefault("meta", {})
        meta["contextProfiles"] = cleaned
        backup = self.save_doc(doc)
        return {"profiles": cleaned, "backup": backup}

    # ------------------------------------------------------------- 校验 --
    @staticmethod
    def _clean_platform(p: dict, base: dict | None = None) -> dict:
        """整理提交上来的平台字典。

        base 为已有平台时做「合并」语义：提交里**没有出现**的字段沿用原值，
        避免调用方漏传字段（如 insecure / follow / note）把它们冲成默认值。
        """
        base = base or {}
        code = (p.get("code") or base.get("code") or "").strip()
        out = {"code": code}

        def pick(key, default):
            """提交里有该键就用提交的，否则用原值，最后才用 default。"""
            if key in p:
                return p.get(key)
            if key in base:
                return base.get(key)
            return default

        out["name"] = (pick("name", "") or "").strip() or code
        out["shortName"] = (pick("shortName", "") or "").strip()

        ips = pick("intranetIp", []) or []
        if isinstance(ips, str):
            ips = [part.strip() for part in ips.split(",") if part.strip()]
        out["intranetIp"] = [str(ip).strip() for ip in ips if str(ip).strip()]

        out["insecure"] = bool(pick("insecure", False))
        out["follow"] = bool(pick("follow", False))
        out["anyScheme"] = bool(pick("anyScheme", False))
        out["note"] = (pick("note", "") or "").strip() or None

        tags = pick("tags", []) or []
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",") if part.strip()]
        out["tags"] = [str(t).strip() for t in tags if str(t).strip()]

        # endpoints 是「带标签的地址条目列表」，只保留 baseUrl（portalPath 已废弃）
        raw_eps = pick("endpoints", []) or []
        if isinstance(raw_eps, dict):
            raw_eps = [{"tags": [k], "baseUrl": (v or {}).get("baseUrl")}
                       for k, v in raw_eps.items() if isinstance(v, dict)]
        endpoints = []
        for src in raw_eps if isinstance(raw_eps, list) else []:
            if not isinstance(src, dict):
                continue
            base_url = str(src.get("baseUrl") or "").strip()
            if not base_url:
                continue
            tags = src.get("tags") or []
            if isinstance(tags, str):
                tags = [x.strip() for x in tags.split(",")]
            endpoints.append({
                "tags": [str(x).strip() for x in tags if str(x).strip()],
                "baseUrl": base_url,
            })
        out["endpoints"] = endpoints
        return out

    @staticmethod
    def validate_one(clean: dict) -> tuple:
        """返回 (errors, warnings)；warnings 暂未使用，预留。"""
        errors = []
        if not clean["code"]:
            errors.append("平台编码（code）不能为空")
        elif not CODE_RE.match(clean["code"]):
            errors.append("平台编码只能包含字母、数字、下划线、中划线、点，长度 1-64")
        if not clean["name"]:
            errors.append("平台名称不能为空")
        # 应用模块的来源由 URL 模板的 {module} 占位符决定——
        # 不在这里按部署类型硬编码预检，缺失会在「试算请求」时由 runner 精确报出。
        if not clean["endpoints"]:
            errors.append("至少需要一个地址条目（含 baseUrl）")
        for i, ep in enumerate(clean["endpoints"]):
            url = ep["baseUrl"]
            if not url.startswith(("http://", "https://")):
                label = "/".join(ep.get("tags") or []) or f"#{i + 1}"
                errors.append(f"地址条目 {label} 必须以 http:// 或 https:// 开头：{url}")
        return errors, []

    # ------------------------------------------------------------- 保存 --
    def upsert(self, items: list, services: list) -> dict:
        """items 里每条带 _deleted:true 即删除，否则按 code 增/改。

        删除前检查是否被 service.moduleByPlatform / bodyByPlatform 引用。
        """
        doc = self.load_doc()
        platforms = list(doc.get("platforms") or [])
        existing = {p.get("code"): p for p in platforms}

        deleted = []
        errors = []
        warnings = []
        new_platforms = []

        for raw in items:
            raw_code = (raw.get("code") or "").strip()
            # 已有平台做合并式更新：提交里没出现的字段保留原值
            clean = self._clean_platform(raw, existing.get(raw_code))
            code = clean["code"]
            if not code:
                continue
            if raw.get("_deleted"):
                if code in existing:
                    used = self._used_by(code, services)
                    if used:
                        errors.append(
                            f"平台 {code} 正被服务引用（{used}），无法删除；"
                            "请先在「服务定义」中取消引用")
                        continue
                    del existing[code]
                    deleted.append(code)
                continue
            errs, warns = self.validate_one(clean)
            if errs:
                errors.append(f"[{code}] " + "；".join(errs))
                continue
            warnings.extend(warns)
            existing[code] = clean

        if errors:
            raise StoreError("；".join(errors))

        new_platforms = list(existing.values())
        doc["platforms"] = new_platforms
        backup = self.save_doc(doc)
        return {"platforms": new_platforms, "backup": backup,
                "warnings": warnings, "deleted": deleted}

    @staticmethod
    def _used_by(code: str, services: list) -> str:
        """返回对该平台的引用描述（用于删除保护），无引用则返回 ''。"""
        used = []
        for s in services or []:
            mb = (s.get("moduleByPlatform") or {}).get(code)
            bb = (s.get("bodyByPlatform") or {}).get(code)
            if mb is not None:
                used.append(f"{s.get('code')}.moduleByPlatform")
            if bb is not None:
                used.append(f"{s.get('code')}.bodyByPlatform")
        return ", ".join(used)

    # ------------------------------------------------------------- 兼容旧接口 --
    def update_short_names(self, items: list) -> dict:
        """旧接口：只改 shortName / note。保留向后兼容。"""
        doc = self.load_doc()
        platforms = list(doc.get("platforms") or [])
        by_code = {p.get("code"): p for p in platforms}
        warnings = []
        for item in items:
            code = (item.get("code") or "").strip()
            if not code:
                continue
            plat = by_code.get(code)
            if plat is None:
                warnings.append(f"未知平台编码：{code}")
                continue
            plat["shortName"] = (item.get("shortName") or "").strip()
            if "note" in item and item["note"] is not None:
                plat["note"] = (item["note"] or "").strip()
        doc["platforms"] = platforms
        backup = self.save_doc(doc)
        return {"platforms": platforms, "backup": backup, "warnings": warnings}


class HeaderStore(_DbStore):
    """全局默认请求头与 body 格式（基于 SQLite）。"""

    def __init__(self, db: Db):
        super().__init__(db, "headers")
        self.db = db

    def load_doc(self) -> dict:
        h = self.db.get_headers()
        return {"defaultHeaders": h["defaultHeaders"], "bodyFormat": h["bodyFormat"]}

    def save_doc(self, doc: dict) -> str | None:
        self.db.set_headers(doc.get("defaultHeaders") or {},
                            (doc.get("bodyFormat") or "json").lower())
        return None

    def save(self, default_headers: dict, body_format: str) -> dict:
        bf = (body_format or "json").lower()
        if bf not in BODY_FORMATS:
            raise StoreError(f"请求体格式不支持：{bf}（仅 {' / '.join(BODY_FORMATS)}）")
        cleaned = {}
        for k, v in (default_headers or {}).items():
            key = (k or "").strip()
            if not key:
                continue
            cleaned[key] = "" if v is None else str(v)
        doc = {"defaultHeaders": cleaned, "bodyFormat": bf}
        backup = self.save_doc(doc)
        return {"defaultHeaders": cleaned, "bodyFormat": bf, "backup": backup}