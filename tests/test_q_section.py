"""Q 系列测试

覆盖：
- Q-1: Judge 可比性机制（指纹 + judge_changed + 规则类切换）
- Q-2: 评测 ROI 报告（模型价格 + 成本金额化 + 降级）
"""

import pytest

from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    EvalRun, EvalSet, EvalCase, CaseResult, EvalSummary, ProjectVersion,
    JudgeConfig, TargetConfig, Project,
)
from app.judge import compute_judge_fingerprint
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
    storage.MODEL_PRICES_FILE = tmp_path / "model-prices.json"
    yield storage


# ============== Q-1: Judge 指纹 ==============

class TestQ1JudgeFingerprint:
    """Q-1: compute_judge_fingerprint 稳定性与不变性"""

    def test_fingerprint_stable_same_config(self):
        """相同配置 → 相同指纹"""
        jc = JudgeConfig(base_url="https://api.example.com", model="deepseek-v3", api_type="openai_compatible")
        fp1 = compute_judge_fingerprint(jc)
        fp2 = compute_judge_fingerprint(jc)
        assert fp1 == fp2
        assert len(fp1) == 12

    def test_fingerprint_changes_on_model_change(self):
        """改 model → 指纹变"""
        jc1 = JudgeConfig(base_url="https://api.example.com", model="deepseek-v3")
        jc2 = JudgeConfig(base_url="https://api.example.com", model="deepseek-v4")
        assert compute_judge_fingerprint(jc1) != compute_judge_fingerprint(jc2)

    def test_fingerprint_excludes_secret(self):
        """改 api_key 不影响指纹（不含 secret）"""
        jc1 = JudgeConfig(base_url="https://api.example.com", model="m1", api_key="secret-A")
        jc2 = JudgeConfig(base_url="https://api.example.com", model="m1", api_key="secret-B")
        assert compute_judge_fingerprint(jc1) == compute_judge_fingerprint(jc2)

    def test_fingerprint_changes_on_prompt_template(self):
        """改 prompt_template → 指纹变"""
        jc1 = JudgeConfig(base_url="https://api.example.com", model="m1", prompt_template="prompt A")
        jc2 = JudgeConfig(base_url="https://api.example.com", model="m1", prompt_template="prompt B")
        assert compute_judge_fingerprint(jc1) != compute_judge_fingerprint(jc2)

    def test_fingerprint_none_for_none(self):
        """None 配置 → None 指纹（旧 run 兼容）"""
        assert compute_judge_fingerprint(None) is None


class TestQ1RunFingerprintWritten:
    """Q-1: create_run 时写入 judge_fingerprint"""

    def test_run_has_fingerprint_on_create(self, client, isolated_storage):
        """发起 run → run 记录含 judge_fingerprint（模拟 create_run 指纹写入逻辑）"""
        from app.routes import _resolve_effective_judge_config, _resolve_version_id, _utc_now, _generate_run_id
        from app.models import EvalRun
        r = client.post("/api/projects", json={"name": "q1-proj", "task_shape": "general"})
        proj_id = r.json()["id"]
        client.put(f"/api/projects/{proj_id}", json={
            "name": "q1-proj",
            "judge_config": {"base_url": "https://judge.example.com", "model": "deepseek-v3", "api_type": "openai_compatible"},
            "target_config": {"base_url": "https://target.example.com", "model": "target-m1", "api_type": "openai_compatible"},
        })
        client.post("/api/evalsets", json={
            "project_id": proj_id, "name": "es",
            "cases": [{"id": "c1", "case_name": "C1", "input": "hi", "eval_type": "exact", "expected_output": "ok"}],
        })
        evalset_id = client.get(f"/api/projects/{proj_id}/evalsets").json()["evalsets"][0]["id"]
        # 模拟 create_run 的指纹写入逻辑（不触发后台任务）
        project = isolated_storage.get_project(proj_id)
        effective_judge = _resolve_effective_judge_config(project)
        judge_fingerprint = compute_judge_fingerprint(effective_judge)
        run = EvalRun(
            id=_generate_run_id(), project_id=proj_id, evalset_id=evalset_id,
            status="queued", created_at=_utc_now(),
            version_id=_resolve_version_id(project, _utc_now(), None),
            judge_fingerprint=judge_fingerprint,
        )
        isolated_storage.save_run(run)
        # 读回验证
        saved = isolated_storage.get_run(run.id, proj_id)
        assert saved is not None
        assert saved.judge_fingerprint is not None
        assert len(saved.judge_fingerprint) == 12
        assert saved.judge_fingerprint == judge_fingerprint


