"""极简 HTTP 客户端（仅标准库）。

支持：超时、忽略证书校验、按内网 IP 直连但保留原 Host 头（DNS 不可用时用）、
自动识别 UTF-8 / GB18030 编码、可选 Cookie 注入（一次性，不持久化会话）。
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

USER_AGENT = "multi-invoker/1.0"
MAX_BODY_CHARS = 20000

# OpenSSL 3.x 的默认套件列表不再包含「静态 RSA 密钥交换」(kRSA) 套件。
# 部分内网老节点只支持静态 RSA（不支持 ECDHE/DHE），客户端只报 ECDHE 时会收到
# SSLV3_ALERT_HANDSHAKE_FAILURE。这里把常用静态 RSA 套件追加回默认列表；
# SECLEVEL 保持 2，证书校验强度不变。
LEGACY_KEX_CIPHERS = "AES256-GCM-SHA384:AES128-GCM-SHA256:AES256-SHA256:AES128-SHA256"


def parse_cookie_text(text: str | None) -> dict:
    """把 `k=v; k2=v2` 形式的 Cookie 字符串解析成 dict。

    - 允许值里有 `=`（只切第一个）
    - 自动 trim 空白与首尾引号
    - 空字符串 / None → {}
    """
    out: dict = {}
    if not text:
        return out
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


class RequestError(Exception):
    """网络层失败：DNS 解析、连接、超时、TLS。"""


def _toggle_scheme(url: str) -> str:
    """把 URL 的 http/https 换成另一个。

    显式写死的非默认端口（如 :8080）原样保留；默认端口（80/443）不写进 URL，
    由新协议自动取默认端口。
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    other = "https" if scheme == "http" else "http"
    host = parts.hostname or ""
    default_port = 443 if scheme == "https" else 80
    port = parts.port
    netloc = f"{host}:{port}" if (port and port != default_port) else host
    return urlunsplit((other, netloc, parts.path or "/", parts.query, parts.fragment))


def _same_target_except_scheme(a: str, b: str) -> bool:
    """两个 URL 是否「只有协议不同」（主机、路径、查询都一致）。

    用于判断 3xx 跳转是不是「同一个请求换个协议再发一次」，
    这种情况在 any_scheme 下要保留原方法与请求体。
    """
    pa, pb = urlsplit(a), urlsplit(b)
    if not pa.scheme or not pb.scheme or pa.scheme.lower() == pb.scheme.lower():
        return False
    if (pa.hostname or "").lower() != (pb.hostname or "").lower():
        return False
    if (pa.path or "/") != (pb.path or "/"):
        return False
    return (pa.query or "") == (pb.query or "")


class HttpResponse:
    __slots__ = ("status", "reason", "headers", "body", "encoding",
                 "truncated", "elapsed_ms", "url", "connected_to", "redirects",
                 "scheme_switched")

    def __init__(self, status, reason, headers, body, encoding, truncated,
                 elapsed_ms, url, connected_to, redirects=0, scheme_switched=False):
        self.status = status
        self.reason = reason
        self.headers = headers
        self.body = body
        self.encoding = encoding
        self.truncated = truncated
        self.elapsed_ms = elapsed_ms
        self.url = url
        self.connected_to = connected_to
        self.redirects = redirects
        self.scheme_switched = scheme_switched

    @property
    def location(self) -> str | None:
        return self.headers.get("Location") or self.headers.get("location")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _build_ssl_context(insecure: bool, connect_host: str | None) -> ssl.SSLContext:
    """构造 TLS 上下文。

    - 证书校验：仅当 insecure / connect_host 时关闭（R061：绝无条件放松校验）。
    - 套件：始终把被 OpenSSL 3.x 移出默认列表的静态 RSA 套件加回来，
      否则连不上只支持静态 RSA 的内网节点（如智维测试）。
    """
    ctx = ssl.create_default_context()
    if insecure or connect_host:
        # 直连 IP 时证书必然和域名不匹配，只能关闭校验
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # DEFAULT 保证沿用系统默认套件，追加的只是静态 RSA 那几套
    ctx.set_ciphers(f"DEFAULT@SECLEVEL=2:{LEGACY_KEX_CIPHERS}")
    return ctx


