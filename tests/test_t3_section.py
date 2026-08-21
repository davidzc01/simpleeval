"""批次 3 测试：T3-1 / T3-2 / T3-5

T3-1: REQ-13 批量跑 k 次 + 并发基础版
T3-2: REQ-14 评测集变更后采样重置
T3-5: case 级并发推广（T3-1 机制复用）
"""

import asyncio
import time
import json
import logging
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from app.main import app
from fastapi.testclient import TestClient

from app.models import (
    EvalRun, CaseResult, EvalSummary, EvalCase, EvalSet, Project,
    TargetConfig, JudgeConfig, AuthConfig, ProjectVersion, CreateVersionRequest,
    ScheduleConfig,
)
from app.sampling import (
    _aggregate_runs,
    _aggregate_runs_by_case_id,
    _filter_runs_by_content_updated_at,
    compute_evalset_sampling,
    compute_project_sampling,
)
from app.runner import execute_run, _build_run_result, _execute_one_sample
from app.routes import _resolve_version_id
from app.scheduler import (
    cron_match, _parse_cron_field, detect_regression,
    get_regression_alerts, check_and_trigger_scheduled_runs,
    _pending_scheduled_tasks, _on_scheduled_task_done,
)


@pytest.fixture
def client():
    return TestClient(app)


def _mk_project(max_concurrency: int = 1) -> Project:
    return Project(
        id="proj-t3",
        name="T3 项目",
        task_shape="general",
        judge_config=JudgeConfig(base_url="http://judge", api_key="sk-j", model="gpt-4"),
        target_config=TargetConfig(base_url="http://target", api_key="sk-t", model="gpt-4"),
        max_concurrency=max_concurrency,
    )


def _mk_case(name: str = "case-a", content: str = "hello") -> EvalCase:
    return EvalCase(
        id=f"c-{name}",
        case_name=name,
        input=content,
        expected_output=content,
        eval_type="exact",
    )


def _mk_evalset(cases: list[EvalCase] = None, content_updated_at: str = None) -> EvalSet:
    return EvalSet(
        id="es-1",
        project_id="proj-t3",
        name="T3 评测集",
        cases=cases or [_mk_case()],
        content_updated_at=content_updated_at,
    )


def _mk_run(created_at: str, results: list[CaseResult], status: str = "completed") -> EvalRun:
    return EvalRun(
        id=f"run-{created_at}",
        project_id="proj-t3",
        evalset_id="es-1",
        status=status,
        created_at=created_at,
        results=results,
        summary=EvalSummary(
            pass_rate=0.0,
            total_token=0,
            total_latency_ms=0.0,
            token_per_pass=0.0,
            latency_p50=0.0,
            latency_p95=0.0,
        ),
    )


# ============== T3-1: 模型字段 ==============

class TestT31ModelFields:
    """T3-1: 模型新增字段默认值与向后兼容"""

    def test_project_max_concurrency_default_1(self):
        """旧项目无 max_concurrency → 默认 1（串行）"""
        p = Project(
            id="p1", name="p",
            judge_config=JudgeConfig(base_url="", api_key="", model="m"),
            target_config=TargetConfig(base_url="", api_key="", model="m"),
        )
        assert p.max_concurrency == 1

    def test_project_max_concurrency_set(self):
        p = _mk_project(max_concurrency=4)
        assert p.max_concurrency == 4

    def test_run_eval_request_samples_default_1(self):
        from app.models import RunEvalRequest
        r = RunEvalRequest(project_id="p1", evalset_id="e1")
        assert r.samples == 1
        assert r.concurrency is None

    def test_case_result_sample_index_default_none(self):
        """旧 CaseResult 无 sample_index → None（向后兼容）"""
        r = CaseResult(case_name="A", actual_output="ok", passed=True)
        assert r.sample_index is None

    def test_eval_summary_concurrency_default_1(self):
        """旧 EvalSummary 无 concurrency → 1（串行）"""
        s = EvalSummary(
            pass_rate=0.5, total_token=0, total_latency_ms=0.0,
            token_per_pass=0.0, latency_p50=0.0, latency_p95=0.0,
        )
        assert s.concurrency == 1


# ============== T3-1: POST /runs 校验 ==============

class TestT31RunValidation:
    """T3-1: POST /api/runs 校验 samples ≥ 1 + concurrency ≤ max_concurrency"""

    def test_samples_lt_1_returns_422(self, client, monkeypatch):
        """samples=0 应被 422 拒绝"""
        # 路由层把 storage 函数导入为本地符号，要 patch app.routes.* 才生效
        monkeypatch.setattr("app.routes.get_project", lambda pid: _mk_project(max_concurrency=4))
        monkeypatch.setattr("app.routes.get_evalset", lambda eid, pid: _mk_evalset([_mk_case()]))
        monkeypatch.setattr("app.routes.save_run", lambda r: None)
        resp = client.post("/api/runs", json={
            "project_id": "proj-t3", "evalset_id": "es-1", "samples": 0,
        })
        assert resp.status_code == 422
        assert "samples" in resp.json()["detail"]["error"]["message"]

    def test_concurrency_exceeds_max_returns_422(self, client, monkeypatch):
        """concurrency > project.max_concurrency 应被 422 拒绝"""
        monkeypatch.setattr("app.routes.get_project", lambda pid: _mk_project(max_concurrency=2))
        monkeypatch.setattr("app.routes.get_evalset", lambda eid, pid: _mk_evalset([_mk_case()]))
        monkeypatch.setattr("app.routes.save_run", lambda r: None)
        resp = client.post("/api/runs", json={
            "project_id": "proj-t3", "evalset_id": "es-1",
            "samples": 1, "concurrency": 5,
        })
        assert resp.status_code == 422
        msg = resp.json()["detail"]["error"]["message"]
        assert "5" in msg and "2" in msg

    def test_concurrency_lt_1_returns_422(self, client, monkeypatch):
        monkeypatch.setattr("app.routes.get_project", lambda pid: _mk_project(max_concurrency=4))
        monkeypatch.setattr("app.routes.get_evalset", lambda eid, pid: _mk_evalset([_mk_case()]))
        monkeypatch.setattr("app.routes.save_run", lambda r: None)
        resp = client.post("/api/runs", json={
            "project_id": "proj-t3", "evalset_id": "es-1",
            "samples": 1, "concurrency": 0,
        })
        assert resp.status_code == 422

    def test_concurrency_within_limit_accepted(self, client, monkeypatch):
        """concurrency ≤ max_concurrency 应接受（201 创建）"""
        monkeypatch.setattr("app.routes.get_project", lambda pid: _mk_project(max_concurrency=4))
        monkeypatch.setattr("app.routes.get_evalset", lambda eid, pid: _mk_evalset([_mk_case()]))
        monkeypatch.setattr("app.routes.save_run", lambda r: None)
        # 不真的执行 background task（TestClient 同步执行；execute_run 会被调用但 mock 掉）
        monkeypatch.setattr("app.routes.execute_run", AsyncMock(return_value=None))
        resp = client.post("/api/runs", json={
            "project_id": "proj-t3", "evalset_id": "es-1",
            "samples": 1, "concurrency": 3,
        })
        assert resp.status_code == 201


