#!/usr/bin/env python3
"""NooVa AI 公开 API 标准调用封装（零依赖，仅 Python 标准库）。

设计目标：让调用方 agent **不需要理解任何上游差异**，只发指令、拿结果。
本脚本负责：模型发现 → 参数查询 → 请求构造 → 协议适配 → 轮询到终态 → 结果交付。

子命令一览
----------
  form       交互问答板（类型 / 模型 / 参数 三级面板，供宿主 agent 弹给用户确认）
  models      列出可用模型（运行时拉取；按类型分区，含模型编码/协议/状态/计价）
  model       查看单个模型的完整契约（调用方式 + 计费 + 参数契约）
  params      查看单个模型的结构化参数契约（JSON，适合 agent 直接消费）
  image       生成图片（任务型/异步：创建任务 → 轮询到终态 → 输出全部结果 URL）
  video       生成视频（同上）
  audio       生成音频（同上；平台无可用音频模型时会明确说明，不会编造模型编码）
  chat        文本生成（同步返回；按模型协议走官方原生路由，支持 --stream 流式）
  task        查询一次任务状态（不轮询；失败原因在响应体 error 字段）
  wait        把已创建的任务轮询到终态（配合 --no-wait 使用）
  upload      上传本地文件（参考图/首帧/参考音频）→ 返回可被生成接口引用的 URL
  credit      查询账户积分余额

输出与反馈约定
--------------
- 结果 URL 与计费反馈走 **stdout**（用户/调用方要拿到的东西）；
  `[提交]` / `[轮询]` 等过程信息走 **stderr**。
- `--json` 时 stdout 只有原始响应（可被程序解析），人读信息块改走 stderr。
- 生成结束后默认输出「模型 / 任务 ID / 计价 / 本次扣减 / 剩余积分」；
  「本次扣减」口径 = 生成前后两次 `GET /api/v1/credit` 的**余额差值**（可核对）。
  不想多花这两次请求时加 `--no-credit`。
- 耗时预期按类型给出区间（图像 30s–5min、音频 30s–10min、视频 1min–1.5h），
  并据此决定默认等待上限；**等待超时 ≠ 任务失败**，超时会打印任务 ID 供续查。

配置
----
首次使用会打印引导块（含 API 直达地址与领取 Key 的方式）；用户把 Key 粘贴过来后，
由 `noova_key.py setup` 自动写入本机配置文件，之后无需再配置。
自检命令：`noova_key.py doctor`（一眼看出还差什么）。

端点选择
--------
- **文本模型**：按模型自身协议走官方原生路由 —— `anthropic` → `/v1/messages`，
  `openai` → `/v1/chat/completions`（平台当前实测只有这两类；其它分支为前向兼容）。
  不使用统一入口；路由以参数契约的 `protocols[0]`（主协议）为准。
- **图像/视频/音频**：统一创建接口 `POST /api/v1/invoke`，创建后用 `POST /v1/content` 轮询。

安全约定
--------
- 只访问本 skill 文档中列出的公开端点，绝不访问任何内部接口。
- 输出中不打印 API Key、不打印鉴权头。
- **文档与错误文本**里的非公开域名（基础设施/存储/第三方 CDN）会被替换为中性占位域名；
  生成结果的媒体 URL 属于业务功能字段，按原值交付（调用方需要它才能访问产物）。

常用示例（命令前缀用当前解释器绝对路径，见 `PYTHON_CMD`）
--------
  <PYTHON> noova_media.py models --type image
  <PYTHON> noova_media.py params --code <模型编码> --json
  <PYTHON> noova_media.py image --prompt "一只戴墨镜的猫"
  <PYTHON> noova_media.py video --prompt "夕阳下的城市天际线" --model <模型编码> --param duration=5
  <PYTHON> noova_media.py chat  --prompt "写一句品牌标语" --model <模型编码>
  <PYTHON> noova_media.py chat  --prompt "写一段介绍" --stream
  <PYTHON> noova_media.py upload --file ./photo.png
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from noova_key import (  # noqa: E402
    DEFAULT_BASE_URL,
    PACKAGE_NAME,
    PYTHON_CMD,
    SCRIPT_DIR,
    USER_AGENT,
    VERSION as CLIENT_VERSION,
    check_base_url,
    console_url,
    get_api_key,
    get_base_url,
    python_cmd,
    render_guide,
)
from noova_common import (  # noqa: E402
    # `REDACTED_HOST` 是**再导出**：外部（测试与文档）按 `noova_media.REDACTED_HOST`
    # 引用这个占位域名，实现本体在 noova_common。
    REDACTED_HOST,
    PUBLIC_HOST_SUFFIXES as PUBLIC_HOST_SUFFIXES_COMMON,
    debug_enabled,
    display_width as _display_width,
    host_is_public,
    is_local_host,
    open_credentialed,
    pad as _pad,
    sanitize_payload,
    sanitize_text,
    strip_control,
)
from noova_upload import (  # noqa: E402
    ALL_HOSTS as UPLOAD_HOSTS,
    HOST_LABELS as UPLOAD_HOST_LABELS,
    UploadError,
    safe_filename,
    upload_file,
    verify_url,
)

# Windows 控制台默认可能不是 UTF-8，统一输出编码，避免中文/report 出现 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 客户端标识与配置读取统一来自 `noova_key`（单一来源，避免多处硬编码不一致）。
# 所有请求都必须带显式客户端标识：部分边缘网络策略会拦截「自动化客户端的默认标识」，
# 例如 Python 标准库 urllib 的默认 UA 会被直接拒绝。
# 用户可见提示里一律使用**绝对路径**，因为 agent 的工作目录不固定。
KEY_SCRIPT = (SCRIPT_DIR / "noova_key.py").as_posix()
SELF_SCRIPT = Path(__file__).resolve().as_posix()
# 用户/agent 照抄的命令绑定当前解释器绝对路径（见 noova_key.PYTHON_CMD）。
KEY_CMD = python_cmd(KEY_SCRIPT)
SELF_CMD = python_cmd(SELF_SCRIPT)

# 平台公开的「官方接入路由」映射：模型协议 → 用户侧请求路径。
# 与平台模型文档「官方接入路由（X，推荐）」小节对齐。平台当前实测只出现 openai / anthropic。
PROTOCOL_PATHS = {
    "openai": "/v1/chat/completions",
    "anthropic": "/v1/messages",
    # 平台确实注册了该路由（实测：POST → 400「请求体缺少 model」，非 404）。
    # 当前公开模型文档均未把 Responses 声明为接入路由，故默认不会被选中；
    # 这里保留映射，是为了将来某模型文档声明「官方接入路由（Responses）」时能被正确识别，
    # 而不是被静默丢弃。
    "responses": "/v1/responses",
}
PROTOCOL_LABELS = {"openai": "OpenAI", "anthropic": "Anthropic",
                   "responses": "OpenAI Responses"}

# 任务型（图像/视频/音频）创建入口：平台文档对该类型模型给出的创建路由。
TASK_CREATE_PATH = "/api/v1/invoke"
TASK_POLL_PATH = "/v1/content"
# 账户积分查询（公开文档端点）。仅在**生成前后**各取一次快照，用于展示
# 「本次扣减 + 剩余积分」；失败不影响生成，绝不因为展示余额而中断任务。
CREDIT_PATH = "/api/v1/credit"

# 模型参数契约：**对外唯一真源**，需 API Key。
# 与网站/控制台用的对内接口严格分离（后者按登录态鉴权、含展示与内部字段，字段口径也不同）。
# 该端点只回「发起请求 + 计价 + 向用户确认」三者所需的最小字段，不含任何展示或上游信息。
MODEL_PARAMS_PATH = "/api/models/params"
# 参数契约版本。客户端只认自己支持的那一版：服务端给出版本号且不等于本值时 fail-closed
# （宁可明确报错，也绝不按错的口径向用户报价）。
SUPPORTED_PARAM_SCHEMA_VERSION = 1
# 对外可见的模型状态白名单：**只呈现两类**——
#   online（上线，可调用）/ maintenance（维护中，暂不可调用）。
# 契约里可能下发 test / deprecated / 未知状态，它们不属于对外可见集合，
# 在唯一取数入口 `_fetch_public_model_specs` 即被剔除；下游（清单 / 选择面板 /
# 参数 / 调用与错误提示）全部消费它的结果，单点收敛不会漏。
# 展示时**必须**用 `_status_label` 注明每条模型是「在线」还是「维护中」。
VISIBLE_MODEL_STATUSES = ("online", "maintenance")
# 其中唯一可发起调用的状态（maintenance 只展示、不可选）。
CALLABLE_MODEL_STATUS = "online"
# 积分 ↔ 人民币换算比例兜底值（100 积分 = 1 元）。
# 真值由响应体 `creditPerYuan` 给出；这里只是响应缺该字段时的兜底，不参与计价本身。
DEFAULT_CREDITS_PER_YUAN = 100

# 生成结果/文档中允许原样保留的公开域名后缀（其余一律视为非公开，做占位替换）。
# 与脱敏实现一并下沉到 `noova_common`（单一来源）；这里只做再导出，保持既有引用可用。
PUBLIC_HOST_SUFFIXES = PUBLIC_HOST_SUFFIXES_COMMON

MODEL_TYPES = ("text", "image", "video", "audio")

# 文本模型：同步返回；其余类型：任务型（可能直接成功，也可能返回 task_id 需轮询）
SYNC_MODEL_TYPES = ("text",)
TASK_MODEL_TYPES = ("image", "video", "audio")

TYPE_LABELS = {"text": "文本", "image": "图像", "video": "视频", "audio": "音频"}

# 各类型调用模式（用于清单与提示语，保证「怎么调」对用户口径统一）。
TYPE_MODE_HINTS = {
    "text": "同步返回（一次请求直接拿正文；可 --stream 流式输出）",
    "image": "任务型/异步（创建任务 → 拿到任务 ID → 轮询到终态）",
    "video": "任务型/异步（创建任务 → 拿到任务 ID → 轮询到终态）",
    "audio": "任务型/异步（创建任务 → 拿到任务 ID → 轮询到终态）",
}

# 各类型生成的**典型耗时区间**（秒）。
# 两个用途：① agent 提前把预期告诉用户，避免用户以为卡死；② 决定默认等待上限，
# 避免长任务（尤其视频）被默认上限截断而误报「超时/失败」。
# 取值口径：平台实际任务的常见区间（图像 30s–5min、音频 30s–10min、视频 1min–1.5h），
# 只用于**预期沟通**与默认等待上限，不是硬性 SLA。
ETA_SECONDS = {
    "text": (1, 10),
    "image": (30, 300),
    "audio": (30, 600),
    "video": (60, 5400),
}
# 各类型默认等待上限（秒）：在耗时上界之上留出余量，仍可用 --max-wait 覆盖。
DEFAULT_MAX_WAIT = {
    "image": 600,
    "audio": 1200,
    "video": 5400,
}
DEFAULT_MAX_WAIT_FALLBACK = 900

TERMINAL_OK = ("succeeded", "success", "completed", "complete", "finished")
TERMINAL_FAIL = ("failed", "failure", "error", "canceled", "cancelled", "expired")

# 任务/模型状态 → 中文标签。
# - 任务侧：词表外的状态一律按「处理中」处理，绝不当成失败；
# - 模型侧：清单里的 `status` 只有 `online` / `maintenance` 两种，同样给出中文对照。
STATUS_LABELS = {
    "not_start": "等待中", "queued": "排队中", "submitted": "已提交",
    "in_progress": "生成中", "processing": "生成中", "running": "生成中",
    "unknown": "处理中", "": "处理中",
    "succeeded": "已完成", "success": "已完成", "completed": "已完成",
    "complete": "已完成", "finished": "已完成",
    "failed": "失败", "failure": "失败", "error": "失败",
    "canceled": "已取消", "cancelled": "已取消", "expired": "已过期",
    "online": "在线", "maintenance": "维护中",
}

class NoovaError(Exception):
    """本 skill 的统一错误类型；message 已脱敏，可直接展示给用户。"""

    def __init__(self, message: str, *, code: int | None = None, retry_after: int | None = None,
                 hint: str | None = None, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retry_after = retry_after
        self.hint = hint
        self.details = details or []

    def render(self) -> str:
        parts = [f"错误：{self.message}"]
        if self.code:
            parts.append(f"（HTTP {self.code}）")
        if self.retry_after:
            parts.append(f"建议 {self.retry_after} 秒后重试")
        if self.hint:
            parts.append(f"→ {self.hint}")
        text = " ".join(parts)
        if self.details:
            text += "\n" + "\n".join(f"  · {line}" for line in self.details)
        return text


# ---------------------------------------------------------------------------
# 脱敏：实现已下沉到 `noova_common`（单一来源）
# ---------------------------------------------------------------------------
# 这里只保留 `_safe()`：它负责把「当前生效的基础域名」加进公开白名单。
# 判据本身（哪些主机算非公开、如何处理大小写/裸主机名/控制字符）
# 全在 `noova_common.sanitize_text()`——曾经的本地实现被 `HTTPS://internal.corp/x`
# 这类大写 scheme 绕过（已实测）。


def _public_hosts() -> tuple[str, ...]:
    """把当前生效的基础域名视为公开（不做占位替换）。"""
    hosts: list[str] = []
    try:
        host = urllib.parse.urlparse(get_base_url()).hostname or ""
        if host:
            hosts.append(host)
    except Exception:  # noqa: BLE001
        pass
    return tuple(hosts)


def _safe(text: object) -> str:
    return sanitize_text(strip_control(text), extra_public_hosts=_public_hosts())


# 兼容别名：`_host_is_public` 是实现细节，判据在 `noova_common`（单一来源）。
_host_is_public = host_is_public


def _safe_obj(payload: object) -> object:
    """`--json` 路径的脱敏入口（媒体字段原样保留，见 `noova_common.sanitize_payload`）。

    **agent 消费的就是这条路**，而它曾经完全没走脱敏：同一份响应，人读模式打印
    `media.example.com`、`--json` 却打印 `internal-gw.corp.local`（已实测）。
    agent 会把 JSON 原样转述给用户，于是内部域名照样出现在用户可见输出里。
    """
    return sanitize_payload(payload, extra_public_hosts=_public_hosts())


def _dump_json(payload: object) -> str:
    """序列化一个**已脱敏**的载荷（stdout 的唯一 JSON 出口）。"""
    return json.dumps(_safe_obj(payload), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 终端排版：按**显示宽度**对齐（中文占 2 列）
# ---------------------------------------------------------------------------
# `_display_width` / `_pad` 由 `noova_common` 提供（单一来源）：它们曾在
# `noova_key.py` 与 `noova_media.py` 里各有一份逐字相同的实现。


def _rule(title: str = "", width: int = 78) -> str:
    """分节横线（可带标题），用于把清单切分成清晰的区块。"""
    if not title:
        return "─" * width
    head = f" {title} "
    return "──" + head + "─" * max(width - 2 - _display_width(head), 0)


def _human_duration(seconds: float) -> str:
    """秒 → 便于阅读的时长文案（用于耗时预期与已等待时长）。"""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if value < 60:
        return f"{int(round(value))} 秒"
    if value < 3600:
        minutes = value / 60
        return f"{int(round(minutes))} 分钟" if abs(minutes - round(minutes)) < 0.05 \
            else f"{minutes:.1f} 分钟"
    return f"{value / 3600:.1f} 小时"


def _eta_text(model_type: str) -> str:
    """该类型生成的典型耗时区间文案（如「30 秒 – 5 分钟」）。"""
    span = ETA_SECONDS.get(str(model_type or "").strip().lower())
    if not span:
        return "耗时不定"
    return f"{_human_duration(span[0])} – {_human_duration(span[1])}"


def _wait_limit_text(seconds: int) -> str:
    """等待上限文案，例如 `600 秒（10 分钟）`。"""
    return f"{int(seconds)} 秒（{_human_duration(seconds)}）"


def _status_label(status: object) -> str:
    key = str(status or "").strip().lower()
    return STATUS_LABELS.get(key, key or "处理中")



# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> object:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return text


def _extract_error_message(payload: object) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or "").strip()
        if isinstance(err, str):
            return err.strip()
        for key in ("message", "msg", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(payload, str):
        return payload.strip()
    return ""


def _friendly_error(code: int, message: str, payload: object, retry_after: int | None) -> NoovaError:
    """把 HTTP 错误翻译成对用户友好的说明；不透出任何内部实现信息。"""
    detail = _safe(message) if message else ""
    data = payload.get("data") if isinstance(payload, dict) else None
    source = str((data or {}).get("source") or "") if isinstance(data, dict) else ""

    if code == 401:
        return NoovaError("API Key 无效或已停用", code=code,
                          hint=f"请到 {console_url()} 重新获取，并重新保存 Key")
    if code == 402:
        return NoovaError("账户积分不足", code=code,
                          hint=f"请到 {get_base_url()} 充值后重试")
    if code == 403:
        if re.search(r"error code:\s*1010", detail):
            return NoovaError("请求被网络策略拦截（客户端标识被拒绝）", code=code,
                              hint="当前脚本已带标准客户端标识；若仍失败，请反馈该现象")
        return NoovaError(detail or "无权调用该模型", code=code,
                          hint="该 API Key 可能没有此模型的调用权限")
    if code == 404:
        return NoovaError(detail or "未找到目标资源（模型或任务不存在）", code=code,
                          hint="请用 models 子命令重新拉取最新模型清单")
    if code == 429:
        return NoovaError(detail or "请求过于频繁，已被限流", code=code, retry_after=retry_after or 10,
                          hint="降低并发或按 Retry-After 等待后重试")
    if code == 503 and source == "platform_quota":
        return NoovaError("平台繁忙（账户分钟配额已满）", code=code, retry_after=retry_after or 60)
    if code in (500, 502, 503, 504):
        return NoovaError(detail or "服务暂时不可用", code=code, retry_after=retry_after or 5,
                          hint="请稍后重试；连续失败可联系官方支持")
    if code == 400:
        # **确定性失败：重试永远不会变好。** 平台对 400 也照样回 `Retry-After`，
        # 若把它透成「建议 N 秒后重试」，会把「改参数」误导成「等一等再试」
        # （实测：比例传展示标签 → 400 + Retry-After: 60，而重试恒为 400）。
        return NoovaError(detail or "请求参数不合法", code=code,
                          hint="请按该模型的运行时参数契约核对参数名与取值："
                               "params --code <模型编码> --json（本类错误重试无效）")
    if code in (408, 425):
        # 超时/过早重发属于**可重放**失败：保留 Retry-After 的提示才成立。
        return NoovaError(detail or f"请求失败（HTTP {code}）", code=code,
                          retry_after=retry_after or 5)
    # 其余 4xx 同为确定性失败：不透传 retry_after，避免诱导无意义重试。
    return NoovaError(detail or f"请求失败（HTTP {code}）", code=code)


def _build_request(path: str, *, key: str | None, body: object = None, method: str = "GET",
                   accept: str = "application/json", extra_headers: dict | None = None):
    # 所有出网请求的唯一构造点：在这里统一拦截「非线上部署域名」的基础地址，
    # 确保任何路径（含只读探测、平台存储直传）都不会连到本机/内网/开发环境地址。
    url = f"{_ensure_online_base_url()}{path}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "X-NooVa-Client": f"skill/{CLIENT_VERSION}",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    return urllib.request.Request(url, data=data, headers=headers, method=method)


def _open(path: str, *, key: str | None, body: object = None, method: str = "GET", timeout: int = 60,
          accept: str = "application/json", extra_headers: dict | None = None):
    """发一次请求并返回原始响应对象（调用方负责读取/关闭）。

    走 `open_credentialed`：跨源重定向会被拒绝。`urlopen` 默认跟随重定向并**复制
    Authorization 头**，于是上游一个 302 就能让 Key 改道到未校验的主机（已实测）。
    """
    req = _build_request(path, key=key, body=body, method=method, accept=accept,
                         extra_headers=extra_headers)
    return open_credentialed(req, timeout=timeout)


def _request(method: str, path: str, *, body: object = None, key: str | None = None,
             timeout: int = 60, max_retries: int = 2, retry: bool = True,
             extra_headers: dict | None = None) -> object:
    """发一次 JSON 请求，返回解析后的响应体；任何非 2xx / 非 JSON 都转成 NoovaError。

    `extra_headers` 供协议原生路由使用（例如 Anthropic 必须带 `anthropic-version`）。

    `retry=False` 用于**非幂等的计费 POST**（创建任务、文本生成）：对它们自动重试
    有重复扣费的风险——若 503 是中间层在**请求已被受理之后**返回的，重试就会创建
    两个任务、扣两次费。这类请求把重试交给用户显式决定（重新执行一次命令）。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            with _open(path, key=key, body=body, method=method, timeout=timeout,
                       extra_headers=extra_headers) as resp:
                payload = _decode(resp.read())
                if payload is None:
                    raise NoovaError("服务返回了空响应")
                return payload
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                pass
            payload = _decode(raw)
            retry_after = None
            try:
                header_value = exc.headers.get("Retry-After") if exc.headers else None
                if header_value and str(header_value).strip().isdigit():
                    retry_after = int(str(header_value).strip())
                elif isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    ms = payload["data"].get("retry_after_ms")
                    if isinstance(ms, (int, float)) and ms > 0:
                        retry_after = max(1, int(float(ms) / 1000))
            except Exception:  # noqa: BLE001
                retry_after = retry_after
            error = _friendly_error(int(exc.code), _extract_error_message(payload), payload, retry_after)
            # 限流：有限次自动等待重试（生成接口按 IP 60/min）。仅对可安全重放的请求。
            if retry and error.code in (429, 503) and attempt <= max_retries:
                wait = min(int(error.retry_after or 5), 60)
                print(f"[限流] {wait}s 后重试（第 {attempt} 次）", file=sys.stderr)
                time.sleep(wait)
                continue
            raise error from exc
        except urllib.error.URLError as exc:
            raise NoovaError(f"网络连接失败：{_safe(exc.reason)}",
                             hint="请检查本机网络或稍后重试") from exc
        except (TimeoutError, socket.timeout) as exc:
            # `socket.timeout` 在 Python 3.8/3.9 上**不是** `TimeoutError` 的别名
            # （3.10 才合并），只捕 TimeoutError 会让 3.8/3.9 用户看到 traceback。
            raise NoovaError("请求超时", hint="请稍后重试，或增大 --timeout") from exc
        except (http.client.HTTPException, ConnectionError, OSError) as exc:
            # 响应被截断（IncompleteRead）、连接被重置、磁盘/套接字层错误……
            # 这些都不该变成 traceback——traceback 会打印 skill 的绝对安装路径。
            raise NoovaError(f"网络传输中断（{type(exc).__name__}）",
                             hint="请重试；若持续出现请检查网络与代理设置") from exc


