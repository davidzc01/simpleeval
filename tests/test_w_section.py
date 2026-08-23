"""W 节（批次 6）后端功能测试

覆盖：
- W-3: 定时任务管理（trigger 字段 + schedule CRUD API + logs）
- W-7: 新建项目默认初始版本 + 版本改名 API
"""

import json
import pytest

from fastapi.testclient import TestClient

from app.main import app
from app.models import EvalRun, ProjectVersion, ScheduleConfig, UpdateScheduleRequest


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def isolated_storage(tmp_path):
    """隔离存储目录"""
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


# ============== W-7: 新建项目默认初始版本 ==============

class TestProjectInitialVersion:
    """W-7: 新建项目自动创建「初始版本」"""

    def test_new_project_has_initial_version(self, client, isolated_storage):
        """新建项目 → versions 含一条「初始版本」"""
        r = client.post("/api/projects", json={"name": "test-w7", "task_shape": "general"})
        assert r.status_code == 201
        data = r.json()
        assert "versions" in data
        versions = data["versions"]
        assert len(versions) == 1
        assert versions[0]["name"] == "初始版本"
        assert versions[0]["id"]
        assert versions[0]["created_at"]

    def test_initial_version_has_valid_id(self, client, isolated_storage):
        """初始版本 id 非空且格式正确"""
        r = client.post("/api/projects", json={"name": "test-w7b", "task_shape": "general"})
        versions = r.json()["versions"]
        assert versions[0]["id"].startswith("ver-")


class TestVersionRename:
    """W-7: 版本改名 API"""

    def test_rename_version_success(self, client, isolated_storage):
        """PUT 版本改名成功"""
        r = client.post("/api/projects", json={"name": "test-rename", "task_shape": "general"})
        pid = r.json()["id"]
        vid = r.json()["versions"][0]["id"]
        r2 = client.put(f"/api/projects/{pid}/versions/{vid}", json={"name": "v2.0"})
        assert r2.status_code == 200
        assert r2.json()["name"] == "v2.0"

    def test_rename_version_empty_name_422(self, client, isolated_storage):
        """空名 → 422"""
        r = client.post("/api/projects", json={"name": "test-empty", "task_shape": "general"})
        pid = r.json()["id"]
        vid = r.json()["versions"][0]["id"]
        r2 = client.put(f"/api/projects/{pid}/versions/{vid}", json={"name": "  "})
        assert r2.status_code == 422

    def test_rename_version_not_found_404(self, client, isolated_storage):
        """版本不存在 → 404"""
        r = client.post("/api/projects", json={"name": "test-nf", "task_shape": "general"})
        pid = r.json()["id"]
        r2 = client.put(f"/api/projects/{pid}/versions/nonexistent", json={"name": "x"})
        assert r2.status_code == 404

    def test_rename_version_persists(self, client, isolated_storage):
        """改名后持久化，再次获取项目验证"""
        r = client.post("/api/projects", json={"name": "test-persist", "task_shape": "general"})
        pid = r.json()["id"]
        vid = r.json()["versions"][0]["id"]
        client.put(f"/api/projects/{pid}/versions/{vid}", json={"name": "renamed"})
        r2 = client.get(f"/api/projects/{pid}")
        versions = r2.json().get("versions", [])
        assert any(v["name"] == "renamed" for v in versions)


# ============== W-3: 定时任务管理 ==============

class TestEvalRunTrigger:
    """W-3: EvalRun.trigger 字段"""

    def test_trigger_defaults_to_manual(self):
        """新 EvalRun 默认 trigger = manual"""
        run = EvalRun(id="r1", project_id="p1", evalset_id="e1", status="queued", created_at="2026-01-01T00:00:00Z")
        assert run.trigger == "manual"

    def test_trigger_scheduled_set(self):
        """定时调度器创建的 run trigger = scheduled"""
        run = EvalRun(
            id="r2", project_id="p1", evalset_id="e1", status="queued",
            created_at="2026-01-01T00:00:00Z", trigger="scheduled",
        )
        assert run.trigger == "scheduled"


