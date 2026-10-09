# 拉取请求

## 改了什么

<!-- 文件与符号；涉及 skill 行为时写明「用户可见行为的前后差异」 -->

## 为什么

<!-- 关联 Issue（如 Fixes #12）；说清动机而不是复述 diff -->

## 怎么验证

<!-- 跑过的命令与结果，例如 `python -m pytest tests -q` → 444 passed -->
- [ ] `python install.py validate`
- [ ] `python -m pytest tests -q`
- [ ] 行为变更已在真实 agent 里手动验证（可选，但涉及安装/加载路径时必填）

## 检查清单

- [ ] 版本号三处一致（`SKILL.md` / `noova_key.py` / `references/changelog.md`）并已在 changelog 记录
- [ ] 修 bug 附带了「改回去就变红」的回归用例
- [ ] 未引入第三方依赖（skill 本体只用 Python 标准库 ≥ 3.8）
- [ ] 未把安装器/测试等仓库侧资产放进 `skills/noova-generation/`
- [ ] 用户可见输出不含 Key、内部域名、堆栈（含 `--json` 路径）
- [ ] 涉及安全边界时，PR 描述里写明了攻击面变化