def _ensure_online_base_url() -> str:
    """请求前守卫：基础地址必须是**部署在服务器上的域名**。

    配置可能被历史版本或手工编辑污染成本机/内网地址。若不拦下，请求会打到
    一个只有当前这台机器能访问的地址，现象却是「服务不可用」，极易误判为故障。
    开发者本机对自建网关联调时，可用环境变量 `NOOVA_ALLOW_LOCAL_BASE_URL=1` 显式放行。
    """
    base = get_base_url()
    guard = check_base_url(base)
    if not guard["ok"]:
        raise NoovaError(
            guard["reason"],
            hint=(f"改回线上部署域名：{KEY_CMD} base {DEFAULT_BASE_URL}"),
        )
    return guard["url"]


def _require_key(purpose: str = "发起生成请求") -> str:
    key = get_api_key()
    if not key:
        # 首次使用（或换了机器）时的引导：直接打印 API 直达地址与领取方式，用户照做即可。
        print(render_guide(), file=sys.stderr)
        raise NoovaError(
            f"尚未配置 API Key，无法{purpose}",
            hint=(f"把用户提供的 Key 交给配置脚本即可自动完成配置："
                  f"{KEY_CMD} setup --api-key sk-xxxx"),
        )
    _ensure_online_base_url()
    return key


# ---------------------------------------------------------------------------
# 模型参数契约（对外唯一真源）
# ---------------------------------------------------------------------------
# 取数只有这一条路：`GET /api/models/params`（API Key 鉴权）。
# 旧实现曾抓取**对内链路**的网页展示数据，再正则解析参数表 ——
# 那是**给网站展示**的数据：字段缺失、枚举没有展示标签、解析口径随文档排版漂移，
# 鉴权语义也属于对内。已整体删除，不保留任何文档解析路径。

# 进程内缓存：一次命令生命周期里清单会被 `_resolve_model` / `build_*_form`
# 等重复索取，重复出网既慢又白吃限流额度。`refresh=True` 强制重取。
_SPEC_CACHE: dict = {}


def _align_option_visible_when(raw_rules: object, expected_len: int) -> list:
    """把契约的 optionVisibleWhen 对齐成与 values **同序等长**的规则数组。

    契约给的平行数组理论上与 values 等长（服务端由同一份 options 生成）。
    但这是运行时可变的对外数据，**长度不等时不得让下标错位**——错位等于把
    条件 A 的规则安到取值 B 上，用户按面板选出来的值会被平台 400 拒绝。
    因此一律以 values 长度为准裁剪/补 None；None 的语义是「无条件的可见」，
    与平台服务端「无规则即放行」的判定逐条一致：
    异常只会**多显示**选项，绝不**少显示**（后者会让面板拿不到任何选项）。
    """
    if not isinstance(raw_rules, list):
        return [None] * expected_len
    aligned: list = []
    for index in range(expected_len):
        rule = raw_rules[index] if index < len(raw_rules) else None
        aligned.append(rule if isinstance(rule, dict) else None)
    return aligned


def _normalize_model(item: dict) -> dict:
    """把对外契约里的一条模型记录转成内部字典（键名固定，供全模块消费）。

    输入形状（`GET /api/models/params` → `data.models[]`）：
        {model, modelType, displayName, status, protocols,
         billing, surchargeRules, params}
    其中 `params[]` 已由服务端投影为「发起请求 + 计价 + 向用户确认」所需的最小集合。
    本函数**不做任何推导**：契约没给的字段就是没有，绝不用其它字段凑。
    """
    code = str(item.get("model") or "").strip()
    protocols = [str(p).strip().lower() for p in (item.get("protocols") or [])
                 if str(p).strip()]
    billing = item.get("billing") if isinstance(item.get("billing"), dict) else {}
    params: list[dict] = []
    for raw in item.get("params") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        labels_raw = raw.get("valueLabels") if isinstance(raw.get("valueLabels"), dict) else {}
        values = [str(v).strip() for v in (raw.get("values") or []) if str(v).strip()]
        param: dict = {
            "name": name,
            "type": str(raw.get("type") or "string").strip(),
            "required": bool(raw.get("required")),
            "label": str(raw.get("label") or "").strip(),
            "values": values,
            "value_labels": {str(k).strip(): str(v).strip()
                             for k, v in labels_raw.items() if str(k).strip()},
            # 字段级条件可见（契约 `visibleWhen`）：契约没给就是 None（始终可见）。
            "visible_when": raw.get("visibleWhen") if isinstance(raw.get("visibleWhen"), dict) else None,
            # 选项级条件可见（契约 `optionVisibleWhen`）：与 `values` **同序等长**。
            # **必须按 `values` 长度对齐并补 None**——契约异常（长度不等）时，
            # 错位会把规则配到别的取值上；补 None 的语义是「无条件的可见」
            # （与后端「无规则即可见」一致），错位只会多显示、不会少显示。
            "option_visible_when": _align_option_visible_when(
                raw.get("optionVisibleWhen"), len(values)),
        }
        for key in ("min", "max", "minItems", "maxItems"):
            if raw.get(key) is not None:
                param[key] = raw[key]
        params.append(param)

    return {
        "code": code,
        "display_name": str(item.get("displayName") or "").strip() or code,
        "model_type": str(item.get("modelType") or "").strip().lower(),
        "status": str(item.get("status") or "").strip().lower(),
        "protocols": protocols,
        "billing": billing,
        "surcharge_rules": [r for r in (item.get("surchargeRules") or [])
                            if isinstance(r, dict)],
        # 服务端算好的「默认组合」报价（contract pricing 块）；旧后端无此字段时为 None。
        "pricing": item.get("pricing") if isinstance(item.get("pricing"), dict) else None,
        "params": params,
    }


def _fetch_public_model_specs(*, refresh: bool = False) -> dict:
    """拉取对外模型参数契约，返回 `{models, credit_per_yuan, min_text_charge}`。

    需要 API Key：对外端点与网站内网端点**严格分离**，后者按登录态鉴权，
    本工具（对外消费者）不得使用。契约版本不认识时 fail-closed ——
    宁可明确报错，也绝不按错的口径向用户报价。
    """
    if _SPEC_CACHE and not refresh:
        return _SPEC_CACHE
    key = _require_key("查询模型信息")
    payload = _request("GET", MODEL_PARAMS_PATH, key=key, timeout=40)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise NoovaError("模型参数契约响应格式异常",
                         hint="请稍后重试；若持续出现请反馈该现象")
    version = data.get("schemaVersion")
    if version is not None:
        try:
            version_int = int(version)
        except (TypeError, ValueError):
            version_int = -1
        if version_int != SUPPORTED_PARAM_SCHEMA_VERSION:
            raise NoovaError(
                f"服务端模型参数契约版本为 {version}，本工具只支持 "
                f"{SUPPORTED_PARAM_SCHEMA_VERSION}",
                hint="请升级 noova-generation skill 后重试（旧版本会按错误口径报价）",
            )
    try:
        credit_per_yuan = float(data.get("creditPerYuan") or DEFAULT_CREDITS_PER_YUAN)
    except (TypeError, ValueError):
        credit_per_yuan = float(DEFAULT_CREDITS_PER_YUAN)
    try:
        min_text_charge = float(data.get("minTextCharge") or 0)
    except (TypeError, ValueError):
        min_text_charge = 0.0
    parsed = [_normalize_model(i) for i in data["models"] if isinstance(i, dict)]
    spec = {
        "credit_per_yuan": (credit_per_yuan if credit_per_yuan > 0
                            else float(DEFAULT_CREDITS_PER_YUAN)),
        "min_text_charge": min_text_charge,
        # 只保留对外可见状态（online / maintenance），其余状态在此剔除。
        "models": [m for m in parsed if m["status"] in VISIBLE_MODEL_STATUSES],
    }
    _SPEC_CACHE.clear()
    _SPEC_CACHE.update(spec)
    return _SPEC_CACHE


def _fetch_public_models(*, refresh: bool = False) -> list[dict]:
    """当前可用模型清单（对外契约）。"""
    return _fetch_public_model_specs(refresh=refresh)["models"]


def resolve_route_path(protocol: str, model_code: str) -> str:
    """协议 → 用户侧官方路由路径（如需插值模型名，由路由模板占位符决定）。

    `model_code` 保留为参数：路由模板可能含 `{model}` 占位符（见 `PROTOCOL_PATHS`）。
    """
    template = PROTOCOL_PATHS.get(str(protocol or "").strip().lower())
    if not template:
        return ""
    if "{model}" in template:
        return template.format(model=urllib.parse.quote(str(model_code or "").strip(), safe=""))
    return template


def select_chat_protocol(model: dict, requested: str) -> tuple[str, str]:
    """决定文本模型本次调用走哪个官方路由，返回 (protocol, path)。

    规则（与平台模型文档的「官方接入路由（推荐）/ 额外协议路由」小节一致）：
    - `requested="auto"`：用该模型的**主协议**（契约 `protocols[0]`）。
    - 显式指定协议：必须是该模型文档声明支持的协议，否则给出明确错误。
    """
    # 契约不变量 I6：`protocols[0]` 即该模型的主协议（服务端保证，不再解析文档标题）。
    available = list(model.get("protocols") or [])
    primary = str((available or [""])[0] or "").strip().lower()
    if primary not in PROTOCOL_PATHS:
        primary = "openai"
    if primary not in available:
        available.insert(0, primary)

    want = str(requested or "auto").strip().lower()
    if want != "auto":
        if want not in available:
            raise NoovaError(
                f"模型「{model['code']}」不支持按 {PROTOCOL_LABELS.get(want, want)} 协议调用",
                hint=("该模型支持：" + "、".join(PROTOCOL_LABELS.get(p, p) for p in available)
                      + "；去掉 --protocol 即按主协议自动选择"),
            )
        return want, resolve_route_path(want, model["code"])

    chosen = primary if primary in available else (available[0] if available else "openai")
    return chosen, resolve_route_path(chosen, model["code"])


# ---------------------------------------------------------------------------
# 模型发现
# ---------------------------------------------------------------------------

def model_family_key(model: dict) -> str:
    """同族判定键：同一类型下的同一显示名。

    生产确实存在**同名不同编码**的模型（同一展示名对应多条编码）。它们不是
    「同一模型的线路」，而是各自独立的模型编码；聚族只为一件事——**同名时
    提醒用户用模型编码区分**（面板的 `duplicate_names` / `duplicate_note`），
    不参与选择，也不参与计价。

    与前端 `getModelFamilyKey` 同口径（`${类型}:${族名}`）。
    """
    return "%s:%s" % (str(model.get("model_type") or "").strip().lower(),
                      str(model.get("display_name") or "").strip())

def build_model_families(models: list[dict]) -> list[dict]:
    """按「类型 + 显示名」把模型聚成族。

    之所以还要聚：同名模型平铺在清单里就是一串看起来重复的行，用户看不出
    「这是同一个名字下的多个独立模型」。族内按**模型编码**稳定排序，
    保证多次运行顺序一致（编码是唯一稳定且可区分的键）。

    兼容两种输入形状：契约模型（有 `model_type`/`display_name`）与模型面板 option
    （有 `family`/`label`/`value`，是同一份数据的投影）。
    """
    families: dict = {}
    for model in models:
        key = str(model.get("family") or "").strip() or model_family_key(model)
        family = families.get(key)
        if family is None:
            family = families[key] = {
                "key": key,
                "label": str(model.get("label") or model.get("display_name")
                             or model.get("code") or "").strip(),
                "model_type": str(model.get("model_type")
                                  or key.split(":", 1)[0] or "").strip().lower(),
                "lines": [],
            }
        family["lines"].append(model)
    for family in families.values():
        family["lines"].sort(key=lambda m: str(m.get("code") or m.get("value") or ""))

    def _order(family: dict) -> tuple:
        mtype = family["model_type"]
        index = MODEL_TYPES.index(mtype) if mtype in MODEL_TYPES else len(MODEL_TYPES)
        return (index, family["label"])

    return sorted(families.values(), key=_order)

def _param_by_name(model: dict, name: object) -> dict | None:
    target = str(name or "").strip().lower()
    if not target:
        return None
    for param in model.get("params") or []:
        if str(param.get("name") or "").strip().lower() == target:
            return param
    return None

def _finite_number(value: object) -> float | None:
    """严格数值解析：布尔、None、非有限值一律当「解析不出」。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None

def _rule_credit_bounds(rule: dict, model: dict) -> tuple | None:
    """一条加收规则在**参数取值未知**时的积分上下界。

    返回 `(下界, 上界, 依据文案)`；只靠契约推不出来时返回 `None`
    （调用方据此退回定性文案，**绝不编数字**）。

    - 加收参数**必填** → 下界按契约 `min`、上界按契约 `max`；
    - 加收参数**可选** → 下界不含该项（不传就不计），上界仍按 `max`；
    - `match` 不是 `present`（`exact`/`range`）时，光看契约推不出实际取值 → `None`；
    - 加收参数在契约里找不到 → 无法判断必填性 → `None`。
    """
    if str(rule.get("match") or "present").strip().lower() != "present":
        return None
    name = _rule_param_name(rule)
    param = _param_by_name(model, name)
    if param is None:
        return None
    required = bool(param.get("required"))
    charge = str(rule.get("charge") or "fixed").strip().lower()
    if charge == "multiply_by_value":
        multiplier = _finite_number(rule.get("multiplier"))
        base = _finite_number(rule.get("base")) or 0.0
        low = _finite_number(param.get("min"))
        high = _finite_number(param.get("max"))
        if multiplier is None or high is None:
            return None
        upper = base + max(high, 0.0) * multiplier
        lower = 0.0
        if required:
            lower = base + max(low if low is not None else 0.0, 0.0) * multiplier
        span = ("%s~%s" % (_fmt_num(low), _fmt_num(high)) if low is not None
                else "≤%s" % _fmt_num(high))
        return (lower, upper, "%s %s × %s 积分" % (name, span, _fmt_num(multiplier)))
    credits = _finite_number(rule.get("credits"))
    if credits is None:
        return None
    return ((credits if required else 0.0), credits,
            "%s 加收 %s 积分" % (name, _fmt_num(credits)))

def approximate_credit(model: dict) -> dict:
    """**选模型时**的近似消耗（此时用户还没定参数，故只能是区间或定性）。

    与 `compute_credit_cost`（参数定好后的精确报价）的分工：

    - `exact`：单价固定且与参数无关 → `20 积分/次`；
    - `range`：单价为 0 或含加收规则，但加收参数在契约里有 `min`/`max`
      → `约 260~1950 积分/次`（`detail` 给出依据：`duration 4~30 × 65 积分`）；
    - `usage`：按 token 结算，或加收参数的边界推不出来 → 定性文案，**不编数字**。

    依据**只有契约**；真实扣费仍由服务端决定，`range` 文案一律带「约」。
    """
    def _out(kind, text, low=None, high=None, detail=""):
        return {"kind": kind, "text": text, "min_credits": low,
                "max_credits": high, "detail": detail}

    if _billing_mode(model) == "per_token":
        return _out("usage", "按量计费（按 token 用量结算）")

    price, unit = _base_price(model)
    base = round(price * unit, 2)
    rules = [r for r in (model.get("surcharge_rules") or []) if isinstance(r, dict)]
    if not rules:
        if base <= 0:
            return _out("usage", "未公布单价（以平台计价为准）")
        text = ("%s 积分/次" % _fmt_num(base) if unit == 1
                else "%s 积分/%d 次" % (_fmt_num(base), unit))
        return _out("exact", text, base, base)

    lower, upper, details = base, base, []
    for rule in rules:
        bounds = _rule_credit_bounds(rule, model)
        if bounds is None:
            return _out("usage", "按用量计价")
        low, high, detail = bounds
        lower = round(lower + low, 2)
        upper = round(upper + high, 2)
        details.append(detail)
    if lower <= 0 or upper <= 0:
        # 下界为 0 时「0~N」看起来像"最便宜那档免费"，上界为 0 时是计费配置异常。
        # 两种情况给区间都会误导，宁可如实定性。
        return _out("usage", "按用量计价")
    detail = "、".join(details)
    if lower == upper:
        return _out("exact", "%s 积分/次" % _fmt_num(lower), lower, upper, detail)
    return _out("range", "约 %s~%s 积分/次" % (_fmt_num(lower), _fmt_num(upper)),
                lower, upper, detail)

def _estimate_text(estimate: dict) -> str:
    """把 `approximate_credit` 的结果渲染成一行文案（带括号里的计算依据）。"""
    if not isinstance(estimate, dict):
        return ""
    text = str(estimate.get("text") or "")
    detail = str(estimate.get("detail") or "")
    if detail and estimate.get("kind") != "usage":
        return "%s（%s）" % (text, detail)
    return text

def approximate_credit_text(model: dict) -> str:
    """`approximate_credit` 的一行文案（带括号里的计算依据）。"""
    return _estimate_text(approximate_credit(model))

def _select_by_code(models: list[dict], code: str) -> dict | None:
    """按模型编码精确匹配（其次按显示名）。

    没有编码的模型（平台 `model_code` 为空）永远选不中——它无法被调用，
    由调用方在清单里标为不可用，而不是在这里猜一个编码出来。

    **显示名命中多条时 fail-closed**：生产真实存在同名不同编码的模型
    （同一展示名对应多条编码），旧实现静默取第一条，等于用户指定了名字却
    拿到一个随机模型、且毫不知情。这里改为报错并列出候选（**编码 + 计价**），
    把选择权交回用户。

    候选里只有编码与计价：编码是唯一可靠且可区分的选择键，计价让用户看出
    同名几条的价差；对外契约已不再下发线路标签，也不该在这里补一个近似的。
    """
    target = str(code or "").strip().lower()
    if not target:
        return None
    callable_models = [m for m in models if str(m.get("code") or "").strip()]
    for model in callable_models:
        if model["code"].lower() == target:
            return model
    matches = [m for m in callable_models if m["display_name"].lower() == target]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidates = "；".join(
            "%s（%s）" % (m["code"], _price_cell(m)) for m in matches)
        raise NoovaError(
            "「%s」命中 %d 个同名模型，无法确定用哪一个" % (code, len(matches)),
            hint="请改用模型编码指定。候选：" + candidates,
        )
    return None


def _resolve_model(code: str | None, model_type: str, *, quiet: bool = False) -> dict:
    """按编码解析模型；未指定编码时自动选择该类型下第一个可用模型。"""
    models = _fetch_public_models()
    if code:
        hit = _select_by_code(models, code)
        if not hit:
            raise NoovaError(f"未找到模型「{code}」",
                             hint="运行 models 子命令查看当前可用清单（模型会随时增减）")
        if hit["model_type"] and hit["model_type"] != model_type:
            raise NoovaError(
                f"模型「{hit['code']}」的类型是「{hit['model_type']}」，不能用于「{model_type}」调用",
                hint=f"可用 models --type {model_type} 查看该类型的模型",
            )
        return hit

    candidates = [m for m in models if m["model_type"] == model_type]
    if not candidates:
        raise NoovaError(
            f"当前平台没有可用的「{model_type}」模型",
            hint=("可用模型以公开模型清单接口为准；请不要自行编造模型编码，"
                  "可运行 models 查看全部类型"),
        )
    online = [m for m in candidates if m["status"] == CALLABLE_MODEL_STATUS]
    if not online:
        raise NoovaError(f"「{model_type}」类型下暂无上线（online）状态的模型",
                         hint="运行 models 查看状态，或稍后重试")
    chosen = online[0]
    # `--quiet` 的语义是「只输出结果」，自动选择模型的提示也必须被抑制，
    # 否则 `--quiet` 的 stdout/stderr 仍会多出一行用户没要的信息。
    if not quiet:
        print(f"[自动选择模型] {chosen['display_name']}（{chosen['code']}）"
              f"　{_price_text(chosen)}", file=sys.stderr)
    return chosen


# ---------------------------------------------------------------------------
# 文本结果提取（多协议）
# ---------------------------------------------------------------------------

def _content_to_text(content: object) -> str:
    """OpenAI message.content 可能是字符串，也可能是分段数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif isinstance(part, str):
                chunks.append(part)
        return "".join(chunks)
    return ""


def extract_text(payload: object) -> str:
    """从任意协议的文本响应中提取正文（兼容平台统一包裹外层）。"""
    if isinstance(payload, str):
        return payload
    for node in (payload, _unwrap(payload)):
        text = _extract_text_from(node)
        if text.strip():
            return text
    return ""


def _extract_text_from(payload: object) -> str:
    """从**单个业务体**提取正文（兼容各协议原生响应形态）。"""
    if not isinstance(payload, dict):
        return ""
    # OpenAI 兼容：choices[].message.content / choices[].text
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                text = _content_to_text(message.get("content"))
                if text.strip():
                    return text
            text = _content_to_text(choice.get("text"))
            if text.strip():
                return text
    # Anthropic Messages：content[].text
    content = payload.get("content")
    if isinstance(content, list):
        chunks = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and str(part.get("type") or "") in ("text", "")
        ]
        if any(c.strip() for c in chunks):
            return "".join(chunks)
    # Responses API：output[].content[].text
    output = payload.get("output")
    if isinstance(output, list):
        chunks = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
        if any(c.strip() for c in chunks):
            return "".join(chunks)
    return ""