class TestQ1OverviewJudgeChanged:
    """Q-1: overview 返回 judge_changed + judge_fingerprints"""

    def _setup_two_runs(self, storage_mod, judge_model_v1="deepseek-v3", judge_model_v2="deepseek-v3"):
        """建项目 + 评测集 + 2 个 completed run（不同指纹可选）"""
        proj_id = "q1-ov-proj"
        evalset_id = "q1-ov-es"
        proj = Project(
            id=proj_id, name="Q1",
            judge_config=JudgeConfig(base_url="https://judge.example.com", model=judge_model_v1, api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="t-m", api_type="openai_compatible"),
            versions=[ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")],
        )
        storage_mod.save_project(proj)
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES",
                     cases=[EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok")])
        storage_mod.save_evalset(es)
        # run 1（旧，指纹 A）
        jc1 = JudgeConfig(base_url="https://judge.example.com", model=judge_model_v1, api_type="openai_compatible")
        fp1 = compute_judge_fingerprint(jc1)
        run1 = EvalRun(
            id="run-q1-1", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-10T00:00:00Z", version_id="ver-1",
            results=[CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=10)],
            summary=EvalSummary(pass_rate=1.0, total_token=10, total_latency_ms=100, judge_token=0, latency_p50=100, latency_p95=100, token_per_pass=1.0),
            judge_fingerprint=fp1,
        )
        storage_mod.save_run(run1)
        # run 2（新，指纹 B 可选不同）
        jc2 = JudgeConfig(base_url="https://judge.example.com", model=judge_model_v2, api_type="openai_compatible")
        fp2 = compute_judge_fingerprint(jc2)
        run2 = EvalRun(
            id="run-q1-2", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-20T00:00:00Z", version_id="ver-1",
            results=[CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=10)],
            summary=EvalSummary(pass_rate=1.0, total_token=10, total_latency_ms=100, judge_token=0, latency_p50=100, latency_p95=100, token_per_pass=1.0),
            judge_fingerprint=fp2,
        )
        storage_mod.save_run(run2)
        return proj_id, fp1, fp2

    def test_judge_not_changed_same_fingerprint(self, client, isolated_storage):
        """两 run 指纹相同 → judge_changed=False"""
        proj_id, fp1, fp2 = self._setup_two_runs(isolated_storage, "deepseek-v3", "deepseek-v3")
        r = client.get(f"/api/projects/{proj_id}/overview")
        data = r.json()
        assert data["judge_changed"] is False
        assert data["judge_fingerprints"]["latest"] == fp2
        assert data["judge_fingerprints"]["previous"] == fp1

    def test_judge_changed_different_fingerprint(self, client, isolated_storage):
        """两 run 指纹不同 → judge_changed=True"""
        proj_id, fp1, fp2 = self._setup_two_runs(isolated_storage, "deepseek-v3", "deepseek-v4")
        r = client.get(f"/api/projects/{proj_id}/overview")
        data = r.json()
        assert data["judge_changed"] is True
        assert fp1 != fp2

    def test_old_run_no_fingerprint_no_crash(self, client, isolated_storage):
        """旧 run 无指纹 → 不报错、judge_changed=False"""
        proj_id = "q1-old-proj"
        evalset_id = "q1-old-es"
        proj = Project(
            id=proj_id, name="Q1old",
            judge_config=JudgeConfig(base_url="https://judge.example.com", model="m", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="t", api_type="openai_compatible"),
            versions=[ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")],
        )
        isolated_storage.save_project(proj)
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES",
                     cases=[EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok")])
        isolated_storage.save_evalset(es)
        run = EvalRun(
            id="run-old", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-10T00:00:00Z", version_id="ver-1",
            results=[CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=10)],
            summary=EvalSummary(pass_rate=1.0, total_token=10, total_latency_ms=100, judge_token=0, latency_p50=100, latency_p95=100, token_per_pass=1.0),
        )
        isolated_storage.save_run(run)
        r = client.get(f"/api/projects/{proj_id}/overview")
        assert r.status_code == 200
        data = r.json()
        assert data["judge_changed"] is False

    def test_trend_includes_judge_fingerprint(self, client, isolated_storage):
        """趋势每条 run 含 judge_fingerprint 字段"""
        proj_id, _, _ = self._setup_two_runs(isolated_storage, "deepseek-v3", "deepseek-v3")
        r = client.get(f"/api/projects/{proj_id}/overview")
        trend = r.json()["trend"]
        assert len(trend) == 2
        for t in trend:
            assert "judge_fingerprint" in t


class TestQ1CaseHistoryFingerprint:
    """Q-1: case history 每条记录含 judge_fingerprint"""

    def test_history_includes_fingerprint(self, client, isolated_storage):
        proj_id = "q1-hist-proj"
        evalset_id = "q1-hist-es"
        proj = Project(
            id=proj_id, name="Q1hist",
            judge_config=JudgeConfig(base_url="https://judge.example.com", model="m", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="t", api_type="openai_compatible"),
            versions=[ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")],
        )
        isolated_storage.save_project(proj)
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES",
                     cases=[EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok")])
        isolated_storage.save_evalset(es)
        run = EvalRun(
            id="run-hist", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-10T00:00:00Z", version_id="ver-1",
            results=[CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=10)],
            summary=EvalSummary(pass_rate=1.0, total_token=10, total_latency_ms=100, judge_token=0, latency_p50=100, latency_p95=100, token_per_pass=1.0),
            judge_fingerprint="abc123def456",
        )
        isolated_storage.save_run(run)
        r = client.get(f"/api/evalsets/{evalset_id}/cases/c1/history?project_id={proj_id}")
        history = r.json()["history"]
        assert len(history) == 1
        assert history[0]["judge_fingerprint"] == "abc123def456"


class TestQ1RuleOnlyFilter:
    """Q-1: rule_only=true 时趋势/delta pass_rate 仅统计规则类 case"""

    def _setup_mixed_cases(self, storage_mod):
        """建项目 + 评测集（规则类 + llm_judge 混合）+ 2 run"""
        proj_id = "q1-rule-proj"
        evalset_id = "q1-rule-es"
        proj = Project(
            id=proj_id, name="Q1rule",
            judge_config=JudgeConfig(base_url="https://judge.example.com", model="m", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="t", api_type="openai_compatible"),
            versions=[ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")],
        )
        storage_mod.save_project(proj)
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES",
                     cases=[
                         EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok"),
                         EvalCase(id="c2", case_name="C2", input="hi", eval_type="llm_judge", output_requirement="friendly"),
                     ])
        storage_mod.save_evalset(es)
        # run 1：c1 通过，c2 失败 → 整体 pass_rate=0.5，规则类 pass_rate=1.0
        run1 = EvalRun(
            id="run-r1", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-10T00:00:00Z", version_id="ver-1",
            results=[
                CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=10),
                CaseResult(case_id="c2", case_name="C2", passed=False, score=0.0, actual_output="bad", latency_ms=100, token_used=10),
            ],
            summary=EvalSummary(pass_rate=0.5, total_token=20, total_latency_ms=200, judge_token=5, latency_p50=100, latency_p95=100, token_per_pass=1.0),
            judge_fingerprint="fp123abc4567",
        )
        storage_mod.save_run(run1)
        # run 2：c1 通过，c2 通过 → 整体 pass_rate=1.0，规则类 pass_rate=1.0
        run2 = EvalRun(
            id="run-r2", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-20T00:00:00Z", version_id="ver-1",
            results=[
                CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=80, token_used=10),
                CaseResult(case_id="c2", case_name="C2", passed=True, score=1.0, actual_output="ok", latency_ms=80, token_used=10),
            ],
            summary=EvalSummary(pass_rate=1.0, total_token=20, total_latency_ms=160, judge_token=5, latency_p50=80, latency_p95=80, token_per_pass=1.0),
            judge_fingerprint="fp123abc4567",
        )
        storage_mod.save_run(run2)
        return proj_id

    def test_rule_only_trend_pass_rate(self, client, isolated_storage):
        """rule_only=true → 趋势 pass_rate 只算规则类 case"""
        proj_id = self._setup_mixed_cases(isolated_storage)
        # 不带 rule_only：run1 整体 pass_rate=0.5
        r_all = client.get(f"/api/projects/{proj_id}/overview")
        trend_all = r_all.json()["trend"]
        assert trend_all[0]["pass_rate"] == 0.5  # run1 整体
        # 带 rule_only=true：run1 规则类 pass_rate=1.0（c1 通过，排除 c2）
        r_rule = client.get(f"/api/projects/{proj_id}/overview?rule_only=true")
        trend_rule = r_rule.json()["trend"]
        assert trend_rule[0]["pass_rate"] == 1.0  # run1 规则类

    def test_rule_only_delta_pass_rate(self, client, isolated_storage):
        """rule_only=true → delta pass_rate 只算规则类"""
        proj_id = self._setup_mixed_cases(isolated_storage)
        r_rule = client.get(f"/api/projects/{proj_id}/overview?rule_only=true")
        delta = r_rule.json()["delta"]
        # 两 run 规则类 pass_rate 都是 1.0 → diff=0
        assert delta["pass_rate"]["current"] == 1.0
        assert delta["pass_rate"]["previous"] == 1.0
        assert delta["pass_rate"]["diff"] == 0.0

    def test_rule_only_flag_returned(self, client, isolated_storage):
        """overview 响应含 rule_only 标志"""
        proj_id = self._setup_mixed_cases(isolated_storage)
        r = client.get(f"/api/projects/{proj_id}/overview?rule_only=true")
        assert r.json()["rule_only"] is True
        r2 = client.get(f"/api/projects/{proj_id}/overview")
        assert r2.json()["rule_only"] is False


