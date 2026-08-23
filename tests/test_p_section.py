"""P 系列一致性修正测试

覆盖：
- P-1: Case 对比视图与统计 Run 详情统一（共享渲染函数，规格一致）
- P-2: 评测集复合筛选器（关键字 + 类型 + 标签 + 状态，AND 叠加）
- P-3: 概览页重构（delta / 趋势版本分段 / 稳定性组合 / 失败导航）
"""

import json
import pytest

from fastapi.testclient import TestClient

from app.main import app
from app.models import EvalRun, EvalSet, EvalCase, CaseResult, EvalSummary, ProjectVersion
from app import storage


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def isolated_storage(tmp_path):
    """隔离存储目录"""
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


# ============== P-1: Case 对比视图统一 ==============

class TestP1CaseCompareView:
    """P-1: Case 对比视图与统计 Run 详情共用渲染函数"""

    def test_history_detail_includes_check_results_judge_info(self, client, isolated_storage):
        """Case 历史 run 详情返回 check_results + judge 字段（供弹窗渲染）"""
        # 建项目 + 评测集 + case
        r = client.post("/api/projects", json={"name": "p1-proj", "task_shape": "general"})
        proj = r.json()
        proj_id = proj["id"]
        evalset_id = proj["evalset_id"]

        # case with llm_judge
        case_data = {
            "id": "case-p1-01",
            "case_name": "P1-CASE-01",
            "input": "测试输入",
            "eval_type": "llm_judge",
            "output_requirement": "应满足X",
            "variables": {"lang": "zh"},
            "tags": ["p1"],
            "enabled": True,
        }
        r = client.put(f"/api/evalsets/{evalset_id}", json={
            "id": evalset_id, "project_id": proj_id, "name": "P1-ES", "cases": [case_data],
        })
        assert r.status_code == 200
        case_id = r.json()["cases"][0]["id"]

        # 模拟 run：写入一条 completed run
        run = EvalRun(
            id="run-p1-test-01",
            project_id=proj_id,
            evalset_id=evalset_id,
            status="completed",
            created_at="2026-08-20T00:00:00Z",
            results=[CaseResult(
                case_id=case_id, case_name="P1-CASE-01", passed=True, score=1.0,
                actual_output="满足X的输出", latency_ms=100, token_used=10,
                check_results=[{"name": "主输出验证", "field": "output", "eval_type": "llm_judge",
                                "expected": "应满足X", "passed": True, "score": 1.0,
                                "judge_raw_response": "judge ok"}],
            )],
            summary=EvalSummary(pass_rate=1.0, total_token=10, total_latency_ms=100, judge_token=5,
                                latency_p50=100, latency_p95=100, token_per_pass=1.0),
        )
        storage.save_run(run)

        # 拉 case history
        r = client.get(f"/api/evalsets/{evalset_id}/cases/{case_id}/history?project_id={proj_id}")
        assert r.status_code == 200
        data = r.json()
        history = data["history"]
        assert len(history) >= 1
        hh = history[0]
        # P-1: 统一字段——check_results / variables / eval_type / passed / score / version_name
        assert "check_results" in hh
        assert "variables" in hh
        assert "eval_type" in hh
        assert "passed" in hh
        assert "score" in hh
        assert "version_name" in hh

    def test_compare_view_and_modal_share_same_field_spec(self, client, isolated_storage):
        """P-1: run 详情「查看」与统计弹窗「详情」字段规格一致

        验证：两者都返回 check_results/actual_output/eval_type 等字段，
        前端 renderCaseRunDetailBody 可从同一份数据渲染。
        """
        # 建项目 + 评测集 + case
        r = client.post("/api/projects", json={"name": "p1-proj2", "task_shape": "general"})
        proj = r.json()
        proj_id = proj["id"]
        evalset_id = proj["evalset_id"]

        case_data = {
            "id": "case-p1-unified",
            "case_name": "P1-UNIFIED",
            "input": "统一输入",
            "eval_type": "exact",
            "expected_output": "期望输出",
            "tags": [],
            "enabled": True,
        }
        r = client.put(f"/api/evalsets/{evalset_id}", json={
            "id": evalset_id, "project_id": proj_id, "name": "P1-ES2", "cases": [case_data],
        })
        cases = r.json()["cases"]
        case_id = cases[0]["id"]

        # run
        run = EvalRun(
            id="run-p1-unified",
            project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-20T00:00:00Z",
            results=[CaseResult(
                case_id=case_id, case_name="P1-UNIFIED", passed=True, score=1.0,
                actual_output="期望输出", latency_ms=50, token_used=5,
                check_results=[{"name": "主输出验证", "field": "output", "eval_type": "exact",
                                "expected": "期望输出", "passed": True, "score": 1.0,
                                "judge_raw_response": None}],
            )],
            summary=EvalSummary(pass_rate=1.0, total_token=5, total_latency_ms=50, judge_token=0,
                                latency_p50=50, latency_p95=50, token_per_pass=1.0),
        )
        storage.save_run(run)

        # run 详情页「查看」: GET /api/runs/{run_id}?project_id= 返回 results[i]
        r = client.get(f"/api/runs/{run.id}?project_id={proj_id}")
        assert r.status_code == 200
        run_data = r.json()
        result = run_data["results"][0]
        assert "actual_output" in result
        assert "check_results" in result
        assert "passed" in result
        assert "score" in result

        # 统计弹窗「详情」: GET case history 返回 history[i]
        r = client.get(f"/api/evalsets/{evalset_id}/cases/{case_id}/history?project_id={proj_id}")
        hh = r.json()["history"][0]
        # 字段一致：两者都有 actual_output / check_results / passed / score
        for field in ("actual_output", "check_results", "passed", "score", "eval_type"):
            assert field in result, f"run result 缺 {field}"
            assert field in hh, f"history 项缺 {field}"


