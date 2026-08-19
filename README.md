# simpleEval

LLM 与 Agent（黑盒）评测工具。

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动服务

```bash
uvicorn app.main:app --reload --port 8000
```

服务启动后访问 http://localhost:8000/docs 查看 API 文档。

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
├── conftest.py           # 测试 fixtures 和配置
├── test_eval_types.py    # 评测类型函数测试
├── test_judge.py        # API 调用测试（含边界情况）
├── test_runner.py        # 评测执行器测试（含异步执行）
├── test_storage.py       # 存储模块测试
├── test_errors.py        # 错误处理测试
└── test_api.py          # API 接口测试
```

### 测试说明

- **eval_types**: 纯函数，测试 exact/contains/not_contains/length 等评测逻辑（100% 覆盖）
- **judge**: 使用 mock 测试 API 调用、错误处理、响应映射、JSON 模板等（93% 覆盖）
- **runner**: 测试评测执行流程、分位数计算、异步执行、Judge 可用性检查等（95% 覆盖）
- **storage**: 测试项目、评测集、Run 的增删改查（81% 覆盖）
- **errors**: 测试所有错误码和错误格式（100% 覆盖）
- **api**: 使用 FastAPI TestClient 测试 REST API 端点（83% 覆盖）

---

## 项目结构

```
simpleeval/
├── app/
│   ├── main.py          # FastAPI 入口
│   ├── models.py        # Pydantic 数据模型
│   ├── eval_types.py    # 评测类型实现
│   ├── judge.py         # LLM-as-Judge 调用
│   ├── runner.py        # 评测执行器
│   ├── storage.py       # JSON 文件存储
│   ├── errors.py        # 统一错误处理
│   └── routes.py        # API 路由
├── tests/               # 测试文件（141 个测试）
├── data/                # 数据存储目录
├── docs/                # 文档
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