def extract_usage(payload: object) -> dict:
    """提取用量（用于向用户解释计费）；兼容统一包裹与各协议字段名。"""
    for node in (payload, _unwrap(payload)):
        usage = _extract_usage_from(node)
        if usage:
            return usage
    return {}


def _extract_usage_from(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage") or payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return {}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", usage.get("promptTokenCount")))
    completion = usage.get("completion_tokens", usage.get("output_tokens", usage.get("candidatesTokenCount")))
    result: dict = {}
    if prompt is not None:
        result["prompt_tokens"] = int(prompt or 0)
    if completion is not None:
        result["completion_tokens"] = int(completion or 0)
    total = usage.get("total_tokens", usage.get("totalTokenCount"))
    if total is not None:
        result["total_tokens"] = int(total or 0)
    if result and "total_tokens" not in result:
        result["total_tokens"] = result.get("prompt_tokens", 0) + result.get("completion_tokens", 0)
    return result


def extract_stream_delta(payload: object) -> str:
    """从单个 SSE 数据块中提取增量文本（兼容各协议方言）。"""
    if not isinstance(payload, dict):
        return ""
    # OpenAI: choices[].delta.content
    choices = payload.get("choices")
    if isinstance(choices, list):
        chunks = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                chunks.append(_content_to_text(delta.get("content")))
            chunks.append(_content_to_text(choice.get("text")))
        text = "".join(chunks)
        if text:
            return text
    # Anthropic: content_block_delta.delta.text
    if str(payload.get("type") or "") == "content_block_delta":
        delta = payload.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            return delta["text"]
    # OpenAI Responses: 增量的 `type` 形如 `response.output_text.delta`，正文在 `delta`
    if str(payload.get("type") or "").endswith(("output_text.delta", "text.delta")):
        delta = payload.get("delta")
        if isinstance(delta, str):
            return delta
    # 兼容协议原生响应：正文可能在更深的嵌套里
    return extract_text(payload)


# ---------------------------------------------------------------------------
# 任务（异步）辅助
# ---------------------------------------------------------------------------

def _unwrap(payload: object) -> object:
    """剥离平台统一包裹外层，返回真正的业务体。

    平台响应有两种形态（实测 2026-09-29）：

    - **裸业务体**：`{"status": "running"}`（任务查询接口的响应示例即此形态）
    - **统一包裹**：`{"code": 200, "data": {...}, "message": ""}` —— 所有错误响应
      与部分成功响应（如鉴权校验）都是这个形态

    因此**任何结果提取都必须对两种形态都成立**，只认顶层字段会在包裹形态下
    把「任务永远查不到终态」这种故障伪装成「任务超时」。

    仅当顶层同时含 `code` 与 `data`、且 `data` 是**非空** dict/list 时才剥离；
    `data: null`（错误响应）保持原样，以免丢掉错误信息。
    """
    if not isinstance(payload, dict) or "code" not in payload:
        return payload
    inner = payload.get("data")
    if isinstance(inner, (dict, list)) and inner:
        return inner
    return payload


def _status_of(payload: object) -> str:
    for node in (payload, _unwrap(payload)):
        if isinstance(node, dict):
            status = str(node.get("status") or "").strip().lower()
            if status:
                return status
    return ""


def _task_id_of(payload: object) -> str:
    for node in (payload, _unwrap(payload)):
        if not isinstance(node, dict):
            continue
        for key in ("id", "task_id", "taskId"):
            value = str(node.get(key) or "").strip()
            if value:
                return value
    return ""


# 结果 URL 可能出现的字段名。平台文档示例里 `result` 与 `results` 都出现过，
# 且 `results` **有时是单个字符串、有时是 [{url, content}] 数组**。
# 字段名取得宽是**刻意的**：漏掉一个键就等于用户拿不到产物（比多脱敏一层严重得多）。
# 代价用下面 `_add()` 里的本机/内网判定来兜：产物地址一律原样交付，唯独本机/内网
# 地址不可能被用户访问到——那不是产物，是泄漏。
_RESULT_KEYS = ("result", "results", "imageUrl", "videoUrl", "audioUrl",
                "url", "output", "data", "content")
_RESULT_MAX_DEPTH = 4


def collect_result_urls(payload: object) -> list[str]:
    """从任务结果中收集全部媒体 URL（去重、保序）。

    兼容以下全部形态（均取自平台公开文档的响应示例）：
      - `{"results": "https://…png", "status": "succeeded"}` ← **单个字符串**
      - `{"result": "…", "results": [{"url": "…", "content": "…"}], "videoUrl": "…"}`
      - `{"code": 200, "data": {"results": [...]}, "message": ""}` ← 统一包裹

    上传存储与平台存储的主机名**不在**公开域名白名单里，所以这里不能用
    `host_is_public()` 当门槛——那样会把用户的产物地址一起替换掉。只拦本机/内网。
    """
    urls: list[str] = []

    def _add(value: object) -> None:
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            candidate = value.strip()
            host = urllib.parse.urlparse(candidate).hostname or ""
            if is_local_host(host):
                # 本机/内网地址用户根本访问不到，它不是产物而是内部信息（已实测的
                # 绕过形态：内部域名藏在 `data` / `output` 里被当成结果地址原样打印）。
                candidate = sanitize_text(candidate)
            if candidate not in urls:
                urls.append(candidate)

    def _scan(node: object, depth: int) -> None:
        if isinstance(node, str):
            _add(node)
            return
        if depth > _RESULT_MAX_DEPTH:
            return
        if isinstance(node, list):
            for item in node:
                _scan(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in _RESULT_KEYS:
                _scan(value, depth + 1)

    _scan(payload, 0)
    return urls


def fetch_pricing(model: dict, params: dict | None = None, *, timeout: int = 20) -> dict | None:
    """向服务端询价（`GET /api/models/params`）：**权威价由服务端算**，本工具不自行计价。

    返回服务端的 `pricing` 块（`{exact, credits, yuan, quotedParams, note}`）；
    任何失败（网络 / 鉴权 / 旧后端无 pricing）都返回 None，
    由调用方回退到本地按契约的**预估**并明确标注「预估」（计划 §4.5）。
    """
    code = str(model.get("code") or "").strip()
    if not code:
        return None
    path = MODEL_PARAMS_PATH + "?model=" + urllib.parse.quote(code)
    if params:
        payload_json = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        path += "&params=" + urllib.parse.quote(payload_json)
    try:
        key = _require_key("查询模型价格")
        response = _request("GET", path, key=key, timeout=timeout, max_retries=0)
    except NoovaError:
        return None
    data = response.get("data") if isinstance(response, dict) else None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list) or not models:
        return None
    first = models[0]
    pricing = first.get("pricing") if isinstance(first, dict) else None
    return pricing if isinstance(pricing, dict) else None


def _authoritative_credits(pricing: object) -> tuple[float, bool] | None:
    """从服务端 `pricing` 块取 (积分数, 是否确定值)；给不出确定值返回 None。"""
    if not isinstance(pricing, dict):
        return None
    value = pricing.get("credits")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return (float(value), bool(pricing.get("exact")))


def _pricing_note(pricing: object) -> str:
    """服务端给不出确定值的原因——**只进机器可读字段，不上用户文案**。

    服务端的 `note` 是给调用方（agent）的接口指令，例如
    「请核对参数取值后用 params 重新询价」。它有助于 agent 自行决策，
    但**不能出现在用户可见输出里**——见 `credit_estimate_text`。
    """
    if isinstance(pricing, dict):
        note = str(pricing.get("note") or "").strip()
        if note:
            return note
    return "服务端未给出确定金额，以生成结算为准"


def _estimate_with_authoritative(estimate: dict, pricing: object) -> dict:
    """把本地复算结果换成**服务端权威口径**（数值层与文案层同源）。

    服务端没作答（`pricing` 为 None）或答了但给不出确定值时，一律把数字清空，
    **绝不拿本地复算顶替**——报价与真值冲突时以服务端结算为准，此时任何数字都可能
    与最终扣减对不上。原因写进 `note`（机器可读，供 agent 决策），不上用户文案。
    `credit_estimate_text` 走同一判断，两者必须始终一致。
    """
    resolved = _authoritative_credits(pricing)
    credits, exact = resolved if resolved is not None else (None, False)
    if not exact:
        credits = None          # 与 credit_estimate_text 同口径：不确定就不给数字
    ratio = _credits_per_yuan()
    return {**estimate, "exact": exact, "credits": credits,
            "yuan": (round(credits / ratio, 4) if credits is not None and ratio else None),
            "note": "" if exact else _pricing_note(pricing)}


def fetch_credit(key: str, *, timeout: int = 20) -> dict | None:
    """查询账户积分快照；**任何失败都返回 None**，绝不因展示余额而中断生成。

    返回 `{"total","permanent","limited","vip"}`（缺省字段为 None）。
    使用公开文档端点 `GET /api/v1/credit`（该端点会真正校验 Key）。
    """
    try:
        payload = _request("GET", CREDIT_PATH, key=key, timeout=timeout, max_retries=0)
    except NoovaError:
        return None
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None

    def _number(*names: str):
        for name in names:
            value = data.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return float(value)
        return None

    total = _number("remainingTotal", "remaining_total")
    if total is None:
        return None
    return {
        "total": total,
        "permanent": _number("remainingPermanent"),
        "limited": _number("remainingLimited"),
        "vip": _number("remainingVip"),
    }


def _credit_amount(value: float | None) -> str:
    if value is None:
        return "-"
    return _fmt_num(value)


def _credit_lines(before: dict | None, after: dict | None) -> list[str]:
    """「本次扣减 / 剩余积分」两行文案。

    口径：**账户余额差值**（生成前一次快照 vs 终态后一次快照）。
    这是唯一不依赖内部计费实现的、可核对的真实消耗口径；因此文案里明确写清
    「按余额差值」，避免与模型标价（可能含参数加收、缓存命中、按秒计费）混淆。
    任一快照缺失或接口不可用时**不猜**，直接省略该行。
    """
    lines: list[str] = []
    if before and after:
        delta = float(before["total"]) - float(after["total"])
        if delta > 0:
            lines.append(f"本次扣减：{_credit_amount(delta)} 积分"
                         f"（账户余额 {_credit_amount(before['total'])} → {_credit_amount(after['total'])}）")
        elif delta == 0:
            # 「本次扣减：0 积分」会让人以为免费。余额没变就是没变，如实说。
            lines.append(f"本次未产生扣费（账户余额保持 {_credit_amount(after['total'])}）")
        else:
            lines.append(f"余额变化：+{_credit_amount(abs(delta))} 积分"
                         f"（{_credit_amount(before['total'])} → {_credit_amount(after['total'])}，"
                         f"期间有充值/返还）")
    if after:
        detail = "、".join(
            f"{label} {_credit_amount(after[key])}"
            for key, label in (("permanent", "永久"), ("limited", "限时"), ("vip", "会员"))
            if after.get(key) is not None
        )
        lines.append(f"剩余积分：{_credit_amount(after['total'])}" + (f"（{detail}）" if detail else ""))
    return lines


def _submit_info_lines(model: dict | None, task_id: str, payload: dict) -> list[str]:
    """生成结果的统一信息块（模型 / 任务 ID / 计价 / 用量）。

    余额相关行不在这里给：终态时由 `_credit_lines` 用「差额 + 余额」一次说清，
    避免同一件事重复两遍。
    """
    lines: list[str] = []
    if model:
        lines.append(f"模型：{model['display_name']}（{model['code']}）")
        lines.append(f"计价：{_price_text(model)}")
    if task_id:
        lines.append(f"任务 ID：{task_id}")
    usage = extract_usage(payload)
    if usage:
        lines.append(f"用量：输入 {usage.get('prompt_tokens', 0)} tokens / "
                     f"输出 {usage.get('completion_tokens', 0)} tokens")
    return lines


def _render_info_block(lines: list[str], *, out=None) -> None:
    """按标签对齐打印信息块（中文按显示宽度对齐）。"""
    if not lines:
        return
    width = max(_display_width(line.split("：")[0]) for line in lines)
    for line in lines:
        if "：" in line:
            label, value = line.split("：", 1)
            print(f"  {_pad(label, width)}：{value}", file=out or sys.stdout)
        else:
            print(f"  {line}", file=out or sys.stdout)


def _result_label(wanted_type: str, count: int) -> str:
    """结果数量的量词要正确：图片论「张」，视频/音频论「个」。"""
    unit = {"image": "张图片", "video": "个视频", "audio": "个音频"}.get(wanted_type)
    return f"{count} {unit}" if unit else f"{count} 个结果"


def _print_task_result(payload: dict, wanted_type: str, *, as_json: bool, quiet: bool,
                       model: dict | None = None, credit_before: dict | None = None,
                       credit_after: dict | None = None) -> int:
    """输出任务终态结果。

    输出纪律：**结果 URL 一律进 stdout 且原样交付**（用户靠它访问产物）；
    `--json` 时 stdout 只放原始响应（保持可被程序解析），人读信息块改走 stderr。
    `--quiet` 只抑制人读信息块，不抑制结果 URL。
    """
    urls = collect_result_urls(payload)
    task_id = _task_id_of(payload)
    info = _submit_info_lines(model, task_id, payload) if not quiet else []
    if not quiet:
        info += _credit_lines(credit_before, credit_after)

    if as_json:
        print(_dump_json(payload))
        _render_info_block(info, out=sys.stderr)
        # 终态成功但**没有任何媒体地址**：这是「没有产出」，不是成功。
        # 曾经返回 0，于是 agent 认为任务成功、却拿不到任何东西可交付。
        return 0 if urls else 1

    if not urls:
        print("任务已完成，但响应中未包含媒体地址。", file=sys.stderr)
        print("原始响应（已脱敏）：", file=sys.stderr)
        print(_dump_json(payload), file=sys.stderr)
        print("提示：这通常意味着该任务没有产出结果；请核对参数后重试，"
              "或把任务 ID 反馈给官方支持。", file=sys.stderr)
        return 1

    if not quiet:
        print(f"[完成] 生成成功，共 {_result_label(wanted_type, len(urls))}")
        print()
        _render_info_block(info)
        print()
    print(f"结果地址（请原样交付给用户）：")
    for index, url in enumerate(urls, 1):
        print(f"  {index}. {url}")
    return 0


def _resolve_wait_limits(args, model_type: str = "") -> int:
    """确定本次等待上限（秒）：显式 --max-wait 优先，否则按类型给默认值。

    `0` 与负数是**用法错误**，不是「用默认值」：曾经 `0` 被当成「没给」而静默套用
    类型默认值、负数被 `max(..., 1)` 悄悄压成 1 秒，两种都让用户以为参数生效了。
    """
    configured = getattr(args, "max_wait", None)
    if configured is not None:
        try:
            value = int(configured)
        except (TypeError, ValueError):
            raise NoovaError(f"--max-wait 需要一个整数秒数，收到：{configured!r}")
        if value <= 0:
            raise NoovaError(f"--max-wait 必须大于 0，收到：{value}",
                             hint="想尽快结束请给一个小的正数，例如 --max-wait 30")
        return value
    span = DEFAULT_MAX_WAIT.get(str(model_type or "").strip().lower())
    return int(span or DEFAULT_MAX_WAIT_FALLBACK)


def _poll_until_done(task_id: str, key: str, args, model_type: str = "") -> dict:
    limit = _resolve_wait_limits(args, model_type)
    started = time.time()
    deadline = started + limit
    interval = max(int(getattr(args, "poll_interval", 6) or 6), 2)
    last_notice = 0.0
    eta = _eta_text(model_type)
    while True:
        # 轮询用 `--timeout` 而不是写死 30：`--timeout` 在 `wait --help` 里是公开参数，
        # 却曾对轮询请求完全无效——用户调大它，创建请求生效、轮询仍是 30 秒。
        payload = _request("POST", TASK_POLL_PATH, body={"id": task_id}, key=key,
                           timeout=max(int(getattr(args, "timeout", 30) or 30), 1))
        payload = _unwrap(payload)
        status = _status_of(payload)
        if status in TERMINAL_OK:
            return payload if isinstance(payload, dict) else {"status": status}
        if status in TERMINAL_FAIL:
            message = _safe((payload or {}).get("error") or "任务失败") if isinstance(payload, dict) else "任务失败"
            raise NoovaError(f"任务失败：{message}",
                             hint=f"任务 ID：{task_id}；如需排障请把该 ID 一并反馈")
        if time.time() > deadline:
            raise NoovaError(
                f"等待超时（已等待 {int(limit)} 秒）——任务仍在处理中，**不是失败**",
                hint=(f"该类型通常耗时 {eta}；继续等待："
                      f"{SELF_CMD} wait --id {task_id} --max-wait {limit}；"
                      f"或只查一次：{SELF_CMD} task --id {task_id}"),
            )
        now = time.time()
        if not getattr(args, "quiet", False) and now - last_notice >= 20:
            progress = payload.get("progress") if isinstance(payload, dict) else None
            suffix = f" · 进度 {progress}%" if isinstance(progress, (int, float)) else ""
            print(f"[轮询] 已等待 {_human_duration(now - started)} / 上限 {_human_duration(limit)}"
                  f" · 状态 {_status_label(status)}{suffix}（任务 {task_id}）", file=sys.stderr)
            last_notice = now
        time.sleep(interval)


def _pricing_quote_params(model: dict, payload: dict) -> dict:
    """挑出**参与计费的参数**用于询价（其余一律不传）。

    两个理由，都不能省：

    1. **自由文本会撑爆 URL**。`fetch_pricing` 把参数塞进 `?params=`（GET）；
       实测 1200 字的提示词编码后 10KB+，超过 nginx 默认 8KB 请求头上限，
       服务端 414、询价静默失败——用户看不到任何错误，报价只能退化成
       「本次消耗以生成结算为准」（不再给数字，见 `credit_estimate_text`）。
    2. **用户创作内容不该进 URL**。提示词会落进各级访问日志，而它不参与计价。

    取参与计费的参数 = 契约 `surchargeRules[].param`（后端结算与契约用的是同一份规则）。
    没有任何一条命中时返回空 dict，服务端按「未传参数」作答——与用户实际没选该参数一致。
    """
    keys = {str(rule.get("param") or "").strip()
            for rule in (model.get("surcharge_rules") or []) if isinstance(rule, dict)}
    keys.discard("")
    return {name: value for name, value in (payload or {}).items() if name in keys}


def _announce_cost(model: dict, payload: dict, *, quiet: bool = False) -> None:
    """提交前在 **stderr** 报价：用户/agent 在扣费前必须看到本次预计消耗。

    只走 stderr —— `--json` 的 stdout 必须保持是纯 JSON。

    **有加收规则时必须按本次真实参数询价**：契约自带的 `pricing` 是「不传参数」的
    默认组合价，直接复用会给出底价——实测 `nano-banana-pro` 选 `imageSize=4K` 时
    面板报 22、提交却报 20；时长型模型更糟（默认组合给不出确定值，退化成
    「未给出确定金额」）。无加收规则时价格与参数无关，契约 `pricing` 即真值，
    不发这次请求（此时代码传 `None`，由 `credit_estimate_text` 取契约 `pricing`）。

    取价策略与 `build_param_form` 保持一致——两处口径不同就会出现「面板报一个价、
    提交报另一个价」。
    """
    if quiet:
        return
    # 契约自带的 `pricing` 是**不传参数**的默认组合价，对「本次参数」而言与底价等价，
    # 不能当权威价用；有加收规则时必须按本次参数重新询价，询价失败宁可本地复算。
    # 询价失败传**空 dict**（而不是 None）：None 会让 `credit_estimate_text` 退回那份
    # 默认组合价，等于又报回底价；空 dict 强制走「本地按本次参数复算」。
    pricing = (fetch_pricing(model, _pricing_quote_params(model, payload)) or {}
               if model.get("surcharge_rules") else None)
    try:
        text = credit_estimate_text(model, payload, pricing=pricing)
    except NoovaError:
        raise
    print(f"[费用] {text}", file=sys.stderr)


