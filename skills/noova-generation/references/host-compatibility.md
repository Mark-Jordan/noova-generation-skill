# 宿主与操作系统差异（运行时边界）

本文件回答一个问题：**同一个 skill 在不同 agent 客户端上跑，哪些差异会真的影响使用，哪些不会。**

---

## 0. 设计原则（先读这段，再读下面的表）

**本 skill 的核心流程不按 agent 分叉。** 原因：

- skill 的调用方式是「跑一个 Python 脚本」，这是**进程级**行为，不是「agent 专属 API」。
  `python <脚本> image --prompt "..."` 在任何客户端里都是同一件事。
- 为每个 agent 写一套分支流程，会让**每个分支都要单独验证**，维护成本随客户端数量线性增长，
  而收益只是「少一次运行时探测」。生态里的主流 skill 也都只有一套流程。
- 因此差异只体现在**三个运行时边界**上：权限、网络、PATH。这三件事在**执行时探测**，
  而不是在**文档里按 agent 名硬编码**。

**唯一例外**：某个客户端有**确定的、可复现的**行为差异时，才在本文件里单独列出（见 §3）。

> **但有一件事确实按 agent 分叉：装到哪里。** 上面说的是**运行时**边界（跑起来之后），
> 而**分发**侧每个 agent 的技能根各不相同，装错位置就是「装完了它读不到」。
> 这一节在 §4，与运行时边界分开处理。

---

## 1. 三个运行时边界（真正需要处理的只有这些）

| 边界 | 症状 | 统一处理策略 |
|---|---|---|
| **权限** | 命令被拒绝执行 / 需要用户逐次批准 | 如实告知用户「需要允许执行本 skill 的 Python 脚本」，把脚本绝对路径给出来让用户判断；**不要**反复重试或绕道 |
| **网络** | 请求超时 / 被沙箱拒绝 / 提示需开启网络 | 见 §2：沙箱默认禁网时，**必须由用户开启**，agent 无法自行绕过；如实说明后停止，不要空转重试 |
| **PATH / 解释器** | `python` / `python3` 找不到，或找到的是假占位符 | 先试 `python3` → `python` → `py -3`；都不可用则按 §3 安装。脚本自身输出的命令用的是**当前解释器的绝对路径**，不受 PATH 别名影响。注意 `py` 启动器**不是标配**，部分 Windows 机器上根本没有 |

### 一个已实测的具体陷阱（Windows）

Windows 上 `python3` 常被 Microsoft Store 的「应用安装程序」占位符占用：
运行后**没有任何输出、也不报错**，退出码非 0。这不是「没装 Python」，而是**名字被占用**。

处理：直接换 `python` 或 `py -3`；**不要**据此判断脚本损坏，也不要改用手写 HTTP 请求绕开脚本。

---

## 2. 缺 Python 时的标准处理（顺序不可颠倒）

```
① 探测：python3 --version || python --version || py -3 --version
   命中 → 正常使用，什么都不用做
② 全部未命中 → 如实告知用户「本机缺少 Python ≥ 3.8，本 skill 无法运行」
③ 询问用户是否同意安装（安装系统运行时 = 改动用户机器，必须用户拍板）
④ 用户同意 → 按 §2.1 的当前系统命令安装
⑤ 安装完成 → 提示用户「重启 agent 后重试」
⑥ 用户不同意 / 装不了 → 明确告知本 skill 当前不可用，停止。不要伪造结果
```

### 2.1 按系统安装（仅在用户同意后执行）

| 系统 | 命令 |
|---|---|
| Windows | `winget install Python.Python.3.12` |
| macOS | `brew install python3` |
| Debian / Ubuntu | `sudo apt update && sudo apt install python3` |

### 2.2 装完之后为什么不能马上用

agent 执行的每条命令都是**独立进程**，其 `PATH` 在 agent 启动时就已经固化；
新装的 Python 写入的是**系统/用户级 PATH（注册表或 shell profile）**，
**当前会话不会自动刷新**。

