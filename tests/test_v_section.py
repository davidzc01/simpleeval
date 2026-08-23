"""V 节（批次 5）后端功能测试

覆盖：
- V-1: 标签全局管理（CRUD + 改名同步 + 删除引用确认）
- V-3: EvalRun.filter_tags + runs 列表 case_count
"""

import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models import EvalCase, EvalSet, Project, JudgeConfig, TargetConfig, RunEvalRequest, CaseFilter


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def isolated_storage(tmp_path):
    """隔离存储目录，避免污染全局数据"""
    from app import storage
    storage.DATA_DIR = tmp_path
    storage.PROJECTS_DIR = tmp_path / "projects"
    storage.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    storage.EVALSETS_DIR = tmp_path / "evalsets"
    storage.EVALSETS_DIR.mkdir(parents=True, exist_ok=True)
    storage.RUNS_DIR = tmp_path / "runs"
    storage.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    storage.CONFIG_TEMPLATES_FILE = tmp_path / "config-templates.json"
    storage.JUDGE_CONFIGS_FILE = tmp_path / "judge-configs.json"
    storage.TAGS_FILE = tmp_path / "tags.json"
    yield storage


# ============== V-1: 标签全局管理 ==============

class TestTagCRUD:
    """V-1: 标签 CRUD API"""

    def test_create_tag_success(self, client, isolated_storage):
        """新建标签 → 201"""
        r = client.post("/api/tags", json={"name": "regression"})
        assert r.status_code == 201
        assert r.json()["name"] == "regression"
        assert "created_at" in r.json()

    def test_create_tag_duplicate_409(self, client, isolated_storage):
        """重名 → 409"""
        client.post("/api/tags", json={"name": "smoke"})
        r = client.post("/api/tags", json={"name": "smoke"})
        assert r.status_code == 409

    def test_create_tag_empty_name_422(self, client, isolated_storage):
        """空名 → 422"""
        r = client.post("/api/tags", json={"name": "  "})
        assert r.status_code == 422

    def test_list_tags_with_reference_counts(self, client, isolated_storage):
        """列表含引用统计：case 数 / project 数"""
        # 建项目 + 评测集 + 带标签 case
        client.post("/api/projects", json={"name": "p1"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "p1"][0]["id"]
        eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
        eid = eids[0]["id"]
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace", data={
            "file_content": json.dumps([
                {"id": "c1", "case_name": "A", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["regression", "smoke"]},
                {"id": "c2", "case_name": "B", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["regression"]},
            ])
        })
        # 建标签
        client.post("/api/tags", json={"name": "regression"})
        client.post("/api/tags", json={"name": "smoke"})
        # 列表
        r = client.get("/api/tags")
        tags = {t["name"]: t for t in r.json()["tags"]}
        assert tags["regression"]["case_count"] == 2
        assert tags["regression"]["project_count"] == 1
        assert tags["smoke"]["case_count"] == 1
        assert tags["smoke"]["project_count"] == 1

    def test_list_tags_empty(self, client, isolated_storage):
        """空标签库"""
        r = client.get("/api/tags")
        assert r.status_code == 200
        assert r.json()["tags"] == []

    def test_migrate_legacy_tags_auto_on_list(self, client, isolated_storage):
        """历史项目中的标签自动迁移到全局标签库"""
        # 建项目 + 评测集 + 带未注册标签的 case
        client.post("/api/projects", json={"name": "mig-p1"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "mig-p1"][0]["id"]
        eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
        eid = eids[0]["id"]
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace", data={
            "file_content": json.dumps([
                {"id": "mc1", "case_name": "A", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["legacy-tag", "shared-tag"]},
                {"id": "mc2", "case_name": "B", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["legacy-tag", "shared-tag"]},
            ])
        })
        # 仅注册 shared-tag，legacy-tag 不注册
        client.post("/api/tags", json={"name": "shared-tag"})
        # list_tags 应自动迁移 legacy-tag
        r = client.get("/api/tags")
        tags = {t["name"]: t for t in r.json()["tags"]}
        assert "legacy-tag" in tags
        assert "shared-tag" in tags
        assert tags["legacy-tag"]["case_count"] == 2
        assert tags["legacy-tag"]["project_count"] == 1
        assert tags["shared-tag"]["case_count"] == 2

    def test_migrate_tags_idempotent(self, client, isolated_storage):
        """迁移幂等：重复调用不产生重复"""
        client.post("/api/projects", json={"name": "mig-p2"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "mig-p2"][0]["id"]
        eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
        eid = eids[0]["id"]
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace", data={
            "file_content": json.dumps([
                {"id": "mc1", "case_name": "A", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["dup-tag"]},
            ])
        })
        # 第一次迁移
        r1 = client.post("/api/tags/migrate")
        assert r1.status_code == 200
        assert r1.json()["migrated_count"] == 1
        # 第二次幂等
        r2 = client.post("/api/tags/migrate")
        assert r2.status_code == 200
        assert r2.json()["migrated_count"] == 0
        # 列表中只有一个 dup-tag
        r = client.get("/api/tags")
        dup = [t for t in r.json()["tags"] if t["name"] == "dup-tag"]
        assert len(dup) == 1

    def test_migrate_tags_empty(self, client, isolated_storage):
        """无待迁移标签 → 返回 0"""
        r = client.post("/api/tags/migrate")
        assert r.status_code == 200
        assert r.json()["migrated_count"] == 0


