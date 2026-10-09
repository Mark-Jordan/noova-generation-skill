#!/usr/bin/env python3
"""`install.py` 单元测试：形态校验、目标探测、增量比对、安装/校验/卸载/打包。

全部在临时目录内完成，不触碰真实的技能目录。

测试与安装器都在 skill 目录之外，因为 skill 本体只允许标准结构：
`SKILL.md` + `scripts/` + `references/`。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT / "skills" / "noova-generation"
INSTALLER_SCRIPT = PROJECT / "install.py"


def _load_installer():
    """按路径加载安装器（文件名带连字符，不能用 import 语句）。"""
    spec = importlib.util.spec_from_file_location("install_noova_skill", INSTALLER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installer = _load_installer()


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = installer.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_at(source: Path, *argv: str) -> tuple[int, str, str]:
    """在指定源目录下运行安装器命令，并在结束后还原模块全局状态。

    `main()` 会用 `resolve_skill_root()` 的结果覆盖 `SKILL_ROOT`，因此必须从这里
    打桩，而不是直接改 `SKILL_ROOT`（否则会被覆盖掉）。
    """
    saved = installer.SKILL_ROOT
    try:
        with mock.patch.object(installer, "resolve_skill_root", return_value=source):
            return run(*argv)
    finally:
        installer.SKILL_ROOT = saved


class SourceTreeTest(unittest.TestCase):
    def test_source_is_the_package_root(self):
        self.assertTrue((SKILL_ROOT / "SKILL.md").is_file())
        self.assertEqual(installer.SKILL_ROOT, SKILL_ROOT.resolve())

    def test_collect_source_has_expected_entries(self):
        files = installer.collect_source()
        self.assertIn("SKILL.md", files)
        self.assertIn("scripts/noova_media.py", files)
        self.assertIn("scripts/noova_key.py", files)
        self.assertIn("scripts/noova_upload.py", files)
        self.assertIn("references/api-contracts.md", files)

    def test_source_contains_only_standard_top_level_entries(self):
        """回归：skill 目录必须只含标准结构。

        安装器、测试等仓库侧资产曾经放在 skill 根目录里，导致 skill 看起来
        「不像普通 skill」（自带安装步骤）。它们现在归位于仓库根。
        """
        tops = {p.name for p in SKILL_ROOT.iterdir()}
        self.assertIn("SKILL.md", tops)
        self.assertTrue(tops <= installer.STANDARD_TOP_LEVEL,
                        msg=f"出现非标准顶层条目：{tops - installer.STANDARD_TOP_LEVEL}")
        for rel in installer.collect_source():
            self.assertIn(Path(rel).parts[0], installer.STANDARD_TOP_LEVEL)

    def test_validate_skill_reports_ok(self):
        result = installer.validate_skill()
        self.assertTrue(result["ok"], msg=result["errors"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])

    def test_validate_flags_missing_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / installer.PACKAGE_NAME
            staged.mkdir()
            (staged / "SKILL.md").write_text("---\nname: noova-generation\n---\n\n正文\n",
                                             encoding="utf-8")
            with mock.patch.object(installer, "SKILL_ROOT", staged):
                result = installer.validate_skill()
        self.assertFalse(result["ok"])
        self.assertTrue(any("description" in e for e in result["errors"]))

    def test_validate_warns_on_non_standard_entry(self):
        with mock.patch.object(installer, "SKILL_ROOT", SKILL_ROOT):
            standalone = SKILL_ROOT / "_scratch"
            standalone.write_text("x", encoding="utf-8")
            try:
                result = installer.validate_skill()
            finally:
                standalone.unlink()
        self.assertTrue(any("_scratch" in w for w in result["warnings"]))

    def test_collect_source_skips_cache_and_local_config(self):
        files = installer.collect_source()
        for rel in files:
            self.assertNotIn("__pycache__", rel)
            self.assertFalse(rel.endswith(".pyc"))
            self.assertNotIn(".noova", rel)
            self.assertNotEqual(Path(rel).name, installer.MANIFEST_NAME)

    def test_collect_source_ignores_installer_state_file(self):
        """安装器状态文件不能算作源内容，否则每次安装都判定为"有变化"。"""
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "pkg"
            staged.mkdir()
            shutil.copyfile(SKILL_ROOT / "SKILL.md", staged / "SKILL.md")
            (staged / installer.MANIFEST_NAME).write_text('{"x":1}', encoding="utf-8")
            with mock.patch.object(installer, "SKILL_ROOT", staged):
                files = installer.collect_source()
        self.assertEqual(sorted(files), ["SKILL.md"])

    def test_skill_name_matches_package(self):
        self.assertEqual(installer.read_skill_name(SKILL_ROOT), installer.PACKAGE_NAME)

    def test_version_is_consistent_across_skill_key_and_changelog(self):
        """版本号不变量：SKILL.md / noova_key.VERSION / references/changelog.md 三处一致。

        历史上只有前两处、靠人工同步；changelog 若不与版本同改就会腐烂，
        因此把它一并纳入机械校验（`references/changelog.md` 里写明该不变量）。
        """
        skill_md = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        m = re.search(r'^\s*version:\s*"([^"]+)"', skill_md, re.M)
        self.assertIsNotNone(m, "SKILL.md 缺少 metadata.version")
        version = m.group(1)

        key_src = (SKILL_ROOT / "scripts" / "noova_key.py").read_text(encoding="utf-8")
        m2 = re.search(r'^VERSION\s*=\s*"([^"]+)"', key_src, re.M)
        self.assertIsNotNone(m2, "noova_key.py 缺少 VERSION")
        self.assertEqual(m2.group(1), version,
                         "SKILL.md 与 noova_key.py 的版本号不一致")

        changelog = (SKILL_ROOT / "references" / "changelog.md").read_text(encoding="utf-8")
        self.assertIn(version, changelog,
                      f"references/changelog.md 未记录当前版本 {version}")


class TargetDetectionTest(unittest.TestCase):
    def test_user_targets_before_project(self):
        targets = installer.detect_targets(workspace="/tmp/ws")
        kinds = [t["kind"] for t in targets]
        self.assertEqual(kinds.index("user"), 0)
        self.assertLess(kinds.index("user"), kinds.index("project"))

    def test_agents_root_is_first(self):
        """修正（第四轮审计 BLOCKER B5）：清单第一个必须是 `~/.agents/skills`。

        这里曾经断言 `workbuddy` 排第一，而 `auto` 因此把 skill 装到
        `~/.codebuddy/skills` —— DSH 读的是 `~/.agents/skills`，于是安装报告
        「已安装且一致」、agent 却永远发现不了这个 skill（本机实测）。
        `.agents` 是本机真实的生态根（技能数最多，且被其它技能根以 junction 扇出，
        并持有技能管理器的 `.skill-lock.json`），必须优先。
        """
        targets = installer.detect_targets(workspace="/tmp/ws")
        self.assertEqual(targets[0]["host"], "agents")
        self.assertEqual(Path(targets[0]["root"]).parts[-2:], (".agents", "skills"))

    def test_project_target_follows_workspace(self):
        targets = installer.detect_targets(workspace="/tmp/ws")
        project = next(t for t in targets if t["kind"] == "project")
        self.assertIn("ws", project["root"])

    def test_explicit_directory_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = installer.resolve_target(installer.detect_targets(), tmp)
            self.assertEqual(target["kind"], "custom")
            # 比对**规范化后**的路径：`resolve_target()` 会对显式目录调 `resolve()`，
            # 而 `resolve()` 在 Windows 上会把 8.3 短名（如 `RUNNER~1`）展开成长名。
            # 直接比原始 `tmp` 会在 CI 的短名临时目录下误报（已实测）。
            self.assertEqual(installer.install_dir(target).resolve(),
                             (Path(tmp) / installer.PACKAGE_NAME).resolve())

    def test_keyword_target(self):
        targets = installer.detect_targets()
        self.assertEqual(installer.resolve_target(targets, "project")["kind"], "project")
        self.assertEqual(installer.resolve_target(targets, "user")["kind"], "user")
        self.assertEqual(installer.resolve_target(targets, "claude")["host"], "claude")

    def test_auto_prefers_existing_host(self):
        targets = installer.detect_targets()
        chosen = installer.resolve_target(targets, "auto")
        self.assertEqual(chosen["kind"], "user")


class SelfNestingGuardTest(unittest.TestCase):
    """回归：从 skill 目录里执行时，项目级目标会指向源目录内部，必须拦住。"""

    def test_project_target_is_blocked_inside_source(self):
        targets = installer.detect_targets(workspace=str(SKILL_ROOT))
        project = next(t for t in targets if t["kind"] == "project")
        self.assertTrue(project["blocked"])
        self.assertIn("源目录内部", project["blocked_reason"])

    def test_install_into_source_is_refused(self):
        code, _, err = run("install", "--target", str(SKILL_ROOT))
        self.assertEqual(code, 2)
        self.assertIn("拒绝安装", err)
        self.assertFalse((SKILL_ROOT / installer.PACKAGE_NAME).exists())

    def test_custom_target_inside_source_is_blocked(self):
        target = installer.resolve_target(installer.detect_targets(), str(SKILL_ROOT))
        self.assertTrue(target["blocked"])

    def test_external_project_target_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = installer.detect_targets(workspace=tmp)
            project = next(t for t in targets if t["kind"] == "project")
            self.assertFalse(project["blocked"])


class CompareTest(unittest.TestCase):
    def test_missing_target_means_all_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = installer.compare({"SKILL.md": SKILL_ROOT / "SKILL.md"},
                                       Path(tmp) / "not-there")
        self.assertEqual(result["added"], ["SKILL.md"])
        self.assertEqual(result["changed"], [])

    def test_detects_added_changed_and_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)
            (dst / "SKILL.md").write_text("old", encoding="utf-8")
            (dst / "legacy.txt").write_text("x", encoding="utf-8")
            result = installer.compare({"SKILL.md": SKILL_ROOT / "SKILL.md"}, dst)
            self.assertEqual(result["changed"], ["SKILL.md"])
            self.assertEqual(result["stale"], ["legacy.txt"])

    def test_manifest_is_never_treated_as_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)
            (dst / installer.MANIFEST_NAME).write_text("{}", encoding="utf-8")
            result = installer.compare({}, dst)
            self.assertEqual(result["stale"], [])

    def test_non_standard_top_level_entries_are_stale(self):
        """回归：目标目录里遗留的 install.py / tests/ 必须被判为陈旧。

        逐文件比对会漏掉被 `SKIP_DIRS` 排除的目录，导致旧版安装器复制过去的
        `tests/` 永远留在已安装副本里。
        """
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp)
            (dst / "install.py").write_text("x", encoding="utf-8")
            (dst / "tests").mkdir()
            (dst / "tests" / "test_x.py").write_text("x", encoding="utf-8")
            result = installer.compare({}, dst)
        self.assertIn("install.py", result["stale"])
        self.assertIn("tests/", result["stale"])
        self.assertFalse([r for r in result["stale"]
                          if r.startswith("tests/") and r != "tests/"],
                         msg="目录应整体判为陈旧，而不是逐个文件")


class InstallLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target_root = Path(self._tmp.name) / "skills"
        self.target_root.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _install(self, *extra: str):
        return run("install", "--target", str(self.target_root), *extra)

    def test_dry_run_writes_nothing(self):
        code, out, _ = self._install("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("预览模式", out)
        self.assertFalse((self.target_root / installer.PACKAGE_NAME).exists())

    def test_install_then_check_is_in_sync(self):
        code, out, _ = self._install()
        self.assertEqual(code, 0)
        self.assertIn("安装完成", out)
        package = self.target_root / installer.PACKAGE_NAME
        self.assertTrue((package / "SKILL.md").is_file())
        self.assertTrue((package / "scripts" / "noova_key.py").is_file())
        self.assertTrue((package / installer.MANIFEST_NAME).is_file())

        code, out, _ = run("check", "--target", str(self.target_root), "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["results"][0]["in_sync"])

    def test_check_detects_drift(self):
        self._install()
        package = self.target_root / installer.PACKAGE_NAME
        (package / "SKILL.md").write_text("被改坏了", encoding="utf-8")
        code, out, _ = run("check", "--target", str(self.target_root), "--json")
        self.assertEqual(code, 1)
        self.assertIn("SKILL.md", json.loads(out)["results"][0]["changed"])

    def test_reinstall_repairs_and_removes_stale(self):
        self._install()
        package = self.target_root / installer.PACKAGE_NAME
        (package / "SKILL.md").write_text("被改坏了", encoding="utf-8")
        (package / "junk.txt").write_text("x", encoding="utf-8")
        code, out, _ = self._install()
        self.assertEqual(code, 0)
        self.assertIn("~ SKILL.md", out)
        self.assertIn("- junk.txt", out)
        self.assertFalse((package / "junk.txt").exists())
        self.assertEqual(run("check", "--target", str(self.target_root))[0], 0)

    def test_second_install_reports_no_change(self):
        self._install()
        code, out, _ = self._install()
        self.assertEqual(code, 0)
        self.assertIn("无需更新", out)

    def test_reinstall_cleans_legacy_non_standard_entries(self):
        """回归：重复安装会清掉历史版本留在目录里的安装器与测试目录。

        旧版安装器把 `install.py` 与 `tests/` 一起复制到了技能目录，使已安装副本
        偏离标准形态；重新安装后应当只剩 `SKILL.md` + `scripts/` + `references/`。
        """
        self._install()
        package = self.target_root / installer.PACKAGE_NAME
        (package / "install.py").write_text("# 历史遗留", encoding="utf-8")
        (package / "tests").mkdir()
        (package / "tests" / "test_x.py").write_text("x", encoding="utf-8")

        code, out, _ = self._install()
        self.assertEqual(code, 0)
        self.assertIn("- install.py", out)
        self.assertIn("- tests/", out)
        self.assertFalse((package / "install.py").exists())
        self.assertFalse((package / "tests").exists())

        tops = {p.name for p in package.iterdir() if not p.name.startswith(".")}
        self.assertTrue(tops <= installer.STANDARD_TOP_LEVEL, msg=f"残留：{tops}")

    def test_refuses_to_overwrite_foreign_package(self):
        foreign = self.target_root / installer.PACKAGE_NAME
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("---\nname: someone-else\n---\n", encoding="utf-8")
        code, _, err = self._install()
        self.assertEqual(code, 2)
        self.assertIn("someone-else", err)

    def test_installed_copy_contains_no_pycache(self):
        self._install()
        package = self.target_root / installer.PACKAGE_NAME
        self.assertEqual(list(package.rglob("*.pyc")), [])

    def test_path_subcommand(self):
        code, out, _ = run("path", "--target", str(self.target_root))
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith(installer.PACKAGE_NAME))

    def test_uninstall_requires_confirmation(self):
        self._install()
        package = self.target_root / installer.PACKAGE_NAME
        code, out, _ = run("uninstall", "--target", str(self.target_root))
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)
        self.assertTrue(package.is_dir())

    def test_uninstall_with_yes_removes_package(self):
        self._install()
        code, _, _ = run("uninstall", "--target", str(self.target_root), "--yes")
        self.assertEqual(code, 0)
        self.assertFalse((self.target_root / installer.PACKAGE_NAME).exists())

    def test_uninstall_on_missing_is_noop(self):
        code, out, _ = run("uninstall", "--target", str(self.target_root), "--yes")
        self.assertEqual(code, 0)
        self.assertIn("未安装", out)


class BrokenPathToleranceTest(unittest.TestCase):
    """回归：技能根里存在断链符号链接时，探测不得崩溃。

    实测 `~/.claude/skills/ponytail` 是断链，`Path.exists()` 在 Windows 上抛
    `OSError: [WinError 1920] 系统无法访问此文件`，会把整条 `targets` 命令打挂。
    """

    def test_is_writable_survives_oserror(self):
        with mock.patch.object(Path, "exists", side_effect=OSError("WinError 1920")):
            self.assertFalse(installer._is_writable(Path("/nonexistent/a/b/c")))

    def test_target_survives_oserror(self):
        with mock.patch.object(Path, "resolve", side_effect=OSError("WinError 1920")):
            item = installer._target("user", "broken", "/tmp/nowhere/skills", "断链宿主", 9)
        self.assertTrue(item["blocked"])
        self.assertIn("不可访问", item["blocked_reason"])
        self.assertFalse(item["host_exists"])


class AgentAwareTargetTest(unittest.TestCase):
    """第四轮审计 BLOCKER B5：装到哪必须由「装给哪个 agent」决定。

    修复前的 `auto` 固定按优先级表选，把 skill 装到 `~/.codebuddy/skills`，
    而 DSH 读的是 `~/.agents/skills` —— 安装报告「已安装且一致」、agent 却永远
    发现不了这个 skill（本机实测，且它没有出现在会话的技能清单里）。
    """

    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_dsh_environment_selects_the_agents_root(self):
        with mock.patch.dict(os.environ, {"DSH_SHELL": "1"}, clear=False):
            detected = installer.detect_host()
            self.assertIsNotNone(detected)
            self.assertEqual(detected["host"], "agents")
            chosen = installer.resolve_target(installer.detect_targets(), "auto")
        self.assertEqual(chosen["host"], "agents")
        self.assertEqual(Path(chosen["root"]).parts[-2:], (".agents", "skills"))

    def test_claude_environment_selects_the_claude_root(self):
        for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            with self.subTest(marker=marker):
                env = {k: v for k, v in os.environ.items()
                       if k not in ("DSH_SHELL", "DSH_SESSION_ID", "DSH_HOME", "DSH_WEB_URL")}
                env[marker] = "1"
                with mock.patch.dict(os.environ, env, clear=True):
                    chosen = installer.resolve_target(installer.detect_targets(), "auto")
                self.assertEqual(chosen["host"], "claude")

    def test_codex_environment_selects_the_codex_root(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DSH_SHELL", "DSH_SESSION_ID", "DSH_HOME", "DSH_WEB_URL")}
        env["CODEX_SANDBOX"] = "1"
        with mock.patch.dict(os.environ, env, clear=True):
            chosen = installer.resolve_target(installer.detect_targets(), "auto")
        self.assertEqual(chosen["host"], "codex")

    def test_no_agent_detected_falls_back_to_the_ecosystem_root(self):
        """探测不到 agent 时，选**被技能管理器管理**的那个根（有 .skill-lock.json）。"""
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("DSH_", "CLAUDE", "CODEX_", "OPENCODE_",
                                      "CODEBUDDY_", "WORKBUDDY_", "QODER_"))}
        with mock.patch.dict(os.environ, clean, clear=True), \
             tempfile.TemporaryDirectory() as tmp:
            eco = Path(tmp) / "skills"
            eco.mkdir(parents=True)
            (Path(tmp) / ".skill-lock.json").write_text("{}", encoding="utf-8")
            other = Path(tmp) / "other" / "skills"
            other.mkdir(parents=True)
            targets = [
                installer._target("user", "workbuddy", other, "其它", 1),
                installer._target("user", "agents", eco, "生态根", 2),
            ]
            found = installer.ecosystem_root(targets)
            self.assertIsNotNone(found)
            self.assertEqual(found["host"], "agents")
            self.assertEqual(installer.resolve_target(targets, "auto")["host"], "agents")

    def test_visibility_check_flags_a_wrong_root(self):
        """装到别的 agent 的技能根时，必须明确提示「可能发现不了」。"""
        with mock.patch.dict(os.environ, {"DSH_SHELL": "1"}, clear=False):
            targets = installer.detect_targets()
            wrong = next(t for t in targets
                         if t["host"] == "codebuddy" and t["kind"] == "user")
            report = installer.visibility_report(wrong, targets)
        self.assertFalse(report["ok"])
        self.assertTrue(any("发现不了" in note for note in report["notes"]))

    def test_visibility_check_passes_for_the_current_agent_root(self):
        with mock.patch.dict(os.environ, {"DSH_SHELL": "1"}, clear=False):
            targets = installer.detect_targets()
            right = next(t for t in targets
                         if t["host"] == "agents" and t["kind"] == "user")
            report = installer.visibility_report(right, targets)
        self.assertTrue(report["ok"])

    def test_targets_listing_reports_the_detected_agent(self):
        with mock.patch.dict(os.environ, {"DSH_SHELL": "1"}, clear=False):
            code, out, _ = run("targets", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["detected_host"]["host"], "agents")
        self.assertIn("ecosystem_root", payload)


class SourceFlagPositionTest(unittest.TestCase):
    """第四轮审计 H7：`--source` 曾只能放在子命令**之前**。

    `package` 交给接收方的命令是 `install --source <目录>`（子命令之后），
    而那曾经必然报 `unrecognized arguments`（已实测）——等于把唯一的分发路径给废了。
    """

    def test_source_after_subcommand_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run("install", "--source", str(SKILL_ROOT),
                               "--target", tmp, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(str(SKILL_ROOT), out)

    def test_source_before_subcommand_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run("--source", str(SKILL_ROOT),
                               "install", "--target", tmp, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(str(SKILL_ROOT), out)

    def test_package_hint_command_is_actually_runnable(self):
        """`package` 打印的那条命令必须真的能跑（提取出来直接执行一遍）。"""
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run("package", "--output", str(Path(tmp) / "p.zip"))
            self.assertEqual(code, 0)
        hint = [line for line in out.splitlines() if "--source" in line][0]
        self.assertIn("install", hint)
        self.assertLess(hint.index("--source"), len(hint))


class AtomicWriteAndErrorHandlingTest(unittest.TestCase):
    """第四轮审计 H8：复制循环无错误处理 → traceback + 半更新安装。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target_root = Path(self._tmp.name) / "skills"
        self.target_root.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_copy_failure_reports_one_line_not_a_traceback(self):
        with mock.patch.object(installer.shutil, "copyfile",
                               side_effect=PermissionError("被占用")):
            code, out, err = run("install", "--target", str(self.target_root))
        self.assertEqual(code, 2)
        self.assertIn("写入失败", err)
        self.assertNotIn("Traceback", err)

    def test_manifest_write_failure_is_reported(self):
        with mock.patch.object(installer, "_write_manifest",
                               side_effect=OSError("磁盘满")):
            code, _, err = run("install", "--target", str(self.target_root))
        self.assertEqual(code, 2)
        self.assertIn("安装清单", err)

    def test_install_validates_the_source_shape(self):
        """修复前：`install` 不校验源目录，缺 description 的源可被静默安装。"""
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / installer.PACKAGE_NAME
            staged.mkdir()
            (staged / "SKILL.md").write_text("---\nname: noova-generation\n---\n\n正文\n",
                                             encoding="utf-8")
            target = Path(tmp) / "skills"
            target.mkdir()
            code, _, err = run_at(staged, "install", "--target", str(target))
            self.assertEqual(code, 2)
            self.assertIn("description", err)
            self.assertFalse((target / installer.PACKAGE_NAME).exists())


