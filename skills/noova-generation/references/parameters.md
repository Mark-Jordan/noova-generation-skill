# 参数查询与参数语义

用户问"这个模型能传什么参数""这个参数能填哪些值"时，**必须走运行时查询**，不允许凭记忆回答。

> 下文命令里的 `python3` 是**占位写法**。不同系统上解释器名不同（Windows 官方发行版是 `python`），
> 先取本机可用的那一个再执行，详见 `SKILL.md` 的「运行环境」一节。
---
## 1. 查询方式

```bash
# 交互问答板（推荐：直接产出可弹给用户的参数面板，含"能不能选/能选什么"）
python3 <SKILL_DIR>/scripts/noova_media.py form --code <模型编码> --json

# 结构化契约（agent 直接消费）
python3 <SKILL_DIR>/scripts/noova_media.py params --code <模型编码> --json

# 人类可读的参数契约
python3 <SKILL_DIR>/scripts/noova_media.py params --code <模型编码>
```

`form --code <模型编码>` 是在 `params` 之上加了一层**面向用户**的判断：每个字段的 `kind` 已经分好
`choice`（多选一）/ `fixed`（**只有一个取值，显示但不可选**）/ `number`（数值区间，带 `range`）/
`text`、`list`（自由填写），并给出 `cli`（该字段在命令行里怎么传）。
要弹面板给用户确认参数时**用它**；要拿原始参数契约做程序化处理时才用 `params`。

`params --json` 的顶层结构：

| 字段 | 含义 |
|---|---|
| `model_code` | 模型编码（请求时原样使用） |
| `callable` | `false` = 平台未给该模型调用编码，**不能调用** |
| `display_name` / `model_type` / `status` | 名称 / 类型 / 状态 |
| `line` / `line_label` | **线路**：`line` 是契约原文（运营手填，可能为空串）；`line_label` 是展示名，空串时回退为「默认」 |
| `family` | 同族键：`<类型>:<显示名>`。同族内即同一模型的各条线路 |
| `siblings[]` | **同模型的其它线路**（每条含 `model_code` / `line_label` / `price_cell` / `approximate_credit`） |
| `approximate_credit` | **选线路阶段的近似消耗**（见下） |
| `ability` | 能力标签（**由参数契约推导**，不是平台字段） |
| `protocols` / `primary_protocol` | 支持的协议列表；**`protocols[0]` 即主协议** |
| `invocation` | 调用方式：`mode`（`sync`/`task`）、`create`、`poll`、`stream_supported`、`eta_seconds`、`default_max_wait`、`routes` |
| `billing` | 计费（见 §5） |
| `params[]` | 参数契约（见下） |
| `credit_per_yuan` | 积分↔人民币换算比例，**当前 100（即 100 积分 = 1 元）** |

`approximate_credit` 的 `kind` 三态（**参数还没定，故只能是近似或定性**）：

| `kind` | 含义 | 怎么向用户说 |
|---|---|---|
| `exact` | 单价固定且与参数无关 | 直接报 `text`（如 `20 积分/次`） |
| `range` | 加收参数在契约里有 `min`/`max`，能算出区间 | 报 `text`（如 `约 260~1950 积分/次`），`detail` 是依据（`duration 4~30 × 65 积分`） |
| `usage` | 按 token 结算，或边界推不出来 | **只给定性文案**（`按用量计价（见加收规则）`），**绝不可编数字** |

参数定好后的**精确价**由 `billing` + `surchargeRules` 算（§5）。

`params --json` 的 `params[]` 每项结构：

| 字段 | 含义 | 用法 |
|---|---|---|
| `name` | 参数名 | **必须原样**作为请求体字段名 |
| `type` | 类型 | `string` / `number` / `boolean` / `string[]` … |
| `required` | 是否必填 | `true` 时必须传，否则会被拒（脚本会在发请求前拦下并提示） |
| `label` | 该参数的中文名 | 直接用于向用户解释；为空时脚本回退到内置名表 |
| `values` | **平台配置的**可选值数组；自由文本参数为空数组 | 非空时**全部**列给用户，不要只给前几个 |
| `valueLabels` | 取值 → 中文标签（如 `{"16:9": "横屏"}`） | 有标签时用标签解释该取值 |
| `min` / `max` | 数值型参数的上下限 | 只在 `type` 为数值类时出现 |
| `minItems` / `maxItems` | 数组型参数的元素个数上下限 | 只在数组类型时出现 |

