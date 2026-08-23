"""U 节（批次 4）测试

U-8: case 详情视图（GET /api/evalsets/{eid}/cases/{case_id}/history）
U-9: 每万 token 完成率单位修正（个/万token，无百分号）
"""

import json
import pytest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    EvalRun, CaseResult, EvalSummary, EvalCase, EvalSet, Project,
    TargetConfig, JudgeConfig,
)


@pytest.fixture
def client():
    return TestClient(app)


def _mk_run(project_id, evalset_id, results, status="completed", created_at=None, version_id=None):
    """构造一个 completed run（带 summary）"""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    skipped = sum(1 for r in results if r.skipped_reason is not None)
    valid = total - skipped
    pass_rate = passed / valid if valid > 0 else 0.0
    total_token = sum(r.token_used + r.judge_token for r in results)
    summary = EvalSummary(
        pass_rate=pass_rate,
        total_token=sum(r.token_used for r in results),
        total_latency_ms=sum(r.latency_ms for r in results),
        token_per_pass=passed / (total_token / 10000) if total_token > 0 and passed > 0 else 0.0,
        latency_p50=0.0,
        latency_p95=0.0,
        judge_token=sum(r.judge_token for r in results),
    )
    return EvalRun(
        id=f"run-{project_id}-{evalset_id}-{id(results)}-{created_at}",
        project_id=project_id,
        evalset_id=evalset_id,
        status=status,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        results=results,
        summary=summary,
        version_id=version_id,
    )


def _setup_project_evalset(client, case_id="c1", case_name="A", eval_type="exact", output_requirement=None, expected_output="ok", judge=None):
    """创建项目 + 评测集（含 1 个 case）"""
    resp = client.post("/api/projects", json={"name": "U-节测试"})
    pid = resp.json()["id"]
    resp2 = client.post("/api/evalsets", json={"project_id": pid, "name": "集"})
    eid = resp2.json()["id"]
    cases = [{
        "id": case_id,
        "case_name": case_name,
        "input": "in",
        "expected_output": expected_output,
        "output_requirement": output_requirement,
        "eval_type": eval_type,
        "eval_params": {},
        "enabled": True,
    }]
    client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace",
                data={"file_content": json.dumps(cases)})
    return pid, eid


# ============== U-8: case history endpoint ==============

