# simpleEval · API 契约 v0.1（骨架）

| 项目 | 内容 |
|---|---|
| 版本 | v0.1 草案 |
| 制定日期 | 2026-08-18 |
| 上游文档 | `docs/ui-spec.md`（页面规格）· `PRD.md`（数据模型） |
| 下游读者 | 后端实现 agent · 前端 coding agent（mock 依据） |
| 基址 | 所有接口前缀 `/api`，本地默认 `http://127.0.0.1:8000` |

> **标注约定**：
> `✅` = 现状已实现（仅需核对细节）　`🔧` = 现状需扩展/修改　`➕` = 需新增
> 前端 agent 在 `🔧/➕` 接口上先用 mock 开发，接口就绪后切换。

---

## 0. 统一约定

| 项 | 约定 |
|---|---|
| 时间 | 一律 **UTC ISO 8601**（`2026-08-18T14:05:00Z`），前端本地化显示。🔧 现状 `created_at` 为本地字符串格式，需改 |
| 错误格式 | 所有非 2xx 统一返回 `{"error": {"code": "...", "message": "..."}}`，见 §3 |
| secret 字段 | GET 返回时**永不回显明文**，只返回 `{"masked": true}`；PUT 提交时用哨兵值 `"__UNCHANGED__"` 表示"保留原值，不要覆盖" |
| ID 规则 | project / evalset / run 的 id 均为字符串；run id 建议 `run-{毫秒时间戳}` 或 `run-{uuid8}`，避免秒级撞车（🔧 现状为秒级时间戳） |
| 分页 | MVP 不分页。列表接口全量返回，但列表项**不含 results 明细**（明细只出现在单资源 GET） |
| 认证 | MVP 无用户体系，单机工具，不设鉴权 |

---

## 1. 资源模型（目标态 JSON 形状）

### 1.1 Project（🔧 扩展）

```json
{
  "id": "proj-01",
  "name": "客服Agent评测",
  "task_shape": "customer_service",
  "judge_config": {
    "base_url": "https://api.example.com/v1",
    "api_key": { "masked": true },
    "model": "gpt-4o-mini",
    "prompt_template": "你是一个评测裁判…（变量：{input} {actual_output} {output_requirement} {case_name}）"
  },
  "target_config": {
    "base_url": "https://api.target.com/v1",
    "model": "deepseek-chat",
    "request_template": "{\"messages\":[{\"role\":\"user\",\"content\":\"{input}\"}]}",
    "auth": {
      "type": "none | bearer | api_key | cookie | headers",
      "bearer_token": { "masked": true },
      "api_key_header": "X-API-Key",
      "api_key_value": { "masked": true },
      "cookies": [ { "name": "session", "value": { "masked": true } } ],
      "headers": [ { "name": "X-Env", "value": "test" } ]
    },
    "response_parsing": {
      "output_paths": ["$.data.reply", "$.choices[0].message.content"],
      "token_paths": ["$.usage.total_tokens"],
      "token_fields": [],
      "token_scope": null
    }
  },
  "token_budget": { "limit": 100000, "warn_only": true }
}
```

- `auth.type` 决定哪些子字段生效；其余子字段允许为空。
- `response_parsing` 四个键的语义与冲突规则见 `docs/response-parsing-design.md`：`output_paths` 按序 fallback，`token_paths` 命中求和，`token_fields` 全树递归求和（配 `token_scope` 过滤）；paths 与 fields 同时给时 paths 优先。全部留空 = 完整响应原文 + 不统计 token。
- 提取后的结果写入 `case.actual_output`，判定逻辑不变。

### 1.2 EvalSet / EvalCase（🔧 扩展 enabled）

```json
{
  "id": "evalset-01",
  "project_id": "proj-01",
  "name": "客服意图评测集",
  "cases": [
    {
      "id": "case-01",
      "case_name": "退款政策-精确",
      "input": "你们支持退款吗？",
      "expected_output": "支持，7 天内可无理由退款。",
      "output_requirement": null,
      "eval_type": "exact | contains | not_contains | length | llm_judge",
      "eval_params": {},
      "task_shape": null,
      "enabled": true
    }
  ]
}
```

- `expected_output` 与 `output_requirement` 按 eval_type 二选一：规则类用前者，`llm_judge` 用后者。
- `enabled: false` = 禁用：保留在评测集里、导出可见，但 run 执行时跳过。
- `id`：➕ 现状 EvalCase 无 id 字段，需补（前端行操作需要稳定标识）。

### 1.3 EvalRun（➕ 状态机扩展）

```json
{
  "id": "run-1723987500123",
  "project_id": "proj-01",
  "evalset_id": "evalset-01",
  "status": "queued | running | completed | failed",
  "created_at": "2026-08-18T14:05:00Z",
  "started_at": "2026-08-18T14:05:01Z",
  "finished_at": "2026-08-18T14:06:30Z",
  "error": null,
  "results": [
    {
      "case_name": "退款政策-精确",
      "actual_output": "支持，7 天内可无理由退款。",
      "passed": true,
      "score": 1.0,
      "latency_ms": 812.4,
      "token_used": 342,
      "skipped_reason": null
    }
  ],
  "summary": {
    "pass_rate": 0.875,
    "total_token": 42300,
    "total_latency_ms": 31240.5,
    "token_per_pass": 20.69,
    "latency_p50": 1240.2,
    "latency_p95": 4820.7
  }
}
```

