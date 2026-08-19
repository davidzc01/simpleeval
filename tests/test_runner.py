"""runner 模块单元测试"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.runner import (
    percentile,
    check_judge_available,
    run_evalset,
    _build_run_result,
    _utc_now,
    _generate_run_id,
)
from app.models import Project, EvalSet, CaseResult, EvalSummary


class TestPercentile:
    """分位数计算测试"""

    def test_empty_data(self):
        """空数据返回 0"""
        assert percentile([], 95) == 0.0

    def test_single_value(self):
        """单个值返回自身"""
        assert percentile([100.0], 95) == 100.0

    def test_p50_median(self):
        """P50 应该是中位数"""
        data = [1, 2, 3, 4, 5]
        result = percentile(data, 50)
        assert result == 3.0

    def test_p95_single_value(self):
        """P95 单值"""
        data = [10.0]
        assert percentile(data, 95) == 10.0

    def test_p95_linear_interpolation(self):
        """P95 线性插值"""
        # 20 个值，P95 应该在 95% 位置附近
        data = list(range(1, 21))  # 1-20
        result = percentile(data, 95)
        # 0.95 * 19 = 18.05，第18个元素 + 0.05 * 差值 = 18 + 0.05 = 18.05
        # 实际结果可能在 18-19 之间
        assert 17 <= result <= 20

    def test_p99(self):
        """P99 测试"""
        data = list(range(1, 101))  # 1-100
        result = percentile(data, 99)
        # 应该在 99 附近
        assert result > 95


class TestUtcNow:
    """UTC 时间测试"""

    def test_utc_now_format(self):
        """时间格式应该是 ISO 8601"""
        result = _utc_now()
        # 应该以 Z 结尾
        assert result.endswith("Z")
        # 应该包含日期和时间
        assert "T" in result


class TestGenerateRunId:
    """Run ID 生成测试"""

    def test_run_id_prefix(self):
        """Run ID 应该以 run- 开头"""
        result = _generate_run_id()
        assert result.startswith("run-")

    def test_run_id_uniqueness(self):
        """Run ID 应该唯一"""
        ids = [_generate_run_id() for _ in range(100)]
        assert len(set(ids)) == 100


class TestBuildRunResult:
    """结果构建测试"""

    def test_basic_result(self):
        """基本结果构建"""
        results = [
            CaseResult(case_name="test1", actual_output="out1", passed=True, score=1.0, latency_ms=100, token_used=50),
            CaseResult(case_name="test2", actual_output="out2", passed=False, score=0.0, latency_ms=200, token_used=100),
        ]
        run = _build_run_result(
            run_id="run-001",
            project_id="proj-001",
            evalset_id="evalset-001",
            results=results,
        )

        assert run.id == "run-001"
        assert run.project_id == "proj-001"
        assert run.evalset_id == "evalset-001"
        assert run.status == "completed"
        assert len(run.results) == 2
        assert run.summary is not None
        assert run.summary.pass_rate == 0.5
        assert run.summary.total_token == 150

    def test_with_skipped_cases(self):
        """有跳过 case 的情况"""
        results = [
            CaseResult(case_name="test1", actual_output="out1", passed=True, score=1.0, latency_ms=100, token_used=50),
            CaseResult(case_name="test2", actual_output="[SKIPPED]", passed=False, score=0.0, latency_ms=0, token_used=0, skipped_reason="llm_unavailable"),
        ]
        run = _build_run_result(
            run_id="run-001",
            project_id="proj-001",
            evalset_id="evalset-001",
            results=results,
        )

        # 通过率应该只计算有效 case
        assert run.summary.pass_rate == 1.0  # 1/1，只有1个有效 case

    def test_failed_run(self):
        """失败状态"""
        results = []
        run = _build_run_result(
            run_id="run-001",
            project_id="proj-001",
            evalset_id="evalset-001",
            results=results,
            status="failed",
            error="配置错误",
        )

        assert run.status == "failed"
        assert run.error == "配置错误"


class TestCheckJudgeAvailable:
    """Judge 可用性检查测试"""

    @pytest.mark.asyncio
    async def test_judge_available(self, sample_project):
        """Judge 可用"""
        project = Project(**sample_project)

        with patch("app.runner.judge_with_llm", new_callable=AsyncMock) as mock_judge:
            mock_judge.return_value = 0.5
            available, error = await check_judge_available(project)

            assert available is True
            assert error == ""

    @pytest.mark.asyncio
    async def test_judge_unavailable_network(self, sample_project):
        """Judge 网络不可用"""
        from app.judge import NetworkError
        project = Project(**sample_project)

        with patch("app.runner.judge_with_llm", new_callable=AsyncMock) as mock_judge:
            mock_judge.side_effect = NetworkError("连接失败")
            available, error = await check_judge_available(project)

            assert available is False
            assert "llm_unavailable" in error

    @pytest.mark.asyncio
    async def test_judge_unavailable_api(self, sample_project):
        """Judge API 错误"""
        from app.judge import APIError
        project = Project(**sample_project)

        with patch("app.runner.judge_with_llm", new_callable=AsyncMock) as mock_judge:
            mock_judge.side_effect = APIError("401 Unauthorized", 401)
            available, error = await check_judge_available(project)

            assert available is False
            assert "401" in error


class TestRunEvalset:
    """评测执行测试"""

    @pytest.mark.asyncio
    async def test_all_cases_pass(self, sample_project, sample_evalset):
        """所有 case 都通过"""
        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)

        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:

            # 模拟 API 调用 - exact case 返回匹配内容
            mock_call.return_value = ("你好！", 50)
            mock_check.return_value = (True, "")

            run = await run_evalset(project, evalset)

            assert run.status == "completed"
            assert run.summary is not None
            # 禁用和 llm_judge 的 case 被过滤/跳过，只有 enabled 的 exact/contains/not_contains/length case

    @pytest.mark.asyncio
    async def test_api_error_handling(self, sample_project, sample_evalset):
        """API 错误处理"""
        from app.judge import NetworkError
        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)

        # 只保留一个 enabled 的 exact case
        evalset.cases = [c for c in evalset.cases if c.id == "case-001"]

        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:

            mock_call.side_effect = NetworkError("网络不可达")
            mock_check.return_value = (True, "")

            run = await run_evalset(project, evalset)

            # case 应该标记为失败
            assert len(run.results) == 1
            assert run.results[0].passed is False
            assert "[ERROR]" in run.results[0].actual_output

    @pytest.mark.asyncio
    async def test_empty_evalset(self, sample_project):
        """空评测集"""
        project = Project(**sample_project)
        evalset = EvalSet(
            id="evalset-empty",
            project_id=project.id,
            name="空评测集",
            cases=[]
        )

        with pytest.raises(ValueError) as exc_info:
            await run_evalset(project, evalset)
        assert "没有启用的 case" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_all_disabled_cases(self, sample_project):
        """所有 case 都被禁用"""
        project = Project(**sample_project)
        evalset = EvalSet(
            id="evalset-disabled",
            project_id=project.id,
            name="禁用评测集",
            cases=[
                {"id": "case-1", "case_name": "禁用", "input": "test", "expected_output": "test",
                 "eval_type": "exact", "enabled": False}
            ]
        )

        with pytest.raises(ValueError) as exc_info:
            await run_evalset(project, evalset)
        assert "没有启用的 case" in str(exc_info.value)


class TestTokenPerPass:
    """每万 token 完成率测试"""

    def test_token_per_pass_calculation(self):
        """每万 token 完成率计算"""
        results = [
            CaseResult(case_name="test1", actual_output="out", passed=True, score=1.0, latency_ms=100, token_used=5000),
            CaseResult(case_name="test2", actual_output="out", passed=True, score=1.0, latency_ms=100, token_used=5000),
            CaseResult(case_name="test3", actual_output="out", passed=False, score=0.0, latency_ms=100, token_used=5000),
        ]

        run = _build_run_result(
            run_id="run-001",
            project_id="proj-001",
            evalset_id="evalset-001",
            results=results,
        )

        # 通过数 2，总 token 15000
        # token_per_pass = 2 / (15000 / 10000) = 2 / 1.5 = 1.333...
        assert run.summary.token_per_pass == pytest.approx(1.33, rel=0.1)

    def test_zero_token(self):
        """零 token 情况"""
        results = [
            CaseResult(case_name="test1", actual_output="out", passed=False, score=0.0, latency_ms=100, token_used=0),
        ]

        run = _build_run_result(
            run_id="run-001",
            project_id="proj-001",
            evalset_id="evalset-001",
            results=results,
        )

        assert run.summary.token_per_pass == 0.0
