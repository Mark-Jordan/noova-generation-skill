# NooVa AI 公开 API 契约（用户侧）

> 本文档只描述**对 API Key 调用方开放**的接口。基础域名：`https://noova.vip`（主）/ `https://noova.live`（备用），两者都是**部署在服务器上的官方线上域名**。
>
> ⛔ **基础地址必须是官方线上部署域名**：`DEFAULT_BASE_URL = https://noova.vip`，备用 `https://noova.live`。
> 不得填写 `localhost` / `127.0.0.1` / `192.168.*` / `10.*` / `172.16-31.*` / `dev.*` / `staging.*`
> 等本机、内网或开发环境地址——
> 那些地址只有开发者本机可访问，写进配置后请求必然失败，且现象像"服务故障"。
> 官方域名的 `www.` 写法会被脚本归一到 apex（`www.noova.vip` → `noova.vip`）。
> `setup` / `base` 会在写盘前拒绝这类地址（`noova_media.py` 在发请求前也会再拦一次）。
> 仅当自建线上部署时，才用环境变量 `NOOVA_BASE_URL` 或 `noova_key.py base <https://你的域名>`
> 指向**你自己的线上域名**；本机联调需显式开启 `NOOVA_ALLOW_LOCAL_BASE_URL=1`。
>
> 所有端点都由 `scripts/noova_media.py` 封装；本文件供 agent 理解契约、回答用户问题、排查问题。
> **任何未出现在本文档中的端点一律不得访问。**

## 0. 链路边界：只用对外端点

本 skill **只使用对 API Key 调用方开放的端点**（下表左列）。平台另有面向官网前端控制台的
**对内链路**（需用户登录态），它不属于公开 API 的一部分：本 skill 一律不访问、不推断、
不在文档中展开具体路径与字段。

| 链路 | 面向 | 鉴权 | 端点 |
|---|---|---|---|
| **对外**（本 skill 唯一可用） | API Key 调用方 | `Authorization: Bearer sk-…` | `/api/models/params`、`/api/models`、`/v1/models`、`/api/v1/invoke`、`/v1/content`、`/api/v1/credit`、`/api/v1/gateway/validate-key` |
| 对内 | 官网前端控制台 | 用户**登录 token** | （不适用；本 skill 一律不访问，不在此罗列） |

> **「线路」也不需要访问对内链路**：线路（`line`）已由对外契约 `/api/models/params` 下发（见 §2.1）。
> 需要线路时用对外字段，**不要**为了拿线路去访问任何对内端点。

## 1. 鉴权

| 方式 | 请求头 | 说明 |
|---|---|---|
| 推荐 | `Authorization: Bearer <API_KEY>` | 所有需要鉴权的端点（模型参数 / 生成 / 查询 / 上传 / 余额） |
| 兼容 | `X-API-Key: <API_KEY>` | 同上 |
| 兼容 | `x-api-key: <API_KEY>` | Anthropic 协议的常见写法 |

- API Key 形如 `sk-...`，在官网 `https://noova.vip/api_control`（备用域名 `https://noova.live/api_control`）创建：登录 → 「API 管理」→ 点「创建 API Key」
  → 填密钥名称 → 「确认创建」→ **立即复制完整 Key**（只在创建时展示一次，之后仅显示摘要）。
- Key **仅创建时完整显示一次**，之后不可再次查看；丢失只能重新创建。
- 展示 Key 时只显示前缀（`sk-abc…`），**绝不**完整回显。

> ⚠️ **鉴权与"校验 Key"是两件事**：**所有对外接口都已收口为需要有效 Key**——
> `GET /api/models`（2026-10-09 起）、`GET /v1/models`（2026-10-06 起）、`GET /api/models/params`
> 均缺失 / 无效 / 停用 → 401。
> 判断 Key 是否有效**只能**用 §5 的 `POST /api/v1/gateway/validate-key`（首选）
> 或 `GET /api/v1/credit`（无效 Key → 401）。

## 2. 模型发现

### 2.1 `GET /api/models/params`（鉴权；**本 skill 的主数据源**）

返回**结构化参数契约 + 计费真值**：参数名 / 类型 / 是否必填 / 平台配置的可选值 / 数值区间，
以及把积分算准所需的全部计费字段。可选 `?model=<对外编码>` 只取单个模型（未知编码 → 404）。

