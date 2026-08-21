"""T2 节（批次 2）测试

T2-1: case 级采样分析 + 稳定 case_id
T2-2: Judge 配置复用（use_target_config）
T2-3: Target 配置模板
T2-4: token 预算硬限制
"""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.main import app
from fastapi.testclient import TestClient

from app.models import (
    EvalRun, CaseResult, EvalSummary, EvalCase, EvalSet, Project,
    TargetConfig, JudgeConfig, TokenBudget, AuthConfig, ResponseParsing,
)
from app.sampling import (
    _aggregate_runs_by_case_id,
    compute_evalset_sampling,
    pass_at_k_case,
    pass_pow_k_case,
)


@pytest.fixture
def client():
    return TestClient(app)


def _mk_run(project_id: str, evalset_id: str, results: list[CaseResult], status: str = "completed") -> EvalRun:
    """构造一个 completed run（带 summary）"""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    skipped = sum(1 for r in results if r.skipped_reason is not None)
    valid = total - skipped
    pass_rate = passed / valid if valid > 0 else 0.0
    summary = EvalSummary(
        pass_rate=pass_rate,
        total_token=sum(r.token_used for r in results),
        total_latency_ms=sum(r.latency_ms for r in results),
        token_per_pass=0.0,
        latency_p50=0.0,
        latency_p95=0.0,
    )
    return EvalRun(
        id=f"run-{project_id}-{evalset_id}-{id(results)}",
        project_id=project_id,
        evalset_id=evalset_id,
        status=status,
        created_at=datetime.now(timezone.utc).isoformat(),
        results=results,
        summary=summary,
    )


# ============== T2-1: CaseResult.case_id 带入 ==============

class TestCaseIdInResult:
    """T2-1: CaseResult.case_id 从 EvalCase.id 带入"""

    def test_case_id_field_exists(self):
        """CaseResult 有 case_id 字段，默认 None（向后兼容）"""
        r = CaseResult(case_name="A", actual_output="ok", passed=True)
        assert r.case_id is None

    def test_case_id_from_eval_case(self):
        """构造 CaseResult 时显式传入 case_id"""
        r = CaseResult(case_name="A", case_id="c-123", actual_output="ok", passed=True)
        assert r.case_id == "c-123"


# ============== T2-1: _aggregate_runs_by_case_id ==============

class TestAggregateByCaseId:
    """按 case_id（fallback case_name）聚合"""

    def test_group_by_case_id(self):
        """同 case_id 的 results 聚合到一组"""
        runs = [
            _mk_run("p", "e", [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True),
                CaseResult(case_name="B", case_id="c2", actual_output="ok", passed=False),
            ]),
            _mk_run("p", "e", [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=False),
                CaseResult(case_name="B", case_id="c2", actual_output="ok", passed=True),
            ]),
        ]
        records = _aggregate_runs_by_case_id(runs)
        assert set(records.keys()) == {"c1", "c2"}
        assert records["c1"]["records"] == [True, False]
        assert records["c2"]["records"] == [False, True]
        assert records["c1"]["case_name"] == "A"

    def test_fallback_case_name_when_no_case_id(self):
        """旧 run 无 case_id → fallback case_name 分组"""
        runs = [
            _mk_run("p", "e", [
                CaseResult(case_name="A", actual_output="ok", passed=True),  # case_id=None
            ]),
            _mk_run("p", "e", [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=False),  # 新 run 有 case_id
            ]),
        ]
        records = _aggregate_runs_by_case_id(runs)
        # 旧 run 用 case_name="A" 做 key，新 run 用 case_id="c1" → 两组（不连续，符合预期）
        assert "A" in records
        assert "c1" in records
        assert records["A"]["records"] == [True]
        assert records["c1"]["records"] == [False]

    def test_skip_skipped_cases(self):
        """skipped_reason 非空的 case 不计入 n"""
        runs = [
            _mk_run("p", "e", [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=False, skipped_reason="budget_exceeded"),
                CaseResult(case_name="B", case_id="c2", actual_output="ok", passed=True),
            ]),
        ]
        records = _aggregate_runs_by_case_id(runs)
        assert "c1" not in records  # 被跳过
        assert records["c2"]["records"] == [True]

    def test_only_completed_runs(self):
        """非 completed 的 run 不纳入"""
        runs = [
            _mk_run("p", "e", [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True),
            ], status="running"),
            _mk_run("p", "e", [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=False),
            ], status="completed"),
        ]
        records = _aggregate_runs_by_case_id(runs)
        assert records["c1"]["records"] == [False]  # 只取 completed 的


