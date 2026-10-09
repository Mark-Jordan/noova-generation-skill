#!/usr/bin/env python3
"""素材上传封装：第三方图床（thirdparty-a / thirdparty-b）+ 平台自有存储兜底。

用途
----
把本地图片/视频/音频上传成**公网可访问的 URL**，供生成接口的
`referenceImages` / `referenceVideos` / `referenceAudios` 等字段引用。

通道优先级（`host="auto"`，默认）
--------------------------------
1. `thirdparty-a`      —— 第三方图床（需浏览器指纹头），返回相对路径需补齐站点域名
2. `thirdparty-b` —— 第三方图床（无需额外头），返回绝对 URL
3. `platform`   —— 平台自有的上传凭证通道，直传平台自有存储（**需要 API Key**）

前两个通道是**第三方免费图床**，非平台自有服务：文件存储与访问由第三方提供，
可能存在文件被清理、链接失效、不校验真实文件内容等风险。请勿上传敏感或重要数据。
因此本模块**按顺序尝试、失败自动降级**，并在最终失败时汇总每个通道的原因。

设计约束
--------
- 只用 Python 标准库（与 skill 其余脚本一致，零安装依赖）。
- 不打印 API Key、不打印鉴权头。
- 通道名与 URL 都来自平台公开的「素材上传」接口文档，属公开信息。

被 `noova_media.py` 引用（`upload` 子命令、参考文件自动上传）。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 客户端标识与脚本路径统一来自 noova_key（单一来源，避免多处硬编码不一致）。
from noova_key import (  # noqa: E402
    SCRIPT_DIR,
    USER_AGENT as CLIENT_USER_AGENT,
    python_cmd,
)
from noova_common import (  # noqa: E402
    is_local_host,
    open_public,
    resolves_to_local,
    sanitize_text,
)

KEY_SCRIPT = (SCRIPT_DIR / "noova_key.py").as_posix()
# 照抄命令绑定当前解释器绝对路径（见 noova_key.PYTHON_CMD）。
KEY_CMD = python_cmd(KEY_SCRIPT)

# ---------------------------------------------------------------------------
# 通道定义（全部来自平台公开接口文档）
# ---------------------------------------------------------------------------

THIRDPARTY_A_BASE = "https://img.thirdparty-a.ee"
THIRDPARTY_A_UPLOAD_URL = f"{THIRDPARTY_A_BASE}/api/upload"
# thirdparty-a 站点做了同源校验：缺少浏览器指纹头会直接 403「不允许直接调用API」。
THIRDPARTY_A_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Origin": THIRDPARTY_A_BASE,
    "Referer": f"{THIRDPARTY_A_BASE}/",
}

THIRDPARTY_B_BASE = "https://thirdparty-b.thirdparty-b-example.chat"
THIRDPARTY_B_UPLOAD_URL = f"{THIRDPARTY_B_BASE}/api/upload"

# thirdparty-a 文档声明：视频 ≤ 20MB。超过时仍会尝试（图片可能不受此限），仅打印提示。
THIRDPARTY_A_DOC_SIZE_LIMIT = 20 * 1024 * 1024

# 本工具自己的硬上限：超过这个体积的素材几乎必然被图床拒绝，而上传过程会长时间
# 占用带宽与内存。提前拒绝比传到一半失败更有用（用户能立刻知道要压缩）。
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

# 客户端标识（见上：统一由 noova_key 提供，避免多处硬编码漂移）

# 这些主机是平台公开接口文档里点名的第三方图床，属公开信息，不做脱敏替换。
PUBLIC_UPLOAD_HOSTS = ("img.thirdparty-a.ee", "thirdparty-b.thirdparty-b-example.chat")

ALL_HOSTS = ("thirdparty-a", "thirdparty-b", "platform")
DEFAULT_HOST_ORDER = ALL_HOSTS

# 常见素材的 MIME 推断（图床据此做类型判断，尽量给准）
MEDIA_CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".gif": "image/gif", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".aac": "audio/aac", ".ogg": "audio/ogg",
}


def guess_content_type(name: str) -> str:
    """按扩展名推断 MIME；未知类型返回 octet-stream。"""
    return MEDIA_CONTENT_TYPES.get(Path(str(name or "")).suffix.lower(), "application/octet-stream")

HOST_LABELS = {
    "thirdparty-a": "第三方图床一",
    "thirdparty-b": "第三方图床二",
    "platform": "平台存储通道",
}


class UploadError(Exception):
    """上传失败（message 已脱敏，可直接展示）；`attempts` 记录每个通道的结果。"""

    def __init__(self, message: str, *, attempts: list[dict] | None = None, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.attempts = attempts or []
        self.hint = hint

    def render(self) -> str:
        parts = [f"错误：{self.message}"]
        for attempt in self.attempts:
            detail = attempt.get("detail") or attempt.get("status") or "-"
            parts.append(f"  · {HOST_LABELS.get(attempt.get('host', ''), attempt.get('host', '?'))}：{detail}")
        if self.hint:
            parts.append(f"→ {self.hint}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
#  Multipart 编码（标准库实现）
# ---------------------------------------------------------------------------

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    """把文件名规整为 ASCII 安全形态（保留扩展名），避免非 ASCII 文件名编码歧义。

    - 非 ASCII 字符按连续段折叠为一个 `_`，并去掉首尾的 `.`/`_`/`-`（避免产出 `-.png` 这类畸形名）。
    - 首尾清理后为空（例如纯中文名）时回退为 `file`；图床会自行重命名，不依赖原名。
    """
    raw = Path(str(name or "")).name.strip() or "file"
    suffix = Path(raw).suffix
    stem = raw[: len(raw) - len(suffix)] if suffix else raw
    stem = _SAFE_FILENAME_RE.sub("_", stem).strip("._-") or "file"
    safe_suffix = _SAFE_FILENAME_RE.sub("", suffix) or ""
    return f"{stem[:80]}{safe_suffix}"


def encode_multipart(field_name: str, filename: str, content: bytes,
                     content_type: str) -> tuple[str, bytes]:
    """构造 `multipart/form-data` 请求体，返回 (Content-Type, body)。

    **注意**：上传实际走 `_post_file()` 的流式生成器（大文件不整体驻留内存）。
    这个函数保留给需要一次性构造 body 的调用方与单元测试（它验证了分隔符与
    Content-Disposition 的格式正确性）。
    """
    boundary = f"----NooVaSkillBoundary{uuid.uuid4().hex}"
    disposition = (
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    body = b"".join([
        f"--{boundary}\r\n".encode("utf-8"),
        disposition,
        content,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ])
    return f"multipart/form-data; boundary={boundary}", body


def _post_file(url: str, *, path: Path, content_type: str, headers: dict[str, str],
               timeout: int, field_name: str = "file") -> tuple[int, bytes]:
    """把本地文件以 multipart/form-data POST 到图床，返回 (HTTP 状态码, 响应体)。

    内存纪律：分块构造请求体，**不把整个文件读进内存两次**。早期实现先 `read_bytes()`
    再拼一份 body，等于同时驻留 2× 文件体积；大文件（视频素材）在低内存机器上会直接
    MemoryError。这里只读一次，并在拼装时按块写入 `bytearray`。
    """
    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise UploadError(
            f"文件过大（{size / 1024 / 1024:.1f} MB，上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）",
            hint="请先压缩或裁剪素材后重试；过大的文件几乎必然被图床拒绝")

    boundary = f"----NooVaSkillBoundary{uuid.uuid4().hex}"
    disposition = (
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{safe_filename(path.name)}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    head = f"--{boundary}\r\n".encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    total = len(head) + len(disposition) + size + len(tail)

    def _stream():
        """生成器：urllib 支持用可迭代对象做 body，按块发送，不整体驻留内存。"""
        yield head
        yield disposition
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        yield tail

    request = urllib.request.Request(url, data=_stream(), method="POST", headers={
        **headers,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(total),
    })
    try:
        # 不带凭据的公开请求：允许跨源跳转（图床常用 CDN），但拒绝跳到本机/内网。
        with open_public(request, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            pass
        return int(exc.code), raw


# ---------------------------------------------------------------------------
# 响应解析（纯函数，便于单测）
# ---------------------------------------------------------------------------

def absolutize(base: str, value: str) -> str:
    """把图床返回的相对路径补齐为绝对 URL；已是绝对 URL 则原样返回。

    已是绝对 URL 时**只放行公网地址**：第三方返回的任意绝对 URL 曾经被原样透传，
    随后 `--verify` 会让**用户本机**去 GET 它（`http://127.0.0.1:9/evil.png` 也能通过），
    等于把本机当探测跳板。指向本机/内网的地址直接判为不可用。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        host = urllib.parse.urlsplit(text).hostname or ""
        if not host or is_local_host(host) or resolves_to_local(host) is True:
            raise UploadError("图床返回了指向本机 / 内网的地址，已拒绝使用",
                              hint="这是异常响应；请换一个上传通道（--host）后重试")
        return text
    if text.startswith("//"):
        return f"{urllib.parse.urlparse(base).scheme}:{text}"
    return f"{base.rstrip('/')}/{text.lstrip('/')}"


