# 调用模式与协议适配

本文件回答两个问题：**这个模型该怎么调（同步/任务型/流式）**，以及**请求与响应是什么格式**。

> 下文命令里的 `python3` 是**占位写法**。不同系统上解释器名不同（Windows 官方发行版是 `python`），
> 先取本机可用的那一个再执行，详见 `SKILL.md` 的「运行环境」一节。

---

## 1. 如何判断一个模型属于哪种调用模式

**唯一判断依据是运行时公开接口**，不要凭模型名猜：

```bash
python3 <SKILL_DIR>/scripts/noova_media.py params --code <模型编码> --json
```

关键字段：

| 字段 | 取值 | 含义 |
|---|---|---|
| `model_type` | `text` | 同步返回（无 `poll`） |
| | `image` / `video` / `audio` | 任务型（`poll` = `POST /v1/content`） |
| `invocation.mode` | `sync` / `task` | 由 `model_type` 决定，脚本已解析好 |
| `invocation.stream_supported` | `true` / `false` | 该模型**所选主协议**是否支持流式（平台当前只会出现 `openai` / `anthropic`）；`routes[].stream_supported` 给出逐协议结果 |
| `invocation.eta_seconds` | 例：`[60, 5400]` | **典型耗时区间**（秒），供向用户说明用（非硬上限） |
| `invocation.default_max_wait` | 例：`5400` | 脚本默认等待上限（秒），可用 `--max-wait` 覆盖 |
| `protocols` | 例：`["anthropic", "openai"]` | 该模型支持哪些协议调用（第一项为主协议） |
| `primary_protocol` | `openai` / `anthropic` | 模型原生协议（平台当前实测仅此两类） |
| `billing` | 见 `parameters.md` §5 | 计价口径（按次 / 按 token / 按用量） |

> 图像/视频/音频虽为"任务型"，但**可能直接返回结果**（`status: succeeded` + URL），
> 也可能只返回任务 ID。脚本对这种两态都做了处理，无需调用方区分。

### 典型耗时（向用户说明用；脚本会按类型自动给出）

| 类型 | 典型耗时 | 默认等待上限 |
|---|---|---|
| 文本 | 1–10 秒出首字符 | —（同步，受 `--timeout` 约束，默认 180s） |
| 图像 | 30 秒 – 5 分钟 | 600 秒 |
| 音频 | 30 秒 – 10 分钟 | 1200 秒 |
| 视频 | 1 分钟 – 1.5 小时 | 5400 秒 |

口径说明：**区间 ≠ 承诺**，实际耗时随模型、时长参数、排队情况变化。这两组数字的用途是
① 让 agent 先把预期告诉用户（不要让人以为卡死）；② 让默认等待上限覆盖长任务，
避免视频这类任务被过短的上限截断而误报「超时 / 失败」。

### 同步 vs 异步 一览

| | 同步（文本） | 异步 / 任务型（图像 / 视频 / 音频） |
|---|---|---|
| 提交后 | 连接一直占用到正文返回 | 立即返回任务 ID（也可能直接给结果） |
| 取结果 | 无需再请求 | 用任务 ID 轮询 `POST /v1/content` |
| 不想等 | —— | `--no-wait` 提交后拿 ID，回头 `wait` / `task` |
| 失败信息位置 | HTTP 非 2xx 的响应体 | HTTP 非 2xx 的响应体；**任务失败**在终态响应体的 `error` 字段 |
| 流式 | 支持（SSE） | 不支持 |
| 计费 | 按 token（含最低扣费），走 `--show-usage` 查看用量与余额 | 按次 / 按用量（按秒等），生成结束脚本自动给出扣减与余额 |

---

## 2. 模式 A：同步（文本模型）

文本模型走**该模型主协议的原生官方路由**（不做跨协议转换），所以报文按协议官方字段构造。
`chat` 默认自动选主协议，`--protocol` 可显式指定。

### OpenAI 协议模型（`protocols` 含 `openai`，最普遍）

```http
POST /v1/chat/completions
Authorization: Bearer <API_KEY>
Content-Type: application/json

{
  "model": "<文本模型编码>",
  "messages": [
    { "role": "system", "content": "<可选：系统提示>" },
    { "role": "user", "content": "写一句品牌标语" }
  ],
  "temperature": 0.7,
  "max_tokens": 1024
}
```

