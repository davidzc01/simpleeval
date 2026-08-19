# simpleEval

LLM 与 Agent（黑盒）评测工具。

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动服务

```bash
# 方式1：使用 Python 模块
python3 -m uvicorn app.main:app --reload --port 8000

# 方式2：直接运行 uvicorn（需先安装）
uvicorn app.main:app --reload --port 8000
```

启动后访问：
- Web UI: http://localhost:8000
- API 文档: http://localhost:8000/docs

---

## 功能特性

### 评测核心

- **5 种评测类型**：exact / contains / not_contains / length / llm_judge
- **成本分析**：pass rate + token 消耗 + 每万 token 完成率 + P50/P95 延迟
- **异步执行**：发起评测落库即返回，BackgroundTasks 后台执行，关页面不打断
- **JSON 存储**：评测集和结果可版本管理，无数据库依赖
- **用例 CRUD**：可视化新增/编辑弹窗（按 eval_type 动态切换字段：exact→expected_output、contains/not_contains→substring、length→min/max、llm_judge→output_requirement）+ 硬删除（红色入口 + 二次确认 + 影响说明）

### 采样稳定性

- **pass@k / pass^k 双指标**：基于全部历史 run 聚合，k=1/2/3
  - `pass@k = 1 - C(n-c,k)/C(n,k)`：k 次采样至少一次通过的概率（潜力上界）
  - `pass^k = C(c,k)/C(n,k)`：k 次采样全部通过的概率（稳定下界）
  - 两线夹缝宽度 = 不确定性，coverage 不足时标灰提示
- **SVG 折线图**：纯前端渲染，无图表库依赖

### 响应解析层

- **JSONPath 子集**：点分路径 `$.a.b.c` + 数组索引 `$.choices[0]` + 负索引 `$.data[-1]` + 通配符 `$.items[*].name`
- **四键模型**：`output_paths`（按序 fallback）/ `token_paths`（命中求和）/ `token_fields`（全树递归求和）/ `token_scope`（过滤）
- **测试面板**：粘贴样例响应即可验证解析配置，无需发起评测

### 配置与安全

- **Target API 双模式**：`openai_compatible`（自动注入 model + `/chat/completions`，model 必填）/ `custom`（纯模板渲染，不注入 model/messages，`request_template` 必填）；两种模式共享同一份 auth 与 response_parsing
- **响应解析可视化**（B-7）：四键行编辑器（output_paths / token_paths / token_fields / token_scope，每行 input + [×] 删除 + [+ 添加]）+ paths/fields/none 模式 radio 切换；**无需手写 JSONPath**——粘贴样例响应 → 渲染可折叠 JSON 树 → 点击节点自动生成路径填入"激活的"路径框（key/叶子/括号均可点选，括号点击折叠/展开子树）
- **多种认证方式**：none / bearer / api_key（自定义 header）/ cookie / headers
- **密钥哨兵值**：`__UNCHANGED__` 保留原值，掩码字符串不会覆盖真实 secret
- **XSS 防护**：所有用户输入（项目名、case 名等）经 `escapeHtml` 转义；删除按钮调用通过内部状态查找，不在 HTML 属性拼接用户输入

### 前端体验

- **概览指标卡 7 张**：pass rate / 总 token / token 量(K) / 每万 token 完成率 / P50 / P95 / 失败数，与 run 详情对齐
- **Token 预算 UI**：配置页 `limit` 输入 + `warn_only` checkbox，超限仅提醒不中断（MVP）
- **导入走后端端点**：`POST /evalsets/{id}/import?mode=merge|replace`，行级错误收集（422 + errors 不保存），支持对象 `eval_params` 与 `task_shape`；前端保留本地预览
- **导入弹窗双模式**：文件拖拽（.csv/.json）+ 逐条表单添加（按 eval_type 切换 expected/substring/min-max/output_requirement 字段，待导入列表可删，确认时序列化复用后端 import 端点）
- **侧栏同步**：active 用 URL hash 判断（不依赖异步 state）+ 项目数据缓存，快速连点多个项目高亮始终同步无闪烁
- **hash 路由**：`#/projects` / `#/project/{id}` / `#/run/{pid}/{rid}`，刷新不丢位置
- **静默轮询**：run 详情增量 DOM 更新（不整页重绘），指数退避封顶 10s，弹窗打开/页面隐藏时暂停
- **骨架屏**：列表/详情/run 详情初次加载扫光过渡
- **运行中任务胶囊**：侧栏底部全局指示器，点击跳转对应 run

---

## Web UI

### 页面结构

```
#/projects                       Projects 列表（入口）
#/project/{pid}                  Project 详情
   ├── 概览 tab                  指标卡 + 趋势图 + 采样稳定性卡片
   ├── 评测集 tab                多评测集选择 + 用例表格（新增/编辑/删除）+ 导入/导出
   ├── 配置 tab                  Target API（双模式）+ 响应解析 + LLM Judge
   └── 历史 tab                  全部 run 列表
#/run/{pid}/{rid}                Run 详情（进度 + 指标 + case 表格 + 三栏对比视图）
```

