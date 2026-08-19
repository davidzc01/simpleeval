"""API 接口测试"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
import json

from app.main import app


@pytest.fixture
def client():
    """测试客户端"""
    return TestClient(app)


class TestHealth:
    """健康检查接口测试"""

    def test_health_check(self, client):
        """健康检查返回 ok"""
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestProjectsAPI:
    """Projects 接口测试"""

    def test_list_projects_empty(self, client):
        """空列表"""
        response = client.get("/api/projects")
        assert response.status_code == 200
        assert response.json() == {"projects": []}

    def test_create_project(self, client):
        """创建项目"""
        response = client.post(
            "/api/projects",
            json={"name": "测试项目", "task_shape": "general"}
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "测试项目"
        assert data["task_shape"] == "general"
        assert "id" in data
        assert data["judge_config"]["api_key"] == {"masked": True}

    def test_get_project(self, client):
        """获取项目详情"""
        # 先创建
        create_response = client.post(
            "/api/projects",
            json={"name": "测试项目"}
        )
        project_id = create_response.json()["id"]

        # 再获取
        response = client.get(f"/api/projects/{project_id}")
        assert response.status_code == 200
        assert response.json()["id"] == project_id

    def test_get_project_not_found(self, client):
        """项目不存在"""
        response = client.get("/api/projects/nonexistent")
        assert response.status_code == 404

    def test_update_project(self, client):
        """更新项目"""
        # 创建
        create_response = client.post(
            "/api/projects",
            json={"name": "旧名称", "task_shape": "general"}
        )
        project_id = create_response.json()["id"]

        # 更新
        update_data = {
            "id": project_id,
            "name": "新名称",
            "task_shape": "customer_service",
            "judge_config": {
                "base_url": "https://api.example.com/v1",
                "api_key": "new-key",
                "model": "gpt-4"
            },
            "target_config": {
                "base_url": "https://api.example.com/v1",
                "api_key": "new-key",
                "model": "gpt-3.5"
            }
        }
        response = client.put(f"/api/projects/{project_id}", json=update_data)
        assert response.status_code == 200
        assert response.json()["name"] == "新名称"


class TestEvalSetsAPI:
    """EvalSets 接口测试"""

    def test_create_evalset(self, client):
        """创建评测集"""
        # 先创建项目
        project_response = client.post(
            "/api/projects",
            json={"name": "测试项目"}
        )
        project_id = project_response.json()["id"]

        # 创建评测集
        response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "测试评测集"}
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "测试评测集"
        assert data["cases"] == []

    def test_create_evalset_project_not_found(self, client):
        """项目不存在"""
        response = client.post(
            "/api/evalsets",
            json={"project_id": "nonexistent", "name": "测试"}
        )
        assert response.status_code == 404

    def test_get_evalset(self, client):
        """获取评测集"""
        # 创建项目和评测集
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "评测集"}
        )
        evalset_id = evalset_response.json()["id"]

        # 获取
        response = client.get(f"/api/evalsets/{evalset_id}?project_id={project_id}")
        assert response.status_code == 200
        assert response.json()["id"] == evalset_id

    def test_update_evalset(self, client):
        """更新评测集"""
        # 创建
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "评测集"}
        )
        evalset_id = evalset_response.json()["id"]

        # 更新
        update_data = {
            "id": evalset_id,
            "project_id": project_id,
            "name": "新名称",
            "cases": [
                {
                    "id": "case-1",
                    "case_name": "测试case",
                    "input": "你好",
                    "expected_output": "你好！",
                    "eval_type": "exact",
                    "enabled": True
                }
            ]
        }
        response = client.put(f"/api/evalsets/{evalset_id}", json=update_data)
        assert response.status_code == 200
        assert response.json()["name"] == "新名称"
        assert len(response.json()["cases"]) == 1

    def test_import_evalset_csv(self, client):
        """导入 CSV 评测集"""
        # 创建
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "评测集"}
        )
        evalset_id = evalset_response.json()["id"]

        # CSV 内容 - 作为 form data 发送
        csv_content = """id,case_name,input,expected_output,eval_type,eval_params,enabled
