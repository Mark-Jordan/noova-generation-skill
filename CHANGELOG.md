# 变更记录

本文件记录**仓库级**发布。skill 本体的详细变更见
[`skills/noova-generation/references/changelog.md`](skills/noova-generation/references/changelog.md)（随 skill 一起分发）。

> **版本号不变量（三处一致）**：
> ① `skills/noova-generation/SKILL.md` 的 `metadata.version`；
> ② `skills/noova-generation/scripts/noova_key.py` 的 `VERSION`；
> ③ `skills/noova-generation/references/changelog.md` 出现同一版本号。
> 由 `tests/test_noova_install.py::SourceTreeTest` 机械校验。

---

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
