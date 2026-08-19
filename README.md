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

### 测试覆盖

| 模块 | 覆盖率 |
|------|--------|
| eval_types.py | 100% |
| models.py | 100% |
| main.py | 100% |
| judge.py | 83% |
| errors.py | 76% |
| routes.py | 72% |
| runner.py | 60% |
| storage.py | 59% |

### 测试结构

```
tests/
├── conftest.py          # 测试 fixtures 和配置
├── test_eval_types.py   # 评测类型函数测试
├── test_judge.py       # API 调用测试
├── test_runner.py       # 评测执行器测试
└── test_api.py         # API 接口测试
```

### 测试说明

- **eval_types**: 纯函数，测试 exact/contains/length 等评测逻辑
- **judge**: 使用 mock 测试 API 调用和错误处理
- **runner**: 测试评测执行流程和统计计算
- **api**: 使用 FastAPI TestClient 测试 REST API

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
├── tests/               # 测试文件
├── data/                # 数据存储目录
├── docs/                # 文档
└── requirements.txt
```

---

## API 文档

### 核心端点

| 接口 | 说明 |
|------|------|
| `GET /api/projects` | 项目列表 |
| `POST /api/projects` | 创建项目 |
| `GET /api/projects/{pid}` | 项目详情 |
| `PUT /api/projects/{pid}` | 更新项目 |
| `POST /api/evalsets` | 创建评测集 |
| `GET /api/evalsets/{eid}` | 评测集详情 |
| `PUT /api/evalsets/{eid}` | 更新评测集 |
| `POST /api/evalsets/{eid}/import` | 导入评测集 |
| `GET /api/evalsets/{eid}/export` | 导出评测集 |
| `POST /api/runs` | 发起评测 |
| `GET /api/runs/{rid}` | 评测结果 |
| `GET /api/projects/{pid}/runs` | 历史记录 |
| `POST /api/test/target` | 测试目标 API |
| `POST /api/test/judge` | 测试 Judge |
| `GET /api/health` | 健康检查 |

详细 API 契约见 [docs/api-contract.md](docs/api-contract.md)。
