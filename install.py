#!/usr/bin/env python3
"""NooVa AI 生成 Skill 分发工具（安装 / 校验 / 打包；零依赖，仅 Python 标准库）。

定位
----
本脚本是**仓库侧的 skill 分发工具**，不是 skill 的一部分。
skill 本体位于 `<repo>/skills/noova-generation/`，只含标准结构
（`SKILL.md` + `scripts/` + `references/`），本身不带任何安装逻辑——
安装属于宿主与分发方的职责，不属于 skill。

  用途一：把 skill 安装到 AI 客户端的技能目录
    （仓库里的 `skills/` 不在客户端的技能发现范围内，必须安装才能被 agent 加载）

  用途二：把 skill 打成可分发的 zip（对标官方 skill-creator 的 package_skill.py）

装到哪里 —— 由「装给哪个 agent」决定
------------------------------------
**每个 agent 有各自的技能根，装错位置 = 装完了它读不到。** 这是本工具最重要的一条
约束（历史上 `auto` 曾固定选 `~/.codebuddy/skills`，而 DSH 读 `~/.agents/skills`，
于是安装报告「已安装且一致」、agent 却永远发现不了这个 skill）。

因此 `--target auto`（默认）的判定顺序是：

  1. **探测当前正在运行哪个 agent**（环境变量特征，见 `HOST_MARKERS`）→ 用它的技能根；
  2. 探测不到时，用本机**被技能管理器管理的生态根**（存在 `.skill-lock.json` 的那个）；
  3. 再退到「已存在且可写」的用户级技能根；
  4. 最后才退到清单里的第一个候选。

装完还会做一次**可见性自检**（`visibility_report`）：若目标根不是当前 agent 的根、
或不是本机的生态根，会明确提示「这个位置可能不会被你的 agent 发现」。

支持的宿主（自动探测；`--target <名字>` 可显式指定）
--------------------------------------------------
  agents      通用 agents 技能根（DSH 等读取此处）
  claude      [CC]（受 `CLAUDE_CONFIG_DIR` 覆盖）
  codex       Codex（受 `CODEX_HOME` 覆盖）
  opencode    opencode / omp（受 `XDG_CONFIG_HOME` 覆盖）
  codebuddy   CodeBuddy
  workbuddy   WorkBuddy
  qoder       Qoder
  goose       Goose（受 `XDG_CONFIG_HOME` 覆盖）
  crush       Crush（受 `XDG_CONFIG_HOME` 覆盖）
  devin       Devin（受 `XDG_CONFIG_HOME` 覆盖）
  zcode       ZCode
  openclaw    OpenClaw
  hermes      Hermes

用法
----
  <PYTHON> install.py targets             # 看看能装到哪、当前 agent 是哪个
  <PYTHON> install.py install --dry-run   # 只预览，不落盘
  <PYTHON> install.py install             # 装到「当前 agent 的技能根」
  <PYTHON> install.py install --target claude
  <PYTHON> install.py install --link      # 建链接（跟本机既有惯例一致）
  <PYTHON> install.py check --all         # 已安装的副本是否与源一致
  <PYTHON> install.py validate            # skill 目录形态是否标准
  <PYTHON> install.py package             # 产出 noova-generation.zip
  <PYTHON> install.py install --target project
  <PYTHON> install.py install --target "D:/my-skills"
  <PYTHON> install.py uninstall --yes

（`<PYTHON>` = 你运行本脚本时用的那个 Python，提示里会替换成它的绝对路径）

源目录定位
----------
  默认按本脚本位置推断：`<脚本所在目录>/../skills/noova-generation`。
  若脚本被移到别处，用 `--source <skill 目录>` 或环境变量 `NOOVA_SKILL_SOURCE` 指定。
  （`--source` 放在子命令前或后都可以。）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

PACKAGE_NAME = "noova-generation"

# 源目录：本脚本位于仓库根，skill 在 <repo>/skills/<name>/
_DEFAULT_SKILL_ROOT = Path(__file__).resolve().parent / "skills" / PACKAGE_NAME
SKILL_ROOT = _DEFAULT_SKILL_ROOT

MANIFEST_NAME = ".install-manifest.json"

# 解释器路径：不猜 PATH 里的名字（python / python3 / py 各不相同，Windows 上
# `python3` 还常被 Microsoft Store 占位符占用）。直接用当前进程的解释器绝对路径，
# 生成的命令不依赖 PATH，与 skill 内部 `noova_key.python_cmd` 同一策略。
PYTHON_CMD = Path(sys.executable).resolve().as_posix() if sys.executable else ""


def python_cmd(script: "Path | str") -> str:
    """拼一条用户可直接复制的命令（解释器绝对路径 + 脚本绝对路径）。"""
    interpreter = PYTHON_CMD or "python3"
    return f'"{interpreter}" "{Path(script).resolve().as_posix()}"'


# 给用户复制的命令：安装器在仓库根，**不在 skill 目录里**，
# 所以提示里必须用本脚本自身的真实路径，不能拼成 <skill>/install.py。
SELF_INVOCATION = python_cmd(Path(__file__).resolve())

# 分发**白名单**：只有这些顶层条目下的文件会被复制/打包。
# 用白名单而不是黑名单：黑名单永远会漏——`.DS_Store` / `Thumbs.db` / `*.bak` /
# `*.swp` / `notes.txt` 都曾会被静默打包进分发物。
DISTRIBUTABLE_TOP_LEVEL = {"SKILL.md", "scripts", "references", "assets"}

# 白名单内部仍需排除的目录/后缀（本地配置、字节码缓存、编辑器残留）
SKIP_DIRS = {"__pycache__", ".noova", ".git", ".idea", ".vscode", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".py~", ".orig", ".bak", ".swp", ".tmp", ".log")

# 标准 skill 允许的顶层条目（官方 skill-creator 规范：SKILL.md 必需，其余三种可选）
STANDARD_TOP_LEVEL = {"SKILL.md", "scripts", "references", "assets"}

KEEP_IN_DEST = {MANIFEST_NAME}

# 安装后可见性自检的退出码/提示阈值
LINK_KIND_LABEL = "链接"


# ---------------------------------------------------------------------------
# 宿主探测：装到哪里，由「装给哪个 agent」决定
# ---------------------------------------------------------------------------
# 每一项：(host, 展示名, 环境变量覆盖, 用户级相对路径, 项目级相对路径)
#   · 环境变量覆盖形如 ("CODEX_HOME", ("skills",)) → `$CODEX_HOME/skills`
#   · 项目级为 None 表示该宿主没有项目级技能根
AGENT_SPECS: tuple = (
    ("agents", "通用 agents 技能根（DSH 等读取此处）",
     None, (".agents", "skills"), (".agents", "skills")),
    ("claude", "[CC]",
     ("CLAUDE_CONFIG_DIR", ("skills",)), (".claude", "skills"), (".claude", "skills")),
    ("codex", "Codex",
     ("CODEX_HOME", ("skills",)), (".codex", "skills"), (".codex", "skills")),
    ("opencode", "opencode / omp",
     ("XDG_CONFIG_HOME", ("opencode", "skills")),
     (".config", "opencode", "skills"), (".opencode", "skills")),
    ("codebuddy", "CodeBuddy",
     None, (".codebuddy", "skills"), (".codebuddy", "skills")),
    ("workbuddy", "WorkBuddy",
     None, (".workbuddy", "skills"), (".workbuddy", "skills")),
    ("qoder", "Qoder",
     None, (".qoder", "skills"), (".qoder", "skills")),
    ("goose", "Goose",
     ("XDG_CONFIG_HOME", ("goose", "skills")), (".config", "goose", "skills"), None),
    ("crush", "Crush",
     ("XDG_CONFIG_HOME", ("crush", "skills")), (".config", "crush", "skills"), None),
    ("devin", "Devin",
     ("XDG_CONFIG_HOME", ("devin", "skills")), (".config", "devin", "skills"), None),
    ("zcode", "ZCode",
     None, (".zcode", "skills"), None),
    ("openclaw", "OpenClaw",
     None, (".openclaw", "skills"), None),
    ("hermes", "Hermes",
     None, (".hermes", "skills"), None),
)

# 当前正在运行哪个 agent：**强特征**（该 agent 自己注入、别人不会设的变量）。
# 顺序即优先级：越靠前越具体。
HOST_MARKERS: tuple = (
    ("agents", ("DSH_SHELL", "DSH_SESSION_ID", "DSH_HOME", "DSH_WEB_URL")),
    ("claude", ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")),
    ("codex", ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")),
    ("opencode", ("OPENCODE_BIN", "OPENCODE_CONFIG")),
    ("codebuddy", ("CODEBUDDY_SESSION", "CODEBUDDY_HOME")),
    ("workbuddy", ("WORKBUDDY_SESSION", "WORKBUDDY_HOME")),
    ("qoder", ("QODER_SESSION", "QODER_HOME")),
)

# **弱特征**：用户可能在 shell profile 里全局导出，别人家的 agent 也会继承到。
# 只有强特征全都探测不到时才使用。
WEAK_HOST_MARKERS: tuple = (
    ("claude", ("CLAUDE_CONFIG_DIR",)),
    ("codex", ("CODEX_HOME",)),
    ("opencode", ("OPENCODE_CONFIG",)),
)


def detect_host() -> dict | None:
    """探测**当前正在运行**的 agent（决定 `--target auto` 装到哪里）。

    返回 `{"host", "label", "root", "marker"}`；探测不到返回 None。

    为什么必须做这件事：每个 agent 的技能根不同，装错位置就是「装完了它读不到」。
    优先看强特征——那是 agent 为本次进程注入的变量，别的 agent 不会设。
    """
    for host, names in HOST_MARKERS:
        for name in names:
            if os.environ.get(name, "").strip():
                return _host_info(host, name)
    for host, names in WEAK_HOST_MARKERS:
        for name in names:
            if os.environ.get(name, "").strip():
                return _host_info(host, name)
    return None


def _host_info(host: str, marker: str) -> dict:
    spec = next((s for s in AGENT_SPECS if s[0] == host), None)
    label = spec[1] if spec else host
    root = _root_for_host(host, "user", Path.home(), Path.cwd())
    return {"host": host, "label": label, "root": str(root) if root else "",
            "marker": marker}


def _root_for_host(host: str, kind: str, home: Path, ws: Path) -> Path | None:
    """按宿主 + 级别算出技能根（含环境变量覆盖）。"""
    spec = next((s for s in AGENT_SPECS if s[0] == host), None)
    if spec is None:
        return None
    _, _, env_override, user_parts, project_parts = spec
    parts = user_parts if kind == "user" else project_parts
    if parts is None:
        return None
    if kind == "user" and env_override:
        env_name, env_suffix = env_override
        base = os.environ.get(env_name, "").strip()
        if base:
            return Path(base).expanduser() / Path(*env_suffix)
    base_dir = home if kind == "user" else ws
    return base_dir / Path(*parts)


def resolve_skill_root(explicit: str | None = None) -> Path:
    """定位 skill 源目录。

    优先级：`--source` 参数 > 环境变量 `NOOVA_SKILL_SOURCE` > 按脚本位置推断。
    本脚本可被移到任意位置；一旦推断不到，就必须显式指定。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("NOOVA_SKILL_SOURCE")
    if env:
        return Path(env).expanduser().resolve()
    return _DEFAULT_SKILL_ROOT


