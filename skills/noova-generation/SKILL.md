---
name: noova-generation
description: Use when the user wants to actually generate or call a model on the NooVa AI platform (noova.live / noova.vip) through its public API — 生图 / 画一张图 / 生视频 / 生成音频 / 写文案 / 文本生成 / 调用平台上的模型 — or asks about NooVa-platform-only live facts, such as which models the platform currently offers, a NooVa model's live parameter contract and allowed values, credit cost, balance, task polling, or one-time API key setup. Always confirms model and parameters through an interactive panel BEFORE generating. Do NOT load this skill to answer knowledge questions about a model itself (for example 「demo-image-2 的官方参数」「size 参数有哪些取值」「SampleModel 支持哪些分辨率」), or to explain what some third-party or upstream model supports; answer those from the provider's official docs or general knowledge. Distinguishing test — does the user want to do something on the NooVa platform, or only understand a model?
license: MIT — see the LICENSE file in this repository.
compatibility: "需要 Python ≥ 3.8（仅标准库，无第三方依赖）；运行时需能访问 https://noova.vip 或 https://noova.live。"
metadata:
  author: NooVa AI
  version: "1.9.0"
---

# NooVa AI 生成调用（公开 API）

面向**终端用户**的 NooVa AI（`https://noova.vip` / `https://noova.live`）公开 API 封装。用户只需说"生一张图 / 生成一段视频 / 写一段文案"，
本 skill 就会先用**问答板**请用户确认模型与参数，再发起调用并交付结果——用户不需要了解请求构造细节，
但**模型与参数必须由用户确认，不得代用户直接生成**（见「标准工作流 §1」）。

零第三方依赖（仅 Python 标准库，需 Python ≥ 3.8）；只访问平台**公开**接口。

## 适用边界（先读这一段，再决定要不要用本 skill）

本 skill 处理两件事：**在 NooVa 平台上执行操作**，以及**只有 NooVa 平台能回答的实时事实**。
它不是独立的「模型知识问答」。

| 用户意图 | 例子 | 冷启动时是否加载本 skill |
|---|---|---|
| 要生成 / 要调用平台 | 「生一张图」「用 demo-image-2 帮我生图」 | ✅ 加载 |
| 问平台实时事实 | 「你们现在有哪些模型」「这个模型在平台能填哪些值」「我还剩多少积分」 | ✅ 加载（`models` / `params` / `credit`） |
| 纯模型知识（官方 / 上游规范） | 「demo-image-2 官方参数是什么」「SampleModel 官方支持哪些分辨率」 | ❌ 不加载；据官方文档 / 通用知识回答 |

**判据**：用户问的是「**这个模型本身（官方 / 上游规范）**」还是「**NooVa 平台 / 这个 API 上的模型**」？

### 已经加载本文件之后（重要）

上面那张表的「不加载」只是**冷启动时的路由规则**，不是「加载后不许回答参数问题」。

- 本文件已在上下文里时，用户问「某个模型有哪些取值 / 能填什么」→ **按「标准工作流 §2」走运行时查询**
  （`form --code <模型编码>` / `params --code <模型编码>`），用**平台契约**回答；
  **不要**以「这是知识问题」为由拒绝，也**不要**改用记忆或通用知识回答。
- 只有当用户明确要的是**官方 / 上游模型规范**（而不是平台配置）时，才用官方文档 / 通用知识回答，
  并说明「那是模型官方口径，不是本平台的实际取值」。
- 属于「纯模型知识」这一类时，**只回答，不执行任何 `scripts/` 命令、不弹问答板、不引导用户生成**。

## 运行环境

本 skill 的脚本是纯 Python 3 标准库实现（**无需 pip install**），只需要机器上有一个
Python ≥ 3.8 解释器。**不同系统上它的名字不一样**，文档与提示里的命令**演示**用 `python3`，
你实际执行时**先挑一个能用的**：

| 候选 | 常见于 | 判断方法 |
|---|---|---|
| `python3` | macOS / Linux | `python3 --version` |
| `python` | Windows 官方发行版 | `python --version` |
| `py -3` | Windows 启动器（**装了才有**，未安装时同样不可用） | `py -3 --version` |

依次试，**取第一个能打印出版本号的**，后面所有命令都用它替换 `python3`。
注意 `py` 启动器**不是标配**（实测部分 Windows 机器上根本没有 `py` 命令），
所以它只是三个候选之一，不是"最稳"的那个。

> ⚠️ **Windows 上不要想当然用 `python3`**：它常被 Microsoft Store 的"应用安装程序"
> 占位符拦截，运行后**没有任何输出、也不报错**（退出码非 0）。遇到"命令像跑了但什么都没发生"，
> 直接换 `python` 或 `py -3`，不要据此判断"脚本坏了"。

脚本自身打印的命令提示**已经按当前实际运行的解释器绝对路径渲染**（`noova_key.py doctor` 的
「解释器路径」一项会显示它），不依赖 `python` / `python3` / `py` 哪个别名在 PATH 中；
只有本文档里的示例需要你自己替换。

