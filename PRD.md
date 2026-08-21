# PRD：simpleEval

> 版本：MVP v0.1（已交付 2026-08-19）+ v0.2 需求池 + 版本路线图
> 日期：2026-08-17（初版）· 2026-08-19（试用反馈更新）
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

> 注：MVP 已按上述计划交付并上线 GitHub（2026-08-19），期间额外落地：api_type 双模式、响应解析四键模型、采样稳定性（pass@k/pass^k）、多变量输入（EvalCase.variables）、content 二次解包、任务队列化、批量操作等（详见 docs/ 与 git 历史）。以下为 v0.2 需求池与路线图。

---

## 九、v0.2 需求池（2026-08-19 试用反馈）

| # | 需求 | 优先级 | 复杂度 | 说明 |
|---|------|--------|--------|------|
| REQ-1 | LLM Judge 配置复用 | P2 | 低 | Judge 与 Target 同 base_url/key/model 时一键引用，免重复填写；改动同步 |
| REQ-2 | Target API 配置复用与管理 | P2 | 中 | base_url、output_paths、token 解析方式存为配置模板，跨项目引用 |
| REQ-3 | 单返回多字段验证 | **P0** | 中高 | 一次响应返回多字段（如 result/leader/evidence），各字段独立验证方式（result 用 exact、evidence 用 contains 等） |
| REQ-4 | Token 统计模式 UI 优化 | P3 | 低 | paths/fields 切换与 scope 配置的可读性、示例引导 |
| REQ-5 | Judge 双模式 | **P0** | 中 | Judge 配置与 Target 对齐：OpenAI Compatible + 自定义 API 双模式，允许用工作流/自定义接口做评测 |
| REQ-6 | Judge token 计入评测成本 | **P0** | 中 | summary 拆分 target_token / judge_token；评测成本 = 被评测消耗 + 评测自身消耗，token_per_pass 口径明确 |
| REQ-7 | 定时回归 | P1 | 中高 | 项目可配定时规则（每天/每周/cron），自动发起评测；结果 vs baseline（上次或历史均值）跌破阈值时告警。前置：服务常驻（本地 uvicorn 在线才生效），与 REQ-6 配合控制定时成本 |
| REQ-8 | 项目版本概念与跨版本分析 | P1 | 中高 | 版本 = **内部变更锚点**：切换主模型/供应商/编排时显式开新版本（可选时间点辅助自动归属，旧数据零迁移）；支持同一时期**平行版本**（A/B，run 需显式归属）。跨版本分析回答：变化来自**外部输入**（数据质量/客户行为——版本内波动）还是**内部变更**（编排/供应商/模型——版本间差异） |
| REQ-9 | 一项目一评测集 + case 标签筛选 | **P0** | 中 | 收敛：一个项目只有一整套测试集（UI 去除多评测集选择器）；增强：`EvalCase` 增 `tags: list[str]`（**可选，默认空列表，旧数据零迁移**；自定义标签：基准测试/回归测试/高频问题…），发起 run 时按标签多选/组合（含任一/含全部）筛选参与 case，**不筛选 = 全部参与（现状行为）**。连带：B-22 删除语义改为清空/重置评测集；批量操作支持按标签选择；REQ-7 定时回归可绑定标签（如只跑「回归测试」）；V-2 采样分析增标签维度 |
| REQ-10 | 评测集免新建 | P2 | 低 | 一项目一评测集的收尾：项目创建时自动建空评测集（或首次访问评测集 tab 自动建），UI 删除「新建评测集」按钮与空态引导，只留导入/表单添加两个入口 |
| REQ-11 | Target/Judge 双模式描述统一 | P3 | 低 | Target 卡片与 Judge 卡片的「OpenAI Compatible / 自定义 API」模式说明文案目前不一致，统一措辞（含字段必填规则与行为说明） |
| REQ-12 | 样例响应自动获取 | P1 | 中 | 响应解析配置的样例来源升级：不再只靠手粘贴——「获取真实样例」按钮自动取第一条启用 case 构造请求真实调用 target API，把响应填入样例区供点选路径。注意：会真实消耗 token，按钮文案需明示 |
| REQ-13 | 同一批 case 批量跑 k 次 | P1 | 中高 | pass@k 采样策略执行层提前（原批次 3）：发起评测时指定采样次数 k（默认 1），run 内每 case 执行 k 次，结果记录含样本序号；执行层采样与历史聚合采样两条路径并存，统计口径需明确（run 内 k 次采样计为同一 case 的 k 条样本） |
| REQ-14 | 评测集变更后采样统计重置 | P1 | 中 | 重新导入（replace）或其他变更后，旧 run 的采样与新版评测集不对应：评测集记录 `content_updated_at`，采样聚合只统计该时间点之后的 run（与 REQ-8 版本概念亲缘，实现可复用时间切分逻辑）；UI 在重导后提示「采样统计已按新评测集重置」 |

