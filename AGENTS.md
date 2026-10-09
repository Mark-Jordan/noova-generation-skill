## 本目录是 `noova-generation-skill` 仓库

`noova-generation` 是面向 AI agent 的技能（Skill），让 agent 能直接调用 NooVa AI 创作平台的公开 API：
生图 / 生视频 / 生音频 / 文本生成、模型发现、参数契约查询、积分与计价、素材上传。

### 权威来源（改动前必读）

| 主题 | 文件 |
|---|---|
| 技能契约（路由、铁律、标准工作流） | `skills/noova-generation/SKILL.md` |
| 公开端点与鉴权 | `skills/noova-generation/references/api-contracts.md` |
| 同步/异步/流式与协议适配 | `skills/noova-generation/references/protocols.md` |
| 参数语义与计费口径 | `skills/noova-generation/references/parameters.md` |
| 排查与宿主兼容 | `skills/noova-generation/references/{troubleshooting,host-compatibility}.md` |
| 版本历史 | `skills/noova-generation/references/changelog.md` |

### 不可违反的不变量

1. **版本号三处一致**：`SKILL.md` / `noova_key.py` 的 `VERSION` / `references/changelog.md`。
   由 `tests/test_noova_install.py::SourceTreeTest` 机械校验。
2. **skill 目录只含标准结构**：`SKILL.md` + `scripts/` + `references/`（+ 可选 `assets/`）。
   安装器、测试、README 一律放**仓库根** —— 放进 skill 目录会被 CI 与安装器同时判为违规。
3. **零第三方依赖**：skill 本体只用 Python 标准库（≥ 3.8）。CI 在 3.8–3.12 × Ubuntu/Windows 上跑。
4. **测试离线**：任何测试不得真实出网、不得消耗积分、不得写用户真实 `~/.noova/config.json`。
5. **安全边界**（详见 `SECURITY.md`）：无 Key / 地址非法 / 校验失败时一个字节都不发、不写盘；
   用户可见输出（含 `--json`）不得出现完整 Key、内部域名、堆栈。

### 常用命令

```bash
python install.py validate                      # skill 目录形态校验
python -m pytest tests -q                       # 全部单元测试（离线）
python install.py targets                       # 看能装到哪些 agent 技能根
python install.py install --dry-run             # 预览安装变更
```

### 约定

- 改动 `skills/noova-generation/` 时同步 bump 版本号并写 changelog；
- 修 bug 必须附带回归用例（`tests/test_noova_regressions.py` 或对应文件）；
- 提交信息用 Conventional Commits（`fix(media): …` / `feat(key): …`）。