**若三个候选都不可用**：说明本机没有 Python 环境。此时**如实告知用户**需要先安装
Python ≥ 3.8。安装系统运行时属于用户机器变更，agent **不得未经用户确认自动安装**。
用户明确同意后，才可按 `references/host-compatibility.md` 中当前系统的方案执行；安装完成后
必须重新启动 agent（或让用户提供新解释器的绝对路径），再运行本 skill。

> **路径约定**：`<SKILL_DIR>` 指本 SKILL.md 所在的目录（即安装后的 `noova-generation/`）。
> 文档中所有脚本路径都相对于它；不确定实际路径时，以本文件所在目录为准。
>
> **路径一律运行时解析，任何地方都不得写死**——不同用户的家目录、安装位置、
> 是否设置 `NOOVA_CONFIG_DIR` 都不一样。提示文案里的路径不是常量，而是脚本按当前
> 机器解析后填进去的，因此：**要路径就去跑脚本拿输出，不要照抄本文档里的示例路径，更不要自己拼一个。**

## 铁律（违反即失败）

1. **只用公开接口**。允许访问的端点仅限 `references/api-contracts.md` 列出的那些。
   **严禁**访问、猜测、推断任何内部/管理端接口、数据库、存储桶地址、服务商名称与配置项。
2. **模型清单永远运行时拉取**，绝不硬编码、绝不凭记忆编造模型编码。公开清单里有什么模型，就能调用什么模型。
3. **参数与取值以运行时查询为准**（`params` / `model` 子命令）。用户问"这个参数能填什么"时，先去查，再回答；
   查不到就说"该模型未提供参数契约"，**不许臆造参数名或可选值**。
4. **没有 API Key 时不得发起生成请求**，也不得谎称"已配置"。
   正常路径是**先弹问答板确认模型与参数、再生成**（见「标准工作流 §1」）——缺 Key 时脚本会自行拦截并打印引导块：
   把引导块**原样呈现给用户**、请用户把 Key 粘贴过来，再用
   `noova_key.py setup` 完成配置（先联网校验，通过才写盘）。
   **不得**自行编造或猜测 Key，也不得让用户去别处找。
   **传 Key 优先用 `--stdin`**（`echo "sk-…" | noova_key.py setup --stdin`）：
   `--api-key <key>` 会把 Key 留在 shell 历史与进程列表里。
5. **校验 Key 只能用专用校验接口**（`POST /api/v1/gateway/validate-key`，或回退用 `GET /api/v1/credit`）。
   **严禁**用模型清单接口判断 Key 是否有效——它是公开只读的，连无效 Key 都返回 200。
6. **输出零内部信息**：不向用户吐露服务商、内部域名、内部路径、错误堆栈；
   不打印完整 API Key（脚本只显示前缀）。这条对 **`--json` 路径同样成立**——
   agent 消费的就是 JSON，而 agent 会把 JSON 转述给用户，所以 JSON 里的非公开域名
   也已被替换（媒体 URL 除外，见铁律 7）。
7. **生成结果原样交付**：结果里的媒体 URL 是功能字段，必须原样给用户（用户靠它访问产物）。
   一次可能产出多个结果，**每一个都要给**。
8. **报价必须引用运行时数据**：单价与加收规则只来自 `params` 的返回（`billing` / `surchargeRules`），
   **不许**凭模型名、记忆或他人报价给出数字；不确定时说"以平台当前计价为准"。
   余额与消耗只引用 `credit` 或生成结束时的反馈（口径为**账户余额差值**）。
9. **耗时只能给区间，不能给承诺**：按类型给「典型区间」并说明"以实际任务为准"；
   **等待超时 ≠ 失败**——超时后任务通常仍在跑，用任务 ID 续等，不许据此宣告"生成失败"。
10. **基础地址只能是线上部署域名**（默认 `https://noova.vip`，备用 `https://noova.live`；两者都是官方域名，等价可用）。
   **严禁**使用 `localhost` / `127.0.0.1` / `0.0.0.0` / `192.168.*` / `10.*` / `172.16-31.*` /
   `dev.*` / `staging.*` / `*.local` / `*.internal` 等本机、内网或开发环境地址——
   它们只有当前这台机器能访问，写进配置后请求必然失败，还会被误判成"服务故障"。
   脚本会在**写盘前**（`setup` / `base`）与**发请求前**（每个出网请求，含 `verify`）
   双重拦截；守卫还会归一化主机名（尾点 / 用户名 / 端口）并做 DNS 解析后判定，
   所以 `localhost.` / `127.0.0.1.nip.io` 这类等价写法同样拦得住；跨源重定向一律拒绝。
   公开域名必须用 **https**（Key 走 `Authorization` 头，明文链路等于泄露凭据）。
   用户若粘了这类地址，直接用 `https://noova.vip` 重配，**不要**按该地址调用，也不要 `--force` 绕过。
   官方域名的 `www.` 写法会被归一到 apex（`www.noova.vip` → `noova.vip`），不必手动改写。
