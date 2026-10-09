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
    def test_body_shape_and_boundary(self):
        content_type, body = nu.encode_multipart("file", "a.png", b"DATA", "image/png")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1]
        text = body.decode("utf-8", "replace")
        self.assertIn(f"--{boundary}\r\n", text)
        self.assertIn('Content-Disposition: form-data; name="file"; filename="a.png"', text)
        self.assertIn("Content-Type: image/png", text)
        self.assertIn("DATA", text)
        self.assertTrue(text.endswith(f"--{boundary}--\r\n"))

    def test_boundary_unique_per_call(self):
        first = nu.encode_multipart("file", "a.png", b"x", "image/png")[0]
        second = nu.encode_multipart("file", "a.png", b"x", "image/png")[0]
        self.assertNotEqual(first, second)


class AbsolutizeTest(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(nu.absolutize("https://img.thirdparty-a.ee", "/api/file/x.png"),
                         "https://img.thirdparty-a.ee/api/file/x.png")
        self.assertEqual(nu.absolutize("https://img.thirdparty-a.ee/", "api/file/x.png"),
                         "https://img.thirdparty-a.ee/api/file/x.png")

    def test_absolute_kept(self):
        url = "https://thirdparty-b.thirdparty-b-example.chat/api/proxy/image/x.png"
        self.assertEqual(nu.absolutize(nu.THIRDPARTY_B_BASE, url), url)

    def test_protocol_relative(self):
        self.assertEqual(nu.absolutize("https://img.thirdparty-a.ee", "//cdn/x.png"),
                         "https://cdn/x.png")

    def test_empty(self):
        self.assertEqual(nu.absolutize("https://img.thirdparty-a.ee", ""), "")


class ThirdpartyAParseTest(unittest.TestCase):
    def test_prefers_direct_url(self):
        payload = {"success": True, "url": "/api/file/a.png", "directUrl": "/api/file/b.png",
                   "previewUrl": "/view/xyz"}
        self.assertEqual(nu.parse_thirdparty-a_response(payload), "https://img.thirdparty-a.ee/api/file/b.png")

    def test_falls_back_to_url(self):
        payload = {"success": True, "url": "/api/file/a.png"}
        self.assertEqual(nu.parse_thirdparty-a_response(payload), "https://img.thirdparty-a.ee/api/file/a.png")

    def test_absolute_direct_url_preserved(self):
        """第三方返回的公网绝对直链原样保留。

        固定 DNS 判定：接管解析器（如企业 DNS 把未知域名指到内网）的环境下，
        `other-cdn.example.org` 会被解析成内网地址而遭拒，本用例测的是解析逻辑本身。
        """
        payload = {"directUrl": "https://other-cdn.example.org/x.png"}
        with mock.patch.object(nu, "resolves_to_local", return_value=False):
            self.assertEqual(nu.parse_thirdparty-a_response(payload), "https://other-cdn.example.org/x.png")

    def test_absolute_url_pointing_at_loopback_is_rejected(self):
        """第三方返回的地址不得让用户本机去 GET（`--verify` 会真的发请求）。"""
        for evil in ("http://127.0.0.1:9/evil.png", "https://localhost/x.png",
                     "http://169.254.169.254/latest/meta-data/"):
            with self.assertRaises(nu.UploadError, msg=evil):
                nu.parse_thirdparty-a_response({"directUrl": evil})

    def test_success_false_raises(self):
        with self.assertRaises(nu.UploadError) as ctx:
            nu.parse_thirdparty-a_response({"success": False, "message": "文件类型不支持"})
        self.assertIn("文件类型不支持", ctx.exception.message)

    def test_missing_url_raises(self):
        with self.assertRaises(nu.UploadError):
            nu.parse_thirdparty-a_response({"success": True})
        with self.assertRaises(nu.UploadError):
            nu.parse_thirdparty-a_response("不是 JSON")


class ThirdpartyBParseTest(unittest.TestCase):
    def test_url_returned(self):
        payload = {"url": "https://thirdparty-b.thirdparty-b-example.chat/api/proxy/image/abc.png",
                   "created": 1786436842981}
        self.assertEqual(nu.parse_thirdparty-b_response(payload),
                         "https://thirdparty-b.thirdparty-b-example.chat/api/proxy/image/abc.png")

    def test_relative_url_absolutized(self):
        self.assertEqual(nu.parse_thirdparty-b_response({"url": "/api/proxy/image/abc.png"}),
                         "https://thirdparty-b.thirdparty-b-example.chat/api/proxy/image/abc.png")

    def test_missing_url_raises(self):
        with self.assertRaises(nu.UploadError):
            nu.parse_thirdparty-b_response({"created": 1})


class FallbackChainTest(unittest.TestCase):
    """用替换过的通道实现验证降级顺序，不做任何真实网络调用。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.file = _fake_file(self.tmp)
        self._orig = (nu.upload_via_thirdparty-a, nu.upload_via_thirdparty-b, nu.upload_via_platform)
        self.calls: list[str] = []

    def tearDown(self):
        (nu.upload_via_thirdparty-a, nu.upload_via_thirdparty-b, nu.upload_via_platform) = self._orig
        self._tmp.cleanup()

    def _stub(self, *, thirdparty-a: str | None = None, thirdparty-b: str | None = None,
              platform: str | None = None):
        def make(name: str, outcome: str | None):
            def _inner(path, **kwargs):
                self.calls.append(name)
                if outcome is None:
                    raise nu.UploadError(f"{name} 不可用")
                return outcome
            return _inner

        nu.upload_via_thirdparty-a = make("thirdparty-a", thirdparty-a)
        nu.upload_via_thirdparty-b = make("thirdparty-b", thirdparty-b)
        # 平台通道通过依赖注入，不走 monkeypatch
        self.platform_outcome = platform

        def _platform_uploader(path, content_type, timeout):
            self.calls.append("platform")
            if platform is None:
                raise nu.UploadError("platform 不可用")
            return platform

        self.platform_uploader = _platform_uploader

    def test_first_channel_wins(self):
        self._stub(thirdparty-a="https://img.thirdparty-a.ee/api/file/ok.png")
        result = nu.upload_file(self.file, api_key="sk-x", platform_uploader=None,
                                log=lambda _: None)
        self.assertEqual(result["provider"], "thirdparty-a")
        self.assertEqual(result["url"], "https://img.thirdparty-a.ee/api/file/ok.png")
        self.assertEqual(self.calls, ["thirdparty-a"])

    def test_degrades_to_second_channel(self):
        self._stub(thirdparty-b="https://thirdparty-b.thirdparty-b-example.chat/api/proxy/image/ok.png")
        result = nu.upload_file(self.file, api_key="sk-x", platform_uploader=None,
                                log=lambda _: None)
        self.assertEqual(result["provider"], "thirdparty-b")
        self.assertEqual(self.calls, ["thirdparty-a", "thirdparty-b"])

    def test_degrades_to_platform_when_key_present(self):
        self._stub(platform="https://noova.vip/v1/media/uploaded.png")
        result = nu.upload_file(self.file, api_key="sk-x",
                                platform_uploader=self.platform_uploader, log=lambda _: None)
        self.assertEqual(result["provider"], "platform")
        self.assertEqual(self.calls, ["thirdparty-a", "thirdparty-b", "platform"])

    def test_platform_skipped_without_key(self):
        self._stub()
        with self.assertRaises(nu.UploadError) as ctx:
            nu.upload_file(self.file, api_key=None, platform_uploader=self.platform_uploader,
                           log=lambda _: None)
        self.assertNotIn("platform", self.calls)
        hosts = {a["host"]: a["detail"] for a in ctx.exception.attempts}
        self.assertIn("未配置 API Key", hosts["platform"])
        self.assertIn("API Key", ctx.exception.hint)

    def test_explicit_host_does_not_fall_back(self):
        self._stub(thirdparty-b="https://thirdparty-b.thirdparty-b-example.chat/api/proxy/image/ok.png")
        with self.assertRaises(nu.UploadError) as ctx:
            nu.upload_file(self.file, api_key="sk-x", host="thirdparty-a", platform_uploader=None,
                           log=lambda _: None)
        self.assertEqual(self.calls, ["thirdparty-a"])
        self.assertEqual([a["host"] for a in ctx.exception.attempts], ["thirdparty-a"])
        self.assertIn("自动降级", ctx.exception.hint)

    def test_explicit_platform_without_key_reports_skip(self):
        self._stub()
        with self.assertRaises(nu.UploadError) as ctx:
            nu.upload_file(self.file, api_key=None, host="platform",
                           platform_uploader=self.platform_uploader, log=lambda _: None)
        self.assertEqual(ctx.exception.attempts[0]["host"], "platform")
        self.assertIn("平台存储通道需要有效 API Key", ctx.exception.hint)

    def test_unknown_host_rejected(self):
        self._stub(thirdparty-a="https://img.thirdparty-a.ee/api/file/ok.png")
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
        self._stub(thirdparty-a="https://img.thirdparty-a.ee/api/file/ok.png")
        result = nu.upload_file(self.file, log=lambda _: None)
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
    def test_default_order_is_third_party_then_platform(self):
        self.assertEqual(nu.DEFAULT_HOST_ORDER, ("thirdparty-a", "thirdparty-b", "platform"))

    def test_public_upload_hosts_declared(self):
        self.assertIn("img.thirdparty-a.ee", nu.PUBLIC_UPLOAD_HOSTS)
        self.assertIn("thirdparty-b.thirdparty-b-example.chat", nu.PUBLIC_UPLOAD_HOSTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
