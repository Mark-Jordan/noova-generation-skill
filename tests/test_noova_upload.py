#!/usr/bin/env python3
"""noova-generation skill 上传模块的单元测试（仅标准库，不联网、不产生真实上传）。

运行：
    cd tests && python -m unittest discover -s . -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "noova-generation"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import noova_upload as nu  # noqa: E402


def _fake_file(tmp: Path, name: str = "a.png", payload: bytes = b"png-bytes") -> Path:
    path = tmp / name
    path.write_bytes(payload)
    return path


class SafeFilenameTest(unittest.TestCase):
    def test_pure_chinese_name_falls_back_to_file(self):
        # 非 ASCII 段被折叠为 _ 后首尾清理为空 → 回退 file，避免产出“-.png”这类畸形名
        self.assertEqual(nu.safe_filename("测试图片-中文名.png"), "file.png")

    def test_keeps_ascii_part_around_chinese(self):
        self.assertEqual(nu.safe_filename("图片product-01.png"), "product-01.png")

    def test_keeps_extension_and_simple_name(self):
        self.assertEqual(nu.safe_filename("photo-01.JPG"), "photo-01.JPG")

    def test_empty_or_weird(self):
        self.assertEqual(nu.safe_filename(""), "file")
        self.assertEqual(nu.safe_filename("..."), "file")
        self.assertEqual(nu.safe_filename("路径/子目录/x.webp"), "x.webp")


class MultipartTest(unittest.TestCase):
    def test_contains_fields(self):
        content_type, body = nu.encode_multipart("file", "a.png", b"data", "image/png")
        self.assertIn("multipart/form-data", content_type)
        self.assertIn(b'name="file"', body)
        self.assertIn(b'filename="a.png"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(b"data", body)

    def test_boundary_unique_per_call(self):
        first = nu.encode_multipart("file", "a.png", b"x", "image/png")[0]
        second = nu.encode_multipart("file", "a.png", b"x", "image/png")[0]
        self.assertNotEqual(first, second)


class AbsolutizeTest(unittest.TestCase):
    """相对路径补全与「绝对 URL 只放行公网」守卫。"""

    def test_relative_path_is_joined(self):
        self.assertEqual(nu.absolutize("https://up.example.com", "/api/file/x.png"),
                         "https://up.example.com/api/file/x.png")
        self.assertEqual(nu.absolutize("https://up.example.com/", "api/file/x.png"),
                         "https://up.example.com/api/file/x.png")

    def test_absolute_public_url_kept(self):
        url = "https://cdn.example.org/api/file/x.png"
        with mock.patch.object(nu, "resolves_to_local", return_value=False):
            self.assertEqual(nu.absolutize("https://up.example.com", url), url)

    def test_protocol_relative(self):
        self.assertEqual(nu.absolutize("https://up.example.com", "//cdn/x.png"),
                         "https://cdn/x.png")

    def test_empty(self):
        self.assertEqual(nu.absolutize("https://up.example.com", ""), "")

    def test_absolute_url_pointing_at_loopback_is_rejected(self):
        """返回的地址不得让用户本机去 GET（`--verify` 会真的发请求）。"""
        for evil in ("http://127.0.0.1:9/evil.png", "https://localhost/x.png",
                     "http://169.254.169.254/latest/meta-data/"):
            with self.assertRaises(nu.UploadError, msg=evil):
                nu.absolutize("https://up.example.com", evil)


class PlatformParseTest(unittest.TestCase):
    """平台上传响应的地址提取。"""

    def test_file_url_field(self):
        payload = {"file_url": "https://noova.vip/media/a.png"}
        with mock.patch.object(nu, "resolves_to_local", return_value=False):
            self.assertEqual(nu.parse_platform_response(payload), "https://noova.vip/media/a.png")

    def test_url_field_fallback(self):
        payload = {"url": "https://noova.vip/media/a.png"}
        with mock.patch.object(nu, "resolves_to_local", return_value=False):
            self.assertEqual(nu.parse_platform_response(payload), "https://noova.vip/media/a.png")

    def test_data_wrapper(self):
        payload = {"data": {"file_url": "https://noova.vip/media/a.png"}}
        with mock.patch.object(nu, "resolves_to_local", return_value=False):
            self.assertEqual(nu.parse_platform_response(payload), "https://noova.vip/media/a.png")

    def test_missing_url_raises_with_reason(self):
        with self.assertRaises(nu.UploadError) as ctx:
            nu.parse_platform_response({"message": "文件类型不支持"})
        self.assertIn("文件类型不支持", ctx.exception.message)

    def test_non_dict_raises(self):
        with self.assertRaises(nu.UploadError):
            nu.parse_platform_response("不是 JSON")


class UploadFlowTest(unittest.TestCase):
    """用注入的 platform_uploader 验证上传流程，不做任何真实网络调用。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.file = _fake_file(self.tmp)
        self.calls: list[str] = []

    def tearDown(self):
        self._tmp.cleanup()

    def _uploader(self, outcome: str | None = "https://noova.vip/media/ok.png"):
        def _inner(path, content_type, timeout):
            self.calls.append("platform")
            if outcome is None:
                raise nu.UploadError("平台通道不可用")
            return outcome
        return _inner

    def test_platform_channel_used(self):
        result = nu.upload_file(self.file, api_key="sk-x",
                                platform_uploader=self._uploader(), log=lambda _: None)
        self.assertEqual(result["provider"], "platform")
        self.assertEqual(result["url"], "https://noova.vip/media/ok.png")
        self.assertEqual(self.calls, ["platform"])

    def test_platform_skipped_without_key(self):
        with self.assertRaises(nu.UploadError) as ctx:
            nu.upload_file(self.file, api_key=None, platform_uploader=self._uploader(),
                           log=lambda _: None)
        self.assertEqual(self.calls, [])
        hosts = {a["host"]: a["detail"] for a in ctx.exception.attempts}
        self.assertIn("未配置 API Key", hosts["platform"])
        self.assertIn("API Key", ctx.exception.hint)

    def test_explicit_platform_without_key_reports_skip(self):
        with self.assertRaises(nu.UploadError) as ctx:
            nu.upload_file(self.file, api_key=None, host="platform",
                           platform_uploader=self._uploader(), log=lambda _: None)
        self.assertEqual(ctx.exception.attempts[0]["host"], "platform")
        self.assertIn("平台存储通道需要有效 API Key", ctx.exception.hint)

    def test_uploader_failure_is_reported(self):
        with self.assertRaises(nu.UploadError) as ctx:
            nu.upload_file(self.file, api_key="sk-x", platform_uploader=self._uploader(None),
                           log=lambda _: None)
        self.assertEqual(ctx.exception.attempts[0]["ok"], False)
        self.assertIn("平台通道不可用", ctx.exception.attempts[0]["detail"])

    def test_unknown_host_rejected(self):
        with self.assertRaises(nu.UploadError) as ctx:
            nu.upload_file(self.file, host="oss", log=lambda _: None)
        self.assertIn("未知的上传通道", ctx.exception.message)

    def test_missing_file_and_empty_file(self):
        with self.assertRaises(nu.UploadError):
            nu.upload_file(self.tmp / "nope.png", log=lambda _: None)
        empty = self.tmp / "empty.png"
        empty.write_bytes(b"")
        with self.assertRaises(nu.UploadError):
            nu.upload_file(empty, log=lambda _: None)

    def test_result_carries_size_and_content_type(self):
        result = nu.upload_file(self.file, api_key="sk-x",
                                platform_uploader=self._uploader(), log=lambda _: None)
        self.assertEqual(result["bytes"], 9)
        self.assertEqual(result["content_type"], "image/png")