11. **基础地址只认可信来源**：`--base-url`、JSON 里的 `base_url` 字段，或与官方域名同域的地址
   （`noova.vip` / `noova.live`，含 `www.` 变体）。
   **粘贴文本里出现的其它 URL 一律忽略**（脚本会提示"已忽略文本中的地址"）——
   否则"用户从网页复制 Key 时连带复制了一行文档链接"就会把 Key 发到那个域名并被持久化。
   需要自定义地址时**显式**传 `--base-url`，不要指望脚本从散乱文本里猜。

## 首次使用：配置 API Key

**正常情况什么都不用做**——直接调用生成命令即可，凭据由脚本按下列优先级自动获取。
宿主若已注入 `NOOVA_API_KEY` 环境变量，则完全零配置。

| 优先级 | 来源 |
|---|---|
| 1 | 环境变量 `NOOVA_API_KEY` / `NOOVA_BASE_URL` |
| 2 | `$NOOVA_CONFIG_DIR/config.json` → `~/.noova/config.json` → `<SKILL_DIR>/.noova/config.json`（兜底） |

第二行的三条路径**都是运行时解析的**（家目录取 `Path.home()`、安装目录由脚本位置反推、
覆盖项取环境变量），换用户 / 换系统 / 换安装位置结果都会变。其中 `NOOVA_CONFIG_DIR`
是**显式指令，读写两侧一视同仁**：设了它就不会因为家目录存在旧配置而改道。
**引导块与 `doctor` 里显示的「凭据保存位置」就是解析结果**，请直接引用，不要另外拼路径。

**基础地址固定为线上部署域名 `https://noova.vip`（备用 `https://noova.live`）**，不要指向本地开发环境。
若用户粘贴的内容里带了本机/内网地址，`setup` 会**拒绝写入**并给出正确写法——
照提示用 `https://noova.vip` 重跑即可，**不要**改成强写。
若用户**有意**要配自建部署地址，只能通过显式 `--base-url <地址>` 指定（见铁律 11）。

**只在确实缺少 Key 时**才走下面的流程。不需要事先检查：脚本会在生成前自动拦截并打印引导块。

1. 引导块（脚本输出，含 ASCII logo、**API 直达地址**与控制台链接、两步操作）
   —— **原样转述给用户**，不要改写，也不要省略地址。
2. 请用户**把 Key 粘贴到对话里**（纯 `sk-…`、`api_key=`、`Bearer …`、JSON、
   甚至整段随手复制的文本都能解析）。
3. 执行自动配置（**优先 `--stdin`**，Key 不落 shell 历史）：

```bash
printf '%s' "<用户粘贴的原始内容>" | python3 <SKILL_DIR>/scripts/noova_key.py setup --stdin
```

（等价写法 `setup "<内容>"` 或 `setup --api-key <内容>`；仅在管道不便时使用。）

`setup` 会解析出 Key 与地址 → **联网校验**（专用校验接口）→ **校验通过才写盘** → 只回显 Key 前缀。
校验失败**不写入**并给出原因；用户确认要强写时可加 `--force`。
用户只给了地址时会先落盘地址，并提示继续索要 Key。
文本里出现了本机/内网地址时**直接拒绝**（不会偷偷改成官方域名），
出现了其它公网地址时**忽略并提示**。

需要排查时才显式检查（**都不是必需的前置步骤**）：

```bash
python3 <SKILL_DIR>/scripts/noova_key.py status --json   # configured: true|false
python3 <SKILL_DIR>/scripts/noova_key.py doctor          # 九项自检：Python/解释器路径/依赖脚本/
                                                        #   配置目录可写/配置文件可解析/Key 状态/
                                                        #   地址是否为线上域名/地址可达性/Key 有效性
```

> **不要把 Key 完整回显给用户**；`status` / `setup` 的输出已做过前缀处理，直接转述即可。
> Key 只保存在本机，脚本不会上传到任何地方；文件按 0600 **原子**写入
> （先写临时文件再替换，中断不会留下半截配置；Windows 上权限位依赖用户目录 ACL）。

## 命令地图

所有生成类操作都通过同一个脚本完成：`python3 <SKILL_DIR>/scripts/noova_media.py <子命令>`。

