#!/usr/bin/env python3
"""第四轮审计修复的回归测试（仅标准库 unittest，不联网、不消耗积分）。

这个文件**只放「曾经真实出过问题」的用例**，每条都对应一个已复现的缺陷，
并写明「修复前会怎样」。目的是：任何人把行为改回去，这里立刻变红。

运行：
    cd tests && python -m unittest discover -s . -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
SKILL_DIR = PROJECT / "skills" / "noova-generation"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import noova_common as nc  # noqa: E402
import noova_key  # noqa: E402
import noova_media as nm  # noqa: E402

# ---------------------------------------------------------------------------
# 模块级隔离：**默认**把所有配置读写指向临时目录
# ---------------------------------------------------------------------------
# 为什么必须是模块级而不是逐类 opt-in：曾经有个用例忘了继承隔离基类，于是
# `setup --no-verify` 把测试用的假 Key 写进了**用户真实的** `~/.noova/config.json`
# （实测发现）。逐类 opt-in 的隔离只要漏一次就会污染用户机器，因此这里改成
# 「默认隔离，需要真实解析器的用例显式覆盖」。
_MODULE_TMP: "tempfile.TemporaryDirectory | None" = None
_SAVED_CONFIG_DIR: "str | None" = None


def setUpModule():
    global _MODULE_TMP, _SAVED_CONFIG_DIR
    _MODULE_TMP = tempfile.TemporaryDirectory()
    _SAVED_CONFIG_DIR = os.environ.get("NOOVA_CONFIG_DIR")
    os.environ["NOOVA_CONFIG_DIR"] = str(Path(_MODULE_TMP.name) / "isolated")


def tearDownModule():
    global _MODULE_TMP, _SAVED_CONFIG_DIR
    if _SAVED_CONFIG_DIR is None:
        os.environ.pop("NOOVA_CONFIG_DIR", None)
    else:
        os.environ["NOOVA_CONFIG_DIR"] = _SAVED_CONFIG_DIR
    if _MODULE_TMP is not None:
        _MODULE_TMP.cleanup()
        _MODULE_TMP = None


def run_key(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = noova_key.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_media(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = nm.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class IsolatedConfigCase(unittest.TestCase):
    """把配置固定到临时目录，并清掉可能影响结果的环境变量。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_env = dict(os.environ)
        for name in ("NOOVA_API_KEY", "NOOVA_BASE_URL", "NOOVA_CONFIG_DIR",
                     nc.ALLOW_LOCAL_ENV, nc.ALLOW_INSECURE_ENV, "NOOVA_DEBUG"):
            os.environ.pop(name, None)
        self.config = Path(self._tmp.name) / "config.json"
        self._patch = mock.patch.object(noova_key, "_config_candidates",
                                        lambda: [self.config])
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)


# ---------------------------------------------------------------------------
# B1 —— 粘贴文本里的任意 URL 都曾被当作 API 地址（Key 会发往该域名并被持久化）
# ---------------------------------------------------------------------------

class BaseUrlProvenanceTest(unittest.TestCase):
    """基础地址只认可信来源；其它 URL 一律忽略。"""

    def test_documentation_link_is_not_used_as_base_url(self):
        """修复前：base_url 变成 evil.example.com，校验请求带着 Key 发过去。"""
        result = noova_key.parse_credentials(
            "https://evil.example.com/steal  my key sk-abc123456")
        self.assertEqual(result["api_key"], "sk-abc123456")
        self.assertEqual(result["base_url"], "")
        self.assertEqual(result["ignored_urls"], ["https://evil.example.com"])

    def test_trailing_doc_link_is_not_used_as_base_url(self):
        result = noova_key.parse_credentials(
            "api_key=sk-abc123456  see docs https://docs.example.org/setup")
        self.assertEqual(result["base_url"], "")
        self.assertEqual(result["ignored_urls"], ["https://docs.example.org"])

    def test_json_base_url_field_is_still_trusted(self):
        """JSON 的 `base_url` 字段是**显式**来源，必须仍然生效（自建部署场景）。"""
        result = noova_key.parse_credentials(
            '{"api_key": "sk-abc123456", "base_url": "https://ai.mycorp.cn"}')
        self.assertEqual(result["base_url"], "https://ai.mycorp.cn")

    def test_official_domain_in_text_is_accepted(self):
        result = noova_key.parse_credentials("sk-abc123456 见 https://noova.vip/api_control")
        self.assertEqual(result["base_url"], "https://noova.vip")
        self.assertEqual(result["ignored_urls"], [])

    def test_setup_warns_about_ignored_url_in_success_path(self):
        """修复前：那条「多个地址」告警只在「Key 和地址都没识别到」时才打印，
        成功路径上用户完全不知道地址被改成了别的域名。"""
        with mock.patch.object(noova_key, "verify_key",
                               return_value={"ok": True, "status": 200, "reason": "",
                                             "credit": 1}):
            code, out, _ = run_key("setup", "sk-abcdefg123 https://evil.example.com/x",
                                   "--no-verify")
        self.assertEqual(code, 0)
        self.assertIn("已忽略", out)
        self.assertNotIn("evil.example.com", out.split("已忽略")[0])


class LocalBaseRejectionTest(IsolatedConfigCase):
    """用户**有意**粘贴的本机地址必须明确拒绝，不能静默改成官方域名。"""

    def test_local_url_in_paste_is_rejected(self):
        code, out, err = run_key("setup", "sk-abcdefg123 http://localhost:5001")
        self.assertEqual(code, 2)
        self.assertIn("本机", out + err)
        self.assertFalse(self.config.exists(), "被拒的地址不得产生配置文件")

    def test_local_url_json_mode_is_machine_readable(self):
        code, out, _ = run_key("setup", "sk-abcdefg123 http://127.0.0.1:5000", "--json")
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["rejected_base_url"], "http://127.0.0.1:5000")
        self.assertNotIn("sk-abcdefg123", out)


