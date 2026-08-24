"""Q-3 ~ Q-6 扩展测试

覆盖：
- Q-3: 版本切换（current_version_id + 切换路由 + 新 run 默认归属 + overview 按当前版本作用域）
- Q-4: pass rate 口径显性化 + 趋势分色连线（后端口径字段）
- Q-5: 模型价格峰谷定价 + 引用式模型源端点
- Q-6: 批量预估（P5/P50/P95 + 跳过比例）
"""

import pytest

from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    EvalRun, EvalSet, EvalCase, CaseResult, EvalSummary, ProjectVersion,
    JudgeConfig, TargetConfig, Project,
)
from app import storage


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def isolated_storage(tmp_path):
    """隔离存储目录（含 MODEL_PRICES_FILE / TAGS_FILE）"""
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
    storage.MODEL_PRICES_FILE = tmp_path / "model-prices.json"
    yield storage


# ============== Q-3: 版本切换 ==============

class TestQ3VersionSwitch:
    """Q-3: current_version_id 切换 + 新 run 默认归属"""

    def _make_project(self, storage_mod, proj_id="q3-proj", versions=None, current=None):
        proj = Project(
            id=proj_id, name="Q3",
            judge_config=JudgeConfig(base_url="https://j.example.com", model="m", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="t", api_type="openai_compatible"),
            versions=versions or [],
            current_version_id=current,
        )
        storage_mod.save_project(proj)
        return proj

    def test_create_version_auto_activates(self, client, isolated_storage):
        """新建版本 → 自动成为当前活动版本"""
        self._make_project(isolated_storage)
        proj_id = "q3-proj"
        r = client.post(f"/api/projects/{proj_id}/versions", json={"name": "v1"})
        assert r.status_code == 201
        vid = r.json()["id"]
        proj = client.get(f"/api/projects/{proj_id}").json()
        assert proj["current_version_id"] == vid

    def test_activate_existing_version(self, client, isolated_storage):
        """切换到已有版本 → current_version_id 更新"""
        v1 = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")
        v2 = ProjectVersion(id="ver-2", name="v2", created_at="2026-08-10T00:00:00Z")
        self._make_project(isolated_storage, versions=[v1, v2], current="ver-2")
        proj_id = "q3-proj"
        r = client.post(f"/api/projects/{proj_id}/versions/ver-1/activate")
        assert r.status_code == 200
        assert r.json()["current_version_id"] == "ver-1"
        proj = client.get(f"/api/projects/{proj_id}").json()
        assert proj["current_version_id"] == "ver-1"

    def test_activate_nonexistent_404(self, client, isolated_storage):
        """切换到不存在版本 → 404"""
        v1 = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")
        self._make_project(isolated_storage, versions=[v1])
        r = client.post(f"/api/projects/q3-proj/versions/ver-x/activate")
        assert r.status_code == 404

    def test_resolve_version_prefers_current(self, isolated_storage):
        """current_version_id 优先于 created_at 回退（切回旧版本时新 run 归旧版本）"""
        from app.routes import _resolve_version_id
        v1 = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")
        v2 = ProjectVersion(id="ver-2", name="v2", created_at="2026-08-10T00:00:00Z")
        proj = self._make_project(isolated_storage, versions=[v1, v2], current="ver-1")
        # created_at 在 v2 之后，但 current=ver-1 → 归 ver-1
        assert _resolve_version_id(proj, "2026-08-20T00:00:00Z", None) == "ver-1"

    def test_resolve_version_explicit_overrides_current(self, isolated_storage):
        """显式指定版本 > current_version_id"""
        from app.routes import _resolve_version_id
        v1 = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")
        v2 = ProjectVersion(id="ver-2", name="v2", created_at="2026-08-10T00:00:00Z")
        proj = self._make_project(isolated_storage, versions=[v1, v2], current="ver-1")
        assert _resolve_version_id(proj, "2026-08-20T00:00:00Z", "ver-2") == "ver-2"

    def test_resolve_version_falls_back_to_created_at_when_no_current(self, isolated_storage):
        """无 current_version_id → 按 created_at 落入最近版本（向后兼容）"""
        from app.routes import _resolve_version_id
        v1 = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")
        v2 = ProjectVersion(id="ver-2", name="v2", created_at="2026-08-10T00:00:00Z")
        proj = self._make_project(isolated_storage, versions=[v1, v2], current=None)
        assert _resolve_version_id(proj, "2026-08-20T00:00:00Z", None) == "ver-2"

    def test_delete_current_version_clears_current(self, client, isolated_storage):
        """删除当前活动版本 → current_version_id 清空"""
        v1 = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")
        self._make_project(isolated_storage, versions=[v1], current="ver-1")
        r = client.delete(f"/api/projects/q3-proj/versions/ver-1")
        assert r.status_code == 200
        proj = client.get(f"/api/projects/q3-proj").json()
        assert proj["current_version_id"] is None

    def test_overview_delta_scoped_to_current_version(self, client, isolated_storage):
        """切换版本后 overview delta/latest 聚焦当前版本"""
        proj_id = "q3-ov-proj"
        evalset_id = "q3-ov-es"
        v1 = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")
        v2 = ProjectVersion(id="ver-2", name="v2", created_at="2026-08-10T00:00:00Z")
        proj = Project(
            id=proj_id, name="Q3ov",
            judge_config=JudgeConfig(base_url="https://j.example.com", model="m", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="t", api_type="openai_compatible"),
            versions=[v1, v2], current_version_id="ver-1",
        )
        isolated_storage.save_project(proj)
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES",
                     cases=[EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok")])
        isolated_storage.save_evalset(es)
        # v1 两条 run（pass_rate 1.0 → 0.0），v2 一条 run（pass_rate 1.0）
        run_v1_a = EvalRun(
            id="run-v1a", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-05T00:00:00Z", version_id="ver-1",
            results=[CaseResult(case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=10)],
            summary=EvalSummary(pass_rate=1.0, total_token=10, total_latency_ms=100, judge_token=0, latency_p50=100, latency_p95=100, token_per_pass=1.0),
        )
        run_v1_b = EvalRun(
            id="run-v1b", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-06T00:00:00Z", version_id="ver-1",
            results=[CaseResult(case_name="C1", passed=False, score=0.0, actual_output="bad", latency_ms=100, token_used=10)],
            summary=EvalSummary(pass_rate=0.0, total_token=10, total_latency_ms=100, judge_token=0, latency_p50=100, latency_p95=100, token_per_pass=0.0),
        )
        run_v2 = EvalRun(
            id="run-v2", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-20T00:00:00Z", version_id="ver-2",
            results=[CaseResult(case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=10)],
            summary=EvalSummary(pass_rate=1.0, total_token=10, total_latency_ms=100, judge_token=0, latency_p50=100, latency_p95=100, token_per_pass=1.0),
        )
        isolated_storage.save_run(run_v1_a)
        isolated_storage.save_run(run_v1_b)
        isolated_storage.save_run(run_v2)
        # current=ver-1 → delta.latest = run-v1b（v1 最新），而非全局最新 run-v2
        r = client.get(f"/api/projects/{proj_id}/overview")
        data = r.json()
        assert data["current_version_id"] == "ver-1"
        assert data["delta"]["pass_rate"]["current"] == 0.0  # run-v1b
        assert data["delta"]["pass_rate"]["previous"] == 1.0  # run-v1a
        assert data["latest_run_id"] == "run-v1b"


# ============== Q-4: pass rate 口径显性化 + 分色连线 ==============

class TestQ4Caliber:
    """Q-4: 口径字段 + 优先全量 run + 口径一致性"""

    def _setup(self, storage_mod, runs, proj_id="q4-proj", evalset_id="q4-es",
               evalset_cases=None, current_version=None):
        v1 = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")
        proj = Project(
            id=proj_id, name="Q4",
            judge_config=JudgeConfig(base_url="https://j.example.com", model="m", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="t", api_type="openai_compatible"),
            versions=[v1], current_version_id=current_version,
        )
        storage_mod.save_project(proj)
        cases = evalset_cases or [EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok")]
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES", cases=cases)
        storage_mod.save_evalset(es)
        for r in runs:
            storage_mod.save_run(r)
        return proj_id

    def _run(self, rid, pr, ts=None, created="2026-08-10T00:00:00Z", evalset_id="q4-es"):
        results = [CaseResult(case_name="C1", passed=(pr >= 0.5), score=pr, actual_output="ok",
                             latency_ms=100, token_used=10)]
        return EvalRun(
            id=rid, project_id="q4-proj", evalset_id=evalset_id, status="completed",
            created_at=created, version_id="ver-1",
            results=results,
            summary=EvalSummary(pass_rate=pr, total_token=10, total_latency_ms=100, judge_token=0,
                                latency_p50=100, latency_p95=100, token_per_pass=pr),
            filter_tags=ts or [],
        )

    def test_trend_has_caliber_fields(self, client, isolated_storage):
        """trend 每条含 filter_tags/is_full/case_count/evalset_case_count/coverage_ratio"""
        self._setup(isolated_storage, [self._run("r1", 1.0), self._run("r2", 0.0, ts=["smoke"], created="2026-08-11T00:00:00Z")])
        r = client.get("/api/projects/q4-proj/overview")
        trend = r.json()["trend"]
        assert len(trend) == 2
        full_pt = next(t for t in trend if t["run_id"] == "r1")
        sub_pt = next(t for t in trend if t["run_id"] == "r2")
        assert full_pt["is_full"] is True
        assert full_pt["filter_tags"] == []
        assert full_pt["case_count"] == 1
        assert full_pt["evalset_case_count"] == 1
        assert full_pt["coverage_ratio"] == 1.0
        assert sub_pt["is_full"] is False
        assert sub_pt["filter_tags"] == ["smoke"]
        assert sub_pt["coverage_ratio"] == 1.0

    def test_delta_prefers_full_run_headline(self, client, isolated_storage):
        """最近一条是子集 run，但有更早的全量 run → delta.current = 全量 run 的 pass_rate"""
        # r1 全量 pr=0.8（早），r2 smoke 子集 pr=1.0（晚）
        self._setup(isolated_storage, [
            self._run("r1", 0.8, created="2026-08-10T00:00:00Z"),
            self._run("r2", 1.0, ts=["smoke"], created="2026-08-11T00:00:00Z"),
        ])
        r = client.get("/api/projects/q4-proj/overview")
        delta = r.json()["delta"]
        # headline 取全量 r1（0.8），而非最近子集 r2（1.0）
        assert delta["pass_rate"]["current"] == 0.8
        assert delta["caliber"]["is_full"] is True
        assert delta["caliber"]["group"] == "全量"

    def test_delta_caliber_consistent_same_full(self, client, isolated_storage):
        """两条全量 run → baseline 同口径，consistent=True，note=None"""
        self._setup(isolated_storage, [
            self._run("r1", 0.8, created="2026-08-10T00:00:00Z"),
            self._run("r2", 1.0, created="2026-08-11T00:00:00Z"),
        ])
        delta = client.get("/api/projects/q4-proj/overview").json()["delta"]
        assert delta["caliber"]["consistent"] is True
        assert delta["caliber"]["note"] is None
        assert delta["caliber"]["previous_group"] == "全量"

    def test_delta_caliber_inconsistent_warns(self, client, isolated_storage):
        """latest 全量，baseline 是 smoke 子集 → consistent=False，note 提示口径不同"""
        # 同版本内：r1 smoke（早），r2 全量（晚）。latest=全量 r2，baseline 同版本最近=r1（smoke，不同口径）
        self._setup(isolated_storage, [
            self._run("r1", 1.0, ts=["smoke"], created="2026-08-10T00:00:00Z"),
            self._run("r2", 0.8, created="2026-08-11T00:00:00Z"),
        ])
        delta = client.get("/api/projects/q4-proj/overview").json()["delta"]
        assert delta["caliber"]["current_group"] == "全量"
        assert delta["caliber"]["previous_group"] == "smoke"
        assert delta["caliber"]["consistent"] is False
        assert "口径不同" in delta["caliber"]["note"]

    def test_delta_caliber_subset_run_group_name(self, client, isolated_storage):
        """无全量 run 时 latest 取子集 run，group 标签组名"""
        # 两条都是 smoke 子集
        self._setup(isolated_storage, [
            self._run("r1", 0.5, ts=["smoke"], created="2026-08-10T00:00:00Z"),
            self._run("r2", 1.0, ts=["smoke"], created="2026-08-11T00:00:00Z"),
        ])
        delta = client.get("/api/projects/q4-proj/overview").json()["delta"]
        assert delta["caliber"]["group"] == "smoke"
        assert delta["caliber"]["is_full"] is False
        assert delta["caliber"]["consistent"] is True  # 同 smoke 口径


# ============== Q-5: 模型价格峰谷定价 + 引用式模型源 ==============

class TestQ5PeakOffPeak:
    """Q-5: 峰谷定价 + cost_estimate 按时段选价 + 旧条目迁移 + 编辑 + 源端点"""

    def test_create_with_peak_off_peak_stored(self, client, isolated_storage):
        """新建含峰谷字段 → 落盘含全部字段"""
        r = client.post("/api/model-prices", json={
            "endpoint_pattern": "", "model_pattern": "deepseek", "price_per_mtok": 1.0,
            "peak_price_per_mtok": 2.0, "off_peak_price_per_mtok": 1.0,
            "peak_start_hour": 9, "peak_end_hour": 22, "currency": "¥",
        })
        assert r.status_code == 201
        item = r.json()
        assert item["peak_price_per_mtok"] == 2.0
        assert item["off_peak_price_per_mtok"] == 1.0
        assert item["peak_start_hour"] == 9
        assert item["peak_end_hour"] == 22

    def test_cost_peak_hour_uses_peak(self, isolated_storage):
        """峰时段 run → 用峰价"""
        isolated_storage.save_model_price("", "deepseek", 1.0, "¥",
                                          peak_price_per_mtok=2.0, off_peak_price_per_mtok=1.0)
        # 12:00 在 9-22 峰时段内
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com", "deepseek-v3",
            None, None, 1_000_000, 0, "2026-08-10T12:00:00Z",
        )
        assert cost["target_cost"] == 2.0  # 1e6/1e6 * 2.0

    def test_cost_off_peak_hour_uses_off_peak(self, isolated_storage):
        """谷时段 run → 用谷价"""
        isolated_storage.save_model_price("", "deepseek", 1.0, "¥",
                                          peak_price_per_mtok=2.0, off_peak_price_per_mtok=1.0)
        # 03:00 在谷时段
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com", "deepseek-v3",
            None, None, 1_000_000, 0, "2026-08-10T03:00:00Z",
        )
        assert cost["target_cost"] == 1.0

    def test_cost_off_peak_none_falls_back_to_peak(self, isolated_storage):
        """谷价未配置 → 谷时段也用峰价"""
        isolated_storage.save_model_price("", "deepseek", 1.0, "¥",
                                          peak_price_per_mtok=2.0)  # 无谷价
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com", "deepseek-v3",
            None, None, 1_000_000, 0, "2026-08-10T03:00:00Z",
        )
        assert cost["target_cost"] == 2.0  # 回退峰价

    def test_cost_old_entry_migration(self, isolated_storage):
        """旧条目（仅 price_per_mtok）→ 峰=price_per_mtok，谷回退峰；峰谷时段都用同一价"""
        isolated_storage.save_model_price("", "deepseek", 1.5, "¥")  # 旧式无峰谷
        peak_cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com", "deepseek-v3",
            None, None, 1_000_000, 0, "2026-08-10T12:00:00Z",
        )
        off_cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com", "deepseek-v3",
            None, None, 1_000_000, 0, "2026-08-10T03:00:00Z",
        )
        assert peak_cost["target_cost"] == 1.5
        assert off_cost["target_cost"] == 1.5  # 谷回退峰

    def test_cost_no_timestamp_uses_peak(self, isolated_storage):
        """未提供 run_created_at → 峰价兜底（向后兼容旧调用）"""
        isolated_storage.save_model_price("", "deepseek", 1.0, "¥",
                                          peak_price_per_mtok=2.0, off_peak_price_per_mtok=1.0)
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com", "deepseek-v3",
            None, None, 1_000_000, 0,
        )
        assert cost["target_cost"] == 2.0

    def test_peak_boundary_hour(self, isolated_storage):
        """峰时段边界：peak_start_hour=9 → 9 属峰，22 属谷"""
        isolated_storage.save_model_price("", "deepseek", 1.0, "¥",
                                          peak_price_per_mtok=2.0, off_peak_price_per_mtok=1.0,
                                          peak_start_hour=9, peak_end_hour=22)
        c9 = isolated_storage.cost_estimate("https://api.deepseek.com", "deepseek-v3",
                                             None, None, 1_000_000, 0, "2026-08-10T09:00:00Z")
        c21 = isolated_storage.cost_estimate("https://api.deepseek.com", "deepseek-v3",
                                              None, None, 1_000_000, 0, "2026-08-10T21:00:00Z")
        c22 = isolated_storage.cost_estimate("https://api.deepseek.com", "deepseek-v3",
                                              None, None, 1_000_000, 0, "2026-08-10T22:00:00Z")
        assert c9["target_cost"] == 2.0   # 9 属峰
        assert c21["target_cost"] == 2.0  # 21 属峰
        assert c22["target_cost"] == 1.0  # 22 属谷

    def test_update_model_price(self, client, isolated_storage):
        """PUT 编辑价格 → 字段更新"""
        r = client.post("/api/model-prices", json={
            "endpoint_pattern": "", "model_pattern": "deepseek", "price_per_mtok": 1.0,
        })
        pid = r.json()["id"]
        r2 = client.put(f"/api/model-prices/{pid}", json={
            "peak_price_per_mtok": 3.0, "off_peak_price_per_mtok": 1.5, "currency": "$",
        })
        assert r2.status_code == 200
        item = r2.json()
        assert item["peak_price_per_mtok"] == 3.0
        assert item["off_peak_price_per_mtok"] == 1.5
        assert item["currency"] == "$"
        assert item["model_pattern"] == "deepseek"  # 未给字段保留

    def test_update_nonexistent_404(self, client, isolated_storage):
        """编辑不存在 → 404"""
        r = client.put("/api/model-prices/mp-none", json={"price_per_mtok": 2.0})
        assert r.status_code == 404

    def test_sources_returns_judge_and_target(self, client, isolated_storage):
        """源端点返回 Judge + Target 配置的模型名"""
        from app.models import JudgeConfig
        # Judge 配置（带 model）
        isolated_storage.save_judge_config("JC1", JudgeConfig(
            base_url="https://j.example.com", model="judge-m1", api_type="openai_compatible"))
        # 项目（target_config 带 model）
        isolated_storage.save_project(Project(
            id="src-proj", name="src-proj",
            judge_config=JudgeConfig(base_url="https://j.example.com", model="judge-m1", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="target-m1", api_type="openai_compatible"),
        ))
        r = client.get("/api/model-prices/sources")
        assert r.status_code == 200
        data = r.json()
        j_names = [m["name"] for m in data["judge_models"]]
        t_names = [m["name"] for m in data["target_models"]]
        assert "judge-m1" in j_names
        assert "target-m1" in t_names

    def test_run_detail_cost_uses_peak_hour(self, client, isolated_storage):
        """run 详情成本按 run.created_at 时段选峰谷价"""
        proj_id = "q5-run-proj"
        evalset_id = "q5-run-es"
        proj = Project(
            id=proj_id, name="Q5run",
            judge_config=JudgeConfig(base_url="https://j.example.com", model="m", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="deepseek-v3", api_type="openai_compatible"),
        )
        isolated_storage.save_project(proj)
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES",
                     cases=[EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok")])
        isolated_storage.save_evalset(es)
        # 价格：峰2 / 谷1
        isolated_storage.save_model_price("t.example", "deepseek-v3", 1.0, "¥",
                                          peak_price_per_mtok=2.0, off_peak_price_per_mtok=1.0)
        # 谷时段 run（03:00）→ 用谷价 1.0
        run = EvalRun(
            id="run-q5", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-10T03:00:00Z",
            results=[CaseResult(case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=1_000_000)],
            summary=EvalSummary(pass_rate=1.0, total_token=1_000_000, total_latency_ms=100, judge_token=0, latency_p50=100, latency_p95=100, token_per_pass=1.0),
        )
        isolated_storage.save_run(run)
        r = client.get(f"/api/runs/run-q5?project_id={proj_id}")
        cost = r.json()["cost"]
        assert cost["target_cost"] == 1.0  # 谷价


# ============== Q-6: 批量预估 ==============

class TestQ6BatchEstimate:
    """Q-6: P5/P50/P95 + 跳过比例 + 最小样本量 + 跨 run 校验"""

    def _make_project(self, storage_mod, proj_id="q6-proj", target_model="deepseek-v3",
                     judge_model="judge-m", versions=None, current=None):
        proj = Project(
            id=proj_id, name="Q6",
            judge_config=JudgeConfig(base_url="https://j.example.com", model=judge_model, api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model=target_model, api_type="openai_compatible"),
            versions=versions or [],
            current_version_id=current,
        )
        storage_mod.save_project(proj)
        return proj

    def _make_run(self, run_id, case_results, created="2026-08-10T12:00:00Z",
                  proj_id="q6-proj", evalset_id="q6-es", version_id=None, filter_tags=None):
        total_token = sum((cr.token_used or 0) for cr in case_results)
        judge_token = sum((cr.judge_token or 0) for cr in case_results)
        latencies = [cr.latency_ms or 0.0 for cr in case_results]
        passed = sum(1 for cr in case_results if cr.passed)
        total = len(case_results) or 1
        run = EvalRun(
            id=run_id, project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at=created, version_id=version_id,
            filter_tags=filter_tags or [],
            results=case_results,
            summary=EvalSummary(
                pass_rate=passed / total,
                total_token=total_token, total_latency_ms=sum(latencies),
                judge_token=judge_token,
                latency_p50=sorted(latencies)[len(latencies) // 2] if latencies else 0,
                latency_p95=max(latencies) if latencies else 0,
                token_per_pass=1.0,
            ),
        )
        return run

    def _case(self, name, token=1000, j_token=0, latency=100.0, passed=True,
              skipped_reason=None, actual_output="ok"):
        return CaseResult(
            case_name=name, passed=passed, score=1.0 if passed else 0.0,
            actual_output=actual_output, latency_ms=latency,
            token_used=token, judge_token=j_token, skipped_reason=skipped_reason,
        )

    def test_insufficient_samples_422(self, client, isolated_storage):
        """样本 < 30 → 422 + insufficient_samples"""
        self._make_project(isolated_storage)
        run = self._make_run("r1", [self._case(f"c{i}") for i in range(10)])
        isolated_storage.save_run(run)
        r = client.post("/api/projects/q6-proj/estimate", json={"count": 100})
        assert r.status_code == 422
        data = r.json()["detail"]["error"]
        assert data["code"] == "insufficient_samples"
        assert data["sample_count"] == 10

    def test_single_run_422(self, client, isolated_storage):
        """样本 ≥ 30 但仅 1 次 run → 422 + insufficient_runs"""
        self._make_project(isolated_storage)
        run = self._make_run("r1", [self._case(f"c{i}") for i in range(50)])
        isolated_storage.save_run(run)
        r = client.post("/api/projects/q6-proj/estimate", json={"count": 100})
        assert r.status_code == 422
        data = r.json()["detail"]["error"]
        assert data["code"] == "insufficient_runs"
        assert data["run_count"] == 1
        assert data["sample_count"] == 50

    def test_normal_estimate_intervals(self, client, isolated_storage):
        """两 run × 50 = 100 样本 → 正常区间，含 P5/P50/P95，单位 seconds"""
        self._make_project(isolated_storage)
        # r1 latency 100ms，r2 latency 200ms → 跨 run 多样性
        r1 = self._make_run("r1", [self._case(f"c{i}", latency=100.0, token=1000)
                                    for i in range(50)], created="2026-08-10T12:00:00Z")
        r2 = self._make_run("r2", [self._case(f"c{i}", latency=200.0, token=2000)
                                    for i in range(50)], created="2026-08-11T12:00:00Z")
        isolated_storage.save_run(r1)
        isolated_storage.save_run(r2)
        r = client.post("/api/projects/q6-proj/estimate", json={"count": 100, "concurrency": 1})
        assert r.status_code == 200
        data = r.json()
        # 成本区间
        assert "median" in data["cost"] and "p5" in data["cost"] and "p95" in data["cost"]
        assert data["cost"]["currency"] == "¥"  # 无价格条目时仍默认 ¥
        assert data["cost"]["p5"] <= data["cost"]["median"] <= data["cost"]["p95"]
        # 时间区间（单位 seconds；P50 latency=150ms × 100 / 1000 / 1 = 15s）
        assert data["time"]["unit"] == "seconds"
        assert data["time"]["p5"] <= data["time"]["median"] <= data["time"]["p95"]
        # 样本来源
        assert data["sample_count"] == 100
        assert data["run_count"] == 2
        assert data["skipped_ratio"] == 0.0
        assert data["low_confidence"] is False  # 100 样本正好达标

    def test_low_confidence_under_100(self, client, isolated_storage):
        """30~100 样本 → low_confidence=True + note 提示"""
        self._make_project(isolated_storage)
        r1 = self._make_run("r1", [self._case(f"c{i}", latency=100.0) for i in range(15)],
                            created="2026-08-10T12:00:00Z")
        r2 = self._make_run("r2", [self._case(f"c{i}", latency=200.0) for i in range(15)],
                            created="2026-08-11T12:00:00Z")
        isolated_storage.save_run(r1)
        isolated_storage.save_run(r2)
        r = client.post("/api/projects/q6-proj/estimate", json={"count": 10})
        assert r.status_code == 200
        data = r.json()
        assert data["low_confidence"] is True
        assert data["sample_count"] == 30
        assert data["note"] is not None
        assert "低置信" in data["note"]

    def test_skipped_ratio_separated(self, client, isolated_storage):
        """跳过 case 不计入区间，但 skipped_ratio 统计"""
        self._make_project(isolated_storage)
        cases_r1 = [self._case(f"ok-{i}", latency=100.0, token=1000) for i in range(40)]
        cases_r1.append(self._case("skip-1", token=0, j_token=0, skipped_reason="budget_exceeded"))
        cases_r2 = [self._case(f"ok-{i}", latency=200.0, token=2000) for i in range(40)]
        isolated_storage.save_run(self._make_run("r1", cases_r1, created="2026-08-10T12:00:00Z"))
        isolated_storage.save_run(self._make_run("r2", cases_r2, created="2026-08-11T12:00:00Z"))
        r = client.post("/api/projects/q6-proj/estimate", json={"count": 100})
        data = r.json()
        # 81 总样本，40 跳过 / 81 ≈ 0.4938
        assert data["sample_count"] == 81
        assert abs(data["skipped_ratio"] - round(1 / 81, 4)) < 0.001
        assert "跳过" in data["note"]

    def test_peak_off_peak_affects_cost(self, client, isolated_storage):
        """峰/谷时段不同 → 成本区间不同"""
        self._make_project(isolated_storage, target_model="deepseek-v3")
        isolated_storage.save_model_price("t.example", "deepseek-v3", 1.0, "¥",
                                          peak_price_per_mtok=2.0, off_peak_price_per_mtok=1.0,
                                          peak_start_hour=9, peak_end_hour=22)
        # 100 样本，每条 token=1e6 → 单样本峰价 2.0 / 谷价 1.0
        r1 = self._make_run("r1", [self._case(f"c{i}", token=1_000_000, latency=100.0)
                                    for i in range(50)], created="2026-08-10T12:00:00Z")
        r2 = self._make_run("r2", [self._case(f"c{i}", token=1_000_000, latency=100.0)
                                    for i in range(50)], created="2026-08-11T12:00:00Z")
        isolated_storage.save_run(r1)
        isolated_storage.save_run(r2)
        # 峰时段（12:00）→ P50 cost = 2.0 × 100 = 200
        r_peak = client.post("/api/projects/q6-proj/estimate",
                             json={"count": 100, "plan_hour": 12})
        # 谷时段（03:00）→ P50 cost = 1.0 × 100 = 100
        r_off = client.post("/api/projects/q6-proj/estimate",
                            json={"count": 100, "plan_hour": 3})
        peak = r_peak.json()["cost"]
        off = r_off.json()["cost"]
        assert peak["median"] == 200.0
        assert off["median"] == 100.0
        assert peak["currency"] == "¥"

    def test_version_scope_filters_samples(self, client, isolated_storage):
        """version_id 给定 → 仅取该版本 run（其他版本数据不混入）"""
        v1 = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")
        v2 = ProjectVersion(id="ver-2", name="v2", created_at="2026-08-05T00:00:00Z")
        self._make_project(isolated_storage, versions=[v1, v2], current="ver-2")
        # v1 两 run：100ms 延迟分布
        isolated_storage.save_run(self._make_run("r1a", [self._case(f"c{i}", latency=100.0) for i in range(25)],
                                                 created="2026-08-02T12:00:00Z", version_id="ver-1"))
        isolated_storage.save_run(self._make_run("r1b", [self._case(f"c{i}", latency=100.0) for i in range(25)],
                                                 created="2026-08-03T12:00:00Z", version_id="ver-1"))
        # v2 两 run：500ms 延迟（不同分布）
        isolated_storage.save_run(self._make_run("r2a", [self._case(f"c{i}", latency=500.0) for i in range(25)],
                                                 created="2026-08-06T12:00:00Z", version_id="ver-2"))
        isolated_storage.save_run(self._make_run("r2b", [self._case(f"c{i}", latency=500.0) for i in range(25)],
                                                 created="2026-08-07T12:00:00Z", version_id="ver-2"))
        # 限定 ver-1 → P50 latency=100ms × N=100 / 1000 / 1 = 10s
        r_v1 = client.post("/api/projects/q6-proj/estimate",
                           json={"count": 100, "version_id": "ver-1"})
        assert r_v1.status_code == 200
        assert r_v1.json()["time"]["median"] == 10.0
        assert "按版本" in r_v1.json()["note"]
        # 限定 ver-2 → P50 latency=500ms × 100 / 1000 = 50s
        r_v2 = client.post("/api/projects/q6-proj/estimate",
                           json={"count": 100, "version_id": "ver-2"})
        assert r_v2.json()["time"]["median"] == 50.0

    def test_tags_filter_runs(self, client, isolated_storage):
        """tags 给定 → 仅取 filter_tags 命中的 run"""
        self._make_project(isolated_storage)
        # smoke 标签 run：100ms（两次以满足跨 run 校验）
        isolated_storage.save_run(self._make_run("r1a", [self._case(f"c{i}", latency=100.0) for i in range(25)],
                                                 created="2026-08-10T12:00:00Z", filter_tags=["smoke"]))
        isolated_storage.save_run(self._make_run("r1b", [self._case(f"c{i}", latency=100.0) for i in range(25)],
                                                 created="2026-08-11T12:00:00Z", filter_tags=["smoke"]))
        # 全量 run：500ms
        isolated_storage.save_run(self._make_run("r2", [self._case(f"c{i}", latency=500.0) for i in range(50)],
                                                 created="2026-08-12T12:00:00Z", filter_tags=[]))
        # 选 smoke → P50=100ms × 100 / 1000 = 10s
        r = client.post("/api/projects/q6-proj/estimate",
                        json={"count": 100, "tags": ["smoke"]})
        assert r.status_code == 200
        assert r.json()["time"]["median"] == 10.0

    def test_concurrency_divides_time(self, client, isolated_storage):
        """concurrency=4 → 时间区间 ÷ 4"""
        self._make_project(isolated_storage)
        r1 = self._make_run("r1", [self._case(f"c{i}", latency=100.0) for i in range(50)],
                            created="2026-08-10T12:00:00Z")
        r2 = self._make_run("r2", [self._case(f"c{i}", latency=100.0) for i in range(50)],
                            created="2026-08-11T12:00:00Z")
        isolated_storage.save_run(r1)
        isolated_storage.save_run(r2)
        # 串行：P50 = 100ms × 100 / 1000 / 1 = 10s
        r_serial = client.post("/api/projects/q6-proj/estimate",
                               json={"count": 100, "concurrency": 1})
        # 并行 4：P50 = 10s / 4 = 2.5s
        r_par = client.post("/api/projects/q6-proj/estimate",
                            json={"count": 100, "concurrency": 4})
        assert r_serial.json()["time"]["median"] == 10.0
        assert r_par.json()["time"]["median"] == 2.5

    def test_invalid_count_422(self, client, isolated_storage):
        """count ≤ 0 → 422 + invalid_count"""
        self._make_project(isolated_storage)
        r1 = self._make_run("r1", [self._case(f"c{i}") for i in range(50)])
        r2 = self._make_run("r2", [self._case(f"c{i}") for i in range(50)],
                            created="2026-08-11T12:00:00Z")
        isolated_storage.save_run(r1)
        isolated_storage.save_run(r2)
        r = client.post("/api/projects/q6-proj/estimate", json={"count": 0})
        assert r.status_code == 422
        assert r.json()["detail"]["error"]["code"] == "invalid_count"

    def test_project_not_found_404(self, client, isolated_storage):
        """项目不存在 → 404"""
        r = client.post("/api/projects/none/estimate", json={"count": 100})
        assert r.status_code == 404

    def test_cost_no_price_returns_zero(self, client, isolated_storage):
        """无价格条目 → 成本区间为 0（不阻断预估）"""
        self._make_project(isolated_storage)
        r1 = self._make_run("r1", [self._case(f"c{i}", token=1000) for i in range(50)])
        r2 = self._make_run("r2", [self._case(f"c{i}", token=1000) for i in range(50)],
                            created="2026-08-11T12:00:00Z")
        isolated_storage.save_run(r1)
        isolated_storage.save_run(r2)
        r = client.post("/api/projects/q6-proj/estimate", json={"count": 100})
        data = r.json()
        assert data["cost"]["median"] == 0.0
        assert data["cost"]["p5"] == 0.0
        assert data["cost"]["p95"] == 0.0


