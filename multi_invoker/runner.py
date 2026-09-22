"""查询编排：URL 组装、应用模块解析、并发执行、结果汇总。"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import urlencode

from . import client
from . import config as config_module
from .config import Config


# 只认这几个占位符，其它花括号（JSON 结构）原样保留
_PLACEHOLDER_RE = re.compile(
    r"\{(baseUrl|context|platformCode|platformName|env|endpointTags|module|serviceCode)\}")


class QueryError(Exception):
    """无法组装请求（配置缺失等）。"""


@dataclass
class Target:
    """一次「节点 + 环境」的查询目标。"""

    platform_code: str
    platform_name: str
    url: str
    endpoint_tags: tuple = ()
    platform_tags: tuple = ()
    context: str | None = None          # 实际代入 URL 的应用上下文
    context_source: str | None = None   # 它来自哪条 URL 规则 / 哪个模块配置
    module: str | None = None
    module_source: str | None = None
    connect_host: str | None = None
    insecure: bool = False
    follow: bool = False
    any_scheme: bool = False

    def base(self) -> dict:
        return {
            "platformCode": self.platform_code,
            "platformName": self.platform_name,
            "endpointTags": list(self.endpoint_tags),
            "endpointLabel": " · ".join(self.endpoint_tags),
            "platformTags": list(self.platform_tags),
            "context": self.context,
            "contextSource": self.context_source,
            "url": self.url,
            "module": self.module,
            "moduleSource": self.module_source,
            "connectedTo": self.connect_host,
            "tlsInsecure": self.insecure,
            "followRedirect": self.follow,
            "anyScheme": self.any_scheme,
        }


def resolve_module(config: Config, service: dict, platform: dict):
    """应用模块解析（URL 模板里 context 为 <module> 时使用），返回 (模块名, 来源)。

    模块是**调用项**的概念，节点不参与；规则只有两条：
      1. service.moduleByPlatform[节点编码] — 该调用项对此节点单独指定
      2. service.defaultModule — 该调用项的默认模块（适用于所有用 {module} 的节点）
    """
    code = platform["code"]
    per_platform = (service.get("moduleByPlatform") or {}).get(code)
    if per_platform:
        return per_platform, "service.moduleByPlatform"
    if service.get("defaultModule"):
        return service["defaultModule"], "service.defaultModule"
    return None, None


def _profile_requires_module(profile: dict) -> bool:
    """profile 的 urlTemplate 或 context 含 {module} / <module> → 需要模块名。"""
    tpl = profile.get("urlTemplate") or ""
    ctx = profile.get("context") or ""
    return "{module}" in tpl or ctx == "<module>"


def build_url(config: Config, service: dict, platform: dict, endpoint: dict,
              module: str | None, profile: dict | None = None) -> str:
    """按 contextProfile 拼 URL。

    解析优先级：service.contextProfile > 标签子集命中模板 > 报错。
    """
    base_url = str(endpoint.get("baseUrl") or "").strip().rstrip("/")
    if not base_url:
        raise QueryError(f"平台 {platform['code']} 的这个地址条目没有 baseUrl")
    if profile is None:
        profile = config.resolve_context_profile(service, platform, endpoint)
    if profile is None:
        raise QueryError(
            f"平台 {platform['code']} 没有匹配到 URL 模板，"
            f"请检查标签是否与 platforms.json 的 contextProfiles 对得上"
        )
    context_value = profile.get("context") or ""
    if context_value == "<module>":
        context_value = module or ""
    tags = list(endpoint.get("tags") or [])
    return render_template(
        profile["urlTemplate"],
        {
            "baseUrl": base_url,
            "context": context_value,
            "module": module or "",
            "serviceCode": service["code"],
            "platformCode": platform["code"],
            # {env} 作为历史占位符保留，填该地址条目的标签（多个用逗号连）
            "env": ",".join(tags),
            "endpointTags": ",".join(tags),
        },
    )


def build_targets(config: Config, service: dict, platform_codes: list,
                  tag_filter: list | None = None, use_ip: bool = False):
    """生成查询目标：每个「平台 × 地址条目」一个目标。

    tag_filter 为空表示不筛选；否则只保留标签命中其中任一（或全部）的地址条目。
    下面按「全部命中」语义：筛选词必须都在条目的生效标签集合里。
    """
    want = {str(x).strip() for x in (tag_filter or []) if str(x).strip()}
    targets, problems = [], []
    for code in platform_codes:
        platform = config.platform(code)
        if platform is None:
            problems.append(_static_result(
                platform_code=code, platform_name=code,
                endpoint_tags=(), url="-", status="config_error",
                error=f"platforms.json 中没有平台：{code}"))
            continue
        # deployment 已不再使用；URL 完全由 contextProfiles 决定
        connect_host = None
        if use_ip:
            ips = platform.get("intranetIp") or []
            if ips:
                connect_host = ips[0]
        endpoints = platform.get("endpoints") or []
        if not endpoints:
            problems.append(_static_result(
                platform_code=code, platform_name=platform.get("name", code),
 endpoint_tags=(), url="-",
                status="config_error", error="该平台没有配置任何地址条目"))
            continue
        for endpoint in endpoints:
            etags = tuple(endpoint.get("tags") or [])
            if want and not want.issubset(config.effective_tags(platform, endpoint)):
                continue
            profile = config.resolve_context_profile(service, platform, endpoint)
            if profile is None:
                problems.append(_static_result(
                    platform_code=code,
                    platform_name=platform.get("name", code),
 endpoint_tags=etags, url="-",
                    status="config_error",
                    error=("没有匹配到 URL 模板：平台 + 该地址条目的标签与 "
                           "platforms.json 的 contextProfiles 对不上")))
                continue
            # 应用上下文 = 该 URL 规则里 context 的取值：
            #   规则写死（如 v1）→ 直接用；规则写 <module> → 取调用项配的应用模块
            module, module_source = (None, None)
            context_value = (profile.get("context") or "").strip()
            if context_value == "<module>":
                module, module_source = resolve_module(config, service, platform)
                if not module:
                    problems.append(_static_result(
                        platform_code=code,
                        platform_name=platform.get("name", code),
 endpoint_tags=etags, url="-",
                        status="config_error",
                        error=("当前 URL 模板需要应用模块，但服务未配置："
                               "请在 services.json 的 defaultModule 或 "
                               f"moduleByPlatform.{code} 中指定")))
                    continue
                context_value = module
                context_source = module_source
            else:
                context_source = f"rule:{profile['name']}"
            try:
                url = build_url(config, service, platform, endpoint, module, profile)
            except QueryError as exc:
                problems.append(_static_result(
                    platform_code=code,
                    platform_name=platform.get("name", code),
 endpoint_tags=etags, url="-",
                    status="config_error", error=str(exc), module=module,
                    context=context_value, context_source=context_source))
                continue
            display = (platform.get("shortName") or "").strip() or platform.get("name", code)
            targets.append(Target(
                platform_code=code,
                platform_name=display,

                url=url,
                endpoint_tags=etags,
                platform_tags=tuple(platform.get("tags") or ()),
                context=context_value,
                context_source=context_source,
                module=module,
                module_source=module_source,
                connect_host=connect_host,
                insecure=bool(platform.get("insecure")),
                follow=bool(platform.get("follow")),
                any_scheme=bool(platform.get("anyScheme")),
            ))
    return targets, problems


def _static_result(platform_code, platform_name, endpoint_tags, url,
                   status, error, module=None, platform_tags=(),
                   context=None, context_source=None, any_scheme=False):
    return {
        "platformCode": platform_code,
        "platformName": platform_name,
        "endpointTags": list(endpoint_tags or ()),
        "endpointLabel": " · ".join(endpoint_tags or ()),
        "platformTags": list(platform_tags or ()),
        "context": context,
        "contextSource": context_source,
        "url": url,
        "module": module,
        "moduleSource": None,
        "connectedTo": None,
        "anyScheme": any_scheme,
        "schemeSwitched": False,
        "status": status,
        "error": error,
        "httpStatus": None,
        "elapsedMs": None,
        "body": None,
        "encoding": None,
        "truncated": False,
        "value": None,
    }


def render_template(text: str, values_or_service, platform: dict | None = None,
                    env: str | None = None, module: str | None = None) -> str:
    """只替换已知占位符，JSON 里的花括号原样保留。

    （不能用 str.format：`{"appCode":"x"}` 会被当成格式字段而报 KeyError。）

    支持两种调用形态（向后兼容）：
      - 新形态：render_template(text, values: dict)
      - 旧形态：render_template(text, service: dict, platform: dict, env: str, module: str | None)
    """
    if isinstance(values_or_service, dict) and platform is None and env is None and module is None:
        values = dict(values_or_service)
    else:
        service = values_or_service
        plat = platform or {}
        values = {
            "baseUrl": "",
            "context": "",
            "platformCode": plat.get("code", ""),
            "platformName": plat.get("name", plat.get("code", "")),
            "env": env or "",
            "endpointTags": env or "",
            "module": module or "",
            "serviceCode": service.get("code", ""),
        }
    return _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), "")), text)


def merge_body_fields(fields: list, overrides: dict | None = None) -> dict:
    """把 bodyFields 合并成 {name: value}；overrides 中存在且非空的值优先。"""
    out: dict = {}
    overrides = overrides or {}
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        name = (field.get("name") or "").strip()
        if not name:
            continue
        if name in overrides and overrides[name] not in (None, ""):
            out[name] = overrides[name]
            continue
        for key in ("value", "default"):
            if field.get(key) not in (None, ""):
                out[name] = field[key]
                break
    return out


def parse_form_text(text: str) -> dict:
    """`key=value&k=v` 或 `key=value` 一行一组 → dict（值皆为 str）。"""
    if text is None:
        return {}
    pairs = []
    for chunk in re.split(r"&", text):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        pairs.append((k.strip(), v.strip()))
    from urllib.parse import unquote
    return {unquote(k): unquote(v) for k, v in pairs if k}


def parse_override_text(text: str, body_format: str):
    """运行时一次性覆盖：JSON 走 JSON.loads；form 走 key=value 解析。"""
    text = (text or "").strip()
    if not text:
        return None
    if body_format == "form":
        return parse_form_text(text)
    try:
        return json.loads(text)
    except ValueError:
        # 看起来不像 JSON 时按 form 兜底
        return parse_form_text(text)


def encode_body(value, body_format: str):
    """dict/list/str → (bytes, decoded_dict_for_GET)。"""
    if value is None:
        return None, None
    if body_format == "form":
        if isinstance(value, dict):
            encoded = urlencode({k: "" if v is None else str(v) for k, v in value.items()}).encode("utf-8")
            return encoded, {k: "" if v is None else str(v) for k, v in value.items()}
        if isinstance(value, str):
            d = parse_form_text(value)
            encoded = urlencode(d).encode("utf-8")
            return encoded, d
        return str(value).encode("utf-8"), None
    # json
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value.encode("utf-8"), None
        return json.dumps(decoded, ensure_ascii=False).encode("utf-8"), \
            decoded if isinstance(decoded, dict) else None
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return encoded, value if isinstance(value, dict) else None


def build_body(service: dict, platform: dict, endpoint_tags, module: str | None,
               options: dict) -> tuple:
    """按 override → bodyFields → bodyByPlatform/body 的优先级组装请求体。

    endpoint_tags 只用于 {env} / {endpointTags} 占位符（历史 {env} 保留为别名）。
    返回 (body_bytes, decoded_dict_for_GET, source)。
    """
    body_format = options.get("body_format") or "json"

    override = options.get("body")
    if isinstance(override, dict):
        raw = override
        source = "options.body(dict)"
    elif isinstance(override, str) and override.strip():
        raw = parse_override_text(override, body_format)
        source = "options.body(text)"
    else:
        fields = service.get("bodyFields") or []
        if isinstance(fields, list) and fields:
            raw = merge_body_fields(fields, options.get("bodyOverrides") or {})
            source = "service.bodyFields"
        else:
            plat_code = platform.get("code") if platform else None
            if plat_code and plat_code in (service.get("bodyByPlatform") or {}):
                raw = service["bodyByPlatform"][plat_code]
                source = "service.bodyByPlatform"
            else:
                raw = service.get("body")
                source = "service.body"

    if raw is None or raw == "":
        return None, None, source

    if isinstance(raw, str):
        rendered = render_template(raw, service, platform or {},
                                   ",".join(endpoint_tags or ()), module)
        bytes_, decoded = encode_body(rendered, body_format)
    else:
        bytes_, decoded = encode_body(raw, body_format)
    return bytes_, decoded, source


def collect_pick_paths(service: dict, options: dict) -> list[str]:
    """决定本次调用要取哪些回参路径，返回一组点号路径列表。

    语义（2026-09 起调整）：
    - 调用页 PICK 留空 / 为空字符串 → 返回 []（按"用户明确不要取值"处理）。
    - 调用页 PICK 非空：
        - 命中叶子且 pick !== false → 仅取该叶子。
        - 命中父项（有勾选子孙）或命中"未存但有勾选子孙的祖先"→ 展开成所有勾选子孙。
        - 与 responseFields 完全无关（自定义路径）→ 单路径兜底。
    - service.responseFields 为空：临时 pick 仍按单路径兜底（保留旧 PICK 行为）。
    """
    fields = (service.get("responseFields") or [])
    pick_overlay = (options.get("pick") or "").strip()
    base = (service.get("pick") or "").strip()

    if not fields:
        # 没配回参结构 → 临时 pick 单独取一个值（旧行为）
        fallback = pick_overlay or base
        return [fallback] if fallback else []

    # 1. 调用页没传 PICK → 用户明确不要取值
    if not pick_overlay:
        return []

    # 2. 调用页传了 PICK → 命中父项/叶子/祖先/完全无关，分类处理
    descendants = [
        (rf.get("path") or "").strip()
        for rf in fields
        if (rf.get("pick") is not False)
        and ((rf.get("path") or "").startswith(pick_overlay + "."))
    ]
    is_self_picked = any(
        (rf.get("path") or "").strip() == pick_overlay and rf.get("pick") is not False
        for rf in fields
    )
    if is_self_picked and not descendants:
        return [pick_overlay]
    if descendants:
        return descendants
    # 3. 与 responseFields 完全无关 → 单路径兜底
    return [pick_overlay]


def collect_pick_values(service: dict, options: dict, body_text: str) -> list[dict]:
    """按 collect_pick_paths 返回的路径，逐条到 body_text 里取值。
    返回 [{name, path, value}] 列表；某条取不到仍保留（value=null），便于前端感知「该字段缺失」。"""
    paths = collect_pick_paths(service, options)
    if not paths:
        return []
    by_path = {}
    for rf in (service.get("responseFields") or []):
        path = (rf.get("path") or "").strip()
        if path:
            by_path.setdefault(path, rf)
    out = []
    for path in paths:
        v = client.pick_json_path(body_text, path)
        rf = by_path.get(path) or {}
        out.append({
            "name": (rf.get("name") or path.split(".")[-1] or path).strip(),
            "path": path,
            "value": v,
        })
    return out


def build_headers(service: dict, options: dict, default_headers: dict | None = None) -> dict:
    """全局默认 → 服务 headers → 一次性追加（最后写赢）。

    bodyFormat=form 时，若当前 Content-Type 形如 application/json，会自动改成
    application/x-www-form-urlencoded（其它自定义 Content-Type 保留）。
    """
    body_format = options.get("body_format") or "json"
    headers: dict = {}
    if default_headers:
        headers.update(default_headers)
    headers.update(service.get("headers") or {})
    headers.update(options.get("headers") or {})
    if "Content-Type" not in headers:
        headers["Content-Type"] = (
            "application/x-www-form-urlencoded" if body_format == "form"
            else "application/json")
    elif body_format == "form" and "json" in headers.get("Content-Type", "").lower():
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers


def _target_query(config: Config, service: dict, target: Target, options: dict) -> dict:
    method = (options.get("method") or service.get("method") or "POST").upper()
    headers = build_headers(service, options, config.default_headers)
    body, decoded_for_get, body_source = build_body(
        service, config.platform(target.platform_code),
        target.endpoint_tags, target.module, options)
    url = target.url

    note = None
    if method in ("GET", "HEAD") and decoded_for_get:
        url = url + ("&" if "?" in url else "?") + urlencode(decoded_for_get)
        note = "GET/HEAD 方法：请求体已转换为查询串"
        body = None
        headers.pop("Content-Type", None)
    elif method in ("GET", "HEAD") and body:
        body = None

    attempts = max(1, int(options.get("retries", 0)) + 1)
    insecure = bool(options.get("insecure")) or target.insecure
    follow = bool(options.get("follow")) or target.follow
    any_scheme = bool(options.get("anyScheme")) or target.any_scheme
    cookies = options.get("cookies")  # dict | str | None；交给 client.request 注入

    result = target.base()
    result.update({
        "serviceCode": service.get("code", ""),
        "serviceName": service.get("name", service.get("code", "")),
        "method": method,
        "url": url,
        "status": "pending",
        "error": None,
        "httpStatus": None,
        "elapsedMs": None,
        "body": None,
        "encoding": None,
        "truncated": False,
        "redirects": 0,
        "value": None,
        "values": [],
        "bodySource": body_source,
        "bodyFormat": options.get("body_format") or "json",
        "anyScheme": any_scheme,
        "schemeSwitched": False,
    })

    last_error = None
    for attempt in range(attempts):
        try:
            resp = client.request(
                url,
                method=method,
                headers=headers,
                body=body,
                timeout=float(options.get("timeout", 15)),
                connect_host=target.connect_host,
                insecure=insecure,
                max_body=int(options.get("maxBody", client.MAX_BODY_CHARS)),
                follow_redirects=follow,
                cookies=cookies,
                any_scheme=any_scheme,
            )
        except client.RequestError as exc:
            last_error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
                continue
            result["status"] = "network_error"
            result["error"] = last_error
            return result

        result["httpStatus"] = resp.status
        result["url"] = resp.url
        result["elapsedMs"] = round(resp.elapsed_ms, 1)
        result["body"] = resp.body
        result["encoding"] = resp.encoding
        result["truncated"] = resp.truncated
        result["redirects"] = resp.redirects
        result["reason"] = resp.reason
        result["schemeSwitched"] = bool(getattr(resp, "scheme_switched", False))
        result["status"] = "ok" if resp.ok else "http_error"
        if not resp.ok:
            message = f"HTTP {resp.status} {resp.reason}"
            if resp.location:
                message += f" → Location: {resp.location}（可加 --follow 跟随跳转）"
            result["error"] = message
        pick_path = options.get("pick") or service.get("pick") or ""
        # 取值：按 responseFields 里勾选了「可取值」的回参逐项取值；
        # 临时 pick 命中父项时展开成所有子孙（见 collect_pick_values / collect_pick_paths）。
        values = collect_pick_values(service, options, resp.body)
        if values:
            result["values"] = values
            # 向后兼容：CLI / 老接口仍读 result.value；只在「只取了一个值」时填充
            if len(values) == 1:
                result["value"] = values[0]["value"]
        elif pick_path:
            # 兜底：服务里没配 responseFields，沿用旧单值行为（CLI --pick / 老 services.json）
            result["value"] = client.pick_json_path(resp.body, pick_path)
        return result

    result["status"] = "network_error"
    result["error"] = last_error or "未知错误"
    return result


def normalize_service(service: dict, fallback_code: str = "") -> dict:
    """补全服务定义的可选字段，供 config / 网页即时试算共用。"""
    service = dict(service or {})
    service["code"] = (service.get("code") or fallback_code or "").strip()
    service.setdefault("name", service["code"])
    service.setdefault("method", "POST")
    service.setdefault("headers", {})
    service.setdefault("defaultModule", None)
    service.setdefault("moduleByPlatform", {})
    service.setdefault("bodyByPlatform", {})
    service.setdefault("bodyFields", [])
    service.setdefault("tags", [])
    service.setdefault("pick", "")
    service.setdefault("bodyFormat", "")
    service.setdefault("enabled", True)
    return service


def apply_body_field_values(service: dict, values: dict | None) -> dict:
    """把网页上按字段填写的运行时值写进服务定义副本。

    - 服务用 bodyFields：写入 field.value（build_body 的优先级 value → default）；
    - 服务只用 raw body（对象）：把值覆盖到 body 的同名字段上；
    - 空值不覆盖，仍走原始配置。
    """
    values = {k: v for k, v in (values or {}).items() if v not in (None, "")}
    if not values:
        return service
    fields = service.get("bodyFields")
    body = service.get("body")
    if not (isinstance(fields, list) and fields) and not isinstance(body, dict):
        return service
    svc = json.loads(json.dumps(service, ensure_ascii=False))
    if isinstance(svc.get("bodyFields"), list) and svc["bodyFields"]:
        for field in svc["bodyFields"]:
            name = (field.get("name") or "").strip()
            if name in values:
                field["value"] = values[name]
    elif isinstance(svc.get("body"), dict):
        for name, value in values.items():
            svc["body"][name] = value
    return svc


def _prepare(config: Config, service_code: str, platform_codes: list,
             tag_filter: list, options: dict, service_override: dict | None = None):
    if service_override:
        service = normalize_service(service_override, service_code)
        if not service["code"]:
            raise QueryError("服务定义缺少 code")
    else:
        service = config.service(service_code)
        if service is None:
            raise QueryError(f"services.json 中没有这个查询项：{service_code}")
    service = apply_body_field_values(service, options.get("bodyFieldValues"))
    # bodyFormat 优先级：单次覆盖 → 服务级 → 全局
    options.setdefault("body_format",
                       (service.get("bodyFormat") or "") or config.body_format)
    options.setdefault("default_headers", config.default_headers)
    # pick 语义区分：
    #   - 网页明确传 "" → 用户主动选「不取值」,不要用 service.pick 兜底
    #   - CLI / 调用方完全没传 pick（key 不存在）→ 用 service.pick 作为默认
    if "pick" not in options:
        options["pick"] = service.get("pick") or ""
    tags = [str(x).strip() for x in (tag_filter or []) if str(x).strip()]
    targets, problems = build_targets(
        config, service, platform_codes, tags,
        use_ip=bool(options.get("useIp")))
    return service, targets, problems, tags


def _service_payload(service: dict, options: dict) -> dict:
    body_override = options.get("body")
    return {
        "code": service["code"],
        "name": service.get("name", service["code"]),
        "method": (options.get("method") or service.get("method") or "POST").upper(),
        "bodyFormat": options.get("body_format") or "json",
        "bodyFields": service.get("bodyFields") or [],
        "pick": (options.get("pick") or service.get("pick") or "").strip(),
        "bodySource": ("options.body" if (isinstance(body_override, str) and body_override.strip())
                       else "service.bodyFields" if service.get("bodyFields")
                       else "service.body"),
    }


def _stats(results: list) -> dict:
    stats = {"total": len(results), "ok": 0, "httpError": 0,
             "networkError": 0, "configError": 0}
    for item in results:
        key = {"ok": "ok", "http_error": "httpError",
               "network_error": "networkError",
               "config_error": "configError"}.get(item["status"])
        if key:
            stats[key] += 1
    return stats


def _build_plan_row(config: Config, service: dict, target: Target,
                    options: dict) -> dict:
    """组装一条「即将发出的请求」（不发网络请求）。"""
    platform = config.platform(target.platform_code) or {"code": target.platform_code}
    method = (options.get("method") or service.get("method") or "POST").upper()
    headers = build_headers(service, options, config.default_headers)
    body_bytes, decoded_for_get, body_source = build_body(
        service, platform, target.endpoint_tags, target.module, options)
    url = target.url
    note = None
    if method in ("GET", "HEAD") and decoded_for_get:
        url = url + ("&" if "?" in url else "?") + urlencode(decoded_for_get)
        note = "GET/HEAD 方法：请求体已转换为查询串"
        body_bytes = None
        headers.pop("Content-Type", None)
    row = target.base()
    row.update({
        "method": method,
        "url": url,
        "headers": headers,
        "requestBody": body_bytes.decode("utf-8") if body_bytes else None,
        "bodySource": body_source,
        "bodyFormat": options.get("body_format") or "json",
        "pick": (options.get("pick") or service.get("pick") or "").strip(),
        "note": note,
        "anyScheme": bool(options.get("anyScheme")) or target.any_scheme,
        "status": "preview",
        "error": None,
    })
    return row


def preview_query(config: Config, service_code: str, platform_codes: list,
                  tag_filter: list | None = None,
                  service_override: dict | None = None, **options) -> dict:
    """预览：列出每个「平台 + 地址条目」将要发出的方法、URL、请求头、请求体。

    与真正查询共用 build_targets / build_body，保证「预览看到的」就是「实际发出的」。
    """
    service, targets, problems, tags = _prepare(
        config, service_code, platform_codes, tag_filter or [], options, service_override)

    results = [_build_plan_row(config, service, t, options) for t in targets]
    for item in problems:
        item.update({"method": (options.get("method") or service.get("method") or "POST").upper(),
                     "headers": build_headers(service, options, config.default_headers),
                     "requestBody": None, "bodySource": None,
                     "bodyFormat": options.get("body_format") or "json",
                     "note": None})
        results.append(item)

    return {
        "service": _service_payload(service, options),
        "tagFilter": tags,
        "results": results,
        "stats": {"total": len(results),
                  "configError": sum(1 for r in results if r["status"] == "config_error")},
    }


def preview_query_batch(config: Config, service_codes: list[str],
                        platform_codes: list, tag_filter: list | None = None,
                        **options) -> dict:
    """多服务预览：每个服务的预览结果都打 serviceCode/serviceName，UI 可按服务分组。"""
    if not service_codes:
        raise QueryError("至少选择一个调用项")
    services_payload: list[dict] = []
    all_results: list[dict] = []
    tags = [str(x).strip() for x in (tag_filter or []) if str(x).strip()]
    options = dict(options)
    if options.get("pick") is None:
        options["pick"] = ""
    for sc in service_codes:
        service = config.service(sc)
        if service is None:
            services_payload.append({"code": sc, "name": sc, "missing": True})
            all_results.append({
                "platformCode": "-", "platformName": "-",
                "endpointTags": [], "endpointLabel": "",
                "platformTags": [], "context": None, "contextSource": None,
                "url": None, "module": None, "method": "-",
                "status": "config_error",
                "error": f"services.json 中没有这个查询项：{sc}",
                "headers": {}, "requestBody": None, "bodyFormat": "json",
                "serviceCode": sc, "serviceName": sc, "note": None,
            })
            continue
        services_payload.append(_service_payload(service, options))
        targets, problems = build_targets(
            config, service, platform_codes, tags,
            use_ip=bool(options.get("useIp")))
        for t in targets:
            row = _build_plan_row(config, service, t, options)
            row["serviceCode"] = service.get("code", "")
            row["serviceName"] = service.get("name", service.get("code", ""))
            all_results.append(row)
        for p in problems:
            p.update({"method": (options.get("method") or service.get("method") or "POST").upper(),
                      "headers": build_headers(service, options, config.default_headers),
                      "requestBody": None, "bodySource": None,
                      "bodyFormat": options.get("body_format") or "json",
                      "note": None,
                      "serviceCode": service.get("code", ""),
                      "serviceName": service.get("name", service.get("code", ""))})
            all_results.append(p)
    return {
        "serviceCodes": list(service_codes),
        "services": services_payload,
        "tagFilter": tags,
        "results": all_results,
        "stats": {"total": len(all_results),
                  "configError": sum(1 for r in all_results if r["status"] == "config_error")},
    }


def run_query(config: Config, service_code: str, platform_codes: list,
              tag_filter: list | None = None, **options) -> dict:
    """执行一次统一调用，返回 {service, tagFilter, results, stats}（命令行用）。"""
    service, targets, problems, tags = _prepare(
        config, service_code, platform_codes, tag_filter or [], options,
        options.pop("service_override", None))
    started = time.time()
    if targets:
        workers = max(1, min(int(options.get("concurrency", 8)), len(targets)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda t: _target_query(config, service, t, options), targets))
    else:
        results = []
    results.extend(problems)
    return {
        "service": _service_payload(service, options),
        "tagFilter": tags,
        "elapsedMs": round((time.time() - started) * 1000.0, 1),
        "stats": _stats(results),
        "results": results,
    }


def iter_query(config: Config, service_code: str, platform_codes: list,
               tag_filter: list | None = None, **options):
    """流式执行：先 meta，再每完成一个目标就产出一条 result，最后 done。

    网页界面用它做「边查边出」，不必等最慢的平台。
    产出格式：{"type": "meta"|"result"|"done", "data": {...}}
    """
    service, targets, problems, tags = _prepare(
        config, service_code, platform_codes, tag_filter or [], options,
        options.pop("service_override", None))
    started = time.time()
    yield {"type": "meta", "data": {
        "service": _service_payload(service, options),
        "tagFilter": tags,
        "total": len(targets) + len(problems),
        "platformCount": len(platform_codes),
    }}

    results = []
    if targets:
        workers = max(1, min(int(options.get("concurrency", 8)), len(targets)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_target_query, config, service, t, options)
                       for t in targets]
            for future in as_completed(futures):
                try:
                    item = future.result()
                except Exception as exc:  # 单个目标出意外也不能拖垮整次查询
                    item = _static_result(
                        platform_code="-", platform_name="-",
                        endpoint_tags=(), url="-", status="network_error",
                        error=f"内部错误：{exc}")
                results.append(item)
                yield {"type": "result", "data": item}

    for item in problems:
        results.append(item)
        yield {"type": "result", "data": item}

    yield {"type": "done", "data": {
        "stats": _stats(results),
        "elapsedMs": round((time.time() - started) * 1000.0, 1),
    }}


def iter_query_batch(config: Config, service_codes: list[str], platform_codes: list,
                     tag_filter: list | None = None, **options):
    """多服务流式执行：先 meta（报 serviceCodes 与总条数），再逐服务内部并发 → 每完成
    一条 result 就立刻 yield，最后 done。result.data 会同时带 serviceCode / serviceName。

    共用同一份 options（body / pick / headers / cookie / --use-ip 等），但
    apply_body_field_values 会在每个服务自己的 bodyFields 上分别执行,所以「请求体」框对
    所有选中服务都生效,而默认值仍取自各服务自己的 bodyFields。
    """
    if not service_codes:
        raise QueryError("至少选择一个调用项")

    # 校验 + 收集 services 与 problems（配置缺失的服务直接以 result 条目出）
    services: list[dict] = []
    all_problems: list[tuple[str, list]] = []  # (service_code, problems)
    for sc in service_codes:
        service = config.service(sc)
        if service is None:
            all_problems.append((sc, [_static_result(
                platform_code="-", platform_name="-",
                endpoint_tags=(), url="-", status="config_error",
                error=f"services.json 中没有这个查询项：{sc}")]))
            continue
        services.append(service)

    started = time.time()
    options = dict(options)
    # pick 默认值兜底到各服务的 svc.pick（仅当调用方未给 pick 时）
    if options.get("pick") is None:
        # pick 留空 → batch 模式下也按「用户不要取值」处理
        options["pick"] = ""

    # 先把所有服务的 target 数与 problems 数加起来，作为 meta.total
    # 「找不到的服务码」也要计入 total 与 serviceCodes,否则 meta 与实际产出的条数对不上
    total = sum(len(probs) for _, probs in all_problems)
    per_service_targets: list[tuple[dict, list, list]] = []
    tags: list[str] = [str(x).strip() for x in (tag_filter or []) if str(x).strip()]
    for service in services:
        service_targets, problems = build_targets(
            config, service, platform_codes, tags,
            use_ip=bool(options.get("useIp")))
        per_service_targets.append((service, service_targets, problems))
        total += len(service_targets) + len(problems)

    services_payload = [_service_payload(s, options) for s in services]
    yield {"type": "meta", "data": {
        "serviceCodes": list(service_codes),
        "services": services_payload,
        "tagFilter": tags,
        "total": total,
        "platformCount": len(platform_codes),
    }}

    all_results: list[dict] = []

    def _service_problems(svc, problems):
        # 给 problems 补上 serviceCode / serviceName
        out = []
        for p in problems:
            p["serviceCode"] = svc.get("code", "")
            p["serviceName"] = svc.get("name", svc.get("code", ""))
            out.append(p)
        return out

    def _worker(svc, target):
        return _target_query(config, svc, target, options)

    for service, targets, problems in per_service_targets:
        if problems:
            for p in _service_problems(service, problems):
                all_results.append(p)
                yield {"type": "result", "data": p}
        if targets:
            workers = max(1, min(int(options.get("concurrency", 8)), len(targets)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_worker, service, t) for t in targets]
                for fut in as_completed(futures):
                    try:
                        item = fut.result()
                    except Exception as exc:
                        item = _static_result(
                            platform_code="-", platform_name="-",
                            endpoint_tags=(), url="-", status="network_error",
                            error=f"内部错误：{exc}")
                        item["serviceCode"] = service.get("code", "")
                        item["serviceName"] = service.get("name", service.get("code", ""))
                    all_results.append(item)
                    yield {"type": "result", "data": item}

    # 把「找不到服务」的 problems 也一起丢出去（补上 serviceCode/serviceName 便于前端分组）
    for sc, probs in all_problems:
        for p in probs:
            p["serviceCode"] = sc
            p["serviceName"] = sc
            all_results.append(p)
            yield {"type": "result", "data": p}

    yield {"type": "done", "data": {
        "stats": _stats(all_results),
        "elapsedMs": round((time.time() - started) * 1000.0, 1),
    }}


def run_query_batch(config: Config, service_codes: list[str], platform_codes: list,
                    tag_filter: list | None = None, **options) -> dict:
    """多服务一次性返回版本（CLI / 老接口用）。"""
    results: list[dict] = []
    services_payload: list[dict] = []
    started = time.time()
    for sc in service_codes:
        service = config.service(sc)
        if service is None:
            missing = _static_result(
                platform_code="-", platform_name="-",
                endpoint_tags=(), url="-", status="config_error",
                error=f"services.json 中没有这个查询项：{sc}")
            missing["serviceCode"] = sc
            missing["serviceName"] = sc
            results.append(missing)
            continue
        services_payload.append(_service_payload(service, options))
        targets, problems = build_targets(
            config, service, platform_codes, tag_filter or [],
            use_ip=bool(options.get("useIp")))
        if targets:
            workers = max(1, min(int(options.get("concurrency", 8)), len(targets)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                rs = list(pool.map(lambda t: _target_query(config, service, t, options), targets))
            results.extend(rs)
        for p in problems:
            p["serviceCode"] = service.get("code", "")
            p["serviceName"] = service.get("name", service.get("code", ""))
            results.append(p)
    return {
        "serviceCodes": list(service_codes),
        "services": services_payload,
        "tagFilter": tag_filter or [],
        "elapsedMs": round((time.time() - started) * 1000.0, 1),
        "stats": _stats(results),
        "results": results,
    }


def preview_platform_urls(config: Config, service_code: str,
                          platform_code: str) -> dict:
    """给「URL 预览」用：列出某服务在某域名（平台）下、每个地址条目的最终 URL。

    返回 {"platform": {...}, "service": {...}, "items": [{endpointTags, url, profile, error}]}
    """
    service = config.service(service_code)
    if service is None:
        raise QueryError(f"services.json 中没有这个服务：{service_code}")
    platform = config.platform(platform_code)
    if platform is None:
        raise QueryError(f"platforms.json 中没有这个域名：{platform_code}")

    items = []
    for endpoint in (platform.get("endpoints") or []):
        etags = list(endpoint.get("tags") or [])
        profile = config.resolve_context_profile(service, platform, endpoint)
        if profile is None:
            items.append({"endpointTags": etags, "url": None, "profile": None,
                          "error": "没有匹配到 URL 模板"})
            continue
        module, module_source = (None, None)
        if _profile_requires_module(profile):
            module, module_source = resolve_module(config, service, platform)
            if not module:
                items.append({"endpointTags": etags, "url": None,
                              "profile": profile["name"], "module": None,
                              "error": "该模板需要应用模块，但服务未配置 defaultModule"})
                continue
        try:
            url = build_url(config, service, platform, endpoint, module, profile)
        except QueryError as exc:
            items.append({"endpointTags": etags, "url": None,
                          "profile": profile["name"], "error": str(exc)})
            continue
        items.append({
            "endpointTags": etags,
            "url": url,
            "profile": profile["name"],
            "module": module,
            "moduleSource": module_source,
            "error": None,
        })
    return {
        "platform": {
            "code": platform["code"],
            "name": platform.get("name", platform["code"]),
            "shortName": platform.get("shortName") or "",
            "tags": list(platform.get("tags") or []),
        },
        "service": {"code": service["code"], "name": service.get("name", service["code"])},
        "items": items,
    }
