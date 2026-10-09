# 安全策略

## 报告漏洞

**请勿通过公开 Issue 报告安全漏洞。**

请使用 GitHub 的
[私密漏洞报告](https://github.com/Mark-Jordan/noova-generation-skill/security/advisories/new)，
或发送邮件到仓库维护者（见 GitHub 主页）。请在报告中包含：

- 受影响的版本（`SKILL.md` 的 `metadata.version`）；
- 漏洞类型与影响面（凭据泄露 / 域名劫持 / SSRF / 注入 …）；
- **最小复现步骤**；
- 你的判断依据（代码行 / 实际数据）。

我们会在确认后尽快修复，并在 `references/changelog.md` 记录（必要时发布安全公告）。

## 安全模型

本 skill 处理用户凭据（API Key）并代表用户出网请求，因此把以下几点当作**不可谈判**的约束：

| 约束 | 实现位置 |
|---|---|
| Key 只发往官方线上域名（`noova.vip` / `noova.live`）或用户**显式**指定的自建线上域名 | `noova_common.check_base_url()`，写盘前 + 发请求前双重拦截 |
| 本机 / 内网 / 开发环境地址一律拒绝（含尾点、userinfo、非标准数字 IP、通配 DNS 指向回环等绕过手法） | `is_local_host()` + `resolves_to_local()`（DNS 解析后判定） |
| 携带凭据的请求只跟随**同源**重定向 | `SameOriginRedirectHandler` |
| 第三方返回的重定向不得跳向本机 / 内网（SSRF） | `PublicRedirectHandler` |
| 粘贴文本里出现的非官方 URL 一律忽略（防止复制 Key 时连带复制文档链接导致 Key 外发） | `noova_key.parse_credentials()` |
| Key 不明文回显、不写日志；配置文件 0600 原子写 | `noova_key.py` |
| 用户可见输出（含 `--json`）脱敏非公开域名与内部错误原文 | `sanitize_text()` / `sanitize_payload()` |
| 校验 Key 只用专用接口，不用公开只读接口 | `POST /api/v1/gateway/validate-key`（回退 `GET /api/v1/credit`） |

## 不在范围内

- 平台服务端本身的漏洞（请报告给 NooVa AI 平台，而不是本仓库）；
- 上游模型服务商的问题；
- 用户在自己的环境里主动设置逃生阀（`NOOVA_ALLOW_LOCAL_BASE_URL=1` /
  `NOOVA_ALLOW_INSECURE_BASE_URL=1` / `NOOVA_DEBUG=1`）后产生的行为 ——
  这些开关默认关闭，且为开发者本机联调而存在。

## 支持版本

安全修复只针对最新发布版本。请保持 skill 更新（`python install.py install`）。
