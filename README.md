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

- **4 种评测类型**：exact / contains / not_contains / length / llm_judge
- **成本分析**：pass rate + token 消耗 + 每万 token 完成率
- **异步执行**：发起评测不阻塞，支持长任务
- **JSON 存储**：评测集和结果可版本管理

---

## Web UI

### 页面结构

```
/                           Projects 列表（入口）
/p/:pid                  Project 详情
   ├── 概览 tab           最近 run + 趋势图 + 指标卡
   ├── 评测集 tab         用例表格 + 导入/导出
   ├── 配置 tab           Target API / Judge 配置
   └── 历史 tab           全部 run 列表
/r/:rid                    Run 详情（三栏对比视图）
```

### 设计风格

- 开发者工具风 · 中性灰底 · 单强调色（#3b6fae）
- 代码类内容暗色块 + 等宽字体
- 语义色状态：✅ passed / ❌ failed / ⚡ running / ⏳ queued

---

## 测试

### 运行测试

```bash
# 运行所有测试
python3 -m pytest tests/ -v

# 带覆盖率报告
python3 -m pytest tests/ --cov=app --cov-report=term-missing

# 生成 HTML 覆盖率报告
python3 -m pytest tests/ --cov=app --cov-report=html
```

### 测试结果

```
141 passed in 0.67s
Coverage: 90%
```

### 测试覆盖

| 模块 | 覆盖率 |
|------|--------|
| eval_types.py | 100% |
| models.py | 100% |
| main.py | 100% |
| errors.py | 100% |
| runner.py | 95% |
| judge.py | 93% |
| routes.py | 83% |
| storage.py | 81% |

### 测试结构

```
tests/
├── conftest.py           # 测试 fixtures
├── test_eval_types.py    # 评测类型函数测试
├── test_judge.py        # API 调用测试
├── test_runner.py       # 评测执行器测试
├── test_storage.py      # 存储模块测试
├── test_errors.py       # 错误处理测试
└── test_api.py         # API 接口测试
```

---

## 项目结构

```
simpleeval/
├── app/
│   ├── main.py          # FastAPI 入口 + 静态文件
│   ├── models.py       # Pydantic 数据模型
│   ├── eval_types.py   # 评测类型实现
│   ├── judge.py        # LLM-as-Judge 调用
│   ├── runner.py       # 评测执行器
│   ├── storage.py      # JSON 文件存储
│   ├── errors.py       # 统一错误处理
│   ├── routes.py       # API 路由
│   └── static/
│       └── index.html  # Web UI（单文件应用）
├── tests/               # 测试文件
├── data/               # 数据存储目录
├── docs/               # 文档（API 契约、UI 规范）
└── requirements.txt
```

---

## API 文档

### 核心端点

| 接口 | 说明 |
|------|------|
| `GET /api/projects` | 项目列表（含 last_run + trend） |
| `POST /api/projects` | 创建项目 |
| `GET /api/projects/{pid}` | 项目详情 |
| `PUT /api/projects/{pid}` | 更新项目 |
| `POST /api/evalsets` | 创建评测集 |
| `GET /api/evalsets/{eid}` | 评测集详情 |
| `PUT /api/evalsets/{eid}` | 更新评测集 |
| `POST /api/evalsets/{eid}/import` | 导入评测集（CSV/JSON） |
| `GET /api/evalsets/{eid}/export` | 导出评测集（CSV） |
| `POST /api/runs` | 发起评测（异步） |
| `GET /api/runs/{rid}` | 评测结果 |
| `GET /api/projects/{pid}/runs` | 历史记录 |
| `GET /api/runs/{rid}/export` | 导出结果（CSV） |
| `POST /api/test/target` | 测试目标 API |
| `POST /api/test/mapping` | 测试响应映射 |
| `POST /api/test/judge` | 测试 Judge |
| `GET /api/health` | 健康检查 |

详细 API 契约见 [docs/api-contract.md](docs/api-contract.md)。
UI 设计规范见 [docs/ui-spec.md](docs/ui-spec.md)。