# ============== T3-1: execute_run 串行路径（samples>1） ==============

class TestT31SerialSamples:
    """T3-1 串行路径：samples=k 时每 case 产 k 条结果，sample_index 1..k"""

    @pytest.mark.asyncio
    async def test_samples_k_produces_k_results_with_sample_index(self):
        """samples=3 单 case → 3 条结果，sample_index 分别 1/2/3"""
        project = _mk_project()
        evalset = _mk_evalset([_mk_case("a", "hello")])
        run = EvalRun(
            id="run-test", project_id="proj-t3", evalset_id="es-1",
            status="queued", created_at="2026-08-21T10:00:00Z",
        )

        async def fake_call_target(**kwargs):
            return ("hello", 5, False)

        async def fake_judge_check(*args, **kwargs):
            return True, ""

        async def fake_save_run(r):
            return None

        with patch("app.runner._call_target_with_hard_timeout", side_effect=fake_call_target), \
             patch("app.runner.check_judge_available", new=AsyncMock(return_value=(True, ""))), \
             patch("app.runner.async_save_run", side_effect=fake_save_run):
            completed = await execute_run(run, project, evalset, samples=3, concurrency=None)

        assert completed.status == "completed"
        assert len(completed.results) == 3
        # sample_index 必须为 1, 2, 3
        indexes = sorted(r.sample_index for r in completed.results)
        assert indexes == [1, 2, 3]
        # 同名 case
        assert all(r.case_name == "a" for r in completed.results)
        # summary.concurrency = 1（串行）
        assert completed.summary.concurrency == 1

    @pytest.mark.asyncio
    async def test_samples_1_default_behavior_unchanged(self):
        """samples=1 + concurrency=None → sample_index 全为 None，行为同 T3-1 之前"""
        project = _mk_project()
        evalset = _mk_evalset([_mk_case("a"), _mk_case("b", "world")])
        run = EvalRun(
            id="run-test2", project_id="proj-t3", evalset_id="es-1",
            status="queued", created_at="2026-08-21T10:00:00Z",
        )

        async def fake_call_target(**kwargs):
            return (kwargs.get("prompt", ""), 1, False)

        with patch("app.runner._call_target_with_hard_timeout", side_effect=fake_call_target), \
             patch("app.runner.check_judge_available", new=AsyncMock(return_value=(True, ""))), \
             patch("app.runner.async_save_run", side_effect=AsyncMock(return_value=None)):
            completed = await execute_run(run, project, evalset)

        assert len(completed.results) == 2
        assert all(r.sample_index is None for r in completed.results)
        assert completed.summary.concurrency == 1
        # case 名按提交顺序
        assert [r.case_name for r in completed.results] == ["a", "b"]


# ============== T3-1: execute_run 并发路径 ==============