# ============== T2-1: compute_evalset_sampling ==============

class TestComputeEvalsetSampling:
    """评测集级 case 粒度采样分析"""

    def test_per_case_metrics_with_mock(self, monkeypatch):
        """mock list_runs，验证每 case 指标正确"""
        runs = []
        for passed_a in [True, False, True]:
            runs.append(_mk_run("p", "e", [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=passed_a),
                CaseResult(case_name="B", case_id="c2", actual_output="ok", passed=True),
            ]))
        from app import sampling
        monkeypatch.setattr(sampling, "list_runs", lambda pid: runs)

        result = compute_evalset_sampling("p", "e")
        assert result["project_id"] == "p"
        assert result["evalset_id"] == "e"
        assert result["total_runs"] == 3
        cases = result["cases"]
        assert len(cases) == 2

        c1 = next(c for c in cases if c["case_id"] == "c1")
        assert c1["case_name"] == "A"
        assert c1["n"] == 3
        assert c1["c"] == 2  # 2 次通过
        assert c1["pass_rate"] == round(2 / 3, 4)
        # pass^3 (k=3, n=3, c=2) = C(2,3)/C(3,3) = 0/1 = 0
        assert c1["pass_pow_3"] == 0.0
        # pass@3 (k=3, n=3, c=2) = 1 - C(1,3)/C(3,3) = 1 - 0/1 = 1.0
        assert c1["pass_at_3"] == 1.0

        c2 = next(c for c in cases if c["case_id"] == "c2")
        assert c2["n"] == 3
        assert c2["c"] == 3
        assert c2["pass_rate"] == 1.0
        # pass^3 (k=3, n=3, c=3) = C(3,3)/C(3,3) = 1/1 = 1.0
        assert c2["pass_pow_3"] == 1.0

    def test_default_sort_by_pass_pow_3_asc(self, monkeypatch):
        """默认按 pass^3 升序（最不稳的排最上）"""
        runs = []
        # case A: 1/3 通过（最不稳）
        # case B: 3/3 通过（最稳）
        for _ in range(3):
            runs.append(_mk_run("p", "e", [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=False if _ == 0 else True),
                CaseResult(case_name="B", case_id="c2", actual_output="ok", passed=True),
            ]))
        from app import sampling
        monkeypatch.setattr(sampling, "list_runs", lambda pid: runs)

        result = compute_evalset_sampling("p", "e")
        cases = result["cases"]
        # A 的 pass^3 = 0（c=2, n=3 → C(2,3)=0）→ 排最前
        # B 的 pass^3 = 1（c=3, n=3 → C(3,3)/C(3,3)=1）
        assert cases[0]["case_id"] == "c1"
        assert cases[0]["pass_pow_3"] == 0.0
        assert cases[1]["case_id"] == "c2"
        assert cases[1]["pass_pow_3"] == 1.0

    def test_n_lt_3_pass_pow_3_is_none(self, monkeypatch):
        """n < 3 时 pass^3 = None"""
        runs = [_mk_run("p", "e", [
            CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True),
        ])]
        from app import sampling
        monkeypatch.setattr(sampling, "list_runs", lambda pid: runs)

        result = compute_evalset_sampling("p", "e")
        c = result["cases"][0]
        assert c["n"] == 1
        assert c["pass_pow_3"] is None
        assert c["pass_at_3"] is None

    def test_empty_runs(self, monkeypatch):
        """无 completed run → cases 空"""
        from app import sampling
        monkeypatch.setattr(sampling, "list_runs", lambda pid: [])
        result = compute_evalset_sampling("p", "e")
        assert result["cases"] == []
        assert result["total_runs"] == 0

    def test_fallback_case_name_for_old_runs(self, monkeypatch):
        """旧 run（无 case_id）按 case_name 聚合"""
        runs = [_mk_run("p", "e", [
            CaseResult(case_name="legacy-A", actual_output="ok", passed=True),  # 无 case_id
        ])]
        from app import sampling
        monkeypatch.setattr(sampling, "list_runs", lambda pid: runs)

        result = compute_evalset_sampling("p", "e")
        assert len(result["cases"]) == 1
        c = result["cases"][0]
        # case_id 可能是 None（旧 run）
        assert c["case_name"] == "legacy-A"
        assert c["n"] == 1


# ============== T2-1: 导入 merge 同名复用 id ==============

