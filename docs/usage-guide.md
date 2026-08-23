# simpleEval 使用指南

> 面向想用它评测自己工作流的人。按顺序走一遍，就能完成第一次评测。
> 练习材料：`examples/demo-evalset.json`（通用客服场景 15 条，五类评测类型）。

---

## 1. 启动

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --port 8000
```

打开 http://localhost:8000 。评测过程中不建议用 `--reload`（开发调试时才用它）。

---

## 2. 新建项目

项目是评测的组织单位：一个项目 = 一个被评测对象 + 一套评测集 + 它的全部评测历史。

侧栏「+ 新建项目」→ 填名称 → 创建后自动带一个空评测集。

---

## 3. 配置 Target API（被评测对象）

配置页 → Target API 卡片。先选模式：

| 模式 | 适用 | 要求 |
|------|------|------|
| **OpenAI 兼容** | DeepSeek、OpenAI、大多数模型 API | base_url + model 必填；api_key 可留空（自托管网关） |
| **自定义 API** | FastGPT 工作流、任意 HTTP JSON 接口 | 请求模板必填；不注入 model/messages，请求体完全由模板决定 |

### 请求模板与变量

模板是发给被评测 API 的请求体。`{input}` 是 case 的输入文本，`{key}` 引用 case 的自定义变量——变量在评测集的每条 case 里配，键名任意。

```json
{"messages":[{"role":"user","content":"{input}"}]}
```

### 认证（auth）

无 / Bearer Token / API Key（自定义 header）/ Cookie / 自定义 Headers 五种。一次只生效一种；密钥保存后永不回显。

### 响应解析：告诉工具"结果在哪"

被评测 API 的返回结构各异，四个配置项：

| 配置 | 填什么 | 行为 |
|------|--------|------|
| 输出路径 | JSONPath 列表 | 从上到下依次尝试，第一个命中生效。留空 = 完整响应作为输出 |
| token 路径 | JSONPath 列表 | 所有命中值求和（如 input+output 分字段） |
| token 字段 | 字段名列表 | 全树递归匹配同名字段求和（路径穷举不了时用） |
| content 是 JSON | 开关 + 取字段 | 输出是 JSON 字符串时，解包后取指定字段 |

「测试连接」按钮验证配置；「获取真实样例」会用第一条用例真实调用一次，把响应填进样例区供点选路径。

### 完整示例 A：DeepSeek 标准 API

```
模式：OpenAI 兼容
base_url: https://api.deepseek.com/v1
model: deepseek-chat
请求模板: {"messages":[{"role":"system","content":"你是一个客服助手。"},{"role":"user","content":"{input}"}]}
输出路径: $.choices[0].message.content
token 路径: $.usage.total_tokens
```

### 完整示例 B：FastGPT 工作流

```
模式：OpenAI 兼容（FastGPT 提供兼容层）
base_url: https://your-fastgpt-host/api/v1
api_key: 应用密钥
请求模板: {"stream":false,"detail":false,"variables":{"content":"{content}"},"messages":[{"content":"{input}","role":"user"}]}
输出路径: $.choices[0].message.content
content 是 JSON: 勾选，取字段 result
token: 兼容层不返回真实 token → 留空，结果会标注「token 不可得」
```

---

## 4. 配置 Judge（判分模型）

侧栏「⚖ Judge 配置」：集中维护多套 Judge 配置（不同模型 API、评测工作流），项目里下拉引用。

只有 `llm_judge` 类型的用例会调用 Judge；纯规则类用例不需要 Judge 也能跑。Judge 消耗的 token 计入评测成本。

---

## 5. 建评测集

评测集 tab：导入 CSV/JSON（有模版可下载），或逐条表单添加。

### 五类评测类型怎么选

| 类型 | 判定 | 用在哪 |
|------|------|--------|
| `exact` | 输出与期望**完全一致**（去除首尾空白后） | 答案固定、话术标准化的场景 |
| `contains` / `not_contains` | 输出包含/不包含指定子串 | 关键词命中、敏感信息拒绝 |
| `length` | 输出长度在 min/max 区间 | 回答长度控制 |
| `llm_judge` | Judge 模型按判据打分（≥0.5 通过） | 语义正确性、开放性回答 |

选型直觉：**能写死规则的用规则类，只有语义判断才用 llm_judge**——Judge 每次调用都消耗 token。

`llm_judge` 的判据（output_requirement）要写清"什么算过、什么判 0"，例如：

> 回复需包含：1) 取消条件（如发货前可取消）；2) 操作路径或时限。两者缺一判 0。

### 标签

每条 case 可打任意标签（如 `smoke` / `regression` / `edge_case`）。发起评测时按标签筛选——日常跑冒烟组，大改跑全量。

### 多字段验证

一次响应返回多个字段、各自用不同方式验证时，用 case 的「多字段验证」区：对解包后的对象按字段路径逐项验证，全部通过才算通过。

---

## 6. 发起评测

「▶ 发起评测」→ 选择标签（不选 = 全部启用用例）→ 确认。

- **采样次数 k**：每条 case 跑 k 次（稳定性数据）；默认 1
- **并发数**：默认串行；可调并发加速，但延迟指标会标注采集条件（并发下延迟失真）
- **token 预算**：超限提醒或中断（剩余 case 标记跳过，不计入通过率）

发起后立即返回，任务在服务端跑；关页面不打断；重新打开从侧栏任务指示器找回。

---

## 7. 读结果

### 概览页（项目首页）

自上而下：版本信息 → **三枚 delta 卡**（最近 run vs 同版本上次：质量/运行成本/评测成本的变化）→ 质量与成本趋势（按版本分段）→ 稳定性短板（最不稳的 case 决定整体）→ 最近 run 列表 → 当前配置摘要。概览回答三个问题：**现在怎么样（变化）、变化从哪来（版本归因）、该看哪里（短板与失败直达）**。

### run 详情页

| 指标 | 含义 |
|------|------|
| pass rate | 通过数 / 有效用例数（跳过的除外） |
| 总 token | 被评测消耗 + Judge 消耗（分开标注） |
| 每万 token 完成率 | 通过数 / (总 token / 10000)——**性价比** |
| P50 / P95 延迟 | 延迟分布；并发跑时仅参考 |
| 失败 | 未通过且未跳过的用例数 |

点任一条 case 看三栏对比：INPUT / EXPECTED / ACTUAL。失败原因在对比里直接可见。

### 采样稳定性怎么读

- **pass@k（上界）**：k 次采样至少一次通过的概率——回答"模型**会不会**"。允许重试的场景看它
- **pass^k（下界）**：k 次采样全部通过的概率——回答"模型**稳不稳**"。一次就要对的场景看它
- 两条线在 k=1 重合，随 k 增大**必然张开**——夹缝越宽，通过越靠运气
- 概览页展示**短板组合**：min(pass^3) 给上线判断，低于阈值的计数给规模感，最不稳列表给导航
- case 详情（评测集里点「详情」→ 统计 tab）：单 case 双线图 + n / pass rate / 延迟 / token 元数据 + 单 case 趋势

### 评测集筛选

评测集 tab 工具栏：关键字（case 名/输入）、类型（多选）、标签（chips）、状态（启用/禁用）——条件 AND 叠加，右侧「重置」一键清空。

---

## 8. 常见问题

**token 显示「不可得」**：被评测 API 不返回真实 token（FastGPT 兼容层常见）。可尝试在响应解析里用 token 字段递归匹配；确实没有时该指标不可用，这是被评测对象的限制。

**exact 一直不通过**：exact 是全文相等。模型自由发挥的回复很难精确命中——要么在 system prompt 里指定标准话术，要么改用 contains / llm_judge。

**llm_judge 判定太松**：判据（output_requirement）写得太泛。把"什么算过、什么判 0"写具体，Judge 才有抓手。