class TestT31ConcurrentSamples:
    """T3-1 并发路径：concurrency=N 时用 Semaphore + asyncio.Lock"""

    @pytest.mark.asyncio
    async def test_concurrent_k_samples_complete_with_correct_indexes(self):
        """k=3 concurrency=2 单 case → 3 条结果，sample_index ∈ {1,2,3}"""
        project = _mk_project(max_concurrency=4)
        evalset = _mk_evalset([_mk_case("a", "hello")])
        run = EvalRun(
            id="run-conc1", project_id="proj-t3", evalset_id="es-1",
            status="queued", created_at="2026-08-21T10:00:00Z",
        )

        async def fake_call_target(**kwargs):
            await asyncio.sleep(0.01)  # 模拟网络延迟
            return ("hello", 5, False)

        with patch("app.runner._call_target_with_hard_timeout", side_effect=fake_call_target), \
             patch("app.runner.check_judge_available", new=AsyncMock(return_value=(True, ""))), \
             patch("app.runner.async_save_run", side_effect=AsyncMock(return_value=None)):
            completed = await execute_run(run, project, evalset, samples=3, concurrency=2)

        assert len(completed.results) == 3
        indexes = sorted(r.sample_index for r in completed.results)
        assert indexes == [1, 2, 3]
        assert completed.summary.concurrency == 2

    @pytest.mark.asyncio
    async def test_concurrent_runs_faster_than_serial(self):
        """T3-5: samples=1 concurrency=4 跑 8 case，应显著快于串行"""
        project = _mk_project(max_concurrency=8)
        cases = [_mk_case(f"c{i}", f"input{i}") for i in range(8)]
        evalset = _mk_evalset(cases)

        async def slow_call(**kwargs):
            await asyncio.sleep(0.1)
            return ("out", 1, False)

        # 串行
        run_serial = EvalRun(
            id="run-serial", project_id="proj-t3", evalset_id="es-1",
            status="queued", created_at="2026-08-21T10:00:00Z",
        )
        with patch("app.runner._call_target_with_hard_timeout", side_effect=slow_call), \
             patch("app.runner.check_judge_available", new=AsyncMock(return_value=(True, ""))), \
             patch("app.runner.async_save_run", side_effect=AsyncMock(return_value=None)):
            t0 = time.perf_counter()
            await execute_run(run_serial, project, evalset, samples=1, concurrency=None)
            serial_elapsed = time.perf_counter() - t0

        # 并发
        run_conc = EvalRun(
            id="run-conc", project_id="proj-t3", evalset_id="es-1",
            status="queued", created_at="2026-08-21T10:00:00Z",
        )
        with patch("app.runner._call_target_with_hard_timeout", side_effect=slow_call), \
             patch("app.runner.check_judge_available", new=AsyncMock(return_value=(True, ""))), \
             patch("app.runner.async_save_run", side_effect=AsyncMock(return_value=None)):
            t0 = time.perf_counter()
            await execute_run(run_conc, project, evalset, samples=1, concurrency=4)
            conc_elapsed = time.perf_counter() - t0

        # 串行 ≈ 8 * 0.1 = 0.8s；并发 ≈ 8/4 * 0.1 = 0.2s
        # 断言并发明显快（至少快 2x）
        assert conc_elapsed < serial_elapsed / 2, (
            f"并发 {conc_elapsed:.3f}s 应明显快于串行 {serial_elapsed:.3f}s 的一半"
        )

    @pytest.mark.asyncio
    async def test_concurrent_save_no_write_contention(self):
        """T3-1: 并发落盘加 asyncio.Lock，重复跑 3 次无异常"""
        project = _mk_project(max_concurrency=4)
        evalset = _mk_evalset([_mk_case("a"), _mk_case("b"), _mk_case("c")])

        save_calls = []

        async def tracked_save(r):
            save_calls.append(r.id)
            await asyncio.sleep(0)
            return None

        for i in range(3):
            run = EvalRun(
                id=f"run-repeat-{i}", project_id="proj-t3", evalset_id="es-1",
                status="queued", created_at="2026-08-21T10:00:00Z",
            )
            with patch("app.runner._call_target_with_hard_timeout",
                        new=AsyncMock(return_value=("out", 1, False))), \
                 patch("app.runner.check_judge_available",
                       new=AsyncMock(return_value=(True, ""))), \
                 patch("app.runner.async_save_run", side_effect=tracked_save):
                completed = await execute_run(run, project, evalset, samples=2, concurrency=3)
            assert completed.status == "completed"
            assert len(completed.results) == 6  # 3 case * 2 samples

        assert len(save_calls) >= 9  # 每次至少 1 次 running + 1 次 completed（实际更多）

    @pytest.mark.asyncio
    async def test_concurrent_results_match_serial(self):
        """T3-5: 相同输入下，并发与串行产出结果集一致（case 名匹配）"""
        project = _mk_project(max_concurrency=4)
        cases = [_mk_case(f"c{i}", f"input{i}") for i in range(6)]
        evalset = _mk_evalset(cases)

        async def fake_call(**kwargs):
            return (kwargs.get("prompt", ""), 1, False)

        # 串行
        run_serial = EvalRun(
            id="run-s", project_id="proj-t3", evalset_id="es-1",
            status="queued", created_at="2026-08-21T10:00:00Z",
        )
        with patch("app.runner._call_target_with_hard_timeout", side_effect=fake_call), \
             patch("app.runner.check_judge_available",
                   new=AsyncMock(return_value=(True, ""))), \
             patch("app.runner.async_save_run", side_effect=AsyncMock(return_value=None)):
            serial = await execute_run(run_serial, project, evalset, samples=1, concurrency=None)

        # 并发
        run_conc = EvalRun(
            id="run-c", project_id="proj-t3", evalset_id="es-1",
            status="queued", created_at="2026-08-21T10:00:00Z",
        )
        with patch("app.runner._call_target_with_hard_timeout", side_effect=fake_call), \
             patch("app.runner.check_judge_available",
                   new=AsyncMock(return_value=(True, ""))), \
             patch("app.runner.async_save_run", side_effect=AsyncMock(return_value=None)):
            conc = await execute_run(run_conc, project, evalset, samples=1, concurrency=3)

        # 集合相等（顺序可能不同——并发不保证提交顺序；但本次实现保持提交顺序）
        serial_names = sorted(r.case_name for r in serial.results)
        conc_names = sorted(r.case_name for r in conc.results)
        assert serial_names == conc_names
        # 全部 passed（exact 匹配 input==input）
        assert all(r.passed for r in serial.results)
        assert all(r.passed for r in conc.results)


# ============== T3-2: EvalSet.content_updated_at ==============

class TestT32ContentUpdatedAt:
    """T3-2: EvalSet.content_updated_at 字段 + 采样过滤"""

    def test_evalset_content_updated_at_default_none(self):
        """旧 EvalSet 无 content_updated_at → None（全部纳入，现状行为）"""
        e = EvalSet(id="e1", project_id="p1", name="n", cases=[])
        assert e.content_updated_at is None

    def test_filter_runs_when_content_updated_at_set(self):
        """content_updated_at 设定后，更早的 run 被过滤"""
        old_run = _mk_run("2026-08-20T10:00:00Z", [
            CaseResult(case_name="a", actual_output="ok", passed=True)])
        new_run = _mk_run("2026-08-22T10:00:00Z", [
            CaseResult(case_name="a", actual_output="ok", passed=True)])
        cutoff = "2026-08-21T00:00:00Z"
        filtered = _filter_runs_by_content_updated_at([old_run, new_run], cutoff)
        assert len(filtered) == 1
        assert filtered[0].id == "run-2026-08-22T10:00:00Z"

    def test_filter_returns_all_when_content_updated_at_none(self):
        """content_updated_at 为 None → 全部纳入（向后兼容）"""
        runs = [
            _mk_run("2026-01-01T00:00:00Z", [CaseResult(case_name="a", actual_output="ok", passed=True)]),
            _mk_run("2026-08-21T00:00:00Z", [CaseResult(case_name="a", actual_output="ok", passed=True)]),
        ]
        filtered = _filter_runs_by_content_updated_at(runs, None)
        assert len(filtered) == 2

    def test_compute_evalset_sampling_filters_by_content_updated_at(self, monkeypatch):
        """T3-2: compute_evalset_sampling 只统计 content_updated_at 之后的 run"""
        old_run = _mk_run("2026-08-20T10:00:00Z", [
            CaseResult(case_name="a", case_id="c-a", actual_output="ok", passed=True)])
        new_run = _mk_run("2026-08-22T10:00:00Z", [
            CaseResult(case_name="a", case_id="c-a", actual_output="ok", passed=False)])

        monkeypatch.setattr("app.sampling.list_runs", lambda pid: [old_run, new_run])
        monkeypatch.setattr("app.sampling.get_evalset", lambda eid, pid: _mk_evalset(
            content_updated_at="2026-08-21T00:00:00Z"))

        result = compute_evalset_sampling("proj-t3", "es-1")
        # 只纳入 new_run → n=1, c=0（new_run 的 case 失败）
        cases = result["cases"]
        assert len(cases) == 1
        assert cases[0]["n"] == 1
        assert cases[0]["c"] == 0
        assert cases[0]["pass_rate"] == 0.0

    def test_compute_project_sampling_filters_by_content_updated_at(self, monkeypatch):
        """T3-2: compute_project_sampling 用第一个评测集的 content_updated_at 过滤"""
        old_run = _mk_run("2026-08-20T10:00:00Z", [
            CaseResult(case_name="a", actual_output="ok", passed=True)])
        new_run = _mk_run("2026-08-22T10:00:00Z", [
            CaseResult(case_name="a", actual_output="ok", passed=False)])

        monkeypatch.setattr("app.sampling.list_runs", lambda pid: [old_run, new_run])
        monkeypatch.setattr("app.sampling.list_evalsets", lambda pid: [_mk_evalset(
            content_updated_at="2026-08-21T00:00:00Z")])

        result = compute_project_sampling("proj-t3")
        # 只纳入 new_run
        assert result["total_runs"] == 1
        assert result["total_cases"] == 1


