"""测试 fixtures 和配置"""

import pytest
import tempfile
import shutil
from pathlib import Path

# 设置测试数据目录
TEST_DATA_DIR = Path(tempfile.mkdtemp())


@pytest.fixture(autouse=True)
def setup_test_env(monkeypatch):
    """为所有测试设置隔离的环境"""
    # 临时数据目录
    test_dir = Path(tempfile.mkdtemp())

    # Patch storage 模块的数据目录
    import app.storage as storage_module
    monkeypatch.setattr(storage_module, "DATA_DIR", test_dir)
    monkeypatch.setattr(storage_module, "PROJECTS_DIR", test_dir / "projects")
    monkeypatch.setattr(storage_module, "EVALSETS_DIR", test_dir / "evalsets")
    monkeypatch.setattr(storage_module, "RUNS_DIR", test_dir / "runs")
    # REQ-16/T2-3: 全局配置文件也隔离到临时目录，避免污染真实 data/
    monkeypatch.setattr(storage_module, "JUDGE_CONFIGS_FILE", test_dir / "judge-configs.json")
    monkeypatch.setattr(storage_module, "CONFIG_TEMPLATES_FILE", test_dir / "config-templates.json")

    # 创建目录
    (test_dir / "projects").mkdir(parents=True, exist_ok=True)
    (test_dir / "evalsets").mkdir(parents=True, exist_ok=True)
    (test_dir / "runs").mkdir(parents=True, exist_ok=True)

    yield test_dir

    # 清理
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.fixture
def sample_project():
    """示例项目数据"""
    return {
        "id": "proj-test-001",
        "name": "测试项目",
        "task_shape": "general",
        "judge_config": {
            "base_url": "https://api.example.com/v1",
            "api_key": "test-key-123",
            "model": "gpt-4o-mini",
            "prompt_template": "判断是否满足要求。",
        },
        "target_config": {
            "base_url": "https://api.example.com/v1",
            "api_key": "test-key-456",
            "model": "gpt-3.5-turbo",
            "request_template": "{input}",
            "auth": {"type": "none"},
            "response_mapping": [],
        },
    }


@pytest.fixture
def sample_evalset():
    """示例评测集数据"""
    return {
        "id": "evalset-test-001",
        "project_id": "proj-test-001",
        "name": "测试评测集",
        "cases": [
            {
                "id": "case-001",
                "case_name": "精确匹配测试",
                "input": "你好",
                "expected_output": "你好！",
                "output_requirement": None,
                "eval_type": "exact",
                "eval_params": {},
                "task_shape": None,
                "enabled": True,
            },
            {
                "id": "case-002",
                "case_name": "包含测试",
                "input": "查询天气",
                "expected_output": None,
                "output_requirement": "天气相关信息",
                "eval_type": "contains",
                "eval_params": {"substring": "天气"},
                "task_shape": None,
                "enabled": True,
            },
            {
                "id": "case-003",
                "case_name": "不包含测试",
                "input": "查询天气",
                "expected_output": None,
                "output_requirement": None,
                "eval_type": "not_contains",
                "eval_params": {"substring": "错误"},
                "task_shape": None,
                "enabled": True,
            },
            {
                "id": "case-004",
                "case_name": "长度测试",
                "input": "短文本",
                "expected_output": None,
                "output_requirement": None,
                "eval_type": "length",
                "eval_params": {"min": 1, "max": 10},
                "task_shape": None,
                "enabled": True,
            },
            {
                "id": "case-005",
                "case_name": "LLM Judge测试",
                "input": "评价这个回复",
                "expected_output": None,
                "output_requirement": "回复需要有礼貌",
                "eval_type": "llm_judge",
                "eval_params": {},
                "task_shape": None,
                "enabled": True,
            },
            {
                "id": "case-006",
                "case_name": "禁用测试",
                "input": "这应该被跳过",
                "expected_output": "结果",
                "eval_type": "exact",
                "eval_params": {},
                "task_shape": None,
                "enabled": False,
            },
        ],
    }


@pytest.fixture
def mock_api_response():
    """模拟 API 响应"""
    return {
        "choices": [
            {
                "message": {
                    "content": "这是一个测试回复"
                }
            }
        ],
        "usage": {
            "total_tokens": 100
        }
    }


@pytest.fixture
def mock_judge_response():
    """模拟 Judge 响应"""
    return {
        "choices": [
            {
                "message": {
                    "content": "0.85"
                }
            }
        ],
        "usage": {
            "total_tokens": 50
        }
    }