响应（OpenAI `chat.completion` 形态）：

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "<模型编码>",
  "choices": [
    { "index": 0, "message": { "role": "assistant", "content": "……正文……" }, "finish_reason": "stop" }
  ],
  "usage": { "prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46 }
}
```

**正文取值路径**：`choices[0].message.content`。该字段可能是字符串，也可能是分段数组
（`[{"type":"text","text":"..."}]`）；脚本两种都兼容。

### Anthropic 协议模型（`protocols` 含 `anthropic`，Claude 系）

```http
POST /v1/messages
Authorization: Bearer <API_KEY>          # 亦兼容 x-api-key
anthropic-version: 2023-06-01
Content-Type: application/json

{
  "model": "<文本模型编码>",
  "max_tokens": 1024,                     # 必填，缺失会被上游拒绝
  "system": "<可选：系统提示>",              # 注意：不是 messages 里的 system 角色
  "messages": [{ "role": "user", "content": "写一句品牌标语" }]
}
```

响应：

```json
{ "id": "msg_...", "type": "message", "role": "assistant",
  "content": [{ "type": "text", "text": "……正文……" }],
  "stop_reason": "end_turn", "usage": { "input_tokens": 12, "output_tokens": 34 } }
```

**正文取值路径**：`content[].text` 拼接（可能有多个块，其中也可能含非文本块，跳过即可）。

### SampleProt 协议模型（**平台当前未提供，以下仅存档官方协议形状**）

```http
POST /v1beta/models/<模型编码>:generateContent
Authorization: Bearer <API_KEY>          # 亦兼容 x-sample-api-key
Content-Type: application/json

{ "contents": [{ "role": "user", "parts": [{ "text": "写一句品牌标语" }] }],
  "systemInstruction": { "parts": [{ "text": "<可选>" }] },
  "generationConfig": { "maxOutputTokens": 1024, "temperature": 0.7 } }
```

**正文取值路径**：`candidates[].content.parts[].text`。

> **注意**：平台当前**不对外提供 SampleProt 协议**（实测 2026-10-09：34 个模型 0 个声明 `sampleprot`；
> `sample-3.7-flash` 等 SampleProt 系模型的契约协议是 `openai` / `anthropic`）。本节描述的是 SampleProt
> **官方协议形状**，仅供解析器前向兼容参考，现有模型用不到。
> 三种协议的解析脚本都已内置，且对**不可用协议**会在发请求前即报错（不发必然失败的请求）。
> 具体某模型支持哪些协议，用 `params --code <编码> --json` 的 `protocols` 字段确认。

---

## 3. 模式 B：任务型（图像 / 视频 / 音频）

### 第 1 步 · 创建

```http
POST /api/v1/invoke
Authorization: Bearer <API_KEY>
Content-Type: application/json

{
  "model": "<模型编码>",
  "prompt": "一只戴墨镜的猫",
  "<参数名>": "<取值>"
}
```

响应二选一：

```json
// (a) 直接产出
{ "id": "2085...", "status": "succeeded",
  "result": "<结果URL>", "results": [{ "url": "<结果URL>", "content": "<结果URL>" }],
  "imageUrl": "<结果URL>" }

// (b) 需轮询
{ "id": "2085...", "status": "running", "task_id": "2085..." }
```

### 第 2 步 · 轮询（仅当拿到 `running`）

```http
POST /v1/content
Authorization: Bearer <API_KEY>
Content-Type: application/json