> **没有 `description`，也没有"从说明文本推导的候选值"**：契约里 `values` 是平台自己配置的枚举，
> 它就是权威取值集合。**`values` 为空 = 该参数是自由文本，无固定取值**——
> 此时如实说"无固定取值"，**任何情况下都不要编造枚举**。

示例（形态参考）：

```json
{
  "model_code": "<模型编码>",
  "callable": true,
  "model_type": "video",
  "protocols": ["openai"],
  "primary_protocol": "openai",
  "credit_per_yuan": 100,
  "invocation": { "mode": "task", "create": "POST /api/v1/invoke", "poll": "POST /v1/content",
                  "stream_supported": false },
  "billing": { "mode": "per_request", "price": 0, "unit": 1,
               "text": "按用量计价（见加收规则）",
               "surcharge_rules": [{ "param": "duration", "match": "present",
                                     "charge": "multiply_by_value", "multiplier": 25 }],
               "surcharge_text": ["提供 duration 时，按 duration × 25 积分 计"] },
  "params": [
    { "name": "prompt", "type": "string", "required": true, "label": "提示词", "values": [] },
    { "name": "duration", "type": "number", "required": true, "label": "时长", "values": [],
      "min": 4, "max": 15 },
    { "name": "aspect_ratio", "type": "string", "required": false, "label": "画面比例",
      "values": ["1:1", "16:9", "9:16"], "valueLabels": { "16:9": "横屏" } }
  ]
}
```

---

## 2. 回答用户的规则

| 用户问 | 你怎么答 |
|---|---|
| "这模型有哪些参数？" | 列出 `params[]` 的 `name` + `label` + 是否必填；不要漏掉数组类型参数 |
| "某个参数能填什么？" | 给出 `values` 全部取值（`valueLabels` 有标签就带上）；`values` 为空才说"自由文本，无固定取值" |
| "这个参数（值）是什么意思？" | 用 `label` 解释；`label` 不足时坦诚说明平台未展开说明，**不要臆测** |
| "不传这个参数行不行？" | `required=false` → 可以省略；`true` → 必须传 |
| "有哪些模型？" | 跑 `models` 后按**类型分区**回答，给出编码、名称、协议、状态与计价；**同名多线路的模型把每条线路都列出（线路名 + 编码 + 计价）**；并说明"清单实时变化" |
| "同名模型怎么区分？" | **靠模型编码与线路（`line`）**。对外契约有 `line` 字段，展示为「线路名」，空串则显示「默认」。用 `models` 看同一模型名下有几条线路，再按编码选一条。注意：**线路文本里的数字不能当单价**（运营手填、会过期），计价真值只有 `billing` + `surchargeRules` |
| "大概要多久？" | 按类型给区间（文本 1–10 秒首字符；图像 30 秒–5 分钟；音频 30 秒–10 分钟；视频 1 分钟–1.5 小时），并强调"以实际任务为准" |
| "要花多少积分？" | 引用 `billing.text`（口径见 §5）；实际消耗以生成结束时的「本次扣减（余额差值）」为准 |
| "推荐哪个模型？" | 可以按 `models` 返回的类型/计价信息给出建议，但**必须先说明依据**，不要编造性能排名 |

---

## 3. 传参注意事项

1. **参数名大小写敏感，且各模型写法可能不同**：必须以**该模型参数契约**的写法为准。
   实测同一平台上既有 `aspect_ratio`（如 `demo-video-rt`）也有 `aspectRatio`（如 `demo-image-2-G`）——
   所以不要凭"惯例"替换命名，**先查表**，再原样传。
2. **未列出的参数名不会生效**：平台会忽略模型不认识的键，**不报错**。写错名字的典型表现是
   "请求成功了但效果没变化"。因此传参前先查表。
3. **无需重复传 `model`**：脚本会从 `--model` 自动写入请求体；`--param model=xxx` 不需要也不建议。
4. **数值型参数**：`--param duration=5` 会被转换为数字 `5`；`--param resolution=720p` 保持字符串。
   布尔值写 `true` / `false`，空值写 `null`。
5. **数组型参数**（如参考图列表）用 `--ref-image`（可重复）或 `--param name=a,b` 形式；
   具体字段名以参数契约为准。
6. **没有默认值**：不传就是没设置（与网页画布不同，画布会替你补默认值）。
   数值型参数有区间（`min`/`max`），越界会被平台拒绝。
7. **参数无法确认时**：可以先不传，用模型的默认行为生成；或者向用户说明"该模型文档未提供此参数"。

---

## 4. 常见参数（理解用，仍需以运行时参数契约为准）