### 设计风格

- 开发者工具风 · 中性灰底 · 单强调色（#3b6fae）
- 代码类内容暗色块 + 等宽字体
- 语义色状态：✅ passed / ❌ failed / ⚡ running / ⏳ queued / ⬚ skipped

---

## 测试

### 运行测试

```bash
# 后端测试
python3 -m pytest tests/ -v

# 带覆盖率报告
python3 -m pytest tests/ --cov=app --cov-report=term-missing

# 生成 HTML 覆盖率报告
python3 -m pytest tests/ --cov=app --cov-report=html

# UI 组件测试（需 Node.js）
node tests/test_sampling_ui.js
node tests/test_evalset_ui.js
node tests/test_parsing_ui.js
```

### 测试结果

```
后端：249 passed in 1.9s   Coverage: 91%
UI 组件：82 passed（采样稳定性 26 + 评测集 CRUD 27 + 响应解析 JSON 树 29）
```

### 测试覆盖

| 模块 | 覆盖率 |
|------|--------|
| errors.py | 100% |
| eval_types.py | 100% |
| models.py | 100% |
| parser.py | 97% |
| sampling.py | 97% |
| runner.py | 96% |
| judge.py | 92% |
| routes.py | 80% |
| storage.py | 81% |
| main.py | 73% |

### 测试结构

```
tests/
├── conftest.py              # 测试 fixtures
├── test_eval_types.py       # 评测类型函数测试
├── test_judge.py            # LLM-as-Judge 调用测试
├── test_runner.py           # 评测执行器测试
├── test_storage.py          # 存储模块测试
├── test_errors.py           # 错误处理测试
├── test_parser.py           # 响应解析层测试（JSONPath 子集）
├── test_sampling.py         # 采样稳定性测试（pass@k/pass^k 数学 + API）
├── test_sampling_ui.js      # 采样卡片 UI 组件测试（SVG 渲染）
├── test_evalset_ui.js       # 评测集用例 CRUD UI 组件测试（B-5）
├── test_parsing_ui.js       # 响应解析 JSON 树 UI 组件测试（B-7）
└── test_api.py              # API 接口测试
```

---

## 项目结构

```
simpleeval/
├── app/
│   ├── main.py              # FastAPI 入口 + 静态文件
│   ├── models.py            # Pydantic 数据模型
│   ├── eval_types.py        # 评测类型实现
│   ├── judge.py             # LLM-as-Judge 调用
│   ├── runner.py            # 评测执行器（异步 + 状态机）
│   ├── parser.py            # 响应解析层（JSONPath 子集 + 四键模型）
│   ├── sampling.py          # 采样稳定性计算（pass@k / pass^k）
│   ├── storage.py           # JSON 文件存储
│   ├── errors.py            # 统一错误处理
│   ├── routes.py            # API 路由
│   └── static/
│       └── index.html       # Web UI（单文件应用）
├── tests/                   # 测试文件
├── data/                    # 数据存储目录
├── docs/                    # 文档（API 契约、UI 规范、解析设计）
└── requirements.txt
```

---

## API 文档

### 核心端点

| 接口 | 说明 |
|------|------|
| `GET /api/projects` | 项目列表（含 last_run + trend） |
| `POST /api/projects` | 创建项目 |
| `GET /api/projects/{pid}` | 项目详情（secret 字段 masked） |
| `PUT /api/projects/{pid}` | 更新项目（支持 `__UNCHANGED__` 哨兵值） |
| `GET /api/projects/{pid}/evalsets` | 列出项目下全部评测集 |
| `GET /api/projects/{pid}/runs` | 历史记录 |
| `GET /api/projects/{pid}/sampling` | 采样稳定性（pass@k / pass^k） |
| `POST /api/evalsets` | 创建评测集 |
| `GET /api/evalsets/{eid}` | 评测集详情 |
| `PUT /api/evalsets/{eid}` | 更新评测集 |
| `POST /api/evalsets/{eid}/import` | 导入评测集（CSV/JSON） |
| `GET /api/evalsets/{eid}/export` | 导出评测集（CSV） |
| `POST /api/runs` | 发起评测（异步，落库即返回） |
| `GET /api/runs/{rid}` | 评测结果（轮询） |
| `GET /api/runs/{rid}/export` | 导出结果（CSV） |
| `POST /api/test/target` | 测试目标 API（支持哨兵值） |
| `POST /api/test/parsing` | 测试响应解析（样例响应 → 输出 + token） |
| `POST /api/test/mapping` | 测试响应映射（旧版兼容） |
| `POST /api/test/judge` | 测试 Judge（支持哨兵值） |
| `GET /api/health` | 健康检查 |

详细 API 契约见 [docs/api-contract.md](docs/api-contract.md)。
UI 设计规范见 [docs/ui-spec.md](docs/ui-spec.md)。
响应解析设计见 [docs/response-parsing-design.md](docs/response-parsing-design.md)。