def _create_task(model: dict, payload: dict, args, key: str,
                 *, credit_before: dict | None = None) -> int:
    """创建生成任务并按需等待终态（含耗时预期与积分反馈）。"""
    if not getattr(args, "quiet", False):
        print(f"[提交] {TYPE_LABELS.get(model['model_type'], model['model_type'])}生成 · "
              f"{model['display_name']}（{model['code']}）"
              f" · 计价：{_price_text(model)} · 典型耗时：{_eta_text(model['model_type'])}",
              file=sys.stderr)
    _announce_cost(model, payload, quiet=getattr(args, "quiet", False))

    body = {"model": model["code"], **payload}
    # `retry=False`：这是**计费的创建请求**，不是幂等查询。自动重试可能在
    # 「上游已受理但中间层返回 503」时创建两个任务、扣两次费（见 `_request`）。
    created = _request("POST", TASK_CREATE_PATH, body=body, key=key,
                       timeout=args.timeout, retry=False)
    if not isinstance(created, dict):
        raise NoovaError("创建任务的响应无法解析")
    # 统一包裹形态（`{code, data, message}`）下真正的业务体在 `data` 里。
    created = _unwrap(created)

    error_text = _safe(
        created.get("error") if isinstance(created.get("error"), str)
        else (created.get("error") or {}).get("message") if isinstance(created.get("error"), dict)
        else ""
    )
    status = _status_of(created)
    if status in TERMINAL_FAIL:
        # 平台把失败原因放在响应体的 `error` 字段（已脱敏），原样中性转述即可。
        raise NoovaError(f"任务创建失败：{error_text or '平台未提供原因'}",
                         hint=f"模型：{model['code']}；可先核对参数：params --code {model['code']}")

    urls = collect_result_urls(created)
    if status in TERMINAL_OK and urls:
        # 少数模型创建即返回结果（同步返回媒体），同样要给出扣费与余额。
        return _print_task_result(created, model["model_type"], as_json=args.json, quiet=args.quiet,
                                  model=model, credit_before=credit_before,
                                  credit_after=None if args.no_credit else fetch_credit(key))

    task_id = _task_id_of(created)
    if not task_id:
        if urls:
            return _print_task_result(created, model["model_type"], as_json=args.json, quiet=args.quiet,
                                      model=model, credit_before=credit_before,
                                      credit_after=None if args.no_credit else fetch_credit(key))
        raise NoovaError("任务创建成功但未返回任务 ID，无法继续查询")

    if getattr(args, "no_wait", False):
        info = [f"模型：{model['display_name']}（{model['code']}）",
                f"任务 ID：{task_id}",
                f"状态：{_status_label(status)}",
                f"计价：{_price_text(model)}"]
        if credit_before:
            info.append(f"当前余额：{_credit_amount(credit_before['total'])} 积分")
        if args.json:
            # `--json` 时 stdout **必须**只有可解析的 JSON：agent 会 `json.loads(stdout)`，
            # 而这里曾经把「[已提交] 任务已受理…」打到 stdout 并返回 0，
            # 直接让 agent 解析崩溃（已实测）。过程信息一律走 stderr。
            print(_dump_json({
                "model": model["code"],
                "model_display_name": model["display_name"],
                "model_type": model["model_type"],
                "task_id": task_id,
                "status": status,
                "billing": billing_spec(model),
                "credit_remaining": (credit_before or {}).get("total"),
                "waited": False,
                "next_steps": [f"wait --id {task_id} --type {model['model_type']}",
                               f"task --id {task_id}"],
            }))
            _render_info_block(info, out=sys.stderr)
            return 0
        print(f"[已提交] 任务已受理，未等待结果；预计耗时 {_eta_text(model['model_type'])}")
        print()
        _render_info_block(info)
        print()
        print("继续等待结果：")
        print(f"  {SELF_CMD} wait --id {task_id} --type {model['model_type']}")
        print("只查一次状态：")
        print(f"  {SELF_CMD} task --id {task_id}")
        return 0

    if not args.quiet:
        print(f"[已提交] 任务 ID {task_id}；预计耗时 {_eta_text(model['model_type'])}，"
              f"等待上限 {_wait_limit_text(_resolve_wait_limits(args, model['model_type']))}",
              file=sys.stderr)
    result = _poll_until_done(task_id, key, args, model["model_type"])
    return _print_task_result(result, model["model_type"], as_json=args.json, quiet=args.quiet,
                              model=model, credit_before=credit_before,
                              credit_after=None if args.no_credit else fetch_credit(key))


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def _fmt_num(value: object) -> str:
    """数字展示：整数不带小数，小数最多保留两位（积分可带小数，不可失真）。

    注意**不要用 `%g`**：`f"{12345.67:g}"` 会丢精度成 `12345.7`，
    而账户余额、单次扣减都可能带两位小数，展示必须与实际值一致。
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return str(int(number))
    digits = 6 if abs(number) < 0.01 else 2
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def _billing_of(model: dict) -> dict:
    billing = model.get("billing")
    return billing if isinstance(billing, dict) else {}


def _billing_mode(model: dict) -> str:
    return str(_billing_of(model).get("mode") or "").strip().lower()


def _token_prices(model: dict) -> dict:
    """四档 token 单价（积分/百万 tokens）；契约缺哪档就哪档为 0。"""
    prices = _billing_of(model).get("pricePer1M")
    prices = prices if isinstance(prices, dict) else {}
    out: dict = {}
    for key in ("input", "output", "cacheHit", "cacheWrite"):
        try:
            out[key] = float(prices.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0.0
    return out


def _surcharge_text(rule: dict) -> str:
    """把一条加收规则说成用户能懂的一句话（只转述契约，不做任何补全）。"""
    param = str(rule.get("param") or "").strip() or "参数"
    match = str(rule.get("match") or "present").strip().lower()
    charge = str(rule.get("charge") or "fixed").strip().lower()
    try:
        base = float(rule.get("base") or 0)
    except (TypeError, ValueError):
        base = 0.0
    condition = {
        "present": f"提供 {param} 时",
        "exact": f"{param}={_fmt_num(rule.get('matchValue'))} 时",
        "range": (f"{param} 在 {_fmt_num(rule.get('rangeMin'))}~"
                  f"{_fmt_num(rule.get('rangeMax'))} 时"),
    }.get(match, f"{param} 命中时")
    if charge == "multiply_by_value":
        try:
            multiplier = float(rule.get("multiplier") or 0)
        except (TypeError, ValueError):
            multiplier = 0.0
        amount = f"{param} × {_fmt_num(multiplier)} 积分"
        if base:
            amount += f" + {_fmt_num(base)} 积分"
        return f"{condition}，按 {amount} 计"
    try:
        credits = float(rule.get("credits") or 0)
    except (TypeError, ValueError):
        credits = 0.0
    return f"{condition}，加收 {_fmt_num(credits)} 积分"


def _base_price(model: dict) -> tuple[float, int]:
    """按次/按时的「单价 × 计费单位」。缺字段或非法值一律当 0（`_price_text` 会说明）。"""
    billing = _billing_of(model)
    try:
        price = float(billing.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    try:
        unit = int(billing.get("unit") or 1)
    except (TypeError, ValueError):
        unit = 1
    return (price, unit if unit > 0 else 1)


def _price_text(model: dict) -> str:
    """该模型的计价口径（用户可见文案）。只读对外契约，不做任何推断。

    1. 按 token 计费（`per_token`）——给出四档单价（积分/百万 tokens）；
    2. 按次计费（`per_request`/`per_time`）单价 > 0——`X 积分/次`；
    3. 按次但单价为 0——必须说明「按用量计价」。**绝不渲染成「0 积分/次」**：
       单价为 0 意味着改由参数加收规则（时长等）计价，写 0 会让用户以为免费。
    """
    mode = _billing_mode(model)
    billing = _billing_of(model)
    if mode == "per_token":
        prices = _token_prices(model)
        if any(prices.values()):
            parts = [f"输入 {_fmt_num(prices['input'])}",
                     f"输出 {_fmt_num(prices['output'])}"]
            if prices["cacheHit"]:
                parts.append(f"缓存命中 {_fmt_num(prices['cacheHit'])}")
            if prices["cacheWrite"]:
                parts.append(f"缓存写入 {_fmt_num(prices['cacheWrite'])}")
            return " / ".join(parts) + " 积分/百万 tokens"
        return "按实际用量结算（未公布 token 单价）"

    price, unit = _base_price(model)
    if price > 0:
        return f"{_fmt_num(price)} 积分/{unit} 次" if unit > 1 else f"{_fmt_num(price)} 积分/次"
    if model.get("surcharge_rules"):
        return "按用量计价"
    return "未公布单价（以平台计价为准）"


def _price_cell(model: dict) -> str:
    """清单表格里的一格计价（紧凑形态；单位由该类型的「计价列」图例统一说明）。

    只与 `_price_text` 在「按 token 计费」这一类上不同：表格里逐行重复
    `输入 650 / 输出 3100 积分/百万 tokens` 会把行撑到 104+ 列，80 列终端整张表折行、
    列对齐全废；改成 `650/3100` 后，含义由图例
    `输入/输出，单位＝积分/百万 tokens` 承担，读起来更快也更整齐。
    """
    if _billing_mode(model) == "per_token":
        prices = _token_prices(model)
        if any(prices.values()):
            cell = f"{_fmt_num(prices['input'])}/{_fmt_num(prices['output'])}"
            if prices["cacheHit"]:
                cell += f"/命中{_fmt_num(prices['cacheHit'])}"
            return cell
        # 四档单价全为 0：不能回退 `_price_text` 的长句
        # （`按实际用量结算（未公布 token 单价）` 有 20 显示列，实测把文本组整行
        # 撑到 113 列、80 列终端折行、列对齐全废）。表格里只说「不是固定单价」，
        # 完整解释交给该类型的「计价列」图例与 `params` 详情。
        return "按量计费"
    if _billing_mode(model) in ("per_request", "per_time") and not _base_price(model)[0] \
            and model.get("surcharge_rules"):
        # 表格里塞不下「按用量计价」以外的解释（实测长句把行撑到 83 列、80 列终端折行）；
        # 单元格只需说清「不是固定单价」，明细不在人读输出里逐条列（见 changelog 1.10.0）。
        return "按用量计价"
    return _price_text(model)


def billing_spec(model: dict) -> dict:
    """模型计费的机器可读描述（供 `params --json` / `model --json` 消费）。

    `surcharge_rules` 是**原样透传**的契约规则（机器消费），
    `surcharge_text` 是对应的人读句子，两者同源，避免口径漂移。
    """
    billing = _billing_of(model)
    rules = [r for r in (model.get("surcharge_rules") or []) if isinstance(r, dict)]
    return {
        "mode": _billing_mode(model),
        "price": billing.get("price"),
        "unit": billing.get("unit"),
        "price_per_1m": _billing_of(model).get("pricePer1M") or None,
        "text": _price_text(model),
        "surcharge_rules": rules,
        "surcharge_text": [_surcharge_text(r) for r in rules],
    }


def _credits_per_yuan() -> float:
    """积分↔人民币换算比例（从对外契约取；取不到才用默认值）。"""
    ratio = _SPEC_CACHE.get("credit_per_yuan")
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else float(DEFAULT_CREDITS_PER_YUAN)


def _min_text_charge() -> float:
    """文本（按 token 计费）单次最低扣费，单位积分。"""
    try:
        return float(_SPEC_CACHE.get("min_text_charge") or 0)
    except (TypeError, ValueError):
        return 0.0


def _rule_param_name(rule: dict) -> str:
    return str(rule.get("param") or "").strip()


def _match_surcharge_charge(rule: dict, params: dict) -> float | None:
    """一条加收规则在给定参数下要加收多少积分；不命中返回 None。

    与平台服务端结算口径逐条对齐：
      - 参数没传 → 规则不参与（返回 None）；
      - `present` 命中但取值无法解析成数值 → **抛错**（服务端会 fail-closed 拒绝请求，
        本地不能给一个「看着能过、实际会被拒」的报价）；
      - `multiply_by_value`：`base + 值 × multiplier`，值为 0/负数 → 抛错；
      - `fixed`：直接加收 `credits`。
    """
    name = _rule_param_name(rule)
    if not name or name not in params:
        return None
    raw = params.get(name)
    match = str(rule.get("match") or "present").strip().lower()
    charge = str(rule.get("charge") or "fixed").strip().lower()

    def _number(value) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    if match == "exact":
        # 取值比较走 `_normalize_option_key`：与平台侧 `_normalize_option_key`
        # 同一口径（`4K` vs `4k` 之外还覆盖 `5.0 == 5`、布尔 `true`/`True`）。
        # 曾经直接比较字符串，导致 `contracted value 为数字 5、用户传 "5.0"`
        # 时本地判不命中而网关判命中（报价偏低）。
        if _normalize_option_key(raw) != _normalize_option_key(rule.get("matchValue")):
            return None
    elif match == "range":
        actual = _number(raw)
        if actual is None:
            return None
        low, high = _number(rule.get("rangeMin")), _number(rule.get("rangeMax"))
        if low is not None and actual < low:
            return None
        if high is not None and actual > high:
            return None

    if charge == "multiply_by_value":
        actual = _number(raw)
        multiplier = _number(rule.get("multiplier"))
        # 契约**没有 `base` 键**时按 0 计：服务端投影只在 base 非 0 时输出该键，
        # 而结算侧读的是规则配置里的基准值（未配置时按 `0`）。本文件的近似价
        # `_rule_credit_bounds` 与服务端结算口径都按 0 处理；
        # 精确价分支若把「缺 base」当成「取值非法」，就会对同一份契约报出自相矛盾的
        # 结论——线上 4 个时长加收模型的精确报价因此**直接崩溃**（exit 1）。
        # 只有 `multiplier` 缺失才是真正的规则配置非法，仍旧 fail-closed。
        base = _number(rule.get("base")) or 0.0
        if actual is None or multiplier is None:
            raise NoovaError(
                f"参数「{name}」参与计费，但取值无法解析为数值：{raw!r}",
                hint=f"请给一个数值，例如 --param {name}=5",
            )
        if actual <= 0 or multiplier < 0 or base < 0:
            raise NoovaError(
                f"参数「{name}」参与计费，取值必须为正数，收到：{raw!r}",
                hint=f"例如 --param {name}=5",
            )
        return round(base + actual * multiplier, 2)
    try:
        return round(float(rule.get("credits") or 0), 2)
    except (TypeError, ValueError):
        return None


def compute_credit_cost(model: dict, params: dict | None = None) -> dict:
    """按**平台网关的真实结算口径**本地复算本次预计消耗积分。

    与平台服务端结算口径对齐：

    - `per_request` / `per_time`：`base = 单价 × 计费单位`，再加各条命中的加收规则，
      四舍五入到 2 位小数 → 可以给出**确定值**（`exact=True`）；
      合计 ≤ 0 时平台会 fail-closed 拒单，本地也必须如实说明「无法完成调用」。
    - `per_token`：真实花费取决于**实际**输入/输出 token 数，任何本地估算都是猜。
      因此只回报四档单价与最低扣费，`exact=False`，绝不编一个数字。
    """
    params = dict(params or {})
    mode = _billing_mode(model)
    ratio = _credits_per_yuan()
    rules = [r for r in (model.get("surcharge_rules") or []) if isinstance(r, dict)]

    if mode == "per_token":
        prices = _token_prices(model)
        floor = _min_text_charge()
        note = "按实际用量结算：单价见上表"
        if floor > 0:
            note += f"，单次最低扣费 {_fmt_num(floor)} 积分"
        return {
            "mode": mode,
            "exact": False,
            "credits": None,
            "yuan": None,
            "price_per_1m": prices,
            "min_text_charge": floor,
            "surcharge": [],
            "note": note,
        }

    price, unit = _base_price(model)
    base = round(price * unit, 2)

    surcharge: list[dict] = []
    total = base
    for rule in rules:
        charge = _match_surcharge_charge(rule, params)
        if charge is None:
            continue
        surcharge.append({"param": _rule_param_name(rule), "credits": charge,
                          "text": _surcharge_text(rule)})
        total = round(total + charge, 2)

    if total <= 0:
        return {
            "mode": mode,
            "exact": False,
            "credits": None,
            "yuan": None,
            "base": base,
            "surcharge": surcharge,
            "note": ("按当前参数本次请求费用为 0，平台会拒绝调用（计费配置异常），"
                     "请联系平台或换一个模型"),
        }
    return {
        "mode": mode,
        "exact": True,
        "credits": total,
        "yuan": round(total / ratio, 4) if ratio else None,
        "base": base,
        "unit": unit,
        "surcharge": surcharge,
        "note": "",
    }


def credit_estimate_text(
    model: dict, params: dict | None = None, *, pricing: dict | None = None
) -> str:
    """一句话报价（用户确认生成前必须看到）。

    **口径（唯一数字来源 = 服务端确定值）**：
    1. 服务端给出确定值（`pricing.credits` + `exact=true`）→ 照抄上屏，调用方**不自己算**；
    2. 其余一切情况（服务端明说给不出 / 旧后端没有 `pricing` 块 / 按 token 结算）
       → **不显示任何金额**，只把用户引向生成后的真实扣减。

    **为什么不给数字**：报价与真值可能冲突，冲突时一律以服务端结算为准；给不出确定值时，
    任何数字（含本地按契约复算的「预估」）都可能与最终扣减对不上，摆到用户面前就是误导。
    用户真正需要的信息，生成结束后的「本次扣减（账户余额差值）」已经完整给出——
    余额变了多少，就是花了多少。

    **也不转述服务端的 `note`**：如「请核对参数取值后用 params 重新询价」是**给 agent 的
    接口指令**，不是给用户的解释。该原因只保留在 `credit_estimate.note`（机器可读）里，
    供 agent 自行决策，**不得出现在用户可见文案中**。
    """
    ratio = _credits_per_yuan()
    rule = f"{_fmt_num(ratio)} 积分 = 1 元"
    resolved = pricing if isinstance(pricing, dict) else model.get("pricing")
    # 唯一数字来源 = 服务端的确定值。只有它才与最终扣减同源（同一结算函数）。
    authoritative = _authoritative_credits(resolved) if isinstance(resolved, dict) else None
    if authoritative is not None:
        credits, exact = authoritative
        if credits is not None and exact:
            return (f"服务端权威价：{_fmt_num(credits)} 积分"
                    f"（≈ {_fmt_num(round(credits / ratio, 4)) if ratio else None} 元；{rule}）")
    # 服务端给不出确定值（含按 token 结算、询价失败、旧后端无 pricing）：
    # **不给任何数字**，也不转述服务端 note（那是给 agent 的接口指令）。
    # 报价与真值可能冲突，冲突时以服务端结算为准；此时唯一可信的信息是
    # 生成结束后的「本次扣减（账户余额差值）」——余额变了多少就是花了多少。
    return "本次消耗以生成结算为准（生成结束后显示账户余额变化）"


def model_protocols(model: dict) -> list[str]:
    """该模型支持的协议路由（主协议在前，去重）。"""
    protocols = list(model.get("protocols") or [])
    primary = str((protocols or [""])[0] or "").strip().lower()
    if primary not in PROTOCOL_PATHS:
        primary = "openai"
    if primary not in protocols:
        protocols = [primary, *protocols]
    return protocols


def model_protocol_text(model: dict) -> str:
    """协议清单的紧凑展示文本，例如 `openai+anthropic`。"""
    return "+".join(model_protocols(model)) or "-"


def model_ability(model: dict) -> list[str]:
    """模型能力标签——**只由契约里的参数直接推导**，不解析文档、不臆测。

    判据是参数本身：含 `images`/`referenceImages` 类数组参数 ⇒ 支持参考图（图生图），
    参考视频/音频同理。契约没给参考类参数时就没有该标签（绝不因为「图片模型大概都能
    图生图」而补一个标签——那正是旧实现被推翻的错误来源）。
    """
    names = {str(p.get("name") or "").strip().lower() for p in (model.get("params") or [])}
    labels: list[str] = []
    if names & {"referenceimages", "images", "image", "img"}:
        labels.append("参考图")
    if names & {"referencevideos", "videos", "video"}:
        labels.append("参考视频")
    if names & {"referenceaudios", "audios", "audio"}:
        labels.append("参考音频")
    return labels


def _param_values_text(param: dict) -> str:
    """参数取值的展示文本（枚举值 + 范围），全部来自契约。

    `valueLabels` 是展示标签（如像素串 `2880x2880` 的 `1:1`），有则一并给出——
    这正是旧文档解析丢得最狠的一项。
    """
    values = [str(v).strip() for v in (param.get("values") or []) if str(v).strip()]
    labels = param.get("value_labels") if isinstance(param.get("value_labels"), dict) else {}
    if values:
        shown = [f"{v}（{labels[v]}）" if labels.get(v) else v for v in values]
        return "，".join(shown)
    bounds: list[str] = []
    if param.get("min") is not None or param.get("max") is not None:
        bounds.append(f"{_fmt_num(param.get('min'))}~{_fmt_num(param.get('max'))}")
    if param.get("minItems") is not None or param.get("maxItems") is not None:
        bounds.append(f"{_fmt_num(param.get('minItems'))}~{_fmt_num(param.get('maxItems'))} 项")
    return "，".join(bounds) if bounds else "-"


def _price_legend(model_type: str) -> str:
    if model_type == "text":
        return "输入/输出，单位＝积分/百万 tokens"
    if model_type in ("image", "video", "audio"):
        return "积分/次 或 积分/秒（按模型约定，逐条给出）"
    return "积分（按模型约定）"


def _usage_block(script: str) -> list[str]:
    """清单尾部的「怎么用」说明：命令示例 + 耗时预期 + 计费与余额口径。

    排版纪律：除脚本绝对路径那一行外，**每行控制在 80 列内**——中文注释按 2 列计。
    超过 80 列会在常见终端里折行，把示例的命令与参数拆散，反而更难照抄。
    """
    return [
        _rule("使用方法"),
        f"  脚本：{PYTHON_CMD} {script} <子命令>",
        "",
        "  图像   image --prompt \"一只戴墨镜的猫\"",
        "         [--model 编码] [--param 名=值] [--ref-image 路径|URL]",
        "  视频   video --prompt \"夕阳下的城市天际线\" --model 编码",
        "         --param duration=5 --param aspect_ratio=16:9",
        "  音频   audio --prompt \"轻快的钢琴短曲\" [--model 编码]",
        "  文本   chat  --prompt \"写一句品牌标语\" [--model 编码] [--stream]",
        "",
        "  查询   params --code 编码 --json　　查余额 credit",
        "         wait --id 任务ID --type video　　task --id 任务ID",
        "",
        "  同步 / 异步：",
        "    文本模型同步返回——一次请求直接拿正文，可 --stream 流式输出；",
        "    图像 / 视频 / 音频为异步任务——提交后返回任务 ID，脚本默认自动轮询到终态。",
        "    --no-wait 只提交不等待，之后用 wait / task 取结果。",
        "",
        _rule("耗时预期（用于向用户说明，非硬性上限）"),
        f"  图像 {_eta_text('image')}　音频 {_eta_text('audio')}　视频 {_eta_text('video')}",
        "  文本通常 1–10 秒出首字符；--stream 可边生成边输出。",
        f"  默认等待上限：图像 {DEFAULT_MAX_WAIT['image']}s / 音频 {DEFAULT_MAX_WAIT['audio']}s / "
        f"视频 {DEFAULT_MAX_WAIT['video']}s，可用 --max-wait 覆盖。",
        "  等待超时 ≠ 失败：任务仍在跑，用返回的任务 ID 继续 wait / task 即可。",
        "",
        _rule("计费与余额"),
        "  生成结束后脚本会显示：本次扣减（账户余额差值）与剩余积分。",
        "  单价以上面清单与 params 查询为准，不要凭记忆报价；余额不足返回 402。",
    ]


def cmd_models(args) -> int:
    models = _fetch_public_models()
    total = len(models)
    if args.type:
        models = [m for m in models if m["model_type"] == args.type]
    if args.online_only:
        models = [m for m in models if m["status"] == CALLABLE_MODEL_STATUS]

    if args.json:
        # 全部 `--json` 出口一律走 `_dump_json`（脱敏 + 媒体字段白名单放行）。
        # agent 消费的就是这些出口，人读路径脱敏而它们不脱敏等于白做（已实测）。
        print(_dump_json([
            {
                "model_code": m["code"],
                # 可调用 = 平台给了编码 **且** 处于上线状态；维护中模型只展示不可调用。
                "callable": bool(m["code"]) and m["status"] == CALLABLE_MODEL_STATUS,
                "display_name": m["display_name"],
                "model_type": m["model_type"],
                # `family` 只用于「同名模型」的提示分组（键 = 类型:显示名）；
                # 选择与计价一律按 `model_code`——它才是请求里要传的值。
                "family": model_family_key(m),
                "approximate_credit": approximate_credit(m),
                "status": m["status"],
                # 状态中文化：向用户展示时必须注明「在线」还是「维护中」。
                "status_label": _status_label(m["status"]),
                "ability": model_ability(m),
                "protocols": model_protocols(m),
                "invocation_mode": "sync" if m["model_type"] in SYNC_MODEL_TYPES else "task",
                "billing": billing_spec(m),
            } for m in models
        ]))
        return 0

    scope = f"{TYPE_LABELS.get(args.type, args.type)}模型" if args.type else "全部模型"
    print(f"NooVa AI 公开模型清单（实时拉取 · {scope} {len(models)} 个 · 平台共 {total} 个）")
    print("说明：模型与价格均随平台调整，本清单即本次接口返回的真实数据。")

    if not models:
        print()
        print(f"当前没有符合条件的模型（{scope}）。")
        print("请不要自行编造模型编码；稍后重试，或去掉 --type/--online 过滤条件再看一次。")
        return 0

    by_type: dict[str, list[dict]] = {}
    for model in models:
        by_type.setdefault(model["model_type"] or "unknown", []).append(model)

    order = [t for t in MODEL_TYPES if t in by_type]
    order += [t for t in by_type if t not in MODEL_TYPES]

    for model_type in order:
        group = by_type[model_type]
        print()
        print(_rule(f"{TYPE_LABELS.get(model_type, model_type)}模型 · {len(group)} 个"))
        print(f"  调用方式：{TYPE_MODE_HINTS.get(model_type, '以平台文档为准')}")
        print(f"  典型耗时：{_eta_text(model_type)}")
        print(f"  计价列　：{_price_legend(model_type)}")

        w_code = min(max(_display_width(m["code"]) for m in group), 26)
        w_name = min(max(_display_width(m["display_name"]) for m in group), 22)
        w_proto = min(max(_display_width(model_protocol_text(m)) for m in group), 18)
        w_state = max(_display_width(_status_label(m["status"])) for m in group)
        header = (f"{_pad('编码', w_code)}  {_pad('名称', w_name)}  "
                  f"{_pad('协议', w_proto)}  {_pad('状态', w_state)}  计价")

        print()
        print(f"  {header}")
        # 按族遍历：同名模型挨着显示，用户才看得出「这是同一个名字下的几个
        # 独立模型（编码不同）」。族标题只在真的同名（族内 >1）时打印——
        # 实测给每个模型都套一个族标题会把清单撑得又长又吵。
        for family in build_model_families(group):
            if len(family["lines"]) > 1:
                print(f"  ▸ {_safe(family['label'])}（同名 {len(family['lines'])} 个，"
                      f"按编码区分）")
            for model in family["lines"]:
                code_cell = model["code"] or "（无编码，不可调用）"
                print(f"  {_pad(code_cell, w_code)}  {_pad(model['display_name'], w_name)}  "
                      f"{_pad(model_protocol_text(model), w_proto)}  "
                      f"{_pad(_status_label(model['status']), w_state)}  {_price_cell(model)}")

    if args.type in (None, "audio") and not by_type.get("audio"):
        print()
        print("注：音频类型当前无可用模型；接口已就绪，模型上线后会自动出现。")

    print()
    for line in _usage_block(SELF_SCRIPT):
        print(line)
    return 0


def model_contract(model: dict, models: list[dict] | None = None) -> dict:
    """模型的对外契约视图（`cmd_model` 与 `cmd_params` 共用同一份，避免口径漂移）。

    `models` 给全量清单时额外带出**同名其它编码**——用户看到一条后最常问的
    就是「还有别的吗、差在哪」，不给就只能再跑一次 `models` 自己找。
    """
    protocols = model_protocols(model)
    family_key = model_family_key(model)
    siblings = [m for m in (models or []) if m["code"] and m["code"] != model["code"]
                and model_family_key(m) == family_key]
    siblings.sort(key=lambda m: m["code"])
    return {
        "model_code": model["code"],
        "callable": bool(model["code"]),
        "display_name": model["display_name"],
        "family": family_key,
        "siblings": [{
            "model_code": m["code"],
            "price_cell": _price_cell(m),
            "billing_text": _price_text(m),
            "approximate_credit": approximate_credit(m),
        } for m in siblings],
        "approximate_credit": approximate_credit(model),
        "model_type": model["model_type"],
        "status": model["status"],
        "ability": model_ability(model),
        "protocols": protocols,
        "primary_protocol": protocols[0] if protocols else "",
        "invocation": _invocation_spec(model),
        "billing": billing_spec(model),
        "params": model["params"],
        "credit_per_yuan": _credits_per_yuan(),
    }


def _print_param_table(params: list[dict]) -> None:
    """参数契约（人读）。列：参数名 / 类型 / 必填 / 取值 / 范围。"""
    w_name = min(max(_display_width(p["name"]) for p in params), 26)
    w_type = min(max(_display_width(p["type"]) for p in params), 14)
    print(f"  {_pad('参数名', w_name)}  {_pad('类型', w_type)}  必填  取值 / 范围")
    for param in params:
        print(f"  {_pad(param['name'], w_name)}  {_pad(param['type'], w_type)}  "
              f"{'是' if param['required'] else '否'}    {_safe(_param_values_text(param))}")


def _print_contract(spec: dict, model: dict) -> None:
    """契约的人读渲染（`cmd_model` 与 `cmd_params` 共用）。"""
    print(f"模型　：{spec['display_name']}（{spec['model_code']}）")
    print(f"类型　：{TYPE_LABELS.get(spec['model_type'], spec['model_type'])}"
          f"　　状态：{_status_label(spec['status'])}")
    if not spec["callable"]:
        print("调用　：不可调用（平台未返回该模型的调用编码）")
        return
    print(f"协议　：{'、'.join(PROTOCOL_LABELS.get(p, p) for p in spec['protocols']) or '-'}"
          f"（主协议 {PROTOCOL_LABELS.get(spec['primary_protocol'], spec['primary_protocol'])}）")
    if spec["ability"]:
        print(f"能力　：{'、'.join(spec['ability'])}")
    print(f"调用　：{TYPE_MODE_HINTS.get(spec['model_type'], '-')}")
    print(f"典型耗时：{_eta_text(spec['model_type'])}")
    print(f"计费　：{_safe(spec['billing']['text'])}")
    ratio = _credits_per_yuan()
    print(f"换算　：{_fmt_num(ratio)} 积分 = 1 元"
          f"（即 1 积分 ≈ {_fmt_num(round(1 / ratio, 4))} 元）")
    print(f"近似消耗：{_safe(_estimate_text(spec['approximate_credit']))}")
    print("　（选模型阶段的近似值；参数定好后会再报一次精确价）")
    if spec["params"]:
        print(f"\n参数（{len(spec['params'])} 个，名称与取值必须原样使用）：")
        _print_param_table(spec["params"])
    else:
        print("\n（该模型没有参数契约条目，只能传提示词）")
    siblings = spec.get("siblings") or []
    if siblings:
        print(f"\n同名其它模型（{len(siblings)} 个，编码不同、计价可能不同）：")
        w_code = min(max(_display_width(m["model_code"]) for m in siblings), 26)
        print(f"  {_pad('编码', w_code)}  计价")
        for m in siblings:
            print(f"  {_pad(m['model_code'], w_code)}  {_safe(m['price_cell'])}")
            estimate = _estimate_text(m["approximate_credit"])
            # 固定单价的近似消耗与「计价」列完全同文，重复一行是噪声；
            # 只有区间/定性形态（如 `约 240~900 积分/次`）才额外给出。
            if estimate and estimate != m["price_cell"]:
                print(f"    ↳ {_safe(estimate)}")

def cmd_model(args) -> int:
    """人读的模型详情：身份 + 计价 + 调用方式 + 参数契约。"""
    models = _fetch_public_models()
    model = _select_by_code(models, args.code)
    if not model:
        raise NoovaError(f"未找到模型「{args.code}」", hint="运行 models 查看当前可用清单")
    spec = model_contract(model, models=models)
    if args.json:
        print(_dump_json(spec))
        return 0
    _print_contract(spec, model)
    return 0


def _invocation_spec(model: dict) -> dict:
    """描述"怎么调用这个模型"：路由、模式、是否支持流式。

    文本模型：按协议给出的官方原生路由（不使用统一入口）。
    任务型模型：平台统一任务入口 + 轮询。
    """
    protocols = model_protocols(model)
    primary = str((protocols or [""])[0] or "").strip().lower()
    if primary not in PROTOCOL_PATHS:
        primary = "openai"

    if model["model_type"] in SYNC_MODEL_TYPES:
        routes = [{"protocol": p, "path": resolve_route_path(p, model["code"]),
                   "recommended": p == primary,
                   "stream_supported": True,
                   "response_format": "该协议原生响应格式"} for p in protocols]
        primary_route = next((r for r in routes if r["protocol"] == primary), routes[0] if routes else None)
        return {
            "mode": "sync",
            # 带上 HTTP 方法：`parameters.md` 的示例是 `"POST /v1/chat/completions"`，
            # 只给路径会让照抄的 agent 少一个关键信息。
            "create": f"POST {primary_route['path']}" if primary_route else "",
            "poll": None,
            "stream_supported": bool(primary_route and primary_route["stream_supported"]),
            "eta_seconds": list(ETA_SECONDS.get("text", (1, 10))),
            "default_max_wait": DEFAULT_MAX_WAIT_FALLBACK,
            "routes": routes,
        }
    return {
        "mode": "task",
        "create": f"POST {TASK_CREATE_PATH}",
        "poll": f"POST {TASK_POLL_PATH}",
        "stream_supported": False,
        "eta_seconds": list(ETA_SECONDS.get(model["model_type"], (0, 0))),
        "default_max_wait": DEFAULT_MAX_WAIT.get(model["model_type"], DEFAULT_MAX_WAIT_FALLBACK),
        "routes": [{"protocol": None, "path": TASK_CREATE_PATH, "recommended": True,
                    "response_format": "平台统一任务契约"}],
    }


def cmd_params(args) -> int:
    """参数契约（给 agent 消费的主入口；`--json` 为推荐形态）。"""
    models = _fetch_public_models()
    model = _select_by_code(models, args.code)
    if not model:
        raise NoovaError(f"未找到模型「{args.code}」", hint="运行 models 查看当前可用清单")
    spec = model_contract(model, models=models)
    if args.json:
        print(_dump_json(spec))
        return 0
    _print_contract(spec, model)
    if spec["params"]:
        if spec["invocation"]["mode"] == "sync":
            print("\n提示：文本模型走协议官方路由，报文使用该协议的官方字段"
                  "（[OI]: `messages`；Anthropic: `messages` + 必填 `max_tokens` + `system`）。")
        else:
            print("\n提示：未列出的参数名不会被平台识别（可能被忽略）。请严格使用参数名原样传递。")
    return 0


# ---------------------------------------------------------------------------
# 交互面板（问答板）数据契约
# ---------------------------------------------------------------------------
# 生成**前**必须让用户先确认「用哪个模型 + 这个模型的每个参数取什么值」。
# 面板要问什么、每个字段有哪些可选项、哪个字段只能看不能选，全部由运行时接口
# （`/api/models/params` 契约）算出来——取数口径只此一处，宿主 agent 只负责渲染。
#
# 为什么放在脚本里而不是写进文档让 agent 自己算：本 skill 要同时服务多个宿主，
# 让每个 agent 各自推导一遍取值，必然出现「A 宿主答对了、B 宿主臆造了枚举」；
# 而枚举臆造正是用户最容易被误导的地方（铁律 3）。放在这里还能被测试覆盖。

# 面板最后一步的确认提示：用户把参数都定好后，必须看到这句话才按确认。
FORM_CONFIRM_HINT = "确认后立即开始生成"

# 类型列归一化（去掉空格后比较，兼容 `string[]` / `array[string]` / `String []` 等写法）。
_NUMERIC_PARAM_TYPES = ("number", "integer", "int", "float", "double", "long")
_LIST_PARAM_TYPES = ("string[]", "array[string]", "array", "list", "[]string", "stringarray")

# 面板里给参数起的中文名。契约里的 `label` 为空时才回退到这里的固定译名。
# （实测 `referenceVideos` 的说明就是 `referenceVideos`），照抄给用户等于没给信息；
# 这里对常见参数给固定译名，命中不了的才退回说明列。
_FIELD_LABELS = {
    "prompt": "提示词",
    "model": "模型",
    "aspectratio": "画面比例",
    "aspect_ratio": "画面比例",
    "ratio": "画面比例",
    "imagesize": "图片分辨率",
    "image_size": "图片分辨率",
    "resolution": "分辨率",
    "size": "尺寸",
    "quality": "画质",
    "duration": "时长",
    "seconds": "时长",
    "fps": "帧率",
    "seed": "随机种子",
    "mode": "模式",
    "style": "风格",
    "format": "格式",
    "voice": "音色",
    "language": "语言",
    "speed": "语速",
    "referenceimages": "参考图",
    "reference_images": "参考图",
    "referencevideos": "参考视频",
    "reference_videos": "参考视频",
    "referenceaudios": "参考音频",
    "reference_audios": "参考音频",
}
_REFERENCE_FIELD_LABELS = {"ref_image": "参考图", "ref_video": "参考视频", "ref_audio": "参考音频"}

# 面板标题里的类型名（与 TYPE_LABELS 同源，单独取一份是为了让文案能整句替换）。
_FORM_FIELD_LABEL_LIMIT = 20


def _param_options(param: dict) -> tuple[list[str], str | None]:
    """算出参数的可选值，返回 `(options, source)`。

    **只认契约**：服务端配置成 `select` 的参数一定带完整 `values`，这就是唯一真源。
    契约没给取值就是自由输入 —— 绝不去文档里猜、绝不从说明文本里抠
    （旧实现三种兜底已全部删除：它们会把「说明里恰好出现的数字」当成枚举，
    用户照着选必然失败）。
    """
    values = [str(v).strip() for v in (param.get("values") or []) if str(v).strip()]
    return (values, "contract") if values else ([], None)


def _field_cli(model_type: str, name: str, ref_reverse: dict) -> str:
    """该字段在命令行里怎么传（让 agent 不必猜参数名怎么落到本工具上）。"""
    option = ref_reverse.get(str(name).strip().lower())
    if option:
        if model_type in SYNC_MODEL_TYPES:
            return "--image" if option == "ref_image" else f"--param {name}"
        return {"ref_image": "--ref-image", "ref_video": "--ref-video",
                "ref_audio": "--ref-audio"}[option]
    if str(name).strip().lower() == "prompt":
        return "--prompt"
    return f"--param {name}"


def _option_rule_key(rule: object) -> tuple | None:
    """把一条选项可见规则归一成可比较的 `(when, operator, value)`；无效规则给 None。

    与后端 `_normalize_visible_when` 同一口径：`when` 为空即「无依赖」（不是
    「依赖空参数名」），`filled` / `empty` 两个操作符不比较取值。
    """
    if not isinstance(rule, dict):
        return None
    # 兼容两种写法：契约投影本身用 `when`（后端 `visible_when` 的字段名），
    # 面板分组里我们把被依赖参数改叫 `param`（更贴近「依赖哪个参数」的读法）。
    when = str(rule.get("when") or rule.get("param") or "").strip()
    if not when:
        return None
    operator = str(rule.get("operator") or "eq").strip().lower() or "eq"
    if operator in ("filled", "empty"):
        return (when, operator, None)
    return (when, operator, rule.get("value"))


def _option_rule_matches(rule: dict, params: dict) -> bool:
    """当前已知参数下，这条选项规则是否命中（语义与平台服务端判定对齐）。

    只做**只读判断**：控制器参数未提供时返回 `False`（不是「不可见」，而是
    「无法判断」——调用方据此退回「按组列出、不阻拦」的渲染，见 SKILL.md）。
    """
    key = _option_rule_key(rule)
    if key is None:
        return False
    when, operator, expected = key
    target = None
    for name, value in (params or {}).items():
        if str(name).strip().lower() == when.lower():
            target = value
            break
    else:
        # 控制器不在本次已知参数里：无法判断（含 filled/empty 也不猜）。
        return False
    filled = not (target is None or (isinstance(target, str) and not target.strip())
                  or (isinstance(target, list) and not target))
    if operator == "filled":
        return filled
    if operator == "empty":
        return not filled
    same = _normalize_option_key(target) == _normalize_option_key(expected)
    return not same if operator == "ne" else same


def _param_field(param: dict, model_type: str, ref_reverse: dict,
                 *, known_params: dict | None = None) -> dict:
    """把一个契约参数转成面板字段（`kind`：能不能选、单选还是只能看）。

    取值形态**只由契约决定**：
      - `values` 恰好 1 个 → `fixed`（唯一取值，用户无从选择，但必须知道）；
      - `values` ≥ 2 个     → `choice`；
      - 数值类型           → `number`（范围取契约 `min`/`max`）；
      - 数组类型           → `list`（数量上限取 `maxItems`）；
      - 其余               → `text`。

    多取值字段若契约给了 `optionVisibleWhen`（与 `values` 同序的条件数组），额外
    输出 `depends_on` / `option_groups`：把「一次平铺几十项」拆成「按控制器取值
    分组的几段」，并给出当前已知取值下命中的那一组（`active_group`）。
    `known_params`（单值预设 + 用户已定参数）仅用于算 `active_group`，**不裁剪**
    `options` / `option_groups`——哪些真的可见由用户当前选择决定，面板要能重算。
    """
    name = str(param.get("name") or "").strip()
    type_text = str(param.get("type") or "string").strip()
    options, source = _param_options(param)

    kind = "text"
    if len(options) == 1:
        kind = "fixed"
    elif len(options) > 1:
        kind = "choice"

    if kind == "text":
        lowered_type = type_text.lower().replace(" ", "")
        if lowered_type in _NUMERIC_PARAM_TYPES:
            kind = "number"
        elif lowered_type in _LIST_PARAM_TYPES:
            kind = "list"

    # 服务端给的 `label` 优先（它才是平台口径的中文名），其次本工具的固定译名，最后退回参数名。
    ref_option = ref_reverse.get(name.lower())
    label = (str(param.get("label") or "").strip()
             or _REFERENCE_FIELD_LABELS.get(ref_option or "")
             or _FIELD_LABELS.get(name.lower())
             or name)
    if _display_width(label) > _FORM_FIELD_LABEL_LIMIT:
        label = label[:12].rstrip() + "…"

    labels = param.get("value_labels") if isinstance(param.get("value_labels"), dict) else {}
    notes: list[str] = []
    if kind == "fixed":
        notes.append("该参数当前只有这一个取值，必须使用它，无需选择")
    if kind == "number" and (param.get("min") is not None or param.get("max") is not None):
        notes.append(f"可填 {_fmt_num(param.get('min'))}~{_fmt_num(param.get('max'))} 之间的数值")
    if kind == "list":
        limit = param.get("maxItems")
        notes.append("可传多个（最多 " + _fmt_num(limit) + " 个）：公网 URL 或本地文件路径"
                     "（本地文件会自动上传）" if limit is not None else
                     "可传多个：公网 URL 或本地文件路径（本地文件会自动上传）")

    # 条件可见性：契约的 `optionVisibleWhen` 与 `values` 同序（`_align_option_visible_when`
    # 已按 values 长度对齐）。有规则的合并成「按控制器取值分组」，无规则的另立一组
    # （**始终可见**，与后端「无 visible_when 即放行」逐条一致）。
    rules = list(param.get("option_visible_when") or [])
    rule_keys = [_option_rule_key(r) for r in rules]
    depends_on: str | None = None
    option_groups: list[dict] = []
    active_group: int | None = None
    if any(key is not None for key in rule_keys):
        for key in rule_keys:
            if key is not None:
                depends_on = key[0]
                break
        buckets: dict = {}
        for value, key in zip(options, rule_keys):
            bucket = buckets.get(key)
            if bucket is None:
                bucket = buckets[key] = {
                    "when": (None if key is None else
                             {"param": key[0], "operator": key[1], "value": key[2]}),
                    "always": key is None,
                    "options": [],
                }
                option_groups.append(bucket)
            bucket["options"].append({"value": value,
                                      "label": str(labels.get(value) or value)})
        # 命中的分组（仅对有规则的组求值；无规则组恒可见，不参与「命中」判定）。
        for index, bucket in enumerate(option_groups):
            if bucket["always"]:
                continue
            if _option_rule_matches(bucket["when"], known_params or {}):
                active_group = index
                break

    field = {
        "name": name,
        "label": label,
        "type": type_text,
        "required": bool(param.get("required")),
        "kind": kind,
        "fixed": kind == "fixed",
        "options": [{"value": v, "label": str(labels.get(v) or v)} for v in options],
        "option_source": source,
        "range": ([param.get("min"), param.get("max")]
                  if kind == "number" and (param.get("min") is not None
                                           or param.get("max") is not None) else None),
        "cli": _field_cli(model_type, name, ref_reverse),
        "note": "；".join(notes),
    }
    if depends_on is not None:
        field["depends_on"] = depends_on
        field["option_groups"] = option_groups
        field["active_group"] = active_group
    # 字段级条件可见（契约 `visibleWhen`）：当前生产 0 命中，属同族漏投影的预防性
    # 补齐。这里**只透出、不过滤**——面板少显示一个必填字段的代价远大于多显示一个
    # 不适用的字段（后者用户看得见、也不会发错请求）。
    visible_when = param.get("visible_when")
    if isinstance(visible_when, dict):
        field["visible_when"] = visible_when
        operator = str(visible_when.get("operator") or "eq").strip().lower()
        if operator == "filled":
            field["visible_note"] = "仅当「%s」已填写时适用" % visible_when.get("when")
        elif operator == "empty":
            field["visible_note"] = "仅当「%s」未填写时适用" % visible_when.get("when")
        else:
            field["visible_note"] = "仅当「%s」= %s 时适用" % (
                visible_when.get("when"), visible_when.get("value"))
    return field


def _topological_order(fields: list[dict]) -> list[dict]:
    """把字段排成「控制器在依赖项之前」的拓扑序，同级内保持「必填在前」稳定排序。

    必要性（生产实证）：`gpt-image-2-stable` 的 `imageSize`（控制器）是 `optional`、
    `aspect_ratio`（依赖项）是 `required`，只按「必填优先」排会把控制器排到依赖项之后，
    用户先撞上 14/40 项的平铺列表，再回头发现「原来先选分辨率能收窄」。

    防御：`depends_on` 指向不存在的字段、或形成环（契约由运营配置，理论上不该出现）
    → **退回原「必填优先」顺序**，绝不为了排序卡死或丢字段。
    """
    index_of = {str(f.get("name") or ""): i for i, f in enumerate(fields)}
    # 依赖边：depends_on 指向的字段必须排在前面。控制器不在本次字段里
    # （单值预设已自动采用）时无需求序，也不会成环。
    edges: dict[str, list[str]] = {}
    indegree = {str(f.get("name") or ""): 0 for f in fields}
    for f in fields:
        name = str(f.get("name") or "")
        dep = f.get("depends_on")
        if not dep or dep not in index_of or dep == name:
            continue
        edges.setdefault(dep, []).append(name)
        indegree[name] += 1

    # 稳定排序的初始序：必填在前，同级保持契约内相对顺序。
    ordered_names = [str(f.get("name") or "") for f in
                     sorted(fields, key=lambda f: 0 if f["required"] else 1)]
    position = {name: i for i, name in enumerate(ordered_names)}

    ready = sorted([n for n in ordered_names if indegree[n] == 0], key=lambda n: position[n])
    result: list[str] = []
    remaining = dict(indegree)
    while ready:
        name = ready.pop(0)
        result.append(name)
        for child in edges.get(name, []):
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
                ready.sort(key=lambda n: position[n])
    if len(result) != len(ordered_names):
        # 有环（或自环被跳过）：契约异常，退回原顺序而不是丢字段。
        return sorted(fields, key=lambda f: 0 if f["required"] else 1)
    by_name = {str(f.get("name") or ""): f for f in fields}
    return [by_name[name] for name in result]


def build_param_form(model: dict, *, model_type: str | None = None,
                     chosen_params: dict | None = None) -> dict:
    """参数面板：基于**已选模型**的契约参数，逐个字段说明能不能选、能选什么。

    `chosen_params`（用户已经定下的参数）非空时，面板额外给出**本次预计消耗积分**——
    这是「用户确认前必须看到价钱」的落点（`form --code X --param k=v`）。

    **单值参数不进面板**：`values` 恰好 1 个的参数用户无从选择，仍按 `fixed` 展示
    只会占位。它们改为进 `preset_params`（已自动采用），但仍然**参与可见性求值**
    ——生产 4 个图像模型的 `aspect_ratio` 选项条件依赖的正是单值的 `imageSize`，
    不带入求值就会得到「没有任何可见选项」的空面板。
    """
    mtype = model_type or model["model_type"]
    ref_reverse = {str(v).strip().lower(): k
                   for k, v in _reference_field_map(model, quiet=True).items()}
    code_tokens = {str(model.get("code") or "").strip().lower(), "model"}

    # 用户已定参数（命令行给的）与单值预设共同构成「当前已知取值」，
    # 用于算每个分组字段的 `active_group`。
    known_params: dict = dict(chosen_params or {})
    preset_params: dict = {}
    visible_params: list[dict] = []
    fields: list[dict] = []
    hidden: list[str] = []
    for param in model.get("params") or []:
        name = str(param.get("name") or "").strip()
        if not name:
            continue
        # 模型编码本身不作为「参数」问用户：它已经在上一步选定了，
        # 再问一遍等于让用户重复决策，还容易与 --model 冲突。
        if name.lower() in code_tokens:
            hidden.append(name)
            continue
        values = [str(v).strip() for v in (param.get("values") or []) if str(v).strip()]
        if len(values) == 1:
            # 单值参数：不进面板，但必须带入求值（见 docstring）。
            preset_params[name] = values[0]
            known_params.setdefault(name, values[0])
            continue
        visible_params.append(param)
    for param in visible_params:
        fields.append(_param_field(param, mtype, ref_reverse, known_params=known_params))
    # 拓扑序（控制器先于依赖项），同级内「必填在前」；异常时退回必填优先。
    fields = _topological_order(fields)

    payload: dict = {
        "panel": "param_panel",
        "step": 3,
        "model_code": model["code"],
        "callable": bool(model["code"]),
        "display_name": model["display_name"],
        "model_type": mtype,
        "type_label": TYPE_LABELS.get(mtype, mtype),
        "status": _status_label(model["status"]),
        "title": f"{model['display_name']} 的参数",
        "billing": billing_spec(model),
        "credit_per_yuan": _credits_per_yuan(),
        "credit_rule": f"{_fmt_num(_credits_per_yuan())} 积分 = 1 元",
        "invocation": _invocation_spec(model),
        "eta": _eta_text(mtype),
        "fields": fields,
        "hidden_fields": hidden,
        # 单值参数（`values` 恰好 1 个）：不进选择面板，已自动采用。
        # 载体是这里 + `_build_media_payload` + `_ensure_required_params` 三处，
        # 缺任何一处都会变成「面板里没这个参数、请求前却报缺它」。
        "preset_params": preset_params,
        "contract_available": bool(model.get("params")),
        "confirm_hint": FORM_CONFIRM_HINT,
        "next_step": FORM_CONFIRM_HINT,
    }
    if preset_params:
        adopted = "、".join("%s=%s" % (k, v) for k, v in preset_params.items())
        payload["preset_note"] = ("单值参数：" + adopted
                                  + "（该参数当前只有这一个取值，无需选择）")
    if chosen_params:
        # 报价按**含单值预设**的完整参数算：少传一个单价相关参数会低估费用。
        estimate_params = {**preset_params, **chosen_params}
        # 权威价优先：服务端用真实结算函数算（服务端出数，调用方不自算）。
        # 拿不到（旧后端/网络失败）时回退本地预估，文案由 credit_estimate_text 标注「预估」。
        # 与 `_announce_cost` **同一套取价策略**（两处口径必须一致，否则面板与提交报不同价）：
        #   有加收规则 → 按本次参数询价（只带参与计费的参数，见 _pricing_quote_params）；
        #   无加收规则 → 价格与参数无关，契约自带 `pricing` 即真值，不再多打一次请求。
        if model.get("surcharge_rules"):
            authoritative = fetch_pricing(
                model, _pricing_quote_params(model, estimate_params)
            )
        else:
            authoritative = model.get("pricing")
        estimate = compute_credit_cost(model, estimate_params)
        # 数值层与文案层**必须同源**：两层都只认服务端的确定值。
        # 服务端给不出确定值时，两层同时留空——否则会出现「文案说以结算为准、
        # 数值层却顶一个本地复算数字」，agent 读到哪个全凭运气，而该数字
        # 可能与最终扣减对不上（报价与真值冲突时一律以服务端结算为准）。
        estimate = _estimate_with_authoritative(estimate, authoritative)
        if authoritative is not None:
            payload["credit_authoritative"] = authoritative
        payload["chosen_params"] = chosen_params
        payload["credit_estimate"] = estimate
        # 文案层只吃**服务端作答**（authoritative）：不传契约自带的默认组合价，
        # 也不让本地复算顶替。给不出确定值就只说「以生成结算为准」。
        # 询价失败时传**空 dict**而不是 None：None 会让文案层退回契约 `pricing`
        # （那是「不传参数」的默认组合价，对本次参数而言与底价等价），
        # 于是出现「面板报 20、提交却说不给数字」——与 `_announce_cost` 口径必须一致。
        payload["credit_estimate_text"] = credit_estimate_text(
            model, estimate_params,
            pricing=(authoritative if authoritative is not None else {}),
        )
    if not fields:
        payload["empty_note"] = ("该模型未提供参数契约条目；只有提示词可填。"
                                 "契约缺失时不要臆造参数名或取值。")
    if mtype in SYNC_MODEL_TYPES:
        # 文本生成还有几个只属于命令行、不属于模型参数契约的旋钮。单独放，
        # 避免它们被当成「模型参数」向用户展示。
        payload["extra_cli_options"] = [
            {"flag": "--system", "kind": "text", "note": "系统提示词（可选）"},
            {"flag": "--stream", "kind": "flag", "note": "流式输出（可选）"},
            {"flag": "--max-tokens", "kind": "number", "note": "输出上限（可选）"},
            {"flag": "--temperature", "kind": "number", "note": "采样温度（可选）"},
        ]
    return payload


def build_model_form(model_type: str, models: list[dict]) -> dict:
    """模型选择面板：先让用户选模型，选完才谈参数。"""
    group = [m for m in models if m["model_type"] == model_type]
    options: list[dict] = []
    unavailable: list[dict] = []
    for model in group:
        if not model["code"]:
            unavailable.append({
                "display_name": model["display_name"],
                "reason": "平台未返回该模型的调用编码，本工具无法调用",
            })
            continue
        if model["status"] not in VISIBLE_MODEL_STATUSES:
            # 白名单外的状态（test / deprecated / 未知）**整条不呈现**：
            # 它们不属于对外可见集合，不得出现在面向用户的输出里。
            continue
        if model["status"] != CALLABLE_MODEL_STATUS:
            # 维护中等状态：**呈现但不可选**，并明确注明状态，
            # 让用户知道该模型存在、当前不可调用。
            unavailable.append({
                "display_name": model["display_name"],
                "model_code": model["code"],
                "status": _status_label(model["status"]),
                "reason": f"当前状态：{_status_label(model['status'])}（暂不可调用）",
            })
            continue
        options.append({
            "value": model["code"],
            "label": model["display_name"],
            "family": model_family_key(model),
            "status": _status_label(model["status"]),
            "billing": _price_text(model),
            # 选模型阶段的近似消耗（参数还没定，所以是区间或定性）。
            # `credit_estimate` 是**不带依据**的短文案（面板一行放得下，实测带
            # 依据会到 88 列）；`credit_estimate_detail` 是依据（如
            # `duration 4~30 × 80 积分`），供需要解释数字来源的调用方使用。
            "credit_estimate": approximate_credit(model)["text"],
            "credit_estimate_detail": approximate_credit(model)["detail"],
            "params_count": len(model.get("params") or []),
        })

    names = [o["label"] for o in options]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    payload: dict = {
        "panel": "model_picker",
        "step": 2,
        "model_type": model_type,
        "type_label": TYPE_LABELS.get(model_type, model_type),
        "title": f"选择要使用的{TYPE_LABELS.get(model_type, model_type)}模型",
        "eta": _eta_text(model_type),
        "mode_hint": TYPE_MODE_HINTS.get(model_type, ""),
        "billing_legend": _price_legend(model_type),
        "options": options,
        "unavailable": unavailable,
        "next_step": ("选完模型后，面板列出该模型的全部参数，"
                      "并在确认前给出精确消耗积分"),
    }
    if duplicated:
        # 实测同名模型确实存在（同一展示名对应多条编码，是**不同的模型**，
        # 请求时 `model` 传的值不同）。唯一可靠的选择键是**模型编码**，
        # 因此把「编码 + 计价」一起摆出来让用户选，不引入任何模糊的展示名。
        payload["duplicate_names"] = duplicated
        payload["duplicate_note"] = "存在同名模型（编码不同），按「模型编码 + 计价」让用户选一个"
    if not options:
        payload["empty_note"] = (f"当前没有可用的{TYPE_LABELS.get(model_type, model_type)}模型"
                                 "（以平台实时清单为准，稍后重试）")
    return payload


def build_type_form(models: list[dict]) -> dict:
    """第一步面板：先问要生成哪一类内容（用户不知道有哪些模型时从这里开始）。"""
    options: list[dict] = []
    for mtype in MODEL_TYPES:
        group = [m for m in models
                 if m["model_type"] == mtype
                 and m["status"] == CALLABLE_MODEL_STATUS and m["code"]]
        if not group:
            continue
        options.append({
            "value": mtype,
            "label": TYPE_LABELS.get(mtype, mtype),
            "count": len(group),
            "eta": _eta_text(mtype),
            "note": TYPE_MODE_HINTS.get(mtype, ""),
        })
    payload: dict = {
        "panel": "type_picker",
        "step": 1,
        "title": "想生成哪一类内容？",
        "options": options,
        "next_step": "选完类型后，面板会列出该类型的全部可用模型（含编码与计价）",
    }
    if not options:
        # 一个可调用模型都没有时，面板不能只留一个空标题：那看起来像"脚本坏了"，
        # 会诱使 agent 凭记忆编出模型名。必须如实说明是平台侧暂无可用模型。
        payload["empty_note"] = "平台当前没有可调用的模型（以实时清单为准，稍后重试）"
    return payload


def _print_wrapped_values(lead: str, items: list[str], *, sep: str = " / ",
                          limit: int = 80) -> None:
    """打印「一行放不下」的取值列表，续行缩进到取值起始列。

    排版纪律与本模块其它输出一致：每行不超过 `limit` 列（中文按 2 列计）。
    实测图像模型的 `aspect_ratio` 有 14 个取值，`取值（标签）` 形态单行可达 280 列——
    终端折行会把取值与标签拆散，用户/agent 反而更容易抄错（这正是本版要修的缺陷的
    成因之一）。单个取值本身就超长时不强拆，照样打印。
    """
    indent = " " * _display_width(lead)
    line = lead
    for index, item in enumerate(items):
        piece = item if index == 0 else sep + item
        if index and _display_width(line) + _display_width(piece) > limit:
            print(line)
            line = indent + item
        else:
            line += piece
    if line.strip():
        print(line)


def _format_option(option: dict) -> str:
    """一个选项的展示文本：value（label），二者相同时只给 value。

    **用户要传的是 value，label 只是展示标签**——传 label 会被平台判为非法
    取值（实测：比例传 16:9 而契约取值是 1280x720，必然 400）。
    """
    value = str(option.get("value") or "")
    label = str(option.get("label") or "")
    return f"{value}（{label}）" if label and label != value else value


def _option_group_head(group: dict) -> str:
    """一组条件选项的档位名（imageSize=1K / mode 已填 / 无条件）。"""
    when = group.get("when")
    if not isinstance(when, dict):
        return "无条件"
    param = str(when.get("param") or "").strip() or "?"
    operator = str(when.get("operator") or "eq").strip().lower()
    if operator == "filled":
        return f"{param} 已填"
    if operator == "empty":
        return f"{param} 未填"
    value = when.get("value")
    if operator == "ne":
        return f"{param}!={value}"
    return f"{param}={value}"


def _render_choice_options(field: dict, mark: str) -> None:
    """渲染一个 choice 字段的可选值：有条件就**按档位分组**，无条件才平铺。

    分组的理由（生产实证）：`gpt-image-2-stable.aspect_ratio` 有 40 个取值，
    每个值只属于 1K/2K/4K 三档中的一档；平铺出来用户根本看不出该先选分辨率。
    这里把「控制器取值 → 一组选项」显式摆出来，并标注**当前命中**的那一组。
    未命中任何一组时（控制器还没选）并不隐藏任何东西——仍然把每档都列出来，
    与后端「控制器缺席则放行」的语义一致，用户可以自由选某一档里的值。
    """
    groups = field.get("option_groups") or []
    if not groups:
        _print_wrapped_values(
            f"  {_pad(field['name'], 20)} {mark}  可选：",
            [_format_option(o) for o in field.get("options") or []])
        return
    dep = field.get("depends_on")
    print(f"  {_pad(field['name'], 20)} {mark}  可选（按 {dep} 分档）：")
    active = field.get("active_group")
    for index, group in enumerate(groups):
        head = _option_group_head(group)
        flag = "（当前命中）" if index == active else ""
        _print_wrapped_values(
            f"      [{head}]{flag} ",
            [_format_option(o) for o in group.get("options") or []])


def _render_form(payload: dict) -> None:
    """`form` 的人类可读渲染（agent 应优先用 `--json`，这里便于人工核对）。

    面板里几乎每个字符串都来自上游（模型名、参数名、取值标签）。
    这里统一过一遍与 `--json` **同一个**脱敏入口，而不是逐个字段调 `_safe()`：
    后者漏一处就是一个泄漏点，而人读输出与 `--json` 必须口径一致——
    曾实测同一份数据 `--json` 已脱敏、人读路径却把内部地址原样打印。
    """
    payload = _safe_obj(payload)  # type: ignore[assignment]
    print(payload.get("title") or "生成参数面板")
    print(_rule())
    if payload.get("panel") == "type_picker":
        for opt in payload["options"]:
            print(f"  {_pad(opt['label'], 6)} {opt['count']:>3} 个模型　典型耗时 {opt['eta']}")
    elif payload.get("panel") == "model_picker":
        print(f"类型：{payload['type_label']}　典型耗时：{payload['eta']}")
        print(f"计价：{payload['billing_legend']}")
        for family in build_model_families(payload["options"]):
            if len(family["lines"]) > 1:
                print(f"  ▸ {_safe(family['label'])}　同名 {len(family['lines'])} 个"
                      f"（编码不同，按编码选）")
            for opt in family["lines"]:
                print(f"  {_pad(opt['value'], 26)}  {_pad(opt['label'], 18)}  "
                      f"{opt['billing']}　参数 {opt['params_count']} 个")
                print(f"      近似消耗：{_safe(opt['credit_estimate'])}")
        if payload.get("duplicate_note"):
            print()
            print(f"  注意：{payload['duplicate_note']}")
        if payload.get("unavailable"):
            print()
            print("  不可调用：")
            for item in payload["unavailable"]:
                code_text = item.get("model_code") or "-"
                print(f"    {item['display_name']}（{code_text}）—— {item['reason']}")
    else:
        print(f"模型：{payload['display_name']}（{payload['model_code']}）")
        print(f"计费：{payload['billing']['text']}　典型耗时：{payload['eta']}"
              f"　换算：{payload['credit_rule']}")
        if payload.get("preset_note"):
            print()
            print(f"  已自动采用：{payload['preset_note']}")
        for field in payload["fields"]:
            mark = "必填" if field["required"] else "可选"
            if field["kind"] == "fixed":
                values = f"固定值 {field['options'][0]['value']}（不可选）"
            elif field["kind"] == "choice":
                # **用户要传的是 `value`，`label` 只是展示标签**——传 label 会被平台判为
                # 非法取值（实测：比例传 `16:9` 而契约取值是 `1280x720`，必然 400）。
                # 两者不同时按 `value（label）` 一并给出，与 `params` / `model` 同一口径。
                values = None
            elif field["kind"] == "number" and field["range"]:
                values = f"数值 {_fmt_num(field['range'][0])}~{_fmt_num(field['range'][1])}"
            elif field["kind"] == "list":
                values = "多个值（URL 或本地路径）"
            else:
                values = "自由填写"
            if values is None:
                _render_choice_options(field, mark)
            else:
                print(f"  {_pad(field['name'], 20)} {mark}  {values}")
            label = str(field.get("label") or "")
            detail = field["note"]
            if label and label != field["name"]:
                detail = f"{label}　{detail}" if detail else label
            if detail:
                print(f"    {detail}")
            dep = field.get("depends_on")
            if dep and field.get("active_group") is None:
                # 未选控制器时不隐藏任何一档（与后端放行语义一致），
                # 但要告诉用户「先选它能收窄」——否则 40 项平铺的体感问题没解决。
                print(f"    → 先确定「{dep}」可收窄可选值；不选也可直接挑上面任一项")
            if field.get("visible_note"):
                print(f"    （{field['visible_note']}）")
    if payload.get("credit_estimate_text"):
        print()
        print(f"  {payload['credit_estimate_text']}")
    if payload.get("empty_note"):
        # `empty_note` 是"没有可选值"时唯一的信息来源（例如该类型无可用模型、
        # 或该模型没有参数契约条目）。人读路径若吞掉它，用户只会看到一个空面板，
        # agent 也会退回去凭记忆编参数——所以三种面板都必须把它打出来。
        print()
        print(f"  说明：{payload['empty_note']}")
    print()
    print(payload.get("next_step") or payload.get("confirm_hint") or "")


def cmd_form(args) -> int:
    """生成前的问答板数据：类型 → 模型 → 参数，三步都只依赖对外参数契约。

    带 `--code` + `--param` 时，面板会额外给出**本次预计消耗积分**——
    用户点确认之前必须看到价钱（`credit_estimate_text`）。
    """
    models = _fetch_public_models()
    if args.code:
        model = _select_by_code(models, args.code)
        if not model:
            raise NoovaError(f"未找到模型「{args.code}」", hint="运行 models 查看当前可用清单")
        if model["status"] != CALLABLE_MODEL_STATUS:
            raise NoovaError(
                f"模型「{args.code}」当前状态为「{_status_label(model['status'])}」，不能调用",
                hint="运行 models --online 查看当前可用的模型",
            )
        chosen = _parse_param_pairs(args.param)
        payload = build_param_form(model, chosen_params=chosen)
    elif args.type:
        payload = build_model_form(args.type, models)
    else:
        payload = build_type_form(models)

    if args.json:
        print(_dump_json(payload))
        return 0
    _render_form(payload)
    return 0


def _preset_params(model: dict | None) -> dict:
    """契约里**只有唯一取值**的参数：不进面板，自动采用并如实带入请求载荷。

    为什么必须带入：生产 4 个图像模型的 `aspect_ratio` 选项条件依赖的正是单值的
    `imageSize`（§计划 §1.2）。不带入 → 选项可见集为空 → 面板拿不到任何比例。
    模型编码本身不算：它由 `--model` 决定，契约里的 `model` 参数只是回显。
    """
    preset: dict = {}
    code = str((model or {}).get("code") or "").strip().lower()
    for param in (model or {}).get("params") or []:
        name = str(param.get("name") or "").strip()
        if not name or name.lower() in {code, "model"}:
            continue
        values = [str(v).strip() for v in (param.get("values") or []) if str(v).strip()]
        if len(values) == 1:
            preset[name] = values[0]
    return preset


def _build_media_payload(args, key: str, model: dict | None = None) -> dict:
    """构造媒体生成的请求载荷。

    参考文件的字段名**从运行时参数契约推导**（`_reference_field_map`），不再写死
    `referenceImages`：契约声明的是别的字段名时，写死会让载荷带着一个平台不认识的
    字段发出去，被**静默忽略**——用户传了参考图却完全没有生效（机制已复现）。

    单值参数（契约里只有唯一取值）在此**自动带入**：它们不进选择面板，但必须
    出现在请求里，否则平台会按缺参处理（生产 7 个单值参数里 5 个是必填）。
    """
    payload: dict = {"prompt": args.prompt}
    for name, value in _preset_params(model).items():
        payload[name] = value
    for name, value in _parse_param_pairs(args.param).items():
        payload[name] = value

    fields = _reference_field_map(model, quiet=True)
    for option, default_field in (("ref_image", "referenceImages"),
                                  ("ref_video", "referenceVideos"),
                                  ("ref_audio", "referenceAudios")):
        values = getattr(args, option, None)
        if not values:
            continue
        field = fields.get(option, default_field)
        if option not in fields:
            # 只有用户**确实传了**这类参考文件、而契约里却没有对应字段时才提示；
            # 无条件提示会污染 chat 等根本用不到该选项的路径（实测）。
            print(f"[提示] 模型「{(model or {}).get('code', '')}」的参数契约里没有与 "
                  f"{default_field} 对应的字段；如参考文件未生效，请用 "
                  f"--param <字段名>=<URL> 直接指定", file=sys.stderr)
        resolved = [_resolve_reference(value, key) for value in values]
        if resolved:
            payload[field] = resolved
    return payload


# 参考文件「选项 → 契约字段名」的候选写法（大小写不敏感）。
_REFERENCE_FIELD_CANDIDATES = {
    "ref_image": ("referenceimages", "reference_images", "images", "image", "img"),
    "ref_video": ("referencevideos", "reference_videos", "videos", "video"),
    "ref_audio": ("referenceaudios", "reference_audios", "audios", "audio"),
}


def _reference_field_map(model: dict | None, *, quiet: bool = False) -> dict:
    """从模型的运行时参数契约推导「本工具选项 → 平台字段名」。

    优先精确匹配惯用名（`referenceImages` 等）；契约里没有该名字时，按候选写法
    找一个**确实存在于契约**的字段名。一个都找不到就沿用惯用名，
    并在 stderr 给出提示（让「契约变了」这件事可见，而不是静默失效）。

    `quiet=True` 用于「只想拿映射、不想让提示混进输出」的调用方
    （`form` 面板要在 `--json` 下产出纯 JSON，提示属于噪音）。
    """
    if not model:
        return {}
    names = [str(p.get("name") or "").strip() for p in (model.get("params") or [])]
    lowered = {name.lower(): name for name in names if name}
    if not lowered:
        return {}

    mapping: dict = {}
    for option, candidates in _REFERENCE_FIELD_CANDIDATES.items():
        hit = next((lowered[c] for c in candidates if c in lowered), None)
        if hit:
            mapping[option] = hit
        elif not quiet:
            default = {"ref_image": "referenceImages", "ref_video": "referenceVideos",
                       "ref_audio": "referenceAudios"}[option]
            if default.lower() not in lowered:
                print(f"[提示] 模型「{model.get('code', '')}」的参数契约里没有与 "
                      f"{default} 对应的字段；如参考文件未生效，请用 "
                      f"--param <字段名>=<URL> 直接指定", file=sys.stderr)
    return mapping


def _resolve_reference(value: str, key: str) -> str:
    """参考文件既可直接给 URL，也可给本地路径（本地路径自动上传后引用）。"""
    text = str(value or "").strip()
    if not text:
        return text
    if text.startswith(("http://", "https://")):
        return text
    path = Path(text).expanduser()
    if not path.is_file():
        raise NoovaError(f"参考文件不存在：{text}",
                         hint="请传入本地文件路径或可公开访问的 URL")
    result = upload_local_file(path, key)
    # 用中性标签而不是内部通道 id（`HOST_LABELS` 就是为此存在的）。
    label = UPLOAD_HOST_LABELS.get(result["provider"], result["provider"])
    print(f"[已上传参考文件] {path.name} → 生成请求将引用该地址（通道：{label}）",
          file=sys.stderr)
    return result["url"]


def _parse_param_pairs(pairs) -> dict:
    """把 `--param 名=值`（可重复）收成 dict；重复给同名时后者覆盖前者。"""
    out: dict = {}
    for raw in (pairs or []):
        if isinstance(raw, (tuple, list)) and len(raw) == 2:
            name, value = raw
        else:
            text = str(raw)
            if "=" not in text:
                raise NoovaError("参数格式应为 名=值", hint="例：--param duration=5")
            name, value = text.split("=", 1)
        name = str(name).strip()
        if not name:
            raise NoovaError("参数名不能为空", hint="例：--param duration=5")
        out[name] = _coerce_param_value(value)
    return out


def _coerce_param_value(value: str):
    """命令行参数值统一为字符串，这里的转换只覆盖明确的字面量，避免误判。"""
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    if re.fullmatch(r"-?\d+\.\d+", value.strip()):
        return float(value.strip())
    return value


def _require_prompt(args) -> None:
    """`--prompt ""` 是「没给提示词」，不是「给了空提示词」。

    曾经空串会被后续的必填校验当成「未提供」，于是报错信息说 `--param prompt=值`
    ——那条命令并不存在（本工具的选项是 `--prompt`）。这里在入口处一次说清。
    """
    prompt = getattr(args, "prompt", None)
    if prompt is None or not str(prompt).strip():
        raise NoovaError("提示词不能为空（--prompt）",
                         hint='示例：--prompt "一只戴墨镜的猫"')


def _ensure_required_params(model: dict, args, *, kind: str = "media") -> None:
    """发请求前核对「必填参数」是否已提供。

    依据是对外参数契约里的 `required` 标记，不硬编码任何模型。
    实测：部分媒体模型会把 `referenceImages` 标为**必填**，
    缺了它发请求会白等一轮（耗时、可能计费）才拿到上游 400。这里提前拦下并说清缺什么。
    """
    params = model.get("params") or []
    if not params:
        return  # 无参数契约：不做臆测式校验，交给平台判定
    explicit = {name for name, _ in getattr(args, "param", None) or []}

    # 本工具选项 → 平台字段名。参考字段名从契约推导（与 `_build_media_payload` 同源），
    # 否则契约声明 `images` 时这里会误报「缺 referenceImages」。
    # `quiet=True`：这里只拿映射做必填校验，不要产生参考字段提示（那会污染 chat 路径）。
    fields = _reference_field_map(model, quiet=True)
    by_option = {
        fields.get("ref_image", "referenceImages"): bool(getattr(args, "ref_image", None)),
        fields.get("ref_video", "referenceVideos"): bool(getattr(args, "ref_video", None)),
        fields.get("ref_audio", "referenceAudios"): bool(getattr(args, "ref_audio", None)),
    }
    if kind == "chat":
        # 文本路径的图片输入走 `--image`，字段名由协议报文决定，这里按契约推导。
        for candidate in ("images", "image", "referenceImages"):
            if candidate in {p["name"] for p in params}:
                by_option[candidate] = bool(getattr(args, "image", None))
                break

    auto_provided = {"model"}                      # 脚本按 --model 自动写入
    if str(getattr(args, "prompt", None) or "").strip():
        auto_provided.add("prompt")                # --prompt 为命令行必填项
    # 单值参数由 `_build_media_payload` 自动写入请求体（契约里只有唯一取值，
    # 用户无从选择），因此必须与 `model` / `prompt` 同等看待。少这条就会出现
    # 「面板里没有这个参数、请求前却报缺它」——生产 7 个单值参数里 5 个是必填
    # （`Seedance-2.0-mini-720p` 等 4 个视频模型的 `resolution`）。
    auto_provided.update(_preset_params(model))

    missing = [p["name"] for p in params
               if p["required"]
               and p["name"] not in auto_provided
               and p["name"] not in explicit
               and not by_option.get(p["name"], False)]
    if not missing:
        return

    flags = {
        "referenceImages": "--ref-image URL_OR_PATH",
        "referenceVideos": "--ref-video URL_OR_PATH",
        "referenceAudios": "--ref-audio URL_OR_PATH",
    }
    for option, field in fields.items():
        if field in missing:
            flags[field] = {"ref_image": "--ref-image URL_OR_PATH",
                            "ref_video": "--ref-video URL_OR_PATH",
                            "ref_audio": "--ref-audio URL_OR_PATH"}[option]
    if kind == "chat":
        for candidate in ("images", "image"):
            if candidate in missing:
                flags[candidate] = "--image URL_OR_PATH"
    raise NoovaError(
        f"模型「{model['code']}」有必填参数未提供：{'、'.join(missing)}",
        hint="参数是否必填以运行时参数契约为准：params --code <模型编码> --json",
        details=[f"{name} —— 用 {flags.get(name, f'--param {name}=值')} 提供" for name in missing],
    )


def _normalize_option_key(value: object) -> str | None:
    """把取值归一化成可比较的 key（与平台侧校验同一口径）。

    平台比较取值时会做等价的归一化（布尔转 `true`/`false`、数值去掉无意义的
    `.0` 与尾零），所以这里的预检必须同样处理——否则 `--param duration=5.0`
    会被本地误判为非法，而平台其实接受。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        number = float(value)
        if number.is_integer():
            return str(int(number))
        text = f"{number}"
        return text if "e" in text.lower() else text.rstrip("0").rstrip(".")
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    rendered = f"{number}"
    return rendered if "e" in rendered.lower() else rendered.rstrip("0").rstrip(".")