- 正解：**重启 agent**，让新 PATH 生效。
- 急用：让用户提供新解释器的**绝对路径**，直接用它调用脚本。
- **不要**反复重试原命令——现象会一直一样，只会浪费用户时间。

### 2.3 禁止的替代做法

不得改用手写 `curl` / 直接拼 HTTP 请求来代替脚本。参数契约解析、任务轮询、错误脱敏、
域名净化都在脚本里；绕开脚本会直接违反 `SKILL.md` 的铁律（尤其是 #3 / #6 / #8）。

---

## 3. 客户端专属注意事项（只列已实证的）

> 原则：**只写「确定的、可复现的」差异**。没有实证就不要往这里加条目——
> 猜测性的「某客户端可能不行」会让文档变成谣言集，比不写更糟。

### 3.1 沙箱类客户端（以 Codex 为例）

部分客户端默认在**沙箱**里执行命令，且**默认禁止联网**。装 Python 必须联网下载，
因此在这类环境下 agent **无法自行完成安装**。

依据（客户端自带的说明文档原文）：

> In many Codex setups, network access is disabled by default and/or the approval policy
> requires confirmation before networked commands run.
>
> `--ask-for-approval never` suppresses approval prompts. It does **not** by itself enable
> network access.

**处理**：如实告知用户「当前客户端沙箱默认禁网，无法自动安装；请自行开启网络后重试，
或改用已具备 Python 环境的客户端」。**不要**反复重试，也不要尝试绕过沙箱。

> 即使本 skill 能跑起来（已装 Python），生成请求本身**也需要出网**——
> 沙箱禁网的环境下，**本 skill 整体不可用**，这与「装不装 Python」无关。

### 3.2 非沙箱客户端（以本机用户权限执行）

部分客户端以当前用户权限直接执行命令，不额外限制网络与文件访问。
这类环境下「缺 Python → 征得同意 → 安装 → 重启」的路径通常可以走通。

但**是否可走通仍取决于用户自己的权限配置**，不能一概而论。
本 skill 不对任何客户端做「一定能装」的假设，一律按 §2 的六步处理。

---

## 4. 与「环境声明」的关系（重要，避免误判）

Python ≥ 3.8 这一前置要求写在 `SKILL.md` **正文的「运行环境」一节**，**不放在 frontmatter**。

原因（跨 agent 取证）：Codex 自带的 `skill-creator/scripts/quick_validate.py` 只允许
`name` / `description` / `license` / `allowed-tools` / `metadata` 五个 frontmatter 键，
多一个 `compatibility` 就直接判 `Unexpected key(s) ... compatibility`
（WorkBuddy 的 `quick_validate.py` 无此白名单，但也**不读**该字段）。
把环境要求放进正文，两类宿主都能读、都不报错。

**这仍然只是声明，不是强制。** 实测结论：

- 客户端**不会**因为缺 Python 而拒绝加载本 skill；
- 客户端**不会**因为这条声明而自动去装 Python；
- 它只对「读文档的人 / agent」有意义：说明本 skill 的前置条件是什么。

因此**真正的兜底只有两件事**：① 执行前探测；② 缺失时按 §2 如实告知。
不要把「能用」寄托在声明字段上。

---

## 5. 分发目标：装到哪，由「装给哪个 agent」决定

**这是与运行时边界并列的另一类差异，而且后果更隐蔽**：装到读不到的位置时，
安装本身完全成功、校验也报「已安装且一致」，用户与 agent 双方都看不到任何错误，
而 skill **永远不生效**。

### 5.1 各 agent 的技能根

