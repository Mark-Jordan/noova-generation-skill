#!/usr/bin/env python3
"""`noova_key.py` 离线单元测试：输入解析、配置读写、首启引导、自检。

全部为离线测试：不联网、不扣费、不接触真实用户配置
（写盘路径被替换为临时目录）。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "skills" / "noova-generation" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import noova_common as nc  # noqa: E402
import noova_key  # noqa: E402

# ---------------------------------------------------------------------------
# 模块级隔离：**默认**把所有配置读写指向临时目录
# ---------------------------------------------------------------------------
# 逐类 opt-in 的隔离只要漏一次，测试就会把假 Key 写进用户真实的
# `~/.noova/config.json`（已实测发生过）。这里改成「默认隔离」，
# 需要验证真实解析器的用例再显式覆盖 `NOOVA_CONFIG_DIR`。
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


def run(*argv: str) -> tuple[int, str, str]:
    """执行 CLI 并捕获 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = noova_key.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class IsolatedConfigCase(unittest.TestCase):
    """把配置文件固定到临时目录，并清掉可能影响结果的环境变量。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_env = dict(os.environ)
        for name in ("NOOVA_API_KEY", "NOOVA_BASE_URL", "NOOVA_CONFIG_DIR",
                     noova_key.ALLOW_LOCAL_ENV):
            os.environ.pop(name, None)
        self.config = Path(self._tmp.name) / "config.json"
        self._patch = mock.patch.object(
            noova_key, "_config_candidates", lambda: [self.config])
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)


class ParseCredentialsTest(unittest.TestCase):
    """核心：用户粘贴什么都要能认出来（这是「自动配置」的地基）。"""

    def test_bare_key(self):
        self.assertEqual(noova_key.parse_credentials("sk-abc123456")["api_key"], "sk-abc123456")

    def test_key_with_quotes_and_chinese_period(self):
        result = noova_key.parse_credentials('"sk-abc123456"。')
        self.assertEqual(result["api_key"], "sk-abc123456")

    def test_key_from_assignment(self):
        for blob in ("api_key=sk-abc123456", "api_key: sk-abc123456",
                     "NOOVA_API_KEY=sk-abc123456", "密钥：sk-abc123456"):
            self.assertEqual(noova_key.parse_credentials(blob)["api_key"], "sk-abc123456", blob)

    def test_key_from_bearer(self):
        blob = "Authorization: Bearer sk-abc123456"
        self.assertEqual(noova_key.parse_credentials(blob)["api_key"], "sk-abc123456")

    def test_key_from_command_line_paste(self):
        blob = "python3 scripts/noova_key.py setup --api-key sk-abc123456"
        self.assertEqual(noova_key.parse_credentials(blob)["api_key"], "sk-abc123456")

    def test_json_blob(self):
        blob = '{"api_key": "sk-abc123456", "base_url": "https://api.example.org"}'
        result = noova_key.parse_credentials(blob)
        self.assertEqual(result["api_key"], "sk-abc123456")
        self.assertEqual(result["base_url"], "https://api.example.org")

    def test_url_only_reduces_to_origin(self):
        result = noova_key.parse_credentials("https://noova.vip/api_control")
        self.assertEqual(result["base_url"], "https://noova.vip")
        self.assertEqual(result["api_key"], "")

    def test_backup_domain_url_is_trusted(self):
        """`noova.live` 是官方线上域名之一，与主域名同等可信。"""
        result = noova_key.parse_credentials("https://noova.live/api_control")
        self.assertEqual(result["base_url"], "https://noova.live")
        self.assertEqual(result["ignored_urls"], [])

    def test_www_variants_normalize_to_official_apex(self):
        """`www.` 写法归一到 apex，不能因写法不同被当成「非官方域名」忽略。"""
        for blob, expected in (
            ("https://www.noova.vip/api_control", "https://noova.vip"),
            ("https://www.noova.live/api_control", "https://noova.live"),
        ):
            result = noova_key.parse_credentials(blob)
            self.assertEqual(result["base_url"], expected, blob)
            self.assertEqual(result["ignored_urls"], [], blob)

    def test_key_and_url_mixed_text(self):
        blob = "这是我的密钥 sk-abc123456 ，官网是 https://noova.vip/api_control ，谢谢"
        result = noova_key.parse_credentials(blob)
        self.assertEqual(result["api_key"], "sk-abc123456")
        self.assertEqual(result["base_url"], "https://noova.vip")

    def test_untrusted_urls_are_ignored_with_warning(self):
        """BLOCKER B1：粘贴文本里的非官方地址一律忽略。

        旧行为是「取第一个 URL 当地址」，后果是校验请求带着用户的 Key 发往那个域名，
        且校验通过后该域名被持久化——用户从网页复制 Key 时连带复制一行文档链接就会命中。
        """
        blob = "https://a.example.com 和 https://b.example.com"
        result = noova_key.parse_credentials(blob)
        self.assertEqual(result["base_url"], "")
        self.assertEqual(sorted(result["ignored_urls"]),
                         ["https://a.example.com", "https://b.example.com"])
        self.assertTrue(any("已忽略" in w for w in result["warnings"]))

    def test_official_url_in_text_is_accepted(self):
        blob = "sk-abc123456 官网 https://noova.vip/api_control 谢谢"
        result = noova_key.parse_credentials(blob)
        self.assertEqual(result["base_url"], "https://noova.vip")
        self.assertEqual(result["ignored_urls"], [])

    def test_bare_hostname_fallback(self):
        self.assertEqual(noova_key.parse_credentials("noova.vip")["base_url"], "https://noova.vip")

    def test_empty_input_reports_warning(self):
        result = noova_key.parse_credentials("   ")
        self.assertEqual(result["api_key"], "")
        self.assertTrue(result["warnings"])

    def test_unrecognized_text_yields_nothing(self):
        result = noova_key.parse_credentials("今天天气不错")
        self.assertEqual((result["api_key"], result["base_url"]), ("", ""))

    def test_does_not_leak_key_into_warnings(self):
        result = noova_key.parse_credentials("sk-abc123456 顺便说一句")
        self.assertFalse(any("sk-abc123456" in w for w in result["warnings"]))


class ConfigStoreTest(IsolatedConfigCase):
    def test_save_and_read_key(self):
        self.assertEqual(noova_key.get_api_key(), "")
        code, out, _ = run("setup", "sk-abcdefg123", "--no-verify")
        self.assertEqual(code, 0)
        self.assertIn("已保存", out)
        self.assertEqual(noova_key.get_api_key(), "sk-abcdefg123")
        self.assertTrue(self.config.is_file())

    def test_full_key_never_printed(self):
        _, out, err = run("setup", "sk-abcdefg123", "--no-verify")
        self.assertNotIn("sk-abcdefg123", out + err)
        self.assertIn("sk-abcd", out)

    def test_env_var_wins_over_file(self):
        run("setup", "sk-file-key1", "--no-verify")
        os.environ["NOOVA_API_KEY"] = "sk-env-key1"
        try:
            self.assertEqual(noova_key.get_api_key(), "sk-env-key1")
            self.assertEqual(noova_key.get_api_key_source(), "env")
        finally:
            os.environ.pop("NOOVA_API_KEY", None)
        self.assertEqual(noova_key.get_api_key_source(), "file")

    def test_base_url_default_and_override(self):
        self.assertEqual(noova_key.get_base_url(), noova_key.DEFAULT_BASE_URL)
        # 固定 DNS 判定：接管解析器（如企业 DNS 把未知域名指到内网）的环境下，
        # 自定义域名会被守卫当成内网地址拒掉，而本用例测的是「落盘/读回」这件事。
        with mock.patch.object(nc, "resolves_to_local", return_value=False):
            run("base", "https://api.example.org/some/path")
        self.assertEqual(noova_key.get_base_url(), "https://api.example.org")

    def test_clear_removes_key_but_keeps_base(self):
        with mock.patch.object(nc, "resolves_to_local", return_value=False):
            run("setup", "sk-abcdefg123", "--base-url", "https://api.example.org", "--no-verify")
        code, out, _ = run("clear")
        self.assertEqual(code, 0)
        self.assertIn("已清除", out)
        self.assertEqual(noova_key.get_api_key(), "")
        self.assertEqual(noova_key.get_base_url(), "https://api.example.org")


class SetupFlowTest(IsolatedConfigCase):
    """`setup` 是用户粘贴 Key 后的自动配置入口。"""

    def test_verified_key_is_written(self):
        with mock.patch.object(noova_key, "verify_key",
                               return_value={"ok": True, "status": 200, "reason": "",
                                             "credit": 120}):
            code, out, _ = run("setup", "sk-abcdefg123")
        self.assertEqual(code, 0)
        self.assertIn("已校验通过", out)
        self.assertIn("剩余积分：120", out)
        self.assertEqual(noova_key.get_api_key(), "sk-abcdefg123")
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["client_version"],
                         noova_key.VERSION)

    def test_rejected_key_is_not_written(self):
        with mock.patch.object(noova_key, "verify_key",
                               return_value={"ok": False, "status": 401,
                                             "reason": "API Key 无效或已停用", "credit": None}):
            code, _, err = run("setup", "sk-badkey123")
        self.assertEqual(code, 1)
        self.assertIn("未写入配置", err)
        self.assertEqual(noova_key.get_api_key(), "")

    def test_force_writes_even_when_verification_fails(self):
        with mock.patch.object(noova_key, "verify_key",
                               return_value={"ok": False, "status": 401,
                                             "reason": "API Key 无效或已停用", "credit": None}):
            code, _, _ = run("setup", "sk-badkey123", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(noova_key.get_api_key(), "sk-badkey123")

    def test_offline_verification_still_saves_with_note(self):
        with mock.patch.object(noova_key, "verify_key",
                               return_value={"ok": None, "status": None,
                                             "reason": "网络不可达：URLError", "credit": None}):
            code, out, _ = run("setup", "sk-abcdefg123")
        self.assertEqual(code, 0)
        self.assertIn("未能判定", out)
        self.assertEqual(noova_key.get_api_key(), "sk-abcdefg123")

    def test_url_only_saves_base_and_asks_for_key(self):
        code, out, _ = run("setup", "https://noova.vip/api_control")
        self.assertEqual(code, 1)
        self.assertIn("还缺 API Key", out)
        self.assertEqual(noova_key.get_base_url(), "https://noova.vip")
        self.assertEqual(noova_key.get_api_key(), "")

    def test_no_input_prints_guide_and_fails(self):
        code, _, err = run("setup")
        self.assertEqual(code, 2)
        self.assertIn("控制台", err)
        self.assertIn("noova.vip/api_control", err)
        self.assertIn("setup --api-key", err)

    def test_unrecognized_input_fails_with_guide(self):
        code, _, err = run("setup", "你好啊")
        self.assertEqual(code, 2)
        self.assertIn("未能从输入中识别", err)
        self.assertIn("尚未配置 API Key", err)

    def test_json_output_is_machine_readable(self):
        with mock.patch.object(noova_key, "verify_key",
                               return_value={"ok": True, "status": 200, "reason": "",
                                             "credit": 2048}):
            code, out, _ = run("setup", "sk-abcdefg123", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["credit"], 2048)
        self.assertNotIn("sk-abcdefg123", out)

    def test_save_alias_writes_without_network(self):
        with mock.patch.object(noova_key, "verify_key",
                               side_effect=AssertionError("不应联网校验")):
            code, _, _ = run("save", "--api-key", "sk-abcdefg123")
        self.assertEqual(code, 0)
        self.assertEqual(noova_key.get_api_key(), "sk-abcdefg123")


class StatusAndDoctorTest(IsolatedConfigCase):
    def test_status_unconfigured_json(self):
        code, out, _ = run("status", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["base_url"], noova_key.DEFAULT_BASE_URL)
        self.assertTrue(payload["console_url"].endswith("/api_control"))

    def test_status_configured_exit_zero(self):
        run("setup", "sk-abcdefg123", "--no-verify")
        code, out, _ = run("status", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["configured"])

    def test_guide_json_contains_direct_address(self):
        code, out, _ = run("guide", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["base_url"], noova_key.DEFAULT_BASE_URL)
        self.assertEqual(len(payload["steps"]), 2)

    def test_doctor_offline_reports_missing_key(self):
        code, out, _ = run("doctor", "--offline", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ready"])
        checks = {c["id"]: c for c in payload["checks"]}
        self.assertFalse(checks["api_key"]["ok"])
        self.assertTrue(checks["python"]["ok"])
        self.assertTrue(checks["scripts"]["ok"])
        self.assertIsNone(checks["api_key_valid"]["ok"])

    def test_doctor_ready_when_key_valid(self):
        run("setup", "sk-abcdefg123", "--no-verify")
        with mock.patch.object(noova_key, "public_models",
                               return_value={"ok": True, "status": 200, "reason": "",
                                             "models": {"text": 3, "total": 3}}), \
             mock.patch.object(noova_key, "verify_key",
                               return_value={"ok": True, "status": 200, "reason": "",
                                             "credit": 99}):
            code, out, _ = run("doctor", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["base_url"], noova_key.DEFAULT_BASE_URL)
        checks = {c["id"]: c for c in payload["checks"]}
        self.assertIn("99", checks["api_key_valid"]["detail"])

    def test_doctor_reachability_true_when_probe_needs_key(self):
        """实测 `/api/models` 需携带 Key（无 Key → 401）；401 ≠ 地址不可达。

        修复前：可达性探测把 401 当回事，于是同一份配置下地址报「不可达」、
        Key 却又「校验通过」——自相矛盾，会把用户引向错误的排查方向。
        """
        run("setup", "sk-abcdefg123", "--no-verify")
        with mock.patch.object(noova_key, "public_models",
                               return_value={"ok": False, "status": 401,
                                             "reason": "缺少 API Key",
                                             "models": {"text": 0, "total": 0}}), \
             mock.patch.object(noova_key, "verify_key",
                               return_value={"ok": True, "status": 200, "reason": "",
                                             "credit": 99}):
            code, out, _ = run("doctor", "--json")
        payload = json.loads(out)
        checks = {c["id"]: c for c in payload["checks"]}
        self.assertTrue(checks["reachable"]["ok"])
        self.assertIn("401", checks["reachable"]["detail"])

    def test_verify_requires_key(self):
        code, _, err = run("verify")
        self.assertEqual(code, 1)
        self.assertIn("尚未配置", err + "")


class ProbeTest(unittest.TestCase):
    """鉴权失败原因必须来自服务端提示，且不得泄漏 HTML/链接。"""

    @staticmethod
    def _http_error(code: int, body: str):
        import urllib.error
        return urllib.error.HTTPError("https://noova.vip/x", code, "err", {},
                                      io.BytesIO(body.encode("utf-8")))

    def test_401_reports_server_message(self):
        body = '{"code": 401, "data": null, "message": "API Key 无效或已停用"}'
        with mock.patch.object(noova_key, "open_credentialed",
                               side_effect=self._http_error(401, body)):
            result = noova_key._probe("https://noova.vip/api/v1/credit", key="sk-x")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "API Key 无效或已停用")

    def test_html_error_body_is_not_surfaced(self):
        with mock.patch.object(noova_key, "open_credentialed",
                               side_effect=self._http_error(401, "<html><body>blocked</body></html>")):
            result = noova_key._probe("https://noova.vip/api/v1/credit", key="sk-x")
        self.assertFalse(result["ok"])
        self.assertNotIn("html", result["reason"].lower())

    def test_403_is_unknown_not_invalid(self):
        """403 语义不唯一（可能是网络策略拦截），不能据此判定 Key 无效。"""
        with mock.patch.object(noova_key, "open_credentialed",
                               side_effect=self._http_error(403, '{"message":"Forbidden"}')):
            result = noova_key._probe("https://noova.vip/api/v1/credit", key="sk-x")
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], 403)

    def test_5xx_is_unknown_not_invalid(self):
        with mock.patch.object(noova_key, "open_credentialed",
                               side_effect=self._http_error(502, '{"message":"bad gateway"}')):
            result = noova_key._probe("https://noova.vip/api/v1/credit", key="sk-x")
        self.assertIsNone(result["ok"])

    def test_network_failure_is_unknown(self):
        import urllib.error
        with mock.patch.object(noova_key, "open_credentialed",
                               side_effect=urllib.error.URLError("no route")):
            result = noova_key._probe("https://noova.vip/api/v1/credit", key="sk-x")
        self.assertIsNone(result["ok"])
        self.assertIn("网络不可达", result["reason"])

    def test_server_message_with_link_is_redacted_not_dropped(self):
        """服务端消息含链接时**脱敏后保留**，不再整条丢弃（曾经的「一泄露一丢信息」）。"""
        body = (r'{"code": 401, "data": null, '
                r'"message": "Key 已停用，详见 https://internal-oss.aliyuncs.com/bucket"}')
        with mock.patch.object(noova_key, "open_credentialed",
                               side_effect=self._http_error(401, body)):
            result = noova_key._probe("https://noova.vip/api/v1/credit", key="sk-x")
        self.assertIn("Key 已停用", result["reason"])
        self.assertNotIn("aliyuncs.com", result["reason"])
        self.assertIn("media.example.com", result["reason"])

    def test_probe_refuses_local_base_without_any_request(self):
        """BLOCKER B2：本机/内网地址时 `_probe` 一个字节都不发。"""
        called = []

        def boom(*a, **k):
            called.append(a)
            raise AssertionError("不得发起请求")

        with mock.patch.object(noova_key, "open_credentialed", side_effect=boom):
            result = noova_key._probe("http://127.0.0.1:9/api/v1/credit", key="sk-x")
        self.assertEqual(called, [])
        self.assertTrue(result["blocked"])
        self.assertIsNone(result["ok"])

    def test_verify_key_reports_blocked_base(self):
        with mock.patch.object(noova_key, "open_credentialed",
                               side_effect=AssertionError("不得发起请求")):
            result = noova_key.verify_key("sk-x", "http://127.0.0.1:9")
        self.assertTrue(result["blocked"])
        self.assertIsNone(result["ok"])
        self.assertIn("本机", result["reason"])


class VerifyEndpointTest(unittest.TestCase):
    """回归：Key 校验必须走真正鉴权的接口。

    实测 2026-09-29：`/api/v1/gateway/models` 与 `/v1/models` 对任何请求都返回 200
    （连无效 Key 也放行），拿它们校验 Key 会把垃圾 Key 判成有效。
    """

    def test_auth_and_public_endpoints_are_distinct(self):
        self.assertNotEqual(noova_key.AUTH_VALIDATE_PATH, noova_key.PUBLIC_PROBE_PATH)
        self.assertNotEqual(noova_key.AUTH_PROBE_PATH, noova_key.PUBLIC_PROBE_PATH)
        self.assertNotIn("models", noova_key.AUTH_VALIDATE_PATH)

    def _call(self, responses: list[dict]) -> tuple[dict, list[dict]]:
        calls: list[dict] = []

        def fake_probe(url, key=None, timeout=20, method="GET", body=None):
            calls.append({"url": url, "key": key, "method": method})
            return responses[min(len(calls) - 1, len(responses) - 1)]

        with mock.patch.object(noova_key, "_probe", fake_probe):
            return noova_key.verify_key("sk-x", "https://noova.vip"), calls

    def test_valid_key_accepted_by_validate_endpoint(self):
        result, calls = self._call([{"ok": True, "status": 200, "reason": "",
                                     "payload": {"data": {"valid": True, "key_prefix": "sk-x"}}}])
        self.assertTrue(result["ok"])
        self.assertTrue(calls[0]["url"].endswith(noova_key.AUTH_VALIDATE_PATH))
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(len(calls), 1)

    def test_invalid_key_rejected_with_server_reason(self):
        result, calls = self._call([{"ok": True, "status": 200, "reason": "",
                                     "payload": {"data": {"valid": False,
                                                          "reason": "API Key 无效或已停用"}}}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "API Key 无效或已停用")
        self.assertEqual(len(calls), 1)

    def test_falls_back_to_credit_when_validate_endpoint_missing(self):
        result, calls = self._call([
            {"ok": None, "status": 404, "reason": "服务返回 HTTP 404，暂时无法判定", "payload": None},
            {"ok": True, "status": 200, "reason": "",
             "payload": {"data": {"remainingTotal": 42}}},
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(result["credit"], 42)
        self.assertTrue(calls[1]["url"].endswith(noova_key.AUTH_PROBE_PATH))

    def test_network_failure_does_not_double_request(self):
        result, calls = self._call([{"ok": None, "status": None, "reason": "网络不可达：URLError",
                                     "payload": None}])
        self.assertIsNone(result["ok"])
        self.assertEqual(len(calls), 1)

    def test_public_models_does_not_send_key(self):
        seen = {}

        def fake_probe(url, key=None, timeout=20, method="GET", body=None):
            seen["url"] = url
            seen["key"] = key
            return {"ok": True, "status": 200, "reason": "",
                    "payload": {"data": [{"model_type": "image"}]}}

        with mock.patch.object(noova_key, "_probe", fake_probe):
            result = noova_key.public_models("https://noova.vip")
        self.assertTrue(seen["url"].endswith(noova_key.PUBLIC_PROBE_PATH))
        self.assertIsNone(seen["key"])
        self.assertEqual(result["models"]["image"], 1)


class ModelSummaryTest(unittest.TestCase):
    def test_counts_by_type(self):
        payload = {"data": [{"model_type": "text"}, {"model_type": "text"},
                            {"model_type": "image"}, {"modelType": "video"}, {"x": 1}]}
        self.assertEqual(noova_key._model_summary(payload),
                         {"text": 2, "image": 1, "video": 1, "unknown": 1, "total": 5})

    def test_handles_bad_payload(self):
        self.assertEqual(noova_key._model_summary("nope"), {})


class ConfigCandidateTest(unittest.TestCase):
    def test_priority_order(self):
        with mock.patch.dict(os.environ, {"NOOVA_CONFIG_DIR": "/tmp/noova-xyz"}):
            candidates = noova_key._config_candidates()
        self.assertEqual(candidates[0], Path("/tmp/noova-xyz") / "config.json")
        self.assertIn(Path.home() / ".noova" / "config.json", candidates)
        # 最后一项是 skill 目录内的兜底
        self.assertEqual(candidates[-1], noova_key.SKILL_DIR / ".noova" / "config.json")

    def test_skill_dir_points_at_package_root(self):
        self.assertTrue((noova_key.SKILL_DIR / "SKILL.md").is_file())
        self.assertTrue((noova_key.SKILL_DIR / "scripts" / "noova_media.py").is_file())


class BaseUrlGuardTest(unittest.TestCase):
    """地址守卫：基础地址必须是「部署在服务器上的域名」。

    本 skill 只对接线上域名。本机 / 内网 / 开发环境地址一旦写进配置，请求就会打到
    一个只有当前这台机器能访问的地址，在别处必然失败，现象却像「服务故障」。
    """

    def _check(self, url: str, dns_verdict: "bool | None" = None) -> dict:
        """跑一次守卫，并把 DNS 判定固定住。

        `resolves_to_local()` 的真实结果取决于运行环境的解析器（企业 DNS / 公共 DNS / 
        离线 CI 各不相同），不固定它会让本类用例在别人的机器上随机变红。
        默认按「域名可解析到公网」处理；要测离线 / 通配 DNS 行为时显式传入。
        """
        with mock.patch.dict(os.environ, {noova_key.ALLOW_LOCAL_ENV: ""}), \
             mock.patch.object(nc, "resolves_to_local", return_value=dns_verdict):
            return noova_key.check_base_url(url)

    def test_production_domain_allowed(self):
        for url in ("https://noova.vip", "https://noova.vip/api_control", "noova.vip"):
            self.assertTrue(self._check(url)["ok"], url)
        # 归约为 origin（丢弃路径），避免把控制台页面地址写进配置
        self.assertEqual(self._check("https://noova.vip/api_control")["url"], "https://noova.vip")
        self.assertEqual(self._check("noova.vip")["url"], "https://noova.vip")

    def test_backup_domain_allowed(self):
        """`noova.live` 与主域名同为官方线上域名，必须同等放行。"""
        for url in ("https://noova.live", "https://noova.live/api_control", "noova.live"):
            self.assertTrue(self._check(url)["ok"], url)
        self.assertEqual(self._check("https://noova.live/api_control")["url"],
                         "https://noova.live")

    def test_www_variants_normalized_to_apex(self):
        """`www.` 变体归一到 apex：避免同一站点两种写法在配置里并存。"""
        for url, expected in (("https://www.noova.vip", "https://noova.vip"),
                              ("https://www.noova.vip/api_control", "https://noova.vip"),
                              ("https://www.noova.live", "https://noova.live"),
                              ("https://www.noova.live/api_control", "https://noova.live")):
            result = self._check(url)
            self.assertTrue(result["ok"], url)
            self.assertEqual(result["url"], expected, url)

    def test_www_variant_keeps_plain_http_rejected(self):
        """归一化不得静默把 `http://www.noova.vip` 升成 https（那会绕过明文拦截）。"""
        result = self._check("http://www.noova.vip")
        self.assertFalse(result["ok"])
        self.assertFalse(result["secure"])

    def test_www_variant_of_dev_host_still_rejected(self):
        """归一化只对官方域名生效，不能把 `dev.` 前缀洗白。"""
        self.assertFalse(self._check("https://www.dev.noova.vip")["ok"])

    def test_loopback_rejected(self):
        for url in ("http://localhost:5001", "https://localhost", "http://127.0.0.1:8000",
                    "http://0.0.0.0:3000", "http://[::1]:8080",
                    "http://host.docker.internal:9000"):
            result = self._check(url)
            self.assertFalse(result["ok"], url)
            self.assertTrue(result["local"], url)
            self.assertIn(noova_key.DEFAULT_BASE_URL, result["reason"], url)

    def test_private_and_link_local_ips_rejected(self):
        for url in ("http://192.168.1.10:8000", "http://10.0.0.5", "http://172.20.3.4",
                    "http://169.254.10.10"):
            self.assertFalse(self._check(url)["ok"], url)

    def test_dev_environment_hosts_rejected(self):
        for url in ("https://dev.noova.vip", "https://staging.noova.vip",
                    "https://test-api.example.com", "https://api.local",
                    "https://api.internal", "https://pre.noova.vip"):
            self.assertFalse(self._check(url)["ok"], url)

    def test_custom_online_domain_allowed(self):
        """守卫只拦「本机/内网/开发环境」，不锁死单一域名：自建线上部署同样可用。"""
        self.assertTrue(self._check("https://ai.mycorp.cn")["ok"])

    def test_http_plain_is_rejected_for_public_hosts(self):
        """公开域名必须 https：Key 走 Authorization 头，明文链路等于泄露凭据。"""
        result = self._check("http://ai.mycorp.cn")
        self.assertFalse(result["ok"])
        self.assertFalse(result["secure"])
        self.assertIn("https", result["reason"])

    def test_insecure_escape_hatch_is_case_insensitive(self):
        with mock.patch.dict(os.environ, {"NOOVA_ALLOW_INSECURE_BASE_URL": "TRUE"}), \
             mock.patch.object(nc, "resolves_to_local", return_value=False):
            result = noova_key.check_base_url("http://ai.mycorp.cn")
        self.assertTrue(result["ok"])
        self.assertIn("NOOVA_ALLOW_INSECURE_BASE_URL", result["warning"])

    def test_numeric_ip_shorthand_rejected(self):
        """`2130706433` / `0x7f000001` / `0177.0.0.1` 在部分解析器里就是 127.0.0.1。"""
        for url in ("http://2130706433", "http://0x7f000001", "http://0177.0.0.1"):
            self.assertFalse(self._check(url)["ok"], url)

    def test_trailing_dot_fqdn_is_normalized(self):
        """`localhost.` 与 `localhost` 解析到同一地址，必须一起拦下。"""
        for url in ("http://localhost.", "http://localhost.:5001"):
            result = self._check(url)
            self.assertFalse(result["ok"], url)
            self.assertTrue(result["local"], url)

    def test_userinfo_disguise_rejected(self):
        """`https://noova.vip@evil.example.com` 的真实 host 是 evil.example.com。"""
        result = self._check("https://noova.vip@evil.example.com")
        self.assertFalse(result["ok"])
        self.assertIn("用户名", result["reason"])

    def test_wildcard_dns_pointing_at_loopback_rejected(self):
        """`127.0.0.1.nip.io` 字面量完全正常，只有解析后才知道它指向回环地址。

        解析结果**注入**而不依赖真实 DNS：`nip.io` 能否解析取决于运行环境的解析器，
        靠真实 DNS 会让用例在离线 / 受限网络的 CI 上随机变红。
        """
        self.assertFalse(self._check("https://127.0.0.1.nip.io:8080", dns_verdict=True)["ok"])

    def test_unresolved_host_is_allowed_with_a_warning(self):
        """离线 / DNS 不可用时按格式放行，但必须给出提示（不静默通过）。"""
        result = self._check("https://ai.mycorp.cn", dns_verdict=None)
        self.assertTrue(result["ok"])
        self.assertIn("未能解析", result["warning"])
    def test_unparsable_input_rejected(self):
        for url in ("", "   ", "not a url"):
            self.assertFalse(self._check(url)["ok"], repr(url))

    def test_explicit_escape_hatch(self):
        with mock.patch.dict(os.environ, {noova_key.ALLOW_LOCAL_ENV: "1"}):
            result = noova_key.check_base_url("http://localhost:5001")
        self.assertTrue(result["ok"])
        self.assertTrue(result["local"])
        self.assertIn(noova_key.ALLOW_LOCAL_ENV, result["warning"])

    def test_escape_hatch_is_case_insensitive(self):
        """`=TRUE` 曾经静默不生效——安全开关「看起来开了其实没开」比没有更危险。"""
        with mock.patch.dict(os.environ, {noova_key.ALLOW_LOCAL_ENV: "TRUE"}):
            self.assertTrue(noova_key.check_base_url("http://localhost:5001")["ok"])


class SetupBaseUrlGuardTest(IsolatedConfigCase):
    """CLI 级：本机/内网地址不得落盘（自动配置里最危险的一条路径）。"""

    def test_local_url_in_pasted_text_is_rejected_and_not_written(self):
        code, out, err = run("setup", "sk-abcdefg123 http://localhost:5001")
        self.assertEqual(code, 2)
        self.assertIn("本机", out + err)
        self.assertFalse(self.config.exists(), "被拒的地址不得产生配置文件")

    def test_local_url_via_flag_is_rejected(self):
        code, _, _ = run("setup", "--api-key", "sk-abcdefg123",
                         "--base-url", "http://192.168.1.9:8000")
        self.assertEqual(code, 2)
        self.assertFalse(self.config.exists())

    def test_json_error_is_machine_readable(self):
        code, out, _ = run("setup", "sk-abcdefg123 http://127.0.0.1:5000", "--json")
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["rejected_base_url"], "http://127.0.0.1:5000")
        self.assertEqual(payload["base_url"], noova_key.DEFAULT_BASE_URL)
        self.assertNotIn("sk-abcdefg123", out)

    def test_base_command_rejects_local_url(self):
        code, _, err = run("base", "http://localhost:5173")
        self.assertEqual(code, 2)
        self.assertIn("拒绝", err)
        self.assertFalse(self.config.exists())

    def test_stale_local_base_is_replaced_by_online_domain(self):
        """历史配置里存过本机地址时，只补 Key 也应自动改用线上部署域名。"""
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({"base_url": "http://localhost:5001"}),
                               encoding="utf-8")
        with mock.patch.object(noova_key, "verify_key",
                               return_value={"ok": True, "status": 200, "reason": "", "credit": 1}):
            code, out, _ = run("setup", "sk-abcdefg123")
        self.assertEqual(code, 0)
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["base_url"], noova_key.DEFAULT_BASE_URL)
        self.assertIn(noova_key.DEFAULT_BASE_URL, out)

    def test_doctor_flags_non_online_base(self):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({"base_url": "http://localhost:5001"}),
                               encoding="utf-8")
        code, out, _ = run("doctor", "--offline", "--json")
        payload = json.loads(out)
        checks = {c["id"]: c for c in payload["checks"]}
        self.assertIn("base_url_online", checks)
        self.assertFalse(checks["base_url_online"]["ok"])
        self.assertEqual(code, 1)


class ClearTest(IsolatedConfigCase):
    """`clear` 只负责清除已保存的 Key，不应凭空产生配置文件。"""

    def test_clear_on_clean_machine_creates_nothing(self):
        self.assertFalse(self.config.exists())
        code, out, _ = run("clear", "--json")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["cleared"])
        self.assertFalse(self.config.exists(), "本机无配置时 clear 不应创建空配置文件")

    def test_clear_removes_key_but_keeps_base_url(self):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({"api_key": "sk-abcdefg123",
                                           "base_url": "https://noova.vip"}),
                               encoding="utf-8")
        code, out, _ = run("clear", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["cleared"])
        remaining = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", remaining)
        self.assertEqual(remaining["base_url"], "https://noova.vip")


class DoctorCheckCountTest(IsolatedConfigCase):
    def test_doctor_reports_nine_checks(self):
        """自检项数变化时，文档与实现必须一起改（历史上曾写死「五项」「七项」）。"""
        code, out, _ = run("doctor", "--offline", "--json")
        payload = json.loads(out)
        self.assertEqual([c["id"] for c in payload["checks"]],
                         ["python", "python_cmd", "scripts", "config_dir", "config_parse",
                          "api_key", "base_url_online", "reachable", "api_key_valid"])
        self.assertEqual(code, 1)  # 临时目录里没有 Key

    def test_corrupt_config_is_reported_not_silently_empty(self):
        """损坏的配置文件必须如实报出来，而不是让用户看到「尚未配置」再白粘贴一遍。"""
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text('{"api_key": "sk-broken",', encoding="utf-8")
        code, out, _ = run("doctor", "--offline", "--json")
        payload = json.loads(out)
        checks = {c["id"]: c for c in payload["checks"]}
        self.assertFalse(checks["config_parse"]["ok"])
        self.assertIn("未生效", checks["config_parse"]["detail"])
        self.assertEqual(code, 1)


class PythonCommandResolutionTest(unittest.TestCase):
    """命令必须绑定当前实际运行的解释器，而不是猜 python/python3/py 的 PATH 名。"""

    def test_python_cmd_uses_the_current_interpreter_absolute_path(self):
        script = Path(noova_key.SCRIPT_DIR / "noova_media.py")
        self.assertEqual(noova_key.PYTHON_CMD, Path(sys.executable).resolve().as_posix())
        self.assertEqual(noova_key.python_cmd(script),
                         f'"{Path(sys.executable).resolve().as_posix()}" "{script.resolve().as_posix()}"')

    def test_command_is_independent_of_the_shell_path_name(self):
        """同一个解释器无论由 python、python3 或 py -3 启动，提示路径都一致。"""
        rendered = noova_key.python_cmd(noova_key.SELF_PATH)
        self.assertIn(Path(sys.executable).resolve().as_posix(), rendered)
        self.assertNotRegex(rendered, r'(^|\\s)(python3?|py)(\\s|$)')

    def test_user_visible_command_hints_use_the_current_interpreter(self):
        guide = noova_key.render_guide(configured=False)
        self.assertIn(noova_key.python_cmd(noova_key.SELF_PATH) + " setup", guide)
        self.assertNotIn("python3 ", guide)

    def test_source_has_no_hardcoded_python3_in_message_strings(self):
        """提示文案不得再写死 python3（shebang、文档和说明注释除外）。"""
        for name in ("noova_key.py", "noova_media.py", "noova_upload.py"):
            source = (noova_key.SCRIPT_DIR / name).read_text(encoding="utf-8")
            offenders = [line.strip() for line in source.splitlines()
                         if re.search(r"[\"'f][^\"']*python3 ", line)
                         and not line.lstrip().startswith("#")]
            self.assertEqual(offenders, [], f"{name} 里仍有写死的 python3：{offenders}")

    def test_skill_md_declares_the_same_version_as_the_script(self):
        skill_md = (noova_key.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r'^\s*version:\s*"([^"]+)"', skill_md, re.M)
        self.assertIsNotNone(match, "SKILL.md frontmatter 缺少 metadata.version")
        self.assertEqual(match.group(1), noova_key.VERSION)


class PathIsDerivedNotHardcodedTest(unittest.TestCase):
    """所有对外展示的路径都必须**运行时解析**，不得写死。

    用户明确要求：不同用户的环境不同（家目录、安装位置、是否用 NOOVA_CONFIG_DIR
    覆盖都不一样），路径写死就等于给错误答案。这里逐条验证「换个环境结果就变」。
    """

    ENV_KEYS = ("NOOVA_API_KEY", "NOOVA_BASE_URL", "NOOVA_CONFIG_DIR",
                "HOME", "USERPROFILE", noova_key.ALLOW_LOCAL_ENV)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_env = dict(os.environ)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)

    @contextlib.contextmanager
    def _env(self, **values):
        """临时替换环境变量（值为 None 表示删除）。"""
        saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        try:
            for key, value in values.items():
                if value is not None:
                    os.environ[key] = value
            yield
        finally:
            for key in self.ENV_KEYS:
                os.environ.pop(key, None)
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def _home(self, name: str) -> Path:
        home = Path(self._tmp.name) / name
        home.mkdir(parents=True, exist_ok=True)
        return home

    def test_config_path_follows_the_users_home(self):
        """换家目录 → 配置文件路径跟着变（两个不同的「用户」得到两个结果）。"""
        seen = []
        for who in ("alice", "bob-with-a-much-longer-name"):
            home = self._home(who)
            with self._env(USERPROFILE=str(home), HOME=str(home)):
                seen.append(noova_key.config_file(create=False))
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(seen[0], self._home("alice") / ".noova" / "config.json")
        self.assertEqual(seen[1].parent.parent, self._home("bob-with-a-much-longer-name"))

    def test_env_override_is_honoured_on_both_read_and_write(self):
        """`NOOVA_CONFIG_DIR` 是显式指令：即使家目录已有旧配置，也不能偷偷改道。"""
        home = self._home("alice")
        legacy = home / ".noova" / "config.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('{"api_key": "sk-legacy"}', encoding="utf-8")

        target_dir = Path(self._tmp.name) / "custom-dir"
        with self._env(NOOVA_CONFIG_DIR=str(target_dir), HOME=str(home),
                       USERPROFILE=str(home)):
            self.assertEqual(noova_key.config_file(create=False),
                             target_dir / "config.json")          # 展示：认覆盖
            written = noova_key._save({"api_key": "sk-new"})       # 落盘：也认覆盖
            self.assertEqual(written, target_dir / "config.json")

        self.assertEqual(json.loads((target_dir / "config.json").read_text(encoding="utf-8")),
                         {"api_key": "sk-new"})
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8"))["api_key"],
                         "sk-legacy")  # 旧文件没被就地改写

    def test_display_resolver_never_touches_the_filesystem(self):
        """展示用解析（create=False）不得创建目录 —— 单纯打屏不能有副作用。"""
        home = self._home("alice")
        with self._env(HOME=str(home), USERPROFILE=str(home)):
            path = noova_key.config_file(create=False)
        self.assertEqual(path, home / ".noova" / "config.json")
        self.assertFalse(path.parent.exists(), "只读解析不应创建 ~/.noova")

    def test_guide_shows_the_resolved_config_path(self):
        """引导里的「凭据保存位置」必须是解析结果，而不是写死的字面量。"""
        home = self._home("alice")
        with self._env(HOME=str(home), USERPROFILE=str(home)):
            guide = noova_key.render_guide(configured=False)
        self.assertIn(str(home / ".noova" / "config.json"), guide)
        self.assertNotIn("默认 ~/.noova/config.json", guide)

    def test_guide_follows_env_override(self):
        target_dir = Path(self._tmp.name) / "custom-dir"
        with self._env(NOOVA_CONFIG_DIR=str(target_dir)):
            guide = noova_key.render_guide(configured=False)
        self.assertIn(str(target_dir / "config.json"), guide)

    def test_guide_command_points_at_this_copy_of_the_script(self):
        """引导里的等价命令必须指向**当前这份脚本**（随安装位置变），不是写死路径。"""
        guide = noova_key.render_guide(configured=False)
        self.assertIn(noova_key.SELF_PATH.as_posix(), guide)
        self.assertIn(noova_key.SCRIPT_DIR.as_posix(), noova_key.render_guide(configured=True))

    def test_no_hardcoded_absolute_path_in_message_strings(self):
        """源码级兜底：提示文案里不得出现写死的盘符/家目录（防止日后手滑写回）。"""
        source = (noova_key.SCRIPT_DIR / "noova_key.py").read_text(encoding="utf-8")
        literals = re.findall(r"[\"']([^\"'\n]*)[\"']", source)
        suspicious = [s for s in literals
                      if re.search(r"(?:^|[\s\"'=])(?:[A-Za-z]:[\\/]|/Users/|/home/)", s)]
        self.assertEqual(suspicious, [], f"疑似写死的绝对路径：{suspicious}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