def _range_problem(param: dict, name: str, value) -> str | None:
    """数值型参数的 `min`/`max` 预检；越界返回人读说明，否则 None。

    为什么必须拦：面板报价已经按用户给的数算出金额（`duration=999` → 24975 积分），
    而平台会因为越界直接 400 拒单。不拦就是「先报一个不可能成交的价」。
    判据只有契约 `min`/`max`，缺的边不设限。平台服务端用的是同一组边界值，
    但它会先做参数名归一与类型归一，本函数只按契约里的 `name` 精确匹配 ——
    这里**只是本地预检**，
    真正的裁决仍以平台返回为准，故判不准的一律放行。
    """
    if str(param.get("type") or "").strip().lower().replace(" ", "") not in _NUMERIC_PARAM_TYPES:
        return None
    low, high = _finite_number(param.get("min")), _finite_number(param.get("max"))
    if low is None and high is None:
        return None
    if value is None:
        return None                      # 非数值（含列表）交给后端判，不在这里猜
    if low is not None and value < low:
        return f"{name} 不能小于 {_fmt_num(low)}（当前 {_fmt_num(value)}）"
    if high is not None and value > high:
        return f"{name} 不能大于 {_fmt_num(high)}（当前 {_fmt_num(value)}）"
    return None


def _ensure_param_values(model: dict, args) -> None:
    """发请求前核对「参数取值」是否满足契约：枚举白名单 + 数值范围。

    为什么必须在本地拦：契约里的 `values` 才是合法取值，而**展示标签**（`valueLabels`，
    如像素串 `1280x720` 的标签 `16:9`）长得更像人话，极易被当成可传值——实测图像模型的
    `aspect_ratio` 传标签必然拿到 HTTP 400，白跑一次往返。数值越界同理：面板已经
    按用户给的数报了价，平台却会拒单，等于先报一个不可能成交的价。

    保守放行原则（宁可少拦，不可误拦）：
      - 参数不在契约里 → 不管（平台对未知键是**静默忽略**，文档已声明，不阻断）；
      - 契约 `values` 为空   → 该参数是自由文本，不校验枚举；
      - 数组型参数          → 元素逐个比（`--ref-image` 那类走专门选项，一般不在此路径）；
      - 数值交给 `--param` 时可能仍是字符串 → 先在本地转成数值再比，转不动就放行。
    """
    params = model.get("params") or []
    if not params:
        return
    by_name = {p.get("name"): p for p in params}
    problems: list[tuple[str, list[str], list[str]]] = []
    range_problems: list[str] = []
    for name, value in _parse_param_pairs(getattr(args, "param", None)).items():
        param = by_name.get(name)
        if not param:
            continue
        problem = _range_problem(param, name, _finite_number(value))
        if problem and problem not in range_problems:
            range_problems.append(problem)
        allowed = [str(v).strip() for v in (param.get("values") or []) if str(v).strip()]
        if not allowed:
            continue  # 自由文本参数：无枚举可校验
        allowed_keys = {_normalize_option_key(v) for v in allowed}
        given = value if isinstance(value, list) else [value]
        if all(_normalize_option_key(item) in allowed_keys for item in given):
            continue
        labels = param.get("value_labels") if isinstance(param.get("value_labels"), dict) else {}
        # 展示一律用 `取值（标签）`：用户真正要传的是取值，标签只帮他对上话。
        shown = [f"{v}（{labels[v]}）" if labels.get(v) else v for v in allowed]
        problems.append((name, given, shown))

    if not problems:
        if range_problems:
            # 越界是**确定性失败**：重试不会变好，只能改数值。文案只说清边界与当前值。
            raise NoovaError(
                "参数取值超出契约范围：" + "；".join(range_problems),
                hint="请按上面给出的上下限重新取值（本类错误重试无效）",
                details=[f"查看契约：params --code {model.get('code', '<模型编码>')} --json"],
            )
        return

    # 命中「用户把标签当取值传了」时给一条针对性的提示——这是最高频的误用。
    misused: list[str] = []
    for name, given, _ in problems:
        param = by_name.get(name) or {}
        labels = param.get("value_labels") if isinstance(param.get("value_labels"), dict) else {}
        for item in given:
            for value, label in labels.items():
                if label and _normalize_option_key(label) == _normalize_option_key(item):
                    misused.append(f"{name}={item} → 应传 {value}")
    hint = ("取值必须在契约的 values 里，valueLabels 只是展示标签、不能当取值传："
            "form --code <模型编码> --json")
    if misused:
        hint = "你把展示标签当成取值传了——" + "、".join(misused) + "。以契约 values 为准。"

    raise NoovaError(
        "参数取值不合法："
        + "；".join(
            f"{name} 当前值 {_fmt_value_for_error(given)}，可选值 {'、'.join(shown)}"
            for name, given, shown in problems),
        hint=hint,
        details=["参数取值一律以该模型的运行时参数契约为准",
                 f"查看契约：params --code {model.get('code', '<模型编码>')} --json"],
    )