class TestT32RoutesSetContentUpdatedAt:
    """T3-2: PUT /evalsets/{id} 与 POST /evalsets/{id}/import?mode=replace 设定 content_updated_at"""

    def test_put_evalset_sets_content_updated_at(self, client, monkeypatch):
        existing = _mk_evalset([_mk_case("a")])
        captured = {}

        def fake_save(evalset):
            captured["content_updated_at"] = evalset.content_updated_at
            captured["cases"] = len(evalset.cases)

        # 路由层把 storage 函数导入为本地符号，要 patch app.routes.* 才生效
        monkeypatch.setattr("app.routes.get_evalset", lambda eid, pid: existing)
        monkeypatch.setattr("app.routes.save_evalset", fake_save)

        resp = client.put(f"/api/evalsets/{existing.id}?project_id={existing.project_id}", json={
            "id": existing.id, "project_id": existing.project_id,
            "name": existing.name,
            "cases": [{"id": "c-a", "case_name": "a", "input": "new", "expected_output": "new", "eval_type": "exact"}],
        })
        assert resp.status_code == 200
        assert captured["content_updated_at"] is not None
        # ISO 8601 格式
        assert "T" in captured["content_updated_at"]
        assert captured["content_updated_at"].endswith("Z")

    def test_replace_import_sets_content_updated_at(self, client, monkeypatch):
        existing = _mk_evalset([_mk_case("a")], content_updated_at=None)
        captured = {}

        def fake_save(evalset):
            captured["content_updated_at"] = evalset.content_updated_at

        monkeypatch.setattr("app.routes.get_evalset", lambda eid, pid: existing)
        monkeypatch.setattr("app.routes.save_evalset", fake_save)

        csv_content = "case_name,input,expected_output,eval_type\na,hello,hello,exact\n"
        resp = client.post(
            f"/api/evalsets/{existing.id}/import?project_id={existing.project_id}&mode=replace",
            data={"file_content": csv_content},
        )
        assert resp.status_code == 200
        assert captured["content_updated_at"] is not None


# ============== T3-1: 采样聚合纳入 k 次采样 ==============

class TestT31SamplingWithKSamples:
    """T3-1: 同一 run 内 k 次采样的样本在聚合时全部纳入"""

    def test_aggregate_includes_k_samples_with_sample_index(self):
        """sample_index 已设的 k 条结果全部纳入 n"""
        run = _mk_run("2026-08-21T10:00:00Z", [
            CaseResult(case_name="a", actual_output="ok", passed=True, sample_index=1),
            CaseResult(case_name="a", actual_output="ok", passed=True, sample_index=2),
            CaseResult(case_name="a", actual_output="ok", passed=False, sample_index=3),
        ])
        records = _aggregate_runs([run])
        # 3 条样本全部纳入
        assert records["a"] == [True, True, False]

    def test_aggregate_dedups_when_sample_index_none(self):
        """sample_index=None 时同名 case 只取第一条（防御重复）"""
        run = _mk_run("2026-08-21T10:00:00Z", [
            CaseResult(case_name="a", actual_output="ok", passed=True),
            CaseResult(case_name="a", actual_output="dup", passed=False),
        ])
        records = _aggregate_runs([run])
        assert records["a"] == [True]  # 只保留第一条

    def test_aggregate_by_case_id_includes_k_samples(self):
        run = _mk_run("2026-08-21T10:00:00Z", [
            CaseResult(case_name="a", case_id="c-a", actual_output="ok", passed=True, sample_index=1),
            CaseResult(case_name="a", case_id="c-a", actual_output="ok", passed=False, sample_index=2),
        ])
        records = _aggregate_runs_by_case_id([run])
        assert records["c-a"]["records"] == [True, False]


# ============== T3-3: 版本概念 + 跨版本对比 ==============

class TestT33ModelFields:
    """T3-3: 模型新增字段默认值与向后兼容"""

    def test_project_versions_default_empty(self):
        """旧项目无 versions → 默认空列表"""
        p = Project(
            id="p1", name="p",
            judge_config=JudgeConfig(base_url="", api_key="", model="m"),
            target_config=TargetConfig(base_url="", api_key="", model="m"),
        )
        assert p.versions == []

    def test_project_version_model(self):
        v = ProjectVersion(id="ver-1", name="v1", created_at="2026-08-21T10:00:00Z")
        assert v.id == "ver-1"
        assert v.name == "v1"

    def test_run_eval_request_version_id_default_none(self):
        from app.models import RunEvalRequest
        r = RunEvalRequest(project_id="p1", evalset_id="e1")
        assert r.version_id is None

    def test_eval_run_version_id_default_none(self):
        """旧 EvalRun 无 version_id → None（向后兼容）"""
        r = EvalRun(id="r1", project_id="p", evalset_id="e", created_at="2026-01-01T00:00:00Z")
        assert r.version_id is None

    def test_create_version_request_requires_name(self):
        v = CreateVersionRequest(name="v1")
        assert v.name == "v1"