| 子命令 | 用途 | 关键参数 |
|---|---|---|
| `form` | **交互问答板**：产出「类型 / 模型 / 参数」三级面板，供 agent 弹给用户逐级确认 | 无参数=类型面板；`--type <类型>`=模型面板；`--code <模型编码>`=参数面板；`--json` |
| `models` | 列出当前可用模型：按类型分区，每条含模型编码 / 协议 / 状态 / 计价；**同名多线路的会在该行下方给出线路子行** | `--type text\|image\|video\|audio`、`--online`、`--json` |
| `params` | **查某模型的参数契约**（JSON，推荐给 agent 消费） | `--code <模型编码>`、`--json` |
| `model` | 查看单个模型的完整契约（调用方式 + 计费 + 参数契约） | `--code <模型编码>`、`--json` |
| `image` | 生成图片（任务型/异步，默认轮询到终态） | `--prompt`、`--model`、`--param k=v`、`--ref-image`、`--no-wait`、`--max-wait`、`--no-credit` |
| `video` | 生成视频 | 同上 + `--ref-video` / `--ref-audio` |
| `audio` | 生成音频 | `--prompt`、`--model` |
| `chat` | 文本生成（同步，支持流式） | `--prompt`、`--model`、`--system`、`--stream`、`--protocol`、`--image`、`--show-usage` |
| `task` | 查询一次任务状态（不轮询）；失败原因在响应体 `error` | `--id <任务ID>`、`--type`、`--timeout`、`--json` |
| `wait` | 把已创建的任务轮询到终态（配合 `--no-wait`） | `--id <任务ID>`、`--type`、`--max-wait` |
| `upload` | 上传本地文件，得到可被生成接口引用的地址 | `--file <路径>`、`--host auto\|thirdparty-a\|thirdparty-b\|platform` |
| `credit` | 查询积分余额 | `--json` |

**过程信息（`[提交]` / `[轮询]`）走 stderr，结果与计费反馈走 stdout**；
`--json` 时 stdout **只有可解析的 JSON**（含 `--no-wait`），信息块改走 stderr。
`--quiet` 只抑制人读信息块与自动选模型提示，**不影响结果 URL 与 `--json` 的 JSON 输出**。

**退出码语义**（agent 按码判断，与人类模式的文字一致）：
`0` = 成功；`1` = 业务失败（Key 无效 / 任务失败 / 没有产出结果）；`2` = 配置或用法错误
（地址不合法、输入无法识别、`--max-wait ≤ 0`）。**`--json` 与人类模式的退出码完全相同。**

> 「任务完成但没有结果地址」和「文本响应里没有正文」都算**没有产出**，退出码为 1——
> 不要把它们当成成功交付。

配置类操作走 `scripts/noova_key.py`：

| 子命令 | 用途 |
|---|---|
| `status` | 检查配置状态（未配置时打印引导块；`--json` 给 agent 消费） |
| `guide` | 打印首次使用引导（含 API 直达地址与控制台链接） |
| `setup` | **一键配置**：解析用户粘贴的文本 → 联网校验 → 写盘（推荐入口） |
| `save` | `setup` 的兼容别名（写盘但不联网校验） |
| `verify` | 联网校验已保存的 Key 是否可用（地址不是线上域名时**不发任何请求**，退出码 2） |
| `doctor` | 环境自检（九项）：Python / 解释器路径 / 依赖脚本 / 配置目录可写 / 配置文件可解析 / Key 状态 / 地址是否为线上域名 / 地址可达性 / Key 有效性 |
| `base` | 查看或设置 API 基础地址（地址不可用时退出码非零） |
| `clear` | 清除已保存的 Key（**清掉全部**配置来源，不只最高优先级那个） |

生成类命令的**通用能力**：
- `--model` **必须由用户选定后显式传入**（见「标准工作流 §1」）。省略时脚本会退化为"自动选择该类型下
  第一个上线模型"并把所选模型打印到 stderr——这只允许在用户已明确授权"你来挑"时使用，
  **不得**作为默认路径。
- **模型编码 / 线路名可能含空格**（生产实测 `Demo Image 2.5 flare`、`Demo Image 2.5 sunburst`、
  `Demo Vision 2.1` 等）。拼命令时给 `--model` / `--code` 的值**加引号**，
  否则 shell 会把编码拆成多个参数、脚本按用法错误退出。
- `--param 名称=值` 可重复，用于传模型参数（名称与取值**必须**来自 `params` 查询结果）。
- `--ref-image/--ref-video/--ref-audio` 可传 URL，**也可传本地文件路径（脚本会自动上传）**。
  字段名按该模型的**运行时参数契约**推导，不写死。
- `--json` 输出原始结构（非公开域名已替换；媒体 URL 原样保留）；默认输出人类可读摘要（含**全部**结果 URL）。
- `--no-wait` 只提交任务并返回任务 ID；之后用 `wait --id` 续等、`task --id` 查一次。
- `--max-wait` 必须是**正数**；给 0 或负数会直接报错（不会静默套用默认值）。
- 创建任务与文本生成属于**计费请求，不会自动重试**（避免重复扣费）；限流重试只用于查询类请求。

## 标准工作流

### 1. 用户要生成（最常见）——**先弹问答板，再生成**

用户说出「生图 / 生视频 / 生音频 / 生文本」时，**不要直接拼生成命令**。
必须先把选择权交给用户：用 `form` 依次产出三级面板，逐级让用户确认。