class TestScheduleCRUD:
    """W-3: 定时配置 CRUD API"""

    def test_update_schedule_success(self, client, isolated_storage):
        """PUT 创建/更新定时配置"""
        r = client.post("/api/projects", json={"name": "sched-test", "task_shape": "general"})
        pid = r.json()["id"]
        r2 = client.put(f"/api/projects/{pid}/schedule", json={
            "enabled": True, "cron": "*/30 * * * *", "tags": ["smoke"],
            "regression_threshold": 0.15,
        })
        assert r2.status_code == 200
        assert r2.json()["enabled"] is True
        assert r2.json()["cron"] == "*/30 * * * *"
        assert r2.json()["tags"] == ["smoke"]
        assert r2.json()["regression_threshold"] == 0.15

    def test_update_schedule_invalid_cron_422(self, client, isolated_storage):
        """cron 非 5 字段 → 422"""
        r = client.post("/api/projects", json={"name": "cron-test", "task_shape": "general"})
        pid = r.json()["id"]
        r2 = client.put(f"/api/projects/{pid}/schedule", json={
            "enabled": True, "cron": "*/30 * * *",  # 只有 4 字段
        })
        assert r2.status_code == 422

    def test_delete_schedule(self, client, isolated_storage):
        """DELETE 删除定时配置"""
        r = client.post("/api/projects", json={"name": "del-sched", "task_shape": "general"})
        pid = r.json()["id"]
        client.put(f"/api/projects/{pid}/schedule", json={"enabled": True, "cron": "0 * * * *"})
        r2 = client.delete(f"/api/projects/{pid}/schedule")
        assert r2.status_code == 200
        # 验证已删除
        r3 = client.get("/api/schedules")
        assert not any(s["project_id"] == pid for s in r3.json()["schedules"])

    def test_list_schedules(self, client, isolated_storage):
        """GET /schedules 列出全部定时配置"""
        r1 = client.post("/api/projects", json={"name": "p1", "task_shape": "general"})
        r2 = client.post("/api/projects", json={"name": "p2", "task_shape": "general"})
        pid1 = r1.json()["id"]
        pid2 = r2.json()["id"]
        client.put(f"/api/projects/{pid1}/schedule", json={"enabled": True, "cron": "0 * * * *"})
        client.put(f"/api/projects/{pid2}/schedule", json={"enabled": False, "cron": "*/5 * * * *"})
        r = client.get("/api/schedules")
        assert r.status_code == 200
        schedules = r.json()["schedules"]
        assert len(schedules) == 2
        names = {s["project_name"] for s in schedules}
        assert names == {"p1", "p2"}

    def test_list_schedules_empty(self, client, isolated_storage):
        """无定时配置 → 空列表"""
        r = client.get("/api/schedules")
        assert r.status_code == 200
        assert r.json()["schedules"] == []

    def test_list_schedules_includes_last_run(self, client, isolated_storage):
        """列表含上次执行结果"""
        r = client.post("/api/projects", json={"name": "last-run-test", "task_shape": "general"})
        pid = r.json()["id"]
        client.put(f"/api/projects/{pid}/schedule", json={"enabled": True, "cron": "0 * * * *"})
        # 创建一个 scheduled run
        from app.storage import save_run
        run = EvalRun(
            id="sched-run-1", project_id=pid, evalset_id="e1",
            status="completed", created_at="2026-08-21T10:00:00Z",
            trigger="scheduled",
        )
        save_run(run)
        r2 = client.get("/api/schedules")
        schedules = r2.json()["schedules"]
        assert len(schedules) == 1
        assert schedules[0]["last_run"] is not None
        assert schedules[0]["last_run"]["run_id"] == "sched-run-1"

    def test_schedule_logs(self, client, isolated_storage):
        """GET /schedules/logs 返回定时触发记录"""
        r = client.post("/api/projects", json={"name": "logs-test", "task_shape": "general"})
        pid = r.json()["id"]
        # 创建多个 scheduled run
        from app.storage import save_run
        for i in range(3):
            run = EvalRun(
                id=f"sched-log-{i}", project_id=pid, evalset_id="e1",
                status="completed", created_at=f"2026-08-21T1{i}:00:00Z",
                trigger="scheduled",
            )
            save_run(run)
        # 也创建一个 manual run（不应出现在 logs 中）
        manual = EvalRun(
            id="manual-run-1", project_id=pid, evalset_id="e1",
            status="completed", created_at="2026-08-21T12:00:00Z",
            trigger="manual",
        )
        save_run(manual)
        r2 = client.get("/api/schedules/logs")
        assert r2.status_code == 200
        logs = r2.json()["logs"]
        assert len(logs) == 3
        assert all(l["project_name"] == "logs-test" for l in logs)
        assert all(l["run_id"].startswith("sched-log-") for l in logs)
        # 按时间倒序
        assert logs[0]["created_at"] >= logs[-1]["created_at"]

    def test_schedule_logs_limit(self, client, isolated_storage):
        """logs limit 参数生效"""
        r = client.post("/api/projects", json={"name": "limit-test", "task_shape": "general"})
        pid = r.json()["id"]
        from app.storage import save_run
        for i in range(10):
            run = EvalRun(
                id=f"limit-{i}", project_id=pid, evalset_id="e1",
                status="completed", created_at=f"2026-08-21T1{i}:00:00Z",
                trigger="scheduled",
            )
            save_run(run)
        r2 = client.get("/api/schedules/logs?limit=5")
        assert len(r2.json()["logs"]) == 5