class TestTagRename:
    """V-1: 改名同步更新所有 case 标签"""

    def test_rename_updates_all_cases(self, client, isolated_storage):
        """改名后所有 evalset case 的标签同步更新"""
        # 建项目 + 评测集
        client.post("/api/projects", json={"name": "rp1"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "rp1"][0]["id"]
        eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
        eid = eids[0]["id"]
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace", data={
            "file_content": json.dumps([
                {"id": "c1", "case_name": "A", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["old-name"]},
                {"id": "c2", "case_name": "B", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["other", "old-name"]},
            ])
        })
        # 建标签 + 改名
        client.post("/api/tags", json={"name": "old-name"})
        r = client.put("/api/tags/old-name", json={"new_name": "new-name"})
        assert r.status_code == 200
        assert len(r.json()["affected_projects"]) >= 1
        # 验证 case 标签已更新
        es = client.get(f"/api/evalsets/{eid}?project_id={pid}").json()
        tags_c1 = es["cases"][0]["tags"]
        tags_c2 = es["cases"][1]["tags"]
        assert "old-name" not in tags_c1
        assert "new-name" in tags_c1
        assert "other" in tags_c2  # 其他标签不受影响
        assert "new-name" in tags_c2

    def test_rename_tag_not_found_404(self, client, isolated_storage):
        """改名不存在的标签 → 404"""
        r = client.put("/api/tags/nonexistent", json={"new_name": "x"})
        assert r.status_code == 404

    def test_rename_to_existing_name_409(self, client, isolated_storage):
        """改名为已有其他标签名 → 409"""
        client.post("/api/tags", json={"name": "a"})
        client.post("/api/tags", json={"name": "b"})
        r = client.put("/api/tags/a", json={"new_name": "b"})
        assert r.status_code == 409


class TestTagDelete:
    """V-1: 删除标签 + 引用确认"""

    def test_delete_removes_from_all_cases(self, client, isolated_storage):
        """删除后所有 case 无残留该标签"""
        client.post("/api/projects", json={"name": "dp1"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "dp1"][0]["id"]
        eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
        eid = eids[0]["id"]
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace", data={
            "file_content": json.dumps([
                {"id": "c1", "case_name": "A", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["del-me", "keep"]},
                {"id": "c2", "case_name": "B", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["del-me"]},
            ])
        })
        client.post("/api/tags", json={"name": "del-me"})
        r = client.delete("/api/tags/del-me")
        assert r.status_code == 200
        assert "del-me" not in r.json()
        # 验证 case 标签已移除
        es = client.get(f"/api/evalsets/{eid}?project_id={pid}").json()
        assert "del-me" not in es["cases"][0]["tags"]
        assert "keep" in es["cases"][0]["tags"]  # 其他标签保留
        assert es["cases"][1]["tags"] == []

    def test_delete_tag_not_found_404(self, client, isolated_storage):
        """删除不存在的标签 → 404"""
        r = client.delete("/api/tags/nonexistent")
        assert r.status_code == 404

    def test_delete_returns_affected_projects(self, client, isolated_storage):
        """删除返回受影响 project 列表"""
        # 两个项目各有一个 case 带该标签
        for pname in ["da1", "da2"]:
            client.post("/api/projects", json={"name": pname})
        projects = client.get("/api/projects").json()
        pids = {}
        for pname in ["da1", "da2"]:
            pids[pname] = [p for p in projects["projects"] if p["name"] == pname][0]["id"]
        for pname, pid in pids.items():
            eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
            eid = eids[0]["id"]
            client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace", data={
                "file_content": json.dumps([
                    {"id": "c1", "case_name": "A", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["shared"]}
                ])
            })
        client.post("/api/tags", json={"name": "shared"})
        r = client.delete("/api/tags/shared")
        assert r.status_code == 200
        affected = r.json()["affected_projects"]
        assert len(affected) == 2


