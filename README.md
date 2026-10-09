# noova-generation-skill

[![CI](https://github.com/Mark-Jordan/noova-generation-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/Mark-Jordan/noova-generation-skill/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/downloads/)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-6f42c1.svg)](https://agentskills.io/specification)

**面向 [NooVa AI](https://noova.vip) 创作平台的 Agent Skill：让 AI agent 直接调用平台公开 API 生成图片 / 视频 / 音频 / 文本。**

用户只要说一句「生一张戴墨镜的猫」，agent 就会先弹出**问答板**确认模型与参数（不擅自扣费），
再发起调用、轮询任务、原样交付结果地址与积分消耗。

```
用户：帮我生一张图，一只戴墨镜的猫

Agent：[类型面板] 图像 / 视频 / 音频 / 文本  → 用户选「图像」
       [模型面板] 模型编码 · 线路 · 近似消耗 · 状态（在线/维护中） → 用户选一个
       [参数面板] 尺寸 / 比例 / 张数 …（按平台实时契约） → 用户确认
       —— 确认后立即开始生成、开始扣费 ——
       [完成] 生成成功，共 1 张图片
              结果地址：https://…（原样交付）
              本次扣减：4 积分（账户余额 100.19 → 96.19）
```

---

## 目录

- [这个 skill 能做什么](#这个-skill-能做什么)
- [快速开始](#快速开始)
- [安装方式](#安装方式)
  - [方式一：把仓库链接发给你的 agent（推荐）](#方式一把仓库链接发给你的-agent推荐)
  - [方式二：一条命令安装](#方式二一条命令安装)
  - [方式三：手动安装](#方式三手动安装)
- [API Key 配置](#api-key-配置)
- [使用示例](#使用示例)
- [仓库结构](#仓库结构)
- [常见问题](#常见问题)
- [License](#license)

---

## 这个 skill 能做什么

| 能力 | 说明 |
|---|---|
| 🖼️ **图片生成** | 文生图、参考图生图，支持一次多张 |
| 🎬 **视频生成** | 文生视频、参考视频/参考音频，长任务可后台续等 |
| 🔊 **音频生成** | 文本转语音、音效等 |
| ✍️ **文本生成** | 同步返回，支持流式输出（SSE）与多模态图片输入 |
| 🔍 **模型发现** | 运行时拉取平台当前可用模型（按类型分区、含计价与状态） |
| 📐 **参数契约** | 每个模型的参数 / 可选值 / 数值区间 / 必填项，全部运行时查询 |
| 💰 **积分与报价** | 单价、加收规则、余额；扣减口径为**账户余额差值** |
| 📤 **素材上传** | 本地文件自动上传（多通道降级），供参考图/首帧/参考音频引用 |
| ⏱️ **任务续查** | `--no-wait` 提交后拿任务 ID，稍后 `wait` / `task` 取结果 |

**适用**：你要在 NooVa 平台上生成内容，或查询只有该平台能回答的实时事实（有哪些模型、某个模型能填哪些参数、还剩多少积分）。

**不适用**：回答模型本身的知识问题（例如「某模型官方支持哪些分辨率」）—— 那属于上游模型的官方文档，本 skill 不含这类知识，也不该用它来回答。

---

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/Mark-Jordan/noova-generation-skill.git
cd noova-generation-skill

# 2. 安装到你的 agent 的技能目录（自动探测当前 agent）
python install.py install

# 3. 配置 API Key（把 Key 粘贴给 agent 即可；也可手动）
echo "sk-你的Key" | python skills/noova-generation/scripts/noova_key.py setup --stdin

# 4. 试一张图
python skills/noova-generation/scripts/noova_media.py image \
    --prompt "一只戴墨镜的猫" --model <模型编码>
```

> 需要 **Python ≥ 3.8**，仅标准库，**无需 `pip install`**。
> Windows 上若 `python3` 无输出，改用 `python`（`python3` 常被 Microsoft Store 占位符拦截）。

---

## 安装方式

### 方式一：把仓库链接发给你的 agent（推荐）

**这是最省事的方式。** 把下面这句话连同仓库链接一起发给你的 AI agent（Claude Code、Codex、Cursor、ZCode、DSH、pi 等任何能执行命令的 coding agent）：

> 请帮我安装这个 skill：`https://github.com/Mark-Jordan/noova-generation-skill`
>
> 克隆仓库后运行 `python install.py install` 把它装到当前 agent 的技能目录，
> 然后按 `skills/noova-generation/SKILL.md` 确认它已被正确加载。

agent 会自己完成「克隆 → 探测技能根 → 安装 → 校验」，你不需要手敲任何命令。

> 💡 也可以直接把仓库里的 `skills/noova-generation/` 目录交给 agent，让它放进对应的技能目录。
> 若你的 agent 支持 Agent Skills 生态，也可以把它装到通用技能根 `~/.agents/skills/`。

### 方式二：一条命令安装

```bash
git clone https://github.com/Mark-Jordan/noova-generation-skill.git
cd noova-generation-skill
python install.py install              # 装到当前 agent 的技能根（自动探测）
python install.py install --target claude   # 或显式指定宿主
python install.py targets              # 先看看能装到哪
python install.py install --dry-run    # 只预览，不写入
```

安装器会：

1. **探测当前运行的 agent**（依据它注入的环境变量），选对技能根；
2. 增量复制（只写有变化的文件，原子写入，中断不会留下半截文件）；
3. 做一次**可见性自检**，若装到 agent 读不到的位置会明确警告。

支持自动探测的宿主：通用 agents 根、Claude Code、Codex、opencode/omp、CodeBuddy、WorkBuddy、Qoder、Goose、Crush、Devin、ZCode、OpenClaw、Hermes。

### 方式三：手动安装

```bash
git clone https://github.com/Mark-Jordan/noova-generation-skill.git
cp -r noova-generation-skill/skills/noova-generation ~/.agents/skills/
```

> ⚠️ 必须**整个目录一起复制**，包含 `scripts/noova_common.py`（缺少它其余脚本无法导入）。
> 目标必须是**你的 agent 真正读取的技能根**，否则等于没装。

---

## API Key 配置

本 skill 需要 [NooVa AI](https://noova.vip) 的 API Key（形如 `sk-…`）。

**获取 Key**：登录 [noova.vip/api_control](https://noova.vip/api_control) → 「API 管理」→「创建 API Key」→ 立即复制完整 Key（只显示一次）。

**交给 skill**（推荐 `--stdin`，Key 不会留在 shell 历史里）：

```bash
echo "sk-你的Key" | python skills/noova-generation/scripts/noova_key.py setup --stdin
```

`setup` 会先**联网校验**，通过后才写盘；校验失败不写入。也支持直接粘贴一整段文本（Key、`api_key=…`、`Bearer …`、JSON、官网链接混排都能解析）。

**其他配置方式**：

| 方式 | 做法 |
|---|---|
| 环境变量 | `export NOOVA_API_KEY=sk-…`（零配置，适合 CI） |
| 配置文件 | `~/.noova/config.json`（`setup` 自动写入，权限 0600，原子写） |
| 环境检查 | `python skills/noova-generation/scripts/noova_key.py doctor`（9 项自检） |

> 🔐 Key **只保存在本机**，只发往 `noova.vip` / `noova.live` 两个官方域名，脚本绝不打印完整 Key。

---

## 使用示例

> 更完整的命令地图与标准工作流见 [`skills/noova-generation/SKILL.md`](skills/noova-generation/SKILL.md)。

### 生成（agent 会自动先弹问答板）

```bash
M=skills/noova-generation/scripts/noova_media.py

# 问答板：类型 → 模型 → 参数（三级，逐级确认）
python $M form --json
python $M form --type image --json
python $M form --code "模型编码示例" --json

# 生成图片（模型编码含空格必须加引号）
python $M image --prompt "一只戴墨镜的猫" --model "模型编码示例" --param size=1024x1024

# 生成视频：先提交，拿到任务 ID 后再续等（避免长占终端）
python $M video --prompt "海浪拍打礁石" --no-wait
python $M wait --id <任务ID> --type video

# 文本生成（同步，可流式）
python $M chat --prompt "为新品咖啡写一句品牌标语"
python $M chat --prompt "继续写" --stream
```

### 查询

```bash
python $M models --type video            # 当前可用模型（含线路与计价）
python $M params --code <模型编码> --json # 该模型的参数契约（取值/区间/必填）
python $M credit --json                  # 积分余额
```

### 上传素材

```bash
python $M upload --file ./photo.png              # 上传素材（平台存储通道）
python $M upload --file ./clip.mp4 --host platform  # 显式指定平台存储通道（需 Key）
```

> 素材只上传到**平台自有存储**（需 API Key），不经手任何第三方服务。

### 输出与退出码

- 结果地址与计费反馈走 **stdout**，过程信息（`[提交]` / `[轮询]`）走 **stderr**；
- `--json` 时 stdout **只有可解析的 JSON**；
- 退出码：`0` 成功 / `1` 业务失败（Key 无效、任务失败、无产出）/ `2` 配置或用法错误。

---

## 仓库结构

```
noova-generation-skill/
├── install.py                    # 安装 / 校验 / 打包工具（仓库侧，不属于 skill）
├── skills/
│   └── noova-generation/         # ← skill 本体（可直接拷进技能目录）
│       ├── SKILL.md              # 主指令：路由、铁律、工作流
│       ├── references/           # 按需加载的参考文档
│       │   ├── api-contracts.md  # 公开端点、鉴权、错误码
│       │   ├── protocols.md      # 同步 / 任务型 / 流式 + 协议适配
│       │   ├── parameters.md     # 参数查询与语义
│       │   ├── troubleshooting.md
│       │   ├── host-compatibility.md
│       │   └── changelog.md
│       └── scripts/              # 零依赖 Python 标准库实现
│           ├── noova_media.py    # CLI 主入口（form/models/params/image/video/…）
│           ├── noova_key.py      # 配置与引导（setup/status/doctor/…）
│           ├── noova_upload.py   # 素材上传（多通道降级）
│           └── noova_common.py   # 地址守卫 + 脱敏 + 排版（共享基础设施）
└── tests/                        # 单元测试（离线、不消耗积分）
```

---

## 常见问题

<details>
<summary><b>agent 说找不到这个 skill / 装了但没生效</b></summary>

装到了 agent **读不到**的技能根。运行 `python install.py targets` 看当前 agent 的技能根，
或 `python install.py install` 让它自动探测；装完重启会话或刷新技能列表。
</details>

<details>
<summary><b>Windows 上 <code>python3</code> 没有任何输出也不报错</b></summary>

这是 Microsoft Store 的「应用安装程序」占位符拦截了 `python3`。改用 `python` 或 `py -3` 重跑，
**不要**据此判断脚本损坏。
</details>

<details>
<summary><b>提示「地址是本机 / 内网 / 开发环境地址」</b></summary>

守卫在保护你：只有你当前这台机器能访问的地址不能作为线上基础地址（Key 会发不到）。
用 `https://noova.vip`（或备用 `https://noova.live`）重跑即可。自建线上部署请显式
传 `--base-url <你的线上域名>`。
</details>

<details>
<summary><b>主域名无法访问怎么办</b></summary>

`noova.live` 是官方备用域名，承载同一套 API、同一批账号（Key 通用）：

```bash
python skills/noova-generation/scripts/noova_key.py base https://noova.live
```
</details>

<details>
<summary><b>任务等待超时，是失败了吗</b></summary>

**不是。** 超时后任务通常仍在跑。脚本会打印任务 ID，用下面命令续查：

```bash
python skills/noova-generation/scripts/noova_media.py wait --id <任务ID> --type video
python skills/noova-generation/scripts/noova_media.py task --id <任务ID>
```
</details>

<details>
<summary><b>缺 Python 能自动安装吗</b></summary>

本 skill 需要 Python ≥ 3.8。安装系统运行时属于改动你的机器，agent **必须先征得你同意**才会安装；
安装后需**重启 agent** 让新 PATH 生效。若客户端沙箱默认禁网，则无法自动安装，需你自行开启网络。
</details>

<details>
<summary><b>Key 会泄露吗</b></summary>

Key 只保存在本机（`~/.noova/config.json`，0600，原子写），只发往 `noova.vip` / `noova.live`；
脚本只回显前缀，不会打印完整 Key，也不会写日志。推荐用 `--stdin` 传 Key，避免留在 shell 历史。
</details>

更多排查见 [`references/troubleshooting.md`](skills/noova-generation/references/troubleshooting.md)。

---

## License

[MIT](LICENSE) © NooVa AI

> 本仓库源码以 MIT 协议开源；NooVa AI 平台服务本身的使用须遵守平台服务条款。