响应外层为 `{ "code": 200, "data": { ... }, "message": "..." }`，`data` 结构：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schemaVersion` | int | 契约版本（当前 `1`）；不认识就**必须报错停止**，不能按旧口径报价 |
| `creditPerYuan` | number | 积分↔人民币换算比例，当前 **100**（100 积分 = 1 元） |
| `minTextCharge` | number | 按 token 计费时的**最低扣费**（积分） |
| `models` | array | 模型数组，见下 |

`models[]` 每项：

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | string | **对外模型编码**（请求时原样使用；空串 = 平台未给编码，**不可调用**） |
| `displayName` | string | 展示名。**同名模型真实存在**（同一展示名可能对应多条线路，编码不同） |
| `line` | string | **线路**：同一模型的接入通道名。空串 = 运营未填，展示为「默认」 |
| `modelType` | string | `text` / `image` / `video` / `audio` |
| `status` | string | `online`（在线，可调用）/ `maintenance`（维护中，暂不可调用）。**对外只呈现这两类**：`test` / `deprecated` 等状态在取数入口（`_fetch_public_model_specs`）即被过滤，不会进入任何输出；展示时必须注明是「在线」还是「维护中」 |
| `protocols` | string[] | 该模型支持的协议，**第一项即主协议** |
| `billing` | object | 计费（见 §2.4） |
| `surchargeRules` | array | 参数加收规则（见 §2.4） |
| `params` | array | 参数契约（见 §2.2） |

### 2.2 `params[]` 参数契约

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 参数名，**必须原样**作为请求体字段名 |
| `type` | string | `string` / `number` / `boolean` / `string[]` … |
| `required` | bool | `true` 时必须传，否则平台拒绝（脚本会在发请求前拦下） |
| `label` | string | 该参数的中文名（可为空） |
| `values` | array | **平台配置的**可选值；空数组 = 自由文本 |
| `valueLabels` | object | 取值 → 中文标签（如 `{"16:9": "横屏"}`） |
| `min` / `max` | number | 数值型参数的上下限（仅数值类型出现） |
| `minItems` / `maxItems` | int | 数组型参数的元素个数上下限（仅数组类型出现） |

- **没有默认值**：不传就是没设置（与网页画布不同，画布会替用户补默认值）。
- **没有 `description`**：契约不下发自由文本说明，取值集合就是 `values`。
- `values` 为空 → 该参数是自由文本，**不要编造枚举**。

### 2.3 `GET /api/models`（鉴权）

对外最小清单，用于轻量列举与**连通性探测**（**需要有效 Key**；无 Key → 401）：
`model` / `modelType` / `status` / `priceOnce` / `priceType` / `priceToken`。

### 2.4 计费字段怎么读（**决定报价对不对**）

`billing`：

| 形态 | 字段 | 说明 |
|---|---|---|
| 按次/按时 | `mode`=`per_request` 或 `per_time`，`price`，`unit` | 基础费用 = `price × unit` 积分 |
| 按 token | `mode`=`per_token`，`pricePer1M.{input,output,cacheHit,cacheWrite}` | 单位 = **积分/百万 tokens**；缺的档为 0（不代表免费） |

`surchargeRules[]`（**积分超出基础单价的唯一来源**）：

| 字段 | 说明 |
|---|---|
| `param` | 触发加收的参数名 |
| `match` | `present`（提供了该参数就加收）/ `exact`（等于 `matchValue`）/ `range`（落在 `rangeMin`~`rangeMax`） |
| `charge` | `multiply_by_value`（按取值倍数）/ `fixed`（固定额度） |
| `multiplier` / `base` | 加收 = `base + 取值 × multiplier`（**`base` 缺省按 0 算**） |
| `credits` | `charge=fixed` 时的固定加收积分 |

**读法与红线**：

1. `per_token` → 用 `pricePer1M` 报「输入 X / 输出 Y 积分/百万 tokens」，并说明"以实际用量结算"，
   且**不低于 `minTextCharge`**。
2. `per_request` / `per_time` 且 `price > 0` → 「X 积分/次」（`unit > 1` 时写「X 积分/N 次」）。
3. **`price == 0` → 绝不能报成「0 积分/次」或"免费"**：这表示单价由 `surchargeRules` 按用量决定。
4. **报价必须把 `surchargeRules` 算进去**：用户选定参数后按上述公式合并基础价与加收，
   再把结果转述给用户；只报基础单价会**系统性低估**费用。
5. **换算人民币用 `creditPerYuan`**（当前 100，即 **100 积分 = 1 元**），报钱数时把比例一并说清。

### 2.5 `GET /v1/models`

[OI] 兼容清单：`{ "object": "list", "data": [{ "id": "...", "object": "model", "owned_by": "..." }] }`。
只包含可用 [OI] 协议调用的模型，适合 [OI] 生态客户端做模型发现。

## 3. 生成

**路由怎么选（先看这一句）**：

- **文本模型** → 走该模型**主协议的原生官方路由**：OpenAI 兼容 → `POST /v1/chat/completions`，
  Anthropic → `POST /v1/messages`（见 §3.2）。
- **图像 / 视频 / 音频（任务型）** → 走 `POST /api/v1/invoke`（见 §3.1）。
  平台对这三类模型公开的契约**只声明这一条创建路由**，没有协议原生替代路径。
- 两类都用 §4 的 `POST /v1/content` 查结果（任务型）。

### 3.1 `POST /api/v1/invoke`（图像/视频/音频的创建入口）

任务型模型（图像 / 视频 / 音频）的创建入口。请求体以 [OI] 兼容格式提交，
网关按模型实际协议自动转换请求与响应，调用方无需关心上游差异。

```json
{
  "model": "<模型编码>",
  "prompt": "一只戴墨镜的猫",
  "aspect_ratio": "1:1"
}
```

- `model`（**必填**）：模型编码，取 `/api/models/params` 的 `model`。
- 其余字段为该模型的参数，**参数名与取值必须来自 `params[]` 契约**（`name` / `values` / `min`~`max`）。
  `model` 字段缺失或为空 → `400 请求体缺少 model`。
- **未在 `params[]` 中出现的参数名不会生效**（平台按模型参数映射处理，未知键会被忽略而不报错）。
  因此传参前必须先查 `params`。
- 文本模型也可以调用本入口（`messages` 数组），但**本 skill 的 `chat` 默认走 §3.2 的原生协议路由**。

**响应（图像/视频/音频，任务型）**：

- 已直接产出结果：
  ```json
  { "id": "<任务ID>", "status": "succeeded",
    "result": "<首个结果URL>", "results": [{ "url": "...", "content": "..." }],
    "imageUrl": "..." }
  ```
  （视频为 `videoUrl`，音频为 `audioUrl`。）
  ⚠️ **`results` 的形态并不统一**：部分模型给的是**单个字符串**，例如
  `{ "results": "<结果URL>", "status": "succeeded" }`。脚本对「字符串 / 数组 / 对象」三种形态都做了收集，
  调用方不需要区分。
- 已受理、需轮询：
  ```json
  { "id": "<任务ID>", "status": "running", "task_id": "<任务ID>" }
  ```
- 创建失败：
  ```json
  { "id": "<任务ID>", "status": "failed", "error": "<已脱敏原因>" }
  ```

**流式（仅文本模型）**：请求体加 `"stream": true`，响应为 `text/event-stream`，事件为 [OI] SSE 风格
（`data: {"choices":[{"delta":{"content":"..."}}]}`，以 `data: [DONE]` 结束）。
本 skill 的 `chat --stream` 走的是 §3.2 的原生协议路由，SSE 事件格式随协议而定（脚本对三种方言都兼容）。

### 3.2 协议原生路由（文本模型的默认调用方式）

每个模型支持哪些协议，看 `params --json` 的 `protocols`（**`protocols[0]` 即主协议**）。

| 路由 | 协议 | 请求体要点 | 鉴权头 | 响应正文路径 |
|---|---|---|---|---|
| `POST /v1/chat/completions` | [OI] | `{model, messages[], stream?}` | `Authorization: Bearer` | `choices[0].message.content` |
| `POST /v1/messages` | Anthropic | `{model, max_tokens, messages[], system?, stream?}`（`max_tokens` **必填**） | `Authorization: Bearer`（亦兼容 `x-api-key`） | `content[].text` |
| `POST /v1/responses` | Responses | `{model, input, instructions?, max_output_tokens?}` | `Authorization: Bearer` | `output[].content[].text` |

- 用原生路由时，**请求与响应都是该协议的原样格式**（不做跨协议转换）——必须按该协议官方字段构造报文，
  否则会得到上游的参数错误。
- 文本模型默认用**主协议**路由；`chat --protocol openai|anthropic|responses` 可显式指定。
- **`/v1/responses` 的现状**：该路由在网关上**确实存在**（实测：`POST` → `400 请求体缺少 model`，
  而非 `404`），脚本也已支持按该协议构造报文与解析响应；但**当前模型契约均未把它声明为支持协议**，
  因此 `--protocol responses` 对现有模型会明确报"不支持"而不是静默失败。
  将来某模型声明了它，脚本无需改动即可直接使用。
- 模型不支持所请求的协议时，脚本在发请求**之前**就报错并列出可用协议（不会发出必然失败的请求）。
- **图像 / 视频 / 音频不使用本节路由**（其公开契约未声明这些路径），一律走 §3.1。

## 4. 任务查询

### `POST /v1/content`

请求体：`{ "id": "<创建任务返回的 id>" }`（也接受 `task_id` / `taskId`）。

响应：

| `status` | 含义 | 附带字段 |
|---|---|---|
| `running` | 处理中 | 可能有 `progress`（0–100） |
| `succeeded` | 终态成功 | `result`、`results`、`imageUrl`/`videoUrl`/`audioUrl` |
| `failed` | 终态失败 | `error`（已脱敏） |

平台的**完整状态词表**（脚本运行时词表）：

| 归类 | 取值 |
|---|---|
| 未完成（继续轮询） | `NOT_START`、`queued`、`submitted`、`in_progress`、`processing`、`running`、`unknown` |
| 成功（终态） | `succeeded`、`success`、`completed`、`complete`、`finished` |
| 失败（终态） | `failed`、`failure`、`error`、`canceled`、`cancelled`、`expired` |

> 未在词表内、或缺失 `status` 的响应一律按"未完成"处理（继续轮询到超时），**不要**当成失败——
> 把"还在跑"误报成"生成失败"会直接误导用户。

**退出码**：`task --id` 与生成类命令在**终态失败**（`failed` / `canceled` / `expired` 等）
或**终态成功但没有结果地址**时返回 **1**；仍在处理中返回 0。`--json` 与人类模式的退出码相同。

- `results` 的形态不统一（单个字符串 / 数组 / 对象），脚本三种都收集；详见 §3.1 的提示。
- **响应包裹形态**：任务接口通常返回裸业务体（`{"status": ...}`）；但平台也存在统一包裹形态
  `{"code": 200, "data": {...}, "message": ""}`（所有**错误**响应、以及 `validate-key`、
  `/api/models/params` 的成功响应都是这个形态）。脚本对两种形态都会自动剥离外层，
  所以无论命中哪种都能正确判定状态与取结果。

建议轮询间隔 5–10 秒。轮询本身不消耗积分，但不要高频空转。

## 5. 其它公开端点

### `GET /api/v1/credit`（鉴权）

账户积分余额：

```json
{ "remainingTotal": 100.00, "remainingPermanent": 100.00, "remainingLimited": 0 }
```

字段为业务平铺；部分部署可能有 `data` 包裹。超出 `remainingTotal` 之外的分项
（`remainingPermanent` / `remainingLimited` / `remainingVip`）只在存在时展示。

**本 skill 的用法**（这是公开端点里唯一能拿到余额的入口）：

1. `credit` 子命令 —— 单独查余额。
2. 生成前后各查一次 → 差值即「本次扣减」，脚本在生成结束时自动展示。
   任一查询失败只跳过展示，**绝不影响生成**；批量调用可用 `--no-credit` 关闭。
3. `noova_key.py verify` / `setup` / `doctor` —— 作为**校验 Key 是否有效**的回退探针
   （无 Key / 无效 Key → 401）。

> 平台的任务响应体（创建 / 查询）**不含**任何积分字段，因此「消耗了多少积分」
> 只能用上面的余额差值口径得出，或引用模型的公开计价。

### `POST /api/v1/gateway/validate-key`（鉴权校验，**恒 200**）

**这是判断 Key 是否有效的首选接口**（`noova_key.py verify` / `doctor` / `setup` 均走这里）。

请求：`POST`，空 JSON 体 `{}`，鉴权头携带待校验的 Key。

| 情况 | HTTP | 响应体 |
|---|---|---|
| Key 有效 | 200 | `{ "code": 200, "data": { "valid": true, "key_prefix": "sk-abc...", "balance": { ... } } }` |
| 无 Key | 200 | `{ "code": 200, "data": { "valid": false, "reason": "缺少 API Key" } }` |
| Key 无效/停用 | 200 | `{ "code": 200, "data": { "valid": false, "reason": "API Key 无效或已停用" } }` |

- **恒 200**：不能靠状态码判断，必须读 `data.valid`；原因文案在 `data.reason`。
- 用其它 HTTP 方法访问 → `405`（说明路由存在、方法不对）。
- 若该端点返回 404/405 等非常规响应（老版本部署），脚本会自动回退到 `GET /api/v1/credit`。

### 素材上传（本地文件 → 可被生成接口引用的 URL）

把本地图片/视频（作为参考图、首帧、参考视频/音频）变成公网地址。

| 通道 | 地址 | 特点与限制 |
|---|---|---|
| 平台存储通道 | `GET /v1/client/resource/sts` 签发后直传（需 API Key） | 产物落在**平台自有存储**：最可靠，素材不经手任何第三方服务 |

- 上传为 `multipart/form-data` / 直传（以签发凭证返回的 `upload.method` 为准）。
- CLI：`upload --file <路径> [--host auto|platform]`（`auto` = 平台存储通道）。
  上传完成后脚本会做一次可达性校验（`verified` 字段）。
- **文件名会转成 ASCII 安全名**（非 ASCII 段折叠为 `_`，空则回退 `file`）——
  中文文件名在部分存储的编码与签名实现里会产生歧义。
- **可达性校验的判定**：`2xx/3xx` 视为通过；`405`/`416`（对方不支持这种校验请求）视为
  **无法判定**；`403` **如实报为"不可公开访问"**——403 恰恰证明上游取不到这个地址，
  不能当作通过。校验请求**不会**发往本机/内网地址（不做本机探测）。

**平台存储通道（通道三）三步走**：

1. `GET /v1/client/resource/sts?filename=&content_type=&size=`（鉴权）
   - query：`filename`（文件名，脚本会自动转成 ASCII 安全名——**平台通道也做**）、
     `content_type`（MIME）、`size`（字节数）
   - 响应 `data`：`upload_url`（直传地址）、`headers`（**必须原样携带**，含签名）、
     `upload.method`（通常 `PUT`）、`file_url`（上传后可被生成接口引用的地址）、
     `expires_in`（凭证有效期秒，通常 image 900s / video 3600s）、`allowed_media_types`
2. 按 `upload.method` + `data.headers` 把文件直传到 `upload_url`（**不要改动签名相关请求头**；
   大文件请在有效期内传完）
3. 把 `data.file_url` 作为参考字段传给生成接口

## 6. 限流

| 端点 | 限制 |
|---|---|
| `POST /api/v1/invoke` | 60 次/分钟（按来源 IP） |
| `GET /api/models/params` | 30 次/分钟 |
| `GET /api/models` | 30 次/分钟 |
| `GET /api/v1/credit` | 40 次/分钟 |
| `POST /api/v1/gateway/validate-key` | 60 次/分钟 |
| `GET /v1/models` | 120 次/分钟 |
| 账户生成配额 | 默认 80 次/分钟；VIP 200 次/分钟（以 **503** + `data.source=platform_quota` 体现，带 `Retry-After: 60`） |

批量生成时请控制并发；命中限流时按响应头 `Retry-After` 等待后重试（脚本会自动重试有限次）。

## 7. 错误码

| 码 | 含义 | 建议处理 |
|---|---|---|
| 400 | 参数错误（缺 `model`、参数值非法、请求体非 JSON、`size` 不合法等） | 用 `params --code <编码>` 核对参数名与取值（取值必须在 `values` 里，数值必须在 `min`~`max` 内） |
| 401 | 缺少或无效 API Key | 引导用户到官网重新获取并保存 |
| 402 | 积分不足 | 引导用户充值 |
| 403 | 无权限调用该模型 | 提示该 Key 没有此模型权限；若响应体形如边缘网络策略拦截错误（特定错误码），属网络策略拦截 |
| 404 | 资源不存在（模型不存在 / 任务不存在或无权访问） | 重新拉取模型清单 |
| 405 | 方法不允许（用错 HTTP 方法或路由） | 检查端点与方法 |
| 429 | 请求过于频繁（生成请求频控） | 按 `Retry-After` 等待后重试 |
| 500 / 502 | 服务或上游暂时异常 | 稍后重试；向用户转述为"服务暂时不可用" |
| 503 | 平台繁忙 / 账户分钟配额已满 | 等 `Retry-After`（通常 60s）后重试 |

## 8. 安全边界（严禁越界）

- 只调用本文档列出的端点。管理端接口、回调接口、内部数据接口、数据库一律不碰。
- 不向用户解释任何服务商、存储、内部实现细节；错误统一用中性话术转述。
- 模型编码、参数、取值全部以运行时公开接口为准，不硬编码、不猜测。
- 不在任何输出中泄露 API Key（含鉴权头）。
