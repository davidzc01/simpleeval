# PRD：simpleEval

> 版本：MVP v0.1（定稿）
> 日期：2026-08-17
> 作者：David

---

## 一、定位

**LLM 与 Agent（黑盒）评测与分析工具。**

- 评"单次/黑盒"的输出质量 + 成本，不做轨迹级评测（v2）
- RAG 评测不在 MVP（后加），但评测集结构为 RAG 指标预留扩展位

### 一句话价值

> 帮你把"这个模型/Agent 到底行不行、值不值这个 token"量化出来，并且能对比每次改动。

### 差异化（为什么不是又一个跑分工具）

1. **任务形状匹配**：不预设评测集形状，按"任务形状"（编码/客服/多轮对话/通用）组织评测集，避免"用一个 benchmark 分数推断所有场景"的类别错误。
2. **成本 vs 得分**：不只报分数，报"每万 token 换来多少任务完成率"——直接回答"这个模型值得这个成本吗"。

---

## 二、MVP 范围（必做）

| 模块 | 内容 |
|------|------|
| 项目（Project）| 组织单位，含 Judge 配置 + 被评测 API 配置 + token 预算 |
| 评测集（EvalSet）| JSON 存储，支持 csv/excel/json 导入 |
| 评测执行 | 4 种评测类型 + 性能统计 |
| 结果展示 | 单次 run 的得分 + 耗时 + token 报告 |
| Web UI | 简单可操作的 HTML 界面 |

### 评测类型（MVP）

| 类型 | 说明 |
|------|------|
| `exact` | 精确匹配 |
| `contains` / `not_contains` | 包含/不包含判断 |
| `length` | 长度判断（min/max）|
| `llm_judge` | LLM-as-Judge，可配 prompt + 打分阈值 |

### 性能统计（MVP）

| 指标 | 说明 |
|------|------|
| 时间 | 最小/最大/平均/P50/P95（Agent 成本是偏态分布，平均值会骗人）|
| token | 总消耗、单 case 消耗 |

### 成本对比（MVP 的差异化核心）

每个 run 输出：**得分 + 总 token + 总耗时**，并计算：

> **每万 token 任务完成率** = 通过数 / (总 token / 10000)

用户改 prompt、换模型后，能直接对比"分数涨了，但成本涨了多少"。

---

## 三、v2 范围（后补，不在 MVP）

| 功能 | 原因 |
|------|------|
| RAG 评测（retrieval recall@k + faithfulness）| 评测集结构已预留，后加指标 |
| 轨迹级 Agent 评测（中间步骤、工具调用日志）| 需要先定轨迹记录格式 |
| 历史 run 对比视图 | MVP 先跑通单次，对比后加 |
| 自配置 API 评测 / 人工标注 | 扩展性功能 |
| pass@k（多次采样）| 需要采样策略 |
| 并发执行 | 并发引入延迟噪声，性能评测需单独串行 |
| token 硬限制 | MVP 只做"超预算提醒"，不中断 |

---

## 四、数据模型

```
Project（项目）
├── id, name, task_shape（编码/客服/多轮/通用/自定义）
├── judge_config: { base_url, api_key, model }        # OpenAI Compatible
├── target_config: { base_url, api_key, model, request_template }
└── token_budget: { limit, warn_only: true }

EvalSet（评测集）
├── id, project_id, name
└── cases: [
    {
      case_name,
      input,
      expected_output | output_requirement,
      eval_type,          # exact / contains / length / llm_judge
      eval_params,        # 如 llm_judge 的 prompt + 阈值
      task_shape          # 继承项目，可单独覆盖
    }
  ]

EvalRun（评测记录）
├── id, project_id, evalset_id, created_at
├── results: [
    {
      case_name,
      actual_output,
      passed, score,
      latency_ms,
      token_used
    }
  ]
└── summary: {
      pass_rate,
      total_token,
      total_latency,
      token_per_pass,     # 每万 token 完成率
      latency_p50, p95
    }
```

### 存储决策

- **JSON**（不是 pickle）：可移植、可导出、可 diff、可被他人审查，无反序列化安全风险。
- 评测集单文件一个 JSON，方便版本管理（git diff 能看出评测集改了什么）。

---

## 五、技术栈

| 层 | 选择 | 理由 |
|----|------|------|
| 后端 | **FastAPI** | 轻、快、自动生成 API 文档，Python 生态（David 主练 Python）|
| 前端 | 简单 HTML + 原生 JS（或 Streamlit 快速起 demo）| MVP 不求花哨，能操作即可 |
| 存储 | JSON 文件 | 简单、可 git diff、零依赖 |
| 模型调用 | OpenAI Compatible API | 一套接口接所有模型（DeepSeek/Claude/GPT/本地）|

### 依赖最小化

```
fastapi, uvicorn, httpx（调 API）, pydantic（校验）
前端：单文件 index.html
```

不引入数据库、不引入 ORM、不引入前端框架。**JSON 文件 + FastAPI + 单页 HTML，够跑通 MVP。**

---

## 六、目录结构

```
simpleeval/
├── README.md            # 项目说明 + 量化示例（recall@5 从 0.6 到 0.85 那种故事）
├── app/
│   ├── main.py          # FastAPI 入口
│   ├── models.py        # pydantic 数据模型
│   ├── eval_types.py    # 4 种评测类型实现
│   ├── judge.py         # LLM-as-Judge 调用
│   └── runner.py        # 评测执行 + 性能统计
├── data/                # 评测集 JSON（示例项目）
├── examples/
│   └── demo-evalset.json # 一个可跑通的示例评测集
└── requirements.txt
```

---

## 七、MVP 完成标准（Definition of Done）

跑通这条最小闭环，就能发 GitHub：

```
1. 导入一个 JSON 评测集（含 4 种 eval_type 的 case）
2. 配置被评测 API + Judge 模型
3. 跑一次评测 → 输出 pass_rate + 每 case 耗时 + token 统计
4. Web UI 能操作上述流程，看到结果表格
5. README 里有一个"量化故事"：某模型/prompt 改动前后的得分 + 成本对比
```

**第 5 条是关键**——它保证这个工具不是"能跑"，而是"能讲出一个量化故事"，直接补 2G 项目"不好量化"的短板。

---

## 八、下一步（Sprint 3 内）

| 步骤 | 时间 |
|------|------|
| 1. 搭 FastAPI 骨架 + 数据模型 | 半天 |
| 2. 实现 4 种评测类型 | 1 天 |
| 3. LLM-as-Judge 接通 | 半天 |
| 4. 性能统计 + 成本对比 | 半天 |
| 5. 简单 Web UI + 示例评测集 | 1 天 |
| 6. README 量化故事 | 半天 |

**合计约 3-4 天碎片时间，1-2 周内可交付。**