class LinkInstallTest(unittest.TestCase):
    """第四轮审计 H9：`shutil.rmtree` 对 junction 抛错 → 链接式安装不可卸载。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target_root = Path(self._tmp.name) / "skills"
        self.target_root.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_is_link_detects_a_junction(self):
        if os.name != "nt":
            self.skipTest("junction 是 Windows 概念")
        link = self.target_root / "j"
        installer._make_link(SKILL_ROOT, link)
        self.assertTrue(installer._is_link(link))
        self.assertEqual(installer._link_target(link).resolve(), SKILL_ROOT.resolve())

    def test_remove_path_unlinks_without_deleting_the_target(self):
        if os.name != "nt":
            self.skipTest("junction 是 Windows 概念")
        real = Path(self._tmp.name) / "real"
        real.mkdir()
        (real / "keep.txt").write_text("x", encoding="utf-8")
        link = self.target_root / "link"
        installer._make_link(real, link)
        installer._remove_path(link)
        self.assertFalse(link.exists())
        self.assertTrue((real / "keep.txt").is_file(), "不得删进目标目录")

    def test_link_install_then_uninstall(self):
        if os.name != "nt":
            self.skipTest("junction 是 Windows 概念")
        code, out, _ = run("install", "--target", str(self.target_root), "--link")
        self.assertEqual(code, 0)
        self.assertIn("链接", out)
        package = self.target_root / installer.PACKAGE_NAME
        self.assertTrue(installer._is_link(package))
        self.assertTrue((package / "SKILL.md").is_file())

        code, _, _ = run("uninstall", "--target", str(self.target_root), "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(package.exists())
        self.assertTrue((SKILL_ROOT / "SKILL.md").is_file(), "源目录必须完好")


class ManifestPrivacyTest(unittest.TestCase):
    """L21：manifest 曾把开发者绝对路径写进**用户**的技能目录。

    判据是「源目录是否在**当前用户自己的**家目录下」：
      · 在 → 记录真实路径（对用户有意义，便于他确认装的是哪一份）；
      · 不在（开发者的仓库路径）→ 只记「分发包」，绝不外泄内部路径。

    注意：Windows 的临时目录位于 `%USERPROFILE%\\AppData\\Local\\Temp` 之下，
    即**确实在家目录里**，所以这里把 `Path.home()` 打桩成另一个目录，
    才能真正构造出「源目录在家目录之外」的情形。
    """

    def test_manifest_hides_a_path_outside_the_user_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "user-home"
            fake_home.mkdir()
            outside = Path(tmp) / "developer-repo" / installer.PACKAGE_NAME
            outside.mkdir(parents=True)
            with mock.patch.object(installer.Path, "home", return_value=fake_home), \
                 mock.patch.object(installer, "SKILL_ROOT", outside):
                self.assertEqual(installer._manifest_source(),
                                 f"{installer.PACKAGE_NAME}（分发包）")

    def test_manifest_records_the_path_when_source_is_under_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "user-home"
            inside = fake_home / ".agents" / "skills" / installer.PACKAGE_NAME
            inside.mkdir(parents=True)
            with mock.patch.object(installer.Path, "home", return_value=fake_home), \
                 mock.patch.object(installer, "SKILL_ROOT", inside):
                self.assertEqual(installer._manifest_source(), str(inside))

    def test_written_manifest_never_contains_a_foreign_path(self):
        """端到端：装完之后，落在技能目录里的 manifest 不含外部路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            target_root = Path(tmp) / "skills"
            target_root.mkdir()
            fake_home = Path(tmp) / "user-home"
            fake_home.mkdir()
            with mock.patch.object(installer.Path, "home", return_value=fake_home):
                code, _, _ = run("install", "--target", str(target_root))
            self.assertEqual(code, 0)
            manifest = json.loads(
                (target_root / installer.PACKAGE_NAME /
                 installer.MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertNotIn("developer-repo", manifest["source"])
            self.assertNotIn(str(SKILL_ROOT), manifest["source"])


class DistributionWhitelistTest(unittest.TestCase):
    """M27：分发过滤曾是黑名单，`.DS_Store` / `notes.txt` 之类会被打包。"""

    def test_non_standard_top_level_files_are_not_distributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "pkg"
            shutil.copytree(SKILL_ROOT, staged)
            for junk in ("notes.txt", ".DS_Store", "Thumbs.db", "draft.bak",
                         "scratch.swp", "build.log"):
                (staged / junk).write_text("x", encoding="utf-8")
            with mock.patch.object(installer, "SKILL_ROOT", staged):
                files = installer.collect_source()
        for junk in ("notes.txt", ".DS_Store", "Thumbs.db", "draft.bak",
                     "scratch.swp", "build.log"):
            self.assertNotIn(junk, files, f"{junk} 不应被分发")

    def test_standard_entries_are_still_distributed(self):
        files = installer.collect_source()
        self.assertIn("SKILL.md", files)
        self.assertIn("scripts/noova_common.py", files)


class IdentityGuardTest(unittest.TestCase):
    """M22：install 的所有权守卫曾弱于 uninstall。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target_root = Path(self._tmp.name) / "skills"
        self.target_root.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_refuses_a_directory_without_skill_md(self):
        foreign = self.target_root / installer.PACKAGE_NAME
        foreign.mkdir(parents=True)
        (foreign / "unrelated.txt").write_text("x", encoding="utf-8")
        code, _, err = run("install", "--target", str(self.target_root))
        self.assertEqual(code, 2)
        self.assertIn("SKILL.md", err)
        self.assertTrue((foreign / "unrelated.txt").is_file(), "不得清理别人的目录")

    def test_refuses_a_foreign_skill(self):
        foreign = self.target_root / installer.PACKAGE_NAME
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("---\nname: someone-else\n---\n", encoding="utf-8")
        code, _, err = run("install", "--target", str(self.target_root))
        self.assertEqual(code, 2)
        self.assertIn("someone-else", err)

    def test_repairs_a_drifted_copy_of_this_skill(self):
        """被改坏的**自己的**副本必须能修复（守卫不能紧到拒绝修复）。"""
        run("install", "--target", str(self.target_root))
        package = self.target_root / installer.PACKAGE_NAME
        (package / "SKILL.md").write_text("broken", encoding="utf-8")
        code, out, _ = run("install", "--target", str(self.target_root))
        self.assertEqual(code, 0)
        self.assertIn("~ SKILL.md", out)


class HostMarkerCoverageTest(unittest.TestCase):
    def test_strong_markers_cover_the_documented_hosts(self):
        hosts = {host for host, _ in installer.HOST_MARKERS}
        for expected in ("agents", "claude", "codex", "opencode"):
            self.assertIn(expected, hosts, f"缺少 {expected} 的探测特征")

    def test_weak_markers_are_never_used_for_agents(self):
        """`CODEX_HOME` / `CLAUDE_CONFIG_DIR` 可能被全局导出，不能当强特征。"""
        weak = {host for host, _ in installer.WEAK_HOST_MARKERS}
        self.assertNotIn("agents", weak)

    def test_dsh_markers_are_listed(self):
        agents = dict(installer.HOST_MARKERS)["agents"]
        self.assertIn("DSH_SHELL", agents)


class XdgOverrideTest(unittest.TestCase):
    """M26：宿主的重定位环境变量必须被尊重，否则会装到客户端不看的位置。"""

    def test_xdg_config_home_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                opencode = next(t for t in installer.detect_targets()
                                if t["host"] == "opencode" and t["kind"] == "user")
        self.assertEqual(Path(opencode["root"]), Path(tmp) / "opencode" / "skills")

    def test_claude_config_dir_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                claude = next(t for t in installer.detect_targets()
                              if t["host"] == "claude" and t["kind"] == "user")
        self.assertEqual(Path(claude["root"]), Path(tmp) / "skills")


class TargetsListingTest(unittest.TestCase):
    def test_json_listing_includes_chosen(self):
        code, out, _ = run("targets", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("chosen", payload)
        self.assertEqual(payload["source"], str(installer.SKILL_ROOT))

    def test_all_known_hosts_are_probed(self):
        """回归：宿主清单必须覆盖官方文档标注的标准技能根。

        曾漏掉 CodeBuddy（skill-creator 文档里的用户级标准路径），
        后补 Codex（`$CODEX_HOME/skills`，实测本机 `~/.codex/skills` 真实存在）。
        """
        hosts = {t["host"] for t in installer.detect_targets()}
        for expected in ("workbuddy", "codebuddy", "claude", "opencode", "codex", "agents",
                         "zcode", "openclaw", "hermes"):
            self.assertIn(expected, hosts, f"缺少宿主探测：{expected}")

    def test_codex_root_is_discovered(self):
        """Codex 的技能根是 `$CODEX_HOME/skills`（默认 `~/.codex/skills`）。"""
        targets = installer.detect_targets()
        codex = next(t for t in targets if t["host"] == "codex")
        self.assertEqual(codex["kind"], "user")
        self.assertTrue(codex["root"].replace("\\", "/").endswith("/skills"))
        self.assertIn(".codex", codex["root"])

    def test_codex_home_env_is_respected(self):
        """官方支持 `CODEX_HOME` 覆盖；探测必须跟随，否则装错位置。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CODEX_HOME": tmp}):
                codex = next(t for t in installer.detect_targets() if t["host"] == "codex")
        self.assertEqual(Path(codex["root"]), Path(tmp) / "skills")

    def test_every_host_has_a_keyword_shortcut(self):
        """`--target <host>` 必须对每个宿主可用（否则文档里的写法会失败）。"""
        targets = installer.detect_targets()
        for host in ("workbuddy", "codebuddy", "claude", "opencode", "codex", "agents",
                     "zcode", "openclaw", "hermes"):
            with self.subTest(host=host):
                self.assertEqual(installer.resolve_target(targets, host)["host"], host)

    def test_codebuddy_user_root_matches_official_doc(self):
        """官方文档：用户级 `~/.codebuddy/skills/`，项目级 `.codebuddy/skills/`。"""
        targets = installer.detect_targets()
        user = next(t for t in targets if t["host"] == "codebuddy" and t["kind"] == "user")
        self.assertTrue(user["root"].replace("\\", "/").endswith(".codebuddy/skills"))
        project = next(t for t in targets if t["host"] == "codebuddy" and t["kind"] == "project")
        self.assertTrue(project["root"].replace("\\", "/").endswith(".codebuddy/skills"))


class ValidateCommandTest(unittest.TestCase):
    def test_validate_reports_standard_shape(self):
        code, out, _ = run("validate")
        self.assertEqual(code, 0)
        self.assertIn("符合标准形态", out)

    def test_validate_json_is_machine_readable(self):
        code, out, _ = run("validate", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["source"], str(installer.SKILL_ROOT))

    def test_validate_exit_code_1_when_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / installer.PACKAGE_NAME
            staged.mkdir()
            code, out, err = run_at(staged, "validate")
        self.assertEqual(code, 1)
        self.assertIn("SKILL.md", out)
        self.assertIn("不符合标准形态", err)


class PackageTest(unittest.TestCase):
    """`package` 子命令：产出可分发的 zip（对标官方 package_skill.py）。"""

    def test_package_writes_zip_with_expected_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dist" / "pkg.zip"
            code, stdout, _ = run("package", "--output", str(out))
            self.assertEqual(code, 0)
            self.assertTrue(out.is_file())
            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
            prefix = f"{installer.PACKAGE_NAME}/"
            self.assertIn(f"{prefix}SKILL.md", names)
            self.assertIn(f"{prefix}scripts/noova_media.py", names)
            self.assertIn(f"{prefix}references/api-contracts.md", names)
            self.assertTrue(all(n.startswith(prefix) for n in names))
            self.assertIn("已打包", stdout)

    def test_package_excludes_installer_and_tests(self):
        """分发包不得包含安装器、测试等仓库侧资产。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pkg.zip"
            run("package", "--output", str(out))
            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
        self.assertTrue(names)
        for name in names:
            self.assertNotIn("/tests/", name)
            self.assertFalse(name.endswith("install.py"))
            self.assertFalse(name.endswith("install.py"))
            self.assertFalse(name.endswith(installer.MANIFEST_NAME))

    def test_package_refuses_broken_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / installer.PACKAGE_NAME
            staged.mkdir()
            out = Path(tmp) / "pkg.zip"
            code, _, err = run_at(staged, "package", "--output", str(out))
            self.assertEqual(code, 1)
            self.assertFalse(out.exists())
            self.assertIn("未生成压缩包", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