def parse_thirdparty-a_response(payload: object) -> str:
    """从 thirdparty-a 响应中取回可引用的绝对直链；失败抛 UploadError。

    成功响应形如：
        {"success": true, "url": "/api/file/xxx.png",
         "directUrl": "/api/file/xxx.png", "previewUrl": "/view/yyy"}
    优先 `directUrl`（直链），退回 `url`；`previewUrl`/`shortUrl` 是预览页，不作为素材地址。
    """
    if not isinstance(payload, dict):
        raise UploadError("图床返回了无法解析的内容")
    if payload.get("success") is False:
        reason = str(payload.get("message") or payload.get("msg") or payload.get("error") or "").strip()
        raise UploadError(f"图床拒绝上传{f'：{reason}' if reason else ''}")
    for field_name in ("directUrl", "url", "data"):
        value = payload.get(field_name)
        if isinstance(value, dict):
            value = value.get("url") or value.get("directUrl")
        url = absolutize(THIRDPARTY_A_BASE, str(value or ""))
        if url:
            return url
    raise UploadError("图床响应中未包含文件地址")


def parse_thirdparty-b_response(payload: object) -> str:
    """从 thirdparty-b 响应中取回绝对 URL；失败抛 UploadError。

    成功响应形如：{"url": "https://thirdparty-b.thirdparty-b-example.chat/api/proxy/image/xxx.png",
                   "created": 1786436842981}
    """
    if not isinstance(payload, dict):
        raise UploadError("图床返回了无法解析的内容")
    url = absolutize(THIRDPARTY_B_BASE, str(payload.get("url") or ""))
    if url:
        return url
    reason = str(payload.get("message") or payload.get("error") or "").strip()
    raise UploadError(f"图床响应中未包含文件地址{f'：{reason}' if reason else ''}")


