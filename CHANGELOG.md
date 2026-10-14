# 变更记录

本文件记录**仓库级**发布。skill 本体的详细变更见
[`skills/noova-generation/references/changelog.md`](skills/noova-generation/references/changelog.md)（随 skill 一起分发）。

> **版本号不变量（三处一致）**：
> ① `skills/noova-generation/SKILL.md` 的 `metadata.version`；
> ② `skills/noova-generation/scripts/noova_key.py` 的 `VERSION`；
> ③ `skills/noova-generation/references/changelog.md` 出现同一版本号。
> 由 `tests/test_noova_install.py::SourceTreeTest` 机械校验。

---

## 1.11.0 — 2026-10-14

**报价不再显示不可信的数字（含行为变更）**

确认生成前的报价**只在服务端给出确定值时才显示金额**；服务端给不出确定值时不再显示任何
数字（也不转述服务端的原因说明，那是给 agent 的接口指令）。此时唯一可信的信息是生成结束
后的账户余额变化。同时移除了本地复算兜底路径，面板数值层与文案层强制同源。

详见 `skills/noova-generation/references/changelog.md`。

## 1.10.0 — 2026-10-13

**参数依赖（选项分档）贯通面板 + 单值参数自动采用 + 两项计费修复（含对外契约变更）**

对外契约 `GET /api/models/params` 从本版起**不再下发 `line`（线路）**；同名模型一律
靠 `model` 编码区分。同一次变更里新增了参数依赖字段，修复了两个计费缺陷。

- 新增 `optionVisibleWhen`（与 `values` 同序等长的平行数组）与字段级 `visibleWhen`，
  面板改为按控制器取值分档（`depends_on` / `option_groups` / `active_group`）；
- `values` 恰好 1 个的参数不进面板，改为进 `preset_params` 由脚本自动写入请求体；
- 面板字段按依赖拓扑排序（控制器在依赖项之前，同级必填在前）；
- 修复精确报价在契约缺 `base` 键时直接报错退出（线上缺陷，影响 4 个时长加收模型）；
- 修 `exact` 规则取值比较为归一化口径（`5.0` == `5`）；
- 报价改为优先引用服务端算好的 `pricing.credits`，`surchargeRules` 降级为明细（非权威）。

详见 `skills/noova-generation/references/changelog.md`。

## 1.9.3 — 2026-10-11

**枚举参数「取值」与「展示标签」混用（含行为变更）**

线上实测复现：图像模型的比例参数，契约 `values` 是像素串（`1280x720`），
`valueLabels` 才把它标成 `16:9`；而参数面板的人读渲染只打印标签，
于是照面板传 `--param aspect_ratio=16:9` 必然拿到 HTTP 400。

- 修 `_render_form` 的 `choice` 渲染：改为 `取值（标签）`（取值多时按 80 列折行）；
- 新增发请求前的取值预检 `_ensure_param_values`（媒体 / 文本两条路径），
  取值不在契约 `values` 里时本地拒发并列出合法取值；
- 确定性错误（400 等 4xx）不再透传网关无差别返回的 `Retry-After`。

详见 `skills/noova-generation/references/changelog.md`。

## 1.9.2 — 2026-10-10

**内容合规清理**

- 移除第三方上传通道（素材只走平台自有存储）；去掉文档/代码中的具体外部模型名；
  删除未启用的死协议分支。详见 `skills/noova-generation/references/changelog.md`。

## 1.9.1 — 2026-10-10

**文档脱敏（无行为变更）**

- skill 文档删去内部链路的端点路径与字段说明，只描述对外端点；
  已下线端点的历史条目一并移除。
- 详见 `skills/noova-generation/references/changelog.md`。

## 1.9.0 — 2026-10-10

**官方双域名支持 + 独立开源仓库**

- 官方域名从单一 `noova.vip` 扩为 `noova.vip`（主）+ `noova.live`（备用）：两者承载同一套
  对外 API，粘贴解析可信、地址守卫放行、脱敏白名单保留，**同等对待**。
- `www.` 变体统一归一到 apex；归一不改变 scheme（`http://www.noova.vip` 仍被明文拦截拦下）。
- 修复缺陷：`is_local_host()` 对 `www.dev.example.com` 这类写法漏判开发前缀。
- 本仓库从零建立：标准开源仓库结构（README / LICENSE / CONTRIBUTING / SECURITY /
  CODE_OF_CONDUCT / CI / Issue 模板），skill 本体迁入 `skills/noova-generation/`。

## 1.8.3 — 2026-10-09

- 文档事实性修正：删除一段“平台已下线功能”的描述；修正模型清单接口的错误描述；
  新增 frontmatter 字段 `compatibility`。

## 1.8.2 / 1.8.1 — 2026-10-06

- 安装器与媒体脚本、宿主兼容与排查文档的修复沉淀。

## 1.8.0 — 2026-10-04

- 模型参数来源改为**对外契约**；同名多线路可选（展示 `line`）；新增近似积分预估。

## 1.7.0 — 2026-10-03

- 参数来源换成对外契约端点；报价把 `surchargeRules` 加收规则一并算入。

## 1.5.0 — 2026-10-03

- 跨平台解释器解析（`python3` / `python` / `py -3`）、出网地址守卫、回归测试、宿主兼容；
  修复 `clear` 清理范围、`NOOVA_CONFIG_DIR` 读写一致、上传可达性误判等一批缺陷。

## 更早 — 2026-09-30 及之前

- skill 于 2026-09-30 更名为 `noova-generation`；更早历史见上游仓库 git。
