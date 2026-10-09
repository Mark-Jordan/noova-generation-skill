# 变更记录

> 按时间**倒序**。本文件只记**有据可查**的变更，**不承诺覆盖全部历史**——
> 更早或未单独留档的迭代见仓库 git 历史。
>
> **不变量（版本号必须三处一致）**：
> ① `SKILL.md` 的 `metadata.version`；
> ② `scripts/noova_key.py` 的 `VERSION`；
> ③ 本文件出现同一版本号。
> 缺任一即视为发布缺陷，由 `test_noova_install.py::SourceTreeTest` 机械校验。

---

## 1.9.1 — 2026-10-10

**文档脱敏（无行为变更）**

- `references/api-contracts.md`：删去内部链路的端点路径与字段说明。现在只描述
  **对外**端点，内部链路仅保留「不使用、不推断、不展开」的边界声明。
- `references/troubleshooting.md`：把「不要用模型清单接口校验 Key」改为不依赖
  该接口鉴权语义的表述（其鉴权行为历史上变过，写死结论会再次失准）。
- 删去已下线端点的历史条目；脚本注释同步去除内部字段名。

> 本版无任何行为变更；`noova_key.py` 的两个鉴权探针与全部调用路径保持不变。

## 1.9.0 — 2026-10-10

**官方双域名支持（noova.vip 主域名 / noova.live 备用域名）**

- 官方域名从单一 `noova.vip` 扩为 `noova.vip` + `noova.live`（`noova_common.OFFICIAL_HOSTS`）：
  两者承载同一套对外 API，粘贴解析可信、地址守卫放行、脱敏白名单保留，**同等对待**。
- `www.` 变体统一归一到 apex（`www.noova.vip` → `noova.vip`，`www.noova.live` → `noova.live`）：
  避免同一站点两种写法在可信域判定与脱敏白名单里漂移；归一**不改变 scheme**，
  `http://www.noova.vip` 仍被明文拦截拦下。
- 修复既有缺陷：`is_local_host()` 对 `www.dev.example.com` 这类写法漏判开发前缀——
  DNS 解析失败时守卫会按「离线」放行，属于安全口子。现在剥离 `www.` 后再判定前缀。

> 升级说明：此前配置为 `noova.vip` 的用户不需任何操作；想改用备用域名的用户用
> `noova_key.py base https://noova.live` 切换即可（后续请求与脱敏自动跟随）。

## 1.8.3 — 2026-10-09

**文档事实性修正（无行为变更）**

- **`sampleprot` 协议**：平台当前**不对外提供**该协议（实测 34 个模型 0 个声明；
  `sample-3.7-flash` / `sample-3.8-flash` 等 SampleProt 系模型的契约协议是 `openai` / `anthropic`）。
  `SKILL.md`、`references/protocols.md`、`references/api-contracts.md` 原先称
  "平台为 SampleProt 提供非流式 `:generateContent` 路由"，属**失实断言**，已改为实测口径。
- 明确**不做 sampleprot 流式**（不实现 `:streamGenerateContent`）；sampleprot 分支保留为
  契约驱动的前向兼容，正常不会触发。
- `references/troubleshooting.md`：修正模型清单接口的鉴权描述（原先称"公开只读"，实际随部署变更，已改为不依赖该语义的写法）。
- 新增 frontmatter 字段 `compatibility`（运行环境要求）。

## 1.8.2 / 1.8.1 — 2026-10-06

修复沉淀：安装器与媒体脚本、`references/host-compatibility.md`、`references/troubleshooting.md`。
（仓库提交 `678cfef` 只给出改动范围，未逐条留档。）

## 1.8.0 — 2026-10-04

模型参数来源改为**对外契约**；同名多线路可选（展示 `line`）；新增近似积分预估。

## 1.7.0 — 2026-10-03

参数来源换成对外契约端点；报价把 `surchargeRules` 加收规则一并算入。

## 1.5.0 — 2026-10-03

跨平台解释器解析（`python3` / `python` / `py -3`）、出网地址守卫、回归测试、宿主兼容；
并修复一批缺陷：`clear` 清理全部配置来源、`NOOVA_CONFIG_DIR` 读写两侧一致、
上传可达性校验不再把 `403` 误判为"可公开访问"、中文文件名 ASCII 安全化、
非法 JSON 配置不生效、任务终态失败明确退出码 1。

## 更早 — 2026-09-30 及之前

skill 于 2026-09-30 更名为 `noova-generation`；更早历史见 git。