以下是跨模型反复出现的**通用命名习惯**，用于帮助你快速理解用户意图；
**实际可用性与取值一律以该模型的参数契约为准**：

| 参数名 | 典型含义 | 常见取值形态 |
|---|---|---|
| `prompt` | 提示词（文生图/文生视频/文本） | 自由文本 |
| `input` / `messages` | 文本输入（部分模型用 `input`） | 字符串 / 消息数组 |
| `images` / `referenceImages` | 参考图、首帧 | URL 数组 |
| `referenceVideos` / `referenceAudios` | 参考视频 / 参考音频 | URL 数组 |
| `aspect_ratio` | 画面比例 | `1:1`、`16:9`、`9:16` |
| `resolution` | 分辨率 | `720p`、`1080p` |
| `duration` | 时长（秒） | 数值或区间 |
| `quality` | 质量档位 | `low` / `medium` / `high` |
| `size` | 尺寸 | 像素串或比例 |
| `seed` | 随机种子 | 整数 |
| `negative_prompt` | 负向提示 | 自由文本 |
| `stream` | 是否流式（仅文本） | `true` / `false` |
| `max_tokens` / `temperature` / `top_p` | 文本采样与长度（仅文本） | 数值 |

如果用户提到的参数名不在该模型参数契约中，**优先核对名称拼写**，其次建议用契约中的等价参数，
最后才如实告知"该模型不支持这个参数"。不要为了"满足用户"而传一个不存在的参数名。

---

## 5. 计费信息怎么读

`params --json` 的 `billing` 字段：

| 字段 | 含义 |
|---|---|
| `text` | **面向用户的计价文案**，直接引用即可 |
| `mode` | `per_request` / `per_time` / `per_token` |
| `price` / `unit` | 按次/按时的单价与计费单位（`price × unit` 即一次基础费用） |
| `price_per_1m` | 按 token 计费时的四档单价（见下） |
| `surcharge_rules` | 参数加收规则（机器可读，原样透传） |
| `surcharge_text` | 同一批规则的人读句子（与 `surcharge_rules` 同源，避免口径漂移） |

**四档 token 单价**（`mode = per_token` 时出现，**单位 = 积分/百万 tokens**）：
`input`（输入）、`output`（输出）、`cacheHit`（缓存命中）、`cacheWrite`（缓存写入）。
契约缺哪档就哪档为 0；**别把 0 说成免费**——只是该档不适用。

**加收规则**（`surcharge_rules[]`，**这是"积分可能超出基础单价"的唯一来源**）：

| 字段 | 含义 |
|---|---|
| `param` | 触发加收的参数名 |
| `match` | `present`（提供了该参数就加收）/ `exact`（取值等于 `matchValue`）/ `range`（取值落在 `rangeMin`~`rangeMax`） |
| `charge` | `multiply_by_value`（按取值倍数）/ `fixed`（固定额度） |
| `multiplier` / `base` | 倍数与基数：加收 = `base + 取值 × multiplier`（`base` 缺省按 0 算） |
| `credits` | `charge=fixed` 时的固定加收积分 |

**读法与红线**：

1. `mode = per_token` → 用 `price_per_1m` 报「输入 X / 输出 Y 积分/百万 tokens」，并说明"以实际用量结算"。
   （`models` 清单表格里这一列写作紧凑的 `X/Y`，单位由表格上方的「计价列」图例统一说明——
   逐行重复单位会把行撑到 100+ 列、在 80 列终端折行拆散表格。向用户转述时用完整说法。）
2. `mode = per_request` / `per_time` 且 `price > 0` → 「X 积分/次」（`unit > 1` 时写「X 积分/N 次」）。
3. **`price = 0` → 绝不能说「0 积分/次」或"免费"**：这表示单价改由 `surcharge_rules`
   按用量（时长等）计价。此时引用 `surcharge_text`，或如实说"以平台当前计价为准"。
4. **报价必须把加收算进去**：用户选定参数后，用 `form --code <编码> --param 名=值 --json` 拿
   `credit_estimate_text`，它已按上述公式把基础价与加收合并成一句可直接转述的话。
5. **积分换算成人民币时用 `credit_per_yuan`**（当前 100，即 **100 积分 = 1 元**）。
   报钱数时把换算比例一并说清，避免用户拿积分当"元"理解。

用户问"这个要花多少积分"时：按上述口径报单价；若想给实际消耗，
用生成结束时的「本次扣减（账户余额差值）」——那才是真实扣费，必要时先 `credit` 查余额。