class TestT33ResolveVersionId:
    """T3-3: _resolve_version_id 自动归属逻辑"""

    def test_no_versions_returns_none(self):
        """项目无版本 → None（向后兼容）"""
        p = _mk_project()
        assert _resolve_version_id(p, "2026-08-21T10:00:00Z") is None

    def test_explicit_version_validated(self):
        """显式指定 version_id 且存在 → 用该值"""
        p = _mk_project()
        p.versions = [ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")]
        assert _resolve_version_id(p, "2026-08-21T10:00:00Z", explicit="ver-1") == "ver-1"

    def test_explicit_version_not_found_returns_none(self):
        """显式指定不存在的 version_id → None（不阻断）"""
        p = _mk_project()
        p.versions = [ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")]
        assert _resolve_version_id(p, "2026-08-21T10:00:00Z", explicit="ver-x") is None

    def test_auto_assign_falls_into_latest_before_run(self):
        """无显式指定 → 落入 created_at ≤ run 时间的最近版本"""
        p = _mk_project()
        p.versions = [
            ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z"),
            ProjectVersion(id="ver-2", name="v2", created_at="2026-08-15T00:00:00Z"),
        ]
        # run 在 v2 之后 → 归入 v2
        assert _resolve_version_id(p, "2026-08-21T10:00:00Z") == "ver-2"
        # run 在 v1 和 v2 之间 → 归入 v1
        assert _resolve_version_id(p, "2026-08-10T10:00:00Z") == "ver-1"

    def test_auto_assign_run_before_all_versions(self):
        """run 创建早于所有版本 → 归入最早的版本"""
        p = _mk_project()
        p.versions = [
            ProjectVersion(id="ver-1", name="v1", created_at="2026-08-10T00:00:00Z"),
            ProjectVersion(id="ver-2", name="v2", created_at="2026-08-20T00:00:00Z"),
        ]
        assert _resolve_version_id(p, "2026-08-01T00:00:00Z") == "ver-1"


class TestT33VersionRoutes:
    """T3-3: POST/DELETE /projects/{pid}/versions + GET compare"""

    def test_create_version(self, client, monkeypatch):
        """POST /projects/{pid}/versions 创建版本"""
        p = _mk_project()
        captured = {}

        def fake_save(project):
            captured["versions"] = project.versions

        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.save_project", fake_save)
        resp = client.post(f"/api/projects/{p.id}/versions", json={"name": "v1"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "v1"
        assert data["id"].startswith("ver-")
        assert "T" in data["created_at"]
        assert len(captured["versions"]) == 1

    def test_create_version_empty_name_422(self, client, monkeypatch):
        p = _mk_project()
        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.save_project", lambda proj: None)
        resp = client.post(f"/api/projects/{p.id}/versions", json={"name": ""})
        assert resp.status_code == 422

    def test_delete_version(self, client, monkeypatch):
        """DELETE /projects/{pid}/versions/{vid} 删除版本"""
        p = _mk_project()
        p.versions = [
            ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z"),
            ProjectVersion(id="ver-2", name="v2", created_at="2026-08-15T00:00:00Z"),
        ]
        captured = {}

        def fake_save(project):
            captured["remaining"] = len(project.versions)

        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.save_project", fake_save)
        resp = client.delete(f"/api/projects/{p.id}/versions/ver-1")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "ver-1"
        assert captured["remaining"] == 1

    def test_delete_version_not_found_404(self, client, monkeypatch):
        p = _mk_project()
        p.versions = []
        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.save_project", lambda proj: None)
        resp = client.delete(f"/api/projects/{p.id}/versions/ver-x")
        assert resp.status_code == 404


class TestT33CompareVersions:
    """T3-3: GET /projects/{pid}/versions/compare 跨版本聚合 + delta"""

    def _mk_completed_run(self, created_at, version_id, pass_rate, total_token, token_per_pass=0.0, latency_p50=0.0):
        return EvalRun(
            id=f"run-{created_at}", project_id="proj-t3", evalset_id="es-1",
            status="completed", created_at=created_at, version_id=version_id,
            results=[], summary=EvalSummary(
                pass_rate=pass_rate, total_token=total_token, total_latency_ms=0.0,
                token_per_pass=token_per_pass, latency_p50=latency_p50, latency_p95=0.0,
            ),
        )

    def test_compare_two_versions_with_delta(self, client, monkeypatch):
        """开 v1 跑 3 次 → 开 v2 跑 2 次 → 对比视图显示两版本聚合 + delta"""
        p = _mk_project()
        p.versions = [
            ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z"),
            ProjectVersion(id="ver-2", name="v2", created_at="2026-08-15T00:00:00Z"),
        ]
        runs = [
            self._mk_completed_run("2026-08-02T00:00:00Z", "ver-1", 0.8, 100),
            self._mk_completed_run("2026-08-03T00:00:00Z", "ver-1", 0.9, 120),
            self._mk_completed_run("2026-08-04T00:00:00Z", "ver-1", 0.7, 110),
            self._mk_completed_run("2026-08-16T00:00:00Z", "ver-2", 0.6, 200),
            self._mk_completed_run("2026-08-17T00:00:00Z", "ver-2", 0.5, 210),
        ]
        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.list_runs", lambda pid: runs)
        resp = client.get(f"/api/projects/{p.id}/versions/compare")
        assert resp.status_code == 200
        data = resp.json()
        versions = data["versions"]
        assert len(versions) == 2
        # v1: avg(0.8, 0.9, 0.7) = 0.8, total_token = 330
        assert versions[0]["version_name"] == "v1"
        assert versions[0]["run_count"] == 3
        assert abs(versions[0]["pass_rate"] - 0.8) < 0.01
        assert versions[0]["total_token"] == 330
        # v2: avg(0.6, 0.5) = 0.55, total_token = 410
        assert versions[1]["version_name"] == "v2"
        assert versions[1]["run_count"] == 2
        assert abs(versions[1]["pass_rate"] - 0.55) < 0.01
        assert versions[1]["total_token"] == 410
        # delta: v2 - v1
        assert abs(versions[1]["delta_pass_rate"] - (-0.25)) < 0.01
        assert versions[1]["delta_total_token"] == 80
        # 第一版 delta = 0
        assert versions[0]["delta_pass_rate"] == 0.0

    def test_compare_no_versions_returns_empty(self, client, monkeypatch):
        """项目无版本 → 对比返回空数组"""
        p = _mk_project()
        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.list_runs", lambda pid: [])
        resp = client.get(f"/api/projects/{p.id}/versions/compare")
        assert resp.status_code == 200
        assert resp.json()["versions"] == []

    def test_compare_unassigned_runs_bucket(self, client, monkeypatch):
        """run.version_id 为 None → 归入「未分版本」桶"""
        p = _mk_project()
        p.versions = [ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z")]
        runs = [
            self._mk_completed_run("2026-07-01T00:00:00Z", None, 0.5, 50),  # 旧 run 无版本
            self._mk_completed_run("2026-08-02T00:00:00Z", "ver-1", 0.8, 100),
        ]
        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.list_runs", lambda pid: runs)
        resp = client.get(f"/api/projects/{p.id}/versions/compare")
        data = resp.json()
        versions = data["versions"]
        assert len(versions) == 2
        assert versions[0]["version_name"] == "v1"
        assert versions[1]["version_name"] == "未分版本"
        assert versions[1]["run_count"] == 1


class TestT33RunAutoAssignVersion:
    """T3-3: POST /runs 时 run.version_id 自动归属"""

    def test_run_auto_assigned_to_latest_version(self, client, monkeypatch):
        """有版本时，run 自动归属最近版本"""
        p = _mk_project(max_concurrency=4)
        p.versions = [
            ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z"),
            ProjectVersion(id="ver-2", name="v2", created_at="2026-08-15T00:00:00Z"),
        ]
        captured = {}

        def fake_save(run):
            captured["version_id"] = run.version_id

        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.get_evalset", lambda eid, pid: _mk_evalset([_mk_case()]))
        monkeypatch.setattr("app.routes.save_run", fake_save)
        monkeypatch.setattr("app.routes.execute_run", AsyncMock(return_value=None))
        resp = client.post("/api/runs", json={
            "project_id": p.id, "evalset_id": "es-1",
        })
        assert resp.status_code == 201
        # run 在 v2 之后创建 → 归入 v2
        assert captured["version_id"] == "ver-2"

    def test_run_no_version_when_project_has_none(self, client, monkeypatch):
        """项目无版本 → run.version_id = None（向后兼容）"""
        p = _mk_project()
        captured = {}

        def fake_save(run):
            captured["version_id"] = run.version_id

        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.get_evalset", lambda eid, pid: _mk_evalset([_mk_case()]))
        monkeypatch.setattr("app.routes.save_run", fake_save)
        monkeypatch.setattr("app.routes.execute_run", AsyncMock(return_value=None))
        resp = client.post("/api/runs", json={"project_id": p.id, "evalset_id": "es-1"})
        assert resp.status_code == 201
        assert captured["version_id"] is None

    def test_run_explicit_version_id(self, client, monkeypatch):
        """显式指定 version_id → 用该值"""
        p = _mk_project()
        p.versions = [
            ProjectVersion(id="ver-1", name="v1", created_at="2026-08-01T00:00:00Z"),
            ProjectVersion(id="ver-2", name="v2", created_at="2026-08-15T00:00:00Z"),
        ]
        captured = {}

        def fake_save(run):
            captured["version_id"] = run.version_id

        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.get_evalset", lambda eid, pid: _mk_evalset([_mk_case()]))
        monkeypatch.setattr("app.routes.save_run", fake_save)
        monkeypatch.setattr("app.routes.execute_run", AsyncMock(return_value=None))
        resp = client.post("/api/runs", json={
            "project_id": p.id, "evalset_id": "es-1", "version_id": "ver-1",
        })
        assert resp.status_code == 201
        assert captured["version_id"] == "ver-1"


# ============== T3-4: 定时回归 ==============

class TestT34ModelFields:
    """T3-4: ScheduleConfig 模型默认值与向后兼容"""

    def test_project_schedule_default_none(self):
        """旧项目无 schedule → None（向后兼容）"""
        p = Project(
            id="p1", name="p",
            judge_config=JudgeConfig(base_url="", api_key="", model="m"),
            target_config=TargetConfig(base_url="", api_key="", model="m"),
        )
        assert p.schedule is None

    def test_schedule_config_defaults(self):
        s = ScheduleConfig()
        assert s.enabled is False
        assert s.cron == "* * * * *"
        assert s.tags == []
        assert s.regression_threshold == 0.1

    def test_schedule_config_custom(self):
        s = ScheduleConfig(enabled=True, cron="*/30 * * * *", tags=["回归测试"], regression_threshold=0.05)
        assert s.enabled is True
        assert s.cron == "*/30 * * * *"
        assert s.tags == ["回归测试"]
        assert s.regression_threshold == 0.05


class TestT34CronMatch:
    """T3-4: cron 表达式匹配逻辑"""

    def test_every_minute(self):
        """* * * * * 匹配任意时间"""
        from datetime import datetime, timezone
        dt = datetime(2026, 8, 21, 10, 30, 0, tzinfo=timezone.utc)
        assert cron_match("* * * * *", dt)

    def test_specific_minute(self):
        from datetime import datetime, timezone
        dt = datetime(2026, 8, 21, 10, 30, 0, tzinfo=timezone.utc)
        assert cron_match("30 * * * *", dt)
        assert not cron_match("31 * * * *", dt)

    def test_step(self):
        """*/15 = 0,15,30,45 分"""
        from datetime import datetime, timezone
        assert cron_match("*/15 * * * *", datetime(2026, 8, 21, 10, 0, 0, tzinfo=timezone.utc))
        assert cron_match("*/15 * * * *", datetime(2026, 8, 21, 10, 15, 0, tzinfo=timezone.utc))
        assert cron_match("*/15 * * * *", datetime(2026, 8, 21, 10, 30, 0, tzinfo=timezone.utc))
        assert not cron_match("*/15 * * * *", datetime(2026, 8, 21, 10, 7, 0, tzinfo=timezone.utc))

    def test_list(self):
        """5,10,15 分"""
        from datetime import datetime, timezone
        assert cron_match("5,10,15 * * * *", datetime(2026, 8, 21, 10, 5, 0, tzinfo=timezone.utc))
        assert cron_match("5,10,15 * * * *", datetime(2026, 8, 21, 10, 10, 0, tzinfo=timezone.utc))
        assert not cron_match("5,10,15 * * * *", datetime(2026, 8, 21, 10, 6, 0, tzinfo=timezone.utc))

    def test_range(self):
        """0-5 分"""
        from datetime import datetime, timezone
        assert cron_match("0-5 * * * *", datetime(2026, 8, 21, 10, 3, 0, tzinfo=timezone.utc))
        assert not cron_match("0-5 * * * *", datetime(2026, 8, 21, 10, 6, 0, tzinfo=timezone.utc))

    def test_hour(self):
        from datetime import datetime, timezone
        assert cron_match("* 10 * * *", datetime(2026, 8, 21, 10, 30, 0, tzinfo=timezone.utc))
        assert not cron_match("* 11 * * *", datetime(2026, 8, 21, 10, 30, 0, tzinfo=timezone.utc))

    def test_invalid_field_count(self):
        from datetime import datetime, timezone
        dt = datetime(2026, 8, 21, 10, 30, 0, tzinfo=timezone.utc)
        assert not cron_match("* * *", dt)
        assert not cron_match("* * * * * *", dt)


class TestT34DetectRegression:
    """T3-4: 回归检测逻辑"""

    def test_regression_detected(self, monkeypatch):
        """pass_rate 降幅超过阈值 → 回归"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, regression_threshold=0.1)
        old_run = _mk_run("2026-08-20T10:00:00Z", [CaseResult(case_name="a", actual_output="ok", passed=True)])
        old_run.summary.pass_rate = 0.9
        new_run = _mk_run("2026-08-21T10:00:00Z", [CaseResult(case_name="a", actual_output="bad", passed=False)])
        new_run.summary.pass_rate = 0.5  # 降幅 0.4 > 阈值 0.1

        monkeypatch.setattr("app.scheduler.get_project", lambda pid: p)
        monkeypatch.setattr("app.scheduler.get_run", lambda rid, pid: new_run)
        monkeypatch.setattr("app.scheduler.list_runs", lambda pid: [old_run, new_run])

        alert = detect_regression("proj-t3", new_run.id, threshold=0.1)
        assert alert is not None
        assert alert["pass_rate"] == 0.5
        assert alert["baseline_pass_rate"] == 0.9
        assert abs(alert["delta"] - (-0.4)) < 0.01

    def test_no_regression_within_threshold(self, monkeypatch):
        """降幅未超过阈值 → 无回归"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, regression_threshold=0.1)
        old_run = _mk_run("2026-08-20T10:00:00Z", [CaseResult(case_name="a", actual_output="ok", passed=True)])
        old_run.summary.pass_rate = 0.9
        new_run = _mk_run("2026-08-21T10:00:00Z", [CaseResult(case_name="a", actual_output="ok", passed=True)])
        new_run.summary.pass_rate = 0.85  # 降幅 0.05 < 阈值 0.1

        monkeypatch.setattr("app.scheduler.get_project", lambda pid: p)
        monkeypatch.setattr("app.scheduler.get_run", lambda rid, pid: new_run)
        monkeypatch.setattr("app.scheduler.list_runs", lambda pid: [old_run, new_run])

        assert detect_regression("proj-t3", new_run.id, threshold=0.1) is None

    def test_no_baseline_returns_none(self, monkeypatch):
        """无历史 run → 无 baseline → 无回归"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True)
        new_run = _mk_run("2026-08-21T10:00:00Z", [CaseResult(case_name="a", actual_output="ok", passed=True)])

        monkeypatch.setattr("app.scheduler.get_project", lambda pid: p)
        monkeypatch.setattr("app.scheduler.get_run", lambda rid, pid: new_run)
        monkeypatch.setattr("app.scheduler.list_runs", lambda pid: [new_run])

        assert detect_regression("proj-t3", new_run.id, threshold=0.1) is None


class TestT34RegressionAlertsRoute:
    """T3-4: GET /projects/{pid}/regression-alerts"""

    def test_alerts_empty_when_no_schedule(self, client, monkeypatch):
        p = _mk_project()
        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.scheduler.get_project", lambda pid: p)
        monkeypatch.setattr("app.scheduler.list_runs", lambda pid: [])
        resp = client.get(f"/api/projects/{p.id}/regression-alerts")
        assert resp.status_code == 200
        assert resp.json()["alerts"] == []

    def test_alerts_returned_on_regression(self, client, monkeypatch):
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, regression_threshold=0.1)
        old_run = _mk_run("2026-08-20T10:00:00Z", [CaseResult(case_name="a", actual_output="ok", passed=True)])
        old_run.summary.pass_rate = 0.9
        new_run = _mk_run("2026-08-21T10:00:00Z", [CaseResult(case_name="a", actual_output="bad", passed=False)])
        new_run.summary.pass_rate = 0.5

        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.scheduler.get_project", lambda pid: p)
        monkeypatch.setattr("app.scheduler.get_run", lambda rid, pid: new_run)
        monkeypatch.setattr("app.scheduler.list_runs", lambda pid: [old_run, new_run])

        resp = client.get(f"/api/projects/{p.id}/regression-alerts")
        assert resp.status_code == 200
        alerts = resp.json()["alerts"]
        assert len(alerts) == 1
        assert alerts[0]["pass_rate"] == 0.5
        assert alerts[0]["baseline_pass_rate"] == 0.9


class TestT34ScheduleSavedViaProject:
    """T3-4: schedule 通过 PUT /projects 保存"""

    def test_put_project_saves_schedule(self, client, monkeypatch):
        p = _mk_project()
        captured = {}

        def fake_save(project):
            captured["schedule"] = project.schedule

        monkeypatch.setattr("app.routes.get_project", lambda pid: p)
        monkeypatch.setattr("app.routes.save_project", fake_save)
        resp = client.put(f"/api/projects/{p.id}", json={
            "id": p.id, "name": p.name, "task_shape": p.task_shape,
            "judge_config": {"base_url": "", "api_key": "__UNCHANGED__", "model": "m"},
            "target_config": {"base_url": "", "api_key": "__UNCHANGED__", "model": "m",
                              "request_template": "{input}"},
            "max_concurrency": 1,
            "schedule": {"enabled": True, "cron": "*/5 * * * *", "tags": [], "regression_threshold": 0.1},
        })
        assert resp.status_code == 200
        assert captured["schedule"] is not None
        assert captured["schedule"].enabled is True
        assert captured["schedule"].cron == "*/5 * * * *"


class TestT34CheckAndTrigger:
    """T3-4: 定时回归触发循环 check_and_trigger_scheduled_runs"""

    def test_skip_projects_without_schedule(self, monkeypatch):
        """无 schedule 的项目跳过"""
        p = _mk_project()  # schedule=None
        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [_mk_evalset()])
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        assert triggered == []

    def test_skip_disabled_schedule(self, monkeypatch):
        """schedule.enabled=False 跳过"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=False, cron="* * * * *")
        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [_mk_evalset()])
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        assert triggered == []

    def test_skip_when_cron_not_match(self, monkeypatch):
        """cron 不匹配当前时间则跳过"""
        p = _mk_project()
        # 用一个绝对不匹配的时间表达式：分=99 非法 → cron_match 返回 False（_parse_cron_field 对越界未硬阻断，
        # 这里改用具体分 + monkeypatch cron_match 返回 False）
        p.schedule = ScheduleConfig(enabled=True, cron="0 0 1 1 1")  # 1月1日0分，几乎不匹配
        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [_mk_evalset()])
        # 强制 cron_match 返回 False
        monkeypatch.setattr("app.scheduler.cron_match", lambda expr, dt: False)
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        assert triggered == []

    def test_skip_when_no_evalsets(self, monkeypatch):
        """无评测集的项目跳过"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [])
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        assert triggered == []

    def test_skip_when_no_enabled_cases(self, monkeypatch):
        """评测集无启用 case 跳过"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        disabled_case = EvalCase(
            id="c-x", case_name="x", input="i", expected_output="o",
            eval_type="exact", enabled=False,
        )
        es = _mk_evalset(cases=[disabled_case])
        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [es])
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        assert triggered == []

    def test_triggers_run_when_all_match(self, monkeypatch):
        """条件全部满足时发起 run"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        es = _mk_evalset(cases=[_mk_case("a")])
        saved_runs = []

        def fake_save_run(run):
            saved_runs.append(run)

        async def fake_execute(run, *args, **kwargs):
            run.status = "completed"
            return run

        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [es])
        monkeypatch.setattr("app.scheduler.save_run", fake_save_run)
        monkeypatch.setattr("app.scheduler.execute_run", fake_execute)
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        assert len(triggered) == 1
        assert saved_runs[0].project_id == p.id
        assert saved_runs[0].evalset_id == es.id

    def test_tag_filter_skips_when_no_match(self, monkeypatch):
        """schedule.tags 不匹配任何 case 时跳过"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *", tags=["nonexistent"])
        es = _mk_evalset(cases=[_mk_case("a")])  # case 无 tags
        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [es])
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        assert triggered == []

    def test_tag_filter_matches_proceeds(self, monkeypatch):
        """schedule.tags 匹配 case 标签时正常发起"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *", tags=["regression"])
        case_with_tag = EvalCase(
            id="c-t", case_name="t", input="i", expected_output="o",
            eval_type="exact", tags=["regression"],
        )
        es = _mk_evalset(cases=[case_with_tag])
        saved = []

        def fake_save_run(run):
            saved.append(run)

        async def fake_execute(run, *args, **kwargs):
            run.status = "completed"
            return run

        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [es])
        monkeypatch.setattr("app.scheduler.save_run", fake_save_run)
        monkeypatch.setattr("app.scheduler.execute_run", fake_execute)
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        assert len(triggered) == 1

    def test_execute_failure_does_not_break_loop(self, monkeypatch):
        """execute_run 抛异常不影响调度循环"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        es = _mk_evalset(cases=[_mk_case("a")])

        def fake_save_run(run):
            pass

        async def fake_execute(run, *args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [es])
        monkeypatch.setattr("app.scheduler.save_run", fake_save_run)
        monkeypatch.setattr("app.scheduler.execute_run", fake_execute)
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        # 即使 execute 抛异常，run_id 仍计入 triggered（save_run 已成功）
        assert len(triggered) == 1

    def test_multiple_projects_in_loop(self, monkeypatch):
        """多项目并发遍历，各自独立触发"""
        p1 = _mk_project()
        p1.id = "p1"
        p1.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        p2 = _mk_project()
        p2.id = "p2"
        p2.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        es1 = _mk_evalset(cases=[_mk_case("a")])
        es2 = _mk_evalset(cases=[_mk_case("b")])
        saved = []

        def fake_save_run(run):
            saved.append(run)

        async def fake_execute(run, *args, **kwargs):
            run.status = "completed"
            return run

        def fake_list_evalsets(pid):
            return [es1] if pid == "p1" else [es2]

        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p1, p2])
        monkeypatch.setattr("app.scheduler.list_evalsets", fake_list_evalsets)
        monkeypatch.setattr("app.scheduler.save_run", fake_save_run)
        monkeypatch.setattr("app.scheduler.execute_run", fake_execute)
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        assert len(triggered) == 2
        project_ids = {r.project_id for r in saved}
        assert project_ids == {"p1", "p2"}

    def test_execute_run_does_not_block_loop(self, monkeypatch):
        """execute_run 在后台 task 中执行，调度循环立即返回不阻塞"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        es = _mk_evalset(cases=[_mk_case("a")])

        async def slow_execute(run, *args, **kwargs):
            await asyncio.sleep(10)  # 模拟长时间执行
            run.status = "completed"
            return run

        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [es])
        monkeypatch.setattr("app.scheduler.save_run", lambda r: None)
        monkeypatch.setattr("app.scheduler.execute_run", slow_execute)

        start = time.time()
        triggered = asyncio.run(check_and_trigger_scheduled_runs())
        elapsed = time.time() - start
        # 调度循环立即返回（远小于 10 秒的 execute_run 时长）
        assert elapsed < 2, f"调度循环被阻塞 {elapsed:.2f}s，应立即返回"
        assert len(triggered) == 1

    def test_pending_tasks_tracked(self, monkeypatch):
        """后台 task 加入 _pending_scheduled_tasks 防止 GC"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        es = _mk_evalset(cases=[_mk_case("a")])

        async def slow_execute(run, *args, **kwargs):
            await asyncio.sleep(10)
            return run

        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [es])
        monkeypatch.setattr("app.scheduler.save_run", lambda r: None)
        monkeypatch.setattr("app.scheduler.execute_run", slow_execute)

        async def run_and_check():
            triggered = await check_and_trigger_scheduled_runs()
            # 调度循环刚返回时，后台 task 还在执行，应在 _pending_scheduled_tasks 中
            assert len(_pending_scheduled_tasks) == 1
            return triggered

        triggered = asyncio.run(run_and_check())
        assert len(triggered) == 1

    def test_pending_tasks_cleaned_after_completion(self, monkeypatch):
        """execute_run 完成后，done callback 从 _pending_scheduled_tasks 移除 task"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        es = _mk_evalset(cases=[_mk_case("a")])

        async def fast_execute(run, *args, **kwargs):
            run.status = "completed"
            return run

        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [es])
        monkeypatch.setattr("app.scheduler.save_run", lambda r: None)
        monkeypatch.setattr("app.scheduler.execute_run", fast_execute)

        async def run_and_wait():
            triggered = await check_and_trigger_scheduled_runs()
            # 等待后台 task 完成并触发 done callback
            await asyncio.sleep(0.05)
            return triggered

        asyncio.run(run_and_wait())
        # done callback 已把 task 从集合移除
        assert len(_pending_scheduled_tasks) == 0

    def test_execute_failure_logged_via_callback(self, monkeypatch, caplog):
        """execute_run 抛异常时，异常通过 done callback 记录到 logger"""
        p = _mk_project()
        p.schedule = ScheduleConfig(enabled=True, cron="* * * * *")
        es = _mk_evalset(cases=[_mk_case("a")])

        async def failing_execute(run, *args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.scheduler.list_projects", lambda: [p])
        monkeypatch.setattr("app.scheduler.list_evalsets", lambda pid: [es])
        monkeypatch.setattr("app.scheduler.save_run", lambda r: None)
        monkeypatch.setattr("app.scheduler.execute_run", failing_execute)

        async def run_and_yield():
            triggered = await check_and_trigger_scheduled_runs()
            # 让 event loop 跑一下后台 task，让异常得以抛出
            await asyncio.sleep(0.05)
            return triggered

        with caplog.at_level(logging.WARNING, logger="app.scheduler"):
            triggered = asyncio.run(run_and_yield())
        assert len(triggered) == 1
        # done callback 记录了异常
        assert any("boom" in r.message for r in caplog.records), \
            "异常应通过 done callback 记录到 logger"