# ---------------------------------------------------------------------------
# B2 —— verify 出网没有任何 base 守卫
# ---------------------------------------------------------------------------

class VerifyGuardTest(unittest.TestCase):
    def test_verify_never_sends_key_to_a_local_base(self):
        """修复前：`verify` 会把 Authorization 头发到 NOOVA_BASE_URL 指向的任意地址。"""
        called = []

        def boom(*args, **kwargs):
            called.append(args)
            raise AssertionError("不得发起请求")

        with mock.patch.object(noova_key, "open_credentialed", side_effect=boom):
            result = noova_key.verify_key("sk-x", "http://127.0.0.1:9")
        self.assertEqual(called, [], "守卫必须在发请求前拦下")
        self.assertTrue(result["blocked"])
        self.assertIsNone(result["ok"])

    def test_verify_command_reports_blocked_and_exits_2(self):
        with mock.patch.dict(os.environ, {"NOOVA_API_KEY": "sk-x",
                                          "NOOVA_BASE_URL": "http://127.0.0.1:9"}):
            code, out, err = run_key("verify")
        self.assertEqual(code, 2)
        self.assertIn("Key 未离开本机", err)

    def test_public_probe_is_guarded_too(self):
        with mock.patch.object(noova_key, "open_credentialed",
                               side_effect=AssertionError("不得发起请求")):
            result = noova_key.public_models("http://10.0.0.5:8000")
        self.assertEqual(result["models"], {})
        self.assertIn("本机", result["reason"])


# ---------------------------------------------------------------------------
# B3 —— 地址守卫的三处绕过
# ---------------------------------------------------------------------------