# ============== W-7 进阶验收 ==============

class TestW7ProjectResponseVersions:
    """W-7 验收 1：项目详情/列表响应带 versions 字段"""

    def test_create_project_response_has_versions(self, client, isolated_storage):
        """新建项目 → 响应体 versions 字段正确返回"""
        r = client.post("/api/projects", json={"name": "p-ver-resp", "task_shape": "general"})
        data = r.json()
        assert "versions" in data
        assert isinstance(data["versions"], list)
        assert len(data["versions"]) == 1
        assert data["versions"][0]["name"] == "初始版本"

    def test_get_project_detail_versions(self, client, isolated_storage):
        """GET 项目详情 → versions 字段"""
        r = client.post("/api/projects", json={"name": "p-detail-ver", "task_shape": "general"})
        pid = r.json()["id"]
        r2 = client.get(f"/api/projects/{pid}")
        versions = r2.json().get("versions", [])
        assert len(versions) == 1
        assert versions[0]["name"] == "初始版本"

    def test_list_projects_versions(self, client, isolated_storage):
        """GET 项目列表 → 每项含 versions 字段"""
        client.post("/api/projects", json={"name": "p-list-ver", "task_shape": "general"})
        r = client.get("/api/projects")
        for p in r.json()["projects"]:
            assert "versions" in p
            assert len(p["versions"]) >= 1


class TestW7RenameVersionCorrectness:
    """W-7 验收 2：改名后对比视图名称更新 + run 归属 version_id 不变"""

    def test_rename_version_reflects_in_compare_view(self, client, isolated_storage):
        """改名 → compare 接口 version_name 更新"""
        r = client.post("/api/projects", json={"name": "rename-compare", "task_shape": "general"})
        pid = r.json()["id"]
        vid = r.json()["versions"][0]["id"]
        # 创建一个属于该版本的 completed run
        from app.storage import save_run
        from app.models import EvalSummary
        run = EvalRun(
            id="r1", project_id=pid, evalset_id="e1", status="completed",
            created_at="2026-08-21T10:00:00Z", version_id=vid,
            summary=EvalSummary(total_latency_ms=200, total_token=100, pass_rate=0.8, token_per_pass=0.0,
                                latency_p50=100, latency_p95=200, sample_count=1,
                                passed_count=1, failed_count=0, skipped_count=0,
                                judge_token=50),
        )
        save_run(run)
        # 改名前 compare
        cmp_before = client.get(f"/api/projects/{pid}/versions/compare").json()
        assert any(v["version_name"] == "初始版本" for v in cmp_before["versions"])
        # 改名
        client.put(f"/api/projects/{pid}/versions/{vid}", json={"name": "v1.0"})
        # 改名后 compare
        cmp_after = client.get(f"/api/projects/{pid}/versions/compare").json()
        assert any(v["version_name"] == "v1.0" for v in cmp_after["versions"])
        # run.version_id 保持不变（版本 id 没变，只改了 name）
        from app.storage import get_run
        updated_run = get_run("r1", pid)
        assert updated_run.version_id == vid

    def test_run_assigns_initial_version_on_creation(self, client, isolated_storage):
        """手动发起 run → 自动落入初始版本"""
        from app import storage
        from app.models import EvalSummary
        storage._PROJECTS_DIR = storage.PROJECTS_DIR
        r = client.post("/api/projects", json={"name": "p-init-ver-assign", "task_shape": "general"})
        pid = r.json()["id"]
        vid = r.json()["versions"][0]["id"]
        eid = r.json()["evalset_id"]
        # 补一个 case 到 evalset（避免 no_enabled_cases）
        client.put(f"/api/evalsets/{eid}", json={
            "id": eid, "project_id": pid, "name": "测试集",
            "cases": [{"id": "c1", "case_name": "c1", "input": "hi", "eval_type": "exact", "expected_output": "ok"}]
        })
        # 创建一个 run（通过 runner 的 resolve 逻辑）
        from app.routes import _resolve_version_id
        from app.storage import get_project
        proj = get_project(pid)
        resolved = _resolve_version_id(proj, "2026-08-21T10:00:00Z", None)
        assert resolved == vid  # 落入初始版本 id