class TestImportMergeIdStability:
    """T2-1: 导入 merge 时同名 case 复用 id"""

    def test_merge_same_name_reuses_id(self, client):
        """同名 case 重导 → id 不变（采样历史连续）"""
        # 创建项目 + 评测集
        resp = client.post("/api/projects", json={"name": "id 稳定测试"})
        pid = resp.json()["id"]
        resp2 = client.post("/api/evalsets", json={"project_id": pid, "name": "集"})
        eid = resp2.json()["id"]

        # 第一次导入：case_name=A, id=a
        c1 = json.dumps([{"id": "a", "case_name": "A", "input": "x", "eval_type": "exact"}])
        r1 = client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=merge",
                         data={"file_content": c1})
        assert r1.status_code == 200

        # 第二次 merge：同名 A 但 id 不同 → 应复用原 id=a
        c2 = json.dumps([{"id": "new-id", "case_name": "A", "input": "y", "eval_type": "exact"}])
        r2 = client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=merge",
                         data={"file_content": c2})
        assert r2.status_code == 200
        cases = r2.json()["evalset"]["cases"]
        assert len(cases) == 1  # 同名不新增，更新内容
        assert cases[0]["id"] == "a"  # id 复用
        assert cases[0]["input"] == "y"  # 内容更新

    def test_merge_different_name_adds_new(self, client):
        """不同名 case → 新增"""
        resp = client.post("/api/projects", json={"name": "新增测试"})
        pid = resp.json()["id"]
        resp2 = client.post("/api/evalsets", json={"project_id": pid, "name": "集"})
        eid = resp2.json()["id"]

        c1 = json.dumps([{"id": "a", "case_name": "A", "input": "x", "eval_type": "exact"}])
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=merge", data={"file_content": c1})

        c2 = json.dumps([{"id": "b", "case_name": "B", "input": "y", "eval_type": "exact"}])
        r2 = client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=merge", data={"file_content": c2})
        cases = r2.json()["evalset"]["cases"]
        assert len(cases) == 2
        names = {c["case_name"] for c in cases}
        assert names == {"A", "B"}


# ============== T2-1: 采样端点 ==============

class TestEvalsetSamplingEndpoint:
    """GET /api/evalsets/{eid}/sampling"""

    def test_endpoint_returns_per_case(self, client, monkeypatch):
        """端点返回每 case 采样指标"""
        # 先建项目 + 评测集
        resp = client.post("/api/projects", json={"name": "端点测试"})
        pid = resp.json()["id"]
        resp2 = client.post("/api/evalsets", json={"project_id": pid, "name": "集"})
        eid = resp2.json()["id"]

        # mock list_runs 返回 3 个 run
        runs = []
        for passed_a in [True, False, True]:
            runs.append(_mk_run(pid, eid, [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=passed_a),
            ]))
        from app import sampling
        monkeypatch.setattr(sampling, "list_runs", lambda p: runs if p == pid else [])

        r = client.get(f"/api/evalsets/{eid}/sampling?project_id={pid}")
        assert r.status_code == 200
        data = r.json()
        assert data["total_runs"] == 3
        assert len(data["cases"]) == 1
        assert data["cases"][0]["case_id"] == "c1"
        assert data["cases"][0]["n"] == 3

    def test_endpoint_404_unknown_evalset(self, client):
        """不存在的评测集 → 404"""
        r = client.get("/api/evalsets/nonexistent/sampling?project_id=any")
        assert r.status_code == 404


# ============== T2-2: Judge 配置复用 ==============