def _decode(raw: bytes) -> object:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return text


def _brief(raw: bytes, limit: int = 160) -> str:
    """第三方响应的**简短、已脱敏**摘要（用于错误信息）。

    第三方可能回显自己的内部地址或 CDN 域名；这些字符串会直接进用户可见的 stderr，
    因此必须过一遍脱敏（原先原样回显最多 160 字符）。
    """
    text = raw.decode("utf-8", errors="replace").strip().replace("\n", " ")
    return sanitize_text(text[:limit])


# ---------------------------------------------------------------------------
# 三个通道
# ---------------------------------------------------------------------------

def upload_via_thirdparty-a(path: Path, *, content_type: str, timeout: int = 120) -> str:
    """通道一：thirdparty-a 图床（携带浏览器指纹头，否则 403）。"""
    status, raw = _post_file(THIRDPARTY_A_UPLOAD_URL, path=path, content_type=content_type,
                             headers=THIRDPARTY_A_BROWSER_HEADERS, timeout=timeout)
    if status >= 400:
        raise UploadError(f"HTTP {status}{f'：{_brief(raw)}' if raw else ''}")
    return parse_thirdparty-a_response(_decode(raw))


def upload_via_thirdparty-b(path: Path, *, content_type: str, timeout: int = 120) -> str:
    """通道二：thirdparty-b 图床（无需额外请求头）。"""
    status, raw = _post_file(THIRDPARTY_B_UPLOAD_URL, path=path, content_type=content_type,
                             headers={"User-Agent": CLIENT_USER_AGENT}, timeout=timeout)
    if status >= 400:
        raise UploadError(f"HTTP {status}{f'：{_brief(raw)}' if raw else ''}")
    return parse_thirdparty-b_response(_decode(raw))