> 来源说明：六条全部来自 2026-08-19 走访提取场景的真实试用。REQ-3 直接对应"一次返回 result+leader+evidence，现在只能验一个字段"的痛点；REQ-5/REQ-6 对应"Judge 也可以走评测工作流"与"成本账要算全"。

## 十、版本路线图

> 2026-08-19 修正：取消"v2 远期无限期"的划分，全部需求（含原 v2 范围）与试用反馈打散，按 **价值 × 紧迫度 ÷（难度 + 依赖）** 排为四个优先序批次。每批完成后可随时重排。

### 批次 1（近期：P0 价值 / 低中难度 / 少依赖）

| 序 | 需求 | 难度 | 依赖 |
|---|---|---|---|
| 1 | B-22~B-25 补丁批（删除×2 / 截断展开 / 图标） | 低 | 无 |
| 2 | **REQ-9 一项目一评测集 + 标签筛选**（结构性变更，数据量小尽早做） | 中 | 无 |
| 3 | REQ-5 Judge 双模式 | 中 | 无 |
| 4 | REQ-6 Judge token 计入成本 | 中 | REQ-5 路径改造 |
| 5 | REQ-3 多字段验证 | 中高 | 无 |
| 6 | V-1 未保存提醒 | 低 | 无 |
| 7 | REQ-4 Token UI 优化 | 低 | 无 |

### 批次 2（中难度 / 部分依赖）

| 序 | 需求 | 难度 | 依赖 |
|---|---|---|---|
| 7 | V-2 case 级采样分析（含稳定 case_id） | 中 | case_id 对齐 |
| 8 | REQ-1 Judge 配置复用 | 低 | REQ-5 后的配置结构 |
| 9 | REQ-2 Target 配置模板 | 中 | 无 |
| 10 | token 硬限制（预算阻断） | 低中 | REQ-6 成本口径 |

### 批次 2.5（2026-08-21 实测反馈，小补丁）

| 序 | 需求 | 难度 |
|---|---|---|
| 1 | BUG-1 硬超时修复（case 挂起问题） | 低中 |
| 2 | REQ-10 评测集免新建 | 低 |
| 3 | REQ-11 双模式描述统一 | 低 |
| 4 | REQ-12 样例响应自动获取 | 中 |

### 批次 3（调整：REQ-13/REQ-14 因重度使用采样统计而提前）

| 序 | 需求 | 难度 | 依赖 |
|---|---|---|---|
| 1 | REQ-13 批量跑 k 次（执行层采样） | 中高 | 无 |
| 2 | REQ-14 评测集变更后采样重置 | 中 | 无（与 REQ-8 时间切分思路复用） |
| 3 | REQ-8 版本概念 + 跨版本对比 | 中高 | 历史数据积累 |
| 4 | REQ-7 定时回归 | 中高 | REQ-6 + 服务常驻 |
| 5 | 并发执行 | 中 | 无 |

### 批次 4（高难度 / 探索性，价值待验证）

| 序 | 需求 | 难度 | 依赖 |
|---|---|---|---|
| 15 | RAG 评测（recall@k + faithfulness） | 高 | 评测集结构扩展 |
| 16 | 轨迹级 Agent 评测 | 高 | 轨迹记录格式先定 |
| 17 | 自配置 API + 人工标注 | 高 | 无 |

**下一步计划**：批次 1 按序开工（补丁批 → REQ-5 → REQ-6 → REQ-3），每完成一项跑全量走访验证集；批次间按当时痛点重排。