class TestJudgeConfigReuse:
    """T2-2: use_target_config 复用 Target 配置"""

    def test_default_use_target_config_false(self):
        """默认不开启复用"""
        jc = JudgeConfig(base_url="http://judge", api_key="k", model="m")
        assert jc.use_target_config is False

    def test_resolve_judge_config_self(self):
        """未开启复用 → 取 judge 自身配置"""
        from app.runner import _resolve_judge_config
        p = Project(
            id="p1", name="t",
            judge_config=JudgeConfig(base_url="http://judge", api_key="jk", model="jm"),
            target_config=TargetConfig(base_url="http://target", api_key="tk", model="tm"),
        )
        assert _resolve_judge_config(p) == ("http://judge", "jk", "jm")

    def test_resolve_judge_config_reuse_openai(self):
        """开启复用 + target openai_compatible → 取 target 配置"""
        from app.runner import _resolve_judge_config
        p = Project(
            id="p1", name="t",
            judge_config=JudgeConfig(
                base_url="http://judge", api_key="jk", model="jm",
                use_target_config=True,
            ),
            target_config=TargetConfig(
                base_url="http://target", api_key="tk", model="tm",
                api_type="openai_compatible",
            ),
        )
        assert _resolve_judge_config(p) == ("http://target", "tk", "tm")

    def test_resolve_judge_config_reuse_blocked_for_custom_target(self):
        """target 为 custom 时，即使 use_target_config=True 也不复用（fallback judge 自身）"""
        from app.runner import _resolve_judge_config
        p = Project(
            id="p1", name="t",
            judge_config=JudgeConfig(
                base_url="http://judge", api_key="jk", model="jm",
                use_target_config=True,
                api_type="custom", request_template="tpl",
            ),
            target_config=TargetConfig(
                base_url="http://target", api_key="tk", model=None,
                api_type="custom", request_template="{input}",
            ),
        )
        # 防御：runner 不应直接复用 custom target（实际由 routes 阻断保存）
        assert _resolve_judge_config(p) == ("http://judge", "jk", "jm")