```bash
# 第 1 步：类型面板（用户还没说清要生成哪一类时）
python3 <SKILL_DIR>/scripts/noova_media.py form --json

# 第 2 步：模型面板（类型定了之后）——用户没指定模型时必须走这一步
python3 <SKILL_DIR>/scripts/noova_media.py form --type image --json

# 第 3 步：参数面板（模型定了之后）——参数一律以这个面板为准
python3 <SKILL_DIR>/scripts/noova_media.py form --code <模型编码> --json
```

三级面板的 `step` 分别是 `1 / 2 / 3`，`panel` 分别是 `type_picker / model_picker / param_panel`。
**把面板弹给用户，不要替用户选。** 宿主 agent 若提供交互式选项 / 提问工具（问答板、选项卡），
就用它在面板数据上向用户提问；没有该工具时，把面板内容整理成清单请用户回复。

每级的硬性要求：

1. **类型面板**（`panel=type_picker`）：列出有在线模型的类型，每项含 `count`（模型数）与 `eta`（典型耗时）。
   用户已经明说了类型就跳过这一步。
   - 某个类型**当前没有可用模型**时，面板不会列出它；用户点名要这一类（例如"生音频"）而该类型为空时，
     直接跑 `form --type audio --json`，把 `empty_note` 如实转告用户（当前确实没有可用的音频模型），
     并说明现在能做哪些类型。**不要**编造模型名，也**不要**含糊地答应"正在生成"。
2. **模型面板**（`panel=model_picker`）：列出该类型**全部在线且编码非空**的模型。
   **未明确指定模型时，必须先让用户选完模型，才能进入参数选择。**
   - 每一项都要把 **模型编码、`line_label`（线路）与 `credit_estimate`（近似消耗）一起给出**：
     同名模型可能有多条线路（编码不同、价格不同），只报名字用户无法区分。
   - **同名多线路时，用户选模型必须同时选线路**：`line_legend` 非空即说明该类型存在同名模型，
     展示时按 `family` 分组，同一族的几条线路并排列出（每条给出 `line_label` + `model_code` + `credit_estimate`），
     让用户明确指定**哪一条线路**。用户只报了名字没报线路时，回问是哪一条，**不要替他挑**。
   - `credit_estimate` 是**近似**值（选线路阶段参数还没定）。`range`/`usage` 形态照原文转述，
     **不要**把区间说成一个确定数字；参数定好后用 `params` 里的精确价再报一次。
   - `duplicate_note` 非空时一并转告用户。
   - `unavailable` 里是**当前不可选**的模型：平台未给调用编码的，或**维护中**的；
     照实说明原因（维护中写明「暂不可调用」），**不要**把它们列进选项。
   - **必须逐条注明状态**：`options[].status` / `unavailable[].status` 给出中文状态
     （`在线` / `维护中`），展示时一并转述——用户要能分清哪些现在能用、哪些只是挂着的。
   - 非 `online` / `maintenance` 的状态（`test` / `deprecated` 等）不会出现，取数入口已过滤。
   - 用户若已指定模型，用 `form --code <该模型编码>` 直接进参数面板（参数仍要确认）。
     **该命令要求编码唯一**：只给显示名且命中多条线路时，脚本会报错并列出候选（编码 + 线路 + 计价），
     把候选转给用户让他选一条，**不要**换成别的命令硬猜。
3. **参数面板**（`panel=param_panel`）：**只基于用户已选中的这个模型**，字段来自该模型的运行时参数契约（`params[]`）。
   - `fields[].kind` 决定这一项怎么问：
     - `choice` —— 多选一，把 `options` 全列出来让用户挑。
     - `fixed` —— **只有一个取值：要显示给用户看，但不可选**（`note` 已写明"必须使用它，无需选择"）。
     - `number` —— 数值区间（`range` 给出上下限），让用户给一个数。
     - `text` / `list` —— 自由填写；`list` 可传多个，支持公网 URL 或本地文件路径。
   - `required: true` 的项**必须**有值，缺了脚本会拒发请求。
   - `hidden_fields` 是脚本自动处理的字段（如 `model`），**不要拿去问用户**。
   - `fields` 为空时按 `empty_note` 说明该模型无额外参数，只需提示词。
4. **确认后立即生成**：把参数面板给用户看时，**必须**同时给出面板里的 `confirm_hint`
   （原文：`确认后立即开始生成`），让用户清楚"再确认一下就真的开始生成、开始扣费了"。
   用户确认后，按面板里每个字段的 `cli` 拼命令：

```bash
python3 <SKILL_DIR>/scripts/noova_media.py image --prompt "一只戴墨镜的猫" --model <用户选定的编码> \
        --param <参数名>=<用户选的值>
```

**不要**在用户确认前执行生成命令；**不要**替用户填默认参数后直接生成。
用户明确说"你来决定 / 随便"时才可由 agent 代选，但**选了什么必须回报**（模型编码、计价、各参数取值）。

拿到输出后：把**每一个**结果 URL 都交给用户，不要只给第一个（一次可能生成多张）。
输出末尾的「本次扣减 / 剩余积分」一并转述，并说明扣减口径是**账户余额差值**。