# ============== Q-2: 模型价格 + 成本估算 ==============

class TestQ2ModelPricesAPI:
    """Q-2: 模型价格 CRUD API"""

    def test_list_empty(self, client, isolated_storage):
        """空列表"""
        r = client.get("/api/model-prices")
        assert r.status_code == 200
        assert r.json()["model_prices"] == []

    def test_create_and_list(self, client, isolated_storage):
        """新建 + 列表（端点 + 模型双 key）"""
        r = client.post("/api/model-prices", json={
            "endpoint_pattern": "api.deepseek", "model_pattern": "deepseek",
            "price_per_mtok": 1.0, "currency": "¥", "note": "官网",
        })
        assert r.status_code == 201
        item = r.json()
        assert item["endpoint_pattern"] == "api.deepseek"
        assert item["model_pattern"] == "deepseek"
        assert item["price_per_mtok"] == 1.0
        r2 = client.get("/api/model-prices")
        assert len(r2.json()["model_prices"]) == 1

    def test_create_both_empty_422(self, client, isolated_storage):
        """端点 + 模型都空 → 422"""
        r = client.post("/api/model-prices", json={"endpoint_pattern": "", "model_pattern": "", "price_per_mtok": 1.0})
        assert r.status_code == 422

    def test_create_endpoint_only_ok(self, client, isolated_storage):
        """只填端点、模型留空 → 允许（通配模型）"""
        r = client.post("/api/model-prices", json={"endpoint_pattern": "api.openai", "model_pattern": "", "price_per_mtok": 2.0})
        assert r.status_code == 201

    def test_delete(self, client, isolated_storage):
        """删除"""
        r = client.post("/api/model-prices", json={"endpoint_pattern": "", "model_pattern": "gpt", "price_per_mtok": 2.0})
        pid = r.json()["id"]
        r2 = client.delete(f"/api/model-prices/{pid}")
        assert r2.status_code == 200
        assert client.get("/api/model-prices").json()["model_prices"] == []

    def test_delete_not_found_404(self, client, isolated_storage):
        """删不存在 → 404"""
        r = client.delete("/api/model-prices/nonexistent")
        assert r.status_code == 404


