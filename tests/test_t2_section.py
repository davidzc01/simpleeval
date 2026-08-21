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


# ============== T2-2 → REQ-16: Judge 配置独立管理 ==============
# T2-2 (use_target_config) 已废弃，由 REQ-16 全局 Judge 配置管理替代

class TestJudgeConfigIndependence:
    """REQ-16: JudgeConfig 不再有 use_target_config 字段（T2-2 废弃确认）"""

    def test_no_use_target_config_field(self):
        """JudgeConfig 模型不再含 use_target_config（REQ-16 移除）"""
        jc = JudgeConfig(base_url="http://judge", api_key="k", model="m")
        assert not hasattr(jc, "use_target_config")

    def test_resolve_judge_config_from_inline(self):
        """无 judge_config_id → fallback 到内联 judge_config（旧项目兼容）"""
        from app.runner import _resolve_effective_judge_config
        p = Project(
            id="p1", name="t",
            judge_config=JudgeConfig(base_url="http://judge", api_key="jk", model="jm"),
            target_config=TargetConfig(base_url="http://target", api_key="tk", model="tm"),
        )
        jc = _resolve_effective_judge_config(p)
        assert jc.base_url == "http://judge"
        assert jc.api_key == "jk"
        assert jc.model == "jm"

    def test_resolve_judge_config_from_global(self):
        """有 judge_config_id → 从全局 judge-configs.json 取（优先于内联）"""
        from app.runner import _resolve_effective_judge_config
        from app.storage import save_judge_config, _read_judge_configs, JUDGE_CONFIGS_FILE
        # 准备：保存一个全局 Judge 配置
        if JUDGE_CONFIGS_FILE.exists():
            JUDGE_CONFIGS_FILE.unlink()
        jc_global = JudgeConfig(base_url="http://global-judge", api_key="gk", model="gm")
        saved = save_judge_config("全局Judge", jc_global)
        jc_id = saved["id"]
        # 项目内联配置与全局不同，验证取全局
        p = Project(
            id="p1", name="t",
            judge_config=JudgeConfig(base_url="http://inline", api_key="ik", model="im"),
            target_config=TargetConfig(base_url="http://target", api_key="tk", model="tm"),
            judge_config_id=jc_id,
        )
        resolved = _resolve_effective_judge_config(p)
        assert resolved.base_url == "http://global-judge"
        assert resolved.api_key == "gk"
        assert resolved.model == "gm"
        # 清理
        JUDGE_CONFIGS_FILE.unlink()

    def test_resolve_judge_config_global_deleted_fallback_inline(self):
        """judge_config_id 指向的全局配置被删 → fallback 到内联（容错）"""
        from app.runner import _resolve_effective_judge_config
        p = Project(
            id="p1", name="t",
            judge_config=JudgeConfig(base_url="http://inline", api_key="ik", model="im"),
            target_config=TargetConfig(base_url="http://target", api_key="tk", model="tm"),
            judge_config_id="jc-nonexistent",
        )
        resolved = _resolve_effective_judge_config(p)
        assert resolved.base_url == "http://inline"