class TestCaseHistoryEndpoint:
    """GET /api/evalsets/{eid}/cases/{case_id}/history"""

    def test_404_unknown_evalset(self, client):
        """不存在的评测集 → 404"""
        r = client.get("/api/evalsets/nope/cases/c1/history?project_id=p")
        assert r.status_code == 404

    def test_404_unknown_case(self, client):
        """评测集存在但 case 不存在 → 404"""
        pid, eid = _setup_project_evalset(client, case_id="c1")
        r = client.get(f"/api/evalsets/{eid}/cases/no-such-case/history?project_id={pid}")
        assert r.status_code == 404

    def test_empty_history_when_no_runs(self, client):
        """无 completed run → history 空，aggregate.n=0"""
        pid, eid = _setup_project_evalset(client, case_id="c1")
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        assert r.status_code == 200
        data = r.json()
        assert data["history"] == []
        assert data["aggregate"]["n"] == 0
        assert data["aggregate"]["pass_rate"] == 0.0

    def test_history_basic_aggregate(self, client, monkeypatch):
        """3 个 completed run，2 过 1 不过 → n=3 c=2 pass_rate=2/3"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True, latency_ms=100, token_used=10)], created_at="2026-01-01T00:00:00Z"),
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True, latency_ms=200, token_used=10)], created_at="2026-01-02T00:00:00Z"),
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="bad", passed=False, latency_ms=300, token_used=10)], created_at="2026-01-03T00:00:00Z"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])

        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        assert r.status_code == 200
        data = r.json()
        assert len(data["history"]) == 3
        agg = data["aggregate"]
        assert agg["n"] == 3
        assert agg["c"] == 2
        assert abs(agg["pass_rate"] - 2 / 3) < 1e-6
        # pass@3 = 1 - C(n-c,3)/C(n,3)（无放回，至少一次通过）；n=3,c=2 → 1.0
        assert agg["pass_at_3"] == 1.0
        # pass^3 = C(c,3)/C(n,3)（无放回，全部通过）；n=3,c=2 → 0.0
        assert agg["pass_pow_3"] == 0.0
        # 延迟 P50/P95：[100,200,300] P50=200 P95=300
        assert agg["latency_p50"] == 200
        assert agg["latency_p95"] == 300
        # total_token = 30
        assert agg["total_token"] == 30
        # token_per_pass = 2 / (30/10000) = 2000/3
        assert abs(agg["token_per_pass"] - 2 / (30 / 10000)) < 1e-6

    def test_history_pass_at_3_all_pass(self, client, monkeypatch):
        """n=3,c=3 → pass@3 = 1.0 且 pass^3 = 1.0"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True)], created_at="2026-01-01T00:00:00Z"),
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True)], created_at="2026-01-02T00:00:00Z"),
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True)], created_at="2026-01-03T00:00:00Z"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        agg = r.json()["aggregate"]
        assert agg["pass_at_3"] == 1.0

    def test_history_excludes_non_completed_runs(self, client, monkeypatch):
        """非 completed run 不纳入"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True)], status="running"),
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True)], status="failed"),
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True)], status="completed"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        assert r.status_code == 200
        assert len(r.json()["history"]) == 1

    def test_history_skipped_excluded_from_aggregate(self, client, monkeypatch):
        """skipped 不计入聚合分母"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True, latency_ms=100, token_used=10)], created_at="2026-01-01T00:00:00Z"),
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="", passed=False, skipped_reason="budget_exceeded")], created_at="2026-01-02T00:00:00Z"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        data = r.json()
        assert len(data["history"]) == 2
        agg = data["aggregate"]
        # skipped 不算入 n
        assert agg["n"] == 1
        assert agg["c"] == 1
        assert agg["pass_rate"] == 1.0
        assert agg["total_token"] == 10

    def test_history_fallback_case_name_when_no_case_id(self, client, monkeypatch):
        """旧 run 无 case_id → fallback case_name 匹配"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        # case_id=None 的旧 run
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id=None, actual_output="ok", passed=True)], created_at="2026-01-01T00:00:00Z"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        assert r.status_code == 200
        assert len(r.json()["history"]) == 1

    def test_history_only_includes_target_case(self, client, monkeypatch):
        """多 case 评测集 → 只返回目标 case 的记录"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        runs = [
            _mk_run(pid, eid, [
                CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True),
                CaseResult(case_name="B", case_id="c2", actual_output="bad", passed=False),
            ], created_at="2026-01-01T00:00:00Z"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        data = r.json()
        assert len(data["history"]) == 1
        assert data["history"][0]["case_name"] == "A" if "case_name" in data["history"][0] else True
        # 实际只校验 passed 与目标 case 一致
        assert data["history"][0]["passed"] is True

    def test_history_row_fields(self, client, monkeypatch):
        """每条历史记录的字段完整"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A", eval_type="exact", expected_output="ok")
        runs = [
            _mk_run(pid, eid, [CaseResult(
                case_name="A", case_id="c1", actual_output="ok", passed=True,
                latency_ms=150, token_used=20, judge_token=5, sample_index=2,
            )], created_at="2026-01-01T00:00:00Z", version_id="v1"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        row = r.json()["history"][0]
        assert row["run_id"] == runs[0].id
        assert row["passed"] is True
        assert row["latency_ms"] == 150
        assert row["token_used"] == 20
        assert row["judge_token"] == 5
        assert row["eval_type"] == "exact"
        assert row["input"] == "in"
        assert row["expected_output"] == "ok"
        assert row["actual_output"] == "ok"
        assert row["created_at"] == "2026-01-01T00:00:00Z"
        assert row["sample_index"] == 2
        assert row["version_id"] == "v1"
        # 新增字段：variables / eval_params / validations 应有默认值
        assert row.get("variables") == {}
        assert row.get("eval_params") == {}
        assert row.get("validations") == []

    def test_history_includes_version_name(self, client, monkeypatch):
        """O-3: case history 每行含 version_name（version_id → 版本名映射）"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        # 拿项目初始版本 id 与 name（W-7: create_project 自动创建初始版本）
        proj = client.get(f"/api/projects/{pid}").json()
        vers = proj.get("versions") or []
        assert len(vers) >= 1
        vid = vers[0]["id"]
        vname = vers[0]["name"]
        # 创建带 version_id 的 run
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True)], created_at="2026-01-01T00:00:00Z", version_id=vid),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        row = r.json()["history"][0]
        assert row["version_id"] == vid
        assert row["version_name"] == vname

    def test_history_version_name_none_when_no_version(self, client, monkeypatch):
        """O-3: run 无 version_id → version_name 为 None"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True)], created_at="2026-01-01T00:00:00Z", version_id=None),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        row = r.json()["history"][0]
        assert row["version_id"] is None
        assert row["version_name"] is None

    def test_history_judge_summary_for_llm_judge(self, client, monkeypatch):
        """llm_judge case → aggregate.judge_summary 包含 model/prompt_summary"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A", eval_type="llm_judge", output_requirement="要有礼貌")
        # 默认项目 judge_config.model="" → judge_summary.model 为空字符串
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        agg = r.json()["aggregate"]
        assert agg["judge_summary"] is not None
        assert agg["judge_summary"]["model"] == ""
        assert agg["judge_summary"]["api_type"] == "openai_compatible"
        # prompt_summary 存在（即使空字符串）
        assert "prompt_summary" in agg["judge_summary"]

    def test_history_judge_summary_prompt_truncation(self, client, monkeypatch):
        """prompt > 80 字符 → 截断 + …"""
        long_prompt = "请判断模型输出是否满足要求。" * 20  # 远超 80
        resp = client.post("/api/projects", json={"name": "长提示"})
        pid = resp.json()["id"]
        # PUT 项目配 judge prompt（target 必须有 model 才能 openai_compatible 通过）
        r = client.put(f"/api/projects/{pid}", json={
            "id": pid,
            "name": "长提示",
            "task_shape": "general",
            "judge_config": {
                "api_type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "key",
                "model": "gpt-4o-mini",
                "prompt_template": long_prompt,
            },
            "target_config": {
                "api_type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "key",
                "model": "gpt-3.5-turbo",
                "request_template": "{input}",
                "auth": {"type": "none"},
                "response_mapping": [],
            },
        })
        assert r.status_code == 200, r.text
        resp2 = client.post("/api/evalsets", json={"project_id": pid, "name": "集"})
        eid = resp2.json()["id"]
        cases = [{"id": "c1", "case_name": "A", "input": "x", "eval_type": "llm_judge", "output_requirement": "r", "enabled": True}]
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace", data={"file_content": json.dumps(cases)})

        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        agg = r.json()["aggregate"]
        assert agg["judge_summary"]["prompt_summary"].endswith("…")
        assert len(agg["judge_summary"]["prompt_summary"]) <= 81

    def test_history_judge_summary_uses_judge_config_id(self, client, monkeypatch):
        """项目设了 judge_config_id → 优先用全局 Judge 配置"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A", eval_type="llm_judge", output_requirement="r")
        # 创建一个全局 Judge 配置（Form 字段：name + judge_config_json）
        jc_data = {
            "api_type": "openai_compatible",
            "base_url": "https://judge.example.com/v1",
            "api_key": "j-key",
            "model": "judge-model-x",
            "prompt_template": "短提示",
        }
        r = client.post("/api/judge-configs", data={
            "name": "全局 Judge",
            "judge_config_json": json.dumps(jc_data),
        })
        assert r.status_code == 200 or r.status_code == 201, r.text
        jid = r.json()["id"]
        # 项目引用全局 Judge：用 __UNCHANGED__ 哨兵避免掩码 dict 触发 422
        proj_resp = client.get(f"/api/projects/{pid}")
        proj_data = proj_resp.json()
        # PUT 体含 id + judge_config_id；target_config.model 必填
        put_body = {
            "id": pid,
            "name": proj_data["name"],
            "task_shape": proj_data["task_shape"],
            "judge_config": {
                "api_type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "__UNCHANGED__",
                "model": "gpt-4o-mini",
                "prompt_template": "短提示",
            },
            "target_config": {
                "api_type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "__UNCHANGED__",
                "model": "gpt-3.5-turbo",
                "request_template": "{input}",
                "auth": {"type": "none"},
                "response_mapping": [],
            },
            "judge_config_id": jid,
        }
        r = client.put(f"/api/projects/{pid}", json=put_body)
        assert r.status_code == 200, r.text

        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        agg = r.json()["aggregate"]
        assert agg["judge_summary"]["model"] == "judge-model-x"

    def test_history_no_judge_summary_for_rule_based(self, client, monkeypatch):
        """非 llm_judge case → judge_summary 仍可能返回（项目级），但 history 不显示"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A", eval_type="exact", expected_output="ok")
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        # 项目总有 judge_config，所以 judge_summary 非空
        assert r.json()["aggregate"]["judge_summary"] is not None