想先让用户知道要等多久：用面板里的 `eta`（与「向用户报时间与积分」一节同口径）。

> **必填参数会被预先拦下**：部分模型的参考素材/时长/比例本身就是必填（以运行时参数契约为准）。
> 缺了它脚本**不会发出请求**，而是直接报"有必填参数未提供"并给出该用哪个选项补——
> 这时回到参数面板补齐，不要反复重试同一条命令。

### 2. 用户咨询"有哪些模型 / 这个模型有什么参数"

用户用自然语言问「你们能生成什么 / 有哪些模型 / 支持什么类型 / 这个模型有什么参数」时，
**不要凭记忆回答**，先拉运行时数据，再用同一种结构复述。最省事的入口是问答板：

```bash
python3 <SKILL_DIR>/scripts/noova_media.py form --json             # 有哪些类型（含各自模型数与典型耗时）
python3 <SKILL_DIR>/scripts/noova_media.py form --type video --json # 该类型有哪些模型（含计价）
python3 <SKILL_DIR>/scripts/noova_media.py models --type video      # 人读版清单
python3 <SKILL_DIR>/scripts/noova_media.py params --code <模型编码> --json
```

`models` 的输出结构是 **类型分区**，每条给出：模型编码、名称、协议、状态、计价；
**状态只有两种对外可见：`在线`（可调用）与 `维护中`（不可调用）**，回答时必须逐条注明。
**同名多线路的模型会在该行下方多出一行 `↳ 线路：<线路名> ・ <近似消耗>`**（每条线路一行）。
回答"有哪些模型"时保持这个结构，并把**模型编码、线路与计价**一并给出
（同名模型编码不同、线路不同、价格不同，只报名字无法区分）。

`params --json` 给出：模型编码、类型、状态、能力、支持的协议、调用方式（同步/任务型/是否支持流式）、
**典型耗时与默认等待上限**（`invocation.eta_seconds` / `invocation.default_max_wait`）、
**计费**（`billing`，含 `surchargeRules` 加收规则）、积分换算（`credit_per_yuan`），
以及 `params[]`（`name` / `type` / `required` / `label` / `values` / `valueLabels` / `min` / `max` / `minItems` / `maxItems`）。
回答用户时**只用这份数据**：

- `values` 是**平台配置的**可选值数组，直接照抄；`valueLabels` 是取值对应的中文标签，一并给出。
- `values` 为空 → 该参数是**自由文本**，没有固定取值，**任何情况下都不要编造枚举**。
- 数值型参数给出 `min`~`max`；数组型给出 `minItems`~`maxItems`（项数）。
- 契约里**没有 `description`**：不要向用户声称"某个参数的说明里写了什么"。
- 报价时把 `billing` 与 `surchargeRules` **一起算**，并用 `credit_per_yuan`（100 积分 = 1 元）换算人民币。
- `line`（线路）是**运营手填的自由文本**，**只是给用户看的通道名**。
  **绝不可**用线路文本里的数字计价——生产实测 `demo-video-rt` 的线路写 `-6/个`，真实单价是 500 积分/次（5 元）。
  计价真值**只有** `billing` 与 `surchargeRules`。

### 3. 文本生成

```bash
python3 <SKILL_DIR>/scripts/noova_media.py chat --prompt "为新品咖啡写一句品牌标语"
python3 <SKILL_DIR>/scripts/noova_media.py chat --prompt "写一段 200 字介绍" --model demo-text-5.5
python3 <SKILL_DIR>/scripts/noova_media.py chat --prompt "继续写" --stream      # 流式
python3 <SKILL_DIR>/scripts/noova_media.py chat --prompt "看图说想法" --image ./photo.png   # 多模态输入
```

`chat` 默认按模型主协议走**原生路由**（OpenAI → `/v1/chat/completions`，Anthropic → `/v1/messages`）。
用户明确说"用 Anthropic/OpenAI 格式调用"时，加 `--protocol anthropic|openai` 让路由与报文格式显式对齐。
`--protocol` 可选 `auto|openai|anthropic|sampleprot|responses`；模型不支持所请求的协议时**发请求前**即报错并列出可用协议。

文本生成同样**先弹问答板再生成**：`form --type text --json` 让用户选模型 → `form --code <该编码> --json` 看参数面板。
文本模型的面板里 `extra_cli_options` 会列出 `--system` / `--stream` / `--max-tokens` / `--temperature`，
它们是调用选项而**不是模型参数**，别当成参数问用户。

### 4. 长任务 / 超时续查

**脚本打印「等待超时……不是失败」时，任务通常仍在跑**——不要告诉用户"生成失败"：

```bash
# 续等到终态（--type 同时决定默认等待上限）
python3 <SKILL_DIR>/scripts/noova_media.py wait --id <任务ID> --type video
# 只想查一次当前状态（失败原因在响应体 error 字段）
python3 <SKILL_DIR>/scripts/noova_media.py task --id <任务ID>

# 或者一开始就不等：提交后立刻拿到任务 ID，稍后再取
python3 <SKILL_DIR>/scripts/noova_media.py video --prompt "..." --no-wait
```