class TestJudgeConfigCRUDAPI:
    """REQ-16: /api/judge-configs CRUD"""

    def _jc_body(self, api_type="openai_compatible", name="测试Judge"):
        jc = {
            "api_type": api_type,
            "base_url": "http://judge.example.com",
            "api_key": "sk-secret",
            "model": "gpt-4o" if api_type == "openai_compatible" else None,
            "prompt_template": "判断：{requirement} {output}",
        }
        if api_type == "custom":
            jc["request_template"] = '{"q": "{requirement}"}'
            jc["response_parsing"] = {"output_paths": ["$.score"]}
        return jc

    def test_create_judge_config(self, client):
        """POST /judge-configs 创建全局 Judge 配置"""
        import json as _json
        r = client.post("/api/judge-configs", data={
            "name": "DeepSeek-Judge",
            "judge_config_json": _json.dumps(self._jc_body()),
        })
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["name"] == "DeepSeek-Judge"
        assert data["id"].startswith("jc-")
        # api_key 必须 masked
        assert data["judge_config"]["api_key"] == "__MASKED__"

    def test_create_judge_config_empty_name_blocked(self, client):
        """空名称 → 422"""
        import json as _json
        r = client.post("/api/judge-configs", data={
            "name": "  ",
            "judge_config_json": _json.dumps(self._jc_body()),
        })
        assert r.status_code == 422

    def test_create_judge_config_invalid_json_blocked(self, client):
        """judge_config_json 非法 JSON → 422"""
        r = client.post("/api/judge-configs", data={
            "name": "坏配置",
            "judge_config_json": "not-json",
        })
        assert r.status_code == 422

    def test_create_judge_config_custom_missing_response_parsing(self, client):
        """custom 模式缺 response_parsing → 422"""
        import json as _json
        jc = self._jc_body(api_type="custom")
        jc["response_parsing"] = None
        r = client.post("/api/judge-configs", data={
            "name": "坏custom",
            "judge_config_json": _json.dumps(jc),
        })
        assert r.status_code == 422

    def test_list_judge_configs(self, client):
        """GET /judge-configs 列表（masked）"""
        import json as _json
        client.post("/api/judge-configs", data={
            "name": "List-Judge-1",
            "judge_config_json": _json.dumps(self._jc_body()),
        })
        r = client.get("/api/judge-configs")
        assert r.status_code == 200
        data = r.json()
        assert "judge_configs" in data
        # 每条 api_key 都 masked
        for jc in data["judge_configs"]:
            if jc.get("judge_config", {}).get("api_key"):
                assert jc["judge_config"]["api_key"] == "__MASKED__"

    def test_update_judge_config_name_conflict(self, client):
        """改名冲突需 overwrite=true"""
        import json as _json
        r1 = client.post("/api/judge-configs", data={
            "name": "Conflict-A",
            "judge_config_json": _json.dumps(self._jc_body()),
        })
        r2 = client.post("/api/judge-configs", data={
            "name": "Conflict-B",
            "judge_config_json": _json.dumps(self._jc_body()),
        })
        id_b = r2.json()["id"]
        # 把 B 改名为 A → 409
        r = client.put(f"/api/judge-configs/{id_b}", data={
            "name": "Conflict-A",
            "judge_config_json": _json.dumps(self._jc_body()),
            "overwrite": "false",
        })
        assert r.status_code == 409
        # overwrite=true → 200
        r = client.put(f"/api/judge-configs/{id_b}", data={
            "name": "Conflict-A",
            "judge_config_json": _json.dumps(self._jc_body()),
            "overwrite": "true",
        })
        assert r.status_code == 200

    def test_delete_judge_config(self, client):
        """DELETE /judge-configs/{id}"""
        import json as _json
        r = client.post("/api/judge-configs", data={
            "name": "Delete-Me",
            "judge_config_json": _json.dumps(self._jc_body()),
        })
        jc_id = r.json()["id"]
        r = client.delete(f"/api/judge-configs/{jc_id}")
        assert r.status_code == 200
        assert r.json()["deleted"] == jc_id
        # 再删 → 404
        r = client.delete(f"/api/judge-configs/{jc_id}")
        assert r.status_code == 404


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
        # mock async_save_run（避免写盘；BUG-1 根治：runner 用异步落盘）
        async def fake_async_save_run(r):
            return None
        monkeypatch.setattr(runner, "async_save_run", fake_async_save_run)

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
        async def fake_async_save_run(r):
            return None
        monkeypatch.setattr(runner, "async_save_run", fake_async_save_run)

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
        async def fake_async_save_run(r):
            return None
        monkeypatch.setattr(runner, "async_save_run", fake_async_save_run)

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

    @pytest.mark.asyncio
    async def test_concurrent_budget_skips_unstarted_samples(self, monkeypatch):
        """并发路径预算超限 → 未开始的样本标 skipped 不执行 call_target"""
        from app import runner
        call_count = [0]

        async def fake_call_target(**kwargs):
            call_count[0] += 1
            return ("output", 100, False)
        monkeypatch.setattr(runner, "call_target", fake_call_target)
        async def fake_check_judge(project):
            return (True, "")
        monkeypatch.setattr(runner, "check_judge_available", fake_check_judge)
        async def fake_async_save_run(r):
            return None
        monkeypatch.setattr(runner, "async_save_run", fake_async_save_run)

        project = self._mk_project_with_budget(limit=250, warn_only=False)
        project.max_concurrency = 2
        evalset = self._mk_evalset(n_cases=6)
        run = EvalRun(id="run-conc-budget", project_id="proj-budget",
                       evalset_id="es-budget",
                       created_at=datetime.now(timezone.utc).isoformat())

        result = await runner.execute_run(
            run, project, evalset, case_filter=None,
            samples=1, concurrency=2,
        )

        # 预算超限标记
        assert result.summary.budget_exceeded is True
        # 总结果数不丢失
        assert len(result.results) == 6
        # 部分样本被 skip
        skipped = [r for r in result.results
                    if r.skipped_reason == "budget_exceeded"]
        executed = [r for r in result.results
                    if r.skipped_reason != "budget_exceeded"]
        assert len(skipped) >= 1, "应有未开始的样本被标记 skipped"
        # skipped 样本的字段正确
        for r in skipped:
            assert r.actual_output == "[SKIPPED] budget_exceeded"
            assert r.passed is False
        # 被 skip 的样本不应调用 call_target
        assert call_count[0] == len(executed), \
            f"call_target 应只被已执行样本调用：{call_count[0]} != {len(executed)}"


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
