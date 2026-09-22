"""命令行入口：平台清单 / 服务清单 / 统一调用 / 启动本地网页。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import unicodedata

from . import runner, runtime
from .config import DEFAULT_CONFIG_DIR, Config, ConfigError

STATUS_LABEL = {
    "ok": "成功",
    "http_error": "HTTP错误",
    "network_error": "网络失败",
    "config_error": "配置缺失",
    "pending": "未执行",
}

CSV_FIELDS = [
    ("serviceCode", "调用项"),
    ("platformCode", "节点编码"),
    ("platformName", "节点名称"),
    ("endpointLabel", "地址标签"),
    ("method", "方法"),
    ("module", "应用模块"),
    ("url", "请求地址"),
    ("statusLabel", "状态"),
    ("httpStatus", "HTTP状态码"),
    ("elapsedMs", "耗时(ms)"),
    ("value", "取值"),
    ("error", "错误信息"),
    ("body", "响应内容"),
]


# ---------------------------------------------------------------- 文本表格 --
def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    space = " " * max(0, width - _width(text))
    return space + text if align == "right" else text + space


def _cell(text, limit=44) -> str:
    text = "" if text is None else str(text).replace("\n", " ").replace("\r", " ")
    text = " ".join(text.split())
    if _width(text) > limit:
        out, used = "", 0
        for ch in text:
            w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            if used + w > limit - 1:
                break
            out += ch
            used += w
        return out + "…"
    return text


def print_table(headers, rows, aligns=None):
    aligns = aligns or ["left"] * len(headers)
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _width(_cell(cell, 60)))
    line = "  ".join(_pad(h, widths[i], aligns[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * _width(line))
    for row in rows:
        print("  ".join(_pad(_cell(c, 60), widths[i], aligns[i])
                        for i, c in enumerate(row)))


# ------------------------------------------------------------------ 子命令 --
def _endpoint_url(platform: dict, label: str) -> str:
    """从「带标签的地址条目列表」里取指定标签的 baseUrl；取不到返回 "-"。

    endpoints 已由 config.normalize_endpoints 统一为
    [{"tags": ["测试"], "baseUrl": "..."}]，不能再当字典用。
    """
    for endpoint in platform.get("endpoints") or []:
        if label in (endpoint.get("tags") or []):
            return endpoint.get("baseUrl") or "-"
    return "-"


def cmd_platforms(config: Config, args) -> int:
    rows = []
    for platform in config.platforms:
        rows.append([
            platform["code"],
            (platform.get("shortName") or "").strip() or "-",
            platform.get("name", ""),
            _endpoint_url(platform, "测试"),
            _endpoint_url(platform, "正式"),
            "/".join(platform.get("intranetIp") or []) or "-",
            ",".join(platform.get("tags") or []) or "-",
        ])
    print_table(["编码", "简称", "平台名称", "测试域名", "生产域名", "内网IP", "标签"], rows)
    # 地址条目标签不是「测试/正式」的平台不会出现在上面两列，单独说明，避免信息被静默丢掉
    unmatched = []
    for platform in config.platforms:
        endpoints = platform.get("endpoints") or []
        tags = {t for endpoint in endpoints for t in (endpoint.get("tags") or [])}
        if endpoints and not ({"测试", "正式"} & tags):
            unmatched.append(f"{platform['code']}({'/'.join(sorted(tags)) or '无标签'})")
    if unmatched:
        print(f"\n另有 {len(unmatched)} 个平台的地址未打 测试/正式 标签，未在上表两列展示："
              f"{'，'.join(unmatched)}")
    print(f"\n共 {len(rows)} 个节点")
    return 0


def cmd_services(config: Config, args) -> int:
    rows = []
    for service in config.services:
        fields = service.get("bodyFields") or []
        body_kind = ("字段×%d" % len(fields)) if fields else ("raw body" if service.get("body") is not None else "-")
        rows.append([
            service["code"],
            service.get("name", ""),
            service.get("method", "POST"),
            service.get("defaultModule") or "-",
            service.get("bodyFormat") or f"(全局:{config.body_format})",
            body_kind,
            service.get("pick") or "-",
            ",".join(service.get("tags") or []) or "-",
        ])
    print_table(["调用项", "名称", "方法", "默认模块", "请求体格式", "请求体", "取值路径", "标签"], rows)
    print(f"\n全局默认请求体格式：{config.body_format}　"
          f"全局默认请求头：{', '.join(f'{k}={v}' for k, v in config.default_headers.items())}")
    return 0


def cmd_headers(config: Config, args) -> int:
    print(f"配置目录：{config.dir}")
    print(f"请求体格式：{config.body_format}")
    print("全局默认请求头：")
    for k, v in config.default_headers.items():
        print(f"  {k}: {v}")
    print("\n（如需修改，直接编辑 config/headers.json）")
    return 0


def _select_platforms(config: Config, args) -> list:
    if args.platform:
        codes = list(args.platform)
    else:
        codes = [p["code"] for p in config.platforms]
    excluded = set(args.exclude or [])
    codes = [c for c in codes if c not in excluded]
    unknown = [c for c in codes if config.platform(c) is None]
    if unknown:
        raise ConfigError(f"未知节点编码：{', '.join(unknown)}")
    if not codes:
        raise ConfigError("没有匹配到任何节点")
    return codes


def _print_results(payload: dict, args) -> None:
    service = payload["service"]
    stats = payload["stats"]
    print(f"\n■ 调用项 {service['code']}  {service.get('name') or ''}"
          f"  [{service['method']}]  请求体格式：{service.get('bodyFormat') or '-'}  "
          f"数据源：{service.get('bodySource') or 'service.body'}  "
          f"取值路径：{service.get('pick') or '(未配置)'}")
    rows = []
    for item in payload["results"]:
        detail = item.get("value")
        if detail is None:
            detail = item.get("error") or _cell(item.get("body"), 44)
        rows.append([
            item["platformCode"],
            item["platformName"],
            item.get("endpointLabel", ""),
            STATUS_LABEL.get(item["status"], item["status"]),
            item.get("httpStatus") or "-",
            f"{item['elapsedMs']:.0f}" if item.get("elapsedMs") is not None else "-",
            detail,
        ])
    print_table(["编码", "节点", "地址标签", "状态", "HTTP", "耗时ms", "结果/错误"],
                rows, ["left", "left", "left", "left", "right", "right", "left"])
    print(f"合计 {stats['total']}：成功 {stats['ok']}，HTTP错误 {stats['httpError']}，"
          f"网络失败 {stats['networkError']}，配置缺失 {stats['configError']}"
          f"（总耗时 {payload['elapsedMs']:.0f}ms）")

    # 「忽略协议」生效时，明确告知实际用了哪个协议；降级到 http 要提示明文风险
    # 只统计真正开了该开关的目标：跟随跳转导致的协议变化属正常行为，不在此列
    switched = [item for item in payload["results"]
                if item.get("schemeSwitched") and item.get("anyScheme")]
    if switched:
        ups = [i["platformCode"] for i in switched if str(i.get("url", "")).startswith("https://")]
        downs = [i["platformCode"] for i in switched if str(i.get("url", "")).startswith("http://")]
        parts = []
        if ups:
            parts.append(f"升级到 https：{'、'.join(ups)}")
        if downs:
            parts.append(f"降级到 http（明文传输）：{'、'.join(downs)}")
        print(f"[忽略协议] {len(switched)} 个目标的实际协议与配置不同 —— " + "；".join(parts))
        if downs:
            print("[忽略协议] 注意：降级为 http 后请求体与响应均为明文，"
                  "请确认该平台确实允许，或改用 https 地址。")

    if args.full:
        for item in payload["results"]:
            if item.get("body"):
                print(f"\n--- {item['platformCode']} / {item.get('endpointLabel', '')} / {item['url']}")
                print(item["body"])
            elif item.get("error"):
                print(f"\n--- {item['platformCode']} / {item.get('endpointLabel', '')} / {item['url']}")
                print(f"[{STATUS_LABEL.get(item['status'])}] {item['error']}")


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _export(payloads: list, args) -> None:
    rows = []
    for payload in payloads:
        for item in payload["results"]:
            row = dict(item)
            row["serviceCode"] = payload["service"]["code"]
            row["statusLabel"] = STATUS_LABEL.get(item["status"], item["status"])
            rows.append(row)
    if args.csv:
        _ensure_parent(args.csv)
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([label for _, label in CSV_FIELDS])
            for row in rows:
                writer.writerow([row.get(key, "") for key, _ in CSV_FIELDS])
        print(f"\n已导出 CSV：{os.path.abspath(args.csv)}（{len(rows)} 行）")
    if args.json:
        _ensure_parent(args.json)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"queries": payloads}, fh, ensure_ascii=False, indent=2)
        print(f"已导出 JSON：{os.path.abspath(args.json)}")


def _collect_headers(args) -> dict:
    headers = {}
    for raw in args.header or []:
        if ":" not in raw:
            raise ConfigError(f"--header 格式应为 K:V，收到：{raw}")
        key, value = raw.split(":", 1)
        headers[key.strip()] = value.strip()
    return headers


def _resolve_tag_filter(value) -> list:
    """把 --tag（可重复）解析成地址条目标签筛选列表；空 = 不限。"""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(x).strip() for x in value if str(x).strip()]


def _resolve_cookies(args) -> str | None:
    """合并 --cookie 与 --cookie-file，返回 "k=v; k=v" 字符串。

    - 同名 key：--cookie 后出现覆盖先出现的；文件先加载，再叠加命令行。
    - 解析失败抛 ConfigError（不静默吞）。
    - 返回 None 表示不注入。
    """
    from .client import parse_cookie_text
    out: dict = {}
    file_paths = list(getattr(args, "cookie_file", []) or [])
    for fp in file_paths:
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            raise ConfigError(f"--cookie-file 读取失败：{fp}（{exc}）") from None
        loaded = parse_cookie_text(text)
        if not loaded and text.strip():
            raise ConfigError(
                f"--cookie-file 内容不是合法的 k=v 形式：{fp}")
        out.update(loaded)
    inline = getattr(args, "cookie", None)
    if inline:
        loaded = parse_cookie_text(inline)
        if not loaded:
            raise ConfigError(f"--cookie 不是合法的 k=v 形式：{inline!r}")
        out.update(loaded)
    if not out:
        return None
    return "; ".join(f"{k}={v}" for k, v in out.items())


def cmd_plan(config: Config, args) -> int:
    """只组装请求、不发送，用于人工核对要调的接口对不对。"""
    tag_filter = _resolve_tag_filter(args.tag)
    platform_codes = _select_platforms(config, args)
    options = {
        "headers": _collect_headers(args),
        "body": args.body,
        "method": args.method,
        "anyScheme": args.any_scheme,
        "cookies": _resolve_cookies(args),
    }
    print(f"地址标签：{'/'.join(tag_filter) or '全部'}　"
          f"平台：{len(platform_codes)} 个　请求体格式：{config.body_format}　"
          f"全局请求头：{len(config.default_headers)} 条　（仅预览，不会发送任何请求）")
    for code in args.services:
        local_options = dict(options)
        # CLI --pick 不传时,plan 也要尊重服务默认 pick
        if local_options.get("pick") is None:
            local_options["pick"] = (config.service(code) or {}).get("pick") or ""
        payload = runner.preview_query(config, code, platform_codes, tag_filter, **local_options)
        service = payload["service"]
        pick = service.get("pick") or "-"
        print(f"\n■ 调用项 {service['code']}  {service.get('name') or ''}  "
              f"[{service['method']}]  请求体格式：{service.get('bodyFormat') or config.body_format}  "
              f"数据源：{service.get('bodySource') or 'service.body'}  取值路径：{pick}")
        rows = []
        for item in payload["results"]:
            if item["status"] == "config_error":
                rows.append([item["platformCode"], item["platformName"],
                             item.get("endpointLabel", ""), "-", "配置缺失", "-", item.get("error") or ""])
                continue
            source = item.get("moduleSource") or ""
            source = {"service.moduleByPlatform": "按节点指定",
                      "service.defaultModule": "调用项默认模块"}.get(source, source)
            rows.append([
                item["platformCode"], item["platformName"],
                item.get("endpointLabel", ""), item["method"], item.get("module") or "-",
                source, item["url"],
            ])
        print_table(["节点", "简称", "地址标签", "方法", "应用上下文", "来源", "请求地址"],
                    rows, ["left", "left", "left", "left", "left", "left", "left"])
        opened = [item["platformCode"] for item in payload["results"] if item.get("anyScheme")]
        if opened:
            print(f"[忽略协议] 已开启（{'、'.join(opened)}）：http/https 只作首选，"
                  f"同址换协议的跳转会自动跟随并保留方法与请求体；协议层失败会换协议重试一次。")

        if args.full:
            for item in payload["results"]:
                if item["status"] == "config_error":
                    print(f"\n--- {item['platformCode']} / {item.get('endpointLabel', '')}：{item.get('error')}")
                    continue
                print(f"\n--- {item['platformCode']} / {item.get('endpointLabel', '')}")
                print(f"{item['method']} {item['url']}")
                for key, value in (item.get("headers") or {}).items():
                    print(f"{key}: {value}")
                if item.get("note"):
                    print(f"# {item['note']}")
                if item.get("requestBody"):
                    print(item["requestBody"])
        bodies = {item.get("requestBody") for item in payload["results"]}
        if len(bodies) == 1:
            only = next(iter(bodies))
            print(f"请求体（各平台一致）：{only or '无'}")
        elif bodies:
            print("各平台请求体不同，加 --full 查看每个平台的具体请求体")


def cmd_query(config: Config, args) -> int:
    tag_filter = _resolve_tag_filter(args.tag)
    platform_codes = _select_platforms(config, args)
    headers = _collect_headers(args)
    cookies = _resolve_cookies(args)
    options = {
        "timeout": args.timeout,
        "concurrency": args.concurrency,
        "retries": args.retries,
        "useIp": args.use_ip,
        "insecure": args.insecure or args.use_ip,
        "headers": headers,
        "body": args.body,
        "method": args.method,
        "pick": args.pick,
        "maxBody": args.max_body,
        "follow": args.follow,
        "anyScheme": args.any_scheme,
        "cookies": cookies,
    }
    print(f"地址标签：{'/'.join(tag_filter) or '全部'}　"
          f"平台：{len(platform_codes)} 个　并发：{options['concurrency']}　"
          f"超时：{options['timeout']}s"
          + ("　[内网IP直连]" if args.use_ip else "")
          + ("　[忽略证书]" if options["insecure"] else "")
          + ("　[忽略协议]" if args.any_scheme else "")
          + ("　[Cookie 注入]" if cookies else ""))
    payloads = []
    for code in args.services:
        # CLI 不传 --pick 时用服务配置的 pick 作默认；与网页"不勾 = 不取值"语义一致
        if options.get("pick") is None:
            default_pick = (config.service(code) or {}).get("pick") or ""
            local_options = dict(options); local_options["pick"] = default_pick
        else:
            local_options = options
        payload = runner.run_query(config, code, platform_codes, tag_filter, **local_options)
        payloads.append(payload)
        _print_results(payload, args)
    _export(payloads, args)
    return 0


def cmd_serve(config: Config, args) -> int:
    # 网页只是编排层入口，初次启动时配置为空是正常状态（用户后续在 UI 上配）。
    # 空配置的放行在 main() 里通过 MULTI_INVOKER_ALLOW_EMPTY_CONFIG 统一处理。
    from .server import serve
    return serve(config, host=args.host, port=args.port, open_browser=not args.no_open,
                 allow_origin=getattr(args, "allow_origin", None))


def cmd_info(config: Config, args) -> int:
    """打印运行形态与路径，单文件部署排障用。"""
    info = runtime.snapshot()
    print("Multi-Service Invoker")
    print(f"  版本      {info['version']}")
    print(f"  运行形态  {info['runtime']}")
    print(f"  可执行    {info['archive'] or sys.argv[0]}")
    print(f"  Python    {info['python']}（{info['platform']}）")
    print(f"  数据根目录 {info['home']}")
    print(f"  配置目录  {config.dir}")
    print(f"  网页资源  {info['webDir'] or '（内嵌在可执行文件里）'}")
    print(f"  平台 {len(config.platforms)} 个，调用项 {len(config.services)} 个，"
          f"URL 模板 {len(config.context_profiles)} 条")
    if info["configDir"] != config.dir:
        print(f"  提示：默认配置目录为 {info['configDir']}，当前被 --config-dir 覆盖。")
    return 0


# -------------------------------------------------------------------- 入口 --
def _parse_port(value: str) -> int:
    """`--port` 的取值：数字，或 `auto`（等价于 0，由系统挑一个空闲端口）。

    返回 0 表示「自动分配」；server.py 绑定后会打印实际拿到的端口。
    """
    text = str(value).strip().lower()
    if text in ("auto", "0"):
        return 0
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"端口必须是数字或 auto，收到：{value!r}") from None
    if not (1 <= number <= 65535):
        raise argparse.ArgumentTypeError(f"端口必须在 1-65535 之间，收到：{number}")
    return number


def _add_serve_args(target, suppress_defaults: bool) -> None:
    """把 serve 的四个参数挂到某个 parser 上。

    顶层 parser 与 `serve` 子命令共用同一组定义，唯一的区别是
    `suppress_defaults`：子命令用 SUPPRESS，避免它在「用户没写这个参数」时
    把顶层已经解析出来的值（如 `./multi-invoker --port 9000`）覆盖回默认值。
    """
    default = argparse.SUPPRESS if suppress_defaults else "127.0.0.1"
    target.add_argument("--host", default=default, help="监听地址，默认 127.0.0.1")
    default = argparse.SUPPRESS if suppress_defaults else 8765
    target.add_argument("--port", type=_parse_port, default=default,
                        metavar="端口",
                        help="监听端口（默认 8765）；写 auto 由系统挑一个空闲端口")
    if suppress_defaults:
        target.add_argument("--no-open", action="store_true", default=argparse.SUPPRESS,
                            help="不自动打开浏览器")
        target.add_argument("--allow-origin", metavar="源", default=argparse.SUPPRESS,
                            help="允许跨域访问的来源（'*' 表示任意来源）")
    else:
        target.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
        target.add_argument("--allow-origin", metavar="源",
                            help="允许跨域访问的来源（'*' 表示任意来源）。"
                                 "独立部署的网页端要连过来时必须设置；绑非回环地址时默认 '*'")


def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    parser = argparse.ArgumentParser(
        prog="multi-invoker",
        description="Multi-Service Invoker（编排层，可在多个外部节点上并发调用同一接口）。"
                    "不带子命令直接运行 = 启动本地网页界面。",
    )
    parser.add_argument("--version", action="version", version=f"multi-invoker {__version__}")
    parser.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR,
                        help=f"配置目录，默认 {DEFAULT_CONFIG_DIR}")
    # serve 的四个参数也放在顶层：这样「不带子命令直接运行」也能带参数，
    # 例如 `./multi-invoker --port 9000`；子命令 serve 里用 SUPPRESS 默认值，避免把顶层值覆盖掉。
    _add_serve_args(parser, suppress_defaults=False)
    sub = parser.add_subparsers(dest="command", required=False)

    p_platforms = sub.add_parser("platforms", help="列出全部节点")
    p_platforms.set_defaults(func=cmd_platforms)

    p_services = sub.add_parser("services", help="列出调用项清单")
    p_services.set_defaults(func=cmd_services)

    p_headers = sub.add_parser("headers", help="查看全局默认请求头与请求体格式")
    p_headers.set_defaults(func=cmd_headers)

    p_info = sub.add_parser("info", help="查看运行形态、配置目录与数据量")
    p_info.set_defaults(func=cmd_info)

    p_query = sub.add_parser("query", help="对一个或多个调用项执行统一调用")
    p_query.add_argument("services", nargs="+", help="调用项（编码），可多个")
    p_query.add_argument("--tag", action="append", default=[], metavar="标签",
                         help="只查带该标签的地址条目，可重复；不传则全部")
    p_query.add_argument("--platform", action="append", default=[],
                         help="只查指定节点编码，可重复；不传则全查")
    p_query.add_argument("--exclude", action="append", default=[],
                         help="排除指定节点编码，可重复")
    p_query.add_argument("--method", help="覆盖请求方法")
    p_query.add_argument("--body", help="覆盖请求体（JSON 字符串或模板文本）")
    p_query.add_argument("--header", action="append", default=[],
                         help="追加请求头，格式 K:V，可重复")
    p_query.add_argument("--cookie", help="注入 Cookie，格式 'k=v; k2=v2'（一次性，不持久化会话）")
    p_query.add_argument("--cookie-file", action="append", default=[],
                         metavar="文件",
                         help="从文件加载 Cookie，每行一条 k=v，可重复；与 --cookie 同名时后者覆盖")
    p_query.add_argument("--pick", help="从 JSON 响应中按点号路径取值（留空则用调用项配置的默认 pick），如 data.0.name")
    p_query.add_argument("--timeout", type=float, default=15.0, help="单请求超时秒数，默认 15")
    p_query.add_argument("--concurrency", type=int, default=8, help="并发数，默认 8")
    p_query.add_argument("--retries", type=int, default=0, help="网络失败重试次数，默认 0")
    p_query.add_argument("--max-body", type=int, default=20000,
                         help="响应体截断字符数，默认 20000")
    p_query.add_argument("--use-ip", action="store_true",
                         help="按节点内网 IP 直连（Host 头保持域名，自动忽略证书校验）")
    p_query.add_argument("--insecure", action="store_true", help="忽略 HTTPS 证书校验")
    p_query.add_argument("--follow", action="store_true",
                         help="跟随 3xx 跳转（307/308 保留方法与请求体）")
    p_query.add_argument("--any-scheme", action="store_true",
                         help="忽略协议：http/https 只作首选，同址换协议的跳转自动跟随并保留方法与请求体，"
                              "协议层失败自动换协议重试一次")
    p_query.add_argument("--full", action="store_true", help="打印完整响应内容")
    p_query.add_argument("--csv", help="导出 CSV 路径（Excel 可直接打开）")
    p_query.add_argument("--json", dest="json", help="导出 JSON 路径")
    p_query.set_defaults(func=cmd_query)

    p_plan = sub.add_parser("plan", help="预览将要发出的请求（不发送，人工核对用）")
    p_plan.add_argument("services", nargs="+", help="调用项（编码），可多个")
    p_plan.add_argument("--tag", action="append", default=[], metavar="标签",
                        help="只查带该标签的地址条目，可重复；不传则全部")
    p_plan.add_argument("--platform", action="append", default=[])
    p_plan.add_argument("--exclude", action="append", default=[])
    p_plan.add_argument("--method", help="覆盖请求方法")
    p_plan.add_argument("--body", help="覆盖请求体")
    p_plan.add_argument("--header", action="append", default=[])
    p_plan.add_argument("--cookie", help="预览请求时同样注入 Cookie（不发送，仅展示）")
    p_plan.add_argument("--cookie-file", action="append", default=[],
                        metavar="文件",
                        help="从文件加载 Cookie 用于预览")
    p_plan.add_argument("--any-scheme", action="store_true",
                        help="预览时按「忽略协议」处理（不改请求地址，只标注该开关已开）")
    p_plan.add_argument("--full", action="store_true",
                        help="逐个打印完整请求（方法、URL、请求头、请求体）")
    p_plan.set_defaults(func=cmd_plan)

    p_serve = sub.add_parser("serve", help="启动本地网页界面（不带子命令时的默认动作）")
    # SUPPRESS：子命令里没显式给的值，不要覆盖顶层已经解析出来的值
    _add_serve_args(p_serve, suppress_defaults=True)
    p_serve.set_defaults(func=cmd_serve)

    # 不带子命令时的默认动作：直接起网页，等价于 `serve`。
    # 这样单文件部署就是「双击 / 直接运行 = 可用」。
    parser.set_defaults(func=cmd_serve)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # 网页与只读清单在「还没配置任何数据」时也应该能跑起来：
    # 单文件首次部署就是空配置，报错拦住反而挡住了补数据的入口。
    # query / plan 这类真正发请求的子命令仍然要求至少 1 个平台 + 1 个调用项。
    if getattr(args, "command", None) in (None, "serve", "info", "platforms",
                                          "services", "headers"):
        os.environ.setdefault("MULTI_INVOKER_ALLOW_EMPTY_CONFIG", "1")
    try:
        config = Config(args.config_dir)
        return args.func(config, args)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except runner.QueryError as exc:
        print(f"[查询错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