## 调用模式：同步 / 异步（任务型）/ 流式

**同步与异步的差别（向用户解释时照这张表说）：**

| | 同步（文本模型） | 异步/任务型（图像 / 视频 / 音频） |
|---|---|---|
| 提交后 | 一次请求**直接返回正文**，连接一直占用到结果返回 | 立即返回**任务 ID**（`status` 可能是 `running`，也可能直接 `succeeded`） |
| 取结果 | 无需再请求 | 用任务 ID 调 `POST /v1/content` 查询/轮询到终态 |
| 中途能做什么 | 只能等（或改流式） | 可以不等：`--no-wait` 提交后先做别的，回头 `wait` / `task` 取 |
| 典型耗时 | 1–10 秒出首字符 | 图像 30 秒–5 分钟；音频 30 秒–10 分钟；视频 1 分钟–1.5 小时 |
| 失败怎么看 | HTTP 非 2xx + 响应体 | HTTP 非 2xx + 响应体；**任务失败**则终态 `status=failed` 且响应体 `error` 字段给出原因 |
| 流式 | `--stream`（SSE，边生成边输出） | 不支持 |

**几个必须记住的点：**

- **任务型也可能直接返回结果**（`status: succeeded` + URL），脚本对两种情形都自动处理，调用方无需区分。
- **等待超时 ≠ 失败**：脚本的等待上限到了会打印任务 ID 并提示续查（`wait --id` / `task --id`），
  任务此时通常仍在跑。**不要**因此告诉用户"生成失败"。
- **状态词表**（词表外或缺失一律按「处理中」继续等，绝不当成失败）：
  - 未完成：`NOT_START` / `queued` / `submitted` / `in_progress` / `processing` / `running` / `unknown`
  - 成功：`succeeded` / `success` / `completed` / `complete` / `finished`
  - 失败：`failed` / `failure` / `error` / `canceled` / `cancelled` / `expired`
  - `task --id` 遇到失败态会打印失败原因并返回**退出码 1**，**不会**再说「仍在处理中」。
- **文本模型走协议原生路由**：OpenAI → `POST /v1/chat/completions`；Anthropic → `POST /v1/messages`
  （`max_tokens` 必填）。`chat --protocol auto|openai|anthropic|sampleprot|responses` 可显式指定，
  模型不支持时**发请求前**即报错。**平台当前不对外提供 SampleProt 协议**（实测 0 个模型声明；
  `sampleprot-*` 命名模型走 `openai`/`anthropic`），该分支仅为契约驱动的前向兼容，正常不会触发；
  本 skill 不做 sampleprot 流式。
- **图像/视频/音频走统一入口**：`POST /api/v1/invoke`；创建后用 `POST /v1/content` 查询/轮询。
- 默认等待上限按类型给足（图像 600s / 音频 1200s / 视频 5400s），可用 `--max-wait` 覆盖；
  `--timeout` 对**创建、轮询、单次查询**都生效。
- **`wait` 只报当前余额，不报「本次扣减」**：任务是在更早的调用里创建的，扣费发生在那一刻，
  在这里取「生成前快照」已经晚了，差值恒为 0——那会让人以为付费生成是免费的。
  要核对本次扣减，看**创建任务那次**的输出。

细节与端到端报文示例见 `references/protocols.md`。

## 向用户报「时间」与「积分」（每次都按这个口径）

**时间**：给**区间**，不给承诺。

| 类型 | 典型耗时（用于向用户说明） |
|---|---|
| 文本 | 1–10 秒出首字符（`--stream` 可边生成边输出） |
| 图像 | 30 秒 – 5 分钟 |
| 音频 | 30 秒 – 10 分钟 |
| 视频 | 1 分钟 – 1.5 小时 |

视频等长任务：先告诉用户大概区间；需要长时间等待时用 `--no-wait` 提交，
拿到任务 ID 后再 `wait --id <ID> --type video`，避免一条命令长时间占住终端。

**积分**（生成结束脚本会自动给出，照抄即可；下面是单图任务的**逐字**输出）：

```
[完成] 生成成功，共 1 张图片

  模型    ：Demo Image 2（demo-image-2-t）
  计价    ：4 积分/次
  任务 ID ：1234567
  本次扣减：4 积分（账户余额 100.19 → 96.19）
  剩余积分：96.19（永久 96.19、限时 0）

结果地址（请原样交付给用户）：
  1. https://…（每个结果都要给，不要只给第一个）
```

数量词随类型变：图片论「张」（`共 2 张图片`），视频/音频论「个」（`共 1 个视频`）。
文本任务额外多一行 `用量：输入 N tokens / 输出 M tokens`（`--show-usage` 时必给）。
余额明细括号只在接口返回对应字段时出现；余额查询失败则整两行省略（不会中断生成）。