# ============== W-5 进阶验收：check_results 充实 + judge 原始响应 ==============

class TestW5CheckResultsEnrichment:
    """W-5 3b/3c：check_results 每项含 field/eval_type/expected/judge_raw_response"""

    def test_check_results_includes_field_type_expected(self):
        """多验证 run 的 check_results 每项含 field/eval_type/expected"""
        from app.models import Project, EvalCase, EvalCheck
        from app.runner import _evaluate_case

        case = EvalCase(
            id="c1", case_name="c1", input="hi",
            validations=[
                EvalCheck(name="主", field="output", eval_type="exact", expected="ok"),
                EvalCheck(name="长度校验", field="output", eval_type="length",
                          eval_params={"min": 1, "max": 10}),
            ],
        )
        project = self._sample_project()
        passed, score, skipped, jt, checks, actual = self._run(project, case, "ok")
        assert len(checks) == 2
        for ch in checks:
            assert "field" in ch
            assert "eval_type" in ch
            assert "expected" in ch
            assert "passed" in ch
            assert "score" in ch
        assert checks[0]["field"] == "output"
        assert checks[0]["eval_type"] == "exact"
        assert checks[0]["expected"] == "ok"
        assert checks[1]["eval_type"] == "length"

    def test_judge_raw_response_in_check_results(self):
        """llm_judge check_results 含 judge_raw_response"""
        from unittest.mock import patch
        from app.models import Project, EvalCase, EvalCheck
        from app.runner import _evaluate_case

        case = EvalCase(
            id="c1", case_name="c1", input="hi",
            validations=[
                EvalCheck(name="judge 主", field="output", eval_type="llm_judge",
                          output_requirement="要礼貌"),
            ],
        )
        project = self._sample_project()
        call_count = [0]
        async def fake_judge(**kwargs):
            call_count[0] += 1
            return (0.9, 42, "judge raw response text")
        with patch("app.runner.judge_with_llm", new=fake_judge):
            passed, score, skipped, jt, checks, actual = self._run(project, case, "hi")
        assert call_count[0] == 1
        assert len(checks) == 1
        assert checks[0]["judge_raw_response"] == "judge raw response text"
        assert checks[0]["eval_type"] == "llm_judge"
        assert checks[0]["field"] == "output"

    @staticmethod
    def _sample_project():
        from app.models import Project
        return Project(id="p1", name="t", task_shape="general",
                       judge_config={"base_url": "https://x", "api_key": "k",
                                     "model": "m", "prompt_template": ""},
                       target_config={"base_url": "https://x", "api_key": "k",
                                      "model": "m", "request_template": "{input}",
                                      "auth": {"type": "none"}, "response_mapping": []})

    @staticmethod
    def _run(project, case, actual, judge_available=True, judge_error=""):
        import asyncio
        from app.runner import _evaluate_case
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _evaluate_case(project, case, actual, 0, False, judge_available, judge_error)
            )
        finally:
            loop.close()


# ============== W-7 追补：旧项目启动时补初始版本 ==============