- `status` 状态机见 §4。`failed`（run 级异常，如 Judge 全挂）时 `error` 带错误码。
- `skipped_reason`：✅ 已实现（2026-08-17 bug-fix：Judge 不可用时跳过该 case；pass_rate 计算排除 skipped，与契约一致）。
- `summary.pass_rate` 计算时**排除 skipped case**（现状逻辑一致，保留）。
- 落库位置：`data/runs/{project_id}/{run_id}.json`（➕ 新增存储层）。

---

## 2. 接口定义

### 2.1 Projects

| 接口 | 状态 | 说明 |
|---|---|---|
| `GET /api/projects` | ➕ | 列表。每项含 `last_run` 摘要（供列表页"最近评测"列 + 趋势 sparkline 数据） |
| `POST /api/projects` | ➕ | 新建。body 最少字段 `{name, task_shape}`，其余配置可后续 PUT |
| `GET /api/projects/{pid}` | ➕ | 详情（含完整 config，secret 字段 masked） |
| `PUT /api/projects/{pid}` | ➕ | 全量更新配置。secret 字段支持 `"__UNCHANGED__"` 哨兵值 |
| `DELETE /api/projects/{pid}` | ❌ | 不入 v0.1（决议：暂不做删除入口，避免误删历史数据） |

**`GET /api/projects` 响应示例**：

```json
{
  "projects": [
    {
      "id": "proj-01",
      "name": "客服Agent评测",
      "task_shape": "customer_service",
      "last_run": {
        "id": "run-1723987500123",
        "status": "completed",
        "created_at": "2026-08-18T14:05:00Z",
        "pass_rate": 0.875,
        "total_token": 42300
      },
      "trend": [
        { "run_id": "run-…", "pass_rate": 0.80 },
        { "run_id": "run-…", "pass_rate": 0.875 }
      ]
    }
  ]
}
```

- `last_run` 为 null 表示从未跑过；`trend` 返回最近 8 次 run 的 `(run_id, pass_rate)`，不足 8 条按实际数量。

### 2.2 EvalSets

| 接口 | 状态 | 说明 |
|---|---|---|
| `POST /api/evalsets` | ➕ | 新建空评测集，绑定 `project_id` |
| `GET /api/evalsets/{eid}` | ➕ | 详情（含全部 cases） |
| `PUT /api/evalsets/{eid}` | ➕ | **全量替换 cases**。前端持有完整状态提交；后端负责 case id 补发 |
| `POST /api/evalsets/{eid}/import` | ➕ | multipart 上传（`.csv` / `.json`），字段 `file` + `mode`（`merge` 默认 / `replace`） |
| `GET /api/evalsets/{eid}/export` | ➕ | 导出 CSV（带 UTF-8 BOM，Excel 兼容），列与 UI 表格一致，`eval_params` 序列化为 JSON 字符串 |

> 设计决策：**用例级 CRUD 用全量 PUT，不做单 case 端点**。JSON 文件存储下，前端每次编辑后整体提交最简单、无并发合并问题。前端 agent 注意：禁用 toggle、单条编辑都走同一条 PUT。

**CSV 导入列约定**：`case_name, input, expected_output, output_requirement, eval_type, eval_params(JSON), enabled`。缺列报 `import_format_error`。

### 2.3 Runs

| 接口 | 状态 | 说明 |
|---|---|---|
| `POST /api/runs` | 🔧 | 发起评测。body `{project_id, evalset_id}`。**落库即返回**，不等执行完成 |
| `GET /api/runs/{rid}` | ➕ | run 详情（含 results + summary + status）。前端轮询此接口 |
| `GET /api/projects/{pid}/runs` | ➕ | 历史列表（列表项不含 results，按 `created_at` 倒序） |
| `GET /api/runs/{rid}/export` | ➕ | 导出评测数据 CSV：每 case 一行（case_name / input / expected_output / actual_output / passed / score / latency_ms / token_used） |

**`POST /api/runs` 响应**：`201 {"run_id": "run-1723987500123", "status": "queued"}`

**`GET /api/projects/{pid}/runs` 响应示例**：

```json
{
  "runs": [
    {
      "id": "run-1723987500123",
      "evalset_id": "evalset-01",
      "status": "completed",
      "created_at": "2026-08-18T14:05:00Z",
      "summary": { "pass_rate": 0.875, "total_token": 42300, "total_latency_ms": 31240.5 }
    }
  ]
}
```

### 2.4 Test 端点（配置页三个"测试"按钮）