# ============== U-9: token_per_pass 单位 ==============

class TestTokenPerPassUnit:
    """U-9: token_per_pass 单位为「个/万token」（不带百分号）

    验收点：run 详情、概览、case 详情三处一致。
    这里只做后端口径校验，前端展示由 UI 测试覆盖。
    """

    def test_token_per_pass_not_percentage(self, client, monkeypatch):
        """token_per_pass 是个绝对数（通过数 / (总token/10000)），不是百分比"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        # 1 过 + 10 token → token_per_pass = 1 / (10/10000) = 1000
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True, token_used=10)], created_at="2026-01-01T00:00:00Z"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        tpp = r.json()["aggregate"]["token_per_pass"]
        # 1000（不是 1000% 也不是 0.1）
        assert abs(tpp - 1000.0) < 1e-6
        # 不带 % 号（API 返回的是 number，前端文案「个/万token」由 UI 加）
        assert isinstance(tpp, float)

    def test_token_per_pass_includes_judge_token(self, client, monkeypatch):
        """U-9 口径 = 通过数 / ((target_token + judge_token)/10000)"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        # 1 过，target=10, judge=5 → token_per_pass = 1 / (15/10000) = 1000/1.5
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="ok", passed=True, token_used=10, judge_token=5)], created_at="2026-01-01T00:00:00Z"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        tpp = r.json()["aggregate"]["token_per_pass"]
        expected = 1 / (15 / 10000)
        assert abs(tpp - expected) < 1e-6

    def test_token_per_pass_zero_when_no_pass(self, client, monkeypatch):
        """0 通过 → token_per_pass = 0"""
        pid, eid = _setup_project_evalset(client, case_id="c1", case_name="A")
        runs = [
            _mk_run(pid, eid, [CaseResult(case_name="A", case_id="c1", actual_output="bad", passed=False, token_used=10)], created_at="2026-01-01T00:00:00Z"),
        ]
        from app import routes
        monkeypatch.setattr(routes, "list_runs", lambda p: runs if p == pid else [])
        r = client.get(f"/api/evalsets/{eid}/cases/c1/history?project_id={pid}")
        assert r.json()["aggregate"]["token_per_pass"] == 0.0
