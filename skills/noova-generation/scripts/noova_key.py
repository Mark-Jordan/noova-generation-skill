#!/usr/bin/env python3
"""NooVa AI 公开 API 客户端配置工具（零依赖，仅 Python 标准库）。

职责
----
本模块是**客户端配置与客户端标识的单一来源**：`noova_media.py` / `noova_upload.py`
都从这里读取 API Key、基础地址与客户端标识，避免多处硬编码不一致。

  status   检查配置状态（`--json` 供 agent 消费；未配置时打印引导块）
  guide    打印首次使用引导（ASCII logo + API 直达地址 + 获取 Key 两步）
  setup    一键配置：解析用户粘贴的文本 → 联网校验 → 写入配置文件（推荐入口）
  save     setup 的兼容别名（写盘但不联网校验）
  verify   联网校验已保存的 Key 是否可用
  doctor   环境自检（Python / 解释器路径 / 依赖脚本 / 配置目录可写 / 配置文件可解析 /
           Key 状态 / 地址是否为线上域名 / 地址可达性 / Key 有效性 —— 共 9 项）
  base     查看或设置 API 基础地址
  clear    清除已保存的 API Key

配置来源（优先级从高到低）
--------------------------
  1. 环境变量 `NOOVA_API_KEY` / `NOOVA_BASE_URL`
  2. 配置文件里的 `api_key` / `base_url`。配置文件位置按序探测：
     `$NOOVA_CONFIG_DIR/config.json`  →  `~/.noova/config.json`
     →  `<SKILL_DIR>/.noova/config.json`（家目录不可写时的兜底）

路径纪律
--------
  上表中的路径**全部在运行时解析，无一处写死**：家目录取自 `Path.home()`（Windows
  走 `USERPROFILE`）、`SKILL_DIR` 由本文件位置反推、覆盖项取自环境变量。因此换用户、
  换操作系统、换安装目录，得到的路径都会随之改变。展示类文案（引导 / 自检）与写盘
  走同一个解析器 `config_file()`，保证「提示里的位置」永远等于「真正落盘的位置」。

凭据安全（本模块是 Key 的唯一处理点）
------------------------------------
  1. **Key 绝不明文回显**（只显示前缀）；不写日志；服务端文本经脱敏后输出。
  2. **Key 只发往线上部署域名**：`_probe()` 是唯一的出网点，发请求前先过
     `check_base_url()`；不合格时**一个字节都不发**（含 `verify`）。
  3. **基础地址只能来自可信来源**：显式 `--base-url`、JSON 里的 `base_url` 字段、
     或与官方域名同域的地址。粘贴文本里出现的其它 URL 一律忽略并提示——
     否则「用户从网页复制 Key 时连带复制了一行文档链接」就会把 Key 发到那个域名。
  4. 地址守卫会归一化主机名（尾点 / userinfo / 端口）、做 DNS 解析后判定，
     并拒绝跨源重定向，避免 `localhost.` / `127.0.0.1.nip.io` 这类等价写法绕过。
  5. 配置文件按 0600 **原子**写入（先写临时文件再 `os.replace`），中断不会留下半截文件。
  6. 传入 Key 优先用 `--stdin`：`setup --api-key <key>` 会把 Key 留在 shell 历史与
     进程列表里。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 路径纪律：本脚本可能被任意 cwd 调用，先把自身目录放进搜索路径，再导入同目录模块。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from noova_common import (  # noqa: E402
    ALLOW_LOCAL_ENV,          # 再导出：测试与文档都按这个名字引用本机地址逃生阀
    CONSOLE_PATH,
    DEFAULT_BASE_URL,
    OFFICIAL_BASE_HINT,
    check_base_url,
    canonical_official_host,
    debug_enabled,
    display_width as _display_width,
    fit as _fit,
    is_local_host,
    is_official_host,
    normalize_host,
    open_credentialed,
    origin_of,
    pad as _pad,
    sanitize_payload,
    sanitize_text,
)

# Windows 控制台默认编码可能不是 UTF-8，统一输出编码，避免中文 banner 报错。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

# ---------------------------------------------------------------------------
# 常量：客户端身份与公开地址（全部为对用户公开的信息）
# ---------------------------------------------------------------------------

PACKAGE_NAME = "noova-generation"
VERSION = "1.9.2"

# 脚本自身位置：用于生成「绝对路径命令」，避免提示里的相对路径在任意 cwd 下失效。
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
SELF_PATH = Path(__file__).resolve()

# ---------------------------------------------------------------------------
# 解释器定位：不能从 Python 进程可靠反推出用户最初用 `py -3`、`python` 还是
# 绝对路径启动。唯一确定可用的是当前进程的 `sys.executable`；用其绝对路径生成
# 后续命令，避免 PATH 名称差异、Windows Store stub 和 py launcher 差异。
# ---------------------------------------------------------------------------
PYTHON_CMD = Path(sys.executable).resolve().as_posix() if sys.executable else ""


def python_cmd(script: "Path | str") -> str:
    """拼一条当前机器可复制的命令，使用当前解释器绝对路径，不依赖 PATH。"""
    if not PYTHON_CMD:
        raise ConfigError("无法定位当前 Python 解释器；请先检查 Python 安装")
    return f'"{PYTHON_CMD}" "{Path(script).resolve().as_posix()}"'

# `DEFAULT_BASE_URL` / `CONSOLE_PATH` 由 `noova_common` 提供（单一来源），
# 这里不再重复定义——重复定义正是「两个模块判据漂移」的起点。
# 其余脚本仍可 `from noova_key import DEFAULT_BASE_URL`（导入即再导出）。

# 粘贴文本里**允许**被认作基础地址的域名：只有官方域名本身（含 www 变体），
# 判定统一走 `noova_common.is_official_host()`（单一来源）；
# 见 `parse_credentials()`——其它 URL 一律忽略，因为校验请求会带着 Key 发出去。

# 显式声明的客户端标识：部分边缘网络策略会拦截「已知自动化客户端的默认标识」
# （例如 Python 标准库 urllib 的默认 UA 会被直接拒绝），因此所有请求必须显式声明。
# 产品标识与 PACKAGE_NAME 同源（仅品牌大小写不同）——有测试守门，改名不会漏改这里。
USER_AGENT = f"NooVa-Generation/{VERSION} (+{DEFAULT_BASE_URL}{CONSOLE_PATH})"

# 连通性探测接口（属**对外 API 链路**，不是对内的前端控制台接口）。
# 2026-10-09 起 `/api/models` 也已收口为**需要有效 Key**（无 Key → 401）；
# 本处只用于「探活」，因此刻意**不带 Key**，401 视为「服务可达、仅缺鉴权」（由 doctor 解读）。
PUBLIC_PROBE_PATH = "/api/models"
# 鉴权探针一（首选）：专用校验接口，**恒 200**，判定看响应体 `data.valid` 与 `data.reason`。
# 实测 2026-09-29：无 Key → `{"valid": false, "reason": "缺少 API Key"}`；
#                  无效 Key → `{"valid": false, "reason": "API Key 无效或已停用"}`。
AUTH_VALIDATE_PATH = "/api/v1/gateway/validate-key"
# 鉴权探针二（回退）：积分查询接口，**真正校验 Key**（无 Key / 无效 Key → 401）。
# 本 skill 统一用下面的 AUTH_PROBE_PATH 判 Key，不依赖模型清单接口。
# 模型清单接口的鉴权语义曾多次变更（实测 2026-09-29 与 2026-10-07 不一致），
# 所以任何依赖它的判断都是不稳定的；本模块只保留上面两个探针。
AUTH_PROBE_PATH = "/api/v1/credit"

CONFIG_DIR_ENV = "NOOVA_CONFIG_DIR"
CONFIG_FILE_NAME = "config.json"


class ConfigError(Exception):
    """本地配置/输入错误（非网络错误）；message 可直接展示给用户。"""


# ---------------------------------------------------------------------------
# 路径与读写
# ---------------------------------------------------------------------------

def _config_candidates() -> list[Path]:
    """按优先级列出候选配置文件路径（不判断存在性）。"""
    paths: list[Path] = []
    env_dir = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if env_dir:
        paths.append(Path(env_dir).expanduser() / CONFIG_FILE_NAME)
    paths.append(Path.home() / ".noova" / CONFIG_FILE_NAME)
    # 兜底：家目录不可写（沙箱/受限环境）时，落到 skill 目录内。
    paths.append(SKILL_DIR / ".noova" / CONFIG_FILE_NAME)
    return paths


def config_file(*, create: bool = True) -> Path:
    """返回**当前写盘目标**配置文件路径。

    **全部按本机实际环境解析，没有任何写死的路径**（不同用户、不同系统、不同安装
    位置都会得到不同结果）。优先级见 `_config_candidates()`：

    1. `NOOVA_CONFIG_DIR` —— 调用方显式指定的位置，**读写两侧都以此为准**；
    2. `Path.home()/.noova/config.json` —— 家目录随用户而变（Windows 走 `USERPROFILE`）；
    3. `<SKILL_DIR>/.noova/config.json` —— 家目录不可写时的兜底，`SKILL_DIR` 由本
       文件位置（`__file__`）反推，随安装位置而变。

    `create=False`：只解析、**不创建任何目录**，供展示类文案（引导 / 自检）使用。
    展示与实际写盘走同一个解析器，因此提示里的路径永远等于真正落盘的位置。

    `NOOVA_CONFIG_DIR` 指定但目录建不出来时**抛 `ConfigError`，不静默改道**：
    改道的后果是「提示里说写到 A、实际写到 B」，用户按提示去改 A 却改不到生效的那份。
    """
    candidates = _config_candidates()

    # 显式覆盖优先：既然指定了位置，就不能因为家目录已有旧配置而偷偷改道
    # （否则「读」认覆盖、「写」认家目录，钥匙会存到用户没想到的地方）。
    if os.environ.get(CONFIG_DIR_ENV, "").strip():
        preferred = candidates[0]
        if not create:
            return preferred
        try:
            preferred.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"{CONFIG_DIR_ENV} 指定的目录不可用：{preferred.parent}"
                f"（{type(exc).__name__}）；请修正该环境变量或取消它") from exc
        return preferred

    for path in candidates:
        if path.exists():
            return path
    if not create:
        return candidates[0]
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    return candidates[-1]


def config_sources() -> list[Path]:
    """返回当前实际存在的配置文件列表（按优先级）。"""
    return [p for p in _config_candidates() if p.exists()]


def _read_json(path: Path) -> dict:
    """读取一个配置文件；解析失败返回 `{}`（由 `corrupt_config_files()` 负责报告）。

    这里**刻意不维护跨调用的全局状态**：曾经用模块级 set 记录损坏文件，结果同一
    进程内跑多次自检时，前一次的临时路径会残留下来，把后一次的自检误判为不通过。
    损坏与否一律现算（`corrupt_config_files()`），没有历史包袱。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def corrupt_config_files() -> list[str]:
    """列出当前存在但**无法解析**的配置文件路径。

    为什么必须报出来：静默当空的后果是用户看到「尚未配置 Key」，于是重新粘贴一遍
    ——期间 `base_url` 等其它字段被一并丢掉，而真正的故障（文件坏了）从未被告知。
    """
    broken: list[str] = []
    for path in config_sources():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            broken.append(str(path))
            continue
        if not text.strip():
            continue
        try:
            if not isinstance(json.loads(text), dict):
                broken.append(str(path))
        except Exception:  # noqa: BLE001
            broken.append(str(path))
    return broken