class TestQ2CostEstimate:
    """Q-2: cost_estimate 计算逻辑（端点 + 模型双 key）"""

    def test_cost_with_prices(self, isolated_storage):
        """两端都有价格 → 给金额"""
        isolated_storage.save_model_price("api.deepseek", "deepseek", 1.0, "¥")
        isolated_storage.save_model_price("api.judge", "judge-model", 2.0, "¥")
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com/v1", "deepseek-v3",
            "https://api.judge.com", "judge-model-v2",
            1000, 500,
        )
        assert cost["target_cost"] == 0.001
        assert cost["judge_cost"] == 0.001
        assert cost["total_cost"] == 0.002
        assert cost["currency"] == "¥"

    def test_cost_no_price(self, isolated_storage):
        """无价格 → total_cost=None"""
        cost = isolated_storage.cost_estimate(
            "https://unknown.example.com", "unknown-model",
            "https://no-judge.com", "no-judge",
            1000, 500,
        )
        assert cost["target_cost"] is None
        assert cost["judge_cost"] is None
        assert cost["total_cost"] is None
        assert cost["currency"] is None

    def test_cost_partial_price(self, isolated_storage):
        """只有 target 价格 → judge_cost=None"""
        isolated_storage.save_model_price("", "deepseek", 1.0, "¥")
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com", "deepseek-v3",
            "https://no-judge.com", "no-judge",
            1000, 500,
        )
        assert cost["target_cost"] is not None
        assert cost["judge_cost"] is None
        assert cost["total_cost"] is not None

    def test_cost_specificity_model_prefix(self, isolated_storage):
        """更具体的模型前缀优先"""
        isolated_storage.save_model_price("", "deepseek", 1.0, "¥")
        isolated_storage.save_model_price("", "deepseek-v3", 0.5, "$")
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com", "deepseek-v3-chat",
            None, None, 1000, 0,
        )
        assert cost["target_cost"] == 0.0005
        assert cost["currency"] == "$"

    def test_cost_specificity_endpoint(self, isolated_storage):
        """同模型不同端点 → 各自匹配不同价格"""
        isolated_storage.save_model_price("api.provider-a", "deepseek", 1.0, "¥")
        isolated_storage.save_model_price("api.provider-b", "deepseek", 3.0, "¥")
        cost_a = isolated_storage.cost_estimate(
            "https://api.provider-a.com/v1", "deepseek-v3",
            None, None, 1000, 0,
        )
        cost_b = isolated_storage.cost_estimate(
            "https://api.provider-b.com/v1", "deepseek-v3",
            None, None, 1000, 0,
        )
        assert cost_a["target_cost"] == 0.001
        assert cost_b["target_cost"] == 0.003

    def test_cost_specificity_both_keys(self, isolated_storage):
        """端点+模型双 key 都具体 > 单 key 优先"""
        isolated_storage.save_model_price("", "deepseek", 2.0, "¥")
        isolated_storage.save_model_price("api.deepseek", "", 1.5, "¥")
        isolated_storage.save_model_price("api.deepseek", "deepseek", 1.0, "¥")
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com/v1", "deepseek-v3",
            None, None, 1000, 0,
        )
        # 最具体（双 key 都命中）= 1.0
        assert cost["target_cost"] == 0.001

    def test_cost_empty_endpoint_matches_any(self, isolated_storage):
        """endpoint_pattern 空 → 匹配任意端点（向后兼容）"""
        isolated_storage.save_model_price("", "deepseek", 1.0, "¥")
        cost = isolated_storage.cost_estimate(
            "https://random.example.com", "deepseek-v3",
            None, None, 1000, 0,
        )
        assert cost["target_cost"] is not None

    def test_cost_empty_model_matches_any(self, isolated_storage):
        """model_pattern 空 → 匹配任意模型"""
        isolated_storage.save_model_price("api.deepseek", "", 1.0, "¥")
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com/v1", "anything",
            None, None, 1000, 0,
        )
        assert cost["target_cost"] is not None

    def test_cost_zero_token(self, isolated_storage):
        """token=0 → cost=0（非 None）"""
        isolated_storage.save_model_price("", "deepseek", 1.0, "¥")
        cost = isolated_storage.cost_estimate(
            "https://api.deepseek.com", "deepseek-v3",
            None, None, 0, 0,
        )
        assert cost["target_cost"] == 0.0
        assert cost["total_cost"] == 0.0

    def test_cost_currency_display(self, isolated_storage):
        """不同币种正确显示"""
        isolated_storage.save_model_price("", "gpt", 5.0, "$")
        cost = isolated_storage.cost_estimate(
            "https://api.openai.com", "gpt-4",
            None, None, 1000, 0,
        )
        assert cost["currency"] == "$"

    def test_cost_backward_compat_old_data(self, isolated_storage):
        """旧数据无 endpoint_pattern 字段 → 等效空端点（匹配任意）"""
        # 直接写旧格式数据（无 endpoint_pattern）
        prices = isolated_storage._read_model_prices()
        prices.append({"id": "mp-old", "model_pattern": "deepseek", "price_per_mtok": 1.0, "currency": "¥", "note": ""})
        isolated_storage._write_model_prices(prices)
        cost = isolated_storage.cost_estimate(
            "https://any.example.com", "deepseek-v3",
            None, None, 1000, 0,
        )
        assert cost["target_cost"] is not None


