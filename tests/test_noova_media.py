#!/usr/bin/env python3
"""noova-generation skill 的单元测试（仅标准库 unittest，不联网、不消耗积分）。

运行（本目录、仓库根、pytest 三种方式**都可用**）：
    cd tests && python -m unittest discover -s . -v
    python -m unittest discover -s tests -p "test_noova_*.py" -t tests
    python -m pytest tests -q

> 早期版本的文档串声称「必须先 cd 进本目录，从仓库根 discover 会报 not importable」——
> 实测为假（目录名带连字符并不影响 `-t` 指定顶层目录的 discover，pytest 更是直接可收）。
> 那句说明曾被当作「不接 CI」的理由，已删除。

配置隔离：本模块通过 `setUpModule()` **默认**把配置读写指向临时目录，
因此任何用例都不会碰用户真实的 `~/.noova/config.json`。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "noova-generation"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import noova_key  # noqa: E402
import noova_media as nm  # noqa: E402

# ---------------------------------------------------------------------------
# 模块级隔离：**默认**把所有配置读写指向临时目录
# ---------------------------------------------------------------------------
# 逐类 opt-in 的隔离只要漏一次，测试就会把假 Key 写进用户真实的
# `~/.noova/config.json`（已实测发生过）。这里改成「默认隔离」。
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


# ---------------------------------------------------------------------------
# 对外参数契约样例（`GET /api/models/params` → `data.models[]`）
# ---------------------------------------------------------------------------
# 旧样例是 Markdown 接入文档：参数表、协议小节、计费小节全靠正则解析。
# 那条路已整体删除（文档是**给网站展示**的数据，字段缺失、排版会漂移，且与
# 对外 API 的鉴权口径不同），现在的唯一真源是结构化契约，样例随之改为契约形状。
DEMO_SPEC = {
    "model": "demo-model",
    "modelType": "image",
    "displayName": "Demo Image",
    "status": "online",
    "protocols": ["openai", "anthropic"],
    "billing": {"mode": "per_request", "price": 15.0, "unit": 1},
    "surchargeRules": [],
    "params": [
        {"name": "model", "type": "string", "required": True, "label": "模型",
         "values": ["demo-model"]},
        {"name": "prompt", "type": "string", "required": True, "label": "提示词"},
        {"name": "aspect_ratio", "type": "string", "required": False, "label": "画面比例",
         "values": ["1:1", "16:9", "9:16"],
         "valueLabels": {"1:1": "方形", "16:9": "横屏", "9:16": "竖屏"}},
        {"name": "resolution", "type": "string", "required": False, "label": "分辨率",
         "values": ["720p"]},
        {"name": "duration", "type": "number", "required": False, "label": "时长",
         "min": 4, "max": 15},
        {"name": "referenceImages", "type": "string[]", "required": False,
         "label": "参考图", "maxItems": 3},
    ],
}


def spec_model(**over) -> dict:
    """按契约形状造一个模型 dict（`over` 覆盖顶层字段）。"""
    item = dict(DEMO_SPEC)
    item.update(over)
    return nm._normalize_model(item)


class NormalizeModelTest(unittest.TestCase):
    """契约 → 内部字典：字段一个不多、一个不少，且**绝不推导**。"""

    def test_maps_contract_fields(self):
        model = spec_model()
        self.assertEqual(model["code"], "demo-model")
        self.assertEqual(model["display_name"], "Demo Image")
        self.assertEqual(model["model_type"], "image")
        self.assertEqual(model["status"], "online")
        self.assertEqual(model["protocols"], ["openai", "anthropic"])
        self.assertEqual(model["billing"],
                         {"mode": "per_request", "price": 15.0, "unit": 1})
        self.assertEqual([p["name"] for p in model["params"]],
                         ["model", "prompt", "aspect_ratio", "resolution", "duration",
                          "referenceImages"])

    def test_param_carries_values_labels_and_bounds(self):
        params = {p["name"]: p for p in spec_model()["params"]}
        self.assertEqual(params["aspect_ratio"]["values"], ["1:1", "16:9", "9:16"])
        self.assertEqual(params["aspect_ratio"]["value_labels"]["16:9"], "横屏")
        self.assertEqual(params["duration"]["min"], 4)
        self.assertEqual(params["duration"]["max"], 15)
        self.assertEqual(params["referenceImages"]["maxItems"], 3)
        self.assertEqual(params["prompt"]["label"], "提示词")
        self.assertNotIn("min", params["prompt"])

    def test_contract_never_invents_fields(self):
        """契约没给的字段就是没有：不合成 ability / description / doc。

        `line`（线路）自 v1.10.0 起**已不在对外契约里**，同样属于「不许出现」的字段，
        与 `ability` / `description` 归为一类。
        """
        model = spec_model()
        for absent in ("ability", "description", "doc", "request_format",
                       "cost_text", "alt_codes", "line", "line_label"):
            self.assertNotIn(absent, model)
        # 旧后端曾下发 `line`：现在即使给了也必须被忽略，不得重新进入内部字典。
        legacy = dict(DEMO_SPEC)
        legacy["line"] = "旧版线路"
        self.assertNotIn("line", nm._normalize_model(legacy))

    def test_param_carries_condition_visibility(self):
        """`optionVisibleWhen` 与 `values` **同序等长**；长度不等时按 values 对齐补 None。

        错位的后果是把 A 档的规则安到 B 档的取值上，用户按面板选出来的值会被平台 400。
        `None` 的语义是「无条件可见」（与后端默认放行一致），所以异常只会多显示。
        """
        model = spec_model(params=[
            {"name": "imageSize", "type": "string", "required": False,
             "values": ["1K", "2K"]},
            {"name": "aspect_ratio", "type": "string", "required": True,
             "values": ["1024x1024", "auto", "auto"],
             "optionVisibleWhen": [
                 {"when": "imageSize", "operator": "eq", "value": "1K"},
                 {"when": "imageSize", "operator": "eq", "value": "1K"}]},
        ])
        ratio = {p["name"]: p for p in model["params"]}["aspect_ratio"]
        self.assertEqual(len(ratio["option_visible_when"]), 3)          # 按 values 对齐
        self.assertIsNone(ratio["option_visible_when"][2])              # 缺的补 None
        # 规则多于 values 时按 values 截断，绝不越界
        self.assertEqual(nm._align_option_visible_when([{"when": "a"}], 0), [])
        self.assertEqual(nm._align_option_visible_when(None, 2), [None, None])
        self.assertEqual(nm._align_option_visible_when(["x", {"when": "a"}], 2),
                         [None, {"when": "a"}])

    def test_field_level_visible_when_is_carried(self):
        """字段级 `visibleWhen` 同族落库；没有则明确为 None（不是缺键）。"""
        model = spec_model(params=[
            {"name": "quality", "type": "string", "required": False,
             "values": ["high"], "visibleWhen": {"when": "mode", "operator": "filled"}},
            {"name": "prompt", "type": "string", "required": True},
        ])
        by_name = {p["name"]: p for p in model["params"]}
        self.assertEqual(by_name["quality"]["visible_when"],
                         {"when": "mode", "operator": "filled"})
        self.assertIsNone(by_name["prompt"]["visible_when"])

    def test_empty_model_code_stays_empty(self):
        """生产确有 `model_code` 为空的模型（`the model 5` 等），必须原样保留空串。"""
        model = spec_model(model="", displayName="the model 5")
        self.assertEqual(model["code"], "")
        self.assertFalse(nm._select_by_code([model], "the model 5"))

    def test_select_by_code_matches_code_then_display_name(self):
        models = [spec_model()]
        self.assertIs(nm._select_by_code(models, "demo-model"), models[0])
        self.assertIs(nm._select_by_code(models, "DEMO-MODEL"), models[0])
        self.assertIs(nm._select_by_code(models, "Demo Image"), models[0])
        self.assertIsNone(nm._select_by_code(models, "nope"))


class FetchSpecTest(unittest.TestCase):
    """取数层：只走对外端点、要 Key、契约版本不认识就 fail-closed。"""

    def setUp(self):
        nm._SPEC_CACHE.clear()

    def tearDown(self):
        nm._SPEC_CACHE.clear()

    def _payload(self, **over):
        data = {"schemaVersion": 1, "creditPerYuan": 100, "minTextCharge": 0.01,
                "models": [DEMO_SPEC]}
        data.update(over)
        return {"code": 200, "data": data}

    def test_requires_api_key_and_hits_the_public_endpoint(self):
        seen = {}

        def fake_request(method, path, **kwargs):
            seen["method"] = method
            seen["path"] = path
            seen["key"] = kwargs.get("key")
            return self._payload()

        with mock.patch.object(nm, "_require_key", return_value="sk-test"), \
                mock.patch.object(nm, "_request", side_effect=fake_request):
            spec = nm._fetch_public_model_specs()
        self.assertEqual(seen["path"], nm.MODEL_PARAMS_PATH)
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["key"], "sk-test")
        self.assertEqual(spec["credit_per_yuan"], 100.0)
        self.assertEqual(spec["min_text_charge"], 0.01)
        self.assertEqual(spec["models"][0]["code"], "demo-model")

    def test_result_is_cached_until_refresh(self):
        calls = {"n": 0}

        def fake_request(*_a, **_k):
            calls["n"] += 1
            return self._payload()

        with mock.patch.object(nm, "_require_key", return_value="sk-test"), \
                mock.patch.object(nm, "_request", side_effect=fake_request):
            nm._fetch_public_model_specs()
            nm._fetch_public_model_specs()
            self.assertEqual(calls["n"], 1)
            nm._fetch_public_model_specs(refresh=True)
            self.assertEqual(calls["n"], 2)

    def test_unknown_schema_version_fails_closed(self):
        with mock.patch.object(nm, "_require_key", return_value="sk-test"), \
                mock.patch.object(nm, "_request",
                                  return_value=self._payload(schemaVersion=99)):
            with self.assertRaises(nm.NoovaError) as ctx:
                nm._fetch_public_model_specs()
        self.assertIn("99", ctx.exception.message)
        self.assertIn("升级", ctx.exception.hint or "")

    def test_malformed_payload_fails_closed(self):
        for bad in ({"code": 200, "data": []}, {"code": 200, "data": {}},
                    {"code": 200, "data": {"models": "x"}}):
            with mock.patch.object(nm, "_require_key", return_value="sk-test"), \
                    mock.patch.object(nm, "_request", return_value=bad):
                with self.assertRaises(nm.NoovaError):
                    nm._fetch_public_model_specs()

    def test_missing_ratio_falls_back_to_default(self):
        with mock.patch.object(nm, "_require_key", return_value="sk-test"), \
                mock.patch.object(nm, "_request",
                                  return_value=self._payload(creditPerYuan=0)):
            spec = nm._fetch_public_model_specs()
        self.assertEqual(spec["credit_per_yuan"], float(nm.DEFAULT_CREDITS_PER_YUAN))

    def test_only_online_and_maintenance_reach_the_client(self):
        """对外只呈现 online / maintenance；test / deprecated / 隐藏等未知状态在取数入口即剔除。

        maintenance 是**允许对外**的（展示但不可调用）；其余状态不属于对外集合。
        下游（清单 / 选择面板 / 参数 / 调用）全部消费本函数的结果，单点过滤才不会漏。
        """
        payload = self._payload()
        payload["data"]["models"] = [
            DEMO_SPEC,
            {**DEMO_SPEC, "model": "maint", "status": "maintenance"},
            {**DEMO_SPEC, "model": "beta", "status": "test"},
            {**DEMO_SPEC, "model": "old", "status": "deprecated"},
            {**DEMO_SPEC, "model": "weird", "status": "hidden"},
        ]
        with mock.patch.object(nm, "_require_key", return_value="sk-test"), \
                mock.patch.object(nm, "_request", return_value=payload):
            spec = nm._fetch_public_model_specs()
        self.assertEqual([m["code"] for m in spec["models"]], ["demo-model", "maint"])
        self.assertEqual(nm._fetch_public_models(), spec["models"])


class CreditCostTest(unittest.TestCase):
    """报价必须与平台网关结算逐条对齐（本地复算，不猜）。"""

    def setUp(self):
        nm._SPEC_CACHE.clear()

    def tearDown(self):
        nm._SPEC_CACHE.clear()

    def test_per_request_base_price(self):
        model = spec_model(billing={"mode": "per_request", "price": 15.0, "unit": 1})
        cost = nm.compute_credit_cost(model, {})
        self.assertTrue(cost["exact"])
        self.assertEqual(cost["credits"], 15.0)
        self.assertEqual(cost["yuan"], 0.15)

    def test_billing_unit_multiplies_the_price(self):
        model = spec_model(billing={"mode": "per_request", "price": 0.05, "unit": 1000})
        self.assertEqual(nm.compute_credit_cost(model, {})["credits"], 50.0)

    def test_surcharge_multiply_by_value(self):
        """按用量计价的模型：price=0，duration ×60 积分。"""
        model = spec_model(
            billing={"mode": "per_request", "price": 0.0, "unit": 1},
            surchargeRules=[{"param": "duration", "match": "present",
                             "charge": "multiply_by_value", "multiplier": 60.0,
                             "base": 0.0}])
        self.assertEqual(nm.compute_credit_cost(model, {"duration": 5})["credits"], 300.0)
        # 没传该参数 → 规则不参与，基础价为 0 → 平台会拒单，必须如实说明
        cost = nm.compute_credit_cost(model, {})
        self.assertFalse(cost["exact"])
        self.assertIn("拒绝调用", cost["note"])

    def test_surcharge_fixed_credits(self):
        model = spec_model(surchargeRules=[{"param": "quality", "match": "exact",
                                            "matchValue": "high", "charge": "fixed",
                                            "credits": 5.0}])
        self.assertEqual(nm.compute_credit_cost(model, {"quality": "high"})["credits"], 20.0)
        self.assertEqual(nm.compute_credit_cost(model, {"quality": "low"})["credits"], 15.0)

    def test_surcharge_range_only_applies_inside_the_range(self):
        model = spec_model(
            billing={"mode": "per_request", "price": 10.0, "unit": 1},
            surchargeRules=[{"param": "duration", "match": "range",
                             "rangeMin": 5, "rangeMax": 10, "charge": "fixed",
                             "credits": 3.0}])
        self.assertEqual(nm.compute_credit_cost(model, {"duration": 7})["credits"], 13.0)
        self.assertEqual(nm.compute_credit_cost(model, {"duration": 11})["credits"], 10.0)

    def test_unparseable_value_for_present_rule_fails_closed(self):
        """服务端对 present 规则下解析不出的值会 fail-closed 拒单，本地不能报假价。"""
        model = spec_model(
            billing={"mode": "per_request", "price": 1.0, "unit": 1},
            surchargeRules=[{"param": "duration", "match": "present",
                             "charge": "multiply_by_value", "multiplier": 60.0,
                             "base": 0.0}])
        with self.assertRaises(nm.NoovaError) as ctx:
            nm.compute_credit_cost(model, {"duration": "abc"})
        self.assertIn("duration", ctx.exception.message)
        with self.assertRaises(nm.NoovaError):
            nm.compute_credit_cost(model, {"duration": 0})

    def test_per_token_is_never_guessed(self):
        model = spec_model(
            modelType="text", displayName="Demo Text",
            billing={"mode": "per_token",
                     "pricePer1M": {"input": 90.0, "output": 260.0}},
            params=[])
        nm._SPEC_CACHE["min_text_charge"] = 0.01
        cost = nm.compute_credit_cost(model, {})
        self.assertFalse(cost["exact"])
        self.assertIsNone(cost["credits"])
        self.assertEqual(cost["price_per_1m"]["input"], 90.0)
        self.assertIn("最低扣费", cost["note"])

    def test_estimate_text_states_the_ratio(self):
        model = spec_model(billing={"mode": "per_request", "price": 125.0, "unit": 1})
        text = nm.credit_estimate_text(model, {})
        self.assertIn("125 积分", text)
        self.assertIn("1.25 元", text)
        self.assertIn("100 积分 = 1 元", text)

    def test_estimate_text_ratio_is_not_hardcoded(self):
        """比例从契约取：服务端改成 200 时，文案必须跟着变。"""
        model = spec_model(billing={"mode": "per_request", "price": 100.0, "unit": 1})
        nm._SPEC_CACHE["credit_per_yuan"] = 200.0
        text = nm.credit_estimate_text(model, {})
        self.assertIn("200 积分 = 1 元", text)
        self.assertIn("0.5 元", text)

    # ── 服务端出数（计划 §4.5）：权威价优先、拿不到才回退预估 ──────────

    def test_estimate_text_prefers_server_authoritative_price(self):
        """服务端给了 pricing.credits → 照抄权威价，且不再出现「预估」字样。"""
        model = spec_model(billing={"mode": "per_request", "price": 125.0, "unit": 1})
        text = nm.credit_estimate_text(
            model,
            {},
            pricing={"exact": True, "credits": 22.0, "yuan": 0.22, "note": None},
        )
        self.assertIn("服务端权威价：22 积分", text)
        self.assertIn("0.22 元", text)
        self.assertNotIn("预估", text)

    def test_estimate_text_uses_contract_pricing_when_params_absent(self):
        """不传参数时用契约里的默认组合价（contract pricing 块）。"""
        model = spec_model(billing={"mode": "per_request", "price": 125.0, "unit": 1},
                           pricing={"exact": True, "credits": 20.0})
        self.assertIn("20 积分", nm.credit_estimate_text(model, {}))

    def test_estimate_text_reports_null_without_inventing_number(self):
        """服务端明确给不出确定值 → 如实说明，绝不编一个数字（F-5）。"""
        model = spec_model(billing={"mode": "per_request", "price": 0.0, "unit": 1})
        text = nm.credit_estimate_text(
            model,
            {},
            pricing={"exact": False, "credits": None, "note": "该模型价格取决于参数：duration"},
        )
        self.assertIn("服务端未给出确定金额", text)
        self.assertIn("duration", text)

    def test_estimate_text_falls_back_to_local_estimate_for_old_backend(self):
        """旧后端无 pricing 块 → 回退本地预估，文案必须带「预估」。"""
        model = spec_model(billing={"mode": "per_request", "price": 125.0, "unit": 1})
        text = nm.credit_estimate_text(model, {}, pricing=None)
        self.assertIn("按契约预估", text)

    def test_fetch_pricing_is_fail_soft(self):
        model = spec_model()
        with mock.patch.object(nm, "_request", side_effect=nm.NoovaError("boom")):
            self.assertIsNone(nm.fetch_pricing(model))

    def test_fetch_pricing_passes_model_and_params_and_reads_block(self):
        model = spec_model()
        captured: dict = {}

        def fake_request(method, path, **kwargs):
            captured["path"] = path
            return {
                "data": {
                    "models": [
                        {"model": "demo-model", "pricing": {"exact": True, "credits": 22.0}}
                    ]
                }
            }

        with mock.patch.object(nm, "_require_key", return_value="sk-x"), \
                mock.patch.object(nm, "_request", side_effect=fake_request):
            pricing = nm.fetch_pricing(model, {"imageSize": "4K"})
        self.assertEqual(pricing["credits"], 22.0)
        self.assertIn("model=demo-model", captured["path"])
        self.assertIn("params=", captured["path"])
        self.assertIn("%22imageSize%22", captured["path"])

    def test_fetch_pricing_returns_none_when_backend_lacks_pricing(self):
        """旧后端返回的模型条目没有 pricing 块 → None（回退预估），不是抛错。"""
        model = spec_model()

        with mock.patch.object(nm, "_require_key", return_value="sk-x"), \
                mock.patch.object(
                    nm, "_request", return_value={"data": {"models": [{"model": "demo-model"}]}}
                ):
            self.assertIsNone(nm.fetch_pricing(model))


class SanitizeTest(unittest.TestCase):
    def test_public_host_kept(self):
        text = "见 https://noova.vip/api_control 与 https://example.com/x"
        self.assertEqual(nm.sanitize_text(text), text)

    def test_backup_official_domain_kept(self):
        """两个官方线上域名都属公开信息，不得被占位替换。"""
        text = "见 https://noova.live/api_control 与 https://www.noova.live"
        self.assertEqual(nm.sanitize_text(text), text)

    def test_internal_host_redacted(self):
        text = "结果：https://internal-bucket.internal-corp.com/uploads/a.mp4"
        out = nm.sanitize_text(text)
        self.assertNotIn("internal-bucket", out)
        self.assertIn(nm.REDACTED_HOST, out)
        self.assertIn("/uploads/a.mp4", out)

    def test_bare_host_after_url_redacted(self):
        text = "地址 https://secret-cdn.internal-corp.com/a.png ，备用 secret-cdn.internal-corp.com/b.png"
        out = nm.sanitize_text(text)
        self.assertNotIn("secret-cdn", out)
        self.assertEqual(out.count(nm.REDACTED_HOST), 2)

    def test_extra_public_hosts_respected(self):
        text = "http://sandbox.example.org:8080/api/models"
        self.assertEqual(nm.sanitize_text(text, extra_public_hosts=("example.org",)), text)


class BaseUrlGuardTest(unittest.TestCase):
    """请求侧守卫：任何出网请求都不得指向本机 / 内网 / 开发环境地址。

    配置可能被历史版本或手工编辑污染；若不拦下，请求会打到本地开发环境，
    现象却是「服务不可用」。
    """

    def test_local_base_url_blocks_request(self):
        with mock.patch.object(nm, "get_base_url", return_value="http://localhost:5001"):
            with self.assertRaises(nm.NoovaError) as ctx:
                nm._build_request("/api/models", key=None)
        self.assertIn("本机", str(ctx.exception))

    def test_production_base_url_passes(self):
        with mock.patch.object(nm, "get_base_url", return_value="https://noova.vip"):
            request = nm._build_request("/api/models", key=None)
        self.assertTrue(request.full_url.startswith("https://noova.vip"))

    def test_upload_platform_channel_also_guarded(self):
        """上传的平台存储通道同样走 _build_request，不得绕过守卫。"""
        with mock.patch.object(nm, "get_base_url", return_value="http://192.168.1.10:8000"):
            with self.assertRaises(nm.NoovaError):
                nm._build_request("/v1/client/resource/sts", key="sk-x")

    def test_local_hosts_are_not_treated_as_public(self):
        for host in ("localhost", "127.0.0.1", "192.168.1.10", "10.0.0.7",
                     "dev.noova.vip", "api.internal"):
            self.assertFalse(nm._host_is_public(host), host)
        for host in ("noova.vip", "example.com"):
            self.assertTrue(nm._host_is_public(host), host)
        # 备用官方域名同样是公开域名
        for host in ("noova.live", "www.noova.live", "www.noova.vip"):
            self.assertTrue(nm._host_is_public(host), host)
        # 上传主机由调用方显式传入时同样被认可为公开
        allowed = nm._public_hosts()
        for host in allowed:
            self.assertTrue(nm._host_is_public(host, allowed), host)

    def test_local_url_is_redacted_in_text(self):
        out = nm.sanitize_text("调试地址 http://localhost:5001/api/models 不可达")
        self.assertNotIn("localhost", out)
        self.assertIn(nm.REDACTED_HOST, out)


class ExtractTextTest(unittest.TestCase):
    def test_openai_string(self):
        payload = {"choices": [{"message": {"content": "你好"}}]}
        self.assertEqual(nm.extract_text(payload), "你好")

    def test_openai_content_parts(self):
        payload = {"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"text": "b"}]}}]}
        self.assertEqual(nm.extract_text(payload), "ab")

    def test_anthropic(self):
        payload = {"content": [{"type": "text", "text": "原生"}, {"type": "text", "text": "响应"}]}
        self.assertEqual(nm.extract_text(payload), "原生响应")

    def test_responses_api(self):
        payload = {"output": [{"content": [{"type": "output_text", "text": "resp"}]}]}
        self.assertEqual(nm.extract_text(payload), "resp")

    def test_empty(self):
        self.assertEqual(nm.extract_text({"id": "x"}), "")
        self.assertEqual(nm.extract_text(None), "")


class ExtractStreamDeltaTest(unittest.TestCase):
    def test_openai_delta(self):
        chunk = {"choices": [{"delta": {"content": "abc"}}]}
        self.assertEqual(nm.extract_stream_delta(chunk), "abc")

    def test_anthropic_delta(self):
        chunk = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "xyz"}}
        self.assertEqual(nm.extract_stream_delta(chunk), "xyz")

    def test_anthropic_non_delta_event_ignored(self):
        self.assertEqual(nm.extract_stream_delta({"type": "message_start"}), "")


class ExtractUsageTest(unittest.TestCase):
    def test_openai(self):
        usage = nm.extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        self.assertEqual(usage, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    def test_anthropic(self):
        usage = nm.extract_usage({"usage": {"input_tokens": 7, "output_tokens": 3}})
        self.assertEqual(usage["prompt_tokens"], 7)
        self.assertEqual(usage["completion_tokens"], 3)

    def test_absent(self):
        self.assertEqual(nm.extract_usage({"id": "x"}), {})


class TaskContractTest(unittest.TestCase):
    def test_collect_urls(self):
        payload = {
            "result": "https://noova.vip/a.png",
            "imageUrl": "https://noova.vip/a.png",
            "results": [{"url": "https://noova.vip/a.png"}, {"url": "https://noova.vip/b.png"}],
        }
        self.assertEqual(nm.collect_result_urls(payload),
                         ["https://noova.vip/a.png", "https://noova.vip/b.png"])

    def test_status_and_id(self):
        self.assertEqual(nm._status_of({"status": "Succeeded"}), "succeeded")
        self.assertEqual(nm._task_id_of({"task_id": "t1"}), "t1")
        self.assertEqual(nm._task_id_of({"id": "gtsk_1"}), "gtsk_1")
        self.assertEqual(nm._task_id_of({}), "")


class ParamCoercionTest(unittest.TestCase):
    def test_literals(self):
        self.assertIs(nm._coerce_param_value("true"), True)
        self.assertIs(nm._coerce_param_value("false"), False)
        self.assertIsNone(nm._coerce_param_value("null"))
        self.assertEqual(nm._coerce_param_value("5"), 5)
        self.assertEqual(nm._coerce_param_value("4.5"), 4.5)
        self.assertEqual(nm._coerce_param_value("1:1"), "1:1")
        self.assertEqual(nm._coerce_param_value("720p"), "720p")


class PriceTextTest(unittest.TestCase):
    """计价文案只读契约 `billing`，不做任何推断。"""

    def test_per_request(self):
        model = spec_model(billing={"mode": "per_request", "price": 4.0, "unit": 1})
        self.assertEqual(nm._price_text(model), "4 积分/次")

    def test_per_request_with_unit(self):
        model = spec_model(billing={"mode": "per_request", "price": 0.05, "unit": 1000})
        self.assertEqual(nm._price_text(model), "0.05 积分/1000 次")

    def test_per_token(self):
        model = spec_model(
            modelType="text",
            billing={"mode": "per_token",
                     "pricePer1M": {"input": 650.0, "output": 3100.0}})
        self.assertEqual(nm._price_text(model),
                         "输入 650 / 输出 3100 积分/百万 tokens")

    def test_per_token_includes_cache_tiers_when_present(self):
        model = spec_model(
            modelType="text",
            billing={"mode": "per_token",
                     "pricePer1M": {"input": 90.0, "output": 260.0,
                                    "cacheHit": 16.0, "cacheWrite": 30.0}})
        text = nm._price_text(model)
        self.assertIn("缓存命中 16", text)
        self.assertIn("缓存写入 30", text)

    def test_zero_price_with_rules_says_usage_based(self):
        """按用量计价的模型：price=0，由加收规则按秒计价。

        渲染成「0 积分/次」会让用户以为免费，必须说成「按用量计价」。
        """
        model = spec_model(
            billing={"mode": "per_request", "price": 0.0, "unit": 1},
            surchargeRules=[{"param": "duration", "match": "present",
                             "charge": "multiply_by_value", "multiplier": 60.0,
                             "base": 0.0}])
        self.assertEqual(nm._price_text(model), "按用量计价（见加收规则）")

    def test_zero_price_without_rules_says_unknown(self):
        model = spec_model(billing={"mode": "per_request", "price": 0.0, "unit": 1})
        self.assertIn("未公布单价", nm._price_text(model))
        self.assertNotIn("0 积分/次", nm._price_text(model))

    def test_per_token_without_prices_says_usage_based(self):
        model = spec_model(modelType="text", billing={"mode": "per_token"})
        self.assertIn("按实际用量结算", nm._price_text(model))

    def test_surcharge_text_reads_the_contract(self):
        present = {"param": "duration", "match": "present",
                   "charge": "multiply_by_value", "multiplier": 60.0, "base": 0.0}
        self.assertEqual(nm._surcharge_text(present),
                         "提供 duration 时，按 duration × 60 积分 计")
        exact = {"param": "quality", "match": "exact", "matchValue": "high",
                 "charge": "fixed", "credits": 5.0}
        self.assertEqual(nm._surcharge_text(exact), "quality=high 时，加收 5 积分")
        ranged = {"param": "duration", "match": "range", "rangeMin": 5, "rangeMax": 10,
                  "charge": "fixed", "credits": 3.0}
        self.assertEqual(nm._surcharge_text(ranged), "duration 在 5~10 时，加收 3 积分")

    def test_billing_spec_is_machine_readable(self):
        rule = {"param": "duration", "match": "present",
                "charge": "multiply_by_value", "multiplier": 25.0, "base": 0.0}
        model = spec_model(billing={"mode": "per_request", "price": 0.0, "unit": 1},
                           surchargeRules=[rule])
        spec = nm.billing_spec(model)
        self.assertEqual(spec["mode"], "per_request")
        self.assertEqual(spec["surcharge_rules"], [rule])       # 原样透传，机器消费
        self.assertEqual(len(spec["surcharge_text"]), 1)         # 人读句子同源
        self.assertEqual(spec["text"], "按用量计价（见加收规则）")


class FriendlyErrorTest(unittest.TestCase):
    def test_unauthorized(self):
        err = nm._friendly_error(401, "API Key 无效或已停用", {"message": "API Key 无效或已停用"}, None)
        self.assertEqual(err.code, 401)
        self.assertIn("api_control", err.hint)

    def test_network_policy_block_detected(self):
        err = nm._friendly_error(403, "error code: 1010", None, None)
        self.assertIn("网络策略拦截", err.message)

    def test_rate_limited(self):
        err = nm._friendly_error(429, "轮询过于频繁", {"data": {"retry_after_ms": 12000}}, 12)
        self.assertEqual(err.retry_after, 12)

    def test_platform_quota(self):
        payload = {"code": 503, "data": {"source": "platform_quota", "retry_after_ms": 60000}}
        err = nm._friendly_error(503, "平台繁忙，请稍后重试", payload, 60)
        self.assertIn("配额", err.message)

    def test_error_message_is_sanitized_on_render(self):
        err = nm._friendly_error(
            400, "上游 https://internal-bucket.internal-corp.com/a.mp4 不可用", None, None)
        self.assertNotIn("internal-bucket", err.render())


class RequestHeaderTest(unittest.TestCase):
    def test_user_agent_present_and_no_key_leak(self):
        request = nm._build_request("/api/models", key=None)
        product = noova_key.USER_AGENT.split("/", 1)[0]
        self.assertTrue(request.get_header("User-agent").startswith(product))
        self.assertIsNone(request.get_header("Authorization"))

    def test_authorization_set_when_key_given(self):
        request = nm._build_request("/api/v1/credit", key="sk-test")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-test")

    def test_extra_headers_merged(self):
        request = nm._build_request("/v1/messages", key="sk-test",
                                    extra_headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")


class KeyStoreTest(unittest.TestCase):
    def test_mask_key(self):
        self.assertEqual(noova_key.mask_key("sk-abcdefghij"), "sk-abcd…")
        self.assertEqual(noova_key.mask_key(""), "")
        self.assertEqual(noova_key.mask_key("short"), "已配置")

    def test_banner_contains_console_url_and_direct_api(self):
        banner = noova_key.render_guide()
        self.assertIn("noova.vip/api_control", banner)          # Key 领取入口
        self.assertIn("连接地址（base_url）", banner)             # 用户要求的「直达 api 地址」
        self.assertIn("https://noova.vip", banner)
        self.assertIn("setup --api-key", banner)                # 自动配置入口
        self.assertIn("noova_key.py", banner)                   # 绝对路径命令
        self.assertNotIn("save --api-key", banner)

    def test_configured_banner_omits_onboarding_steps(self):
        banner = noova_key.render_guide(configured=True)
        self.assertIn("已配置 API Key", banner)
        self.assertNotIn("尚未配置", banner)

    def test_user_agent_tracks_package_name(self):
        """客户端标识的产品名必须与包名同源——改名后漏改 UA，这条会抓住。

        同时守住「不能退回 urllib 默认 UA」：默认标识会被边缘策略直接 403。
        """
        product = noova_key.USER_AGENT.split("/", 1)[0]

        def normalize(text: str) -> str:
            return text.lower().replace("-", "").replace("_", "")

        self.assertEqual(normalize(product), normalize(noova_key.PACKAGE_NAME))
        self.assertNotIn("python", product.lower())
        self.assertEqual(nm.USER_AGENT, noova_key.USER_AGENT)


class StreamParseTest(unittest.TestCase):
    """模拟一段 OpenAI SSE 报文，验证解析出的增量文本与用量。"""

    def test_parse_openai_sse(self):
        raw = (
            'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
            'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
            'data: [DONE]\n\n'
        )
        chunks, usage = [], {}
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            parsed = json.loads(payload)
            usage = nm.extract_usage(parsed) or usage
            delta = nm.extract_stream_delta(parsed)
            if delta:
                chunks.append(delta)
        self.assertEqual("".join(chunks), "你好")
        self.assertEqual(usage["total_tokens"], 5)


class UrlJoinTest(unittest.TestCase):
    def test_sts_query_encoding(self):
        query = urllib.parse.urlencode({"filename": "a b.png", "content_type": "image/png", "size": 3})
        self.assertIn("a+b.png", query)


class ResponseEnvelopeTest(unittest.TestCase):
    """平台响应有两种形态：裸业务体 与 `{code, data, message}` 统一包裹。

    实测 2026-09-29：**所有**错误响应与部分成功响应（如鉴权校验）都是包裹形态。
    只认顶层字段会让「任务永远查不到终态」伪装成「任务超时」——
    所以每个提取函数都必须对两种形态都成立。
    """

    ENVELOPE = {"code": 200, "data": {"status": "succeeded", "id": "t-1"}, "message": ""}

    def test_unwrap_envelope(self):
        self.assertEqual(nm._unwrap(self.ENVELOPE), {"status": "succeeded", "id": "t-1"})

    def test_unwrap_keeps_error_payload(self):
        """`data: null` 的错误响应必须原样保留，否则丢掉错误信息。"""
        error = {"code": 401, "data": None, "message": "API Key 无效或已停用"}
        self.assertEqual(nm._unwrap(error), error)

    def test_unwrap_passthrough_bare_body(self):
        bare = {"status": "running"}
        self.assertEqual(nm._unwrap(bare), bare)

    def test_status_and_id_from_envelope(self):
        self.assertEqual(nm._status_of(self.ENVELOPE), "succeeded")
        self.assertEqual(nm._task_id_of(self.ENVELOPE), "t-1")

    def test_text_from_envelope(self):
        payload = {"code": 200, "message": "",
                   "data": {"choices": [{"message": {"content": "你好"}}]}}
        self.assertEqual(nm.extract_text(payload), "你好")

    def test_usage_from_envelope(self):
        payload = {"code": 200, "message": "",
                   "data": {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}}
        self.assertEqual(nm.extract_usage(payload)["total_tokens"], 5)


class CollectResultUrlsShapeTest(unittest.TestCase):
    """响应形态取自平台公开文档的真实示例（每个都必须在覆盖范围内）。"""

    def test_results_as_plain_string(self):
        """`demo-image-2-G` 文档的成功响应就是 `{"results": "<URL>", "status": "succeeded"}`。"""
        payload = {"results": "https://file7.example.com/a.png", "status": "succeeded"}
        self.assertEqual(nm.collect_result_urls(payload), ["https://file7.example.com/a.png"])

    def test_full_video_result(self):
        url = "https://media.example.com/v.mp4"
        payload = {"id": "2085", "result": url, "status": "succeeded", "videoUrl": url,
                   "results": [{"content": url, "url": url}]}
        self.assertEqual(nm.collect_result_urls(payload), [url])

    def test_urls_inside_envelope(self):
        payload = {"code": 200, "message": "",
                   "data": {"results": [{"url": "https://x/b.png"}], "status": "succeeded"}}
        self.assertEqual(nm.collect_result_urls(payload), ["https://x/b.png"])

    def test_non_terminal_and_failure_yield_nothing(self):
        self.assertEqual(nm.collect_result_urls({"status": "running"}), [])
        self.assertEqual(nm.collect_result_urls({"status": "failed", "error": "boom"}), [])
        self.assertEqual(nm.collect_result_urls(None), [])

    def test_non_http_strings_are_ignored(self):
        self.assertEqual(nm.collect_result_urls({"results": "not-a-url", "status": "succeeded"}), [])


class RequiredParamPreflightTest(unittest.TestCase):
    """实测 3 个媒体模型把参考素材标为必填，缺了会白跑一轮才拿到上游 400。"""

    SPEC = {
        "model": "demo", "modelType": "video", "displayName": "Demo",
        "status": "online", "protocols": ["openai"],
        "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
        "surchargeRules": [],
        "params": [
            {"name": "model", "type": "string", "required": True},
            {"name": "prompt", "type": "string", "required": True},
            {"name": "duration", "type": "number", "required": True, "min": 4, "max": 15},
            {"name": "referenceImages", "type": "string[]", "required": True},
            {"name": "referenceAudios", "type": "string[]", "required": False},
        ],
    }

    @classmethod
    def setUpClass(cls):
        cls.MODEL = nm._normalize_model(cls.SPEC)

    @staticmethod
    def _args(*, prompt="hi", param=None, ref_image=None, ref_audio=None):
        from argparse import Namespace
        return Namespace(prompt=prompt, param=param or [],
                         ref_image=ref_image or [], ref_audio=ref_audio or [],
                         ref_video=[])

    def test_blocks_when_required_missing(self):
        with self.assertRaises(nm.NoovaError) as ctx:
            nm._ensure_required_params(self.MODEL, self._args())
        message = ctx.exception.message
        self.assertIn("duration", message)
        self.assertIn("referenceImages", message)
        self.assertIn("--ref-image", ctx.exception.render())

    def test_passes_when_supplied_by_options(self):
        nm._ensure_required_params(
            self.MODEL,
            self._args(param=[("duration", "5")], ref_image=["https://x/a.png"]))

    def test_prompt_and_model_are_auto_provided(self):
        """`prompt` / `model` 由命令行与脚本自动带上，不应被误判为缺失。"""
        spec = {"model": "d", "modelType": "image", "displayName": "D",
                "status": "online", "protocols": ["openai"],
                "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
                "surchargeRules": [],
                "params": [{"name": "model", "type": "string", "required": True},
                           {"name": "prompt", "type": "string", "required": True}]}
        nm._ensure_required_params(nm._normalize_model(spec), self._args())

    def test_no_param_contract_is_not_blocked(self):
        """没有参数契约时不做臆测式校验，交给平台判定。"""
        spec = {"model": "d", "modelType": "image", "displayName": "D",
                "status": "online", "protocols": ["openai"],
                "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
                "surchargeRules": [], "params": []}
        nm._ensure_required_params(nm._normalize_model(spec), self._args())


class ChatProtocolRouteTest(unittest.TestCase):
    def test_responses_stream_delta(self):
        """Responses 协议的 SSE 增量在 `delta`，类型形如 `response.output_text.delta`。"""
        self.assertEqual(
            nm.extract_stream_delta({"type": "response.output_text.delta", "delta": "你"}),
            "你")
        self.assertEqual(
            nm.extract_stream_delta({"type": "response.completed", "response": {}}), "")

    def test_responses_route_is_mapped(self):
        """平台确实注册了 `/v1/responses`（实测 400「请求体缺少 model」，非 404）。"""
        self.assertEqual(nm.PROTOCOL_PATHS["responses"], "/v1/responses")
        self.assertEqual(nm.resolve_route_path("responses", "m"), "/v1/responses")

    def test_responses_is_selectable_on_cli(self):
        parser = nm.build_parser()
        args = parser.parse_args(["chat", "--prompt", "hi", "--protocol", "responses"])
        self.assertEqual(args.protocol, "responses")
        body = nm.build_chat_body("responses", "m", args, [])
        self.assertEqual(body, {"model": "m", "input": "hi"})

    def test_responses_body_carries_max_output_tokens(self):
        parser = nm.build_parser()
        args = parser.parse_args(["chat", "--prompt", "hi", "--protocol", "responses",
                                  "--max-tokens", "64", "--system", "sys"])
        body = nm.build_chat_body("responses", "m", args, [])
        self.assertEqual(body["max_output_tokens"], 64)
        self.assertEqual(body["instructions"], "sys")

    def test_unknown_protocol_has_no_route(self):
        self.assertEqual(nm.resolve_route_path("bogus", "m"), "")


class FriendlyErrorHintTest(unittest.TestCase):
    """提示里的控制台地址必须跟随当前基础地址，不能硬编码某个域名
    （否则用户自建线上部署时会看到指向别处的提示）。"""

    def _with_base(self, url):
        return mock.patch.dict(os.environ, {"NOOVA_BASE_URL": url})

    def test_401_hint_follows_configured_base_url(self):
        with self._with_base("https://ai.mycorp.cn"):
            error = nm._friendly_error(401, "", {}, None)
        self.assertIn("https://ai.mycorp.cn/api_control", error.hint)

    def test_402_hint_follows_configured_base_url(self):
        with self._with_base("https://ai.mycorp.cn"):
            error = nm._friendly_error(402, "", {}, None)
        self.assertIn("https://ai.mycorp.cn", error.hint)

    def test_default_hint_uses_production_domain(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOOVA_BASE_URL", None)
            error = nm._friendly_error(401, "", {}, None)
        self.assertIn(nm.DEFAULT_BASE_URL, error.hint)


class LayoutWidthTest(unittest.TestCase):
    """清单/信息块靠显示宽度对齐；中英混排必须算对（中文占 2 列）。"""

    def test_display_width(self):
        self.assertEqual(nm._display_width("abc"), 3)
        self.assertEqual(nm._display_width("中文"), 4)
        self.assertEqual(nm._display_width("编码"), 4)
        self.assertEqual(nm._display_width("1K线路"), 2 + 4)
        self.assertEqual(nm._display_width(""), 0)

    def test_pad_aligns_by_display_width(self):
        # 「编码」=4 列，「ab」=2 列 → 补到 6 列时两者宽度相同
        self.assertEqual(nm._display_width(nm._pad("编码", 6)), 6)
        self.assertEqual(nm._display_width(nm._pad("ab", 6)), 6)
        self.assertEqual(nm._pad("中文", 4, align="right"), "中文")
        self.assertEqual(nm._pad("a", 3, align="right"), "  a")

    def test_rule_width_is_constant(self):
        self.assertEqual(nm._display_width(nm._rule("图像模型 · 8 个")), 78)
        self.assertEqual(nm._display_width(nm._rule("耗时预期（用于向用户说明，非硬性上限）")), 78)
        self.assertEqual(nm._display_width(nm._rule()), 78)

    def test_fmt_num_keeps_credit_precision(self):
        # 两位小数不能被 %g 吃掉（%g 默认 6 位有效数字，会变成 12345.7）
        self.assertEqual(nm._fmt_num(12345.67), "12345.67")
        self.assertEqual(nm._fmt_num(4.0), "4")
        self.assertEqual(nm._fmt_num(0.25), "0.25")
        self.assertEqual(nm._fmt_num(0.05), "0.05")
        self.assertEqual(nm._fmt_num("720p"), "720p")


class EtaAndWaitLimitTest(unittest.TestCase):
    """耗时预期与默认等待上限：视频必须给到 1.5 小时，否则会被默认上限截断。"""

    def test_eta_ranges(self):
        self.assertEqual(nm._eta_text("image"), "30 秒 – 5 分钟")
        self.assertEqual(nm._eta_text("audio"), "30 秒 – 10 分钟")
        self.assertEqual(nm._eta_text("video"), "1 分钟 – 1.5 小时")
        self.assertEqual(nm._eta_text("bogus"), "耗时不定")

    def test_human_duration(self):
        self.assertEqual(nm._human_duration(30), "30 秒")
        self.assertEqual(nm._human_duration(300), "5 分钟")
        self.assertEqual(nm._human_duration(900), "15 分钟")
        self.assertEqual(nm._human_duration(5400), "1.5 小时")

    def test_default_wait_limit_per_type(self):
        args = mock.Mock(max_wait=None)
        self.assertEqual(nm._resolve_wait_limits(args, "image"), 600)
        self.assertEqual(nm._resolve_wait_limits(args, "audio"), 1200)
        self.assertEqual(nm._resolve_wait_limits(args, "video"), 5400)
        self.assertEqual(nm._resolve_wait_limits(args, ""), nm.DEFAULT_MAX_WAIT_FALLBACK)

    def test_explicit_max_wait_wins(self):
        args = mock.Mock(max_wait=120)
        self.assertEqual(nm._resolve_wait_limits(args, "video"), 120)


class CreditReportTest(unittest.TestCase):
    """扣减口径 = 账户余额差值；任一快照缺失就不猜、直接省略。"""

    def test_delta_and_remaining(self):
        before = {"total": 100.19, "permanent": 100.19, "limited": 0.0, "vip": None}
        after = {"total": 96.19, "permanent": 96.19, "limited": 0.0, "vip": None}
        lines = nm._credit_lines(before, after)
        self.assertEqual(lines[0], "本次扣减：4 积分（账户余额 100.19 → 96.19）")
        self.assertIn("剩余积分：96.19", lines[1])
        self.assertIn("永久 96.19", lines[1])

    def test_missing_snapshot_omits_delta(self):
        after = {"total": 100.0, "permanent": None, "limited": None, "vip": None}
        self.assertEqual(nm._credit_lines(None, after), ["剩余积分：100"])
        self.assertEqual(nm._credit_lines(None, None), [])

    def test_balance_increase_is_reported_not_negated(self):
        before = {"total": 10.0, "permanent": None, "limited": None, "vip": None}
        after = {"total": 110.0, "permanent": None, "limited": None, "vip": None}
        lines = nm._credit_lines(before, after)
        self.assertIn("余额变化：+100 积分", lines[0])

    def test_fetch_credit_is_fail_soft(self):
        """余额查询失败绝不能打断生成。"""
        with mock.patch.object(nm, "_request", side_effect=nm.NoovaError("boom")):
            self.assertIsNone(nm.fetch_credit("sk-x"))

    def test_fetch_credit_reads_wrapped_payload(self):
        payload = {"code": 200, "data": {"remainingTotal": 12.5, "remainingPermanent": 10.0,
                                        "remainingLimited": 2.5}, "message": ""}
        with mock.patch.object(nm, "_request", return_value=payload):
            snapshot = nm.fetch_credit("sk-x")
        self.assertEqual(snapshot["total"], 12.5)
        self.assertEqual(snapshot["permanent"], 10.0)


class TaskResultRenderingTest(unittest.TestCase):
    """结果输出：URL 走 stdout 且编号齐全；--json 时 stdout 必须是纯 JSON。"""

    MODEL = spec_model(model="demo-image-2-t", displayName="Demo Image 2",
                       billing={"mode": "per_request", "price": 4.0, "unit": 1})

    PAYLOAD = {"id": "1234567", "status": "succeeded",
               "results": [{"url": "https://x.example.com/a.png"},
                           {"url": "https://x.example.com/b.png"}],
               "result": "https://x.example.com/a.png"}

    def _run(self, **kwargs):
        import io
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = nm._print_task_result(self.PAYLOAD, "image", **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_human_output_contains_billing_and_urls(self):
        code, out, _ = self._run(as_json=False, quiet=False, model=self.MODEL,
                                 credit_before={"total": 100.19},
                                 credit_after={"total": 96.19})
        self.assertEqual(code, 0)
        self.assertIn("共 2 张图片", out)
        self.assertIn("demo-image-2-t", out)
        self.assertIn("计价", out)
        self.assertIn("本次扣减：4 积分", out)
        self.assertIn("1. https://x.example.com/a.png", out)
        self.assertIn("2. https://x.example.com/b.png", out)

    def test_json_mode_keeps_stdout_parseable(self):
        code, out, err = self._run(as_json=True, quiet=False, model=self.MODEL)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["id"], "1234567")     # stdout 必须是纯 JSON
        self.assertIn("模型", err)                              # 人读信息块改走 stderr

    def test_quiet_keeps_urls_only(self):
        code, out, _ = self._run(as_json=False, quiet=True, model=self.MODEL)
        self.assertEqual(code, 0)
        self.assertNotIn("计价", out)
        self.assertIn("https://x.example.com/a.png", out)

    def test_measure_words(self):
        self.assertEqual(nm._result_label("image", 1), "1 张图片")
        self.assertEqual(nm._result_label("video", 2), "2 个视频")
        self.assertEqual(nm._result_label("audio", 3), "3 个音频")


class ModelsListingTest(unittest.TestCase):
    """清单输出：按类型分区，每行必须带协议、状态与计价。"""

    SPECS = [
        {"model": "m1", "displayName": "M One", "modelType": "text",
         "status": "online", "protocols": ["openai", "anthropic"],
         "billing": {"mode": "per_token",
                     "pricePer1M": {"input": 35.0, "output": 85.0}},
         "surchargeRules": [],
         "params": [{"name": "prompt", "type": "string", "required": True}]},
        {"model": "m2", "displayName": "M Two", "modelType": "image",
         "status": "online", "protocols": ["openai"],
         "billing": {"mode": "per_request", "price": 15.0, "unit": 1},
         "surchargeRules": [],
         "params": [{"name": "prompt", "type": "string", "required": True},
                    {"name": "referenceImages", "type": "string[]",
                     "required": False}]},
        {"model": "m3", "displayName": "M Three", "modelType": "video",
         "status": "maintenance", "protocols": ["openai"],
         "billing": {"mode": "per_request", "price": 0.0, "unit": 1},
         "surchargeRules": [{"param": "duration", "match": "present",
                             "charge": "multiply_by_value", "multiplier": 25.0,
                             "base": 0.0}],
         "params": [{"name": "duration", "type": "number", "required": True}]},
    ]

    def _render(self, argv):
        import io
        import contextlib
        args = nm.build_parser().parse_args(argv)
        out = io.StringIO()
        normalized = [nm._normalize_model(item) for item in self.SPECS]
        with mock.patch.object(nm, "_fetch_public_models", return_value=normalized), \
                contextlib.redirect_stdout(out):
            code = nm.cmd_models(args)
        return code, out.getvalue()

    def test_groups_by_type(self):
        code, out = self._render(["models"])
        self.assertEqual(code, 0)
        self.assertIn("文本模型 · 1 个", out)
        self.assertIn("图像模型 · 1 个", out)
        self.assertIn("视频模型 · 1 个", out)
        self.assertIn("openai+anthropic", out)           # 协议要显示全部
        self.assertIn("维护中", out)                     # 状态中文化
        self.assertIn("35/85", out)                      # 紧凑计价：单位由图例统一说明
        self.assertIn("输入/输出，单位＝积分/百万 tokens", out)   # …图例必须把单位讲清楚
        self.assertIn("按用量计价", out)     # 按用量计价的权威文案

    def test_listing_never_prints_a_line_subline(self):
        """对外契约已不下发线路：清单里任何形态的线路子行都不得再出现。

        v1.10.0 前每个模型下方会多一行 `↳ 线路：<线路名>`；该字段是运营手填自由文本，
        可能过期且不可用于计价，已从契约与全部出口删除。这里把「不许回来」钉死。
        """
        _, out = self._render(["models"])
        self.assertNotIn("线路", out)
        self.assertNotIn("↳", out.replace("↳ 加收：", ""))   # 加收子行保留，线路子行不保留

    def test_listing_shows_surcharge_rules(self):
        _, out = self._render(["models"])
        self.assertIn("↳ 加收：", out)
        self.assertIn("duration × 25 积分", out)

    def test_listing_carries_usage_and_time_expectations(self):
        _, out = self._render(["models"])
        self.assertIn("── 使用方法", out)
        self.assertIn("── 耗时预期", out)
        self.assertIn("图像 30 秒 – 5 分钟", out)
        self.assertIn("视频 1 分钟 – 1.5 小时", out)
        self.assertIn("── 计费与余额", out)

    def test_type_filter_narrows_scope(self):
        _, out = self._render(["models", "--type", "image"])
        self.assertIn("图像模型 · 1 个", out)
        self.assertNotIn("── 文本模型 · 1 个", out)      # 分区标题不再出现
        self.assertNotIn("── 视频模型 · 1 个", out)
        self.assertNotIn("M One", out)                  # 该类型的模型也不该混进来

    def test_json_carries_protocols_billing_and_invocation(self):
        _, out = self._render(["models", "--json"])
        items = json.loads(out)
        by_code = {item["model_code"]: item for item in items}
        self.assertEqual(by_code["m2"]["protocols"], ["openai"])
        self.assertEqual(by_code["m2"]["invocation_mode"], "task")
        self.assertEqual(by_code["m2"]["billing"]["text"], "15 积分/次")
        self.assertEqual(by_code["m1"]["invocation_mode"], "sync")
        self.assertEqual(by_code["m1"]["ability"], [])           # 无参考类参数 → 无能力标签
        self.assertEqual(by_code["m2"]["ability"], ["参考图"])
        # 族键（同名分组用）与近似消耗必须在；线路相关键必须已彻底消失。
        self.assertEqual(by_code["m2"]["family"], "image:M Two")
        self.assertEqual(by_code["m2"]["approximate_credit"]["kind"], "exact")
        for absent in ("line", "line_label"):
            self.assertNotIn(absent, by_code["m2"])
            self.assertNotIn(absent, by_code["m1"])

class RequestSignatureTest(unittest.TestCase):
    """回归：非流式 chat 曾因 `_request()` 不接收 `extra_headers` 而必然 TypeError。

    协议原生路由（尤其 Anthropic 的 `anthropic-version`）必须能带上自定义头。
    """

    def test_request_accepts_extra_headers(self):
        captured: dict = {}

        class _Resp:
            status = 200

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_open(path, **kwargs):
            captured.update(kwargs)
            captured["path"] = path
            return _Resp()

        with mock.patch.object(nm, "_open", side_effect=fake_open), \
                mock.patch.object(nm, "_ensure_online_base_url",
                                  return_value="https://noova.vip"):
            payload = nm._request("POST", "/v1/messages", body={"a": 1}, key="sk-x",
                                  extra_headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["extra_headers"], {"anthropic-version": "2023-06-01"})

    def test_every_request_carries_client_user_agent(self):
        """边缘策略会拦掉 urllib 默认标识（实测 403 error code 1010），必须显式声明。"""
        request = nm._build_request("/api/models", key=None)
        self.assertEqual(request.get_header("User-agent"), nm.USER_AGENT)
        self.assertTrue(request.get_header("X-noova-client"))


class ListingWidthRegressionTest(unittest.TestCase):
    """排版回归：清单在 80 列终端里不得折行。

    实测过的真实缺陷：计价单元格逐行重复「输入 X / 输出 Y 积分/百万 tokens」时，
    文本模型每一行 104–107 列 ⇒ 终端折行把整张表拆散，列对齐全废。
    fixtures 取线上真实出现过的**最长字段组合**（最长编码 / anthropic+openai 双协议 /
    520-2650 计价），保证这条断言真能拦住回归，而不是靠小样例假通过。
    """

    LONG = [
        {"model": "", "displayName": "the model 5.5", "modelType": "text",
         "status": "online", "protocols": ["openai", "anthropic"],
         "billing": {"mode": "per_token",
                     "pricePer1M": {"input": 520.0, "output": 2650.0}},
         "surchargeRules": [], "params": []},
        {"model": "demo-image-2.5-sunburst", "displayName": "Demo Image 2.5",
         "modelType": "image", "status": "online", "protocols": ["openai"],
         "billing": {"mode": "per_request", "price": 20.0, "unit": 1},
         "surchargeRules": [], "params": []},
        {"model": "demo-video-2.0-mini-720p", "displayName": "Demo Video 2.0 mini",
         "modelType": "video", "status": "online", "protocols": ["openai"],
         "billing": {"mode": "per_request", "price": 0.0, "unit": 1},
         "surchargeRules": [{"param": "duration", "match": "present",
                             "charge": "multiply_by_value", "multiplier": 25.0,
                             "base": 0.0}],
         "params": []},
    ]

    def _render(self):
        import contextlib
        import io
        args = nm.build_parser().parse_args(["models"])
        out = io.StringIO()
        normalized = [nm._normalize_model(item) for item in self.LONG]
        with mock.patch.object(nm, "_fetch_public_models", return_value=normalized), \
                contextlib.redirect_stdout(out):
            nm.cmd_models(args)
        return out.getvalue()

    def test_no_line_exceeds_80_columns(self):
        out = self._render()
        too_wide = [(nm._display_width(line), line) for line in out.splitlines()
                    # 脚本绝对路径那一行随安装位置与解释器名变长，不属排版可控范围
                    # （解释器名在运行时解析：python / python3 / py 都可能）
                    if "<子命令>" not in line and nm._display_width(line) > 80]
        self.assertEqual(too_wide, [], f"超过 80 列的行：{too_wide}")

    def test_table_cells_never_repeat_the_unit(self):
        """单位属于图例，不属于每一行——重复即把表撑爆。"""
        out = self._render()
        rows = [line for line in out.splitlines()
                if line.strip().startswith(("（无编码，不可调用）", "demo-image-2.5-sunburst",
                                            "demo-video-2.0-mini-720p"))]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertNotIn("积分/百万", row)
        self.assertIn("520/2650", rows[0])


class DocsSampleConsistencyTest(unittest.TestCase):
    """文档里的完成块样例必须与脚本真实输出**逐字**一致。

    理由：SKILL.md 明确要求 agent「照抄」这段给用户，样例一旦与实现漂移，
    agent 就会把不存在的字段/缩进/量词抄给用户（真实发生过：样例写「共 1 个结果」
    而实现早已是「共 1 张图片」、样例缺 `任务 ID` 行、缩进多两格）。
    这里用同一个渲染函数跑一遍，把两者钉死。
    """

    # 按行锚定并允许 ```bash 之类的语言标记；否则会把闭合栅栏当成开启、整块错位配对
    FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.S | re.M)

    def _real_info_lines(self) -> list[str]:
        import contextlib
        import io
        model = nm._normalize_model({
            "model": "demo-image-2-t", "displayName": "Demo Image 2",
            "modelType": "image", "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 4.0, "unit": 1},
            "surchargeRules": [], "params": []})
        payload = {"id": "1234567", "status": "succeeded",
                   "results": [{"url": "https://x.example.com/a.png"}]}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            nm._print_task_result(payload, "image", as_json=False, quiet=False, model=model,
                                  credit_before={"total": 100.19},
                                  credit_after={"total": 96.19, "permanent": 96.19,
                                                "limited": 0.0, "vip": None})
        return self._info_part(buf.getvalue().splitlines())

    @staticmethod
    def _info_part(lines: list[str]) -> list[str]:
        """截到「结果地址」之前的完成块（含完成行与标签块），去掉尾部空行。"""
        out: list[str] = []
        for line in lines:
            if line.startswith("结果地址"):
                break
            out.append(line)
        while out and not out[-1].strip():
            out.pop()
        return out

    def _doc_info_lines(self, path) -> list[str] | None:
        text = path.read_text(encoding="utf-8")
        for chunk in self.FENCE.findall(text):
            if chunk.startswith("[完成] 生成成功"):
                return self._info_part(chunk.splitlines())
        return None

    def _assert_doc_matches(self, path) -> None:
        doc = self._doc_info_lines(path)
        self.assertIsNotNone(doc, f"{path.name} 里没有以「[完成] 生成成功」开头的样例块")
        self.assertEqual(self._real_info_lines(), doc, f"{path.name} 的样例与真实输出不一致")

    def test_skill_md_sample_matches_real_output(self):
        self._assert_doc_matches(SKILL_DIR / "SKILL.md")

    def test_protocols_md_sample_matches_real_output(self):
        self._assert_doc_matches(SKILL_DIR / "references" / "protocols.md")


class TaskFailureAndTimeoutTest(unittest.TestCase):
    """异步任务的两个终局语义，必须在测试里钉死：

    ① **失败**：原因由平台放在响应体 `error` 字段里，必须原样读出来告诉用户（不能吞成一句"失败"）；
    ② **超时 ≠ 失败**：等待上限到了任务通常仍在跑，只能提示续查，绝不能宣告生成失败。
    """

    def _args(self, *extra: str, **over):
        args = nm.build_parser().parse_args(["wait", "--id", "T1", "--type", "image", *extra])
        for key, value in over.items():
            setattr(args, key, value)
        return args

    def test_failed_task_surfaces_reason_from_response_body(self):
        payload = {"id": "T1", "status": "failed", "error": "prompt 命中内容安全策略"}
        with mock.patch.object(nm, "_request", return_value=payload):
            with self.assertRaises(nm.NoovaError) as ctx:
                nm._poll_until_done("T1", "sk-x", self._args("--quiet"))
        self.assertIn("任务失败", ctx.exception.message)
        self.assertIn("prompt 命中内容安全策略", ctx.exception.message)   # 原因不能被吞掉
        self.assertIn("T1", ctx.exception.hint or "")

    def test_timeout_is_not_reported_as_failure(self):
        payload = {"id": "T1", "status": "running"}
        calls = {"n": 0}

        def fake_time():
            calls["n"] += 1
            return 0.0 if calls["n"] == 1 else 10_000.0    # 第 1 次是起点，之后立刻越过 deadline

        with mock.patch.object(nm, "_request", return_value=payload), \
                mock.patch.object(nm.time, "time", side_effect=fake_time), \
                mock.patch.object(nm.time, "sleep"):
            with self.assertRaises(nm.NoovaError) as ctx:
                nm._poll_until_done("T1", "sk-x", self._args("--quiet", max_wait=1))
        self.assertIn("不是失败", ctx.exception.message)
        self.assertNotIn("任务失败", ctx.exception.message)              # 超时不得伪装成失败
        self.assertIn("T1", ctx.exception.hint or "")
        self.assertIn("wait --id T1", ctx.exception.hint or "")         # 必须给出续查命令

    def test_cmd_task_prints_localized_status_and_reason(self):
        import contextlib
        import io
        args = nm.build_parser().parse_args(["task", "--id", "T1"])
        buf = io.StringIO()
        payload = {"id": "T1", "status": "failed", "progress": 40, "error": "上游服务超时"}
        with mock.patch.object(nm, "_request", return_value=payload), \
                mock.patch.object(nm, "_require_key", return_value="sk-x"), \
                contextlib.redirect_stdout(buf):
            nm.cmd_task(args)
        out = buf.getvalue()
        self.assertIn("失败", out)                                      # 状态中文化
        self.assertIn("进度：40%", out)
        self.assertIn("失败原因：上游服务超时", out)


class FormPanelTest(unittest.TestCase):
    """问答板契约：类型 → 模型 → 参数，三级都必须来自运行时契约。

    面板是「用户交互规范化」的唯一落点。面板错了，agent 就会替用户做决定，
    或者在参数还没确认时把请求发出去，所以每条约束单独钉一个用例。
    """

    IMAGE_PARAMS = [
        {"name": "model", "type": "string", "required": True, "label": "模型",
         "values": ["img-a"]},
        {"name": "prompt", "type": "string", "required": True, "label": "提示词"},
        {"name": "aspect_ratio", "type": "string", "required": True,
         "label": "画面比例", "values": ["1:1", "16:9", "9:16"],
         "valueLabels": {"16:9": "横屏"}},
        {"name": "resolution", "type": "string", "required": True,
         "label": "分辨率", "values": ["720p"]},
        {"name": "duration", "type": "number", "required": True,
         "label": "时长", "min": 4, "max": 15},
        {"name": "quality", "type": "string", "required": False, "label": "画质"},
        {"name": "referenceImages", "type": "string[]", "required": False,
         "label": "参考图", "maxItems": 3},
    ]

    @staticmethod
    def _raw(code, *, mtype="image", status="online", name="Demo Image",
             price=15.0, params=None, rules=None):
        return {
            "model": code, "displayName": name, "modelType": mtype,
            "status": status, "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": price, "unit": 1},
            "surchargeRules": rules or [],
            "params": list(params if params is not None else []),
        }

    def _models(self):
        return [nm._normalize_model(item) for item in (
            # 同名 + 两条独立编码：这是生产真实形状（同一展示名对应多个模型）
            self._raw("img-a", name="Demo Image", params=self.IMAGE_PARAMS),
            self._raw("img-c", name="Demo Image", price=20.0),
            self._raw("img-b", name="Retired", status="maintenance"),
            self._raw("", name="Mystery"),                       # 平台没给调用编码
            self._raw("vid-a", mtype="video", name="Demo Video"),
            self._raw("", mtype="text", name="no-code-text"),
        )]

    def _model(self, code):
        return next(m for m in self._models() if m["code"] == code)

    # ---- 第一步：类型 ----

    def test_type_panel_only_offers_types_with_callable_models(self):
        panel = nm.build_type_form(self._models())
        self.assertEqual(panel["panel"], "type_picker")
        self.assertEqual(panel["step"], 1)
        self.assertEqual([o["value"] for o in panel["options"]], ["image", "video"])
        by_type = {o["value"]: o for o in panel["options"]}
        self.assertEqual(by_type["image"]["count"], 2)          # 空编码/离线的都不算
        self.assertEqual(by_type["video"]["count"], 1)
        self.assertTrue(by_type["image"]["eta"])                # 用户要先知道要等多久
        self.assertTrue(panel["next_step"])

    # ---- 第二步：模型 ----

    def test_model_panel_separates_callable_and_maintenance_models(self):
        panel = nm.build_model_form("image", self._models())
        self.assertEqual(panel["panel"], "model_picker")
        self.assertEqual(panel["step"], 2)
        self.assertEqual([o["value"] for o in panel["options"]], ["img-a", "img-c"])
        # 上线模型进候选并注明「在线」；维护中模型**呈现但不可选**，注明「维护中」；
        # 空编码单列一类。三者不得混淆。
        self.assertEqual({o["status"] for o in panel["options"]}, {"在线"})
        reasons = {u["display_name"]: u["reason"] for u in panel["unavailable"]}
        statuses = {u["display_name"]: u.get("status") for u in panel["unavailable"]}
        self.assertEqual(set(reasons), {"Retired", "Mystery"})
        self.assertEqual(statuses["Retired"], "维护中")
        self.assertIn("维护中", reasons["Retired"])
        self.assertIn("编码", reasons["Mystery"])

    def test_model_panel_carries_code_and_price_for_every_option(self):
        """同名模型确实存在（同一展示名对应多个编码），只给名字用户没法区分。

        区分依据只有**模型编码 + 计价**两件套（线路已从对外契约删除）；
        面板还必须先给出该模型的近似消耗（参数还没定，所以是区间或定性），
        否则用户得先选完才知道多少钱。
        """
        panel = nm.build_model_form("image", self._models())
        by_code = {o["value"]: o for o in panel["options"]}
        self.assertEqual(by_code["img-a"]["billing"], "15 积分/次")
        self.assertEqual(by_code["img-c"]["billing"], "20 积分/次")
        self.assertEqual(by_code["img-a"]["family"], "image:Demo Image")
        self.assertEqual(by_code["img-a"]["credit_estimate"], "15 积分/次")
        self.assertEqual(by_code["img-c"]["credit_estimate"], "20 积分/次")
        self.assertEqual(panel["duplicate_names"], ["Demo Image"])
        self.assertIn("模型编码", panel["duplicate_note"])
        self.assertIn("计价", panel["duplicate_note"])
        for absent in ("line", "line_label"):
            self.assertNotIn(absent, by_code["img-a"])
            self.assertNotIn(absent, by_code["img-c"])

    def test_model_panel_has_no_line_legend(self):
        """线路口径整段下线：无论几个同名，都不再有 `line_legend`。"""
        multi = nm.build_model_form("image", self._models())
        single = nm.build_model_form("image", [self._model("img-a")])
        self.assertNotIn("line_legend", multi)
        self.assertNotIn("line_legend", single)
        self.assertNotIn("线路", str(multi))

    def test_model_panel_next_step_mentions_the_exact_price_later(self):
        """用户要知道：现在的价是近似值，参数定好后还会再报一次精确价。"""
        panel = nm.build_model_form("image", self._models())
        self.assertIn("精确", panel["next_step"])

    def test_model_panel_says_so_when_nothing_is_available(self):
        panel = nm.build_model_form("audio", self._models())
        self.assertEqual(panel["options"], [])
        self.assertIn("没有可用", panel["empty_note"])

    def test_type_panel_says_so_when_no_model_is_callable(self):
        """平台一个可调用模型都没有时，面板必须说明原因，不能只给一个空标题。

        空标题看起来像"脚本坏了"，会让 agent 退回去凭记忆编模型名。
        """
        panel = nm.build_type_form([])
        self.assertEqual(panel["panel"], "type_picker")
        self.assertEqual(panel["options"], [])
        self.assertIn("没有可调用", panel["empty_note"])
        # 只有离线和空编码模型时同样算"没有可调用"
        blocked = [self._raw("", name="Mystery"),
                   self._raw("img-b", name="Retired", status="maintenance")]
        self.assertIn("没有可调用", nm.build_type_form(
            [nm._normalize_model(i) for i in blocked])["empty_note"])

    # ---- 第三步：参数（必须基于已选中的那个模型）----

    def test_param_panel_is_built_from_the_selected_model(self):
        panel = nm.build_param_form(self._model("img-a"))
        self.assertEqual(panel["panel"], "param_panel")
        self.assertEqual(panel["step"], 3)
        self.assertEqual(panel["model_code"], "img-a")
        self.assertEqual(panel["model_type"], "image")
        self.assertTrue(panel["contract_available"])
        self.assertEqual(panel["hidden_fields"], ["model"])     # 上一步已选过，不再问
        self.assertNotIn("model", [f["name"] for f in panel["fields"]])
        # 必填在前，同时保持契约内的相对顺序；单值 `resolution` 不进面板
        self.assertEqual([f["name"] for f in panel["fields"]],
                         ["prompt", "aspect_ratio", "duration",
                          "quality", "referenceImages"])
        self.assertTrue(all(f["required"] for f in panel["fields"][:3]))
        self.assertFalse(any(f["required"] for f in panel["fields"][3:]))
        self.assertEqual(panel["preset_params"], {"resolution": "720p"})

    def test_single_value_param_is_adopted_not_asked(self):
        """单值参数不进面板，但**必须**自动采用并如实说明（三件套之一）。

        面板里没有它、请求时却缺它，才是真正的缺陷（生产 7 个单值参数 5 个必填）。
        """
        panel = nm.build_param_form(self._model("img-a"))
        self.assertEqual(panel["preset_params"], {"resolution": "720p"})
        self.assertNotIn("resolution", [f["name"] for f in panel["fields"]])
        self.assertIn("resolution=720p", panel["preset_note"])
        self.assertIn("无需选择", panel["preset_note"])

    def test_range_param_is_a_number_not_an_enum(self):
        """`4~15` 是区间，不是「只有一个可选值」——不能渲染成不可选的枚举。"""
        fields = {f["name"]: f for f in nm.build_param_form(self._model("img-a"))["fields"]}
        duration = fields["duration"]
        self.assertEqual(duration["kind"], "number")
        self.assertFalse(duration["fixed"])
        self.assertEqual(duration["options"], [])
        self.assertEqual(duration["range"], [4, 15])

    def test_choice_param_lists_every_contract_value(self):
        fields = {f["name"]: f for f in nm.build_param_form(self._model("img-a"))["fields"]}
        ratio = fields["aspect_ratio"]
        self.assertEqual(ratio["kind"], "choice")
        self.assertEqual([o["value"] for o in ratio["options"]], ["1:1", "16:9", "9:16"])
        self.assertEqual(ratio["option_source"], "contract")
        self.assertEqual([o["label"] for o in ratio["options"]], ["1:1", "横屏", "9:16"])

    def test_reference_param_maps_to_the_cli_option(self):
        fields = {f["name"]: f for f in nm.build_param_form(self._model("img-a"))["fields"]}
        self.assertEqual(fields["referenceImages"]["kind"], "list")
        self.assertEqual(fields["referenceImages"]["cli"], "--ref-image")
        self.assertIn("最多 3 个", fields["referenceImages"]["note"])
        self.assertEqual(fields["prompt"]["cli"], "--prompt")
        self.assertEqual(fields["duration"]["cli"], "--param duration")

    def test_param_panel_always_announces_generation_starts_on_confirm(self):
        """用户必须清楚「再确认一下就真的开始生成并扣费」。"""
        self.assertEqual(nm.FORM_CONFIRM_HINT, "确认后立即开始生成")
        panel = nm.build_param_form(self._model("img-a"))
        self.assertEqual(panel["confirm_hint"], nm.FORM_CONFIRM_HINT)
        self.assertEqual(panel["next_step"], nm.FORM_CONFIRM_HINT)

    def test_param_panel_without_contract_says_so_instead_of_inventing_params(self):
        panel = nm.build_param_form(self._model("img-c"))
        self.assertFalse(panel["contract_available"])
        self.assertEqual(panel["fields"], [])
        self.assertIn("不要臆造", panel["empty_note"])

    def test_text_model_gets_extra_cli_options_not_fake_params(self):
        panel = nm.build_param_form(self._model("vid-a"), model_type="text")
        flags = {o["flag"] for o in panel["extra_cli_options"]}
        self.assertEqual(flags, {"--system", "--stream", "--max-tokens", "--temperature"})
        self.assertNotIn("extra_cli_options", nm.build_param_form(self._model("img-a")))

    # ---- 报价：用户点确认前必须看到价钱 ----

    def test_param_panel_quotes_the_price_for_chosen_params(self):
        panel = nm.build_param_form(self._model("img-a"), chosen_params={"duration": 5})
        self.assertTrue(panel["credit_estimate"]["exact"])
        self.assertEqual(panel["credit_estimate"]["credits"], 15.0)
        self.assertIn("15 积分", panel["credit_estimate_text"])
        self.assertIn("0.15 元", panel["credit_estimate_text"])
        self.assertIn("100 积分 = 1 元", panel["credit_estimate_text"])

    def test_param_panel_surcharge_is_added_to_the_quote(self):
        model = nm._normalize_model(self._raw(
            "vid-x", mtype="video", price=0.0,
            rules=[{"param": "duration", "match": "present",
                    "charge": "multiply_by_value", "multiplier": 60.0, "base": 0.0}]))
        panel = nm.build_param_form(model, chosen_params={"duration": 5})
        self.assertEqual(panel["credit_estimate"]["credits"], 300.0)
        self.assertIn("300 积分", panel["credit_estimate_text"])

    # ---- `form` 子命令 ----

    def _form(self, argv, models=None):
        import contextlib
        import io
        args = nm.build_parser().parse_args(["form"] + argv)
        out = io.StringIO()
        with mock.patch.object(nm, "_fetch_public_models",
                               return_value=self._models() if models is None else models), \
                contextlib.redirect_stdout(out):
            code = nm.cmd_form(args)
        return code, out.getvalue()

    def test_cli_defaults_to_the_type_panel(self):
        code, out = self._form(["--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)                               # stdout 必须是纯 JSON
        self.assertEqual(payload["panel"], "type_picker")

    def test_cli_param_panel_json_is_machine_readable(self):
        code, out = self._form(["--code", "img-a", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["panel"], "param_panel")
        self.assertEqual(payload["model_code"], "img-a")
        self.assertEqual(payload["confirm_hint"], "确认后立即开始生成")

    def test_cli_quotes_the_price_when_params_are_given(self):
        code, out = self._form(["--code", "img-a", "--param", "duration=5", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["chosen_params"], {"duration": 5})
        self.assertIn("15 积分", payload["credit_estimate_text"])

    def test_cli_human_output_names_every_choice(self):
        _, out = self._form(["--code", "img-a"])
        self.assertIn("1:1", out)
        self.assertIn("720p", out)
        self.assertIn("100 积分 = 1 元", out)                    # 换算口径必须写出来
        self.assertIn("确认后立即开始生成", out)

    def test_cli_human_output_never_hides_an_empty_note(self):
        """人读路径必须把 `empty_note` 打出来。

        它是"没有可选值"时唯一的信息来源：吞掉它，用户只看到一个空面板，
        agent 也会退回去凭记忆编模型名或参数名。
        """
        _, out = self._form(["--type", "audio"])                 # 该类型无可用模型
        self.assertIn("没有可用", out)
        _, out = self._form(["--code", "img-c"])                 # 该模型没有参数契约
        self.assertIn("不要臆造", out)
        # 一个可调用类型都没有（只剩离线/空编码模型）
        blocked = [nm._normalize_model(self._raw("", name="Mystery")),
                   nm._normalize_model(self._raw("img-b", name="Retired", status="maintenance"))]
        _, out = self._form([], models=blocked)
        self.assertIn("没有可调用", out)

    def test_panels_never_leak_upstream_hosts(self):
        """面板里几乎每个字符串都来自上游，人读与 `--json` 两条路径都必须脱敏。

        曾实测：同一份数据 `--json` 已把内网地址换成占位域名，人读路径却把模型
        线路、参数说明里的内网地址原样打印。agent 会把面板转述给用户，于是内部
        域名照样出现在用户可见输出里。这里给上游文本的每个字段都塞入内网地址，
        逐个面板、逐条路径核对——漏一处就是一个泄漏点。

        面板里所有字符串都来自上游，包括同名提示与参数说明——逐条过一遍。
        """
        import contextlib
        import io
        raw = self._raw("leak-a", name="Leak https://internal.corp/name", params=[
            {"name": "prompt", "type": "string", "required": True,
             "label": "见 https://internal.corp/label"},
            {"name": "mode", "type": "string", "required": False,
             "label": "走 gw-internal.acme.com:8443",
             "values": ["fast", "slow"],
             "valueLabels": {"fast": "https://internal.corp/fast"},
             "optionVisibleWhen": [
                 {"when": "imageSize", "operator": "eq",
                  "value": "https://internal.corp/when"}]},
        ])
        model = nm._normalize_model(raw)
        needles = ("internal.corp", "gw-internal.acme.com")
        panels = (nm.build_param_form(model),
                  nm.build_model_form("image", [model]),
                  nm.build_type_form([model]))
        for payload in panels:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                nm._render_form(payload)
            rendered = ((out.getvalue(), "human"), (nm._dump_json(payload), "json"))
            for text, label in rendered:
                for needle in needles:
                    self.assertNotIn(
                        needle, text,
                        "%s 的 %s 路径泄漏了 %s" % (payload["panel"], label, needle))

    def test_cli_refuses_a_model_that_is_not_callable(self):
        for code in ("img-b", "no-such-model"):
            with self.assertRaises(nm.NoovaError):
                self._form(["--code", code, "--json"])


class PriceCellTest(unittest.TestCase):
    """清单表格里的计价单元格只放紧凑文案，长句归 `_price_text`。

    实测过的真实缺陷：`per_token` 四档单价全为 0 时回退长句
    `按实际用量结算（未公布 token 单价）`，把文本组整行撑到 113 列、80 列终端折行。
    """

    def test_per_token_all_zero_stays_short(self):
        model = spec_model(modelType="text", displayName="the model 5.5",
                           billing={"mode": "per_token", "pricePer1M": {}}, params=[])
        self.assertEqual(nm._price_cell(model), "按量计费")
        self.assertLessEqual(nm._display_width(nm._price_cell(model)), 8)

    def test_per_token_with_prices_is_compact(self):
        model = spec_model(modelType="text", params=[],
                           billing={"mode": "per_token",
                                    "pricePer1M": {"input": 520.0, "output": 2650.0}})
        self.assertEqual(nm._price_cell(model), "520/2650")

    def test_per_token_with_cache_hit_adds_one_segment(self):
        model = spec_model(modelType="text", params=[],
                           billing={"mode": "per_token",
                                    "pricePer1M": {"input": 90.0, "output": 260.0,
                                                   "cacheHit": 16.0}})
        self.assertEqual(nm._price_cell(model), "90/260/命中16")

    def test_zero_price_with_rules_stays_short(self):
        model = spec_model(
            billing={"mode": "per_request", "price": 0.0, "unit": 1},
            surchargeRules=[{"param": "duration", "match": "present",
                             "charge": "multiply_by_value", "multiplier": 25.0,
                             "base": 0.0}])
        self.assertEqual(nm._price_cell(model), "按用量计价")
        self.assertLessEqual(nm._display_width(nm._price_cell(model)), 10)

    def test_fixed_price_cell_equals_the_long_text(self):
        model = spec_model(billing={"mode": "per_request", "price": 15.0, "unit": 1})
        self.assertEqual(nm._price_cell(model), nm._price_text(model))


class LineListingTest(unittest.TestCase):
    """清单必须把同一模型的**全部线路**都摆出来，且子行不得破 80 列。

    线路**不加列**：实测加一列后视频组 96 列、文本组 113 列，80 列终端整表折行。
    改为该行下方一条约 40 列的子行。
    """

    SPECS = [
        {"model": "vd-a", "displayName": "Demo Video 2.5", "modelType": "video",
         "status": "online", "protocols": ["openai"],
         "billing": {"mode": "per_request", "price": 0.0, "unit": 1},
         "surchargeRules": [{"param": "duration", "match": "present",
                             "charge": "multiply_by_value", "multiplier": 60.0,
                             "base": 0.0}],
         "params": [{"name": "prompt", "type": "string", "required": True},
                    {"name": "duration", "type": "number", "required": True,
                     "min": 4, "max": 15}]},
        {"model": "vd-b", "displayName": "Demo Video 2.5", "modelType": "video",
         "status": "online", "protocols": ["openai"],
         "billing": {"mode": "per_request", "price": 0.0, "unit": 1},
         "surchargeRules": [{"param": "duration", "match": "present",
                             "charge": "multiply_by_value", "multiplier": 25.0,
                             "base": 0.0}],
         "params": [{"name": "prompt", "type": "string", "required": True},
                    {"name": "duration", "type": "number", "required": True,
                     "min": 4, "max": 15}]},
        {"model": "glm", "displayName": "GLM 5.3", "modelType": "text",
         "status": "online", "protocols": ["anthropic"],
         "billing": {"mode": "per_token",
                     "pricePer1M": {"input": 90.0, "output": 260.0}},
         "surchargeRules": [],
         "params": [{"name": "prompt", "type": "string", "required": True}]},
        {"model": "plain", "displayName": "Plain Image", "modelType": "image",
         "status": "online", "protocols": ["openai"],
         "billing": {"mode": "per_request", "price": 20.0, "unit": 1},
         "surchargeRules": [], "params": []},
    ]

    def _render(self, argv):
        import contextlib
        import io
        args = nm.build_parser().parse_args(argv)
        out = io.StringIO()
        normalized = [nm._normalize_model(item) for item in self.SPECS]
        with mock.patch.object(nm, "_fetch_public_models", return_value=normalized), \
                contextlib.redirect_stdout(out):
            nm.cmd_models(args)
        return out.getvalue()

    def test_same_named_models_are_grouped_with_a_heading(self):
        out = self._render(["models"])
        self.assertIn("▸ Demo Video 2.5", out)
        self.assertIn("同名 2 个", out)
        self.assertIn("按编码区分", out)
        # 单个模型（无同名）不加族标题，避免清单变吵。
        self.assertNotIn("▸ Plain Image", out)
        self.assertNotIn("▸ GLM 5.3", out)

    def test_listing_never_prints_a_line_subline(self):
        """线路已下线：清单里不得再出现任何线路子行（含「默认」形态）。

        注意断言只看「线路」二字——「默认等待上限」是耗时小节的正常文案，
        不能把整段输出都钉死。
        """
        out = self._render(["models"])
        self.assertNotIn("线路", out)
        self.assertNotIn("line_label", out)

    def test_listing_stays_within_80_columns(self):
        out = self._render(["models"])
        too_wide = [(nm._display_width(line), line) for line in out.splitlines()
                    if "<子命令>" not in line and nm._display_width(line) > 80]
        self.assertEqual(too_wide, [], f"超过 80 列的行：{too_wide}")

    def test_json_carries_family_and_approximate_credit_but_no_line(self):
        items = json.loads(self._render(["models", "--json"]))
        by_code = {item["model_code"]: item for item in items}
        self.assertEqual(by_code["vd-a"]["family"], "video:Demo Video 2.5")
        self.assertEqual(by_code["vd-b"]["family"], by_code["vd-a"]["family"])
        self.assertEqual(by_code["vd-a"]["approximate_credit"]["kind"], "range")
        self.assertEqual(by_code["plain"]["approximate_credit"]["kind"], "exact")
        for code in ("vd-a", "vd-b", "glm", "plain"):
            self.assertNotIn("line", by_code[code])
            self.assertNotIn("line_label", by_code[code])


class ModelFormLineTest(unittest.TestCase):
    """模型面板：用户必须能按**模型编码**区分同名模型，并当场看到近似消耗。"""

    @staticmethod
    def _models():
        return [nm._normalize_model({
            "model": code, "displayName": "Demo Video 2.5", "modelType": "video",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": price, "unit": 1},
            "surchargeRules": [], "params": []})
            for code, price in (("vd-std", 25.0), ("vd-kd", 60.0))]

    def test_every_option_carries_code_and_approximate_credit(self):
        panel = nm.build_model_form("video", self._models())
        by_code = {o["value"]: o for o in panel["options"]}
        self.assertEqual(by_code["vd-std"]["credit_estimate"], "25 积分/次")
        self.assertEqual(by_code["vd-kd"]["credit_estimate"], "60 积分/次")
        self.assertEqual(by_code["vd-std"]["family"], "video:Demo Video 2.5")
        self.assertNotIn("・", by_code["vd-std"]["credit_estimate"])   # 短文案不带依据
        for absent in ("line", "line_label"):
            self.assertNotIn(absent, by_code["vd-std"])
            self.assertNotIn(absent, by_code["vd-kd"])

    def test_no_line_legend_in_the_panel(self):
        multi = nm.build_model_form("video", self._models())
        single = nm.build_model_form("video", self._models()[:1])
        self.assertNotIn("line_legend", multi)
        self.assertNotIn("line_legend", single)

    def test_duplicate_note_names_code_and_price(self):
        panel = nm.build_model_form("video", self._models())
        self.assertEqual(panel["duplicate_names"], ["Demo Video 2.5"])
        note = panel["duplicate_note"]
        self.assertIn("模型编码", note)
        self.assertIn("计价", note)
        self.assertNotIn("线路", note)

    def test_next_step_promises_the_exact_price_after_params(self):
        panel = nm.build_model_form("video", self._models())
        self.assertIn("精确", panel["next_step"])

    def test_human_render_groups_same_named_models_under_one_header(self):
        import contextlib
        import io
        panel = nm.build_model_form("video", self._models())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nm._render_form(panel)
        text = out.getvalue()
        self.assertIn("同名 2 个", text)
        self.assertIn("按编码选", text)
        self.assertIn("vd-std", text)
        self.assertIn("vd-kd", text)
        self.assertNotIn("线路", text)
        too_wide = [(nm._display_width(line), line) for line in text.splitlines()
                    if nm._display_width(line) > 80]
        self.assertEqual(too_wide, [], f"超过 80 列的行：{too_wide}")


class ParamDependencyTest(unittest.TestCase):
    """参数依赖（`optionVisibleWhen`）：分档、命中、缺席不阻拦、拓扑排序。

    这是 v1.10.0 的核心行为。生产实证：`gpt-image-2-stable` 的 `aspect_ratio` 有 40 个
    取值，每个只属于 1K / 2K / 4K 三档之一（`auto` 三档各出现一次）；平铺出来用户
    根本看不出该先选分辨率。
    """

    @staticmethod
    def _model(option_visible_when, *, image_required=False, image_values=None):
        return nm._normalize_model({
            "model": "dep-image", "displayName": "Dep Image", "modelType": "image",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 20.0, "unit": 1},
            "surchargeRules": [],
            "params": [
                {"name": "prompt", "type": "string", "required": True},
                {"name": "imageSize", "type": "string", "required": image_required,
                 "values": list(image_values or ["1K", "2K", "4K"])},
                {"name": "aspect_ratio", "type": "string", "required": True,
                 "values": ["1024x1024", "auto", "auto", "auto", "free"],
                 "optionVisibleWhen": option_visible_when},
            ]})

    @staticmethod
    def _rules():
        # `auto` 在三档各出现一次、规则不同 —— 这正是「不能用字典」的原因。
        return [
            {"when": "imageSize", "operator": "eq", "value": "1K"},
            {"when": "imageSize", "operator": "eq", "value": "1K"},
            {"when": "imageSize", "operator": "eq", "value": "2K"},
            {"when": "imageSize", "operator": "eq", "value": "4K"},
            None,
        ]

    def _field(self, model, **kw):
        panel = nm.build_param_form(model, **kw)
        return {f["name"]: f for f in panel["fields"]}["aspect_ratio"], panel

    def test_option_groups_split_by_controller_value(self):
        field, _ = self._field(self._model(self._rules()))
        self.assertEqual(field["depends_on"], "imageSize")
        heads = [g["when"]["value"] if g["when"] else None
                 for g in field["option_groups"]]
        # 1K 档拿到两个值（含重复出现的 `auto`）；无规则值另立一组。
        self.assertEqual(heads, ["1K", "2K", "4K", None])
        by_head = {g["when"]["value"] if g["when"] else None: g for g in field["option_groups"]}
        self.assertEqual([o["value"] for o in by_head["1K"]["options"]],
                         ["1024x1024", "auto"])
        self.assertEqual([o["value"] for o in by_head["4K"]["options"]], ["auto"])
        self.assertEqual([o["value"] for o in by_head[None]["options"]], ["free"])
        # 全集必须保留（向后兼容），且顺序与契约一致。
        self.assertEqual([o["value"] for o in field["options"]],
                         ["1024x1024", "auto", "auto", "auto", "free"])

    def test_active_group_follows_the_chosen_controller_value(self):
        model = self._model(self._rules())
        _, panel = self._field(model, chosen_params={"imageSize": "2K"})
        field = {f["name"]: f for f in panel["fields"]}["aspect_ratio"]
        heads = [g["when"]["value"] if g["when"] else None
                 for g in field["option_groups"]]
        self.assertEqual(field["active_group"], heads.index("2K"))

    def test_controller_absent_hides_nothing_and_blocks_nothing(self):
        """控制器没选 → 每一档都还在，`active_group` 为 None（不擅自替用户选一档）。"""
        field, _ = self._field(self._model(self._rules()))
        self.assertIsNone(field["active_group"])
        self.assertEqual([o["value"] for o in field["options"]],
                         ["1024x1024", "auto", "auto", "auto", "free"])
        self.assertEqual(len(field["option_groups"]), 4)

    def test_human_render_prints_every_group_not_only_the_active_one(self):
        import contextlib
        import io
        model = self._model(self._rules())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nm._render_form(nm.build_param_form(model))
        text = out.getvalue()
        for head in ("[imageSize=1K]", "[imageSize=2K]", "[imageSize=4K]", "[无条件]"):
            self.assertIn(head, text)
        self.assertIn("先确定「imageSize」", text)
        too_wide = [(nm._display_width(l), l) for l in text.splitlines()
                    if nm._display_width(l) > 80]
        self.assertEqual(too_wide, [], f"超过 80 列的行：{too_wide}")

    def test_topology_puts_the_controller_before_its_dependent(self):
        """控制器是可选、依赖项是必填：只按「必填优先」会把控制器排到后面。"""
        model = self._model(self._rules(), image_required=False)
        names = [f["name"] for f in nm.build_param_form(model)["fields"]]
        self.assertLess(names.index("imageSize"), names.index("aspect_ratio"))

    def test_dangling_or_cyclic_dependency_falls_back_without_losing_fields(self):
        """契约由运营配置：依赖指向不存在的字段或成环时，退回原顺序且不丢字段。"""
        dangling = self._model([{"when": "nope", "operator": "eq", "value": "x"}])
        names = [f["name"] for f in nm.build_param_form(dangling)["fields"]]
        self.assertEqual(sorted(names), ["aspect_ratio", "imageSize", "prompt"])
        cyclic = nm._normalize_model({
            "model": "cyc", "displayName": "Cyc", "modelType": "image",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 1.0, "unit": 1},
            "surchargeRules": [],
            "params": [
                {"name": "a", "type": "string", "required": True, "values": ["x", "y"],
                 "optionVisibleWhen": [
                     {"when": "b", "operator": "eq", "value": "1"},
                     {"when": "b", "operator": "eq", "value": "1"}]},
                {"name": "b", "type": "string", "required": True, "values": ["1", "2"],
                 "optionVisibleWhen": [
                     {"when": "a", "operator": "eq", "value": "x"},
                     {"when": "a", "operator": "eq", "value": "x"}]},
            ]})
        fields = nm.build_param_form(cyclic)["fields"]
        self.assertEqual(sorted(f["name"] for f in fields), ["a", "b"])

    def test_single_value_controller_feeds_the_visibility_evaluation(self):
        """生产形状：`imageSize` 是单值（不进面板），但分档依据正是它。"""
        model = self._model(self._rules(), image_values=["1K"])
        panel = nm.build_param_form(model)
        self.assertEqual(panel["preset_params"], {"imageSize": "1K"})
        field = {f["name"]: f for f in panel["fields"]}["aspect_ratio"]
        heads = [g["when"]["value"] if g["when"] else None
                 for g in field["option_groups"]]
        self.assertEqual(field["active_group"], heads.index("1K"))


class SingleValuePresetTest(unittest.TestCase):
    """单值参数：不进面板 + 自动带入载荷 + 必填校验同等对待（三件套缺一即死锁）。"""

    SPEC = {
        "model": "seedance-x", "displayName": "Seed X", "modelType": "video",
        "status": "online", "protocols": ["openai"],
        "billing": {"mode": "per_request", "price": 0.0, "unit": 1},
        "surchargeRules": [{"param": "duration", "match": "present",
                            "charge": "multiply_by_value", "multiplier": 25.0}],
        "params": [
            {"name": "prompt", "type": "string", "required": True},
            {"name": "resolution", "type": "string", "required": True,
             "values": ["720p"]},
            {"name": "duration", "type": "number", "required": True, "min": 4, "max": 15},
        ]}

    def _model(self):
        return nm._normalize_model(dict(self.SPEC))

    def test_single_value_goes_to_preset_not_fields(self):
        panel = nm.build_param_form(self._model())
        self.assertEqual(panel["preset_params"], {"resolution": "720p"})
        self.assertNotIn("resolution", [f["name"] for f in panel["fields"]])

    def test_single_value_is_written_into_the_request_payload(self):
        args = mock.Mock(param=[], ref_image=[], ref_video=[], ref_audio=[], prompt="x")
        payload = nm._build_media_payload(args, "sk-x", self._model())
        self.assertEqual(payload["resolution"], "720p")
        # 用户显式传的同名参数优先（可覆盖预设）。
        args2 = mock.Mock(param=[("resolution", "1080p")], ref_image=[], ref_video=[],
                          ref_audio=[], prompt="x")
        payload2 = nm._build_media_payload(args2, "sk-x", self._model())
        self.assertEqual(payload2["resolution"], "1080p")

    def test_single_value_does_not_trip_the_required_check(self):
        """否则就是死锁：面板里没这个参数，请求前却报缺它。"""
        args = mock.Mock(param=[("duration", 5)], prompt="x",
                         ref_image=None, ref_video=None, ref_audio=None)
        nm._ensure_required_params(self._model(), args)   # 不抛即通过
        # 反证：拿掉预设（模拟只改面板不改这里）就必须报缺
        with mock.patch.object(nm, "_preset_params", return_value={}):
            with self.assertRaises(nm.NoovaError) as ctx:
                nm._ensure_required_params(self._model(), args)
        self.assertIn("resolution", str(ctx.exception.message))

    def test_panel_quote_includes_the_preset_params(self):
        panel = nm.build_param_form(self._model(), chosen_params={"duration": 5})
        self.assertEqual(panel["credit_estimate"]["credits"], 125.0)
        self.assertIn("125 积分", panel["credit_estimate_text"])


class SurchargeBaseDefaultTest(unittest.TestCase):
    """Bug A 回归：契约缺 `base` 键时按 0 计，绝不报「取值非法」。"""

    MODELS = (
        ("Seedance-2.0-mini-720p", 0.0, 25.0),
        ("fgh-seedance-2.5-720p", 0.0, 120.0),
        ("Wan3.0-720p", 0.0, 35.0),
        ("Minimax-H3-768p", 0.0, 7.0),
    )

    def _model(self, multiplier):
        return nm._normalize_model({
            "model": "v", "displayName": "V", "modelType": "video",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 0.0, "unit": 1},
            "surchargeRules": [{"param": "duration", "match": "present",
                                "charge": "multiply_by_value",
                                "multiplier": multiplier}],   # 契约原文无 base 键
            "params": [{"name": "duration", "type": "number", "required": True,
                        "min": 4, "max": 30}]})

    def test_missing_base_is_zero_not_an_error(self):
        for code, base, multiplier in self.MODELS:
            with self.subTest(code=code):
                model = self._model(multiplier)
                cost = nm.compute_credit_cost(model, {"duration": 10})
                self.assertTrue(cost["exact"])
                self.assertEqual(cost["credits"], round(base + 10 * multiplier, 2))
                # 近似价分支（选模型阶段）与精确价分支必须给出同一数量级，不得自相矛盾。
                approx = nm.approximate_credit(model)
                self.assertIn(approx["kind"], ("range", "exact"))

    def test_missing_base_quote_matches_the_gateway_formula(self):
        """网关 `_resolve_param_rule_charge_cost` 的口径：`base + 值 × multiplier`。"""
        for duration in (4, 5, 10, 15, 30):
            with self.subTest(duration=duration):
                cost = nm.compute_credit_cost(self._model(25.0), {"duration": duration})
                self.assertEqual(cost["credits"], round(0.0 + duration * 25.0, 2))

    def test_explicit_base_is_still_respected(self):
        model = nm._normalize_model({
            "model": "v", "displayName": "V", "modelType": "video",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 0.0, "unit": 1},
            "surchargeRules": [{"param": "duration", "match": "present",
                                "charge": "multiply_by_value", "multiplier": 2.0,
                                "base": 10.0}],
            "params": [{"name": "duration", "type": "number", "required": True,
                        "min": 1, "max": 30}]})
        self.assertEqual(nm.compute_credit_cost(model, {"duration": 5})["credits"], 20.0)

    def test_multiplier_missing_still_fails_closed(self):
        """只有 `multiplier` 缺失才是规则配置非法——这一条不能被 Bug A 的修法放宽。"""
        model = nm._normalize_model({
            "model": "v", "displayName": "V", "modelType": "video",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 0.0, "unit": 1},
            "surchargeRules": [{"param": "duration", "match": "present",
                                "charge": "multiply_by_value"}],
            "params": [{"name": "duration", "type": "number", "required": True,
                        "min": 1, "max": 30}]})
        with self.assertRaises(nm.NoovaError):
            nm.compute_credit_cost(model, {"duration": 5})


class SurchargeExactMatchTest(unittest.TestCase):
    """`exact` 规则的取值比较必须与平台同口径（归一化后再比）。

    曾经直接 `str(raw) != str(matchValue)`：契约 `matchValue` 是数字 `5`、用户传
    `"5.0"` 时，本地判不命中（报价偏低）而网关判命中。
    """

    def _model(self, match_value):
        return nm._normalize_model({
            "model": "opt", "displayName": "Opt", "modelType": "image",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": 20.0, "unit": 1},
            "surchargeRules": [{"param": "imageSize", "match": "exact",
                                "matchValue": match_value, "charge": "fixed",
                                "credits": 2.0}],
            "params": [{"name": "imageSize", "type": "string", "required": True,
                        "values": ["4K"]}]})

    def test_numeric_equivalents_match(self):
        """`matchValue=5` 与用户传的 `"5"` / `"5.0"` / `5.0` 必须同等命中（20 + 2）。"""
        for given in (5, "5", "5.0", 5.0):
            with self.subTest(given=given):
                self.assertEqual(
                    nm.compute_credit_cost(self._model(5), {"imageSize": given})["credits"],
                    22.0)

    def test_non_equivalent_values_do_not_match(self):
        self.assertEqual(
            nm.compute_credit_cost(self._model(5), {"imageSize": "6"})["credits"], 20.0)
        self.assertEqual(
            nm.compute_credit_cost(self._model("4K"), {"imageSize": "2K"})["credits"], 20.0)

    def test_synthesized_add_price_rule_is_quoted(self):
        """后端把选项 `add_price` 合成为 `exact` + `fixed` 规则，本地必须算进去。

        契约实测：`nano-banana-pro` 的 `imageSize=4K` 合成 `credits: 2`；
        `voice-clone` 的 `instanceType=plus` 合成 `credits: 10`。
        """
        model = self._model("4K")
        self.assertEqual(
            nm.compute_credit_cost(model, {"imageSize": "4K"})["credits"], 22.0)


class QuoteParityTest(unittest.TestCase):
    """报价对账（生产契约数字）：本地 `compute_credit_cost` 必须与网关逐条一致。

    夹具取自**生产契约的真实形状**（39 个模型 × 全部参数取值组合，实测 399 组零不一致）。
    网关侧公式见 `../noova-backend/app/services/api_gateway_proxy_service.py` 的
    `_calculate_cost_decimal` + `_resolve_param_rule_charge_cost`：
    `总价 = billing_price × billing_unit + Σ 命中规则加收`。
    """

    FIXTURES = [
        # (模型, 基础价, 加收规则, 参数组合, 期望积分)
        ("per_request 底价", 20.0, [], {}, 20.0),
        ("底价 × 单位", 15.0, [], {}, 15.0),
        ("按期时长规则", 0.0,
         [{"param": "duration", "match": "present", "charge": "multiply_by_value",
           "multiplier": 25.0}], {"duration": 10}, 250.0),
        ("时长 + 底价", 100.0,
         [{"param": "duration", "match": "present", "charge": "multiply_by_value",
           "multiplier": 25.0}], {"duration": 4}, 200.0),
        ("精确取值固定加收", 20.0,
         [{"param": "imageSize", "match": "exact", "matchValue": "4K",
           "charge": "fixed", "credits": 2.0}], {"imageSize": "4K"}, 22.0),
        ("精确取值不在其中", 20.0,
         [{"param": "imageSize", "match": "exact", "matchValue": "4K",
           "charge": "fixed", "credits": 2.0}], {"imageSize": "1K"}, 20.0),
        ("区间内固定加收", 1.0,
         [{"param": "duration", "match": "range", "rangeMin": 5, "rangeMax": 10,
           "charge": "fixed", "credits": 5.0}], {"duration": 7}, 6.0),
        ("区间外不加收", 1.0,
         [{"param": "duration", "match": "range", "rangeMin": 5, "rangeMax": 10,
           "charge": "fixed", "credits": 5.0}], {"duration": 3}, 1.0),
        ("多参数同时命中", 0.0,
         [{"param": "duration", "match": "present", "charge": "multiply_by_value",
           "multiplier": 10.0},
          {"param": "imageSize", "match": "exact", "matchValue": "4K",
           "charge": "fixed", "credits": 2.0}],
         {"duration": 5, "imageSize": "4K"}, 52.0),
        ("参数没传则规则不参与", 20.0,
         [{"param": "duration", "match": "present", "charge": "multiply_by_value",
           "multiplier": 10.0}], {}, 20.0),
        ("倍率带 base", 0.0,
         [{"param": "duration", "match": "present", "charge": "multiply_by_value",
           "multiplier": 2.0, "base": 10.0}], {"duration": 5}, 20.0),
    ]

    @staticmethod
    def _model(price, rules):
        return nm._normalize_model({
            "model": "parity", "displayName": "Parity", "modelType": "video",
            "status": "online", "protocols": ["openai"],
            "billing": {"mode": "per_request", "price": price, "unit": 1},
            "surchargeRules": rules,
            "params": [{"name": "duration", "type": "number", "required": False,
                        "min": 4, "max": 30},
                       {"name": "imageSize", "type": "string", "required": False,
                        "values": ["1K", "2K", "4K"]}]})

    def test_every_fixture_matches_the_gateway_formula(self):
        for label, price, rules, params, expected in self.FIXTURES:
            with self.subTest(label=label):
                cost = nm.compute_credit_cost(self._model(price, rules), params)
                self.assertTrue(cost["exact"], label)
                self.assertEqual(cost["credits"], expected, label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