{ "id": "2085..." }
```

| `status` | 说明 | 之后怎么办 |
|---|---|---|
| `running` | 处理中（可能带 `progress` 百分比） | 等 5–10 秒再查 |
| `succeeded` | 终态成功 | 取 `results` / `result` 交付 |
| `failed` | 终态失败 | 把 `error` 中性转述给用户 |

> 平台文档给出的状态词表更宽：未完成态还包括 `NOT_START`、`queued`、`submitted`、
> `in_progress`、`processing`、`unknown`；成功态还包括 `completed`。
> **词表之外的任何状态都按"未完成"继续轮询**，不要当成失败。

> **等待超时 ≠ 任务失败**。超时后任务通常仍在跑：请用返回的任务 ID 继续
> `wait --id <ID>` 或 `task --id <ID>`，**不要**因为本地超时就告诉用户"生成失败"。

### 结果 URL 的取法（不要只取第一个）

- `results`：可能是**数组**（每项 `{ url, content }`），也可能是**单个字符串**——
  平台文档两种都出现过。脚本两种都收集，**一次生成多张图时必须全部交付**。
- `result` / `imageUrl` / `videoUrl` / `audioUrl`：首个产物的便捷字段。
- 结果 URL 为可直接访问的媒体地址，**没有时效限制**，原样交给用户即可。
- 响应可能被统一包裹成 `{"code":200,"data":{…},"message":""}`，脚本会自动剥离外层再取值。

### 失败、超时与取消：语义要分清

| 现象 | 真实含义 | 该怎么说 / 怎么做 |
|---|---|---|
| 终态 `status: failed` + `error` | **任务真的失败** | 把 `error`（平台已脱敏）中性转述；不要解释内部原因 |
| 创建接口返回 `status: failed` | 创建即失败（多为参数或权限问题） | 先用 `params --code <编码>` 核对参数名与取值；余额不足是 402 |
| 脚本打印「等待超时……**不是失败**」 | 本地等待上限到了，任务仍在跑 | 用任务 ID 续等：`wait --id <ID> --type <类型>`；或 `task --id <ID>` 查一次 |
| HTTP 4xx / 5xx | 请求本身被拒 / 服务暂时异常 | 见 `troubleshooting.md` 的错误码表 |

> 判断「任务是否失败」的**唯一依据是终态 `status`**，不是本地等待时长。
> 词表外的状态（含 `unknown`、缺字段）一律按"处理中"继续轮询。

### 计费与余额反馈（生成结束自动给出）

任务终态后脚本会输出：

```
[完成] 生成成功，共 1 张图片

  模型    ：Demo Image 2（demo-image-2-t）
  计价    ：4 积分/次
  任务 ID ：1234567
  本次扣减：4 积分（账户余额 100.19 → 96.19）
  剩余积分：96.19（永久 96.19、限时 0）

结果地址（请原样交付给用户）：
  1. https://…
```

- 标签列按显示宽度对齐（中文算 2 列）；条数随任务而变——`任务 ID` 在拿到 ID 时才有，
  `用量` 仅文本任务有，余额两行在两次 `GET /api/v1/credit` 都成功时才有。
- 若生成期间余额**不减反增**（充值/返还），第二行改为 `余额变化：+N 积分（…）`，不要报成扣减。

- **「本次扣减」口径 = 账户余额差值**：生成前、终态后各调一次 `GET /api/v1/credit`，
  两者相减。这是唯一可核对的真实消耗（模型标价可能叠加参数加收、按秒计费、缓存命中折扣）。
- 平台的任务响应体**不含**任何积分字段，所以不要试图从响应里找「消耗」；
  报价只引用 `models` / `params` 给出的计价，不要自己算。
- 两次余额查询都**失败即跳过**（不会中断生成）；批量调用想省掉它们：`--no-credit`。
- 文本模型用 `chat --show-usage` 查看 token 用量与同样的扣减 / 余额反馈。

---

## 4. 模式 C：流式（文本模型，SSE）

在模式 A 的**同一条原生路由**上把 `stream` 置为 `true`：

```http
POST /v1/chat/completions          # Anthropic 模型则用 POST /v1/messages
Authorization: Bearer <API_KEY>
Content-Type: application/json

{ "model": "<文本模型编码>", "messages": [{"role":"user","content":"写一段介绍"}], "stream": true }
```

OpenAI 协议路由的 SSE（`Content-Type: text/event-stream`）：

```
data: {"choices":[{"delta":{"content":"你"},"index":0}]}

data: {"choices":[{"delta":{"content":"好"},"index":0}]}

data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}

data: [DONE]
```

Anthropic 协议路由的 SSE 事件形态不同（按事件类型分行，增量文本在 `delta.text`）：

```
event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你"}}