def _fmt_value_for_error(values: list) -> str:
    """把非法取值渲染成人读形态（单值不加括号，多值逗号分隔）。"""
    texts = [str(v) for v in values]
    return texts[0] if len(texts) == 1 else "[" + ", ".join(texts) + "]"


def _run_media(args, model_type: str) -> int:
    key = _require_key()
    model = _resolve_model(args.model, model_type, quiet=getattr(args, "quiet", False))
    if model["status"] and model["status"] != CALLABLE_MODEL_STATUS:
        raise NoovaError(f"模型「{model['code']}」当前状态为「{_status_label(model['status'])}」，暂不可调用")
    _ensure_required_params(model, args)
    _ensure_param_values(model, args)
    payload = _build_media_payload(args, key, model)
    # 生成前取一次余额快照：终态后再取一次，两者差值即「本次扣减」（真实、可核对）。
    credit_before = None if args.no_credit else fetch_credit(key)
    return _create_task(model, payload, args, key, credit_before=credit_before)


def cmd_image(args) -> int:
    return _run_media(args, "image")


def cmd_video(args) -> int:
    return _run_media(args, "video")


def cmd_audio(args) -> int:
    return _run_media(args, "audio")


def _openai_user_content(prompt: str, images: list[str]) -> object:
    """OpenAI 消息 content：无图时为纯字符串，有图时为分段数组。"""
    if not images:
        return prompt
    return [{"type": "text", "text": prompt},
            *({"type": "image_url", "image_url": {"url": url}} for url in images)]


