#!/usr/bin/env python3
"""NooVa AI skill 共享基础设施：地址守卫 + 脱敏 + 终端排版（零依赖，仅标准库）。

为什么必须是一个共享模块
------------------------
`noova_key.py` / `noova_media.py` / `noova_upload.py` 都需要同一件事：

1. **判断一个地址是不是「部署在服务器上的公开域名」**——凭据配置、每次请求、
   文档脱敏三处都必须用同一套判据；
2. **把非公开域名（本机 / 内网 / 基础设施 / 第三方 CDN）换成中性占位域名**——
   错误信息、文档、日志都要过一遍；
3. **按终端显示宽度对齐**（中文占 2 列），否则中英混排的表格全是错位的。

这些判据一旦各写一份就会漂移，而**安全判据的漂移就是漏洞**：本 skill 第三轮审计
发现的 4 个 BLOCKER 全部源于「生成路径拦得住、配置路径拦不住」。因此这里集中实现，
其余脚本一律引用，不再各自实现。

设计约束
--------
- 只用 Python 标准库，兼容 Python 3.8（`from __future__ import annotations`）。
- `is_local_host()` 是**纯字面量**判定，绝不做网络请求：脱敏路径会大量调用它。
  需要「解析后再判定」的场景用 `resolves_to_local()`（带进程内缓存）。
- 本模块不 import 其余脚本，避免循环依赖。
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 公开常量（全部是对用户公开的信息）
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://noova.vip"
# 官方站点的另一个线上域名（备用）：承载同一套对外 API，主域名被网络策略阻断时可用。
# 两者必须**同等对待**（粘贴解析可信、脱敏保留），否则用户切换域名后 skill 会拒绝配置。
OFFICIAL_HOSTS = ("noova.vip", "noova.live")
OFFICIAL_BASE_URLS = tuple("https://" + host for host in OFFICIAL_HOSTS)
# 向用户展示的官方地址提示（主域名在前）。
OFFICIAL_BASE_HINT = " / ".join(OFFICIAL_BASE_URLS)
# 控制台（领取/管理 Key 的前端页面）与基础地址同域，便于用户一处完成。
CONSOLE_PATH = "/api_control"

# 非公开域名的中性占位（只替换主机名，保留路径与文件名结构，便于阅读）。
REDACTED_HOST = "media.example.com"
# 允许原样出现在文档/日志里的公开域名后缀（其余一律视为非公开）。
PUBLIC_HOST_SUFFIXES = (
    "noova.vip",
    "noova.live",
    "noova.cn",
    "example.com",
)

# 开发者本机自测的显式逃生阀（默认关闭）。
ALLOW_LOCAL_ENV = "NOOVA_ALLOW_LOCAL_BASE_URL"
# 明文 http 指向公开域名的显式逃生阀（默认关闭）：默认拒绝，因为 API Key 走
# Authorization 请求头，明文 http 等于把凭据暴露在链路上。
ALLOW_INSECURE_ENV = "NOOVA_ALLOW_INSECURE_BASE_URL"
# 调试开关：打开后才允许打印堆栈（默认关闭，避免把绝对安装路径与调用栈暴露给用户）。
DEBUG_ENV = "NOOVA_DEBUG"


def _env_flag(name: str) -> bool:
    """读取布尔型环境变量：大小写不敏感，接受 1/true/yes/on。

    早期实现只认小写 `1`/`true`/`yes`，导致 `=TRUE` 静默不生效——一个安全开关
    「看起来打开了其实没打开」比没有开关更危险。
    """
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def local_allow_enabled() -> bool:
    """本机/内网地址逃生阀是否打开（仅用于开发者对自建网关做本地联调）。"""
    return _env_flag(ALLOW_LOCAL_ENV)


def insecure_allow_enabled() -> bool:
    """明文 http 逃生阀是否打开。"""
    return _env_flag(ALLOW_INSECURE_ENV)


def debug_enabled() -> bool:
    """调试开关是否打开（打开后才允许打印堆栈）。

    必须用真值判断：早期实现是「非空即开」，于是 `NOOVA_DEBUG=0`、`=false`
    也会把 traceback（含 skill 绝对安装路径与内部调用栈）打到用户面前。
    """
    return _env_flag(DEBUG_ENV)


# ---------------------------------------------------------------------------
# 主机名归一化
# ---------------------------------------------------------------------------

def normalize_host(host: str) -> str:
    """把各种写法的 host 归一到「小写、无 userinfo、无端口、无尾点」形态。

    必须归一化：以下写法都与 `localhost` 等价，但字面量比对会全部漏判（均已实测）：
      · `localhost.`  —— 尾点 FQDN，`getaddrinfo('localhost.')` → 127.0.0.1；
      · `user@host`   —— 真正的 host 在 `@` 之后；
      · `[::1]`       —— IPv6 字面量的方括号；
      · `host:8080`   —— 端口。
    """
    text = str(host or "").strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    elif text.count(":") == 1:
        # 只有「单个冒号」才当作 host:port 切分：IPv6 字面量含多个冒号，
        # 照切会把 "::1" 切成空串而漏判。
        text = text.split(":", 1)[0]
    return text.rstrip(".")


def canonical_official_host(host: str) -> str:
    """把官方域名归一到规范形式（去掉 `www.` 前缀）；非官方域名原样返回。

    实测（2026-10-09）：`www.noova.vip` 301 → `noova.vip`；`noova.live` 与
    `www.noova.live` 的 API 路径均可直接访问。用户粘贴的凭据文本里常带 www 写法，
    这里统一归一到 apex，避免「同一站点两种写法」在可信域判定与脱敏白名单里漂移。
    只对官方域名做归一：自建域名是否带 www 由用户自己决定，脚本不自作主张。
    """
    h = normalize_host(host)
    bare = h[4:] if h.startswith("www.") else h
    return bare if bare in OFFICIAL_HOSTS else h


def is_official_host(host: str) -> bool:
    """host 是否属于官方域名（含 `www.` 变体）。"""
    return canonical_official_host(host) in OFFICIAL_HOSTS


# ---------------------------------------------------------------------------
# 地址守卫：基础地址必须是「部署在服务器上的公开域名」
# ---------------------------------------------------------------------------
# 本 skill 只对接已部署在服务器上的项目域名（默认 `DEFAULT_BASE_URL`）。
# 把本机 / 内网 / 开发环境地址写进配置的后果是：请求会指向一个只有当前这台
# 开发机才能访问的地址，在别处必然失败，且现象看起来像「服务故障」而非配置错误。
# 因此**在任何写盘路径与任何出网路径上都要拦下**，并明确告诉用户该填什么。

# 本机 / 容器内视角的回环别名
_LOCAL_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "host.docker.internal", "gateway.docker.internal", "kubernetes.docker.internal",
})
# 保留/内网专用域名后缀（RFC 6761 / RFC 8375 / 常见内网约定）
_LOCAL_HOST_SUFFIXES = (
    ".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa",
    ".corp", ".intranet", ".test", ".invalid", ".example",
)
# 明显的开发/测试环境主机名前缀
# （不含 sandbox./demo. 之类：它们也可能是对外可访问的演示站点，不属于「本机/内网」）
_DEV_HOST_PREFIXES = (
    "dev.", "dev-", "test.", "test-", "testing.", "staging.", "staging-",
    "local.", "pre.", "pre-", "uat.", "debug.", "nightly.",
)

# 非标准 IP 写法：`0` / `2130706433` / `0177.0.0.1` / `0x7f000001`。
# 部分解析器会把它们当作 127.0.0.1，属于同一类绕过。
_NUMERIC_HOST_RE = re.compile(
    r"^(?:0[xX][0-9a-fA-F]+|\d+)(?:\.(?:0[xX][0-9a-fA-F]+|\d+)){0,3}$")

_HOSTNAME_RE = re.compile(r"[a-z0-9.\-]+")


def _is_private_ip(ip) -> bool:
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified or ip.is_multicast)


def is_local_host(host: str) -> bool:
    """字面量判定：主机名是否指向本机 / 内网 / 开发环境。

    **纯字面量、不联网**——脱敏路径大量调用它。需要解析后再判定用 `resolves_to_local()`。
    """
    h = normalize_host(host)
    if not h:
        return False
    if h in _LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        pass
    else:
        return _is_private_ip(ip)
    if any(h.endswith(suffix) for suffix in _LOCAL_HOST_SUFFIXES):
        return True
    # `www.` 不改变主机的性质：`www.dev.example.com` 仍是开发环境主机。
    # 不剥前缀就会漏判（DNS 解析失败时守卫会按「离线」放行，等于开口子）。
    bare = h[4:] if h.startswith("www.") else h
    return any(bare.startswith(prefix) for prefix in _DEV_HOST_PREFIXES)


def looks_like_numeric_host(host: str) -> bool:
    """像 IP 但不是标准 IP 的写法（`0` / `2130706433` / `0177.0.0.1` / `0x7f000001`）。"""
    h = normalize_host(host)
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
    except ValueError:
        return bool(_NUMERIC_HOST_RE.match(h))
    return False


# 解析结果按主机名缓存：守卫在**每次请求前**都会跑，不能每次都做 DNS。
_RESOLVE_CACHE: dict[str, "bool | None"] = {}


def clear_resolve_cache() -> None:
    """清空 DNS 判定缓存（供测试隔离使用）。"""
    _RESOLVE_CACHE.clear()


def resolves_to_local(host: str, *, use_cache: bool = True) -> "bool | None":
    """DNS 解析后按 IP 判定主机是否指向本机 / 内网。

    返回 `True`=解析到本机/内网；`False`=解析到公网；`None`=解析失败（离线/DNS 不可用）。

    必要性：`127.0.0.1.nip.io`、`localtest.me` 这类通配 DNS 的字面量完全正常，
    只有解析后才知道它指向回环地址——纯字面量守卫会被它们绕过（已实测）。
    """
    h = normalize_host(host)
    if not h:
        return None
    if use_cache and h in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[h]
    verdict: "bool | None"
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception:  # noqa: BLE001  （OSError / UnicodeError / socket.gaierror 等）
        verdict = None
    else:
        verdict = False
        for info in infos or []:
            try:
                address = str(info[4][0]).split("%")[0]
                if _is_private_ip(ipaddress.ip_address(address)):
                    verdict = True
                    break
            except (IndexError, TypeError, ValueError):
                continue
    if use_cache:
        _RESOLVE_CACHE[h] = verdict
    return verdict


def origin_of(url: str) -> str:
    """把 URL 归约成 `scheme://host[:port]`（丢弃路径，兼容控制台页面地址）。"""
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc}"


def _verdict(ok: bool, url: str = "", host: str = "", *, local: bool = False,
             secure: bool = True, warning: str = "", reason: str = "") -> dict:
    return {"ok": bool(ok), "url": url, "host": host, "local": bool(local),
            "secure": bool(secure), "warning": warning, "reason": reason}


def check_base_url(url: str, *, resolve: bool = True) -> dict:
    """校验基础地址是否是「部署在服务器上的公开域名」。

    返回 `{"ok", "url", "host", "local", "secure", "warning", "reason"}`：

    - `ok=False` → 不得写入配置、不得用于调用；`reason` 可直接展示给用户
      （已包含应填的正确地址）。
    - `ok=True` 且 `warning` 非空 → 可用但不够规范（如放行了本机地址 / 明文 http）。

    判据逐条对应一种**已实测的绕过手法**：

    | 判据 | 拦下的绕过 |
    |---|---|
    | 能解析成 `scheme://host[:port]` 且 host 合法 | 把整句文本当地址写入 |
    | host 非本机/内网/开发环境**字面量** | `localhost` / `10.0.0.5` / `*.internal` |
    | host **解析后**不指向本机/内网 | `127.0.0.1.nip.io`、`localhost.`（通配 DNS） |
    | 不含 userinfo | `https://noova.vip@evil.example.com` |
    | 非非标准数字 IP 写法 | `2130706433`、`0x7f000001`、`0177.0.0.1` |
    | 公开域名必须 https | Key 明文走 http |

    `resolve=False` 跳过 DNS 判定（供脱敏等不能联网的路径使用）。
    """
    raw = str(url or "").strip()
    if not raw:
        return _verdict(False, reason="地址为空")
    origin = origin_of(raw if "//" in raw else f"https://{raw}")
    if not origin:
        return _verdict(False, reason=f"无法识别的地址：{raw}")

    parts = urllib.parse.urlsplit(origin)
    host = normalize_host(parts.hostname or "")
    secure = parts.scheme == "https"
    if not host:
        return _verdict(False, origin, secure=secure, reason="地址缺少主机名")
    if parts.username or parts.password:
        # `https://noova.vip@evil.example.com` 的 hostname 其实是 evil.example.com，
        # 肉眼却像官方域名。直接拒绝，不做「取 @ 之后」的猜测。
        return _verdict(False, origin, host, secure=secure,
                        reason=f"地址中不应包含用户名或密码（收到的写法具有误导性）：{host}")

    if is_local_host(host):
        if local_allow_enabled():
            return _verdict(True, origin, host, local=True, secure=secure,
                            warning=(f"已按 {ALLOW_LOCAL_ENV} 放行本机/内网地址 {host}；"
                                     f"线上环境请改用部署域名 {DEFAULT_BASE_URL}"))
        return _verdict(False, origin, host, local=True, secure=secure,
                        reason=(f"{host} 是本机 / 内网 / 开发环境地址，只有当前这台机器能"
                                f"访问它；本 skill 只对接部署在服务器上的域名，请填写 "
                                f"{OFFICIAL_BASE_HINT}（或你自己的线上部署域名）"))

    if looks_like_numeric_host(host):
        return _verdict(False, origin, host, secure=secure,
                        reason=f"地址中的主机名不合法（非标准 IP 写法）：{host}")

    # 主机名格式：域名（字母/数字/连字符/点）或标准 IP 字面量，
    # 防止把整句文本当地址写入。
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if (not _HOSTNAME_RE.fullmatch(host) or ".." in host
                or host.startswith("-") or host.endswith("-") or host.startswith(".")):
            return _verdict(False, origin, host, secure=secure,
                            reason=f"地址中的主机名不合法：{host}")

    warning = ""
    if resolve:
        resolved = resolves_to_local(host)
        if resolved is True:
            return _verdict(False, origin, host, local=True, secure=secure,
                            reason=(f"{host} 解析到本机 / 内网地址（只有当前这台机器能访问），"
                                    f"不能作为线上基础地址；请填写 {OFFICIAL_BASE_HINT}"
                                    f"（或你自己的线上部署域名）"))
        if resolved is None:
            warning = ("未能解析该主机名（可能离线或 DNS 不可用），已按格式放行；"
                       "请求时若不可达会再报错")

    # 官方域名的 `www.` 变体归一为 apex（用户粘贴的地址常见 www 写法）。
    # 放在判据之后：本机/内网/DNS 判定都按用户实际填写的地址来做，不受归一影响。
    # 保留原 scheme：不能把 `http://www.noova.vip` 静默升成 https（那会绕开下面的明文拦截）。
    canonical = canonical_official_host(host)
    if canonical != host:
        host = canonical
        origin = f"{parts.scheme}://{host}"

    if not secure:
        if not insecure_allow_enabled():
            return _verdict(False, origin, host, secure=False, reason=(
                f"线上地址必须使用 https（当前是 {parts.scheme}）："
                f"API Key 以 Authorization 请求头发送，明文链路等于泄露凭据。"
                f"请改用 https://{host}；确有需要可用环境变量 "
                f"{ALLOW_INSECURE_ENV}=1 显式放行"))
        note = f"已按 {ALLOW_INSECURE_ENV} 放行 {parts.scheme} 明文地址"
        warning = f"{warning}；{note}" if warning else note

    return _verdict(True, origin, host, secure=secure, warning=warning)


# ---------------------------------------------------------------------------
# 重定向守卫
# ---------------------------------------------------------------------------
# 为什么必须自己接管重定向：CPython 的 `HTTPRedirectHandler.redirect_request()`
# 只剔除 `content-length` / `content-type`，**会把 `Authorization` 原样复制到
# 新目标**。于是一个 `302 → http://127.0.0.1:PORT/…` 就能让 Key 改道到未校验的
# 主机上（已实测）。守卫在「发出带凭据的请求」这条路径上只放行同源重定向。

class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """只允许**同源**重定向（scheme + netloc 必须一致）。

    用于一切可能携带凭据的请求。跨源时抛 `HTTPError` 而不是静默跟随，
    调用方会把它当成一次失败的请求处理，绝不静默改道。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise urllib.error.HTTPError(
                newurl, code,
                f"拒绝跨域重定向（{old.netloc} → {new.netloc}）：带凭据的请求不跟随",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    """允许跨源重定向，但**拒绝跳到本机 / 内网地址**。

    用于第三方图床与上传结果地址的校验：这些服务可能正常跳转到别的公网域名
    （CDN），不能一律拒绝；但也不能让第三方返回的地址把用户本机当成探测跳板
    （SSRF）。判定含 DNS 解析，因此 `127.0.0.1.nip.io` 这类写法同样拦得住。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        host = normalize_host(parsed.hostname or "")
        if parsed.scheme not in ("http", "https") or not host or is_local_host(host):
            raise urllib.error.HTTPError(
                newurl, code, f"拒绝重定向到非公网地址：{host or newurl}", headers, fp)
        if resolves_to_local(host) is True:
            raise urllib.error.HTTPError(
                newurl, code, f"拒绝重定向到本机/内网地址：{host}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# opener 在导入期构建：纯对象组装，不联网。
_CREDENTIALED_OPENER = urllib.request.build_opener(SameOriginRedirectHandler)
_PUBLIC_OPENER = urllib.request.build_opener(PublicRedirectHandler)


def open_credentialed(request: urllib.request.Request, *, timeout: int = 60):
    """发出**可能带凭据**的请求（同源重定向守卫）。"""
    return _CREDENTIALED_OPENER.open(request, timeout=timeout)


def open_public(request: urllib.request.Request, *, timeout: int = 60):
    """发出**不带凭据**的公开请求（允许跨源，但不得跳向本机/内网）。"""
    return _PUBLIC_OPENER.open(request, timeout=timeout)


# ---------------------------------------------------------------------------
# 脱敏：非公开域名 → 中性占位域名
# ---------------------------------------------------------------------------
# 契约（`SKILL.md` 铁律 6）：文档 / 报错 / 日志里**绝不出现**基础设施、存储桶、
# 内网、第三方 CDN 的真实域名，一律替换为 `media.example.com`。
# 例外（铁律 7）：生成结果的媒体 URL 是业务功能字段，必须原样交付。

# 大小写不敏感：`HTTPS://internal.corp/x` 曾能原样通过（已实测）。
_URL_RE = re.compile(r"(?:https?|ftp)://([^/\s<>\"'`)\]}]+)", re.IGNORECASE)
# 裸主机名（无 scheme）：分两种写法，判定口径不同——
#   * 单独出现（`gw-internal.acme.com`）：只替换**判定为本机/内网**的那些，
#     避免误伤文件名（`photo.png` 长得也像主机名）。
#   * 带端口或带路径（`gw-internal.acme.com:8443`、`gw-internal.acme.com/x`）：
#     这种形状不可能是文件名，改按公开域名白名单判定，非公开一律替换
#     （这类写法曾能原样通过，已实测）。
_BARE_HOST_RE = re.compile(
    r"(?<![\w.\-@/])((?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})(?::\d{1,5})?",
    re.IGNORECASE)
_BARE_HOST_TAIL_RE = re.compile(
    r"(?<![\w.\-@/])((?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})(?P<tail>:\d{1,5}|/)",
    re.IGNORECASE)
_IPV4_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?(?![\d.])")
# ANSI 转义序列（颜色/光标控制）与其余 C0 控制字符：上游文本可能带，进了用户
# 终端会破坏排版甚至伪装输出。
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_control(text: object) -> str:
    """去掉 ANSI 转义与其它控制字符（保留换行/制表）。"""
    body = str(text if text is not None else "")
    if not body:
        return body
    return _CONTROL_RE.sub("", _ANSI_RE.sub("", body))


def host_is_public(host: str, extra_public_hosts: tuple = ()) -> bool:
    """主机名是否属于「可以对用户原样展示」的公开域名。

    本机/内网地址同样**不是**公开域名——出现在文档或日志里必须脱敏。
    不能用 `extra_public_hosts` 洗白本地地址：那里只用于真实对外的域名。
    """
    h = normalize_host(host)
    if not h:
        return True
    if is_local_host(h):
        return False
    for suffix in tuple(PUBLIC_HOST_SUFFIXES) + tuple(extra_public_hosts):
        s = str(suffix or "").strip().lower().lstrip(".")
        if s and (h == s or h.endswith("." + s)):
            return True
    return False


def sanitize_text(text: object, *, extra_public_hosts: tuple = ()) -> str:
    """把文本中的非公开域名替换为中性占位域名。

    用于「文档 / 报错 / 日志」类文本；**不用于**生成结果的媒体 URL（那是功能字段，
    调用方必须拿到真实地址才能访问产物）。

    四层处理，缺一层就有绕过（四层都对应实测过的绕过手法）：
      1. 去掉 ANSI 转义与控制字符；
      2. 带 scheme 的 URL（大小写不敏感）——主机名非公开即替换；
      3. 裸主机名 + 端口/路径（`gw-internal.acme.com:8443`）——非公开即替换；
      4. 裸主机名 / 裸私网 IP 单独出现——只替换判定为本机/内网的那些，
         避免把 `photo.png` 这类文件名误判成主机名。
    """
    body = strip_control(text)
    if not body:
        return body
    leaked: set[str] = set()

    def _repl(match: re.Match) -> str:
        host = match.group(1)
        if host_is_public(host, extra_public_hosts):
            return match.group(0)
        leaked.add(normalize_host(host))
        return f"https://{REDACTED_HOST}"

    result = _URL_RE.sub(_repl, body)

    # 同一文本里已判定为非公开的主机，其裸出现（无 scheme）也一并替换，
    # 避免 "host:443" / "host/path" 之类的写法漏网。
    for host in leaked:
        if host:
            result = re.sub(re.escape(host), REDACTED_HOST, result, flags=re.IGNORECASE)

    def _bare_tail(match: re.Match) -> str:
        host = match.group(1)
        if host_is_public(host, extra_public_hosts):
            return match.group(0)
        return REDACTED_HOST + match.group("tail")

    result = _BARE_HOST_TAIL_RE.sub(_bare_tail, result)

    def _bare(match: re.Match) -> str:
        host = match.group(1)
        return REDACTED_HOST if is_local_host(host) else match.group(0)

    result = _BARE_HOST_RE.sub(_bare, result)

    def _ip(match: re.Match) -> str:
        host = match.group(1)
        return REDACTED_HOST if is_local_host(host) else match.group(0)

    return _IPV4_RE.sub(_ip, result)


# 生成结果里承载媒体地址的字段名：这些字段的值**原样交付**（铁律 7）。
# 大小写不敏感比对。
MEDIA_KEYS = frozenset({
    "result", "results", "imageurl", "videourl", "audiourl",
    "url", "file_url", "downloadurl", "previewurl", "output", "content",
})

# 这些键的值是**文本产物本身**（文本模型的回复正文），原样交付，不做域名替换：
# 它是模型写给用户的内容，人读路径同样原样打印，两条路径口径必须一致。
TEXT_PRODUCT_KEYS = frozenset({"content"})


def _media_value(value: object, *, extra_public_hosts: tuple,
                 verbatim_text: bool = False) -> object:
    """媒体字段的取值处理。

    只放行「本身就是 http(s) 地址」的字符串——旧实现把整棵子树原样放行，
    于是 `result` / `output` / `url` 里嵌套的报错文本、内部域名可以绕过脱敏
    （已实测）。字典与列表继续按媒体规则递归，保证 `[{"url": "…"}]` 这类
    形态仍能原样交付；`verbatim_text=True`（文本产物）时字符串原样保留。
    """
    if isinstance(value, str):
        if verbatim_text or value.strip().startswith(("http://", "https://")):
            return value
        return sanitize_text(value, extra_public_hosts=extra_public_hosts)
    if isinstance(value, (list, tuple)):
        return [_media_value(item, extra_public_hosts=extra_public_hosts,
                             verbatim_text=verbatim_text) for item in value]
    if isinstance(value, dict):
        return sanitize_payload(value, extra_public_hosts=extra_public_hosts,
                                preserve_media=True)
    return value


def sanitize_payload(obj: object, *, extra_public_hosts: tuple = (),
                     preserve_media: bool = True) -> object:
    """递归脱敏一个 JSON 结构（就地返回新对象，不修改入参）。

    这是 `--json` 路径的脱敏入口：人读路径早就走了 `sanitize_text`，而 agent 消费
    的 `--json` 路径曾经完全没走，导致内部域名随 JSON 原样流到用户面前（已实测）。

    `preserve_media=True` 时，媒体字段（`MEDIA_KEYS`）里**本身就是 http(s) 地址**
    的字符串原样保留——生成结果的 URL 必须原样交付，否则用户拿不到产物。其余
    形态（嵌套结构、非地址文本）继续递归脱敏，避免整棵子树成为绕过口子。
    """
    if isinstance(obj, str):
        return sanitize_text(obj, extra_public_hosts=extra_public_hosts)
    if isinstance(obj, list):
        return [sanitize_payload(item, extra_public_hosts=extra_public_hosts,
                                 preserve_media=preserve_media) for item in obj]
    if isinstance(obj, tuple):
        return [sanitize_payload(item, extra_public_hosts=extra_public_hosts,
                                 preserve_media=preserve_media) for item in obj]
    if isinstance(obj, dict):
        out: dict = {}
        for key, value in obj.items():
            name = str(key).strip().lower()
            if preserve_media and name in MEDIA_KEYS:
                out[key] = _media_value(value, extra_public_hosts=extra_public_hosts,
                                        verbatim_text=name in TEXT_PRODUCT_KEYS)
            else:
                out[key] = sanitize_payload(value, extra_public_hosts=extra_public_hosts,
                                            preserve_media=preserve_media)
        return out
    return obj


# ---------------------------------------------------------------------------
# 终端排版：按**显示宽度**对齐（中文占 2 列）
# ---------------------------------------------------------------------------
# 目的：清单里有大量中文（模型名、类型名、表头），直接用 str.ljust/format 会
# 因为「1 个汉字 = 1 个字符但占 2 列」而对不齐；这里统一按终端显示宽度补白，
# 让 agent 与用户看到的表格是工整的。

def display_width(text: object) -> int:
    """字符串在等宽终端里占用的列数（East Asian Wide/Fullwidth 记 2 列）。"""
    width = 0
    for char in str(text if text is not None else ""):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad(text: object, width: int, align: str = "left") -> str:
    """按显示宽度补空格；超宽不截断（宁可不齐也不要丢信息）。"""
    body = str(text if text is not None else "")
    space = " " * max(width - display_width(body), 0)
    return space + body if align == "right" else body + space


def fit(text: object, width: int) -> str:
    """按显示宽度截断（超出用 … 收尾），用于把内容塞进定宽框内。"""
    body = str(text if text is not None else "")
    if display_width(body) <= width:
        return body
    kept = ""
    for char in body:
        if display_width(kept + char) > width - 1:
            break
        kept += char
    return kept + "…"