class TestJudgeConfigReuseValidation:
    """T2-2: PUT /projects 校验 use_target_config 仅 openai_compatible 可用"""

    def _base_project_body(self, target_api_type="openai_compatible",
                           use_target_config=False, judge_api_type="openai_compatible"):
        return {
            "id": "p-reuse",
            "name": "复用测试",
            "task_shape": "general",
            "judge_config": {
                "api_type": judge_api_type,
                "base_url": "http://judge",
                "api_key": "jk",
                "model": "jm" if judge_api_type == "openai_compatible" else None,
                "request_template": "{input}" if judge_api_type == "custom" else None,
                "response_parsing": (
                    {"output_paths": ["$.score"]} if judge_api_type == "custom" else None
                ),
                "use_target_config": use_target_config,
                "prompt_template": "判断：{requirement} {output}",
            },
            "target_config": {
                "api_type": target_api_type,
                "base_url": "http://target",
                "api_key": "tk",
                "model": "tm" if target_api_type == "openai_compatible" else None,
                "request_template": "{input}" if target_api_type == "custom" else "{input}",
            },
            "token_budget": None,
        }

    def _create_project(self, client, name="复用测试"):
        resp = client.post("/api/projects", json={"name": name})
        pid = resp.json()["id"]
        return pid

    def test_save_with_reuse_on_openai_target_ok(self, client):
        """target=openai_compatible + use_target_config=True → 保存成功"""
        pid = self._create_project(client, "复用-合法")
        body = self._base_project_body(
            target_api_type="openai_compatible", use_target_config=True)
        body["id"] = pid
        r = client.put(f"/api/projects/{pid}", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["judge_config"]["use_target_config"] is True

    def test_save_with_reuse_on_custom_target_blocked(self, client):
        """target=custom + use_target_config=True → 422"""
        pid = self._create_project(client, "复用-非法")
        body = self._base_project_body(
            target_api_type="custom", use_target_config=True)
        body["id"] = pid
        r = client.put(f"/api/projects/{pid}", json=body)
        assert r.status_code == 422
        assert "openai_compatible" in r.json()["detail"]["error"]["message"]

    def test_save_with_reuse_off_any_target_ok(self, client):
        """use_target_config=False + target=custom → 保存成功（不触发复用校验）"""
        pid = self._create_project(client, "复用-关闭")
        body = self._base_project_body(
            target_api_type="custom", use_target_config=False,
            judge_api_type="openai_compatible")
        body["id"] = pid
        r = client.put(f"/api/projects/{pid}", json=body)
        assert r.status_code == 200


# ============== T2-4: token 预算硬限制 ==============

class TestTokenBudgetEnforce:
    """T2-4: execute_run 预算阻断（warn_only=false）"""

    def _mk_project_with_budget(self, limit, warn_only):
        return Project(
            id="proj-budget",
            name="预算测试",
            judge_config=JudgeConfig(base_url="http://j", api_key="k", model="m"),
            target_config=TargetConfig(base_url="http://t", api_key="k", model="m"),
            token_budget=TokenBudget(limit=limit, warn_only=warn_only),
        )

    def _mk_evalset(self, n_cases=4):
        cases = [
            EvalCase(id=f"c-{i}", case_name=f"case-{i}", input="in",
                     eval_type="exact", expected_output="ok")
            for i in range(n_cases)
        ]
        return EvalSet(id="es-budget", project_id="proj-budget", name="集", cases=cases)

    @pytest.mark.asyncio
    async def test_budget_blocks_when_exceeded(self, monkeypatch):
        """超预算 → 剩余 case 标 budget_exceeded，run 状态 completed，summary.budget_exceeded=True"""
        from app import runner
        # mock call_target 每次消耗 100 token，模拟超过 250 限制（4 个 case × 100 = 400）
        async def fake_call_target(**kwargs):
            return ("output", 100, False)
        monkeypatch.setattr(runner, "call_target", fake_call_target)
        # mock judge 检查可用
        async def fake_check_judge(project):
            return (True, "")
        monkeypatch.setattr(runner, "check_judge_available", fake_check_judge)
        # mock save_run（避免写盘）
        monkeypatch.setattr(runner, "save_run", lambda r: None)

        project = self._mk_project_with_budget(limit=250, warn_only=False)
        evalset = self._mk_evalset(n_cases=4)
        run = EvalRun(id="run-budget", project_id="proj-budget", evalset_id="es-budget",
                       created_at=datetime.now(timezone.utc).isoformat())

        result = await runner.execute_run(run, project, evalset, case_filter=None)

        # 第 3 个 case 累计 300 > 250，剩余标 skipped
        assert result.summary.budget_exceeded is True
        skipped = [r for r in result.results if r.skipped_reason == "budget_exceeded"]
        assert len(skipped) == 1  # 只剩最后一个被 skip
        # skipped 不计入 pass_rate 分母
        valid = len(result.results) - len(skipped)
        assert valid == 3

    @pytest.mark.asyncio
    async def test_budget_warn_only_does_not_block(self, monkeypatch):
        """warn_only=true → 不阻断，全部 case 执行"""
        from app import runner
        async def fake_call_target(**kwargs):
            return ("output", 100, False)
        monkeypatch.setattr(runner, "call_target", fake_call_target)
        async def fake_check_judge(project):
            return (True, "")
        monkeypatch.setattr(runner, "check_judge_available", fake_check_judge)
        monkeypatch.setattr(runner, "save_run", lambda r: None)

        project = self._mk_project_with_budget(limit=250, warn_only=True)
        evalset = self._mk_evalset(n_cases=4)
        run = EvalRun(id="run-budget2", project_id="proj-budget", evalset_id="es-budget",
                       created_at=datetime.now(timezone.utc).isoformat())

        result = await runner.execute_run(run, project, evalset, case_filter=None)

        assert result.summary.budget_exceeded is False
        skipped = [r for r in result.results if r.skipped_reason == "budget_exceeded"]
        assert len(skipped) == 0  # 不阻断，全部执行
        assert len(result.results) == 4

    @pytest.mark.asyncio
    async def test_no_budget_no_block(self, monkeypatch):
        """无 token_budget → 不阻断（向后兼容）"""
        from app import runner
        async def fake_call_target(**kwargs):
            return ("output", 100, False)
        monkeypatch.setattr(runner, "call_target", fake_call_target)
        async def fake_check_judge(project):
            return (True, "")
        monkeypatch.setattr(runner, "check_judge_available", fake_check_judge)
        monkeypatch.setattr(runner, "save_run", lambda r: None)

        project = Project(
            id="proj-nobudget", name="无预算",
            judge_config=JudgeConfig(base_url="http://j", api_key="k", model="m"),
            target_config=TargetConfig(base_url="http://t", api_key="k", model="m"),
            token_budget=None,
        )
        evalset = self._mk_evalset(n_cases=3)
        run = EvalRun(id="run-nobudget", project_id="proj-nobudget", evalset_id="es-budget",
                       created_at=datetime.now(timezone.utc).isoformat())

        result = await runner.execute_run(run, project, evalset, case_filter=None)

        assert result.summary.budget_exceeded is False
        assert len(result.results) == 3


# ============== T2-3: 配置模板 ==============

class TestConfigTemplates:
    """T2-3: Target 配置模板（GET/POST/DELETE）"""

    def setup_method(self):
        """每个测试前清空 templates 文件，避免相互污染"""
        from app.storage import CONFIG_TEMPLATES_FILE
        if CONFIG_TEMPLATES_FILE.exists():
            CONFIG_TEMPLATES_FILE.unlink()

    def test_list_empty(self, client):
        """无模板 → 空列表"""
        r = client.get("/api/config-templates")
        assert r.status_code == 200
        assert r.json() == {"templates": []}

    def test_save_template_from_project(self, client):
        """从项目保存 target_config 为模板"""
        # 先建项目并配置 target
        pid = client.post("/api/projects", json={"name": "模板源"}).json()["id"]
        body = {
            "id": pid, "name": "模板源", "task_shape": "general",
            "judge_config": {"base_url": "http://j", "api_key": "jk", "model": "jm"},
            "target_config": {
                "api_type": "openai_compatible",
                "base_url": "http://target-x",
                "api_key": "secret-key",
                "model": "gpt-test",
                "request_template": "{input}",
            },
            "token_budget": None,
        }
        client.put(f"/api/projects/{pid}", json=body)

        # 保存模板
        r = client.post(f"/api/config-templates?project_id={pid}",
                        data={"name": "OpenAI 模板"})
        assert r.status_code == 201
        tpl = r.json()
        assert tpl["name"] == "OpenAI 模板"
        assert tpl["id"].startswith("tpl-")
        # api_key 应被 masked
        assert tpl["target_config"]["api_key"] == "__MASKED__"

    def test_save_template_secret_preserved_in_storage(self, client):
        """模板存储保留 secret 原值（GET 单条时返回原值供加载）"""
        pid = client.post("/api/projects", json={"name": "源"}).json()["id"]
        body = {
            "id": pid, "name": "源", "task_shape": "general",
            "judge_config": {"base_url": "http://j", "api_key": "jk", "model": "jm"},
            "target_config": {
                "api_type": "openai_compatible",
                "base_url": "http://t",
                "api_key": "the-secret",
                "model": "m",
                "request_template": "{input}",
            },
            "token_budget": None,
        }
        client.put(f"/api/projects/{pid}", json=body)
        r = client.post(f"/api/config-templates?project_id={pid}",
                        data={"name": "Tpl"})
        tpl_id = r.json()["id"]
        # GET 单条返回原值
        r2 = client.get(f"/api/config-templates/{tpl_id}")
        assert r2.status_code == 200
        assert r2.json()["target_config"]["api_key"] == "the-secret"

    def test_list_returns_masked(self, client):
        """列表接口返回 masked secret"""
        pid = client.post("/api/projects", json={"name": "源"}).json()["id"]
        body = {
            "id": pid, "name": "源", "task_shape": "general",
            "judge_config": {"base_url": "http://j", "api_key": "jk", "model": "jm"},
            "target_config": {
                "api_type": "openai_compatible",
                "base_url": "http://t", "api_key": "secret",
                "model": "m", "request_template": "{input}",
            },
            "token_budget": None,
        }
        client.put(f"/api/projects/{pid}", json=body)
        client.post(f"/api/config-templates?project_id={pid}", data={"name": "T1"})

        r = client.get("/api/config-templates")
        templates = r.json()["templates"]
        assert len(templates) == 1
        assert templates[0]["target_config"]["api_key"] == "__MASKED__"

    def test_delete_template(self, client):
        """删除模板后列表不再出现"""
        pid = client.post("/api/projects", json={"name": "源"}).json()["id"]
        body = {
            "id": pid, "name": "源", "task_shape": "general",
            "judge_config": {"base_url": "http://j", "api_key": "jk", "model": "jm"},
            "target_config": {
                "api_type": "openai_compatible",
                "base_url": "http://t", "api_key": "k",
                "model": "m", "request_template": "{input}",
            },
            "token_budget": None,
        }
        client.put(f"/api/projects/{pid}", json=body)
        r = client.post(f"/api/config-templates?project_id={pid}", data={"name": "T-del"})
        tpl_id = r.json()["id"]

        # 删除
        r2 = client.delete(f"/api/config-templates/{tpl_id}")
        assert r2.status_code == 200
        # 列表不再出现
        r3 = client.get("/api/config-templates")
        assert all(t["id"] != tpl_id for t in r3.json()["templates"])

    def test_delete_unknown_template_404(self, client):
        """删除不存在的模板 → 404"""
        r = client.delete("/api/config-templates/nonexistent")
        assert r.status_code == 404

    def test_save_template_unknown_project_404(self, client):
        """从不存在项目保存模板 → 404"""
        r = client.post("/api/config-templates?project_id=unknown",
                        data={"name": "X"})
        assert r.status_code == 404

    def test_save_template_empty_name_422(self, client):
        """模板名为空 → 422"""
        pid = client.post("/api/projects", json={"name": "源"}).json()["id"]
        r = client.post(f"/api/config-templates?project_id={pid}",
                        data={"name": "  "})
        assert r.status_code == 422