# ============== P-2: 评测集复合筛选器 ==============

class TestP2CompositeFilter:
    """P-2: 评测集复合筛选器（关键字 + 类型 + 标签 + 状态，AND 叠加）"""

    def _setup_evalset_with_mixed_cases(self, client, isolated_storage):
        """建立含多种类型/标签/状态的评测集"""
        r = client.post("/api/projects", json={"name": "p2-proj", "task_shape": "general"})
        proj = r.json()
        proj_id, evalset_id = proj["id"], proj["evalset_id"]

        # 通过 PUT /evalsets/{id} 全量设置 cases
        cases = [
            {"id": "case-exact-01", "case_name": "EXACT-01", "input": "exact input", "eval_type": "exact",
             "expected_output": "ok", "tags": ["A"], "enabled": True},
            {"id": "case-exact-02", "case_name": "EXACT-02", "input": "exact disabled", "eval_type": "exact",
             "expected_output": "no", "tags": ["B"], "enabled": False},
            {"id": "case-contains-01", "case_name": "CONTAINS-01", "input": "contains text", "eval_type": "contains",
             "eval_params": {"substring": "x"}, "tags": ["A"], "enabled": True},
            {"id": "case-llm-01", "case_name": "LLM-01", "input": "llm judge case", "eval_type": "llm_judge",
             "output_requirement": "判断", "tags": ["B"], "enabled": True},
            {"id": "case-len-01", "case_name": "LEN-01", "input": "length check", "eval_type": "length",
             "eval_params": {"min": 1, "max": 10}, "tags": [], "enabled": True},
        ]
        r = client.put(f"/api/evalsets/{evalset_id}", json={
            "id": evalset_id, "project_id": proj_id, "name": "P2-ES", "cases": cases,
        })
        assert r.status_code == 200
        return proj_id, evalset_id

    def _get_cases(self, client, proj_id, evalset_id):
        r = client.get(f"/api/evalsets/{evalset_id}?project_id={proj_id}")
        assert r.status_code == 200
        return r.json()["cases"]

    def test_type_filter_isolation(self, client, isolated_storage):
        """P-2: 类型筛选只返回对应类型的 case"""
        proj_id, evalset_id = self._setup_evalset_with_mixed_cases(client, isolated_storage)
        cases = self._get_cases(client, proj_id, evalset_id)
        exact_cases = [c for c in cases if c["eval_type"] == "exact"]
        assert len(exact_cases) == 2
        llm_cases = [c for c in cases if c["eval_type"] == "llm_judge"]
        assert len(llm_cases) == 1

    def test_status_filter_enabled_vs_disabled(self, client, isolated_storage):
        """P-2: 状态筛选 enabled/disabled 正确"""
        proj_id, evalset_id = self._setup_evalset_with_mixed_cases(client, isolated_storage)
        cases = self._get_cases(client, proj_id, evalset_id)
        enabled = [c for c in cases if c["enabled"]]
        disabled = [c for c in cases if not c["enabled"]]
        assert len(enabled) == 4
        assert len(disabled) == 1
        assert disabled[0]["case_name"] == "EXACT-02"

    def test_tag_and_type_composite_and(self, client, isolated_storage):
        """P-2: 标签 + 类型 AND 叠加（前端筛选逻辑的后端数据验证）"""
        proj_id, evalset_id = self._setup_evalset_with_mixed_cases(client, isolated_storage)
        cases = self._get_cases(client, proj_id, evalset_id)
        # 标签 A + 类型 exact → 1 条（EXACT-01）
        filtered = [c for c in cases
                    if "A" in (c.get("tags") or []) and c["eval_type"] == "exact"]
        assert len(filtered) == 1
        assert filtered[0]["case_name"] == "EXACT-01"
        # 标签 B + 类型 llm_judge → 1 条（LLM-01）
        filtered = [c for c in cases
                    if "B" in (c.get("tags") or []) and c["eval_type"] == "llm_judge"]
        assert len(filtered) == 1
        assert filtered[0]["case_name"] == "LLM-01"

    def test_keyword_search_case_name_and_input(self, client, isolated_storage):
        """P-2: 关键字搜索 case_name + input 子串"""
        proj_id, evalset_id = self._setup_evalset_with_mixed_cases(client, isolated_storage)
        cases = self._get_cases(client, proj_id, evalset_id)
        # 关键字 "llm" → 匹配 case_name "LLM-01"
        filtered = [c for c in cases
                    if "llm" in (c["case_name"] or "").lower()
                    or "llm" in (c["input"] or "").lower()]
        assert len(filtered) == 1
        # 关键字 "disabled" → 匹配 input "exact disabled"
        filtered = [c for c in cases
                    if "disabled" in (c["case_name"] or "").lower()
                    or "disabled" in (c["input"] or "").lower()]
        assert len(filtered) == 1
        assert filtered[0]["case_name"] == "EXACT-02"