def _open_socket(scheme: str, host: str, port: int, timeout: float,
                 connect_host: str | None, insecure: bool) -> socket.socket:
    target = connect_host or host
    raw = socket.create_connection((target, port), timeout)
    if scheme != "https":
        return raw
    ctx = _build_ssl_context(insecure, connect_host)
    try:
        return ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise


def _detect_encoding(headers: dict, raw: bytes) -> str:
    content_type = headers.get("Content-Type", "")
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            enc = part.split("=", 1)[1].strip().strip('"').lower()
            if enc:
                return enc
    for enc in ("utf-8", "gb18030"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _decode(raw: bytes, headers: dict, max_body: int):
    encoding = _detect_encoding(headers, raw)
    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        encoding, text = "utf-8", raw.decode("utf-8", errors="replace")
    truncated = False
    if max_body and len(text) > max_body:
        text = text[:max_body]
        truncated = True
    return text, encoding, truncated


def request(url: str, method: str = "GET", headers: dict | None = None,
            body: bytes | None = None, timeout: float = 15.0,
            connect_host: str | None = None, insecure: bool = False,
            max_body: int = MAX_BODY_CHARS, follow_redirects: bool = False,
            max_redirects: int = 3,
            cookies: dict | str | None = None,
            any_scheme: bool = False) -> HttpResponse:
    """发起一次 HTTP 请求；网络层异常统一抛 RequestError。

    follow_redirects：跟随 301/302/303/307/308（307/308 保留方法与请求体，
    其余按浏览器语义转 GET）。

    cookies：可选 Cookie 注入；dict 或 "k=v;k2=v2" 字符串均可。
    - 一次性注入到本次请求的 Cookie 头，**不会**与对端 Set-Cookie 联动，
      也不跨重定向保留。会话复用请走 runner 层。
    - 与传入 headers["Cookie"] 冲突时，cookies 优先。

    any_scheme（忽略协议）：把 URL 里的 http/https 只当作"首选"，以服务端为准：
    - 3xx 的 Location 若与本请求**只有协议不同**（主机/路径/查询一致），
      即使没开 follow_redirects 也跟随，且**保留原方法与请求体**
      （不按 301/302 语义降级成 GET）。303 仍按标准转 GET。
    - 首选协议发生**协议层失败**（TLS 握手/证书失败、连接被拒/被重置）时，
      自动换成另一个协议、用同样的方法与请求体重试一次（http↔https 双向）。
    - DNS 解析失败不换协议（与协议无关）；超时不换（避免等待翻倍）。
    """
    current_url = url
    current_method = method.upper()
    current_body = body
    hops = 0
    original_host = urlsplit(url).hostname
    original_scheme = (urlsplit(url).scheme or "http").lower()
    scheme_fallback_used = False  # 「忽略协议」的换协议重试只做一次

    while True:
        parts = urlsplit(current_url)
        scheme = (parts.scheme or "http").lower()
        if scheme not in ("http", "https"):
            raise RequestError(f"不支持的协议：{scheme or '(空)'}")
        host = parts.hostname
        if not host:
            raise RequestError(f"URL 缺少主机名：{current_url}")
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        send_headers = dict(headers or {})
        default_port = 443 if scheme == "https" else 80
        send_headers["Host"] = host if port == default_port else f"{host}:{port}"
        send_headers.setdefault("User-Agent", USER_AGENT)
        send_headers.setdefault("Accept", "*/*")
        send_headers.setdefault("Accept-Encoding", "identity")
        # Cookie 注入：cookies 参数优先于传入的 Cookie 头
        if cookies is not None:
            if isinstance(cookies, str):
                cookies = parse_cookie_text(cookies)
            if isinstance(cookies, dict) and cookies:
                send_headers["Cookie"] = "; ".join(
                    f"{k}={v}" for k, v in cookies.items() if v is not None)
        # 若用户 headers 给了 Cookie 但 cookies 参数为空，仍走用户给定
        # （不主动清空，避免覆盖合法设置）

        # 跳转到其它主机时不再沿用原来的内网 IP 直连
        hop_connect_host = connect_host if (connect_host and host == original_host) else None

        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        started = time.perf_counter()
        try:
            conn.sock = _open_socket(scheme, host, port, timeout, hop_connect_host, insecure)
            conn.request(current_method, path, body=current_body, headers=send_headers)
            resp = conn.getresponse()
            raw = resp.read()
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            resp_headers = {k: v for k, v in resp.getheaders()}
        except socket.timeout:
            raise RequestError(f"请求超时（{timeout:g}s）") from None
        except ssl.SSLError as exc:
            if any_scheme and not scheme_fallback_used:
                # 忽略协议：TLS 层不通 → 换另一个协议重试（方法与请求体原样）
                scheme_fallback_used = True
                current_url = _toggle_scheme(current_url)
                continue
            raise RequestError(f"TLS 握手/证书失败：{exc}") from None
        except socket.gaierror as exc:
            # DNS 与协议无关，换协议也没用
            raise RequestError(f"域名解析失败（{exc.strerror or exc}）") from None
        except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
            if any_scheme and not scheme_fallback_used:
                # 忽略协议：端口不通 → 换另一个协议重试（方法与请求体原样）
                scheme_fallback_used = True
                current_url = _toggle_scheme(current_url)
                continue
            raise RequestError(f"连接失败：{exc}") from None
        except http.client.HTTPException as exc:
            raise RequestError(f"HTTP 协议错误：{exc}") from None
        finally:
            try:
                conn.close()
            except Exception:
                pass

        location = resp_headers.get("Location") or resp_headers.get("location")
        redirectable = resp.status in (301, 302, 303, 307, 308) and location
        if redirectable:
            next_url = urljoin(current_url, location)
            # 「忽略协议」：只是换协议的同址跳转 → 当作同一请求换个协议再发，
            # 不按 301/302 的浏览器语义把 POST 降级成 GET（303 例外）。
            scheme_only = any_scheme and _same_target_except_scheme(current_url, next_url)
            if (follow_redirects or scheme_only) and hops < max_redirects:
                hops += 1
                keep_method = resp.status in (307, 308) or (scheme_only and resp.status != 303)
                if not keep_method:
                    current_method, current_body = "GET", None
                    for key in list(send_headers):
                        if key.lower() == "content-type":
                            send_headers.pop(key)
                headers = send_headers
                current_url = next_url
                continue

        text, encoding, truncated = _decode(raw, resp_headers, max_body)
        final_scheme = (urlsplit(current_url).scheme or "http").lower()
        return HttpResponse(
            status=resp.status,
            reason=resp.reason,
            headers=resp_headers,
            body=text,
            encoding=encoding,
            truncated=truncated,
            elapsed_ms=elapsed_ms,
            url=current_url,
            connected_to=hop_connect_host or host,
            redirects=hops,
            scheme_switched=(final_scheme != original_scheme),
        )


def pick_json_path(text: str, path: str):
    """从 JSON 文本中按点号路径取值，如 data.list.0.value。取不到返回 None。"""
    if not path:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    current = data
    for token in path.split("."):
        if isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
        elif isinstance(current, list):
            try:
                index = int(token)
            except ValueError:
                return None
            if index < 0 or index >= len(current):
                return None
            current = current[index]
        else:
            return None
    if isinstance(current, (dict, list)):
        return json.dumps(current, ensure_ascii=False)
    return current