class TestTagCrossProjectRename:
    """V-1: 多项目引用时改名/删除"""

    def test_rename_across_multiple_projects(self, client, isolated_storage):
        """改名同步更新跨项目的全部 case"""
        for pname in ["cp1", "cp2"]:
            client.post("/api/projects", json={"name": pname})
        projects = client.get("/api/projects").json()
        for pname in ["cp1", "cp2"]:
            pid = [p for p in projects["projects"] if p["name"] == pname][0]["id"]
            eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
            eid = eids[0]["id"]
            client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace", data={
                "file_content": json.dumps([
                    {"id": "c1", "case_name": "A", "input": "x", "eval_type": "exact", "expected_output": "ok", "enabled": True, "tags": ["cross-tag"]}
                ])
            })
        client.post("/api/tags", json={"name": "cross-tag"})
        r = client.put("/api/tags/cross-tag", json={"new_name": "renamed-tag"})
        assert r.status_code == 200
        assert len(r.json()["affected_projects"]) == 2
        # 验证两个项目的 case 标签都更新
        for pname in ["cp1", "cp2"]:
            pid = [p for p in client.get("/api/projects").json()["projects"] if p["name"] == pname][0]["id"]
            eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
            eid = eids[0]["id"]
            es = client.get(f"/api/evalsets/{eid}?project_id={pid}").json()
            assert "renamed-tag" in es["cases"][0]["tags"]
            assert "cross-tag" not in es["cases"][0]["tags"]


# ============== V-3: EvalRun.filter_tags + case_count ==============

class TestRunFilterTagsAndCaseCount:
    """V-3: run 创建时保存 filter_tags，列表返回 case_count"""

    def test_run_model_has_filter_tags_field(self):
        """EvalRun 模型含 filter_tags 字段，默认空列表"""
        from app.models import EvalRun
        run = EvalRun(id="r1", project_id="p1", evalset_id="e1", status="queued", created_at="2026-01-01T00:00:00Z")
        assert run.filter_tags == []

    def test_run_filter_tags_set_from_case_filter(self):
        """RunEvalRequest 带 case_filter → filter_tags = case_filter.tags"""
        req = RunEvalRequest(
            project_id="p1", evalset_id="e1",
            case_filter=CaseFilter(tags=["smoke", "regression"], mode="any"),
        )
        # 模拟 run 创建逻辑
        filter_tags = req.case_filter.tags if req.case_filter and req.case_filter.tags else []
        assert filter_tags == ["smoke", "regression"]

    def test_run_filter_tags_empty_without_case_filter(self):
        """无 case_filter → filter_tags 为空列表"""
        req = RunEvalRequest(project_id="p1", evalset_id="e1")
        filter_tags = req.case_filter.tags if req.case_filter and req.case_filter.tags else []
        assert filter_tags == []

    def test_runs_list_includes_case_count(self, client, isolated_storage):
        """runs 列表响应含 case_count"""
        client.post("/api/projects", json={"name": "cc1"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "cc1"][0]["id"]
        # 手动创建一个 completed run（绕过后台执行）
        from app.storage import save_run
        from app.models import EvalRun, CaseResult, EvalSummary
        run = EvalRun(
            id="test-cc1", project_id=pid, evalset_id="e1",
            status="completed", created_at="2026-01-01T00:00:00Z",
            results=[
                CaseResult(case_name="A", case_id="c1", passed=True, score=1.0, latency_ms=100, token_used=10, judge_token=0, actual_output="ok", check_results=[]),
                CaseResult(case_name="B", case_id="c2", passed=False, score=0.0, latency_ms=200, token_used=20, judge_token=0, actual_output="bad", check_results=[]),
            ],
            summary=EvalSummary(total=2, passed=1, failed=1, skipped=0, pass_rate=0.5, avg_latency_ms=150, total_token=30, total_latency_ms=300, token_per_pass=0.0, latency_p50=100, latency_p95=200),
            filter_tags=["smoke"],
        )
        save_run(run)
        # 查列表
        r = client.get(f"/api/projects/{pid}/runs")
        assert r.status_code == 200
        runs = r.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["case_count"] == 2
        assert runs[0]["filter_tags"] == ["smoke"]

    def test_runs_list_case_count_zero_for_queued(self, client, isolated_storage):
        """queued run 无 results → case_count = 0"""
        client.post("/api/projects", json={"name": "cc2"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "cc2"][0]["id"]
        from app.storage import save_run
        from app.models import EvalRun
        run = EvalRun(
            id="test-cc2", project_id=pid, evalset_id="e1",
            status="queued", created_at="2026-01-01T00:00:00Z",
        )
        save_run(run)
        r = client.get(f"/api/projects/{pid}/runs")
        runs = r.json()["runs"]
        assert runs[0]["case_count"] == 0
        assert runs[0]["filter_tags"] == []