| Agent | 用户级技能根 | 环境变量覆盖 |
|---|---|---|
| 通用 agents 根（DSH / opencode 等读取此处） | `~/.agents/skills` | — |
| [CC] | `~/.claude/skills` | `CLAUDE_CONFIG_DIR` |
| Codex | `~/.codex/skills` | `CODEX_HOME` |
| opencode / omp | `~/.config/opencode/skill(s)` | `XDG_CONFIG_HOME` |
| ZCode | `~/.zcode/skills` | — |
| OpenClaw | `~/.openclaw/skills` | — |
| Hermes | `~/.hermes/skills` | — |
| CodeBuddy | `~/.codebuddy/skills` | — |
| WorkBuddy | `~/.workbuddy/skills` | — |
| Qoder | `~/.qoder/skills` | — |
| Goose / Crush / Devin | `~/.config/<name>/skills` | `XDG_CONFIG_HOME` |

项目级根：多数宿主在 `<工作区>/.<name>/skills`（例如 `.claude/skills`、`.codex/skills`），
只对当前工作区生效。

**一个关键事实：`~/.agents/skills` 是被多个宿主**原生**读取的通用根。** opencode 自带文档
原文列出：「External skills (auto-loaded) | `~/.claude/skills/<name>/SKILL.md`,
`~/.agents/skills/<name>/SKILL.md`」。因此对 opencode 而言，装到 `~/.agents/skills`
（或 `~/.claude/skills`）就已经生效，`~/.config/opencode/skill(s)` 只是可选的项目/全局根。
这也是本机技能管理器把 `.agents/skills` 当**生态根**、再扇出的原因。

### 5.1.1 扇出：一个生态根 + 每个宿主根一个 junction

本机（以及同类「技能管理器」安装的机器）的既有惯例是：

- 真实技能只存一份：`~/.agents/skills/<name>/`；
- 其余宿主根下放**目录联接（junction）**指向它，例如
  `~/.claude/skills/<name> -> ~/.agents/skills/<name>`，`~/.zcode/skills/<name>`、
  `~/.openclaw/skills/<name>`、`~/.hermes/skills/<name>` 同理。

好处：更新一次、所有宿主同步生效；不会产生 §5.3 说的「孤儿副本」。
需要扇出到新宿主时的正确做法：**建 junction，而不是复制目录**。

Windows（Git Bash）实测可用的建联接命令（注意 `//J`，单写 `/J` 会被 MSYS 当成路径）：

```bash
cmd //c mklink //J 'C:\Users\<u>\.zcode\skills\<name>' 'C:\Users\<u>\.agents\skills\<name>'
```

### 5.2 分发工具的选择顺序

由分发方提供的安装器（**不是 skill 的一部分**）。
`--target auto`（默认）按下列顺序决定：

1. **探测当前正在运行哪个 agent**（依据它注入的环境变量，如 DSH 的 `DSH_SHELL`、
   [CC] 的 `CLAUDECODE`、Codex 的 `CODEX_SANDBOX`）→ 用它的技能根；
2. 探测不到时，用本机**被技能管理器管理的生态根**（存在 `.skill-lock.json` 的那个）；
3. 再退到「已存在且可写」的用户级技能根。

装完会做一次**可见性自检**：目标根若不是当前 agent 的技能根、或不是生态根，
会明确提示"该 agent 很可能发现不了这个 skill"并给出应改用的 `--target`。

### 5.3 为什么不要复制真实目录到非生态根

不同客户端对技能根的配置方式不同；当多个技能根存在时，它们**可能是同一个真实目录的
目录联接（junction）**，此时真正**真实**的技能根只有一个。
往非生态根复制一份真实目录的后果：

- 该副本不在技能管理器的账本里，**不会被更新、也不会被清理**——是一份无人管理的孤儿；
- 用户以为"装好了"，而实际生效的是另一份（或根本没有生效的那一份）。

因此：**优先生态根**；需要扇出到别的客户端时，用 `install --link`（建目录联接）而不是复制。

### 5.4 手工复制的注意事项

若确实要手工复制目录（不推荐）：

- **整个目录一起复制**，必须包含 `scripts/noova_common.py`（缺少它其余脚本无法导入）；
- 不要复制 `.noova/config.json`——那里面是用户的 API Key；
- 复制后确认目标目录是**该 agent 读取的那个**技能根，否则等于没装。
