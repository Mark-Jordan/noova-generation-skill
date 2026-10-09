# 贡献指南

感谢你有兴趣改进 `noova-generation-skill`。本文件说明提交流程与本仓库的硬性约束。

## 提交前必读

### 1. 三条不变量（CI 会机械校验）

- **版本号三处一致**：`skills/noova-generation/SKILL.md` 的 `metadata.version`、
  `skills/noova-generation/scripts/noova_key.py` 的 `VERSION`、
  `skills/noova-generation/references/changelog.md` 里出现同一版本号。
- **零第三方依赖**：skill 本体只用 Python 标准库（≥ 3.8）。新增依赖 = 拒绝。
- **测试离线且不碰用户配置**：测试必须能在死代理下全绿，且不得写入用户真实的
  `~/.noova/config.json`（默认指向临时目录）。

### 2. skill 目录只放标准结构

`skills/noova-generation/` 下**只允许**：

```
SKILL.md
scripts/
references/
assets/        # 可选
```

安装器、测试、README 等仓库侧资产都放**仓库根**，绝不能进 skill 目录
（那会让 skill 看起来「自带安装步骤」）。

### 3. 安全边界（硬约束）

以下任何一条被破坏都视为阻断性缺陷：

- 只用 `references/api-contracts.md` 列出的**公开**端点；不得访问或推断管理端、数据库、
  对象存储、服务商名称与内部配置。
- **模型清单与参数契约永远运行时拉取**，不得硬编码模型编码、参数名或可选值。
- **报价必须引用运行时数据**（`billing` / `surchargeRules`），不得凭模型名推算。
- 用户可见输出**不得**出现完整 Key、鉴权头、内部域名、堆栈（`--json` 路径同样适用）。
- **失败关闭**：无 Key / 地址非法 / 校验失败时，一个字节都不发、不写盘。
- 基础地址只允许官方线上域名（`noova.vip` / `noova.live`）或用户显式指定的自建线上域名；
  本机 / 内网 / 开发环境地址一律拒绝。

## 开发流程

```bash
# 1. fork + clone
git clone git@github.com:<你的用户名>/noova-generation-skill.git
cd noova-generation-skill

# 2. 改代码（skill 本体在 skills/noova-generation/）
#    - 修 bug：先在 tests/ 里加一条会失败的回归用例，再修实现
#    - 加能力：同步更新 SKILL.md 的「命令地图」与对应 references/

# 3. 同步版本号（三处）+ 写 changelog
# 4. 跑测试
python -m pytest tests -q
python install.py validate

# 5. 提交（Conventional Commits）
git commit -m "fix(media): 修正轮询超时被判成失败的逻辑"
```

## 测试要求

- **回归优先**：修 bug 必须附带一条「改回去就变红」的用例，写在
  `tests/test_noova_regressions.py` 或对应用例文件里，并在 docstring 写明
  「修复前会怎样」。
- **不联网**：所有测试用 `mock` 打桩出网调用；不允许真实请求、不允许消耗积分。
- **跨平台**：代码必须兼容 Python 3.8（CI 跑 3.8 / 3.9 / 3.10 / 3.12 × Ubuntu / Windows）。
  禁止 `str.removeprefix`、`Path.is_relative_to`、`os.path.isjunction` 等 3.9+ API
  （已有测试静态扫描拦截）。

## Commit 规范

使用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
feat(media): 新增按线路选择同名模型
fix(key): 修正 www 变体未归一到 apex
docs(skill): 补充双域名切换说明
test(regressions): 覆盖 www.dev 前缀漏判
```

## Pull Request

PR 描述里请包含：

1. **改了什么**（文件与符号）；
2. **为什么**（用户可见行为的前后差异）；
3. **怎么验证**（跑过哪些命令，结果如何）；
4. 涉及安全边界的改动，明确说明**攻击面变化**。

## 报告问题

请用仓库的 Issue 模板。报告 bug 时附上：

- 你的 agent 客户端与操作系统、Python 版本；
- 完整命令与**脱敏后**的输出（务必删掉 API Key）；
- `python skills/noova-generation/scripts/noova_key.py doctor` 的结果。

> ⚠️ **绝不要**在 issue 里粘贴真实 API Key。需要复现鉴权问题时，用占位符 `sk-xxxx`。

## License

提交代码即表示你同意以 [MIT](LICENSE) 协议授权你的贡献。