# ---------------------------------------------------------------------------
# 链接/联接识别与安全删除
# ---------------------------------------------------------------------------
# 本机生态大量使用 junction（实测 `~/.claude/skills` 74 项里 72 项是指向
# `~/.agents/skills` 的 junction），因此这两件事必须做对：
#   ① 认出 junction；② 删除时**只解除链接**，绝不递归删进目标目录。

def _is_link(path: Path) -> bool:
    """判断路径是否是符号链接或 Windows 目录联接（junction）。

    注意：Python 3.8–3.11 的 `Path.is_symlink()` 与 `os.path.islink()` 对 junction
    都返回 **False**（实测 3.11.5）；`os.path.isjunction()` 直到 3.12 才存在。
    唯一可靠的判据是 `os.readlink()`——它对符号链接与 junction 都成功。
    """
    try:
        os.readlink(str(path))
    except (OSError, ValueError, AttributeError):
        return False
    return True


def _link_target(path: Path) -> Path | None:
    """读出链接/联接指向的目标（junction 会带 `\\\\?\\` 前缀，需剥掉）。"""
    try:
        raw = os.readlink(str(path))
    except (OSError, ValueError, AttributeError):
        return None
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    try:
        return Path(raw)
    except (OSError, ValueError):
        return None


def _remove_path(path: Path) -> None:
    """删除一个路径；**链接/联接只解除链接本身**。

    `shutil.rmtree` 对 junction 直接抛 `OSError: Cannot call rmtree on a symbolic
    link`（实测），于是「链接式安装」会变成**不可卸载**。
    """
    if _is_link(path):
        for remover in (os.unlink, os.rmdir):
            try:
                remover(str(path))
                return
            except OSError:
                continue
        raise OSError(f"无法解除链接：{path}")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _atomic_copy(src: Path, dst: Path) -> None:
    """**原子**复制单个文件：先写同目录临时文件，再 `os.replace`。

    `shutil.copyfile` 是**先截断目标再写**。写到一半失败（Windows 上目标被编辑器 /
    杀软 / agent 自身占用，或磁盘满）就留下一个被截断的脚本，而且没有备份——
    已安装副本会以「文件存在但内容残缺」的状态留在用户机器上。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".noova-tmp")
    try:
        shutil.copyfile(src, tmp)
        os.replace(str(tmp), str(dst))
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _make_link(target: Path, link_path: Path) -> None:
    """把 `link_path` 建成指向 `target` 的目录链接（Windows 用 junction）。

    与本机既有惯例一致：非 `.agents` 的技能根都是指向 `.agents` 的 junction。
    链接式安装的优点是「源一改就生效」，代价是卸载时源目录不能删。
    """
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target)],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise OSError(f"创建目录联接失败：{(result.stderr or result.stdout or '').strip()}")
    else:
        os.symlink(str(target), str(link_path), target_is_directory=True)


# ---------------------------------------------------------------------------
# 目标探测
# ---------------------------------------------------------------------------

def _is_writable(path: Path) -> bool:
    """判断目录（或可创建的最邻近上级）是否可写。

    某些技能根里存在**断链符号链接**（实测 `~/.claude/skills/<name>` 在 Windows 上会抛
    `OSError: [WinError 1920]`），因此所有文件系统调用都必须容错，否则探测阶段就会崩。
    """
    probe = Path(path)
    while True:
        try:
            if probe.exists():
                return os.access(probe, os.W_OK)
            if probe.parent == probe:
                return False
            probe = probe.parent
        except OSError:
            # 断链/无权限路径：向上退一级继续判断
            if probe.parent == probe:
                return False
            probe = probe.parent


def _within(path: Path, parent: Path) -> bool:
    """path 是否等于 parent 或位于 parent 之下。"""
    return path == parent or parent in path.parents


def _target(kind: str, host: str, root: Path, label: str, priority: int) -> dict:
    root = Path(root)
    blocked = False
    blocked_reason = ""
    host_exists = False
    installed = False
    linked = False
    try:
        blocked = _within((root / PACKAGE_NAME).resolve(), SKILL_ROOT)
        if blocked:
            blocked_reason = ("目标位于 skill 源目录内部，会把 skill 装进自己里；"
                              "请 cd 到你的项目根目录，或用 --workspace 指定")
        host_exists = root.parent.exists()
        installed = (root / PACKAGE_NAME / "SKILL.md").is_file()
        linked = _is_link(root / PACKAGE_NAME)
    except OSError:
        # 断链符号链接或权限不足：标记为不可用，而不是让整个探测崩掉
        blocked = True
        blocked_reason = "路径不可访问（可能是断链符号链接或权限不足）"
    return {
        "kind": kind,
        "host": host,
        "root": str(root),
        "label": label,
        "priority": priority,
        "host_exists": host_exists,
        "installed": installed,
        "linked": linked,
        "writable": _is_writable(root),
        "blocked": blocked,
        "blocked_reason": blocked_reason,
    }


def detect_targets(workspace: str | None = None) -> list[dict]:
    """探测全部候选技能根（用户级在前，项目级在后）。"""
    home = Path.home()
    ws = Path(workspace).expanduser().resolve() if workspace else Path.cwd()
    targets: list[dict] = []
    for index, (host, label, _env, user_parts, project_parts) in enumerate(AGENT_SPECS, start=1):
        user_root = _root_for_host(host, "user", home, ws)
        if user_root is not None:
            targets.append(_target("user", host, user_root, f"{label} 用户级", index))
    for index, (host, label, _env, user_parts, project_parts) in enumerate(AGENT_SPECS, start=1):
        project_root = _root_for_host(host, "project", home, ws)
        if project_root is not None:
            targets.append(_target("project", host, project_root,
                                   f"{label} 项目级（仅当前工作区）", 100 + index))
    return targets


def ecosystem_root(targets: list[dict]) -> dict | None:
    """找出本机**被技能管理器管理的技能根**。

    判据（按可靠性排序）：

    1. 技能根的**父目录**存在 `.skill-lock.json` —— 这是技能管理器自己的账本
       （本机 `~/.agents/.skill-lock.json` 就是），装在它管辖的根里才会被更新/清理；
    2. 退而求其次：统计「其它技能根里有多少条链接指向本根」——本机非 `.agents`
       的技能根**全部**是指向 `.agents` 的 junction，扇入最多的那个就是真实根。

    为什么需要它：装到非生态根会留下一份**无人管理的副本**——技能管理器不会更新它、
    也不会清理它，而用户以为「装好了」。
    """
    usable = [t for t in targets if not t["blocked"]]
    managed = []
    for item in usable:
        try:
            if (Path(item["root"]).parent / ".skill-lock.json").is_file():
                managed.append(item)
        except OSError:
            continue
    if managed:
        return sorted(managed, key=lambda t: t["priority"])[0]

    fan_in: dict[str, int] = {}
    for item in usable:
        root = Path(item["root"])
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if not _is_link(entry):
                    continue
                target = _link_target(entry)
                if target is None:
                    continue
                resolved = target.resolve() if target.exists() else target
                for other in usable:
                    other_root = Path(other["root"])
                    try:
                        if resolved == other_root.resolve() or other_root.resolve() in resolved.parents:
                            fan_in[other["root"]] = fan_in.get(other["root"], 0) + 1
                    except OSError:
                        continue
        except OSError:
            continue
    if fan_in:
        best = max(fan_in.items(), key=lambda kv: kv[1])[0]
        return next((t for t in usable if t["root"] == best), None)
    return None


def resolve_target(targets: list[dict], target: str) -> dict:
    """把 `--target` 的值解析成确定的目标（跳过被阻塞的位置）。

    取值：`auto`（默认）| `user` | `project` | 宿主名 | 任意目录路径。
    """
    value = str(target or "auto").strip()
    usable = [item for item in targets if not item["blocked"]]
    detected = detect_host()

    def _preferred(kind: str) -> dict | None:
        """该级别下最该用的目标：当前 agent 的 > 生态根 > 已存在且可写 > 第一个。"""
        if detected:
            hit = next((t for t in usable
                        if t["kind"] == kind and t["host"] == detected["host"]), None)
            if hit:
                return hit
        eco = ecosystem_root(usable)
        if eco and eco["kind"] == kind:
            return eco
        for item in usable:
            if item["kind"] == kind and item["host_exists"] and item["writable"]:
                return item
        return next((t for t in usable if t["kind"] == kind), None)

    if value in ("auto", ""):
        return _preferred("user") or (usable[0] if usable else targets[0])

    if value in ("user", "project"):
        chosen = _preferred(value)
        if chosen:
            return chosen
        return next((item for item in usable if item["kind"] == value),
                    next(item for item in targets if item["kind"] == value))

    if value in {item["host"] for item in targets}:
        # 宿主名：优先用户级（所有项目可用），退回项目级
        return (next((item for item in usable
                      if item["host"] == value and item["kind"] == "user"), None)
                or next((item for item in usable
                         if item["host"] == value and item["kind"] == "project"), None)
                or next(item for item in targets if item["host"] == value))

    # 显式目录。宿主关键字优先（避免 `--target claude` 被当成 ./claude 目录）；
    # 想装到名叫 claude 的目录，请给明确路径（`./claude` 或绝对路径）。
    custom = Path(value).expanduser()
    if not custom.is_absolute() and not value.replace("\\", "/").startswith(("./", "../", "/")):
        custom = Path.cwd() / custom
    return _target("custom", "custom", custom.resolve(), "自定义目录", 0)


def install_dir(target: dict) -> Path:
    """目标 skills 根目录下的本 skill 目录。"""
    return Path(target["root"]).expanduser() / PACKAGE_NAME


def visibility_report(chosen: dict, targets: list[dict]) -> dict:
    """安装后自检：这个位置**真的会被 agent 发现**吗？

    这是本工具最容易被忽略、后果又最严重的一件事：装到读不到的位置时，安装本身
    完全成功、`check` 也报「已安装且一致」，用户与 agent 双方都看不到任何错误，
    而 skill 永远不生效。因此这里主动核对两件事：

    1. 目标根是不是**当前 agent 的技能根**；
    2. 目标根是不是**本机被管理的生态根**（否则副本无人更新、无人清理）。
    """
    notes: list[str] = []
    ok = True
    detected = detect_host()
    eco = ecosystem_root(targets)
    if detected and chosen["kind"] == "user" and chosen["host"] != detected["host"]:
        ok = False
        notes.append(
            f"当前运行的是「{detected['label']}」，它的技能根是 {detected['root']}；"
            f"而本次装到了 {chosen['root']}（{chosen['label']}）——"
            f"该 agent 很可能**发现不了**这个 skill。"
            f"改用：--target {detected['host']}")
    if eco and chosen["root"] != eco["root"] and chosen["kind"] == "user":
        notes.append(
            f"本机被技能管理器管理的根是 {eco['root']}（存在 .skill-lock.json）；"
            f"{chosen['root']} 里的副本不在它的管理范围内，"
            f"不会被自动更新或清理（用 --target {eco['host']} 可装到生态根）")
    return {
        "ok": ok,
        "notes": notes,
        "detected_host": detected["host"] if detected else None,
        "detected_host_root": detected["root"] if detected else None,
        "ecosystem_root": eco["root"] if eco else None,
    }


# ---------------------------------------------------------------------------
# 文件清单与比对
# ---------------------------------------------------------------------------

def _skip(rel: str) -> bool:
    """该相对路径是否**不参与分发**。

    白名单优先：顶层条目不在 `DISTRIBUTABLE_TOP_LEVEL` 里的一律不分发。
    这样 `notes.txt` / `.DS_Store` / `Thumbs.db` 之类不会溜进分发物。
    """
    parts = Path(rel).parts
    if not parts:
        return True
    if parts[0] not in DISTRIBUTABLE_TOP_LEVEL:
        return True
    if any(part in SKIP_DIRS for part in parts):
        return True
    # 安装器自身的状态文件不参与分发（否则每次安装都会"变化"，永远不同步）
    if Path(rel).name == MANIFEST_NAME:
        return True
    return rel.endswith(SKIP_SUFFIXES)


def collect_source() -> dict[str, Path]:
    """收集源目录中应分发的文件 → {相对路径: 绝对路径}。"""
    files: dict[str, Path] = {}
    for path in sorted(SKILL_ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(SKILL_ROOT).as_posix()
        if _skip(rel):
            continue
        files[rel] = path
    return files


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def compare(src: dict[str, Path], dst_dir: Path) -> dict:
    """比对源与目标，返回 added / changed / identical / stale / missing。"""
    result = {"added": [], "changed": [], "identical": [], "stale": [], "missing": []}
    if not dst_dir.is_dir():
        result["added"] = sorted(src)
        result["missing"] = sorted(src)
        return result

    for rel, path in sorted(src.items()):
        target = dst_dir / rel
        if not target.is_file():
            result["added"].append(rel)
        elif _digest(target) != _digest(path):
            result["changed"].append(rel)
        else:
            result["identical"].append(rel)

    for path in sorted(dst_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(dst_dir).as_posix()
        if rel in KEEP_IN_DEST or _skip(rel) or rel in src:
            continue
        result["stale"].append(rel)

    # 非标准顶层条目整体清理（历史版本可能把安装器/测试留在了 skill 目录里）。
    # 逐文件比对会漏掉被 `_skip()` 排除的目录（如 tests/），所以这里按顶层条目单独判定；
    # 目的是让已安装副本与「标准形态」保持一致，而不只是「文件内容一致」。
    for entry in sorted(dst_dir.iterdir()):
        name = entry.name
        if name.startswith(".") or name in STANDARD_TOP_LEVEL:
            continue
        if entry.is_dir():
            prefix = f"{name}/"
            result["stale"] = [r for r in result["stale"] if not r.startswith(prefix)]
            result["stale"].append(prefix)
        elif name not in result["stale"]:
            result["stale"].append(name)
    result["stale"] = sorted(set(result["stale"]))
    return result


def read_manifest(dst_dir: Path) -> dict:
    try:
        return json.loads((dst_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _read_frontmatter_field(skill_dir: Path, field: str) -> str:
    """读取 SKILL.md frontmatter 中某个字段的值（支持 YAML 块标量）。"""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    lines = text[3:end].splitlines()
    for idx, line in enumerate(lines):
        key, sep, value = line.partition(":")
        if not sep or key.strip().lower() != field.lower():
            continue
        value = value.strip()
        if value in (">", "|", ">-", "|-", ">+", "|+"):
            collected = []
            for follow in lines[idx + 1:]:
                if follow.strip() and not follow.startswith((" ", "\t")):
                    break
                collected.append(follow.strip())
            return " ".join(part for part in collected if part)
        return value.strip("\"'")
    return ""


def read_skill_name(skill_dir: Path) -> str:
    """读取 SKILL.md frontmatter 的 name 字段。"""
    return _read_frontmatter_field(skill_dir, "name")


def validate_skill() -> dict:
    """校验 skill 目录是否符合标准形态。

    检查项对标官方 skill-creator 规范：
      · 必需 `SKILL.md`
      · frontmatter 必须含 `name` 与 `description`（前者标识、后者决定何时触发）
      · `name` 应与目录名一致
      · 顶层只允许 `SKILL.md` / `scripts/` / `references/` / `assets/`
        （超出者为「非标准条目」，只警告不阻断——规范允许但会显得不专业）
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not (SKILL_ROOT / "SKILL.md").is_file():
        errors.append("缺少必需的 SKILL.md")

    name = read_skill_name(SKILL_ROOT)
    if not name:
        errors.append("SKILL.md frontmatter 缺少 name 字段")
    elif name != PACKAGE_NAME:
        warnings.append(f'frontmatter name="{name}" 与目录名 "{PACKAGE_NAME}" 不一致')

    if not _read_frontmatter_field(SKILL_ROOT, "description"):
        errors.append("SKILL.md frontmatter 缺少 description 字段（它决定何时触发本 skill）")

    if SKILL_ROOT.is_dir():
        for entry in sorted(SKILL_ROOT.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.name not in STANDARD_TOP_LEVEL:
                warnings.append(
                    f"顶层存在非标准条目：{entry.name}"
                    f"（规范只允许 {'/'.join(sorted(STANDARD_TOP_LEVEL))}）")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_validate(args) -> int:
    result = validate_skill()
    result["source"] = str(SKILL_ROOT)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    print(f"源目录：{SKILL_ROOT}")
    for item in result["errors"]:
        print(f"[!!] {item}")
    for item in result["warnings"]:
        print(f"[--] {item}")
    if not result["ok"]:
        print("[!!] 不符合标准形态，请先修复上面的错误", file=sys.stderr)
        return 1
    suffix = f"（{len(result['warnings'])} 条非阻断提示）" if result["warnings"] else ""
    print(f"[OK] skill 目录符合标准形态{suffix}")
    return 0


def cmd_package(args) -> int:
    """把 skill 打成 zip，便于分发到别的机器/客户端。"""
    result = validate_skill()
    if not result["ok"]:
        for item in result["errors"]:
            print(f"[!!] {item}", file=sys.stderr)
        print("[!!] 校验未通过，未生成压缩包", file=sys.stderr)
        return 1
    for item in result["warnings"]:
        print(f"[--] {item}", file=sys.stderr)

    files = collect_source()
    out = (Path(args.output).expanduser().resolve() if args.output
           else Path.cwd() / f"{PACKAGE_NAME}.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(files):
            archive.write(SKILL_ROOT / rel, f"{PACKAGE_NAME}/{rel}")
    size_kb = out.stat().st_size / 1024
    print(f"[OK] 已打包：{out}（{len(files)} 个文件，{size_kb:.1f} KB）")
    print(f"     解压后把 {PACKAGE_NAME}/ 整个目录放进客户端的技能目录即可；")
    print(f"     也可直接安装解压出的目录：{SELF_INVOCATION} --source <解压出的目录> install")
    return 0


def cmd_targets(args) -> int:
    targets = detect_targets(args.workspace)
    chosen = resolve_target(targets, args.target)
    detected = detect_host()
    eco = ecosystem_root(targets)
    if args.json:
        print(json.dumps({"chosen": chosen, "targets": targets, "source": str(SKILL_ROOT),
                          "detected_host": detected,
                          "ecosystem_root": eco["root"] if eco else None},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"源目录：{SKILL_ROOT}")
    if detected:
        print(f"当前 agent：{detected['label']}（依据环境变量 {detected['marker']}）"
              f"→ 技能根 {detected['root']}")
    else:
        print("当前 agent：未探测到（将按生态根/已存在的技能根选择）")
    if eco:
        print(f"生态根（被技能管理器管理）：{eco['root']}")
    print("可选安装位置：")
    for item in targets:
        marks = []
        marks.append("宿主存在" if item["host_exists"] else "宿主不存在")
        marks.append("可写" if item["writable"] else "不可写")
        if item["installed"]:
            marks.append("已安装（链接）" if item["linked"] else "已安装")
        if item["blocked"]:
            marks.append("不可用")
        if detected and item["host"] == detected["host"] and item["kind"] == "user":
            marks.append("← 当前 agent 的技能根")
        if eco and item["root"] == eco["root"]:
            marks.append("← 生态根")
        flag = "←（自动选择）" if item is chosen else ""
        print(f"  [{item['priority']:>3}] {item['label']}")
        print(f"        {item['root']}    ({'，'.join(marks)}) {flag}")
        if item["blocked"]:
            print(f"        → {item['blocked_reason']}")
    return 0


def _manifest_source() -> str:
    """manifest 里的来源标识。

    **绝不写开发者的绝对路径**：manifest 会留在**用户**的技能目录里，把
    `D:\\workSpace\\...` 之类的内部路径写进去，与「不泄露内部路径」的约定相悖。
    只有当源目录确实位于**当前用户自己的家目录下**时才记录真实路径。
    """
    try:
        if _within(SKILL_ROOT.resolve(), Path.home().resolve()):
            return str(SKILL_ROOT)
    except OSError:
        pass
    return f"{PACKAGE_NAME}（分发包）"


def _write_manifest(dst_dir: Path, src: dict[str, Path]) -> None:
    payload = {
        "package": PACKAGE_NAME,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "source": _manifest_source(),
        "file_count": len(src),
        "files": {rel: _digest(path) for rel, path in sorted(src.items())},
    }
    (dst_dir / MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _installed_identity_ok(dst_dir: Path) -> tuple[bool, str]:
    """目标目录是否**确实属于本 skill**（install 与 uninstall 用同一套判据）。

    两个方向都要防：

    - **太松**：曾经的 install 守卫只在「有 SKILL.md 且 frontmatter 有 name」时才生效，
      于是「目录存在但 SKILL.md 缺失/无 name」会跳过守卫，随后把「陈旧顶层条目」
      全部删掉——等于在别人的目录里做清理。
    - **太紧**：只用 `name` 判定会把「被改坏的已安装副本」当成别人的 skill 拒绝修复，
      而 `install` 的正当用途之一恰恰就是**修复漂移的副本**。

    因此这里看**多个信号**，只有在拿到「明确属于别人」的证据时才拒绝：
    有 name 且不等于本 skill；或既没有本 skill 的任何痕迹、又没有可确认的身份。
    """
    if not dst_dir.is_dir():
        return True, ""

    name = read_skill_name(dst_dir)
    if name == PACKAGE_NAME:
        return True, ""

    # 名字读不出来（SKILL.md 缺失 / 损坏 / 无 name）时，找本 skill 的其它痕迹。
    manifest = read_manifest(dst_dir)
    if str(manifest.get("package") or "") == PACKAGE_NAME:
        return True, ""          # 我们自己写的安装清单
    scripts = dst_dir / "scripts"
    if (scripts / "noova_media.py").is_file() and (scripts / "noova_key.py").is_file():
        return True, ""          # 本 skill 的脚本组合

    if name:
        return False, f"目标目录的 SKILL.md 声明的 name 是「{name}」"
    if (dst_dir / "SKILL.md").is_file():
        return False, "目标目录的 SKILL.md 缺少 name 字段，且没有本 skill 的任何痕迹"
    return False, "目标目录已存在但没有 SKILL.md"


def cmd_install(args) -> int:
    targets = detect_targets(args.workspace)
    target = resolve_target(targets, args.target)
    dst_dir = install_dir(target)
    src = collect_source()

    if "SKILL.md" not in src:
        print(f"错误：源目录缺少 SKILL.md：{SKILL_ROOT}", file=sys.stderr)
        return 2

    # 源目录形态校验：`package` 一直会做，`install` 曾经不做——于是缺 description
    # 的源可以被静默安装，用户得到一个「永远不会被触发」的 skill。
    shape = validate_skill()
    if not shape["ok"]:
        for item in shape["errors"]:
            print(f"[!!] {item}", file=sys.stderr)
        print("[!!] 源目录不符合标准形态，已中止安装（可先用 validate 查看）", file=sys.stderr)
        return 2
    if not args.json:
        for item in shape["warnings"]:
            print(f"[--] {item}", file=sys.stderr)

    if target["blocked"]:
        print(f"错误：拒绝安装到 {dst_dir}", file=sys.stderr)
        print(f"      {target['blocked_reason']}", file=sys.stderr)
        return 2

    # 归属守卫（与 uninstall 同一判据）
    owned, why = _installed_identity_ok(dst_dir)
    if not owned and not args.force:
        print(f"错误：{dst_dir} 已存在，但{why}，与本 skill 不一致。", file=sys.stderr)
        print("      如确认要覆盖，请加 --force。", file=sys.stderr)
        return 2

    plan = compare(src, dst_dir)
    changed_total = len(plan["added"]) + len(plan["changed"]) + len(plan["stale"])

    if not target["writable"] and not args.dry_run:
        print(f"错误：目标目录不可写：{target['root']}", file=sys.stderr)
        print(f"      可改用：{SELF_INVOCATION} install --target project", file=sys.stderr)
        return 2

    visibility = visibility_report(target, targets)
    summary = {
        "action": "install",
        "target": target["label"],
        "host": target["host"],
        "kind": target["kind"],
        "install_path": str(dst_dir),
        "source": str(SKILL_ROOT),
        "mode": LINK_KIND_LABEL if args.link else "复制",
        "dry_run": bool(args.dry_run),
        "added": plan["added"],
        "changed": plan["changed"],
        "stale": plan["stale"],
        "unchanged": len(plan["identical"]),
        "up_to_date": changed_total == 0 and dst_dir.is_dir(),
        "visibility": visibility,
    }

    if args.json and args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if not args.json:
        print(f"源目录  ：{SKILL_ROOT}")
        print(f"安装到  ：{dst_dir}    （{target['label']}）")
        print(f"方式    ：{'目录链接（源一改即生效）' if args.link else '复制文件'}")
        print(f"变更    ：新增 {len(plan['added'])}，更新 {len(plan['changed'])}，"
              f"清理 {len(plan['stale'])}，未变 {len(plan['identical'])}")
        for rel in plan["added"]:
            print(f"  + {rel}")
        for rel in plan["changed"]:
            print(f"  ~ {rel}")
        for rel in plan["stale"]:
            print(f"  - {rel}")

    if args.dry_run:
        if not args.json:
            print("（预览模式，未写入任何文件）")
            for note in visibility["notes"]:
                print(f"[注意] {note}", file=sys.stderr)
        summary["next_commands"] = [
            python_cmd(dst_dir / "scripts" / "noova_key.py") + " status --json",
        ]
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.link:
        # 链接式安装：源目录必须长期存在，否则链接会变成断链。
        if _is_link(dst_dir):
            _remove_path(dst_dir)
        elif dst_dir.is_dir():
            try:
                shutil.rmtree(dst_dir)
            except OSError as exc:
                print(f"错误：无法清理既有目录以便建链接：{exc}", file=sys.stderr)
                return 2
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            _make_link(SKILL_ROOT, dst_dir)
        except OSError as exc:
            print(f"错误：创建链接失败：{exc}", file=sys.stderr)
            print("      可去掉 --link 改用复制安装。", file=sys.stderr)
            return 2
        removed = []
        # 链接式安装不写 manifest：manifest 会落进**源目录**，污染仓库。
    else:
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            for rel in plan["added"] + plan["changed"]:
                _atomic_copy(src[rel], dst_dir / rel)
        except OSError as exc:
            # 逐文件原子写已保证不会留下被截断的脚本；这里只需一句话说清失败原因，
            # 绝不让 traceback（含安装器绝对路径）打到用户面前。
            print(f"错误：写入失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
            print("      已安装副本可能处于部分更新状态；请关闭占用该目录的程序"
                  "（编辑器 / 杀软 / agent 自身）后重新执行 install。", file=sys.stderr)
            return 2
        removed = []
        for rel in plan["stale"]:
            target_path = dst_dir / rel.rstrip("/")
            try:
                _remove_path(target_path)
                removed.append(rel)
            except OSError as exc:
                print(f"[警告] 无法清理陈旧文件 {rel}：{exc}", file=sys.stderr)
        try:
            _write_manifest(dst_dir, src)
        except OSError as exc:
            print(f"错误：写入安装清单失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
            return 2

    key_script = (dst_dir / "scripts" / "noova_key.py").as_posix()
    summary.update({
        "removed": removed,
        "next_steps": [
            "让 agent 重新加载技能列表（重启会话或刷新技能目录），即可发现该 skill",
            f'{python_cmd(key_script)} status --json     # 检查配置状态',
            f'{python_cmd(key_script)} doctor            # 环境自检（是否可直接使用）',
        ],
        "note": "本 skill 目录由本安装器维护；若你使用其它技能同步/清理工具，"
                "请把它加入白名单，避免被误清理。",
    })

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print("")
    if changed_total == 0 and dst_dir.is_dir():
        print("[OK] 已安装且与源完全一致，无需更新。")
    else:
        print("[OK] 安装完成。")
    if not visibility["ok"]:
        print("")
        print("[!!] 可见性自检未通过 —— 装到了可能读不到的位置：", file=sys.stderr)
        for note in visibility["notes"]:
            print(f"     {note}", file=sys.stderr)
    elif visibility["notes"]:
        print("")
        for note in visibility["notes"]:
            print(f"[注意] {note}")
    print("下一步：")
    for step in summary["next_steps"]:
        print(f"  · {step}")
    print(f"  · {summary['note']}")
    return 0


def cmd_check(args) -> int:
    targets = detect_targets(args.workspace)
    chosen = resolve_target(targets, args.target)
    src = collect_source()
    detected = detect_host()
    payload = {"source": str(SKILL_ROOT), "target": chosen["label"], "host": chosen["host"],
               "detected_host": detected["host"] if detected else None,
               "visibility": visibility_report(chosen, targets)}
    results = []

    scan_all = bool(args.all)
    items = targets if scan_all else [chosen]
    for item in items:
        dst_dir = install_dir(item)
        linked = _is_link(dst_dir)
        installed = (dst_dir / "SKILL.md").is_file()
        entry = {
            "host": item["host"], "kind": item["kind"], "label": item["label"],
            "install_path": str(dst_dir), "installed": installed, "linked": linked,
        }
        if installed:
            plan = compare(src, dst_dir)
            manifest = read_manifest(dst_dir)
            entry.update({
                "in_sync": not (plan["added"] or plan["changed"] or plan["stale"]),
                "changed": plan["changed"],
                "missing": plan["added"],
                "stale": plan["stale"],
                "installed_at": manifest.get("installed_at"),
                "installed_source": manifest.get("source"),
                "skill_name": read_skill_name(dst_dir),
            })
        results.append(entry)

    payload["results"] = results
    payload["ok"] = all(r.get("in_sync") for r in results if r["installed"]) and any(
        r["installed"] for r in results)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1

    for entry in results:
        if not entry["installed"]:
            print(f"[--] 未安装：{entry['label']}  {entry['install_path']}")
            continue
        mode = "（链接）" if entry["linked"] else ""
        if entry["in_sync"]:
            print(f"[OK] 已安装且一致{mode}：{entry['install_path']}"
                  + (f"（安装于 {entry['installed_at']}）" if entry["installed_at"] else ""))
        else:
            print(f"[!!] 已安装但需更新{mode}：{entry['install_path']}")
            for rel in entry["missing"]:
                print(f"      缺少 {rel}")
            for rel in entry["changed"]:
                print(f"      已过期 {rel}")
            for rel in entry["stale"]:
                print(f"      陈旧 {rel}")
            print(f"      → {SELF_INVOCATION} install")
    if not payload["ok"]:
        print("提示：加 --all 可检查全部候选位置。")
    if not payload["visibility"]["ok"]:
        print("")
        print("[!!] 已安装的位置可能不会被当前 agent 发现：", file=sys.stderr)
        for note in payload["visibility"]["notes"]:
            print(f"     {note}", file=sys.stderr)
    return 0 if payload["ok"] else 1


def cmd_path(args) -> int:
    targets = detect_targets(args.workspace)
    chosen = resolve_target(targets, args.target)
    dst_dir = install_dir(chosen)
    if args.json:
        print(json.dumps({"install_path": str(dst_dir),
                          "installed": (dst_dir / "SKILL.md").is_file(),
                          "linked": _is_link(dst_dir),
                          "target": chosen,
                          "visibility": visibility_report(chosen, targets)},
                         ensure_ascii=False, indent=2))
        return 0
    print(dst_dir)
    return 0


def cmd_uninstall(args) -> int:
    targets = detect_targets(args.workspace)
    chosen = resolve_target(targets, args.target)
    dst_dir = install_dir(chosen)

    if not dst_dir.is_dir() and not _is_link(dst_dir):
        print(f"未安装（{chosen['label']}）：{dst_dir}")
        return 0

    linked = _is_link(dst_dir)
    if not linked:
        owned, why = _installed_identity_ok(dst_dir)
        if not owned and not args.force:
            print(f"错误：{dst_dir} {why}，不是本 skill，已中止。", file=sys.stderr)
            print("      如确认要删除，请加 --force。", file=sys.stderr)
            return 2

    if not args.yes:
        print(f"将删除：{dst_dir}" + ("（这是目录链接，只解除链接，不动源目录）" if linked else ""))
        print("这是不可逆操作；确认请加 --yes 重新执行。")
        return 1

    try:
        _remove_path(dst_dir)
    except OSError as exc:
        print(f"错误：删除失败：{exc}", file=sys.stderr)
        print("      若为链接且目标不存在，可手动删除该路径。", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"removed": str(dst_dir), "linked": linked,
                          "target": chosen["label"]}, ensure_ascii=False, indent=2))
        return 0
    print(f"[OK] 已卸载：{dst_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description=f"{PACKAGE_NAME} skill 分发工具（安装 / 校验 / 打包）",
        epilog="示例：\n"
               "  install.py targets\n"
               "  install.py install --dry-run\n"
               "  install.py install\n"
               "  install.py install --target claude\n"
               "  install.py package\n"
               "  install.py check --all\n"
               "  install.py uninstall --yes\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source",
                        help="skill 源目录（默认按脚本位置推断；也可用环境变量 "
                             "NOOVA_SKILL_SOURCE）。放在子命令前后都可以。")
    parser.add_argument("--version", action="version", version=f"{PACKAGE_NAME} installer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _common(p):
        # `--source` 同时挂在根与子命令上：argparse 只接受子命令**之前**的根选项，
        # 而文档里 `package` 交给接收方的命令是 `install --source <目录>`
        # （子命令之后）——那曾经必然报 "unrecognized arguments"（已实测）。
        p.add_argument("--source",
                       help="skill 源目录（等价于根上的 --source）")
        p.add_argument("--target", default="auto",
                       help="auto（默认，= 当前 agent 的技能根）| user | project | "
                            + " | ".join(s[0] for s in AGENT_SPECS)
                            + " | 任意目录路径")
        p.add_argument("--workspace", help="项目级目标所依据的工作区目录（默认当前目录）")
        p.add_argument("--json", action="store_true")

    p_targets = sub.add_parser("targets", help="列出可用安装位置（含当前 agent 探测结果）")
    _common(p_targets)
    p_targets.set_defaults(func=cmd_targets)

    p_install = sub.add_parser("install", help="安装 / 更新（增量）")
    _common(p_install)
    p_install.add_argument("--dry-run", action="store_true", help="只预览变更，不写入")
    p_install.add_argument("--force", action="store_true", help="目标同名目录归属不符时仍覆盖")
    p_install.add_argument("--link", action="store_true",
                           help="建目录链接（junction/symlink）而不是复制文件；"
                                "源目录需长期存在")
    p_install.set_defaults(func=cmd_install)

    p_check = sub.add_parser("check", help="校验已安装副本与源是否一致")
    _common(p_check)
    p_check.add_argument("--all", action="store_true", help="检查全部候选位置")
    p_check.set_defaults(func=cmd_check)

    p_validate = sub.add_parser("validate", help="校验 skill 目录是否符合标准形态")
    p_validate.add_argument("--json", action="store_true")
    p_validate.set_defaults(func=cmd_validate)

    p_package = sub.add_parser("package", help="打成可分发的 zip")
    p_package.add_argument("--output", help=f"输出路径（默认 ./{PACKAGE_NAME}.zip）")
    p_package.set_defaults(func=cmd_package)

    p_path = sub.add_parser("path", help="打印安装路径")
    _common(p_path)
    p_path.set_defaults(func=cmd_path)

    p_un = sub.add_parser("uninstall", help="卸载（仅删除本 skill 自己的目录/链接）")
    _common(p_un)
    p_un.add_argument("--yes", action="store_true", help="确认删除（必须显式给出）")
    p_un.add_argument("--force", action="store_true", help="目标归属不符时仍删除")
    p_un.set_defaults(func=cmd_uninstall)

    args = parser.parse_args(argv)

    global SKILL_ROOT
    explicit_source = getattr(args, "source", None)
    SKILL_ROOT = resolve_skill_root(explicit_source)
    if not SKILL_ROOT.is_dir():
        print(f"[!!] 找不到 skill 源目录：{SKILL_ROOT}", file=sys.stderr)
        print("     用 --source <skill 目录> 指定，或设置环境变量 NOOVA_SKILL_SOURCE。",
              file=sys.stderr)
        return 2

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:  # noqa: BLE001
            pass
        return 0
    except OSError as exc:
        print(f"错误：文件系统操作失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"错误：发生未预期的内部错误（{type(exc).__name__}）；"
              f"请重试，若持续出现请反馈该现象。", file=sys.stderr)
        if os.environ.get("NOOVA_DEBUG", "").strip():
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
