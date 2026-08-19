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

    def test_list_projects_last_run_full_fields(self, client):
        """P2-2: GET /api/projects 的 last_run 暴露完整指标字段（P50/P95/token_per_pass/failed_count）"""
        from app.storage import save_project, save_run
        from app.models import Project, JudgeConfig, TargetConfig, EvalRun, EvalSummary, CaseResult

        # 创建项目 + 一次完成的 run
        proj = Project(
            id="proj-p2-full",
            name="P2 完整字段",
            task_shape="general",
            judge_config=JudgeConfig(base_url="https://j.example.com", api_key="k", model="gpt-4"),
            target_config=TargetConfig(base_url="https://t.example.com", api_key="k", model="gpt-3.5"),
        )
        save_project(proj)
        save_run(EvalRun(
            id="run-full",
            project_id="proj-p2-full",
            evalset_id="es",
            status="completed",
            created_at="2026-08-19T10:00:00Z",
            results=[
                CaseResult(case_name="a", passed=True, actual_output="ok", token_used=10, latency_ms=100),
                CaseResult(case_name="b", passed=False, actual_output="bad", token_used=20, latency_ms=200),
                CaseResult(case_name="c", passed=True, skipped_reason="skipped", actual_output="", token_used=0, latency_ms=0),
            ],
            summary=EvalSummary(
                pass_rate=2/3,
                total_token=30,
                total_latency_ms=300,
                token_per_pass=10000 * 2 / 30,
                latency_p50=100,
                latency_p95=200,
            )
        ))

        resp = client.get("/api/projects")
        last_run = [p for p in resp.json()["projects"] if p["id"] == "proj-p2-full"][0]["last_run"]
        assert last_run is not None
        # P2-2 暴露的完整字段
        assert last_run["pass_rate"] == 2/3
        assert last_run["total_token"] == 30
        assert last_run["token_per_pass"] == 10000 * 2 / 30
        assert last_run["latency_p50"] == 100
        assert last_run["latency_p95"] == 200
        # failed_count = 1（只算 !passed && !skipped_reason 的；c 被跳过不计）
        assert last_run["failed_count"] == 1

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

    def test_update_project_with_sentinel(self, client):
        """更新项目时使用 __UNCHANGED__ 哨兵值保留原 api_key"""
        create_response = client.post(
            "/api/projects",
            json={"name": "哨兵测试", "task_shape": "general"}
        )
        project_id = create_response.json()["id"]

        # 设置真实 api_key + model（A-4: openai_compatible 模式 model 必填）
        proj = client.get(f"/api/projects/{project_id}").json()
        proj["judge_config"]["api_key"] = "sk-real-secret-123"
        proj["target_config"]["api_key"] = "sk-target-real-456"
        proj["target_config"]["model"] = "gpt-4o-mini"
        client.put(f"/api/projects/{project_id}", json=proj)

        # 用哨兵值更新其它字段，不应覆盖原 key
        proj = client.get(f"/api/projects/{project_id}").json()
        proj["name"] = "改名后"
        proj["judge_config"]["api_key"] = "__UNCHANGED__"
        proj["target_config"]["api_key"] = "__UNCHANGED__"
        response = client.put(f"/api/projects/{project_id}", json=proj)
        assert response.status_code == 200
        assert response.json()["name"] == "改名后"
        # 验证 api_key 仍是真实 key（不是哨兵值，不是掩码）
        # GET 接口会返回掩码，所以只验证不是 "__UNCHANGED__"
        assert response.json()["judge_config"]["api_key"] != "__UNCHANGED__"

    def test_update_project_token_budget(self, client):
        """P2-6: PUT /projects 支持 token_budget 字段（设置 / 清空 / warn_only 切换）"""
        create_response = client.post(
            "/api/projects",
            json={"name": "P2-6 预算", "task_shape": "general"}
        )
        project_id = create_response.json()["id"]
        # 设置 model（openai_compatible 必填）
        proj = client.get(f"/api/projects/{project_id}").json()
        proj["target_config"]["model"] = "gpt-4o"
        proj["target_config"]["api_key"] = "__UNCHANGED__"
        proj["judge_config"]["api_key"] = "__UNCHANGED__"

        # 1) 设置 token_budget
        proj["token_budget"] = {"limit": 1000000, "warn_only": True}
        r = client.put(f"/api/projects/{project_id}", json=proj)
        assert r.status_code == 200
        assert r.json()["token_budget"] == {"limit": 1000000, "warn_only": True}

        # 2) warn_only 切换为 False（中断模式）
        proj = client.get(f"/api/projects/{project_id}").json()
        # GET 返回掩码 api_key={masked:True}，PUT 时必须显式设哨兵值
        proj["target_config"]["api_key"] = "__UNCHANGED__"
        proj["judge_config"]["api_key"] = "__UNCHANGED__"
        proj["token_budget"] = {"limit": 500000, "warn_only": False}
        r = client.put(f"/api/projects/{project_id}", json=proj)
        assert r.status_code == 200, r.text
        assert r.json()["token_budget"] == {"limit": 500000, "warn_only": False}

        # 3) 清空预算（token_budget = null）
        proj = client.get(f"/api/projects/{project_id}").json()
        proj["target_config"]["api_key"] = "__UNCHANGED__"
        proj["judge_config"]["api_key"] = "__UNCHANGED__"
        proj["token_budget"] = None
        r = client.put(f"/api/projects/{project_id}", json=proj)
        assert r.status_code == 200, r.text
        assert r.json()["token_budget"] is None

    def test_update_project_with_sentinel_storage(self, client):
        """更新项目时使用 __UNCHANGED__ 哨兵值保留原 api_key（验证存储）"""
        create_response = client.post(
            "/api/projects",
            json={"name": "哨兵测试", "task_shape": "general"}
        )
        project_id = create_response.json()["id"]

        # 设置真实 api_key + model
        proj = client.get(f"/api/projects/{project_id}").json()
        proj["judge_config"]["api_key"] = "sk-real-secret-123"
        proj["target_config"]["api_key"] = "sk-target-real-456"
        proj["target_config"]["model"] = "gpt-4o-mini"
        client.put(f"/api/projects/{project_id}", json=proj)

        # 用哨兵值更新其它字段，不应覆盖原 key
        proj = client.get(f"/api/projects/{project_id}").json()
        proj["name"] = "改名后"
        proj["judge_config"]["api_key"] = "__UNCHANGED__"
        proj["target_config"]["api_key"] = "__UNCHANGED__"
        response = client.put(f"/api/projects/{project_id}", json=proj)
        assert response.status_code == 200
        assert response.json()["name"] == "改名后"
        # 接口仍返回掩码
        assert response.json()["judge_config"]["api_key"] == {"masked": True}

        # 直接读存储验证原 key 仍在
        import json
        from app.storage import PROJECTS_DIR
        proj_file = PROJECTS_DIR / f"{project_id}.json"
        stored = json.loads(proj_file.read_text(encoding="utf-8"))
        assert stored["judge_config"]["api_key"] == "sk-real-secret-123"
        assert stored["target_config"]["api_key"] == "sk-target-real-456"

    def test_update_project_openai_compat_requires_model(self, client):
        """A-4: openai_compatible 模式无 model → 422"""
        create_response = client.post(
            "/api/projects",
            json={"name": "A4 test", "task_shape": "general"}
        )
        project_id = create_response.json()["id"]
        proj = create_response.json()
        # model 为 null + api_type=openai_compatible → 422
        response = client.put(f"/api/projects/{project_id}", json=proj)
        assert response.status_code == 422

    def test_update_project_custom_mode_no_model_ok(self, client):
        """A-4: custom 模式无 model 可保存"""
        create_response = client.post(
            "/api/projects",
            json={"name": "A4 custom", "task_shape": "general"}
        )
        project_id = create_response.json()["id"]
        proj = create_response.json()
        # masked api_key 是 dict，PUT 时需用哨兵值替换
        proj["judge_config"]["api_key"] = "__UNCHANGED__"
        proj["target_config"]["api_key"] = "__UNCHANGED__"
        proj["target_config"]["api_type"] = "custom"
        proj["target_config"]["model"] = None
        proj["target_config"]["request_template"] = '{"query": "{input}"}'
        response = client.put(f"/api/projects/{project_id}", json=proj)
        assert response.status_code == 200
        assert response.json()["target_config"]["api_type"] == "custom"

    def test_update_project_custom_mode_empty_template_422(self, client):
        """A-4: custom 模式 request_template 为空 → 422"""
        create_response = client.post(
            "/api/projects",
            json={"name": "A4 empty tpl", "task_shape": "general"}
        )
        project_id = create_response.json()["id"]
        proj = create_response.json()
        proj["judge_config"]["api_key"] = "__UNCHANGED__"
        proj["target_config"]["api_key"] = "__UNCHANGED__"
        proj["target_config"]["api_type"] = "custom"
        proj["target_config"]["request_template"] = ""
        response = client.put(f"/api/projects/{project_id}", json=proj)
        assert response.status_code == 422

    def test_list_project_evalsets_empty(self, client):
        """列出项目下的评测集（空）"""
        create_response = client.post(
            "/api/projects",
            json={"name": "评测集列表测试", "task_shape": "general"}
        )
        project_id = create_response.json()["id"]

        response = client.get(f"/api/projects/{project_id}/evalsets")
        assert response.status_code == 200
        assert response.json() == {"evalsets": []}

    def test_list_project_evalsets_with_data(self, client):
        """列出项目下的评测集（有数据）"""
        create_response = client.post(
            "/api/projects",
            json={"name": "评测集列表测试2", "task_shape": "general"}
        )
        project_id = create_response.json()["id"]

        # 创建两个评测集
        client.post("/api/evalsets", json={
            "project_id": project_id, "name": "set1", "cases": []
        })
        client.post("/api/evalsets", json={
            "project_id": project_id, "name": "set2", "cases": []
        })

        response = client.get(f"/api/projects/{project_id}/evalsets")
        assert response.status_code == 200
        data = response.json()
        assert len(data["evalsets"]) == 2
        names = {e["name"] for e in data["evalsets"]}
        assert names == {"set1", "set2"}

    def test_list_project_evalsets_project_not_found(self, client):
        """项目不存在时列评测集返回 404"""
        response = client.get("/api/projects/proj-notexist/evalsets")
        assert response.status_code == 404


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

    def test_case_crud_add_edit_delete_via_put(self, client):
        """B-5: 用例 CRUD（新增 → 编辑 → 硬删除）走全量 PUT"""
        project_response = client.post("/api/projects", json={"name": "B5测试"})
        project_id = project_response.json()["id"]
        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "CRUD测试集"}
        )
        evalset_id = evalset_response.json()["id"]

        # 1) 新增 case-1（exact）
        evalset = client.get(f"/api/evalsets/{evalset_id}?project_id={project_id}").json()
        evalset["cases"] = [{
            "id": "case-1", "case_name": "原 case", "input": "你好",
            "expected_output": "你好！", "eval_type": "exact",
            "eval_params": {}, "enabled": True
        }]
        r = client.put(f"/api/evalsets/{evalset_id}", json=evalset)
        assert r.status_code == 200
        assert len(r.json()["cases"]) == 1

        # 2) 编辑 case-1：改成 contains + 加新 case-2（llm_judge）
        evalset = client.get(f"/api/evalsets/{evalset_id}?project_id={project_id}").json()
        evalset["cases"][0]["eval_type"] = "contains"
        evalset["cases"][0]["expected_output"] = None
        evalset["cases"][0]["eval_params"] = {"substring": "客服"}
        evalset["cases"].append({
            "id": "case-2", "case_name": "判据 case", "input": "投诉",
            "output_requirement": "回复需包含歉意", "eval_type": "llm_judge",
            "eval_params": {}, "enabled": True
        })
        r = client.put(f"/api/evalsets/{evalset_id}", json=evalset)
        assert r.status_code == 200
        cases = r.json()["cases"]
        assert len(cases) == 2
        assert cases[0]["eval_type"] == "contains"
        assert cases[0]["eval_params"] == {"substring": "客服"}
        assert cases[1]["output_requirement"] == "回复需包含歉意"

        # 3) 硬删除 case-1（保留 case-2）
        evalset = client.get(f"/api/evalsets/{evalset_id}?project_id={project_id}").json()
        evalset["cases"] = [c for c in evalset["cases"] if c["id"] != "case-1"]
        r = client.put(f"/api/evalsets/{evalset_id}", json=evalset)
        assert r.status_code == 200
        cases = r.json()["cases"]
        assert len(cases) == 1
        assert cases[0]["id"] == "case-2"

    def test_case_edit_preserves_enabled_when_unspecified(self, client):
        """B-5: 编辑用例时若 PUT 体未带 enabled 字段，后端默认 True（Pydantic 默认值）"""
        project_response = client.post("/api/projects", json={"name": "B5启用测试"})
        project_id = project_response.json()["id"]
        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "启用状态集"}
        )
        evalset_id = evalset_response.json()["id"]
        evalset = client.get(f"/api/evalsets/{evalset_id}?project_id={project_id}").json()
        evalset["cases"] = [{
            "id": "c1", "case_name": "禁用态", "input": "x",
            "eval_type": "exact", "enabled": False
        }]
        client.put(f"/api/evalsets/{evalset_id}", json=evalset)

        # 编辑：去掉 enabled 字段（模拟前端只更新内容）→ 应默认 True
        evalset = client.get(f"/api/evalsets/{evalset_id}?project_id={project_id}").json()
        evalset["cases"][0] = {
            "id": "c1", "case_name": "禁用态-改名", "input": "x",
            "eval_type": "exact"
            # 注意：未带 enabled
        }
        r = client.put(f"/api/evalsets/{evalset_id}", json=evalset)
        # Pydantic 默认 enabled=True，所以重置为启用
        assert r.json()["cases"][0]["enabled"] is True

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
        data = response.json()
        assert data["imported"] == 2
        assert data["mode"] == "replace"
        assert len(data["evalset"]["cases"]) == 2

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
        data = response.json()
        assert data["imported"] == 1
        assert len(data["evalset"]["cases"]) == 1

    def test_import_evalset_object_eval_params_and_task_shape(self, client):
        """P2-8: 后端 import 端点支持对象 eval_params + task_shape 字段"""
        project_response = client.post("/api/projects", json={"name": "P2-8 对象"})
        project_id = project_response.json()["id"]
        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "对象测试"}
        )
        evalset_id = evalset_response.json()["id"]

        json_content = json.dumps([
            {
                "case_name": "对象 params",
                "input": "x",
                "eval_type": "length",
                "eval_params": {"min": 1, "max": 10},  # 对象形式（不是 JSON 字符串）
                "task_shape": "coding"
            }
        ])
        response = client.post(
            f"/api/evalsets/{evalset_id}/import?project_id={project_id}&mode=replace",
            data={"file_content": json_content}
        )
        assert response.status_code == 200
        cases = response.json()["evalset"]["cases"]
        assert cases[0]["eval_params"] == {"min": 1, "max": 10}
        assert cases[0]["task_shape"] == "coding"

    def test_import_evalset_merge_dedup(self, client):
        """P2-8: merge 模式按 id 去重，同 id 不重复添加"""
        project_response = client.post("/api/projects", json={"name": "merge 测试"})
        project_id = project_response.json()["id"]
        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "merge 集"}
        )
        evalset_id = evalset_response.json()["id"]

        # 第一次：导入 2 条
        content1 = json.dumps([
            {"id": "a", "case_name": "A", "input": "x", "eval_type": "exact"},
            {"id": "b", "case_name": "B", "input": "y", "eval_type": "exact"}
        ])
        r1 = client.post(
            f"/api/evalsets/{evalset_id}/import?project_id={project_id}&mode=merge",
            data={"file_content": content1}
        )
        assert r1.status_code == 200
        assert r1.json()["imported"] == 2

        # 第二次：merge 一条新 + 一条已存在（id=a）
        content2 = json.dumps([
            {"id": "a", "case_name": "A-改名", "input": "x2", "eval_type": "exact"},
            {"id": "c", "case_name": "C", "input": "z", "eval_type": "exact"}
        ])
        r2 = client.post(
            f"/api/evalsets/{evalset_id}/import?project_id={project_id}&mode=merge",
            data={"file_content": content2}
        )
        assert r2.status_code == 200
        cases = r2.json()["evalset"]["cases"]
        # merge 模式：a 已存在不重复添加，c 新增 → 共 3 条
        assert len(cases) == 3
        ids = [c["id"] for c in cases]
        assert ids.count("a") == 1
        assert "c" in ids

    def test_import_evalset_row_level_errors(self, client):
        """P2-8: 行级错误收集——有错误时不保存，返回 422 + errors 列表"""
        project_response = client.post("/api/projects", json={"name": "错误测试"})
        project_id = project_response.json()["id"]
        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "错误集"}
        )
        evalset_id = evalset_response.json()["id"]

        # 第 2 行 eval_type 非法枚举值 → Pydantic 构造失败
        content = json.dumps([
            {"case_name": "正常", "input": "x", "eval_type": "exact"},
            {"case_name": "坏行", "input": "y", "eval_type": "INVALID_TYPE"}
        ])
        response = client.post(
            f"/api/evalsets/{evalset_id}/import?project_id={project_id}&mode=replace",
            data={"file_content": content}
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["error"] == "import_validation_failed"
        assert detail["imported"] == 0
        assert len(detail["errors"]) == 1
        assert detail["errors"][0]["row"] == 2
        # evalset 应未被修改（仍是空）
        evalset = client.get(f"/api/evalsets/{evalset_id}?project_id={project_id}").json()
        assert len(evalset["cases"]) == 0

    def test_import_evalset_invalid_mode(self, client):
        """P2-8: 不支持的 mode 报错"""
        project_response = client.post("/api/projects", json={"name": "mode 测试"})
        project_id = project_response.json()["id"]
        evalset_response = client.post(
            "/api/evalsets",
            json={"project_id": project_id, "name": "mode 集"}
        )
        evalset_id = evalset_response.json()["id"]

        response = client.post(
            f"/api/evalsets/{evalset_id}/import?project_id={project_id}&mode=append",
            data={"file_content": "[]"}
        )
        assert response.status_code == 400 or response.status_code == 422

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
            mock_call.return_value = ("测试回复", 100, False)

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

    def test_test_parsing_openai_compatible(self, client):
        """测试响应解析 - OpenAI 兼容形态"""
        response = client.post(
            "/api/test/parsing",
            json={
                "response_parsing": {
                    "output_paths": ["$.choices[0].message.content"],
                    "token_paths": ["$.usage.total_tokens"],
                },
                "sample_response": json.dumps({
                    "choices": [{"message": {"content": "解析输出"}}],
                    "usage": {"total_tokens": 88},
                }),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["output"] == "解析输出"
        assert data["token_used"] == 88
        assert data["token_missing"] is False
        assert data["output_found"] is True

    def test_test_parsing_fallback_chain(self, client):
        """测试响应解析 - fallback 链命中第二条"""
        response = client.post(
            "/api/test/parsing",
            json={
                "response_parsing": {
                    "output_paths": [
                        "$.choices[0].message.content",
                        "$.output",
                    ],
                },
                "sample_response": json.dumps({"output": "fallback 命中"}),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["output"] == "fallback 命中"
        assert data["output_found"] is True
        assert data["token_missing"] is True

    def test_test_parsing_token_fields_recursive(self, client):
        """测试响应解析 - token_fields 递归求和"""
        response = client.post(
            "/api/test/parsing",
            json={
                "response_parsing": {
                    "output_paths": ["$.text"],
                    "token_fields": ["total_tokens"],
                },
                "sample_response": json.dumps({
                    "text": "回复",
                    "trace": [{"total_tokens": 30}, {"total_tokens": 40}],
                }),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["output"] == "回复"
        assert data["token_used"] == 70
        assert data["token_missing"] is False

    def test_test_parsing_token_scope(self, client):
        """测试响应解析 - token_scope 过滤"""
        response = client.post(
            "/api/test/parsing",
            json={
                "response_parsing": {
                    "output_paths": ["$.data[-1].pluginOutput.text"],
                    "token_fields": ["total_tokens"],
                    "token_scope": {"moduleType": "tools"},
                },
                "sample_response": json.dumps({
                    "data": [
                        {"moduleType": "tools", "total_tokens": 30, "pluginOutput": {"text": "first"}},
                        {"moduleType": "chat", "total_tokens": 999},
                        {"moduleType": "tools", "total_tokens": 20, "pluginOutput": {"text": "final"}},
                    ],
                }),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["output"] == "final"
        assert data["token_used"] == 50
        assert data["token_missing"] is False

    def test_test_parsing_output_miss(self, client):
        """测试响应解析 - 输出路径全部未命中"""
        response = client.post(
            "/api/test/parsing",
            json={
                "response_parsing": {"output_paths": ["$.choices[0].message.content"]},
                "sample_response": json.dumps({"other": "x"}),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["output_found"] is False
        assert data["output"] == ""

    def test_test_parsing_all_empty(self, client):
        """测试响应解析 - 全部留空（A-1: 输出 = 完整响应原文）"""
        response = client.post(
            "/api/test/parsing",
            json={
                "response_parsing": {},
                "sample_response": json.dumps({"any": "thing"}),
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["token_used"] == 0
        assert data["token_missing"] is True
        # A-1: 空路径 → 原文兜底，output_found=True
        assert data["output_found"] is True
        assert data["output"] == json.dumps({"any": "thing"})

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
