"""本地网页服务：页面 + 轻量 JSON 接口，仅标准库实现。"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import runner, runtime, store
from .config import Config

# 网页资源统一用 `web/` 下的相对名访问；源码运行读磁盘，单文件运行从归档里取。
ASSET_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "services.html": "text/html; charset=utf-8",
    "platforms.html": "text/html; charset=utf-8",
    "templates.html": "text/html; charset=utf-8",
    "common.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml; charset=utf-8",
}


class QueryServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, config: Config, allow_origin: str | None = None):
        self.config = config
        self.allow_origin = allow_origin
        self.services = store.ServiceStore(config.db)
        self.headers = store.HeaderStore(config.db)
        self.platforms = store.PlatformStore(config.db)
        super().__init__(address, QueryHandler)

    def reload(self):
        """改了任意配置后重新加载，后续查询立刻生效。"""
        try:
            self.config = Config(self.config.dir)
        except Exception:
            pass  # 保留旧配置，避免改坏配置后界面直接不可用


class QueryHandler(BaseHTTPRequestHandler):
    server_version = "multi-invoker/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 工具方法 --
    def _cors_headers(self) -> dict:
        """跨域头。默认关闭：绑回环时只允许同源，避免任意网页驱动本机去发请求。

        要让「独立部署的网页端」连过来，用 `serve --allow-origin`，或直接把服务
        绑到非回环地址（此时默认放开为 `*`，并在启动日志里提示）。
        """
        origin = self.server.allow_origin
        if not origin:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_csv(self, text: str, filename: str):
        body = ("\ufeff" + text).encode("utf-8")
        self._send(200, body, "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{filename}"'})

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _redirect(self, target: str):
        body = f"<meta http-equiv='refresh' content='0;url={target}'>".encode("utf-8")
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 400):
        self._send_json({"error": message}, status)

    # ------------------------------------------------------------------ 路由 --
    # 路径 → web/ 下的资源名；`None` 表示该路径不提供静态资源
    PAGE_ROUTES = {
        "/": "index.html",
        "/index.html": "index.html",
        "/services": "services.html",
        "/services.html": "services.html",
        "/platforms": "platforms.html",
        "/platforms.html": "platforms.html",
        "/templates": "templates.html",
        "/templates.html": "templates.html",
        "/common.js": "common.js",
        "/style.css": "style.css",
        "/favicon.ico": "favicon.svg",
        "/favicon.svg": "favicon.svg",
    }

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        asset = self.PAGE_ROUTES.get(path)
        if asset:
            self._send_asset(asset)
        elif path in ("/headers", "/headers.html"):
            # 请求头页已下线（并入服务定义/调用页），旧链接重定向到服务定义
            self._redirect("/services")
        elif path in ("/services/export.csv", "/api/services/export.csv"):
            self._send_csv(self.server.services.export_csv(),
                           f"services-{time.strftime('%Y%m%d-%H%M%S')}.csv")
        elif path in ("/services/template.csv", "/api/services/template.csv"):
            self._send_csv(store.ServiceStore.template_csv(), "services-template.csv")
        elif path in ("/services/export.json", "/api/services/export.json"):
            body = json.dumps(self.server.services.export_json(), ensure_ascii=False,
                              indent=2).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8",
                       {"Content-Disposition":
                        f'attachment; filename="services-{time.strftime("%Y%m%d-%H%M%S")}.json"'})
        elif path == "/api/config":
            self._send_json(self.server.config.snapshot())
        elif path == "/api/meta":
            self._send_json(self._meta_payload())
        elif path == "/api/services":
            self._send_json(self._services_payload())
        elif path == "/api/headers":
            self._send_json(self._headers_payload())
        elif path == "/api/platforms":
            self._send_json(self._platforms_payload())
        elif path == "/api/health":
            self._send_json({"ok": True})
        else:
            self._send_error_json("未找到该路径", 404)

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        """跨域预检。允许的路径与方法由实际路由决定，这里统一回 204。"""
        path = self.path.split("?", 1)[0]
        allowed = (path in self.PAGE_ROUTES or path.startswith("/api/")
                   or path.startswith("/services/"))
        if not allowed or not self.server.allow_origin:
            self._send(405, b"", "text/plain; charset=utf-8")
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        routes = {
            "/api/query": self._handle_query,
            "/api/query/batch": self._handle_query_batch,
            "/api/preview": self._handle_preview,
            "/api/preview/batch": self._handle_preview_batch,
            "/api/services/save": self._handle_save_service,
            "/api/services/delete": self._handle_delete_service,
            "/api/services/import": self._handle_import_services,
            "/api/headers/save": self._handle_save_headers,
            "/api/platforms/save": self._handle_save_platforms,
            "/api/context-profiles/save": self._handle_save_context_profiles,
            "/api/url-preview": self._handle_url_preview,
        }
        handler = routes.get(path)
        if handler is None:
            self._send_error_json("未找到该路径", 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            request = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_error_json(f"请求体不是合法 JSON：{exc}")
            return
        try:
            handler(request)
        except (runner.QueryError, store.StoreError) as exc:
            self._send_error_json(str(exc))
        except (KeyError, ValueError, TypeError) as exc:
            self._send_error_json(f"请求参数有问题：{exc}")

    # ------------------------------------------------------------- 业务接口 --
    def _handle_query(self, request: dict):
        if request.get("stream"):
            self._stream(request)
            return
        self._send_json(self._run(request))

    def _handle_preview(self, request: dict):
        service_code, platform_codes, tag_filter, options = self._args(request)
        self._send_json(runner.preview_query(
            self.server.config, service_code, platform_codes, tag_filter, **options))

    def _handle_preview_batch(self, request: dict):
        codes, platform_codes, tag_filter, options = self._args_batch(request)
        self._send_json(runner.preview_query_batch(
            self.server.config, codes, platform_codes, tag_filter, **options))

    def _services_payload(self) -> dict:
        config = self.server.config
        services = self.server.services.list_services()
        return {
            "configDir": config.dir,
            "services": services,
            "contextProfiles": list(config.context_profiles),
        }

    def _headers_payload(self) -> dict:
        return {
            "configDir": self.server.config.dir,
            "defaultHeaders": self.server.config.default_headers,
            "bodyFormat": self.server.config.body_format,
        }

    @staticmethod
    def _plat_brief(p: dict) -> dict:
        """给调用/试算页用的平台摘要：带上地址条目里的标签，便于做标签筛选。"""
        endpoints = p.get("endpoints") or []
        ep_tags = []
        for ep in endpoints:
            for tg in (ep.get("tags") or []):
                if tg not in ep_tags:
                    ep_tags.append(tg)
        return {
            "code": p["code"],
            "name": p.get("name", p["code"]),
            "shortName": p.get("shortName") or "",
            "tags": p.get("tags") or [],
            "endpointTags": ep_tags,
        }

    def _platforms_payload(self) -> dict:
        return {
            "configDir": self.server.config.dir,
            "contextProfiles": list(self.server.config.context_profiles),
            # 已有标签汇总，供模板页的两个下拉做选项（不写死任何词）
            "endpointTags": sorted({          # 地址条目上用过的标签 → 适用环境
                tg for p in self.server.config.platforms
                for ep in (p.get("endpoints") or [])
                for tg in (ep.get("tags") or [])
            }),
            "platformTags": sorted({          # 平台自身用过的标签 → 平台标签
                tg for p in self.server.config.platforms
                for tg in (p.get("tags") or [])
            }),
            "platforms": [
                {
                    "code": p["code"],
                    "name": p.get("name", p["code"]),
                    "shortName": p.get("shortName") or "",
                    "intranetIp": p.get("intranetIp") or [],
                    "note": p.get("note"),
                    "insecure": bool(p.get("insecure")),
                    "follow": bool(p.get("follow")),
                    "anyScheme": bool(p.get("anyScheme")),
                    "tags": p.get("tags") or [],
                    # 地址条目列表（带标签），只维护到域名
                    "endpoints": [
                        {"tags": list(ep.get("tags") or []),
                         "baseUrl": ep.get("baseUrl", "")}
                        for ep in (p.get("endpoints") or [])
                        if isinstance(ep, dict) and ep.get("baseUrl")
                    ],
                }
                for p in self.server.config.platforms
            ],
        }

    def _handle_save_service(self, request: dict):
        service = request.get("service")
        if not isinstance(service, dict):
            self._send_error_json("缺少 service 对象")
            return
        result = self.server.services.save(
            service, request.get("originalCode"),
            self.server.config.platforms)
        self.server.reload()
        payload = self._services_payload()
        payload.update({"ok": True, "action": result["action"],
                        "backup": result["backup"], "warnings": result["warnings"]})
        self._send_json(payload)

    def _handle_delete_service(self, request: dict):
        code = (request.get("code") or "").strip()
        if not code:
            self._send_error_json("缺少 code")
            return
        result = self.server.services.delete(code)
        self.server.reload()
        payload = self._services_payload()
        payload.update({"ok": True, "action": result["action"],
                        "backup": result["backup"], "warnings": []})
        self._send_json(payload)

    def _handle_import_services(self, request: dict):
        """导入服务：csv（文本）或 services（JSON 数组）；mode=merge / replace。"""
        store_ = self.server.services
        warnings = []
        services = request.get("services")
        if services is None:
            csv_text = request.get("csv") or ""
            if not csv_text.strip():
                self._send_error_json("缺少导入内容（csv 或 services）")
                return
            try:
                services, warnings = store_.csv_to_services(csv_text)
            except store.StoreError as exc:
                self._send_error_json(str(exc))
                return
        if not isinstance(services, list):
            self._send_error_json("services 必须是数组")
            return
        result = store_.import_services(
            services, self.server.config.platforms,
            mode=request.get("mode") or "merge",
            dry_run=bool(request.get("dryRun")))
        self.server.reload()
        payload = self._services_payload()
        payload.update({"ok": True, "added": result["added"], "updated": result["updated"],
                        "backup": result["backup"], "dryRun": result.get("dryRun", False),
                        "warnings": list(warnings) + list(result["warnings"])})
        self._send_json(payload)

    def _handle_save_headers(self, request: dict):
        default_headers = request.get("defaultHeaders") or {}
        body_format = request.get("bodyFormat") or "json"
        result = self.server.headers.save(default_headers, body_format)
        self.server.reload()
        payload = self._headers_payload()
        payload.update({"ok": True, "backup": result["backup"], "warnings": []})
        self._send_json(payload)

    def _handle_save_platforms(self, request: dict):
        items = request.get("items") or []
        if not isinstance(items, list):
            self._send_error_json("items 必须是数组")
            return
        # 先重载一次，确保删除保护用到的 services 是磁盘上最新的
        self.server.reload()
        result = self.server.platforms.upsert(items, self.server.config.services)
        self.server.reload()
        payload = self._platforms_payload()
        payload.update({"ok": True, "backup": result["backup"],
                        "warnings": result["warnings"],
                        "deleted": result.get("deleted", [])})
        self._send_json(payload)

    def _handle_url_preview(self, request: dict):
        """URL 预览：选域名 + 服务，列出每个地址条目的最终 URL。"""
        platform_code = (request.get("platform") or "").strip()
        service_code = (request.get("service") or "").strip()
        if not platform_code or not service_code:
            self._send_error_json("需要 platform（域名）与 service（服务）")
            return
        try:
            payload = runner.preview_platform_urls(
                self.server.config, service_code, platform_code)
        except runner.QueryError as exc:
            self._send_error_json(str(exc))
            return
        self._send_json(payload)

    def _handle_save_context_profiles(self, request: dict):
        """整体保存 meta.contextProfiles（来自 /templates 页面）。"""
        profiles = request.get("profiles")
        if not isinstance(profiles, list):
            self._send_error_json("缺少 profiles 数组")
            return
        try:
            result = self.server.platforms.save_context_profiles(profiles)
        except store.StoreError as e:
            self._send_error_json(str(e), status=400)
            return
        self.server.reload()
        payload = self._platforms_payload()
        payload.update({"ok": True, "profiles": result["profiles"],
                        "backup": result["backup"], "warnings": []})
        self._send_json(payload)

    # ----------------------------------------------------------------- 流 --
    def _stream(self, request: dict):
        """NDJSON 流式返回：每个平台一完成就写一条，前端可边查边看。"""
        try:
            service_code, platform_codes, tag_filter, options = self._args(request)
        except (runner.QueryError, KeyError, ValueError, TypeError) as exc:
            self._send_error_json(str(exc))
            return

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for chunk in runner.iter_query(
                    self.server.config, service_code, platform_codes, tag_filter, **options):
                self.wfile.write(
                    (json.dumps(chunk, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器提前关闭（例如用户重新查询），忽略

    def _handle_query_batch(self, request: dict):
        """多服务调用：request.serviceCodes: list[str]，其他字段与 /api/query 一致。"""
        if request.get("stream"):
            self._stream_batch(request)
            return
        try:
            service_codes, platform_codes, tag_filter, options = self._args_batch(request)
        except (runner.QueryError, KeyError, ValueError, TypeError) as exc:
            self._send_error_json(str(exc))
            return
        payload = runner.run_query_batch(
            self.server.config, service_codes, platform_codes, tag_filter, **options)
        self._send_json(payload)

    def _stream_batch(self, request: dict):
        """多服务 NDJSON 流式。"""
        try:
            service_codes, platform_codes, tag_filter, options = self._args_batch(request)
        except (runner.QueryError, KeyError, ValueError, TypeError) as exc:
            self._send_error_json(str(exc))
            return
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for chunk in runner.iter_query_batch(
                    self.server.config, service_codes, platform_codes, tag_filter, **options):
                self.wfile.write(
                    (json.dumps(chunk, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _args_batch(self, request: dict):
        """把 /api/query/batch 的 payload 拆成 (serviceCodes, platform_codes, tag_filter, options)。"""
        config = self.server.config
        codes = request.get("serviceCodes")
        if not isinstance(codes, list) or not codes:
            raise runner.QueryError("至少选择一个调用项")
        codes = [str(c).strip() for c in codes if str(c).strip()]
        if not codes:
            raise runner.QueryError("至少选择一个调用项")
        # 地址条目标签筛选
        raw_tags = request.get("endpointTags")
        if raw_tags is None:
            raw_tags = request.get("env")
        if raw_tags in (None, "", "both"):
            tag_filter = []
        elif isinstance(raw_tags, str):
            tag_filter = [x.strip() for x in raw_tags.split(",") if x.strip()]
        else:
            tag_filter = [str(x).strip() for x in raw_tags if str(x).strip()]

        platform_codes = request.get("platforms") or []
        if not platform_codes:
            platform_codes = config.platform_codes()
        unknown = [c for c in platform_codes if config.platform(c) is None]
        if unknown:
            raise runner.QueryError(f"未知平台编码：{', '.join(unknown)}")

        options = {
            "timeout": float(request.get("timeout") or 15),
            "concurrency": int(request.get("concurrency") or 8),
            "retries": int(request.get("retries") or 0),
            "useIp": bool(request.get("useIp")),
            "insecure": bool(request.get("insecure") or request.get("useIp")),
            "headers": request.get("headers") or {},
            "body": request.get("body"),
            "bodyFieldValues": request.get("bodyFieldValues") or {},
            "bodyOverrides": request.get("bodyOverrides") or {},
            "method": request.get("method"),
            "pick": (request.get("pick") or "").strip() or None,
            "follow": bool(request.get("follow")),
            "anyScheme": bool(request.get("anyScheme")),
        }
        return codes, platform_codes, tag_filter, options

    def _send_asset(self, name: str):
        """发送 `web/` 下的资源。源码运行读磁盘，单文件运行从归档里读。"""
        content_type = ASSET_TYPES.get(name, "application/octet-stream")
        body = runtime.read_asset(name)
        if body is None:
            self._send_error_json(f"缺少页面文件：web/{name}", 404)
            return
        self._send(200, body, content_type)

    def _meta_payload(self) -> dict:
        """客户端握手用：核心版本、运行形态、数据目录、跨域开关。"""
        config = self.server.config
        payload = runtime.snapshot()
        payload.update({
            "ok": True,
            "allowOrigin": self.server.allow_origin,
            "platforms": len(config.platforms),
            "services": len(config.services),
            "contextProfiles": len(config.context_profiles),
            "bodyFormat": config.body_format,
        })
        return payload

    # ------------------------------------------------------------- 查询参数 --
    def _args(self, request: dict):
        """把网页请求翻译成 runner 参数（命令行/网页共用同一套校验）。"""
        config = self.server.config
        inline = request.get("service") if isinstance(request.get("service"), dict) else None
        service_code = (request.get("serviceCode") or "").strip()
        if not service_code and inline:
            service_code = (inline.get("code") or "").strip()
        if not service_code:
            raise runner.QueryError("缺少 serviceCode")
        # 地址条目标签筛选：空 = 不限；request 里可为字符串或数组
        raw_tags = request.get("endpointTags")
        if raw_tags is None:
            raw_tags = request.get("env")     # 兼容旧前端字段
        if raw_tags in (None, "", "both"):
            tag_filter = []
        elif isinstance(raw_tags, str):
            tag_filter = [x.strip() for x in raw_tags.split(",") if x.strip()]
        else:
            tag_filter = [str(x).strip() for x in raw_tags if str(x).strip()]

        platform_codes = request.get("platforms") or []
        if not platform_codes:
            platform_codes = config.platform_codes()
        unknown = [c for c in platform_codes if config.platform(c) is None]
        if unknown:
            raise runner.QueryError(f"未知平台编码：{', '.join(unknown)}")

        options = {
            "timeout": float(request.get("timeout") or 15),
            "concurrency": int(request.get("concurrency") or 8),
            "retries": int(request.get("retries") or 0),
            "useIp": bool(request.get("useIp")),
            "insecure": bool(request.get("insecure") or request.get("useIp")),
            "headers": request.get("headers") or {},
            "body": request.get("body"),
            "bodyFieldValues": request.get("bodyFieldValues") or {},
            "bodyOverrides": request.get("bodyOverrides") or {},
            "method": request.get("method"),
            "pick": (request.get("pick") or "").strip() or None,
            "follow": bool(request.get("follow")),
            "anyScheme": bool(request.get("anyScheme")),
            "service_override": inline,
        }
        return service_code, platform_codes, tag_filter, options

    def _run(self, request: dict) -> dict:
        service_code, platform_codes, tag_filter, options = self._args(request)
        return runner.run_query(self.server.config, service_code, platform_codes,
                                tag_filter, **options)

    def log_message(self, fmt, *args):
        if not str(args[1] if len(args) > 1 else "").startswith(("2", "3")):
            super().log_message(fmt, *args)


def _resolve_allow_origin(host: str, explicit: str | None) -> str | None:
    """决定跨域放开的程度。

    - 显式传 `--allow-origin`：按传的值来（`*` 或某个源）
    - 绑非回环地址：默认放开为 `*`，因为这种部署本来就是要给别的机器访问
    - 绑回环地址且没传：不放开，只有同源页面能调
    """
    if explicit:
        return explicit
    loopback = host in ("127.0.0.1", "localhost", "::1", "")
    return None if loopback else "*"


def _probe_core(url: str, timeout: float = 1.5) -> bool:
    """探测某个地址上是不是已经有一个「本工具的核心」在跑。

    只认 /api/health 返回 {"ok": true}：端口被别的程序占用时必须能区分出来，
    否则会误导用户去 kill 一个无关进程。
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=timeout) as resp:
            return b'"ok"' in resp.read(256)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _report_port_taken(host: str, port: int, open_browser: bool) -> int:
    """端口被占时的处理：能复用就复用，不能复用就给出可照做的提示。

    分两种情况，因为用户该做的事完全不同：
    1. 占端口的是本工具自己的核心 —— 什么都不用做，直接用；
    2. 占端口的是别的程序 —— 要么换端口，要么先查清占用者。
    """
    if port == 0:
        # --port auto 理论上不会冲突（内核负责挑空闲端口）；真撞上只能重试
        print("自动分配端口时发生冲突，请重新执行一次。", flush=True)
        return 2
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    existing = f"http://{probe_host}:{port}/"

    if _probe_core(existing):
        print(f"端口 {port} 上已经有一个核心在跑，直接用它：{existing}", flush=True)
        print("配置与调用记录都在那一个进程里；不需要再启动第二个。", flush=True)
        print(f"确实要同时再起一个：换端口，例如 --port {port + 1}", flush=True)
        if open_browser:
            try:
                webbrowser.open(existing)
            except Exception:
                pass
        return 0

    print(f"端口 {port} 被别的程序占用了，本工具无法监听。", flush=True)
    print(f"  看是谁占的：lsof -nP -iTCP:{port} -sTCP:LISTEN", flush=True)
    print(f"  换一个端口：multi-invoker serve --port {port + 1}", flush=True)
    print("  也可以先结束占用它的程序，再重新启动。", flush=True)
    return 2