def upload_via_platform(path: Path, *, content_type: str, timeout: int = 180,
                        platform_uploader: Callable[[Path, str, int], str] | None = None) -> str:
    """通道三：平台自有上传（签发凭证 → 直传平台自有存储），需要 API Key。

    `platform_uploader` 由 `noova_media.py` 注入（复用其鉴权、错误映射与脱敏逻辑）。
    """
    if platform_uploader is None:
        raise UploadError("平台存储通道不可用（缺少平台上传实现）")
    return platform_uploader(path, content_type, timeout)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def upload_file(path: Path, *, api_key: str | None = None, content_type: str | None = None,
                host: str = "auto", timeout: int = 180,
                platform_uploader: Callable[[Path, str, int], str] | None = None,
                log: Callable[[str], None] | None = None) -> dict:
    """把本地文件上传为公网 URL；按 `host` 决定通道。

    - `host="auto"`（默认）：依次尝试 thirdparty-a → thirdparty-b → platform，任一成功即返回。
      platform 通道仅在提供了 API Key 时参与，否则跳过（不影响前两个通道）。
    - `host=<通道名>`：只用该通道（便于定位问题，不做降级）。

    返回 `{"url", "provider", "bytes", "content_type"}`；全部失败抛 `UploadError`
    （其 `attempts` 逐条记录每个通道的结果）。
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise UploadError(f"文件不存在或不是普通文件：{target}")
    size = target.stat().st_size
    if size <= 0:
        raise UploadError("文件内容为空，无法上传")

    resolved_type = content_type or guess_content_type(target.name)

    if host != "auto":
        if host not in ALL_HOSTS:
            raise UploadError(f"未知的上传通道：{host}",
                              hint="可用通道：" + "、".join(ALL_HOSTS) + "（auto = 依次尝试）")
        order: tuple[str, ...] = (host,)
    else:
        order = DEFAULT_HOST_ORDER

    tell = log or (lambda message: print(message, file=sys.stderr))
    attempts: list[dict] = []
    show_progress = host == "auto" and len(order) > 1

    for index, name in enumerate(order, start=1):
        if name == "platform" and not api_key:
            attempts.append({"host": name, "ok": False, "detail": "跳过（未配置 API Key）"})
            continue
        if show_progress:
            tell(f"[上传 {index}/{len(order)}] 使用{HOST_LABELS.get(name, name)}…")
        if name == "thirdparty-a" and size > THIRDPARTY_A_DOC_SIZE_LIMIT:
            tell(f"[提示] 文件 {size // 1024 // 1024}MB 超过第三方图床一文档声明的 20MB 上限，仍会尝试")
        try:
            if name == "thirdparty-a":
                url = upload_via_thirdparty-a(target, content_type=resolved_type, timeout=timeout)
            elif name == "thirdparty-b":
                url = upload_via_thirdparty-b(target, content_type=resolved_type, timeout=timeout)
            else:
                url = upload_via_platform(target, content_type=resolved_type, timeout=timeout,
                                          platform_uploader=platform_uploader)
        except UploadError as exc:
            attempts.append({"host": name, "ok": False, "detail": exc.message})
            tell(f"[上传失败] {HOST_LABELS.get(name, name)}：{exc.message}")
            continue
        except Exception as exc:  # noqa: BLE001
            # 非 UploadError 的异常文本可能内插上游地址或本机路径，必须脱敏后再进 detail。
            attempts.append({"host": name, "ok": False,
                             "detail": sanitize_text(f"{type(exc).__name__}: {exc}")})
            tell(f"[上传失败] {HOST_LABELS.get(name, name)}：{type(exc).__name__}")
            continue
        attempts.append({"host": name, "ok": True, "detail": url})
        return {"url": url, "provider": name, "bytes": size, "content_type": resolved_type}

    if host == "auto":
        if not api_key and any(a["host"] == "platform" for a in attempts):
            hint = (f"第三方图床均不可用。平台存储通道需要 API Key："
                    f"先执行 {KEY_CMD} setup --stdin 后重试")
        else:
            hint = "第三方图床为免费服务，可能临时不可用；可稍后重试或更换文件后重试"
    elif host == "platform":
        hint = (f"平台存储通道需要有效 API Key："
                f"先执行 {KEY_CMD} setup --stdin 后重试")
    else:
        hint = "该图床为第三方免费服务，可能限流或临时故障；可改用 --host auto 自动降级到其它通道"
    raise UploadError("所有上传通道均失败", attempts=attempts, hint=hint)


def verify_url(url: str, *, timeout: int = 30) -> tuple[bool, str]:
    """校验上传结果是否真的可访问；返回 (是否可用, 说明)。仅做提示，不阻断流程。

    两条纪律：

    1. **403 不是「通过」**。403 恰恰证明该地址不可公开访问——曾经把它归入
       「该图床不响应校验请求」而返回 `True`，于是用户看到「校验：通过」，
       而把该 URL 交给生成接口时上游取不到图。403 现在如实报为不可用。
       405/416 才是真的「不支持这种校验方式」，仍视为无法判定。
    2. **不得把用户本机当探测跳板**。URL 来自第三方响应或用户输入，直接 GET 会
       让本机去访问任意地址（SSRF 面）。因此先拒绝本机/内网目标（含解析后判定），
       并禁止重定向跳到内网。
    """
    text = str(url or "").strip()
    if not text.startswith(("http://", "https://")):
        return False, "地址不是 http(s) 绝对地址"
    parsed = urllib.parse.urlsplit(text)
    host = parsed.hostname or ""
    if not host:
        return False, "地址缺少主机名"
    if is_local_host(host) or resolves_to_local(host) is True:
        return False, "地址指向本机 / 内网，已跳过校验（不做本机探测）"
    request = urllib.request.Request(text, method="GET", headers={
        "User-Agent": CLIENT_USER_AGENT,
        "Range": "bytes=0-0",
    })
    try:
        # 公开请求：允许跨源跳转（CDN），但拒绝跳到本机/内网。
        with open_public(request, timeout=timeout) as resp:
            size = resp.headers.get("Content-Range") or resp.headers.get("Content-Length") or "-"
            return 200 <= int(resp.status) < 400, f"HTTP {resp.status}，大小标识 {size}"
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        if code in (405, 416):
            # 该图床不响应带 Range 的 GET / 不支持该请求方式：无法判定，不当失败。
            return True, f"HTTP {code}（该图床不响应这种校验请求，视为无法判定）"
        if code == 403:
            return False, "HTTP 403（该地址不可公开访问）"
        return False, f"HTTP {code}"
    except urllib.error.URLError as exc:
        return False, f"连接失败：{sanitize_text(exc.reason)}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"