class BaseUrlBypassTest(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        for name in (nc.ALLOW_LOCAL_ENV, nc.ALLOW_INSECURE_ENV):
            os.environ.pop(name, None)
        nc.clear_resolve_cache()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        nc.clear_resolve_cache()

    def _check(self, url: str, dns_verdict: "bool | None" = False) -> dict:
        """跑一次守卫，并把 DNS 判定固定住。

        `resolves_to_local()` 的真实结果取决于运行环境的解析器（企业 DNS / 公共 DNS /
        离线 CI 各不相同），不固定它就会让本类用例在别人的机器上随机变红。
        默认「解析到公网」（`False`）；要测离线用 `None`，要测通配 DNS 指向回环用 `True`。
        """
        with mock.patch.object(nc, "resolves_to_local", return_value=dns_verdict):
            return nc.check_base_url(url)

    def test_trailing_dot_fqdn_is_caught(self):
        """修复前：`http://localhost.` → ok=True（getaddrinfo 解析到 127.0.0.1）。

        归一化后 `localhost.` → `localhost` 会被**字面量**判据拦下，不依赖 DNS。
        """
        result = self._check("http://localhost.")
        self.assertFalse(result["ok"])
        self.assertTrue(result["local"])

    def test_wildcard_dns_to_loopback_is_caught(self):
        """修复前：`127.0.0.1.nip.io` 字面量正常，纯字面量守卫拦不住。

        这里**注入**解析结果（而不是依赖真实 DNS）：`nip.io` 能不能解析出
        127.0.0.1 取决于运行环境的解析器，靠真实 DNS 会让用例在离线 /
        受限网络的 CI 上随机变红。被测的是「解析到回环时必须拦下」这一逻辑。
        """
        self.assertFalse(self._check("https://127.0.0.1.nip.io:8080", dns_verdict=True)["ok"])

    def test_userinfo_disguise_is_rejected(self):
        """`https://noova.vip@evil.example.com` 的真实 host 是 evil.example.com。"""
        result = self._check("https://noova.vip@evil.example.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["host"], "evil.example.com")

    def test_numeric_ip_shorthand_is_rejected(self):
        for url in ("http://2130706433", "http://0x7f000001", "http://0177.0.0.1"):
            self.assertFalse(self._check(url)["ok"], url)

    def test_plain_http_public_host_is_rejected(self):
        """Key 走 Authorization 头，明文链路等于泄露凭据。"""
        result = self._check("http://ai.mycorp.cn")
        self.assertFalse(result["ok"])
        self.assertFalse(result["secure"])

    def test_escape_hatches_are_case_insensitive(self):
        """修复前：`=TRUE` 静默不生效——安全开关「看起来开了其实没开」。"""
        with mock.patch.dict(os.environ, {nc.ALLOW_LOCAL_ENV: "TRUE"}):
            self.assertTrue(self._check("http://localhost:5001")["ok"])
        with mock.patch.dict(os.environ, {nc.ALLOW_INSECURE_ENV: "TRUE"}):
            self.assertTrue(self._check("http://ai.mycorp.cn")["ok"])

    def test_cross_origin_redirect_is_refused(self):
        """修复前：302 会把 Authorization 头原样复制到未校验的主机。"""
        import urllib.error
        import urllib.request
        handler = nc.SameOriginRedirectHandler()
        request = urllib.request.Request(
            "https://noova.vip/api/v1/credit",
            headers={"Authorization": "Bearer sk-x"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(request, None, 302, "Found", {},
                                     "http://127.0.0.1:9/steal")
        self.assertIn("跨域重定向", str(ctx.exception))

    def test_public_redirect_handler_refuses_loopback(self):
        import urllib.error
        import urllib.request
        handler = nc.PublicRedirectHandler()
        request = urllib.request.Request("https://cdn.example.org/x")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, "Found", {},
                                     "http://127.0.0.1:9/evil.png")


# ---------------------------------------------------------------------------
# B4 —— 解析器曾优先取 token= / secret=，压过真正的 sk- Key
# ---------------------------------------------------------------------------

class KeyPrecedenceTest(unittest.TestCase):
    def test_sk_key_wins_over_generic_token_field(self):
        """修复前：key='zzzzzzzzzzzz'，于是有效 Key 被报「无效」并拒绝写入。"""
        blob = "token=zzzzzzzzzzzz and the real key sk-abc123456"
        self.assertEqual(noova_key.parse_credentials(blob)["api_key"], "sk-abc123456")

    def test_sk_key_wins_inside_a_cookie_header(self):
        blob = ("Cookie: token=abcdefgh12345; Authorization: Bearer sk-abc123456")
        self.assertEqual(noova_key.parse_credentials(blob)["api_key"], "sk-abc123456")

    def test_api_key_label_still_works_without_sk_prefix(self):
        for blob in ("api_key=abcdefgh12345", "密钥：abcdefgh12345",
                     "NOOVA_API_KEY=abcdefgh12345"):
            self.assertEqual(noova_key.parse_credentials(blob)["api_key"],
                             "abcdefgh12345", blob)


# ---------------------------------------------------------------------------
# H1 —— --json 路径曾完全绕过域名脱敏
# ---------------------------------------------------------------------------

class JsonRedactionTest(unittest.TestCase):
    def test_internal_host_is_redacted_in_json_payload(self):
        payload = {"status": "succeeded", "gateway": "internal-gw.corp.local",
                   "note": "see https://internal-oss.internal-corp.com/bucket/x"}
        safe = nm._safe_obj(payload)
        text = json.dumps(safe, ensure_ascii=False)
        self.assertNotIn("internal-gw.corp.local", text)
        self.assertNotIn("internal-corp.com", text)
        self.assertIn("media.example.com", text)

    def test_media_urls_are_preserved_verbatim(self):
        """铁律 7：结果 URL 是功能字段，必须原样交付。"""
        url = "https://bucket-9.internal-oss.internal-corp.com/out/a.png"
        payload = {"status": "succeeded", "result": url, "results": [{"url": url}]}
        safe = nm._safe_obj(payload)
        self.assertEqual(safe["result"], url)
        self.assertEqual(safe["results"][0]["url"], url)

    def test_uppercase_scheme_is_redacted(self):
        """修复前：`HTTPS://internal.corp/x` 因大小写不敏感缺失而原样通过。"""
        out = nm.sanitize_text("HTTPS://internal.corp/x and internal.corp:8080/y")
        self.assertNotIn("internal.corp", out)

    def test_control_characters_are_stripped(self):
        out = nm.sanitize_text("before\x1b[31mRED\x1b[0m\x07after")
        self.assertEqual(out, "beforeREDafter")

    def test_no_wait_json_stdout_is_parseable(self):
        """修复前：`image --no-wait --json` 把「[已提交]…」打到 stdout，agent 解析崩溃。"""
        model = {"code": "m1", "display_name": "M1", "model_type": "image",
                 "request_format": "openai", "cost_text": "4 积分/次",
                 "billing_mode": "per_call", "billing_price": 4, "billing_unit": "",
                 "doc": "", "ability": [], "alt_codes": [], "description": "",
                 "status": "online", "input_price_per_1m": None,
                 "output_price_per_1m": None}
        args = mock.Mock(json=True, quiet=False, no_wait=True, no_credit=True,
                         timeout=90, max_wait=None, poll_interval=6, param=[],
                         ref_image=None, ref_video=None, ref_audio=None, prompt="x")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(nm, "_request",
                               return_value={"status": "running", "id": "T1"}), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = nm._create_task(model, {"prompt": "x"}, args, "sk-x")
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())     # 必须能解析
        self.assertEqual(payload["task_id"], "T1")
        # 人读信息块改走 stderr，stdout 保持纯净
        self.assertIn("任务 ID", err.getvalue())


# ---------------------------------------------------------------------------
# H3 —— task --id 曾把「失败」报成「仍在处理中」且退出码 0
# ---------------------------------------------------------------------------

class TaskStatusReportingTest(unittest.TestCase):
    def _task(self, payload: dict, *extra: str):
        with mock.patch.object(nm, "_request", return_value=payload), \
             mock.patch.object(nm, "_require_key", return_value="sk-x"):
            return run_media("task", "--id", "9", *extra)

    def test_failed_task_exits_nonzero_and_does_not_say_processing(self):
        code, out, err = self._task({"status": "failed", "error": "boom"})
        self.assertEqual(code, 1)
        self.assertIn("boom", out)
        self.assertNotIn("仍在处理中", out)
        self.assertIn("不要继续等待", err)

    def test_failed_task_json_exit_code_matches_human_mode(self):
        code, out, _ = self._task({"status": "failed", "error": "boom"}, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "failed")

    def test_running_task_still_exits_zero(self):
        code, out, _ = self._task({"status": "running", "progress": 30})
        self.assertEqual(code, 0)
        self.assertIn("仍在处理中", out)

    def test_succeeded_without_urls_is_not_success(self):
        """修复前：无产出的运行被报成成功（exit 0）。"""
        code, _, err = self._task({"status": "succeeded"})
        self.assertEqual(code, 1)
        self.assertIn("没有结果地址", err)

    def test_cancelled_and_expired_are_terminal_failures(self):
        for status in ("canceled", "cancelled", "expired", "error", "failure"):
            code, out, _ = self._task({"status": status})
            self.assertEqual(code, 1, status)
            self.assertNotIn("仍在处理中", out)


class StatusVocabularyTest(unittest.TestCase):
    """状态词表：未完成 → 继续轮询；成功/失败 → 终态。**未知状态必须继续轮询**。"""

    PENDING = ("NOT_START", "queued", "submitted", "in_progress", "processing",
               "running", "unknown", "", "weird-status", "SOMETHING_NEW")
    SUCCESS = ("succeeded", "success", "completed", "complete", "finished")
    FAILURE = ("failed", "failure", "error", "canceled", "cancelled", "expired")

    def test_pending_statuses_keep_polling(self):
        for status in self.PENDING:
            with self.subTest(status=status):
                self.assertNotIn(status.lower(), nm.TERMINAL_OK)
                self.assertNotIn(status.lower(), nm.TERMINAL_FAIL)

    def test_success_statuses_are_terminal_ok(self):
        for status in self.SUCCESS:
            with self.subTest(status=status):
                self.assertIn(status, nm.TERMINAL_OK)

    def test_failure_statuses_are_terminal_fail(self):
        for status in self.FAILURE:
            with self.subTest(status=status):
                self.assertIn(status, nm.TERMINAL_FAIL)

    def test_every_known_status_has_a_chinese_label(self):
        """清单与报错都按词表给中文标签；标签缺失会让用户看到原始英文状态。"""
        known = (*self.SUCCESS, *self.FAILURE,
                 "NOT_START", "queued", "submitted", "in_progress", "processing",
                 "running", "unknown")
        for status in known:
            with self.subTest(status=status):
                self.assertIn(status.lower(), nm.STATUS_LABELS)

    def test_unknown_status_falls_back_to_a_readable_label(self):
        """词表外的状态也要有可读文案（回退为原值或「处理中」），不能是空串。"""
        self.assertTrue(nm._status_label("brand-new-status"))
        self.assertEqual(nm._status_label(""), "处理中")

    def test_unknown_status_keeps_polling_until_timeout(self):
        """最面向用户的规则：词表外按「处理中」，绝不误报失败。"""
        args = mock.Mock(max_wait=1, poll_interval=2, quiet=True, timeout=30)
        with mock.patch.object(nm, "_request",
                               return_value={"status": "brand-new-status"}), \
             mock.patch.object(nm.time, "sleep", lambda *_: None):
            with self.assertRaises(nm.NoovaError) as ctx:
                nm._poll_until_done("T1", "sk-x", args, "image")
        self.assertIn("等待超时", str(ctx.exception))


# ---------------------------------------------------------------------------
# H4 —— 未捕获异常曾把 traceback（含 skill 绝对安装路径）打到用户面前
# ---------------------------------------------------------------------------

class NoTracebackLeakTest(unittest.TestCase):
    def test_unexpected_error_is_reported_without_traceback(self):
        with mock.patch.object(nm, "build_parser",
                               side_effect=RuntimeError("boom")), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOOVA_DEBUG", None)
            code, out, err = run_media("models")
        self.assertEqual(code, 1)
        self.assertIn("未预期的内部错误", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn(str(SKILL_DIR), err)

    def test_debug_flag_reenables_traceback_for_developers(self):
        """排障开关：设了 `NOOVA_DEBUG` 才打印堆栈（仅开发者本地使用）。"""
        with mock.patch.dict(os.environ, {"NOOVA_DEBUG": "1"}), \
             mock.patch.object(nm, "build_parser",
                               side_effect=RuntimeError("boom")):
            code, _, err = run_media("models")
        self.assertEqual(code, 1)
        self.assertIn("Traceback", err)

    def test_socket_timeout_is_caught_on_python38(self):
        """`socket.timeout` 在 3.8/3.9 上不是 `TimeoutError` 的别名（3.10 才合并）。"""
        import socket
        with mock.patch.object(nm, "_open", side_effect=socket.timeout("timed out")):
            with self.assertRaises(nm.NoovaError) as ctx:
                nm._request("GET", "/api/v1/credit")
        self.assertIn("超时", str(ctx.exception))

    def test_incomplete_read_is_translated_not_raised_raw(self):
        import http.client
        with mock.patch.object(nm, "_open",
                               side_effect=http.client.IncompleteRead(b"partial")):
            with self.assertRaises(nm.NoovaError) as ctx:
                nm._request("GET", "/api/v1/credit")
        self.assertIn("网络传输中断", str(ctx.exception))


# ---------------------------------------------------------------------------
# H5 —— 自动重试曾施加在非幂等的计费 POST 上（重复扣费风险）
# ---------------------------------------------------------------------------

class BillingRetryTest(unittest.TestCase):
    def test_task_creation_disables_automatic_retry(self):
        seen = {}

        def fake_request(method, path, **kwargs):
            seen["path"] = path
            seen["retry"] = kwargs.get("retry", True)
            return {"status": "running", "id": "T1"}

        model = {"code": "m1", "display_name": "M1", "model_type": "image",
                 "cost_text": "", "billing_mode": "per_call",
                 "billing_price": 1, "billing_unit": "", "doc": ""}
        args = mock.Mock(json=True, quiet=True, no_wait=True, no_credit=True,
                         timeout=90, max_wait=None, poll_interval=6, param=[],
                         ref_image=None, ref_video=None, ref_audio=None, prompt="x")
        with mock.patch.object(nm, "_request", side_effect=fake_request):
            nm._create_task(model, {"prompt": "x"}, args, "sk-x")
        self.assertEqual(seen["path"], nm.TASK_CREATE_PATH)
        self.assertFalse(seen["retry"], "计费 POST 必须关闭自动重试")

    def test_chat_post_disables_automatic_retry(self):
        seen = {}
        model = {"code": "t1", "display_name": "T1", "model_type": "text",
                 "status": "online", "request_format": "openai",
                 "doc": "", "cost_text": "", "billing_mode": "per_token",
                 "billing_price": 0, "billing_unit": "", "ability": [],
                 "input_price_per_1m": None, "output_price_per_1m": None,
                 "alt_codes": [], "description": ""}

        def fake_request(method, path, **kwargs):
            seen["retry"] = kwargs.get("retry", True)
            return {"choices": [{"message": {"content": "hi"}}]}

        args = mock.Mock(prompt="x", model="t1", protocol="auto", system=None,
                         max_tokens=None, temperature=None, stream=False, json=True,
                         quiet=True, param=[], image=[], show_usage=False,
                         no_credit=True, timeout=180)
        with mock.patch.object(nm, "_require_key", return_value="sk-x"), \
             mock.patch.object(nm, "_resolve_model", return_value=model), \
             mock.patch.object(nm, "_request", side_effect=fake_request):
            nm.cmd_chat(args)
        self.assertFalse(seen["retry"], "文本生成也是计费请求，必须关闭自动重试")


# ---------------------------------------------------------------------------
# M 级 —— 状态/退出码「说谎」类问题
# ---------------------------------------------------------------------------

class CreditHonestyTest(unittest.TestCase):
    def test_zero_delta_is_not_reported_as_zero_charge(self):
        """修复前：`本次扣减：0 积分` —— 会被读成「免费」。"""
        lines = nm._credit_lines({"total": 100.0}, {"total": 100.0})
        text = "\n".join(lines)
        self.assertNotIn("本次扣减：0", text)
        self.assertIn("未产生扣费", text)

    def test_positive_delta_still_reported(self):
        lines = nm._credit_lines({"total": 100.0}, {"total": 96.0})
        self.assertIn("本次扣减：4", "\n".join(lines))

    def test_wait_does_not_fabricate_a_deduction(self):
        """修复前：`wait` 的「生成前快照」取自创建之后，差值恒为 0。"""
        source = (SKILL_DIR / "scripts" / "noova_media.py").read_text(encoding="utf-8")
        wait_body = source.split("def cmd_wait(args)")[1].split("def cmd_credit")[0]
        self.assertIn("credit_before=None", wait_body)


class MaxWaitValidationTest(unittest.TestCase):
    def test_zero_and_negative_are_rejected(self):
        """修复前：0 被当成「没给」而静默套默认值，负数被压成 1 秒。"""
        for value in (0, -5):
            with self.subTest(value=value):
                args = mock.Mock(max_wait=value)
                with self.assertRaises(nm.NoovaError):
                    nm._resolve_wait_limits(args, "image")

    def test_positive_value_wins(self):
        self.assertEqual(nm._resolve_wait_limits(mock.Mock(max_wait=42), "image"), 42)

    def test_none_falls_back_to_type_default(self):
        self.assertEqual(nm._resolve_wait_limits(mock.Mock(max_wait=None), "video"), 5400)


class ClearAllSourcesTest(IsolatedConfigCase):
    def test_clear_removes_key_from_every_source(self):
        """修复前：只 pop 最高优先级那个 → 打印「已清除」但 Key 仍然生效。"""
        secondary = Path(self._tmp.name) / "secondary.json"
        secondary.write_text(json.dumps({"api_key": "sk-secondary"}), encoding="utf-8")
        self.config.write_text(json.dumps({"api_key": "sk-primary"}), encoding="utf-8")
        with mock.patch.object(noova_key, "_config_candidates",
                               lambda: [self.config, secondary]):
            code, out, _ = run_key("clear", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["cleared"])
        self.assertEqual(len(payload["config_files"]), 2)
        for path in (self.config, secondary):
            self.assertNotIn("api_key", json.loads(path.read_text(encoding="utf-8")))


class ConfigAtomicityTest(IsolatedConfigCase):
    def test_write_is_atomic_and_leaves_no_temp_file(self):
        noova_key._save({"api_key": "sk-x"})
        leftovers = list(self.config.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["api_key"],
                         "sk-x")

    def test_corrupt_config_is_reported_not_silently_ignored(self):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text('{"api_key": "sk-broken",', encoding="utf-8")
        code, out, _ = run_key("doctor", "--offline", "--json")
        checks = {c["id"]: c for c in json.loads(out)["checks"]}
        self.assertFalse(checks["config_parse"]["ok"])
        self.assertEqual(code, 1)


class EnvOverrideFailureTest(unittest.TestCase):
    """`NOOVA_CONFIG_DIR` 指向不可用目录时必须**报错**，不能静默改道。

    修复前：回退到家目录 → 提示说写到 A、实际写到 B，用户按提示改 A 却改不到生效那份。
    本类**刻意不继承** `IsolatedConfigCase`——那个基类会把 `_config_candidates` 打桩掉，
    而本用例要验证的正是真实解析器在覆盖目录不可用时的行为。
    """

    def test_unusable_override_raises_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocked"
            blocker.write_text("i am a file, not a directory", encoding="utf-8")
            with mock.patch.dict(os.environ, {"NOOVA_CONFIG_DIR": str(blocker)}):
                with self.assertRaises(noova_key.ConfigError) as ctx:
                    noova_key.config_file()
            self.assertIn("NOOVA_CONFIG_DIR", str(ctx.exception))

    def test_display_resolution_does_not_raise(self):
        """展示路径（create=False）不能因为目录不可用而崩——引导块还要打出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocked"
            blocker.write_text("file", encoding="utf-8")
            with mock.patch.dict(os.environ, {"NOOVA_CONFIG_DIR": str(blocker)}):
                self.assertEqual(noova_key.config_file(create=False),
                                 blocker / "config.json")


class DoctorCheckCountTest(IsolatedConfigCase):
    def test_nine_checks_and_docs_agree(self):
        code, out, _ = run_key("doctor", "--offline", "--json")
        ids = [c["id"] for c in json.loads(out)["checks"]]
        self.assertEqual(len(ids), 9)
        # SKILL.md 与 troubleshooting.md 都写了项数，必须与实现一致
        for name in ("SKILL.md", "references/troubleshooting.md"):
            text = (SKILL_DIR / name).read_text(encoding="utf-8")
            self.assertTrue("9 项" in text or "九项" in text,
                            f"{name} 未同步 doctor 项数（应为 9 项）")
            for stale in ("8 项", "八项", "7 项", "七项"):
                self.assertNotIn(stale, text, f"{name} 仍写着旧的项数：{stale}")

    def test_local_allow_flag_does_not_make_doctor_green(self):
        """修复前：放行本机地址时 doctor 的「基础地址为线上域名」也报 ok=true，
        自检因此失去了发现它的能力。"""
        with mock.patch.dict(os.environ, {nc.ALLOW_LOCAL_ENV: "1",
                                          "NOOVA_BASE_URL": "http://localhost:5001"}):
            _, out, _ = run_key("doctor", "--offline", "--json")
        checks = {c["id"]: c for c in json.loads(out)["checks"]}
        self.assertIsNone(checks["base_url_online"]["ok"])


class RequiredParamFieldMappingTest(unittest.TestCase):
    """参考图字段名必须跟着运行时契约走。

    平台不同模型给参考图的字段名不一样（`referenceImages` / `images` …）。
    本工具对外是 `--ref-image`，映射错了参考图就被静默丢掉——
    用户以为传了，实际生成里没有。
    """

    @staticmethod
    def _model(*names, code="m1"):
        return nm._normalize_model({
            "model": code, "displayName": "M", "modelType": "image",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
            "surchargeRules": [],
            "params": [{"name": n, "type": "string[]", "required": True, "label": n}
                       for n in names]})

    def test_reference_field_name_follows_the_contract(self):
        model = self._model("prompt", "images")
        self.assertEqual(nm._reference_field_map(model).get("ref_image"), "images")
        args = mock.Mock(prompt="x", param=[], ref_image=[], ref_video=None,
                         ref_audio=None, image=None)
        with self.assertRaises(nm.NoovaError) as ctx:
            nm._ensure_required_params(model, args)
        self.assertIn("images", str(ctx.exception))

    def test_standard_reference_images_still_mapped(self):
        model = self._model("referenceImages")
        self.assertEqual(nm._reference_field_map(model).get("ref_image"),
                         "referenceImages")

    def test_no_params_means_no_mapping(self):
        """契约里没有参数表时不能瞎猜字段名——宁可不映射，也不要发明一个。"""
        self.assertEqual(nm._reference_field_map(self._model()), {})
        self.assertEqual(nm._reference_field_map(None), {})


class InvocationSpecTruthfulnessTest(unittest.TestCase):
    def test_route_without_mapping_reports_no_stream_support(self):
        """未映射的协议不得虚报成支持流式。"""
        model = nm._normalize_model({
            "model": "t1", "displayName": "T", "modelType": "text",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_token", "pricePer1M": {"input": 1, "output": 2}},
            "surchargeRules": [], "params": []})
        spec = nm._invocation_spec(model)
        route = next(r for r in spec["routes"] if r["protocol"] == "openai")
        self.assertEqual(route["path"], "/v1/chat/completions")
        self.assertTrue(route["stream_supported"])

    def test_create_route_carries_http_method(self):
        model = nm._normalize_model({
            "model": "t1", "displayName": "T", "modelType": "text",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_token", "pricePer1M": {"input": 1, "output": 2}},
            "surchargeRules": [], "params": []})
        self.assertTrue(nm._invocation_spec(model)["create"].startswith("POST "))

    def test_task_route_carries_http_method(self):
        model = nm._normalize_model({
            "model": "m1", "displayName": "M", "modelType": "image",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
            "surchargeRules": [], "params": []})
        spec = nm._invocation_spec(model)
        self.assertTrue(spec["create"].startswith("POST "))
        self.assertTrue(spec["poll"].startswith("POST "))


class PromptValidationTest(unittest.TestCase):
    def test_empty_prompt_is_rejected_with_the_real_option_name(self):
        """修复前：提示写 `--param prompt=值` —— 那条命令不存在。"""
        args = mock.Mock(prompt="   ")
        with self.assertRaises(nm.NoovaError) as ctx:
            nm._require_prompt(args)
        self.assertIn("--prompt", str(ctx.exception))
        self.assertNotIn("--param prompt", str(ctx.exception))


class EnumValueVsLabelTest(unittest.TestCase):
    """枚举参数的「取值」与「展示标签」不得混用（真实缺陷，已线上复现）。

    复现链：参数面板把 `1280x720` 渲染成它的标签 `16:9` → agent 照抄
    `--param aspect_ratio=16:9` → 平台判定取值非法，**HTTP 400**。
    实测平台上有 6 个模型存在 `value != label` 的参数（比例类像素串），
    且集中在图像模型上。
    """

    LABELS = {"1280x720": "16:9", "720x1280": "9:16"}

    def _model(self):
        return nm._normalize_model({
            "model": "img-x", "displayName": "Img X", "modelType": "image",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 6.0, "unit": 1},
            "surchargeRules": [],
            "params": [{"name": "prompt", "type": "string", "required": True,
                        "label": "提示词"},
                       {"name": "aspect_ratio", "type": "string", "required": False,
                        "label": "比例", "values": ["auto", "1280x720", "720x1280"],
                        "valueLabels": self.LABELS}]})

    # ---- A) 人读面板必须给出「可传的取值」 ----

    def test_human_panel_shows_the_value_not_only_the_label(self):
        """修复前：面板只打印 `16:9`，用户/agent 照抄即成非法取值。"""
        import contextlib
        import io
        panel = nm.build_param_form(self._model())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nm._render_form(panel)
        text = out.getvalue()
        self.assertIn("1280x720", text, "人读面板丢了可传取值")
        self.assertIn("16:9", text, "人读面板丢了展示标签")

    def test_json_and_human_panels_agree_on_the_value(self):
        """两条渲染口径不得再分叉（JSON 一直是对的，错的只有人读那条）。"""
        import contextlib
        import io
        panel = nm.build_param_form(self._model())
        field = next(f for f in panel["fields"] if f["name"] == "aspect_ratio")
        self.assertEqual([o["value"] for o in field["options"]],
                         ["auto", "1280x720", "720x1280"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nm._render_form(panel)
        for option in field["options"]:
            self.assertIn(option["value"], out.getvalue())

    def test_choice_values_never_exceed_80_columns(self):
        """取值多时也要折行：实测 14 个取值的 `取值（标签）` 单行可达 280 列，
        终端折行把取值与标签拆散，反而更容易抄错（本版缺陷的成因之一）。"""
        import contextlib
        import io
        model = nm._normalize_model({
            "model": "img-z", "displayName": "Img Z", "modelType": "image",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
            "surchargeRules": [],
            "params": [{"name": "aspect_ratio", "type": "string", "required": False,
                        "label": "比例",
                        "values": [f"{1000 + i}x{2000 + i}" for i in range(20)],
                        "valueLabels": {f"{1000 + i}x{2000 + i}": f"ratio-{i}"
                                        for i in range(20)}}]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nm._render_form(nm.build_param_form(model))
        too_wide = [(nm._display_width(line), line) for line in out.getvalue().splitlines()
                    if nm._display_width(line) > 80]
        self.assertEqual(too_wide, [], f"超过 80 列的行：{too_wide}")
        # 折行不得吃掉任何取值
        for i in range(20):
            self.assertIn(f"{1000 + i}x{2000 + i}", out.getvalue())

    def test_fixed_field_keeps_printing_the_value(self):
        """`fixed` 分支一直打印 value——修 choice 时不得把它改坏。"""
        import contextlib
        import io
        model = nm._normalize_model({
            "model": "img-y", "displayName": "Img Y", "modelType": "image",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
            "surchargeRules": [],
            "params": [{"name": "resolution", "type": "string", "required": False,
                        "label": "分辨率", "values": ["720p"]}]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nm._render_form(nm.build_param_form(model))
        self.assertIn("720p", out.getvalue())

    # ---- C) 发请求前必须按契约预检取值，不得白跑一次往返 ----

    def _args(self, **kw):
        base = {"param": [], "prompt": "x", "ref_image": None,
                "ref_video": None, "ref_audio": None}
        base.update(kw)
        return mock.Mock(**base)

    def test_label_instead_of_value_is_rejected_before_the_request(self):
        """修复前：本地不校验，一路发到平台才 400。"""
        args = self._args(param=[("aspect_ratio", "16:9")])
        with self.assertRaises(nm.NoovaError) as ctx:
            nm._ensure_param_values(self._model(), args)
        rendered = ctx.exception.render()
        self.assertIn("取值不合法", rendered)
        self.assertIn("1280x720", rendered, "报错必须给出合法取值")
        self.assertIn("1280x720", rendered)

    def test_label_misuse_hint_names_the_correct_value(self):
        """把标签当取值传是最高频误用：提示要直接给出「应传什么」。"""
        args = self._args(param=[("aspect_ratio", "16:9")])
        with self.assertRaises(nm.NoovaError) as ctx:
            nm._ensure_param_values(self._model(), args)
        self.assertIn("标签", ctx.exception.hint or "")
        self.assertIn("1280x720", ctx.exception.hint or "")

    def test_legal_value_passes(self):
        args = self._args(param=[("aspect_ratio", "1280x720")])
        nm._ensure_param_values(self._model(), args)   # 不抛异常

    def test_free_text_param_is_never_blocked(self):
        """`values` 为空 = 自由文本，任何情况下都不得被本地拦下。"""
        args = self._args(param=[("quality", "whatever-the-user-wants")])
        nm._ensure_param_values(self._model(), args)

    def test_unknown_param_name_is_not_blocked_locally(self):
        """契约外的参数名交给平台判定（平台是静默忽略，不阻断）。"""
        args = self._args(param=[("not_in_contract", "anything")])
        nm._ensure_param_values(self._model(), args)

    def test_numeric_normalization_matches_the_platform(self):
        """平台比较取值时会归一化（`5.0` == `5`），本地预检必须同口径，否则误拦。"""
        self.assertEqual(nm._normalize_option_key("5.0"), nm._normalize_option_key(5))
        self.assertEqual(nm._normalize_option_key(True), "true")
        self.assertEqual(nm._normalize_option_key(" 1280x720 "), "1280x720")
        model = nm._normalize_model({
            "model": "v1", "displayName": "V", "modelType": "video",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
            "surchargeRules": [],
            "params": [{"name": "duration", "type": "number", "required": False,
                        "label": "时长", "values": [5, 10]}]})
        nm._ensure_param_values(model, self._args(param=[("duration", "5.0")]))

    def test_no_contract_means_no_validation(self):
        model = nm._normalize_model({
            "model": "n1", "displayName": "N", "modelType": "image",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
            "surchargeRules": [], "params": []})
        nm._ensure_param_values(model, self._args(param=[("aspect_ratio", "16:9")]))


class DeterministicErrorMustNotSuggestRetryTest(unittest.TestCase):
    """确定性失败不得附「建议 N 秒后重试」。

    修复前：400 也把网关的 `Retry-After: 60` 透成重试建议——而取值非法的请求
    重试一万次都是 400。实测 401 也带同一个头，可见该头与「可否恢复」无关。
    """

    def test_400_does_not_suggest_a_retry(self):
        err = nm._friendly_error(400, "参数 比例 取值不合法", None, 60)
        self.assertIsNone(err.retry_after)
        self.assertNotIn("建议", err.render(), "不得给出「建议 N 秒后重试」")
        self.assertIn("取值", err.hint or "")

    def test_403_and_404_stay_silent_about_retry(self):
        for code in (400, 403, 404, 422):
            err = nm._friendly_error(code, "x", None, 60)
            self.assertIsNone(err.retry_after, f"HTTP {code} 是确定性失败")
            self.assertNotIn("建议 60 秒后重试", err.render())

    def test_rate_limit_still_suggests_a_retry(self):
        err = nm._friendly_error(429, "too many", None, 30)
        self.assertEqual(err.retry_after, 30)
        self.assertIn("重试", err.render())

    def test_server_error_still_suggests_a_retry(self):
        for code in (500, 503, 504):
            err = nm._friendly_error(code, "", None, None)
            self.assertIsNotNone(err.retry_after, f"HTTP {code} 是暂时性失败，应可重试")

    def test_platform_quota_still_suggests_a_retry(self):
        err = nm._friendly_error(503, "", {"data": {"source": "platform_quota"}}, None)
        self.assertEqual(err.retry_after, 60)


class Python38CompatibilityTest(unittest.TestCase):
    """静态守住 Python 3.8 兼容性（CI 还会在真实的 3.8 上跑一遍）。

    为什么要有这条静态检查：3.8 是 skill 声明的最低版本，而开发机通常是 3.11/3.12，
    很容易顺手用上 3.9+ 才有的 API（`str.removeprefix` / `Path.is_relative_to` /
    `os.path.isjunction` …）而本地测试全绿。CI 的 3.8 job 会真的跑，但反馈慢；
    这里在本地就立刻报出来。
    """

    PATHS = (
        SKILL_DIR / "scripts" / "noova_common.py",
        SKILL_DIR / "scripts" / "noova_key.py",
        SKILL_DIR / "scripts" / "noova_media.py",
        SKILL_DIR / "scripts" / "noova_upload.py",
        PROJECT / "install.py",
    )
    BANNED_ATTRS = {
        "removeprefix": "str.removeprefix (3.9+)",
        "removesuffix": "str.removesuffix (3.9+)",
        "is_relative_to": "Path.is_relative_to (3.9+)",
        "isjunction": "os.path.isjunction (3.12+)",
        "pairwise": "itertools.pairwise (3.10+)",
        "bit_count": "int.bit_count (3.10+)",
    }
    BANNED_IMPORTS = {"zoneinfo", "graphlib", "tomllib"}

    def test_sources_parse_as_python_38(self):
        import ast
        for path in self.PATHS:
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                ast.parse(source, feature_version=(3, 8))   # 语法不兼容会抛 SyntaxError

    def test_future_annotations_is_present(self):
        """PEP 604（`X | Y`）注解在运行时求值需要 3.10，靠 future import 规避。"""
        for path in self.PATHS:
            with self.subTest(path=path.name):
                self.assertIn("from __future__ import annotations",
                              path.read_text(encoding="utf-8"))

    def test_no_post_38_runtime_apis(self):
        import ast
        for path in self.PATHS:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in self.BANNED_ATTRS:
                    self.fail(f"{path.name}:{node.lineno} 使用了 "
                              f"{self.BANNED_ATTRS[node.attr]}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], self.BANNED_IMPORTS,
                                         f"{path.name}:{node.lineno} 导入了 {alias.name}")


class LineContractRemovalGuardTest(unittest.TestCase):
    """守门：`line`（线路）相关的**全部**出口不得再出现在脚本里。

    这是 v1.10.0 的对外契约删除项。只靠改判用例挡不住「以后有人手滑再加回来」，
    所以这里直接对脚本源码做静态检查——线路是运营手填自由文本、可能过期，
    且**任何数字都不可用于计价**，它的回归就是一次计价误导事故的入口。
    """

    BANNED = (
        "line_label",
        "line_legend",
        'm["line"]',
        "model[\"line\"]",
        "\"line\":",
        "model_family_key(m)[:0] or model.get(\"line\")",
    )

    def test_no_line_symbols_left_in_the_scripts(self):
        for name in ("noova_media.py", "noova_key.py", "noova_common.py", "noova_upload.py"):
            source = (SKILL_DIR / "scripts" / name).read_text(encoding="utf-8")
            for needle in self.BANNED:
                with self.subTest(file=name, needle=needle):
                    self.assertNotIn(needle, source,
                                     f"{name} 仍存在线路残留：{needle}")

    def test_no_exported_line_key_in_any_json_payload(self):
        """`models` / `params` / `model` / `form` 的 JSON 出口都不得含 `line` 键。"""
        for name in ("noova_media.py",):
            source = (SKILL_DIR / "scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn("\"line\"", source)
            self.assertNotIn("'line'", source)


class ConfigIsolationGuardTest(unittest.TestCase):
    """守门：模块级隔离必须生效，任何用例都不得写用户真实的配置目录。

    这条测试的存在理由是一次**真实事故**：某个用例忘了继承隔离基类，
    `setup --no-verify` 把测试用的假 Key 写进了用户的 `~/.noova/config.json`。
    """

    def test_module_isolation_redirects_the_config_path(self):
        path = noova_key.config_file(create=False)
        self.assertIn("isolated", str(path))
        self.assertNotEqual(path.parent, Path.home() / ".noova")

    def test_setup_does_not_touch_the_real_config_file(self):
        """真正的不变量是「真实配置文件**未被改动**」，而不是「它不存在」——
        用户机器上完全可能本来就有配置，那时断言「不存在」会误报。"""
        real = Path.home() / ".noova" / "config.json"
        before = real.read_text(encoding="utf-8") if real.exists() else None
        run_key("setup", "sk-isolation-probe-12345", "--no-verify")
        after = real.read_text(encoding="utf-8") if real.exists() else None
        self.assertEqual(before, after, "测试改动了用户真实的配置文件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