event: message_stop
data: {"type":"message_stop"}
```

取值规则：

- 逐行读取，只处理以 `data:` 开头的行（`event:` 行可忽略）；`data: [DONE]` 表示结束。
- 增量文本：OpenAI 在 `choices[0].delta.content`；Anthropic 在 `delta.text`；SampleProt 在 `candidates[0].content.parts[].text`。
- 用量（若有）在最后一个数据块里。三种方言脚本都已兼容。

命令行：`noova_media.py chat --prompt "..." --stream`（默认直接打印增量文本；
加 `--json` 则逐条输出 SSE 事件原文，**但事件会先过一遍域名脱敏**——
agent 拿到的是可解析的 JSON 行，且不会把上游的内部域名转述给用户）。

> 流式是文本模型的能力。图像/视频/音频为任务型，**不支持** `stream`。

---

## 5. 协议适配：OpenAI / Anthropic / SampleProt

**本 skill 的文本调用默认走协议原生路由**（用户指定 OpenAI 格式就发 OpenAI 路由、Anthropic 格式就发 Anthropic 路由），
请求与响应都是该协议的**原样格式**，不做跨协议转换：

| 协议 | 路由 | 请求体 | 响应正文路径 |
|---|---|---|---|
| `openai` | `POST /v1/chat/completions` | `{model, messages[], stream?}` | `choices[0].message.content` |
| `anthropic` | `POST /v1/messages` | `{model, max_tokens, messages[], system?, stream?}` | `content[].text`（拼接） |
| `sampleprot`（**平台未提供**） | `POST /v1beta/models/<model>:generateContent` | `{contents:[{parts:[{text}]}]}` | `candidates[].content.parts[].text` |

路由选择规则：

- `chat`（不带 `--protocol`）→ 用该模型的**主协议**（契约里的 `protocols[0]`）。
- `chat --protocol openai|anthropic|sampleprot` → 强制走该协议；模型不支持则**发请求前**报错并列出可用协议。

```bash
python3 <SKILL_DIR>/scripts/noova_media.py params --code <模型编码> --json   # 看 protocols 字段
```

补充说明：

- 统一入口 `POST /api/v1/invoke` 存在且对文本模型同样可用（网关会把 Anthropic/SampleProt 响应转换回
  OpenAI 形态），但**本 skill 不用于文本生成**——用户要什么格式就用什么格式的原生路由，少一层转换、语义更直白。
- 图像 / 视频 / 音频**只有**统一入口这一条创建路由（公开文档未声明原生路由），故任务型仍走 `/api/v1/invoke`。
- **平台当前不对外提供 `sampleprot` 协议**（实测 2026-10-09：34 个模型 0 个声明 `sampleprot`；
  `sample-3.7-flash` / `sample-3.8-flash` 等 SampleProt 系模型的契约协议是 `openai` / `anthropic`，
  即“SampleProt 系模型”走的是 OpenAI/Anthropic 报文）。后端虽保留
  `/v1beta/models/<model>:generateContent` 路由（历史 a retired integration 图像模型兜底，当前无模型命中），
  但 skill 不再把它当作可用协议呈现：`--protocol sampleprot` 对现有模型会在发请求前报“不支持”。
  本 skill **明确不做 sampleprot 流式**；该分支仅为契约驱动的前向兼容，正常不会触发。
- 脚本的文本解析器对三种协议的**响应与 SSE 方言都做了兼容**（即使某天响应形态变化也不会解析失败）。

---

## 6. 参考文件（本地文件 → 生成参数）

需要参考图 / 首帧 / 参考视频 / 参考音频时，先把本地文件变成可引用的地址：

```bash
python3 <SKILL_DIR>/scripts/noova_media.py upload --file ./photo.png
# 输出即文件地址，把它传给生成参数；加 --json 可拿到 provider/verified 等元信息
```

上传通道（默认 `--host auto`，按顺序尝试、失败自动降级）：

| 顺序 | 通道 | 说明 |
|---|---|---|
| 1 | `thirdparty-a` | 第三方图床一；需浏览器指纹头（脚本已带）；图片与 ≤20MB 视频 |
| 2 | `thirdparty-b` | 第三方图床二；无需额外头；不校验文件真实内容 |
| 3 | `platform` | 平台自有存储通道；**需要 API Key**（未配置 Key 时自动跳过） |

> 前两个是**第三方免费图床，非平台自有服务**：可能被清理、链接失效、内容不校验。
> **禁止上传敏感或重要数据**。需要可靠存放时用 `--host platform`（需 Key）。

生成命令支持**直接给本地路径**（脚本内部自动完成上传）：

```bash
python3 <SKILL_DIR>/scripts/noova_media.py image --prompt "改成水彩风" \
        --model <模型编码> --ref-image ./photo.png
```

- 平台通道分两步：签发凭证 → 按凭证里的方法与请求头直传（签名头不可改；大文件请在有效期内传完）。
- 各模型接受的参考字段名不同（例如 `referenceImages` / `referenceAudios` / `referenceVideos` 或文档里
  其它名称），**以该模型的参数契约为准**（`params --code <编码> --json` 的 `params[].name`）。