class TestQ2OverviewCostField:
    """Q-2: overview 响应含成本字段"""

    def test_overview_delta_has_cost(self, client, isolated_storage):
        """delta 含 cost 字段"""
        proj_id = "q2-ov-proj"
        evalset_id = "q2-ov-es"
        proj = Project(
            id=proj_id, name="Q2",
            judge_config=JudgeConfig(base_url="https://j.example.com", model="deepseek-v3", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="deepseek-v3", api_type="openai_compatible"),
            versions=[ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")],
        )
        isolated_storage.save_project(proj)
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES",
                     cases=[EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok")])
        isolated_storage.save_evalset(es)
        run = EvalRun(
            id="run-q2", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-10T00:00:00Z", version_id="ver-1",
            results=[CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=1000)],
            summary=EvalSummary(pass_rate=1.0, total_token=1000, total_latency_ms=100, judge_token=500, latency_p50=100, latency_p95=100, token_per_pass=1.0),
        )
        isolated_storage.save_run(run)
        # 配价格（端点 + 模型双 key，端点子串匹配 base_url）
        isolated_storage.save_model_price("t.example", "deepseek-v3", 1.0, "¥")
        isolated_storage.save_model_price("j.example", "deepseek-v3", 1.0, "¥")
        r = client.get(f"/api/projects/{proj_id}/overview")
        delta = r.json()["delta"]
        assert "cost" in delta
        cost = delta["cost"]
        assert cost["total_cost"] is not None
        assert cost["currency"] == "¥"

    def test_overview_cost_null_no_price(self, client, isolated_storage):
        """无价格 → cost.total_cost=None"""
        proj_id = "q2-ov-np"
        evalset_id = "q2-ov-np-es"
        proj = Project(
            id=proj_id, name="Q2np",
            judge_config=JudgeConfig(base_url="https://j.example.com", model="m", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="t", api_type="openai_compatible"),
            versions=[ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")],
        )
        isolated_storage.save_project(proj)
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES",
                     cases=[EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok")])
        isolated_storage.save_evalset(es)
        run = EvalRun(
            id="run-q2-np", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-10T00:00:00Z", version_id="ver-1",
            results=[CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=10)],
            summary=EvalSummary(pass_rate=1.0, total_token=10, total_latency_ms=100, judge_token=0, latency_p50=100, latency_p95=100, token_per_pass=1.0),
        )
        isolated_storage.save_run(run)
        r = client.get(f"/api/projects/{proj_id}/overview")
        delta = r.json()["delta"]
        assert delta["cost"]["total_cost"] is None


class TestQ2RunDetailCostField:
    """Q-2: run 详情响应含 cost 字段"""

    def test_run_detail_has_cost(self, client, isolated_storage):
        """run 详情含 cost 字段"""
        proj_id = "q2-run-proj"
        evalset_id = "q2-run-es"
        proj = Project(
            id=proj_id, name="Q2run",
            judge_config=JudgeConfig(base_url="https://j.example.com", model="deepseek-v3", api_type="openai_compatible"),
            target_config=TargetConfig(base_url="https://t.example.com", model="deepseek-v3", api_type="openai_compatible"),
        )
        isolated_storage.save_project(proj)
        es = EvalSet(id=evalset_id, project_id=proj_id, name="ES",
                     cases=[EvalCase(id="c1", case_name="C1", input="hi", eval_type="exact", expected_output="ok")])
        isolated_storage.save_evalset(es)
        run = EvalRun(
            id="run-q2-det", project_id=proj_id, evalset_id=evalset_id, status="completed",
            created_at="2026-08-10T00:00:00Z",
            results=[CaseResult(case_id="c1", case_name="C1", passed=True, score=1.0, actual_output="ok", latency_ms=100, token_used=1000)],
            summary=EvalSummary(pass_rate=1.0, total_token=1000, total_latency_ms=100, judge_token=500, latency_p50=100, latency_p95=100, token_per_pass=1.0),
        )
        isolated_storage.save_run(run)
        isolated_storage.save_model_price("t.example", "deepseek-v3", 1.0, "¥")
        isolated_storage.save_model_price("j.example", "deepseek-v3", 1.0, "¥")
        r = client.get(f"/api/runs/run-q2-det?project_id={proj_id}")
        data = r.json()
        assert "cost" in data
        assert data["cost"]["total_cost"] is not None