def _anthropic_user_content(prompt: str, images: list[str]) -> object:
    """Anthropic 消息 content：图片按官方 image block 传（原生透传，不做跨协议转换）。"""
    if not images:
        return prompt
    return [*( {"type": "image", "source": {"type": "url", "url": url}} for url in images),
            {"type": "text", "text": prompt}]


def build_chat_body(protocol: str, model_code: str, args, images: list[str]) -> dict:
    """按目标协议的**原生格式**构造文本请求体。

    原生路由是透传语义（请求按协议官方格式直达上游），因此必须构造该协议的合法报文：
    - OpenAI：`{model, messages[{role, content}]}`
    - Anthropic：`{model, max_tokens, system?, messages[{role, content}]}`（`max_tokens` 必填）
    """
    if protocol == "anthropic":
        body: dict = {
            "model": model_code,
            "messages": [{"role": "user", "content": _anthropic_user_content(args.prompt, images)}],
            "max_tokens": int(args.max_tokens or 1024),
        }
        if args.system:
            body["system"] = args.system
        if args.temperature is not None:
            body["temperature"] = float(args.temperature)
        if args.stream:
            body["stream"] = True
        return body

    if protocol == "responses":
        if images:
            raise NoovaError("Responses 协议路由暂不支持图片输入",
                             hint="可改用该模型的其它协议路由，或去掉 --image")
        body = {"model": model_code, "input": args.prompt}
        if args.system:
            body["instructions"] = args.system
        if args.max_tokens:
            body["max_output_tokens"] = int(args.max_tokens)
        if args.temperature is not None:
            body["temperature"] = float(args.temperature)
        if args.stream:
            body["stream"] = True
        return body

    messages: list[dict] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": _openai_user_content(args.prompt, images)})
    body = {"model": model_code, "messages": messages}
    if args.max_tokens:
        body["max_tokens"] = int(args.max_tokens)
    if args.temperature is not None:
        body["temperature"] = float(args.temperature)
    if args.stream:
        body["stream"] = True
    return body


def _protocol_headers(protocol: str) -> dict:
    """按协议官方规范补的必要请求头（鉴权统一走 Authorization，不在此重复携带密钥）。"""
    if protocol == "anthropic":
        return {"anthropic-version": "2023-06-01"}
    return {}


def _report_text_usage(payload: object, model: dict, args, key: str,
                       credit_before: dict | None) -> None:
    """文本调用的「用量 / 计价 / 本次扣减 / 剩余积分」反馈。

    仅在 `--show-usage` 打开时输出，且**一律走 stderr**——保证正文（stdout）
    可直接被管道与程序消费；余额快照取不到时只省略对应行，不报错。
    """
    if not getattr(args, "show_usage", False):
        return
    lines = [f"模型：{model['display_name']}（{model['code']}）",
             f"计价：{_price_text(model)}"]
    usage = extract_usage(payload)
    if usage:
        lines.append(f"用量：输入 {usage.get('prompt_tokens', 0)} tokens / "
                     f"输出 {usage.get('completion_tokens', 0)} tokens")
    if credit_before is not None:
        lines += _credit_lines(credit_before, fetch_credit(key))
    _render_info_block(lines, out=sys.stderr)


def cmd_chat(args) -> int:
    """文本生成：按模型协议走官方原生路由（不使用统一入口）。"""
    key = _require_key()
    _require_prompt(args)
    model = _resolve_model(args.model, "text", quiet=getattr(args, "quiet", False))
    if model["status"] and model["status"] != CALLABLE_MODEL_STATUS:
        raise NoovaError(f"模型「{model['code']}」当前状态为「{_status_label(model['status'])}」，暂不可调用")
    # 文本路径同样做「发请求前核对必填参数」：媒体路径一直有，文本路径曾经没有，
    # 于是缺 `images` 之类必填项的请求会白等一轮才拿到上游 400。
    _ensure_required_params(model, args, kind="chat")
    # 枚举取值同样在本地按契约预检（与媒体路径同源）：传展示标签必被平台 400 拒绝。
    _ensure_param_values(model, args)

    protocol, path = select_chat_protocol(model, args.protocol)
    images = [_resolve_reference(value, key) for value in getattr(args, "image", None) or []]
    body = build_chat_body(protocol, model["code"], args, images)
    # 单值参数自动带入（与媒体路径同一口径）：它们不进参数面板，但仍是模型参数,
    # 漏传同样会被平台判为缺参。用户显式 `--param` 优先，可覆盖预设。
    for name, value in _preset_params(model).items():
        body[name] = value
    for name, value in _parse_param_pairs(args.param).items():
        body[name] = value

    if not getattr(args, "quiet", False):
        print(f"[路由] {PROTOCOL_LABELS.get(protocol, protocol)} 协议 → POST {path}"
              f"（模型 {model['code']} · 计价 {_price_text(model)} · "
              f"典型耗时 {_eta_text('text')}）", file=sys.stderr)
        _announce_cost(model, body, quiet=getattr(args, "quiet", False))

    credit_before = (fetch_credit(key)
                     if args.show_usage and not getattr(args, "no_credit", False) else None)
    headers = _protocol_headers(protocol)
    if args.stream:
        return _stream_output(path, body, key, headers, args, model=model,
                              credit_before=credit_before)
    # `retry=False`：文本生成同样是**计费**请求，自动重试有重复扣费风险。
    payload = _request("POST", path, body=body, key=key, timeout=args.timeout,
                       retry=False, extra_headers=headers)
    # 先交付正文，再补用量与积分（顺序反过来会让人以为正文在"附录"之后）。
    status = _print_text_result(payload, model, args)
    _report_text_usage(payload, model, args, key, credit_before)
    return status