case-1,测试1,你好,你好！,exact,{},true
case-2,测试2,天气,天气好,contains,"{""substring"": ""天气""}",true"""

        response = client.post(
            f"/api/evalsets/{evalset_id}/import?project_id={project_id}&mode=replace",
            data={"file_content": csv_content}
        )
        assert response.status_code == 200
        assert len(response.json()["cases"]) == 2

    def test_import_evalset_json(self, client):
        """导入 JSON 评测集"""
        # 创建
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "评测集"}
        )
        evalset_id = evalset_response.json()["id"]

        # JSON 内容 - 作为 form data 发送
        json_content = json.dumps([
            {
                "case_name": "测试1",
                "input": "你好",
                "expected_output": "你好！",
                "eval_type": "exact"
            }
        ])

        response = client.post(
            f"/api/evalsets/{evalset_id}/import?project_id={project_id}&mode=replace",
            data={"file_content": json_content}
        )
        assert response.status_code == 200
        assert len(response.json()["cases"]) == 1

    def test_export_evalset(self, client):
        """导出评测集"""
        # 创建
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "评测集"}
        )
        evalset_id = evalset_response.json()["id"]

        # 导出
        response = client.get(f"/api/evalsets/{evalset_id}/export?project_id={project_id}")
        assert response.status_code == 200
        data = response.json()
        assert "content" in data
        assert data["filename"] == "评测集.csv"


class TestRunsAPI:
    """Runs 接口测试"""

    def test_create_run(self, client):
        """发起评测"""
        # 创建项目和评测集
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={
                "project_id": project_id,
                "name": "评测集",
                "cases": [
                    {
                        "id": "case-1",
                        "case_name": "测试",
                        "input": "你好",
                        "expected_output": "你好！",
                        "eval_type": "exact",
                        "enabled": True
                    }
                ]
            }
        )
        evalset_id = evalset_response.json()["id"]

        # Mock 异步执行
        with patch("app.routes.execute_run"):
            response = client.post(
                "/api/runs",
                json={"project_id": project_id, "evalset_id": evalset_id}
            )
        assert response.status_code == 201
        assert "run_id" in response.json()
        assert response.json()["status"] == "queued"

    def test_create_run_no_enabled_cases(self, client):
        """无启用 case"""
        # 创建项目和评测集
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={
                "project_id": project_id,
                "name": "评测集",
                "cases": [
                    {
                        "id": "case-1",
                        "case_name": "测试",
                        "input": "你好",
                        "expected_output": "你好！",
                        "eval_type": "exact",
                        "enabled": False
                    }
                ]
            }
        )
        evalset_id = evalset_response.json()["id"]

        response = client.post(
            "/api/runs",
            json={"project_id": project_id, "evalset_id": evalset_id}
        )
        assert response.status_code == 422

    def test_list_project_runs(self, client):
        """项目历史列表"""
        # 创建项目和评测集
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={
                "project_id": project_id,
                "name": "评测集",
                "cases": [
                    {
                        "id": "case-1",
                        "case_name": "测试",
                        "input": "你好",
                        "expected_output": "你好！",
                        "eval_type": "exact",
                        "enabled": True
                    }
                ]
            }
        )
        evalset_id = evalset_response.json()["id"]

        # Mock 异步执行
        with patch("app.routes.execute_run"):
            # 发起评测
            client.post(
                "/api/runs",
                json={"project_id": project_id, "evalset_id": evalset_id}
            )

        # 列表
        response = client.get(f"/api/projects/{project_id}/runs")
        assert response.status_code == 200
        assert "runs" in response.json()


class TestTestEndpoints:
    """Test 端点测试"""

    def test_test_target_success(self, client):
        """测试目标 API - 成功"""
        with patch("app.routes.call_target", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = ("测试回复", 100)

            response = client.post(
                "/api/test/target",
                json={
                    "base_url": "https://api.example.com/v1",
                    "api_key": "test-key",
                    "model": "gpt-3.5-turbo",
                    "request_template": "{input}"
                }
            )
            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is True
            assert "latency_ms" in data

    def test_test_target_network_error(self, client):
        """测试目标 API - 网络错误"""
        from app.judge import NetworkError
        with patch("app.routes.call_target", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = NetworkError("连接失败")

            response = client.post(
                "/api/test/target",
                json={
                    "base_url": "https://api.example.com/v1",
                    "api_key": "test-key",
                    "model": "gpt-3.5-turbo",
                    "request_template": "{input}"
                }
            )
            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is False
            assert "error" in data

    def test_test_mapping(self, client):
        """测试映射提取"""
        response = client.post(
            "/api/test/mapping",
            json={
                "response_mapping": [
                    {"name": "reply", "jsonpath": "$.data.reply"}
                ],
                "sample_response": json.dumps({"data": {"reply": "测试回复"}})
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "测试回复" in data["result"]

    def test_test_judge_success(self, client):
        """测试 Judge - 成功"""
        with patch("app.routes.judge_with_llm", new_callable=AsyncMock) as mock_judge:
            mock_judge.return_value = 0.8

            response = client.post(
                "/api/test/judge",
                json={
                    "base_url": "https://api.example.com/v1",
                    "api_key": "test-key",
                    "model": "gpt-4o-mini",
                    "prompt_template": "判断是否满足要求：{requirement}",
                    "input": "你好",
                    "output_requirement": "需要礼貌",
                    "actual_output": "好的，请问有什么可以帮助您的？"
                }
            )
            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is True
            assert data["score"] == 0.8
            assert data["passed"] is True


class TestErrorHandling:
    """错误处理测试"""

    def test_404_error_format(self, client):
        """404 错误格式"""
        response = client.get("/api/projects/nonexistent")
        assert response.status_code == 404
        data = response.json()
        assert "detail" in data
        assert "error" in data["detail"]
        assert data["detail"]["error"]["code"] == "project_not_found"

    def test_422_validation_error(self, client):
        """422 验证错误"""
        response = client.post(
            "/api/evalsets",
            json={"project_id": "test"}  # 缺少 name
        )
        assert response.status_code == 422


class TestRunDetailAPI:
    """Run 详情接口测试"""

    def test_get_run_detail(self, client):
        """获取 run 详情"""
        # 创建项目和评测集
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={
                "project_id": project_id,
                "name": "评测集",
                "cases": [
                    {
                        "id": "case-1",
                        "case_name": "测试",
                        "input": "你好",
                        "expected_output": "你好！",
                        "eval_type": "exact",
                        "enabled": True
                    }
                ]
            }
        )
        evalset_id = evalset_response.json()["id"]

        # Mock 异步执行
        with patch("app.routes.execute_run"):
            create_response = client.post(
                "/api/runs",
                json={"project_id": project_id, "evalset_id": evalset_id}
            )
        run_id = create_response.json()["run_id"]

        # 获取详情
        response = client.get(f"/api/runs/{run_id}?project_id={project_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == run_id
        assert "results" in data

    def test_get_run_not_found(self, client):
        """Run 不存在"""
        response = client.get("/api/runs/nonexistent?project_id=proj-123")
        assert response.status_code == 404


class TestExportEndpoints:
    """导出接口测试"""

    def test_export_run_csv(self, client):
        """导出 run 结果"""
        # 创建项目和评测集
        project_response = client.post("/api/projects", json={"name": "测试"})
        project_id = project_response.json()["id"]

        evalset_response = client.post(
            "/api/evalsets",
            json={
                "project_id": project_id,
                "name": "评测集",
                "cases": [
                    {
                        "id": "case-1",
                        "case_name": "测试",
                        "input": "你好",
                        "expected_output": "你好！",
                        "eval_type": "exact",
                        "enabled": True
                    }
                ]
            }
        )
        evalset_id = evalset_response.json()["id"]

        # Mock 异步执行
        with patch("app.routes.execute_run"):
            create_response = client.post(
                "/api/runs",
                json={"project_id": project_id, "evalset_id": evalset_id}
            )
        run_id = create_response.json()["run_id"]

        # 导出
        response = client.get(f"/api/runs/{run_id}/export?project_id={project_id}")
        assert response.status_code == 200
        data = response.json()
        assert "content" in data
        assert data["filename"] == f"run-{run_id}.csv"

    def test_export_run_not_found(self, client):
        """导出不存在的 run"""
        response = client.get("/api/runs/nonexistent/export?project_id=proj-123")
        assert response.status_code == 404


class TestTestEndpointsExtended:
    """Test 端点扩展测试"""

    def test_test_target_api_error(self, client):
        """测试目标 API - API 错误"""
        from app.judge import APIError
        with patch("app.routes.call_target", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = APIError("401 Unauthorized", 401)

            response = client.post(
                "/api/test/target",
                json={
                    "base_url": "https://api.example.com/v1",
                    "api_key": "test-key",
                    "model": "gpt-3.5-turbo",
                    "request_template": "{input}"
                }
            )
            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is False
            assert "error" in data

    def test_test_target_mapping_error(self, client):
        """测试目标 API - 映射错误"""
        from app.judge import ResponseFormatError
        with patch("app.routes.call_target", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = ResponseFormatError("映射失败")

            response = client.post(
                "/api/test/target",
                json={
                    "base_url": "https://api.example.com/v1",
                    "api_key": "test-key",
                    "model": "gpt-3.5-turbo",
                    "request_template": "{input}"
                }
            )
            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is False

    def test_test_judge_network_error(self, client):
        """测试 Judge - 网络错误"""
        from app.judge import NetworkError
        with patch("app.routes.judge_with_llm", new_callable=AsyncMock) as mock_judge:
            mock_judge.side_effect = NetworkError("连接超时")

            response = client.post(
                "/api/test/judge",
                json={
                    "base_url": "https://api.example.com/v1",
                    "api_key": "test-key",
                    "model": "gpt-4o-mini",
                    "prompt_template": "判断",
                    "input": "你好",
                    "output_requirement": "需要礼貌",
                    "actual_output": "好的"
                }
            )
            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is False

    def test_test_judge_api_error(self, client):
        """测试 Judge - API 错误"""
        from app.judge import APIError
        with patch("app.routes.judge_with_llm", new_callable=AsyncMock) as mock_judge:
            mock_judge.side_effect = APIError("500 Server Error", 500)

            response = client.post(
                "/api/test/judge",
                json={
                    "base_url": "https://api.example.com/v1",
                    "api_key": "test-key",
                    "model": "gpt-4o-mini",
                    "prompt_template": "判断",
                    "input": "你好",
                    "output_requirement": "需要礼貌",
                    "actual_output": "好的"
                }
            )
            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is False

    def test_test_mapping_invalid(self, client):
        """测试映射 - 无效映射"""
        from app.judge import ResponseFormatError
        with patch("app.judge._extract_response") as mock_extract:
            mock_extract.side_effect = ResponseFormatError("路径不存在")

            response = client.post(
                "/api/test/mapping",
                json={
                    "response_mapping": [
                        {"name": "reply", "jsonpath": "$.invalid.path"}
                    ],
                    "sample_response": json.dumps({"data": {"reply": "测试"}})
                }
            )
            assert response.status_code == 422