# ============== P-3: 概览页重构 ==============

class TestP3OverviewRedesign:
    """P-3: 概览页 4 区块数据正确"""

    def _setup_project_with_runs(self, isolated_storage, same_version=True):
        """建立项目 + 评测集 + 2 个 completed run（同版本或跨版本）"""
        proj_id = "proj-p3-overview"
        evalset_id = "evalset-p3"
        # 项目
        from app.models import Project, JudgeConfig, TargetConfig, ProjectVersion
        v1 = ProjectVersion(id="ver-v1", name="v1", created_at="2026-08-01T00:00:00Z")
        v2 = ProjectVersion(id="ver-v2", name="v2", created_at="2026-08-10T00:00:00Z")
        proj = Project(
            id=proj_id, name="P3-Overview", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
            versions=[v1, v2],
        )
        storage.save_project(proj)
        # 评测集
        es = EvalSet(id=evalset_id, project_id=proj_id, name="P3-ES", cases=[])
        storage.save_evalset(es)
        # run 1（v1，pass_rate=0.5）
        run1 = EvalRun(
            id="run-p3-01", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-05T00:00:00Z", version_id="ver-v1",
            results=[
                CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=10),
                CaseResult(case_id="c2", case_name="C2", passed=False, score=0.0, actual_output="bad", latency_ms=100, token_used=10),
            ],
            summary=EvalSummary(pass_rate=0.5, total_token=20, total_latency_ms=200, judge_token=0,
                                latency_p50=100, latency_p95=100, token_per_pass=1.0),
        )
        storage.save_run(run1)
        # run 2（v2，pass_rate=0.8）
        run2 = EvalRun(
            id="run-p3-02", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-15T00:00:00Z", version_id="ver-v2",
            results=[
                CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=80, token_used=10),
                CaseResult(case_id="c2", case_name="C2", passed=True, score=1.0, actual_output="ok", latency_ms=80, token_used=10),
            ],
            summary=EvalSummary(pass_rate=1.0, total_token=20, total_latency_ms=160, judge_token=5,
                                latency_p50=80, latency_p95=80, token_per_pass=1.0),
        )
        storage.save_run(run2)
        return proj_id, evalset_id

    def test_overview_delta_same_version_baseline(self, client, isolated_storage):
        """P-3 ①: delta 基准=同版本内上次 completed run"""
        proj_id, _ = self._setup_project_with_runs(isolated_storage)
        r = client.get(f"/api/projects/{proj_id}/overview")
        assert r.status_code == 200
        data = r.json()
        delta = data["delta"]
        assert delta is not None
        # run2 是 v2，同版本内只有 1 个 → first_in_version=True
        assert delta["is_first_in_version"] is True
        # current = run2 的 pass_rate = 1.0
        assert delta["pass_rate"]["current"] == 1.0
        # 同版本无上次 → previous=None
        assert delta["pass_rate"]["previous"] is None

    def test_overview_trend_includes_version_and_status(self, client, isolated_storage):
        """P-3 ②: 趋势含 version_id / created_at / status / judge_token"""
        proj_id, _ = self._setup_project_with_runs(isolated_storage)
        r = client.get(f"/api/projects/{proj_id}/overview")
        trend = r.json()["trend"]
        assert len(trend) == 2
        # 旧→新
        assert trend[0]["run_id"] == "run-p3-01"
        assert trend[1]["run_id"] == "run-p3-02"
        for t in trend:
            assert "version_id" in t
            assert "created_at" in t
            assert "status" in t
            assert "judge_token" in t

    def test_overview_versions_returned(self, client, isolated_storage):
        """P-3 ②: 版本列表返回（用于趋势分段）"""
        proj_id, _ = self._setup_project_with_runs(isolated_storage)
        r = client.get(f"/api/projects/{proj_id}/overview")
        versions = r.json()["versions"]
        assert len(versions) == 2
        names = [v["name"] for v in versions]
        assert "v1" in names and "v2" in names

    def test_overview_failed_cases_navigation(self, client, isolated_storage):
        """P-3 ④: 上次 run 失败 case 列表（导航到问题）"""
        proj_id, _ = self._setup_project_with_runs(isolated_storage)
        r = client.get(f"/api/projects/{proj_id}/overview")
        data = r.json()
        # run2 全通过 → failed_cases=[]
        assert data["failed_cases"] == []
        assert data["latest_run_id"] == "run-p3-02"

    def test_overview_failed_cases_when_run_has_failures(self, client, isolated_storage):
        """P-3 ④: 上次 run 有失败 case 时返回列表"""
        proj_id, evalset_id = self._setup_project_with_runs(isolated_storage)
        # 覆盖 run2 为有失败
        run2 = storage.get_run("run-p3-02", proj_id)
        run2.results[1].passed = False  # C2 失败
        storage.save_run(run2)
        r = client.get(f"/api/projects/{proj_id}/overview")
        failed = r.json()["failed_cases"]
        assert len(failed) == 1
        assert failed[0]["case_name"] == "C2"

    def test_overview_stability_block(self, client, isolated_storage):
        """P-3 ③: 稳定性区块 min_pass_pow_3 + 计数"""
        proj_id, _ = self._setup_project_with_runs(isolated_storage)
        r = client.get(f"/api/projects/{proj_id}/overview")
        s = r.json()["stability"]
        assert "min_pass_pow_3" in s
        assert "below_50_count" in s
        assert "below_80_count" in s
        assert "unstable_top3" in s

    def test_overview_unstable_top3_excludes_stable_cases(self, client, isolated_storage):
        """P-3 ③: unstable_top3 只含 pass^3 < 0.8 的 case，不列 100% 稳定的"""
        proj_id, _ = self._setup_project_with_runs(isolated_storage)
        r = client.get(f"/api/projects/{proj_id}/overview")
        unstable = r.json()["stability"]["unstable_top3"]
        for c in unstable:
            assert c["pass_pow_3"] < 0.8, f"stable case (pass^3={c['pass_pow_3']}) should not be in unstable list"

    def test_overview_no_runs_graceful(self, client, isolated_storage):
        """P-3: 项目无 run 时概览不报错"""
        from app.models import Project, JudgeConfig, TargetConfig
        proj_id = "proj-p3-empty"
        proj = Project(
            id=proj_id, name="P3-Empty", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
        )
        storage.save_project(proj)
        r = client.get(f"/api/projects/{proj_id}/overview")
        assert r.status_code == 200
        data = r.json()
        assert data["delta"] is None
        assert data["trend"] == []
        assert data["failed_cases"] == []
        assert data["latest_run_id"] is None