def _stream_output(path: str, body: dict, key: str, headers: dict, args,
                   *, model: dict | None = None, credit_before: dict | None = None) -> int:
    """流式输出：逐块打印增量文本；--json 时输出每条 SSE 事件。"""
    collected: list[str] = []
    usage: dict = {}
    try:
        resp = _open(path, key=key, body=body, method="POST", timeout=int(args.timeout or 120),
                     accept="text/event-stream", extra_headers=headers)
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            pass
        payload = _decode(raw)
        raise _friendly_error(int(exc.code), _extract_error_message(payload), payload, None) from exc
    except urllib.error.URLError as exc:
        raise NoovaError(f"网络连接失败：{_safe(exc.reason)}") from exc

    with resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(chunk, dict):
                chunk_usage = extract_usage(chunk)
                if chunk_usage:
                    usage = chunk_usage
            delta = extract_stream_delta(chunk)
            if not delta:
                continue
            if args.json:
                # 逐个事件输出**已脱敏**的 JSON 行（agent 按行解析）。
                # 直接打印原始 `data` 会把上游的域名原样透出（同 H1 的漏点）。
                print(json.dumps(_safe_obj(chunk), ensure_ascii=False))
            else:
                print(delta, end="", flush=True)
            collected.append(delta)
    if not args.json:
        print()
        text = "".join(collected)
        if model is not None:
            _report_text_usage({"usage": usage} if usage else {}, model, args, key, credit_before)
        if not text:
            raise NoovaError("流式响应结束但未收到任何文本内容",
                             hint="该模型可能不支持流式（stream=true），可去掉 --stream 重试")
    return 0


def _print_text_result(payload: object, model: dict, args) -> int:
    if args.json:
        print(_dump_json(payload))
        return 0
    text = extract_text(payload)
    if not text.strip():
        # 没有正文 = 没有产出。曾经返回 0，agent 便以为拿到了结果。
        print("未从响应中解析出文本内容。", file=sys.stderr)
        print("原始响应（已脱敏）：", file=sys.stderr)
        print(_dump_json(payload), file=sys.stderr)
        return 1
    print(text)
    return 0


def cmd_task(args) -> int:
    """查询一次任务状态（不轮询）。失败原因由平台放在响应体 `error` 字段。

    退出码语义（与人类模式的文字**必须一致**，否则 agent 读码、用户读字，两边结论相反）：

    | 情况 | 退出码 |
    |---|---|
    | 仍在处理中 | 0（查询本身成功了） |
    | 已完成且有结果 | 0 |
    | 已完成但**无结果地址** | 1（没有产出，不算成功） |
    | 已终止且失败 / 取消 / 过期 | 1 |

    曾经的实现把**失败**任务打印成「任务仍在处理中」并返回 0（已实测），
    自相矛盾之外还指引 agent 无限 `wait`——而 `protocols.md` 自己就写着
    「判断任务是否失败的唯一依据是终态 status」。
    """
    key = _require_key()
    payload = _request("POST", TASK_POLL_PATH, body={"id": args.id}, key=key,
                       timeout=max(int(getattr(args, "timeout", 30) or 30), 1))
    payload = _unwrap(payload)
    status = _status_of(payload)
    urls = collect_result_urls(payload)
    failed = status in TERMINAL_FAIL
    succeeded = status in TERMINAL_OK
    error_text = ""
    if isinstance(payload, dict) and payload.get("error"):
        error_text = _safe(payload["error"])

    if args.json:
        print(_dump_json(payload))
        return 1 if (failed or (succeeded and not urls)) else 0

    print(f"任务 {args.id}：{_status_label(status)}（{status or 'unknown'}）")
    if isinstance(payload, dict) and payload.get("progress") is not None:
        print(f"进度：{payload['progress']}%")
    if error_text:
        print(f"失败原因：{error_text}")
    if urls:
        print(f"结果地址（{len(urls)} 个，请原样交付）：")
        for index, url in enumerate(urls, 1):
            print(f"  {index}. {url}")
        if not failed:
            return 0

    if failed:
        print("       该任务已终止且未成功，**不要继续等待**。", file=sys.stderr)
        print(f"       如需重试，请重新发起一次生成（任务 ID：{args.id}）。", file=sys.stderr)
        return 1
    if succeeded:
        print("       任务状态为完成，但响应中没有结果地址；请把任务 ID 反馈给官方支持。",
              file=sys.stderr)
        return 1

    hint = (f"通常耗时 {_eta_text(args.type)}" if args.type
            else "未指定 --type，无法给出耗时预期（可加 --type image|video|audio）")
    print(f"提示：任务仍在处理中（{hint}），"
          f"可继续等待：{SELF_CMD} wait --id {args.id}")
    return 0


def cmd_wait(args) -> int:
    key = _require_key()
    model_type = getattr(args, "type", "") or ""
    payload = _poll_until_done(args.id, key, args, model_type)
    # **不报「本次扣减」**：任务是在上一次调用里创建的，扣费发生在那一刻，
    # 所以这里取到的「生成前快照」其实已经是扣费之后的值，差值恒为 0——
    # 那等于告诉用户「付费生成是免费的」。只报当前余额，不编造差额。
    return _print_task_result(payload, model_type, as_json=args.json, quiet=args.quiet,
                              credit_before=None,
                              credit_after=None if args.no_credit else fetch_credit(key))


def cmd_credit(args) -> int:
    key = _require_key()
    # 只发**一次**请求：曾经先 `_request` 再 `fetch_credit`（后者又发一次 GET），
    # 白白吃掉两次 40/min 的限流额度，还可能前后自相矛盾。
    snapshot = fetch_credit(key)
    if args.json:
        if snapshot is None:
            payload = _request("GET", CREDIT_PATH, key=key, timeout=30)
            print(_dump_json(payload))
            return 0
        print(_dump_json({
            "remainingTotal": snapshot["total"],
            "remainingPermanent": snapshot.get("permanent"),
            "remainingLimited": snapshot.get("limited"),
            "remainingVip": snapshot.get("vip"),
        }))
        return 0
    if snapshot is None:
        raise NoovaError("暂时无法查询积分余额", hint="请稍后重试，或检查网络")
    print(f"剩余积分：{_credit_amount(snapshot['total'])}")
    for field, label in (("permanent", "永久积分"), ("limited", "限时积分"), ("vip", "会员积分")):
        if snapshot.get(field) is not None:
            print(f"{label}：{_credit_amount(snapshot[field])}")
    print("提示：生成结束后脚本会自动显示本次扣减与剩余积分。")
    return 0


def upload_local_file(path: Path, key: str = "", *, content_type: str | None = None,
                      timeout: int = 180, host: str = "auto") -> dict:
    """上传本地文件，返回 `{"url","provider","bytes","content_type"}`。

    通道由 `noova_upload` 决定：平台存储通道。
    需要 API Key；未配置 Key 时拒绝上传并给出配置引导。
    """
    try:
        return upload_file(path, api_key=key or get_api_key(), content_type=content_type,
                           host=host, timeout=timeout, platform_uploader=_platform_upload)
    except UploadError as exc:
        details = [f"{UPLOAD_HOST_LABELS.get(a.get('host', ''), a.get('host', '?'))}："
                   f"{a.get('detail') or '-'}" for a in exc.attempts]
        raise NoovaError(exc.message, hint=exc.hint, details=details) from exc


def _platform_upload(path: Path, content_type: str, timeout: int) -> str:
    """平台存储通道：签发上传凭证 → 直传平台自有存储 → 返回可引用的文件地址。"""
    key = get_api_key()
    if not key:
        raise UploadError("未配置 API Key，无法使用平台存储通道",
                          hint=f"先完成 Key 配置：{KEY_CMD} setup --stdin")
    query = urllib.parse.urlencode({
        # 文件名必须转 ASCII 安全形态：中文名在部分存储/签名实现里会产生编码歧义，
        # 导致签名与实际请求不一致。第三方两个通道一直这么做，平台通道曾经漏了。
        "filename": safe_filename(path.name),
        "content_type": content_type,
        "size": path.stat().st_size,
    })
    credential = _request("GET", f"/v1/client/resource/sts?{query}", key=key, timeout=30)
    data = credential.get("data") if isinstance(credential, dict) else None
    if not isinstance(data, dict):
        raise UploadError("上传凭证签发响应异常")
    upload_url = str(data.get("upload_url") or "").strip()
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    method = str((data.get("upload") or {}).get("method") or "PUT").strip().upper()
    file_url = str(data.get("file_url") or "").strip()
    if not upload_url:
        raise UploadError("上传凭证缺少上传地址")

    # 上传地址来自**响应体**，是本模块唯一由上游数据决定请求目标的地方：不过地址
    # 守卫，一次被污染的响应就能让带签名的请求发往本机/内网/非 https 主机。判据与
    # 基础地址完全一致（`check_base_url`）。
    guard = check_base_url(upload_url)
    if not guard.get("ok"):
        raise UploadError("上传地址不可用（已拒绝非公开或非 https 的目标）",
                          hint=_safe(str(guard.get("reason") or "")))

    # 上传地址由平台签发，必须原样使用其附带请求头（含签名），额外只补客户端标识。
    upload_headers = dict(headers)
    upload_headers.setdefault("User-Agent", USER_AGENT)
    upload_headers.setdefault("Content-Type", content_type)
    request = urllib.request.Request(upload_url, data=path.read_bytes(),
                                     headers=upload_headers, method=method)
    try:
        # 这里必须用 `open_credentialed`（只允许同源跳转）而不是 `open_public`：
        # 请求头里带着平台签名，302 到任意公网主机会把签名一起带过去。
        # `urlopen` 对 >=400 直接抛 HTTPError，所以不需要再判 resp.status
        # （原先那句 `if resp.status >= 400` 是不可达代码）。
        with open_credentialed(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        raise UploadError(f"直传失败（HTTP {exc.code}）",
                          hint="若为大文件，可能是上传凭证已过期，请重试") from exc
    except urllib.error.URLError as exc:
        raise UploadError(f"直传失败：{_safe(exc.reason)}") from exc
    except OSError as exc:
        raise UploadError(f"直传失败：{type(exc).__name__}") from exc

    if not file_url:
        raise UploadError("上传完成但未返回文件访问地址")
    return file_url


def cmd_upload(args) -> int:
    """上传本地素材：走平台存储通道（需 API Key）。
    """
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise NoovaError(f"文件不存在或不是普通文件：{path}")
    if path.stat().st_size <= 0:
        raise NoovaError("文件内容为空，无法上传")

    key = get_api_key()  # 平台存储通道需要
    result = upload_local_file(path, key, content_type=args.content_type,
                               timeout=args.timeout, host=args.host)

    verified: bool | None = None
    verify_detail = ""
    if args.verify:
        verified, verify_detail = verify_url(result["url"], timeout=min(int(args.timeout), 30))
        if not verified:
            print(f"[提示] 上传完成，但地址探测未通过（{verify_detail}）；"
                  "可稍后重试或检查凭证是否过期。", file=sys.stderr)

    if args.json:
        print(_dump_json({
            "file_url": result["url"],
            "provider": result["provider"],
            "provider_label": UPLOAD_HOST_LABELS.get(result["provider"], result["provider"]),
            "content_type": result["content_type"],
            "size": result["bytes"],
            "verified": verified,
        }))
        return 0

    print(result["url"])
    label = UPLOAD_HOST_LABELS.get(result["provider"], result["provider"])
    print(f"[已上传] {path.name}（{result['bytes']} 字节，{result['content_type']}）；"
          f"通道：{label}；校验：{'通过' if verified else (verify_detail or '未校验')}",
          file=sys.stderr)
    print("[提示] 上传产物由平台存储托管；重要素材建议随用随传。", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _param_pair(raw: str) -> tuple:
    """argparse 的 `type=`：把 `名=值` 拆成二元组；不合格式当场拒绝。"""
    if "=" not in str(raw):
        raise argparse.ArgumentTypeError("格式应为 参数名=值")
    name, value = str(raw).split("=", 1)
    return (name.strip(), value)


def _add_param_option(parser) -> None:
    parser.add_argument("--param", action="append", type=_param_pair, default=[], metavar="NAME=VALUE",
                        help="模型参数（可重复），如 --param aspect_ratio=1:1；"
                             "参数名与取值请先用 params/model 子命令查询")


def _add_credit_option(parser) -> None:
    parser.add_argument("--no-credit", dest="no_credit", action="store_true",
                        help="不查询账户余额（生成前后各一次 GET /api/v1/credit）；"
                             "默认会查询，用于显示本次扣减与剩余积分")


def _add_wait_options(parser) -> None:
    parser.add_argument("--timeout", type=int, default=90, help="单次请求超时秒数（默认 90）")
    parser.add_argument("--max-wait", type=int, default=None,
                        help="最长等待秒数；省略时按类型给足（图像 600 / 音频 1200 / 视频 5400），"
                             "以免长任务被误判为超时")
    parser.add_argument("--poll-interval", type=int, default=6, help="轮询间隔秒数（默认 6）")
    parser.add_argument("--no-wait", action="store_true", help="只提交任务，不等待结果（返回任务 ID）")
    parser.add_argument("--quiet", action="store_true", help="只输出结果地址，不输出过程与计费信息")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noova_media.py",
        description="NooVa AI 公开 API 标准调用封装（只访问公开接口）",
        epilog="示例：\n"
               "  noova_media.py models --type video\n"
               "  noova_media.py params --code <模型编码> --json\n"
               "  noova_media.py image --prompt \"一只戴墨镜的猫\"\n"
               "  noova_media.py chat --prompt \"写一句标语\" --model <模型编码>\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PACKAGE_NAME} {CLIENT_VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_models = sub.add_parser("models", help="列出可用模型（运行时拉取）")
    p_models.add_argument("--type", choices=list(MODEL_TYPES), help="按类型过滤")
    p_models.add_argument("--online", dest="online_only", action="store_true", help="只看上线状态")
    p_models.add_argument("--json", action="store_true", help="输出 JSON")
    p_models.set_defaults(func=cmd_models)

    p_model = sub.add_parser("model", help="查看模型参数契约与调用方式")
    p_model.add_argument("--code", required=True, help="模型编码")
    p_model.add_argument("--json", action="store_true")
    p_model.set_defaults(func=cmd_model)

    p_params = sub.add_parser("params", help="查看模型结构化参数契约")
    p_params.add_argument("--code", required=True, help="模型编码")
    p_params.add_argument("--json", action="store_true", help="输出 JSON（推荐给 agent 消费）")
    p_params.set_defaults(func=cmd_params)

    p_form = sub.add_parser(
        "form",
        help="生成前的问答板数据（先选模型、再选参数；供 agent 渲染成问题面板）")
    p_form.add_argument("--type", choices=list(MODEL_TYPES),
                        help="该类型的模型选择面板；两个都不给则返回「选类型」面板")
    p_form.add_argument("--code", help="该模型的参数面板（编码来自 models 的实时清单）")
    p_form.add_argument("--param", action="append", type=_param_pair, default=[], metavar="名=值",
                        help="已定下的参数值（可重复）；给出后面板会算出预计消耗积分")
    p_form.add_argument("--json", action="store_true", help="输出 JSON（推荐给 agent 消费）")
    p_form.set_defaults(func=cmd_form)

    for name, label, handler in (("image", "图片", cmd_image),
                                 ("video", "视频", cmd_video),
                                 ("audio", "音频", cmd_audio)):
        p = sub.add_parser(name, help=f"生成{label}（任务型/异步，默认轮询到完成；"
                                      f"典型耗时 {_eta_text(name)}）")
        p.add_argument("--prompt", required=True, help="提示词")
        p.add_argument("--model", help="模型编码；省略时自动选择该类型下第一个可用模型")
        p.add_argument("--ref-image", action="append", default=[], metavar="URL_OR_PATH",
                       help="参考图（可重复，URL 或本地文件路径）")
        p.add_argument("--ref-video", action="append", default=[], metavar="URL_OR_PATH",
                       help="参考视频（可重复）")
        p.add_argument("--ref-audio", action="append", default=[], metavar="URL_OR_PATH",
                       help="参考音频（可重复）")
        p.add_argument("--json", action="store_true")
        _add_param_option(p)
        _add_wait_options(p)
        _add_credit_option(p)
        p.set_defaults(func=handler)

    p_chat = sub.add_parser("chat", help="文本生成（同步返回；按模型协议走官方原生路由，支持流式）")
    p_chat.add_argument("--prompt", required=True, help="用户消息")
    p_chat.add_argument("--model", help="模型编码；省略时自动选择第一个可用文本模型")
    p_chat.add_argument("--system", help="系统提示（system prompt）")
    p_chat.add_argument("--image", action="append", default=[], metavar="URL_OR_PATH",
                        help="图片输入（可重复，URL 或本地文件路径；需模型支持 img2txt）")
    p_chat.add_argument("--max-tokens", type=int, help="最大输出 token 数（Anthropic 协议默认 1024）")
    p_chat.add_argument("--temperature", type=float, help="采样温度")
    p_chat.add_argument("--stream", action="store_true", help="流式输出（SSE）")
    p_chat.add_argument("--protocol", choices=["auto", *PROTOCOL_PATHS.keys()],
                        default="auto",
                        help="auto=按该模型主协议自动选择官方路由（推荐）；"
                             "openai/anthropic/responses=强制走该协议的官方路由（需模型支持）")
    p_chat.add_argument("--show-usage", action="store_true",
                        help="输出用量与积分信息（token 用量、本次扣减、剩余积分）；"
                             "一律写到标准错误，不污染正文")
    p_chat.add_argument("--json", action="store_true")
    p_chat.add_argument("--quiet", action="store_true", help="不输出过程信息")
    _add_param_option(p_chat)
    _add_credit_option(p_chat)
    p_chat.add_argument("--timeout", type=int, default=180, help="请求超时秒数（默认 180）")
    p_chat.set_defaults(func=cmd_chat)

    p_task = sub.add_parser("task", help="查询一次任务状态（不轮询）")
    p_task.add_argument("--id", required=True, help="任务 ID")
    p_task.add_argument("--type", choices=list(TASK_MODEL_TYPES), help="结果类型（仅影响提示文案）")
    p_task.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数（默认 30）")
    p_task.add_argument("--json", action="store_true")
    p_task.set_defaults(func=cmd_task)

    p_wait = sub.add_parser("wait", help="把已创建的任务轮询到终态")
    p_wait.add_argument("--id", required=True, help="任务 ID")
    p_wait.add_argument("--type", choices=list(TASK_MODEL_TYPES),
                        help="结果类型；同时决定默认等待上限（图 600 / 音频 1200 / 视频 5400 秒）")
    p_wait.add_argument("--json", action="store_true")
    p_wait.add_argument("--timeout", type=int, default=30)
    p_wait.add_argument("--max-wait", type=int, default=None,
                        help="最长等待秒数；省略时按 --type 给默认值（未给 --type 则为 900）")
    p_wait.add_argument("--poll-interval", type=int, default=6)
    p_wait.add_argument("--quiet", action="store_true")
    _add_credit_option(p_wait)
    p_wait.set_defaults(func=cmd_wait)

    p_upload = sub.add_parser(
        "upload", help="上传本地素材，得到可被生成接口引用的 URL（平台存储通道，需 API Key）")
    p_upload.add_argument("--file", required=True, help="本地文件路径")
    p_upload.add_argument("--content-type", help="覆盖内容类型（默认按扩展名推断）")
    p_upload.add_argument("--host", choices=["auto", *UPLOAD_HOSTS], default="auto",
                          help="上传通道：auto / platform = 平台存储通道（需 API Key）")
    p_upload.add_argument("--timeout", type=int, default=180, help="上传超时秒数")
    p_upload.add_argument("--no-verify", dest="verify", action="store_false", default=True,
                          help="跳过上传后的地址可达性校验（默认会校验一次）")
    p_upload.add_argument("--json", action="store_true")
    p_upload.set_defaults(func=cmd_upload)

    p_credit = sub.add_parser("credit", help="查询积分余额")
    p_credit.add_argument("--json", action="store_true")
    p_credit.set_defaults(func=cmd_credit)

    return parser


def main(argv: list[str] | None = None) -> int:
    # 解析与分发**都在 try 内**：`build_parser()` 本身若抛异常（例如某次改动引入
    # 的编程错误），原先会因为它在 try 之外而把 traceback 直接打到用户面前。
    # `parse_args` 的 `SystemExit` 不是 `Exception` 子类，不受这里的兜底影响。
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        return int(args.func(args) or 0)
    except NoovaError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # `models | head` 这类管道被提前关闭是日常操作，不是故障。
        # 必须把 stdout 换成 devnull，否则解释器退出时还会补一条
        # "Exception ignored in: <_io.TextIOWrapper ...>"，看起来像崩溃。
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:  # noqa: BLE001
            pass
        return 0
    except Exception as exc:  # noqa: BLE001
        # 兜底：任何未预期异常都**不得**把 traceback（含 skill 的绝对安装路径与
        # 完整调用栈）打到用户面前——那正是 SKILL.md 铁律 6 明令禁止的内容。
        # 已复现的逃逸路径：http.client.IncompleteRead、流式读超时、
        # BrokenPipeError、helper 抛出的 ValueError/OSError。
        print(f"错误：发生未预期的内部错误（{type(exc).__name__}）；"
              f"请重试，若持续出现请反馈该现象。", file=sys.stderr)
        if debug_enabled():
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