def serve(config: Config, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True, allow_origin: str | None = None) -> int:
    import errno

    auto_port = port == 0
    resolved_origin = _resolve_allow_origin(host, allow_origin)
    try:
        httpd = QueryServer((host, port), config, allow_origin=resolved_origin)
    except OSError as exc:
        # 最常见的启动失败就是端口冲突。不能把 socketserver 的 traceback 甩给用户 ——
        # 里面既没有「其实已经在跑了」这个最可能的原因，也没有下一步该做什么。
        if exc.errno == errno.EADDRINUSE:
            return _report_port_taken(host, port, open_browser)
        if exc.errno == errno.EACCES:
            print(f"没有权限监听 {host}:{port}。小于 1024 的端口需要管理员权限，"
                  f"换一个大于 1024 的端口即可。", flush=True)
            return 2
        print(f"启动失败：无法监听 {host}:{port} —— {exc}", flush=True)
        return 2
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"
    # flush=True：启动信息经常被重定向到日志文件（桌面外壳、systemd），
    # 不 flush 的话在块缓冲下会一直看不到，排障时容易误判成「没起来」。
    print(f"Multi-Service Invoker已启动：{url}", flush=True)
    if auto_port:
        # --port auto：端口是系统挑的，必须显式告诉用户实际值，否则没法访问
        print(f"端口：auto → 系统分配了 {actual_port}", flush=True)
    print(f"运行形态：{runtime.describe()}", flush=True)
    print(f"配置目录：{config.dir}", flush=True)
    print(f"平台 {len(config.platforms)} 个，调用项 {len(config.services)} 个。Ctrl+C 退出。",
          flush=True)
    if resolved_origin:
        print(f"跨域：已放开（Access-Control-Allow-Origin: {resolved_origin}）"
              "—— 任何知道该地址的页面都能读写这里的配置与调用记录，请只在可信网络使用。",
              flush=True)
    else:
        print("跨域：未放开（仅同源页面可用）。要连接独立部署的网页端，"
              "加 --allow-origin '*' 或改用 --host 0.0.0.0。", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        httpd.server_close()
    return 0