def _load() -> dict:
    """合并所有存在的配置来源，优先级高的覆盖低的。"""
    merged: dict = {}
    for path in reversed(config_sources()):
        merged.update({k: v for k, v in _read_json(path).items() if v not in (None, "")})
    return merged


def _write_config(path: Path, data: dict) -> None:
    """**原子**写入配置文件，权限 0600（在文件诞生那一刻就生效）。

    两点都不能省：

    - 先写同目录临时文件再 `os.replace()`。`write_text` 是**先截断再写**，写到一半
      被中断（断电、被 kill、磁盘满）就留下半截 JSON，用户下次看到的是「尚未配置」。
    - 用 `os.open(..., 0o600)` 而不是「先写再 chmod」：后者在 umask 022 下有一个
      短暂的 0644 窗口，这段时间里同机其它用户能读到明文 Key。
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    handle_fd = None
    try:
        handle_fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle_fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
    except OSError as exc:
        raise ConfigError(f"无法写入配置文件 {path}（{type(exc).__name__}）") from exc
    finally:
        if handle_fd is not None:
            try:
                os.close(handle_fd)
            except OSError:
                pass
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 上不支持 POSIX 权限位，依赖用户目录 ACL


def _save(data: dict) -> Path:
    path = config_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"无法创建配置目录 {path.parent}（{type(exc).__name__}）") from exc
    _write_config(path, data)
    return path


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def get_api_key() -> str:
    """读取生效的 API Key；未配置返回空串。环境变量优先。"""
    env_key = os.environ.get("NOOVA_API_KEY", "").strip()
    if env_key:
        return env_key
    return str(_load().get("api_key") or "").strip()


def get_api_key_source() -> str:
    """返回 Key 的来源：`env` / `file` / `none`（供 doctor 与 status 使用）。"""
    if os.environ.get("NOOVA_API_KEY", "").strip():
        return "env"
    if str(_load().get("api_key") or "").strip():
        return "file"
    return "none"


def get_base_url() -> str:
    env_base = os.environ.get("NOOVA_BASE_URL", "").strip()
    if env_base:
        return env_base.rstrip("/")
    configured = str(_load().get("base_url") or "").strip().rstrip("/")
    return configured or DEFAULT_BASE_URL


def console_url(base_url: str | None = None) -> str:
    """Key 领取页面地址（与基础地址同域）。"""
    return f"{(base_url or get_base_url()).rstrip('/')}{CONSOLE_PATH}"


def mask_key(key: str) -> str:
    """只展示前缀，绝不明文回显完整 Key。"""
    text = str(key or "")
    if not text:
        return ""
    return f"{text[:7]}…" if len(text) > 7 else "已配置"


# ---------------------------------------------------------------------------
# 输入解析：把「用户粘贴的任何文本」变成 (api_key, base_url)
# ---------------------------------------------------------------------------

_BEARER_RE = re.compile(r"\bBearer\s+([A-Za-z0-9._\-]{8,})", re.IGNORECASE)
# 只认「API Key」类标签。**刻意不含裸 `token` / `secret`**：`Cookie: token=abcdefgh12345`
# 或任何含 `token=` 的页面片段会让 `_ASSIGN_RE` 压过真正的 `sk-` Key，
# 于是一个完全有效的 Key 被拿去校验、被报成「无效」并拒绝写入（已实测）。
_ASSIGN_RE = re.compile(
    r"(?:^|[\s\"'`\[{,;、])"
    r"(?:noova[_-]?)?(?:api[_-]?key|apikey|key|密钥|秘钥)"
    r"\s*[:=：]\s*[\"'`]?([A-Za-z0-9._\-]{8,})[\"'`]?",
    re.IGNORECASE,
)
# 平台 Key 的统一前缀。解析顺序上它**优先于**上面的赋值标签（见 parse_credentials）。
_SK_RE = re.compile(r"\b(sk-[A-Za-z0-9][A-Za-z0-9._\-]{5,})\b")
_URL_RE = re.compile(r"https?://[^\s\"'`,;、，。)）\]\}<>]+", re.IGNORECASE)
_TRAILING_NOISE = "。．,，;；、'"  # 中文标点与粘贴残留


def _clean_blob(text: str) -> str:
    """去掉包裹引号/反引号/尖括号与首尾噪声字符，保留内部换行。"""
    raw = str(text or "")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    for _ in range(3):
        before = raw
        raw = raw.strip()
        for pair in (('"', '"'), ("'", "'"), ("`", "`"), ("<", ">"), ("（", "）")):
            if raw.startswith(pair[0]) and raw.endswith(pair[1]) and len(raw) > 1:
                raw = raw[1:-1].strip()
        raw = raw.strip(_TRAILING_NOISE).strip()
        if raw == before:
            break
    return raw


def _origin(url: str) -> str:
    """把 URL 归约成 `scheme://host[:port]`（丢弃路径，兼容控制台页面地址）。

    官方域名的 `www.` 变体一并归一为 apex，让 `https://www.noova.vip/x` 与
    `https://noova.vip` 得到同一个 origin（不会因写法不同被当成两个域）。
    """
    origin = origin_of(url)
    if not origin:
        return ""
    parts = urllib.parse.urlsplit(origin)
    host = canonical_official_host(parts.hostname or "")
    if not host or host == parts.hostname:
        return origin
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


# ---------------------------------------------------------------------------
# 地址守卫：实现已下沉到 `noova_common`（单一来源）
# ---------------------------------------------------------------------------
# 这里只做 re-export，不再保留一份自己的实现。原因见本文件头部「凭据安全」第 2 条：
# 判据分叉 = 漏洞。曾经 `noova_media.py` 有自己的守卫、`noova_key.py` 也有一份，
# 结果 `verify` 路径两边都没拦（第四轮审计 BLOCKER B2）。
#
# `check_base_url` / `is_local_host` / `ALLOW_LOCAL_ENV` 由文件头部 import 提供。


def _is_trusted_base(origin: str) -> bool:
    """该 origin 的 host 是否属于「可信基础地址来源」（官方域名本身，含 www 变体）。"""
    host = normalize_host(urllib.parse.urlsplit(str(origin or "")).hostname or "")
    return bool(host) and is_official_host(host)


def parse_credentials(text: str) -> dict:
    """从任意粘贴文本中解析凭据。

    支持：纯 Key（`sk-...`）、`api_key=...` / `api_key: ...` / `Bearer ...`、
      `NOOVA_API_KEY=...`、JSON 片段 `{"api_key": "...", "base_url": "..."}`、
      单独一个官方地址（URL）、以及「Key + 地址」混排的长文本。

    返回 `{"api_key", "base_url", "urls", "ignored_urls", "warnings"}`。
    识别不到时不抛异常（由调用方决定如何提示）。解析结果**绝不回显完整 Key**。

    两条规则来自第四轮审计的 BLOCKER，都是安全约束而非风格偏好：

    1. **基础地址只认可信来源**（B1）：JSON 的 `base_url` 字段，或 host 与官方域名
       相同的 URL。粘贴文本里的其它 URL 一律**忽略**并计入 `ignored_urls`。
       原因：`setup` 的流程是「解析 → 联网校验 → 校验通过才写盘」，**校验请求本身
       就带着用户的 Key**。若把首个 URL 当地址，「用户从网页复制 Key 时连带复制了
       一行文档链接」就会把 Key 发到那个域名，且校验通过后该域名被持久化。
    2. **Key 优先按平台前缀 `sk-` 认定**（B4）：见 `_ASSIGN_RE` 的注释。
    """
    blob = _clean_blob(text)
    result: dict = {"api_key": "", "base_url": "", "urls": [],
                    "ignored_urls": [], "rejected_urls": [], "warnings": []}
    if not blob:
        result["warnings"].append("输入为空")
        return result

    # ① JSON 片段（用户可能整段粘贴配置）——`base_url` 字段是**可信来源**
    if blob.startswith("{"):
        data = None
        try:
            data = json.loads(blob)
        except Exception:  # noqa: BLE001  不是合法 JSON，继续走文本解析
            data = None
        if isinstance(data, dict):
            key = str(data.get("api_key") or data.get("apiKey") or data.get("key") or "").strip()
            if key:
                result["api_key"] = key
            base = str(data.get("base_url") or data.get("baseUrl") or data.get("base") or "").strip()
            origin = _origin(base if "//" in base else f"https://{base}") if base else ""
            if origin:
                result["base_url"] = origin

    # ② 裸 Key（平台 Key 统一 `sk-` 前缀）——最先尝试，见规则 2
    if not result["api_key"]:
        match = _SK_RE.search(blob)
        if match:
            result["api_key"] = match.group(1).strip()

    # ③ 显式赋值（api_key= / 密钥: …）
    if not result["api_key"]:
        match = _ASSIGN_RE.search(blob)
        if match:
            result["api_key"] = match.group(1).strip()

    # ④ Bearer 头
    if not result["api_key"]:
        match = _BEARER_RE.search(blob)
        if match:
            result["api_key"] = match.group(1).strip()

    # ⑤ 地址：只有「与官方域名同域」的 URL 才被采纳，其余全部忽略并如实告知
    urls = _URL_RE.findall(blob)
    result["urls"] = urls
    origins: list[str] = []
    for url in urls:
        origin = _origin(url)
        if origin and origin not in origins:
            origins.append(origin)
    trusted = [origin for origin in origins if _is_trusted_base(origin)]
    # 被忽略的地址分两类，处理方式**不同**：
    #   · 本机/内网/开发环境地址 —— 用户很可能是**有意**要配这个地址（自建网关）。
    #     静默改成线上域名会让他的 Key 发去一个他没想到的地方，所以这一类**明确报错**。
    #   · 其它公开但非官方的地址 —— 多半是从网页连带复制进来的文档链接，忽略并提示。
    ignored = [origin for origin in origins if origin not in trusted]
    rejected = [origin for origin in ignored
                if is_local_host(urllib.parse.urlsplit(origin).hostname or "")]
    result["rejected_urls"] = rejected
    result["ignored_urls"] = [origin for origin in ignored if origin not in rejected]
    if trusted and not result["base_url"]:
        result["base_url"] = trusted[0]
    if result["ignored_urls"]:
        result["warnings"].append(
            "已忽略文本中的地址：{0}。基础地址只能来自 --base-url、JSON 的 base_url "
            "字段，或与 {1} 同域的地址；如需自定义地址，请显式传 --base-url。".format(
                "、".join(result["ignored_urls"]), DEFAULT_BASE_URL))

    # ⑥ 兜底：纯主机名（如 `noova.vip`），仅在完全没有 Key 线索且文本很短时才认
    if not result["api_key"] and not result["base_url"]:
        candidate = blob.split()[0] if blob.split() else ""
        if re.fullmatch(r"[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(:\d+)?", candidate):
            origin = _origin(f"https://{candidate}")
            if _is_trusted_base(origin):
                result["base_url"] = origin

    return result


# ---------------------------------------------------------------------------
# 引导块
# ---------------------------------------------------------------------------

LOGO = r"""
  _   _              _         _    _         _
 | \ | | ___   __ _(_)_   __ | |  / \  _   _ (_)
 |  \| |/ _ \ / _` | \ \ / / | | / _ \| | | || |
 | |\  | (_) | (_| | |\ V /  | |/ ___ \ |_| || |
 |_| \_|\___/ \__,_|_| \_/   |_/_/   \_\__,_||_|
"""

_RULE = "─" * 78
_FRAME_WIDTH = 70

# `_display_width` / `_pad` / `_fit` 均来自 `noova_common`（见文件头部 import）——
# 这三个函数在 `noova_key.py` 与 `noova_media.py` 里曾各有一份完全相同的实现，
# 改一处忘一处就会让两个脚本的表格排版不一致。
# `_fit` 在这里仍然有用：它负责把超宽内容收进定宽框。


def _frame(lines: list[str], *, width: int = _FRAME_WIDTH, pad: int = 3,
           center: bool = False) -> list[str]:
    """把若干行文字装进圆角框（按显示宽度对齐），返回逐行文本。"""
    inner = width - 2
    body_width = inner - pad * 2
    out = ["╭" + "─" * inner + "╮"]
    for line in lines:
        body = _fit(line, body_width)
        if center and body.strip():
            body = " " * ((body_width - _display_width(body)) // 2) + body
        out.append("│" + " " * pad + _pad(body, body_width) + " " * pad + "│")
    out.append("╰" + "─" * inner + "╯")
    return out


def render_guide(base_url: str | None = None, *, configured: bool | None = None) -> str:
    """首次使用引导块：API 直达地址 + 领取 Key 的两步操作。

    既是给人看的终端提示，也是给 agent 直接转述给用户的文本——
    因此**只有纯文本与制表字符**，不含颜色控制码，复制到任何地方都不会乱。
    """
    base = (base_url or get_base_url()).rstrip("/")
    console = console_url(base)
    # 凭据落盘位置按本机实际环境解析（不创建目录）——不同用户 / 系统 / 安装位置结果不同。
    # 这里绝不可写死路径：它是给用户照着找文件的，写死等于给错误答案。
    cred_path = config_file(create=False)
    media_cmd = python_cmd(SCRIPT_DIR / "noova_media.py")
    setup_cmd = python_cmd(SELF_PATH)

    # 框内只放「状态与地址」这类短行；**命令一律放在框外**。
    # 原因：`_frame` 会把超宽内容截断成 `…`，而绝对路径命令必然超过框宽——
    # 那会让用户复制到一条被截断、根本跑不起来的命令。
    lines = _frame(
        [*LOGO.strip("\n").splitlines(), "", "NooVa AI 创作平台 · 公开 API 网关",
         f"v{VERSION}"],
        center=True,
    )

    if configured:
        lines += [
            "",
            "  状态：已配置 API Key —— 无需再次配置，可直接发起生成。",
            "",
            f"  连接地址（base_url）  {base}",
            f"  控制台（管理 Key）    {console}",
            f"  凭据保存位置          {cred_path}（仅本机，不会上传）",
            "",
            "  试一试：生成一张图",
            f"    {media_cmd} image --prompt \"一只戴墨镜的猫\"",
        ]
        return "\n".join(lines)

    lines += [
        "",
        "  状态：尚未配置 API Key —— 完成下面两步即可开始生成。",
        "",
        "  ① 领取 API Key",
        f"       控制台   {console}",
        "       路径     登录 → 「API 管理」→「创建 API Key」→ 填写名称 →「确认创建」",
        "       注意     完整 Key 仅在创建时展示一次，请立即复制（sk- 开头）",
        "",
        "  ② 交给助手自动完成配置",
        "       把 Key 直接粘贴到对话里即可；助手会先联网校验，通过后自动保存到本机",
        "",
        f"  连接地址（base_url）  {base}",
        f"  凭据保存位置          {cred_path}（仅本机，不会上传）",
        "",
        "  说明：Key 只需配置一次；换机器、换客户端工具时需要重新配置。",
        "",
        "  手动配置（等价命令）：",
        "    # 推荐 --stdin：Key 不会留在 shell 历史与进程列表里",
        f'    echo "sk-xxxx" | {setup_cmd} setup --stdin',
        "    # 等价写法（会留在命令历史里，仅在不便用管道时使用）",
        f"    {setup_cmd} setup --api-key sk-xxxx",
        "    # 只有在你使用自建部署地址时才需要显式指定 --base-url",
        f"    {setup_cmd} setup --api-key sk-xxxx --base-url {DEFAULT_BASE_URL}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 网络探测（零依赖）
# ---------------------------------------------------------------------------

def _server_message(payload: object) -> str:
    """只取一句简短、无标记的服务端提示语；带 HTML/标记的丢弃，**含链接的脱敏**。

    早期实现遇到含 `http` 的消息就**整条丢弃**，于是同一个响应里
    「一个分支泄露、一个分支丢信息」：服务端明明给了可读原因，用户只看到兜底文案。
    现在统一过 `sanitize_text()`——非公开域名换成中性占位域名后照常展示。
    """
    if not isinstance(payload, dict):
        return ""
    for field in ("message", "msg", "error"):
        value = payload.get(field)
        if isinstance(value, dict):
            value = value.get("message")
        if isinstance(value, str) and value.strip():
            text = value.strip()
            # 带标记语言的整条丢弃：它们多半是网关的 HTML 错误页，转述没有价值。
            if any(char in text for char in "<>{}") or len(text) > 200:
                continue
            return sanitize_text(text)
    return ""


def _probe(url: str, *, key: str | None = None, timeout: int = 20, method: str = "GET",
           body: object = None) -> dict:
    """探测一个公开端点。

    返回 `{"ok": True|False|None, "status": int|None, "reason": str, "payload": obj,
    "blocked": bool}`。

    `ok` 的语义（很重要）：
      - `True`  —— 服务端明确认可（HTTP 2xx）
      - `False` —— 服务端明确拒绝鉴权（HTTP 401）；**只有这一种情况能判定「Key 无效」**
      - `None`  —— 无法判定：网络不可达、服务端异常，或 HTTP 403
                  （403 可能是边缘策略拦截而非 Key 问题，不能据此判定 Key 无效）

    **本函数是本模块唯一的出网点**，因此地址守卫装在这里：任何探测在发出请求前
    先过 `check_base_url()`，不合格时**一个字节都不发**。守卫曾经只装在
    `setup` / `base` / `doctor` 上，`verify` 完全没有——于是 `verify` 会把
    `Authorization: Bearer sk-…` 发到 `NOOVA_BASE_URL` 指向的任何地址
    （第四轮审计 BLOCKER B2）。放在这里意味着以后新增探测路径也自动受保护。
    """
    guard = check_base_url(url)
    if not guard["ok"]:
        return {"ok": None, "status": None, "blocked": True,
                "reason": guard["reason"], "payload": None}

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # 同源重定向守卫：跨源 302 会带着 Authorization 改道（见 noova_common）。
        with open_credentialed(request, timeout=timeout) as resp:
            raw = resp.read()
            return {"ok": True, "status": int(resp.status), "reason": "",
                    "payload": _parse(raw), "blocked": False}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            payload = _parse(exc.read())
        except Exception:  # noqa: BLE001
            payload = None
        detail = _server_message(payload)
        if status == 401:
            return {"ok": False, "status": status, "blocked": False,
                    "reason": detail or "认证未通过（Key 无效或已停用）", "payload": payload}
        if status == 403:
            # 403 语义不唯一（无权限 / 网络策略拦截），不下「Key 无效」的结论。
            return {"ok": None, "status": status, "blocked": False,
                    "reason": detail or "请求被拒绝（HTTP 403），无法判定 Key 是否有效",
                    "payload": payload}
        return {"ok": None, "status": status, "blocked": False,
                "reason": f"服务返回 HTTP {status}，暂时无法判定", "payload": payload}
    except Exception as exc:  # noqa: BLE001
        return {"ok": None, "status": None, "blocked": False,
                "reason": f"网络不可达：{type(exc).__name__}", "payload": None}


def _parse(raw: bytes) -> object:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return text


def _model_summary(payload: object) -> dict:
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return {}
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("model_type") or item.get("modelType") or "").strip().lower() or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return counts


def _credit_of(payload: object) -> object:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        return data.get("remainingTotal", data.get("remaining_total"))
    if isinstance(payload, dict):
        return payload.get("remainingTotal", payload.get("remaining_total"))
    return None


def verify_key(key: str, base_url: str, *, timeout: int = 20) -> dict:
    """联网校验 Key 是否真的可用。

    两级探测（都只发一次 GET/POST，不创建任务、不产生任何费用）：
      ① `AUTH_VALIDATE_PATH` 专用校验接口（首选，恒 200，判定看 `data.valid`）
      ② `AUTH_PROBE_PATH` 积分查询接口（回退：专用接口不可用时）

    **绝不能**用模型清单接口校验 Key：实测它公开只读，连无效 Key 都返回 200。

    `base_url` 不是线上部署域名时**不发任何请求**，直接返回
    `{"ok": None, "blocked": True, "reason": <可直接展示的原因>}`——`verify` 是
    文档指定的「配置出问题后的恢复命令」，因此它是被走到最多的路径，绝不能是
    唯一没有守卫的那条（见 `_probe`）。

    返回 `{"ok": True|False|None, "status", "reason", "credit", "endpoint", "blocked"}`。
    """
    base = base_url.rstrip("/")
    probe = _probe(f"{base}{AUTH_VALIDATE_PATH}", key=key, timeout=timeout,
                   method="POST", body={})
    if probe.get("blocked"):
        return {"ok": None, "status": None, "blocked": True, "reason": probe["reason"],
                "credit": None, "endpoint": None}
    data = probe["payload"].get("data") if isinstance(probe["payload"], dict) else None

    if probe["ok"] is True and isinstance(data, dict) and isinstance(data.get("valid"), bool):
        valid = bool(data["valid"])
        reason = str(data.get("reason") or _server_message(probe["payload"]) or "").strip()
        return {"ok": valid, "status": probe["status"], "blocked": False,
                "reason": "" if valid else (sanitize_text(reason) or "API Key 无效或已停用"),
                "credit": _credit_of(data.get("balance") or {}), "endpoint": AUTH_VALIDATE_PATH}

    if probe["status"] is None:
        # 网络不可达：不再发第二次请求（避免双倍超时），直接回报「无法判定」
        return {"ok": None, "status": None, "blocked": False, "reason": probe["reason"],
                "credit": None, "endpoint": AUTH_VALIDATE_PATH}

    # 回退：积分接口（401 = 明确无效）
    fallback = _probe(f"{base}{AUTH_PROBE_PATH}", key=key, timeout=timeout)
    return {"ok": fallback["ok"], "status": fallback["status"], "blocked": False,
            "reason": sanitize_text(fallback["reason"]),
            "credit": _credit_of(fallback["payload"]), "endpoint": AUTH_PROBE_PATH}


def public_models(base_url: str, *, timeout: int = 20) -> dict:
    """探测对外模型清单端点（现需 Key，无 Key → 401）。

    仅用于**可达性探测**：不带 Key，因此拿到 401 也说明服务可达（见 `cmd_doctor`）。
    返回 `{"ok", "status", "reason", "models"}`。
    """
    probe = _probe(f"{base_url.rstrip('/')}{PUBLIC_PROBE_PATH}", timeout=timeout)
    return {"ok": probe["ok"], "status": probe["status"], "reason": probe["reason"],
            "models": _model_summary(probe["payload"])}


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

# 顶层回显用户自己输入的字段。这些值来自用户刚粘贴/刚传入的参数，不是上游响应，
# 原样回显才说得清"哪个地址被拒了"；脱敏要拦的是上游文本，不是用户自己的输入。
_USER_INPUT_KEYS = ("base_url", "rejected_base_url")


def _public_hosts() -> tuple[str, ...]:
    """当前生效的基础域名视为公开（用户自己配置的地址不必替换成占位域名）。"""
    try:
        host = urllib.parse.urlparse(get_base_url()).hostname or ""
    except Exception:  # noqa: BLE001
        return ()
    return (host,) if host else ()


def _safe_obj(payload: object) -> object:
    """`--json` 路径的脱敏入口（与 `noova_media._safe_obj()` 同口径）。

    本模块曾是全 skill 里**唯一**完全不脱敏的模块：`setup --json` 会把
    `guard.reason` 里内插的原始主机名原样打到用户面前。SKILL.md 与
    troubleshooting 都承诺「输出零内部信息」，这条路径让承诺落不了地。

    例外：`_USER_INPUT_KEYS` 里的字段是**用户自己刚粘贴/配置的地址**，原样回显
    才能让用户知道"哪个地址被拒了"。它们不来自上游响应，不构成泄漏面——泄漏面
    是上游文本（服务端消息、错误详情、探测结果），那些一律走脱敏。
    """
    safe = sanitize_payload(payload, extra_public_hosts=_public_hosts())
    if isinstance(payload, dict) and isinstance(safe, dict):
        for name in _USER_INPUT_KEYS:
            if name in payload:
                safe[name] = payload[name]
    return safe


def _json_out(payload: object) -> None:
    """打印一个**已脱敏**的 JSON 载荷（本模块 stdout 的唯一 JSON 出口）。"""
    print(json.dumps(_safe_obj(payload), ensure_ascii=False, indent=2))


def _status_payload() -> dict:
    key = get_api_key()
    base = get_base_url()
    # `config_file()` 现在可能在覆盖目录不可用时抛 ConfigError（不再静默改道）。
    # 状态查询绝不能因此崩掉：退回「只解析不创建」的路径，并把错误如实带上。
    config_error = None
    try:
        target = str(config_file())
    except ConfigError as exc:
        target = str(config_file(create=False))
        config_error = str(exc)
    return {
        "configured": bool(key),
        "key_prefix": mask_key(key) if key else None,
        "key_source": get_api_key_source(),
        "base_url": base,
        "console_url": console_url(base),
        "config_file": target,
        "config_error": config_error,
        "config_sources": [str(p) for p in config_sources()],
        "corrupt_config_files": corrupt_config_files(),
        "client_user_agent": USER_AGENT,
        "version": VERSION,
    }


def cmd_status(args) -> int:
    payload = _status_payload()
    if args.json:
        payload["next_step"] = None if payload["configured"] else "运行 noova_key.py setup 完成配置"
        _json_out(payload)
        return 0 if payload["configured"] else 1

    if payload["configured"]:
        print(render_guide(configured=True))
        print()
        print(f"[OK] API Key 已配置：{payload['key_prefix']}"
              f"（来源：{'环境变量' if payload['key_source'] == 'env' else '配置文件'}）")
        print(f"     连接地址：{payload['base_url']}")
        print(f"     配置文件：{payload['config_file']}")
        print("     可直接使用 noova_media.py 发起生成请求。")
        if payload["corrupt_config_files"]:
            print(f"[!!] 以下配置文件不是合法 JSON，其中的配置未生效："
                  f"{'、'.join(payload['corrupt_config_files'])}", file=sys.stderr)
        return 0

    print(render_guide())
    if payload["config_error"]:
        print(f"[!!] {payload['config_error']}", file=sys.stderr)
    return 1


def cmd_guide(args) -> int:
    base = get_base_url()
    if args.json:
        _json_out({
            "version": VERSION,
            "base_url": base,
            "console_url": console_url(base),
            "configured": bool(get_api_key()),
            "steps": [
                f"打开 {console_url(base)} → 登录 → 「API 管理」→「创建 API Key」→ 复制 sk- 开头的 Key",
                "把 Key 粘贴给 AI 助手（或执行 setup --stdin / setup --api-key sk-xxxx）完成配置",
            ],
        })
        return 0
    print(render_guide(configured=bool(get_api_key())))
    return 0


def _apply_credentials(text: str, api_key: str | None, base_url: str | None) -> dict:
    """解析 + 显式参数覆盖，得到最终待写入的凭据与提示。

    地址来源优先级（高 → 低）：`--base-url` > `--api-key` 文本里的 JSON `base_url`
    字段 > 粘贴文本里的 JSON 字段 > 粘贴文本里与官方域名同域的 URL。
    低于第三档的来源在 `parse_credentials()` 里就被丢弃了（见其规则 1）。
    """
    parsed = parse_credentials(text)
    # --api-key 也可能是一整段粘贴文本，因此同样走一次解析
    explicit = (parse_credentials(api_key) if api_key
                else {"api_key": "", "base_url": "", "warnings": [],
                      "ignored_urls": [], "rejected_urls": []})
    key = (explicit.get("api_key") or "").strip() or parsed["api_key"]
    base = (str(base_url or "").strip().rstrip("/")
            or explicit.get("base_url") or parsed["base_url"])
    if base and not re.match(r"^https?://[^/\s]+", base):
        base = _origin(base if "//" in base else f"https://{base}") or base

    warnings: list[str] = []
    for item in [*parsed["warnings"], *explicit.get("warnings", [])]:
        if item not in warnings:
            warnings.append(item)
    ignored: list[str] = []
    for item in [*parsed.get("ignored_urls", []), *explicit.get("ignored_urls", [])]:
        if item not in ignored:
            ignored.append(item)
    rejected: list[str] = []
    for item in [*parsed.get("rejected_urls", []), *explicit.get("rejected_urls", [])]:
        if item not in rejected:
            rejected.append(item)
    return {"api_key": key, "base_url": base, "warnings": warnings,
            "urls": parsed["urls"], "ignored_urls": ignored, "rejected_urls": rejected}


def cmd_setup(args) -> int:
    """一键配置：解析粘贴文本 → （可选）联网校验 → 写盘。

    这是「用户在对话里粘贴 Key，自动配置到对应文件」的实现入口。
    """
    text = ""
    if args.text:
        text = " ".join(args.text)
    elif args.api_key:
        text = args.api_key
    elif args.stdin:
        text = sys.stdin.read()
    elif not args.base_url:
        # 地址也是合法输入：`setup --base-url <地址>` 单独使用时只落盘地址、
        # 返回 exit 1 提示还缺 Key（与 troubleshooting 文档一致）。
        print("错误：请提供粘贴文本：setup \"<Key>\"，或 --api-key sk-xxxx，"
              "或 --stdin，或 --base-url <地址>", file=sys.stderr)
        print(render_guide(), file=sys.stderr)
        return 2

    resolved = _apply_credentials(text, args.api_key, args.base_url)
    key = resolved["api_key"]
    base = resolved["base_url"]

    # 粘贴文本里出现本机/内网地址：用户很可能是**有意**要配它。必须明确拒绝，
    # 绝不能静默改用线上域名——那会把他的 Key 发到一个他没想到的地方。
    if resolved["rejected_urls"]:
        rejected = resolved["rejected_urls"][0]
        guard = check_base_url(rejected)
        message = {
            "configured": False,
            "reason": guard["reason"],
            "rejected_base_url": rejected,
            "base_url": DEFAULT_BASE_URL,
            "next_step": (f"请改用线上部署域名重试：setup --stdin --base-url "
                          f"{DEFAULT_BASE_URL}"),
        }
        if args.json:
            _json_out(message)
        else:
            print(f"[失败] {guard['reason']}", file=sys.stderr)
            print(f"       已拒绝写入配置（收到的地址：{rejected}）。", file=sys.stderr)
            print(f"       正确写法：--base-url {DEFAULT_BASE_URL}", file=sys.stderr)
        return 2

    # 输入里带了地址 → 先过守卫。本机/内网/开发环境地址一律拒绝写入，
    # 否则配置会永久指向一个只有当前这台机器能访问的地址。
    base_note = ""
    if base:
        guard = check_base_url(base)
        if not guard["ok"]:
            message = {
                "configured": False,
                "reason": guard["reason"],
                "rejected_base_url": base,
                "base_url": DEFAULT_BASE_URL,
                "next_step": (f"请改用线上部署域名重试：noova_key.py setup "
                              f"--api-key <你的Key> --base-url {DEFAULT_BASE_URL}"),
            }
            if args.json:
                _json_out(message)
            else:
                print(f"[失败] {guard['reason']}", file=sys.stderr)
                print(f"       已拒绝写入配置（收到的地址：{base}）。", file=sys.stderr)
                print(f"       正确写法：--base-url {DEFAULT_BASE_URL}", file=sys.stderr)
            return 2
        base = guard["url"]
        base_note = guard["warning"]

    if not key and not base:
        print("错误：未能从输入中识别出 API Key 或 API 地址。", file=sys.stderr)
        for warning in resolved["warnings"]:
            print(f"  · {warning}", file=sys.stderr)
        print(render_guide(), file=sys.stderr)
        return 2

    if not key:
        # 只拿到地址：先落盘地址，再提示还需要 Key
        data = _load()
        data["base_url"] = base
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        path = _save(data)
        message = {
            "configured": False,
            "base_url_saved": base,
            "base_url_note": base_note,
            "config_file": str(path),
            "next_step": "尚未提供 API Key，请粘贴 sk- 开头的 Key 完成配置",
            "console_url": console_url(base),
            "warnings": resolved["warnings"],
        }
        if args.json:
            _json_out(message)
        else:
            print(f"[OK] API 地址已保存：{base}")
            if base_note:
                print(f"     注意：{base_note}")
            for warning in resolved["warnings"]:
                print(f"     注意：{warning}")
            print(f"     配置文件：{path}")
            print("     还缺 API Key —— 请到下面地址获取后粘贴给助手：")
            print(f"     {console_url(base)}")
        return 1

    effective_base = base
    if not effective_base:
        # 没给地址 → 用已有的；若已有的本身是历史遗留的本机/内网地址，
        # 自动回落到线上部署域名，避免请求打到本地开发环境。
        current = check_base_url(get_base_url())
        if current["ok"]:
            effective_base = current["url"]
            base_note = current["warning"]
        else:
            effective_base = DEFAULT_BASE_URL
            base_note = (f"原先保存的地址不是线上部署域名，已自动改用 "
                         f"{DEFAULT_BASE_URL}")

    verification = {"ok": None, "status": None, "reason": "已跳过校验", "credit": None}
    if not args.no_verify:
        verification = verify_key(key, effective_base, timeout=args.timeout)

    if verification["ok"] is False and not args.force:
        message = {
            "configured": False,
            "reason": verification["reason"],
            "status": verification["status"],
            "base_url": effective_base,
            "console_url": console_url(effective_base),
            "next_step": "请确认 Key 是否完整、是否已停用；或换一个 Key 后重试",
        }
        if args.json:
            _json_out(message)
        else:
            print(f"[失败] {verification['reason']}（HTTP {verification['status']}）", file=sys.stderr)
            print(f"       未写入配置。请到 {console_url(effective_base)} 重新获取 Key。", file=sys.stderr)
        return 1

    data = _load()
    data["api_key"] = key
    data["base_url"] = effective_base
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["client_version"] = VERSION
    path = _save(data)

    mode = {True: "已校验通过", False: "未通过", None: "未能判定（离线或服务异常）"}[verification["ok"]]
    message = {
        "configured": True,
        "key_prefix": mask_key(key),
        "base_url": effective_base,
        "base_url_note": base_note,
        "config_file": str(path),
        "verified": verification["ok"],
        "verify_note": mode,
        "credit": verification.get("credit"),
        # 成功路径也**必须**把 warnings 带出来：曾经那条「文本中有多个地址，已采用
        # 第一个」只在「Key 和地址都没识别到」的分支才打印，于是成功路径上用户
        # 完全不知道自己的地址被改成了别的域名（第四轮审计 BLOCKER B1 的一半）。
        "warnings": resolved["warnings"],
        "next_step": "可直接发起生成请求：noova_media.py image --prompt \"...\"",
    }
    if args.json:
        _json_out(message)
        return 0

    print(f"[OK] API Key 已保存：{mask_key(key)}")
    print(f"     基础地址：{effective_base}")
    if base_note:
        print(f"     注意：{base_note}")
    for warning in resolved["warnings"]:
        print(f"     注意：{warning}")
    print(f"     配置文件：{path}")
    print(f"     可用性校验：{mode}"
          + (f"（剩余积分：{verification['credit']}）"
             if verification.get("credit") is not None else ""))
    if verification["ok"] is None:
        print(f"     注意：{verification['reason']}，请稍后用 verify 复查。")
    print(f"     现在可以直接生成：{python_cmd(SCRIPT_DIR / 'noova_media.py')} "
          f"image --prompt \"一只戴墨镜的猫\"")
    return 0


def cmd_save(args) -> int:
    """兼容入口：写盘但不做联网校验。"""
    args.no_verify = True
    return cmd_setup(args)


def cmd_verify(args) -> int:
    key = get_api_key()
    base = get_base_url()
    if not key:
        payload = {"configured": False, "base_url": base, "console_url": console_url(base),
                   "ok": False, "reason": "尚未配置 API Key"}
        if args.json:
            _json_out(payload)
        else:
            print(render_guide(), file=sys.stderr)
        return 1

    result = verify_key(key, base, timeout=args.timeout)
    payload = {"configured": True, "key_prefix": mask_key(key), "base_url": base, **result}

    # 地址被守卫拒绝：这是**配置错误**，不是 Key 的问题。退出码 2 与其它配置错误一致，
    # 且明确告诉用户「没有向该地址发过任何请求」——用户最需要知道的是 Key 有没有出境。
    if result.get("blocked"):
        if args.json:
            _json_out(payload)
        else:
            print(f"[失败] {result['reason']}", file=sys.stderr)
            print("       已拒绝向该地址发送任何请求（Key 未离开本机）。", file=sys.stderr)
            print(f"       改回线上地址：noova_key.py base {DEFAULT_BASE_URL}", file=sys.stderr)
        return 2

    # 退出码在两种输出模式下**必须一致**：agent 按退出码判断、用户看文字，
    # 两边结论相反是最坏的结果。判定口径统一为「只有 ok=True 才算成功」。
    if args.json:
        _json_out(payload)
        return 0 if result["ok"] is True else 1
    if result["ok"] is True:
        suffix = (f"；剩余积分：{result['credit']}" if result.get("credit") is not None else "")
        print(f"[OK] Key 可用（{mask_key(key)}）{suffix}")
        return 0
    if result["ok"] is False:
        print(f"[失败] {result['reason']}（HTTP {result['status']}）", file=sys.stderr)
        print(f"       请到 {console_url(base)} 重新获取 Key。", file=sys.stderr)
        return 1
    print(f"[未知] {result['reason']}；请检查网络后重试。", file=sys.stderr)
    return 1


def cmd_doctor(args) -> int:
    """环境自检：一眼看出「还差什么」。"""
    checks: list[dict] = []

    # 1) 运行环境
    #    `py_ok` 是「版本达标」；解释器路径单独报一行 —— 后续所有命令提示都用它，
    #    它必须真实存在，否则提示就是错的。
    py_ok = sys.version_info >= (3, 8)
    checks.append({
        "id": "python", "label": "Python 运行时", "ok": py_ok,
        "detail": f"{platform.python_version()}（需 ≥ 3.8）",
    })
    checks.append({
        "id": "python_cmd", "label": "解释器路径",
        "ok": bool(PYTHON_CMD and Path(PYTHON_CMD).is_file()),
        "detail": (f"使用当前 Python：{PYTHON_CMD}（命令不依赖 PATH）" if PYTHON_CMD else
                   "当前 Python 解释器路径不可用；请检查 Python 安装"),
    })

    # 2) 依赖脚本（含共享模块——少了它其余脚本 import 就会失败）
    deps = ("noova_common.py", "noova_media.py", "noova_upload.py")
    missing = [name for name in deps if not (SCRIPT_DIR / name).is_file()]
    checks.append({
        "id": "scripts", "label": "依赖脚本", "ok": not missing,
        "detail": f"{' / '.join(deps)} 均存在" if not missing
                  else f"缺少：{', '.join(missing)}（请重新安装本 skill）",
    })

    # 3) 配置目录可写
    #    展示路径用 `create=False`（不创建目录）；可写性走一次真正的「写盘目标解析」，
    #    因为那一步会真的去 mkdir——只有它失败才说明确实写不进去。
    path = config_file(create=False)
    writable = True
    reason = ""
    try:
        config_file()
    except (ConfigError, OSError) as exc:
        writable = False
        reason = f"（{exc if isinstance(exc, ConfigError) else type(exc).__name__}）"
    checks.append({
        "id": "config_dir", "label": "配置目录可写", "ok": writable,
        "detail": f"{path.parent}{reason}" if writable else f"{path.parent} 不可写{reason}",
    })

    # 4) 配置文件可解析（损坏必须如实报出来，而不是让用户以为「还没配置」）
    corrupt = corrupt_config_files()
    checks.append({
        "id": "config_parse", "label": "配置文件可解析", "ok": not corrupt,
        "detail": ("全部配置文件均可正常解析" if not corrupt else
                   "以下文件不是合法 JSON，其中的配置**未生效**："
                   + "、".join(corrupt) + "（删除该文件后重新配置即可）"),
    })

    # 5) Key 状态
    key = get_api_key()
    source = get_api_key_source()
    checks.append({
        "id": "api_key", "label": "API Key", "ok": bool(key),
        "detail": (f"已配置 {mask_key(key)}（来源：{'环境变量' if source == 'env' else '配置文件'}）"
                   if key else "未配置 —— 按下方引导获取"),
    })

    # 6) 基础地址是否为线上部署域名（而非本机 / 内网 / 开发环境）
    base = get_base_url()
    guard = check_base_url(base)
    if guard["ok"] and guard.get("local"):
        # 逃生阀打开时自检**不能**报 [OK]：那会让「自检通过」失去意义——
        # 用户看到全绿，却在一个只有本机能访问的地址上调试线上问题。
        checks.append({
            "id": "base_url_online", "label": "基础地址为线上域名", "ok": None,
            "detail": guard["warning"],
        })
    else:
        checks.append({
            "id": "base_url_online", "label": "基础地址为线上域名", "ok": guard["ok"],
            "detail": (f"{base}{'（' + guard['warning'] + '）' if guard['warning'] else ''}"
                       if guard["ok"] else guard["reason"]),
        })

    # 7) 基础地址可达性（探测 /api/models；该端点现需 Key，无 Key 会返回 401）
    reachable = None
    reach_detail = "已跳过（--offline）"
    if not args.offline and not guard["ok"]:
        reachable = False
        reach_detail = "已跳过（地址不是线上部署域名，见上一项）"
    elif not args.offline:
        result = public_models(base, timeout=args.timeout)
        summary = result["models"]
        status = result["status"]
        # 可达性 = 服务器回了 HTTP 状态码（哪怕 401）。不能把 401 当「地址不可达」——
        # `/api/models` 现在需要 Key，无 Key 时必然 401，那会和下一项「Key 有效性」
        # 自相矛盾（实测：地址报不可达、Key 却校验通过）。Key 是否有效由第 8 项单独判定。
        reachable = result["ok"] is True or status is not None
        if result["ok"] is True:
            reach_detail = f"{base} 可达（HTTP {status}，模型数 {summary.get('total', '-')}）"
        elif status is not None:
            reach_detail = f"{base} 可达（HTTP {status}，该探测接口需携带 Key）"
        else:
            reach_detail = f"{base} —— {result['reason']}"
    checks.append({"id": "reachable", "label": "API 地址可达", "ok": reachable, "detail": reach_detail})

    # 8) Key 有效性（走真正校验 Key 的公开接口）
    auth_ok = None
    auth_detail = "已跳过（未配置 Key）" if not key else "已跳过（--offline）"
    if key and not args.offline and not guard["ok"]:
        auth_detail = "已跳过（地址不是线上部署域名）"
    elif key and not args.offline:
        result = verify_key(key, base, timeout=args.timeout)
        auth_ok = result["ok"]
        if result.get("blocked"):
            auth_detail = f"已跳过（{result['reason']}）"
        elif result["ok"] is True:
            auth_detail = ("校验通过"
                           + (f"（剩余积分 {result['credit']}）" if result.get("credit") is not None else ""))
        elif result["ok"] is False:
            auth_detail = result["reason"]
        else:
            auth_detail = f"未能判定：{result['reason']}"
    checks.append({"id": "api_key_valid", "label": "Key 有效性", "ok": auth_ok, "detail": auth_detail})

    blockers = [c for c in checks if c["ok"] is False]
    ready = bool(key) and not blockers

    payload = {
        "ready": ready,
        "version": VERSION,
        "base_url": base,
        "console_url": console_url(base),
        "config_file": str(path),
        "checks": checks,
        "next_step": ("一切就绪，可直接生成" if ready
                      else "先到 " + console_url(base) + " 获取 Key 并完成配置"),
    }
    if args.json:
        _json_out(payload)
        return 0 if ready else 1

    print(f"NooVa AI Skill 自检　v{VERSION}")
    print(f"  连接地址  {base}")
    print(f"  控制台    {console_url(base)}")
    print(f"  配置文件  {path}")
    print(_RULE)
    label_width = max(_display_width(c["label"]) for c in checks)
    for check in checks:
        mark = {True: "[OK]", False: "[!!]", None: "[--]"}[check["ok"]]
        print(f"{mark} {_pad(check['label'], label_width)}  {check['detail']}")
    print(_RULE)
    if ready:
        print("结论：一切就绪，可直接发起生成请求。")
    elif not key:
        print("结论：尚未配置 API Key，请按下面引导完成配置。")
        print()
        print(render_guide(base_url=base))
    else:
        print("结论：存在阻塞项，请按上面的 [!!] 项处理：")
        for check in blockers:
            print(f"  · {check['label']} —— {check['detail']}")
    return 0 if ready else 1


def cmd_base(args) -> int:
    if args.base_url:
        guard = check_base_url(args.base_url)
        if not guard["ok"]:
            print(f"错误：{guard['reason']}", file=sys.stderr)
            print(f"      已拒绝写入配置（收到的地址：{args.base_url}）。", file=sys.stderr)
            print(f"      正确写法：noova_key.py base {DEFAULT_BASE_URL}", file=sys.stderr)
            return 2
        data = _load()
        data["base_url"] = guard["url"]
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save(data)
    base = get_base_url()
    guard = check_base_url(base)
    if args.json:
        payload = {"base_url": base, "console_url": console_url(base),
                   "config_file": str(config_file()),
                   "online": guard["ok"], "note": guard["warning"]}
        if not guard["ok"]:
            payload["warning"] = guard["reason"]
        _json_out(payload)
    else:
        print(f"基础地址：{base}")
        print(f"控制台  ：{console_url(base)}")
        if guard["warning"]:
            print(f"[!!] {guard['warning']}", file=sys.stderr)
        if not guard["ok"]:
            print(f"[!!] {guard['reason']}", file=sys.stderr)
    # 地址不可用时退出码必须非零：否则 `base && echo ok` 这类脚本会把
    # 「配置里是个本机地址」误判成成功。
    return 0 if guard["ok"] else 1


def cmd_clear(args) -> int:
    """清除本机保存的 API Key。

    **必须清掉所有来源**：`_load()` 合并全部存在的配置文件（优先级高的覆盖低的），
    只 pop 最高优先级那一个的后果是「打印已清除，但 Key 依然生效」——
    本地唯一的吊销手段会撒谎。
    """
    sources = config_sources()
    if not sources:
        # 本机本来就没有配置文件：不要为了「清除」而凭空创建一个空文件。
        message = {"cleared": False, "config_file": None, "config_files": [],
                   "note": "本机没有保存过 API Key（也不存在配置文件），无需清除"}
        if args.json:
            _json_out(message)
            return 0
        print(f"[OK] {message['note']}")
        return 0

    cleared: list[str] = []
    for path in sources:
        data = _read_json(path)
        if "api_key" not in data:
            continue
        data.pop("api_key", None)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_config(path, data)
        cleared.append(str(path))

    note = (f"已清除本机保存的 API Key（{len(cleared)} 个配置文件）" if cleared
            else "配置文件里没有保存过 API Key，无需清除")
    if os.environ.get("NOOVA_API_KEY", "").strip():
        note += "；环境变量 NOOVA_API_KEY 仍然生效，若在环境中设置过请自行清理"
    message = {"cleared": bool(cleared),
               "config_file": cleared[0] if cleared else None,
               "config_files": cleared, "note": note}
    if args.json:
        _json_out(message)
        return 0
    print(f"[OK] {note}")
    for path in cleared:
        print(f"     {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="noova_key.py",
        description=f"NooVa AI 公开 API 客户端配置工具（v{VERSION}）",
        epilog="示例：\n"
               "  noova_key.py status --json\n"
               "  noova_key.py setup --stdin\n"
               "  noova_key.py doctor\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PACKAGE_NAME} {VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="检查配置状态（未配置时打印引导）")
    p_status.add_argument("--json", action="store_true", help="输出 JSON")
    p_status.set_defaults(func=cmd_status)

    p_guide = sub.add_parser("guide", help="打印首次使用引导（含 API 直达地址）")
    p_guide.add_argument("--json", action="store_true")
    p_guide.set_defaults(func=cmd_guide)

    p_setup = sub.add_parser("setup", help="一键配置：解析粘贴文本 → 校验 → 写盘")
    p_setup.add_argument("text", nargs="*", help="用户粘贴的任意文本（含 Key）")
    p_setup.add_argument("--api-key", help="API Key（也可直接传一整段粘贴文本）")
    p_setup.add_argument("--base-url",
                         help=f"API 基础地址（仅自建部署时需要；默认 {DEFAULT_BASE_URL}）")
    p_setup.add_argument("--stdin", action="store_true",
                         help="从标准输入读取 Key（推荐：Key 不会留在 shell 历史里）")
    p_setup.add_argument("--no-verify", dest="no_verify", action="store_true",
                         help="跳过联网校验，直接写盘")
    p_setup.add_argument("--force", action="store_true", help="校验失败时仍写入")
    p_setup.add_argument("--timeout", type=int, default=20, help="校验请求超时秒数")
    p_setup.add_argument("--json", action="store_true")
    p_setup.set_defaults(func=cmd_setup)

    p_save = sub.add_parser("save", help="保存 API Key（不联网校验；setup 的兼容别名）")
    p_save.add_argument("text", nargs="*")
    p_save.add_argument("--api-key", help="API Key")
    p_save.add_argument("--base-url", help="API 基础地址（可选）")
    p_save.add_argument("--stdin", action="store_true")
    p_save.add_argument("--no-verify", dest="no_verify", action="store_true")
    p_save.add_argument("--force", action="store_true")
    p_save.add_argument("--timeout", type=int, default=20)
    p_save.add_argument("--json", action="store_true")
    p_save.set_defaults(func=cmd_save)

    p_verify = sub.add_parser("verify", help="联网校验已保存的 Key")
    p_verify.add_argument("--json", action="store_true")
    p_verify.add_argument("--timeout", type=int, default=20)
    p_verify.set_defaults(func=cmd_verify)

    p_doctor = sub.add_parser("doctor", help="环境自检（能否直接使用）")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.add_argument("--offline", action="store_true", help="跳过联网检查")
    p_doctor.add_argument("--timeout", type=int, default=20)
    p_doctor.set_defaults(func=cmd_doctor)

    p_base = sub.add_parser("base", help="查看/设置 API 基础地址")
    p_base.add_argument("base_url", nargs="?", help="新的基础地址（可选）")
    p_base.add_argument("--json", action="store_true")
    p_base.set_defaults(func=cmd_base)

    p_clear = sub.add_parser("clear", help="清除已保存的 API Key")
    p_clear.add_argument("--json", action="store_true")
    p_clear.set_defaults(func=cmd_clear)

    args = parser.parse_args(argv)
    # 解析与分发**都在 try 内**：`parse_args` 的 `SystemExit` 不是 `Exception`
    # 子类，不受下面的兜底影响；而任何其它未预期异常都不该把 traceback
    # （含 skill 的绝对安装路径与完整调用栈）打到用户面前。
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # `noova_key.py doctor | head` 这类管道被提前关闭是日常操作，不是故障。
        # 必须把 stdout 换成 devnull，否则解释器退出时还会再补一条
        # "Exception ignored in: <_io.TextIOWrapper ...>"，看起来像崩溃。
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:  # noqa: BLE001
            pass
        return 0
    except Exception as exc:  # noqa: BLE001
        # 兜底：任何未预期异常都**不得**把 traceback（含 skill 的绝对安装路径与
        # 完整调用栈）打到用户面前。需要堆栈时显式设 NOOVA_DEBUG=1。
        print(f"错误：发生未预期的内部错误（{type(exc).__name__}）；"
              f"请重试，若持续出现请反馈该现象。", file=sys.stderr)
        if debug_enabled():
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