| 接口 | 状态 | 说明 |
|---|---|---|
| `POST /api/test/target` | ➕ | 用 project 的 target 配置发一个最小请求（内容 "ping"），返回耗时/token/错误。**body 直接携带完整 target_config**（不依赖已保存状态，方便未保存时测试） |
| `POST /api/test/parsing` | ➕ | body 携带 `{response_parsing: {...}, sample_response: "<JSON 字符串>"}`，返回输出提取结果 + token 计数/错误。替代原 test/mapping 设计 |
| `POST /api/test/judge` | ➕ | body 携带 judge_config + 一条样例 `{input, output_requirement, actual_output}`，返回判定输出 |

**`POST /api/test/target` 响应**：`{"ok": true, "latency_ms": 812.4, "token_used": 12, "status_code": 200}` 或 `{"ok": false, "error": {"code": "target_api_error", "message": "401 Unauthorized"}}`

### 2.5 Health

`GET /api/health` ✅ 现状已实现，返回 `{"status": "ok"}`，可扩展带 `active_runs` 计数。

---

## 3. 错误规范

统一格式：

```json
{ "error": { "code": "run_not_found", "message": "找不到 run：run-12345" } }
```

| code | HTTP | 场景 |
|---|---|---|
| `project_not_found` / `evalset_not_found` / `run_not_found` | 404 | 资源不存在 |
| `invalid_config` | 422 | 配置校验失败（base_url 缺失、auth 结构非法等） |
| `no_enabled_cases` | 422 | 发起评测时评测集无启用 case |
| `import_format_error` | 422 | CSV/JSON 导入格式非法（message 带行号） |
| `mapping_invalid` | 422 | JSONPath 非法或提取失败（message 带具体路径） |
| `target_api_error` | 502 | 被评测 API 返回错误（message 带 status_code） |
| `judge_api_error` | 502 | Judge API 错误 |
| `network_error` | 502 | 网络不可达（对应现状 `NetworkError`） |
| `internal_error` | 500 | 兜底 |

> 后端 agent 注意：现状 `judge.py` 的 `APIError / NetworkError / ResponseFormatError` 需映射到上表。

---

## 4. Run 状态机

```
POST /api/runs → queued（落库）
              → running（开始执行第一个 case）
              → completed（全部 case 执行完毕，含 judge 不可用时 skipped 的情况）
              → failed（run 级异常：如 Judge 在首个 llm_judge case 前就不可用、
                        配置非法导致无法启动、任务进程崩溃）
```

- **单 case 失败不置 run 为 failed**：case 标 `passed: false`，run 照常 completed。
- 前端轮询：`GET /api/runs/{rid}`，2s 起，指数退避封顶 10s；`document.visibilitychange` 隐藏时暂停，恢复时立即补拉。
- 关页面不打断任务：执行在服务端（FastAPI BackgroundTasks 起步），落库即与请求生命周期解耦。
- **并发（决议：v0.1 不做）**：不做并发控制与排队。允许多个 run 同时执行，各自独立落库（每个 run 独立文件，无共享状态竞争）；前端不做并发引导 UI。并发语义留给 v2。

---

## 5. 现状差距清单（后端 agent 工作台）

| # | 改动 | 涉及 | 状态 |
|---|---|---|---|
| 1 | `TargetConfig` 增 `auth` + `response_parsing`（四键模型，见 `docs/response-parsing-design.md`）；`JudgeConfig` 增 `prompt_template`；`EvalCase` 增 `id` + `enabled`；`EvalRun` 增 `status/started_at/finished_at/error` | models.py | 🔧 |
| 2 | run 落库 `data/runs/{pid}/{rid}.json` + 读写工具函数 | 新增 storage 模块 | ➕ |
| 3 | `POST /api/runs` 异步化：落库 + BackgroundTasks + 状态推进 | main.py + runner.py | 🔧 |
| 4 | `GET /api/projects`（含 last_run + trend）、`GET/PUT /api/projects/{pid}`、`POST /api/projects`（DELETE 不入 v0.1） | 新增 routes | ➕ |
| 5 | evalset 全部接口 + CSV 导入导出（BOM 处理） | 新增 routes + util | ➕ |
| 6 | run 查询接口（详情/历史/导出） | 新增 routes | ➕ |
| 7 | 3 个 test 端点 | 新增 routes | ➕ |
| 8 | 统一错误响应 + 错误码映射 | 全局 | 🔧 |
| 9 | `created_at` 改 UTC ISO 8601；run id 防撞车 | runner.py | 🔧 |

**建议实现顺序**：模型扩展(#1) → 存储(#2) → 异步 run(#3) → 查询接口(#4,#6) → 配置与测试端点(#7) → evalset 与导入导出(#5) → 错误规范收敛(#8)。

---

## 6. 评审决议（2026-08-18 已定）

| 开放项 | 决议 |
|---|---|
| Projects 列表 trend 数据 | **后端算好**（列表接口直接返回近 8 次 run 的 pass_rate），维持轻量前端 |
| DELETE /api/projects | **不入 v0.1**（§2.1 已标 ❌） |
| run 并发 | **v0.1 不做并发控制**（§4 已写入：允许多 run 并行、各自独立落库，无排队语义） |