class TestW7MigrateMissingVersions:
    """W-7 追补：versions 为空的旧项目启动时自动补「初始版本」"""

    def test_migrate_legacy_project_no_versions(self, isolated_storage):
        """旧项目 versions=[] → 迁移后补 1 条初始版本"""
        from app.storage import save_project, migrate_missing_initial_versions, list_projects
        from app.models import Project, JudgeConfig, TargetConfig

        # 模拟旧项目：直接创建一个无 versions 字段的项目（旧版本数据）
        pid = "proj-old-w7"
        project = Project(
            id=pid, name="旧项目-无版本", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
        )
        # 手动移除 versions（模拟旧数据零迁移情况）
        data = project.model_dump()
        del data["versions"]
        import json as _json
        (isolated_storage.PROJECTS_DIR / f"{pid}.json").write_text(
            _json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

        count = migrate_missing_initial_versions()
        assert count == 1

        # 验证结果
        projects = list_projects()
        p = [x for x in projects if x.id == pid][0]
        assert len(p.versions) == 1
        assert p.versions[0].name == "初始版本"
        assert p.versions[0].id.startswith("ver-")
        assert p.versions[0].created_at  # 非空

    def test_migrate_no_op_when_versions_exist(self, isolated_storage):
        """已有 versions 的项目 → 迁移为 no-op，count=0 且内容不变"""
        from app.storage import save_project, migrate_missing_initial_versions, list_projects
        from app.models import Project, ProjectVersion, JudgeConfig, TargetConfig

        pid = "proj-existing-w7"
        v0 = ProjectVersion(id="ver-aaa", name="v0", created_at="2024-01-01T00:00:00Z")
        project = Project(
            id=pid, name="已有版本项目", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
            versions=[v0],
        )
        save_project(project)

        count = migrate_missing_initial_versions()
        assert count == 0

        projects = list_projects()
        p = [x for x in projects if x.id == pid][0]
        assert len(p.versions) == 1
        assert p.versions[0].id == "ver-aaa"
        assert p.versions[0].name == "v0"

    def test_migrate_mixed_projects(self, isolated_storage):
        """混合场景：部分有版本、部分无 → 只迁移无版本的"""
        from app.storage import save_project, migrate_missing_initial_versions, list_projects
        from app.models import Project, ProjectVersion, JudgeConfig, TargetConfig
        import json as _json

        # A：有版本（不迁移）
        a = Project(
            id="pa", name="A", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
            versions=[ProjectVersion(id="vA", name="vA", created_at="2024-01-01T00:00:00Z")],
        )
        save_project(a)

        # B：无 versions（迁移）
        b = Project(
            id="pb", name="B", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
        )
        b_data = b.model_dump()
        del b_data["versions"]
        (isolated_storage.PROJECTS_DIR / "pb.json").write_text(
            _json.dumps(b_data, ensure_ascii=False), encoding="utf-8"
        )

        # C：无 versions（迁移）
        c = Project(
            id="pc", name="C", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
        )
        c_data = c.model_dump()
        del c_data["versions"]
        (isolated_storage.PROJECTS_DIR / "pc.json").write_text(
            _json.dumps(c_data, ensure_ascii=False), encoding="utf-8"
        )

        count = migrate_missing_initial_versions()
        assert count == 2

        projects = {p.id: p for p in list_projects()}
        assert len(projects["pa"].versions) == 1  # 不变
        assert projects["pa"].versions[0].id == "vA"
        assert len(projects["pb"].versions) == 1
        assert projects["pb"].versions[0].name == "初始版本"
        assert len(projects["pc"].versions) == 1
        assert projects["pc"].versions[0].name == "初始版本"

    def test_migrate_empty_versions_list(self, isolated_storage):
        """旧项目 versions=[]（空列表但非 None）→ 也应触发迁移"""
        from app.storage import migrate_missing_initial_versions, list_projects
        from app.models import Project, JudgeConfig, TargetConfig
        import json as _json

        pid = "proj-empty-versions"
        data = {
            "id": pid, "name": "空列表项目", "task_shape": "general",
            "judge_config": {"base_url": "", "api_key": "", "model": ""},
            "target_config": {"base_url": "", "api_key": "", "model": None},
            "versions": [],  # 空列表
        }
        (isolated_storage.PROJECTS_DIR / f"{pid}.json").write_text(
            _json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

        count = migrate_missing_initial_versions()
        assert count == 1

        p = [x for x in list_projects() if x.id == pid][0]
        assert len(p.versions) == 1
        assert p.versions[0].name == "初始版本"

    def test_migrate_idempotent(self, isolated_storage):
        """迁移是幂等的：运行两次不重复补版本"""
        from app.storage import migrate_missing_initial_versions, list_projects
        from app.models import Project, JudgeConfig, TargetConfig
        import json as _json

        pid = "proj-idempotent"
        data = {
            "id": pid, "name": "幂等项目", "task_shape": "general",
            "judge_config": {"base_url": "", "api_key": "", "model": ""},
            "target_config": {"base_url": "", "api_key": "", "model": None},
        }
        (isolated_storage.PROJECTS_DIR / f"{pid}.json").write_text(
            _json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

        count1 = migrate_missing_initial_versions()
        assert count1 == 1
        count2 = migrate_missing_initial_versions()
        assert count2 == 0  # 第二次 no-op

        p = [x for x in list_projects() if x.id == pid][0]
        assert len(p.versions) == 1  # 只有一条，不重复