- 「本次扣减」的口径是**账户余额差值**（生成前 / 终态后各查一次 `GET /api/v1/credit`），
  是唯一可核对的真实消耗；向用户解释时要说明这个口径。
  **余额没有变化时脚本会说「本次未产生扣费」，不会写「本次扣减：0 积分」**——后者会被读成免费。
- 「计价」是该模型的公开单价（含按次 / 按 token / 按用量三种形态）。**报价只引用它，不要自己算**。
- 想省掉这两次余额查询（例如批量调用）：加 `--no-credit`。
- 只想单独看余额：`credit`（只发一次请求）。余额不足时平台返回 **402**，脚本会提示充值。

## 上传素材（参考图 / 首帧 / 参考视频音频）

```bash
python3 <SKILL_DIR>/scripts/noova_media.py upload --file ./photo.png          # 默认自动选通道
python3 <SKILL_DIR>/scripts/noova_media.py upload --file ./clip.mp4 --host platform   # 指定通道
```

默认按 **第三方图床一 → 第三方图床二 → 平台存储通道** 顺序尝试，任一成功即返回（脚本会做可达性校验）。
平台存储通道需要 API Key，未配置时自动跳过。

> ⚠️ **前两个是第三方免费图床，不是平台自有服务**：文件存储与访问由第三方提供，可能出现文件被清理、
> 链接失效、内容不校验等问题。**禁止上传敏感或重要数据**；如需可靠存放，用 `--host platform`（需 Key）。
> 逐通道失败原因会打印出来，便于判断是限流还是格式问题。
>
> 其中**第三方图床一依赖浏览器指纹头**（对方站点对程序化直调做了同源校验），属对方随时可能收紧的
> 非稳定通道；默认顺序是它的历史行为，实际选用请以 `--host` 显式指定为准。

细节见 `references/api-contracts.md` 的「素材上传」一节。

## 安全边界（必须遵守）

- **禁止**访问或以任何形式推断：管理端接口、回调接口、数据库、对象存储地址、服务商名称/密钥、内部文件路径。
- **禁止**把模型清单、参数契约、域名写死进代码或回答；一切以运行时公开接口返回为准。
- **禁止**在用户可见输出中出现：完整 API Key、鉴权头、内部域名、内部错误原文、堆栈。
- 后端返回的**错误信息已由平台脱敏**，脚本还会再做一层域名替换（非公开域名 → `media.example.com`）。
  **这一层对 `--json` 输出同样生效**（媒体 URL 字段除外），所以 JSON 可以放心转述给用户。
  向用户转述错误时保持中性（如"服务暂时不可用，请稍后重试"），不要解释内部原因。
- 脚本**不会打印任何 traceback**：未预期的内部错误只会输出一行中性提示（含 skill 绝对路径与
  调用栈的堆栈信息一律不外泄）。需要堆栈排障时由开发者显式设 `NOOVA_DEBUG=1`。
- 校验 Key 有效性时**只允许**用 `POST /api/v1/gateway/validate-key` 或 `GET /api/v1/credit`；
  模型清单类接口不校验鉴权，不得用于此目的。
- 生成结果的**媒体 URL 是功能字段**，按原值交付；但**不要**把结果 URL 里的域名当成"接口"去讨论或复用到其它请求。

## 参考文件（按需加载）

| 文件 | 内容 |
|---|---|
| `references/api-contracts.md` | 公开端点、鉴权方式、请求/响应结构、错误码、限流 |
| `references/protocols.md` | 同步/任务型/流式三种调用模式 + OpenAI/Anthropic/SampleProt 协议适配与报文样例 |
| `references/parameters.md` | 参数查询方法与参数语义（如何回答"这个参数是什么/能填什么"） |
| `references/troubleshooting.md` | 常见错误现象 → 原因 → 处理方式 |
| `references/host-compatibility.md` | 不同 agent / 操作系统的运行时、权限、网络边界，以及**装到哪**（分发目标） |
| `references/changelog.md` | 版本变更记录（含「版本号三处一致」不变量） |

## 脚本说明

| 文件 | 职责 |
|---|---|
| `scripts/noova_key.py` | 配置与引导：`status` / `guide` / `setup` / `save` / `verify` / `doctor` / `base` / `clear` |
| `scripts/noova_media.py` | 交互问答板（`form`）、模型发现、参数查询、生成、轮询、上传、余额（CLI 主入口） |
| `scripts/noova_upload.py` | 素材上传：第三方图床一/二 + 平台存储通道（自动降级、可达性校验） |
| `scripts/noova_common.py` | 共享基础设施：**地址守卫**、**脱敏**、终端排版（其余三个脚本都引用它） |

本 skill 只含标准结构（`SKILL.md` + `scripts/` + `references/`）；不含任何安装/分发逻辑——
这些属于宿主与分发方的职责，不要在本目录里找安装器。

> **`noova_common.py` 是必需文件**：缺少它其余脚本无法导入。若手工复制本 skill，
> 请**整个目录一起复制**，不要只挑 `noova_key.py` / `noova_media.py`。