class ContentTypeTest(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(nu.guess_content_type("a.PNG"), "image/png")
        self.assertEqual(nu.guess_content_type("a.mp4"), "video/mp4")
        self.assertEqual(nu.guess_content_type("a.mp3"), "audio/mpeg")

    def test_unknown(self):
        self.assertEqual(nu.guess_content_type("a.xyz"), "application/octet-stream")


class HostConstantTest(unittest.TestCase):
    def test_only_platform_channel_is_exposed(self):
        """上传产物只进平台自有存储：不提供任何第三方上传通道。"""
        self.assertEqual(nu.ALL_HOSTS, ("platform",))
        self.assertEqual(nu.DEFAULT_HOST_ORDER, ("platform",))
        self.assertEqual(set(nu.HOST_LABELS), {"platform"})

    def test_no_third_party_upload_hosts_are_referenced(self):
        """回归：模块内不得再出现任何第三方上传通道（域名与通道名）。

        被禁字符串由碎片拼出，避免测试自身成为禁词的落地点。
        """
        source = (SKILL_DIR / "scripts" / "noova_upload.py").read_text(encoding="utf-8").lower()
        forbidden = ("re" + "mit", "image" + "proxy", "zhong" + "zhuan")
        for token in forbidden:
            self.assertNotIn(token, source, "残留第三方通道引用")


class VerifyUrlTest(unittest.TestCase):
    """地址可达性校验：403 不是「通过」，且不得把本机当探测跳板。"""

    def test_local_target_is_skipped(self):
        ok, why = nu.verify_url("http://127.0.0.1:9/x.png")
        self.assertFalse(ok)
        self.assertIn("本机", why)

    def test_non_http_rejected(self):
        ok, why = nu.verify_url("ftp://x/y.png")
        self.assertFalse(ok)
        self.assertIn("http", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
