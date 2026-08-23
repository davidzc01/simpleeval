"""runner 模块单元测试"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.runner import (
    percentile,
    check_judge_available,
    run_evalset,
    _build_run_result,
    _token_missing_on_error,
    _utc_now,
    _generate_run_id,
)
from app.models import Project, EvalSet, CaseResult, EvalSummary, ResponseParsing


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
            mock_judge.return_value = (0.5, 10, "test")
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
            mock_call.return_value = ("你好！", 50, False)
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


class TestExecuteRun:
    """execute_run 异步执行测试"""

    @pytest.mark.asyncio
    async def test_execute_run_success(self, sample_project, sample_evalset):
        """成功执行"""
        from app.runner import execute_run
        from app.models import EvalRun

        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)

        # 只保留一个 exact case
        evalset.cases = [c for c in evalset.cases if c.id == "case-001"]

        run = EvalRun(
            id="run-execute-001",
            project_id=project.id,
            evalset_id=evalset.id,
            status="queued",
            created_at="2024-01-01T00:00:00Z",
        )

        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check, \
             patch("app.runner.async_save_run", new_callable=AsyncMock) as mock_save:

            mock_call.return_value = ("你好！", 50, False)
            mock_check.return_value = (True, "")

            result = await execute_run(run, project, evalset)

            assert result.status == "completed"
            assert len(result.results) == 1
            assert result.results[0].passed is True
            # async_save_run 应该被调用（每 case 落盘 + 最终落盘）
            assert mock_save.called

    @pytest.mark.asyncio
    async def test_execute_run_with_judge(self, sample_project, sample_evalset):
        """带 LLM Judge 执行"""
        from app.runner import execute_run
        from app.models import EvalRun

        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)

        # 只保留一个 llm_judge case
        evalset.cases = [c for c in evalset.cases if c.id == "case-005"]

        run = EvalRun(
            id="run-judge-001",
            project_id=project.id,
            evalset_id=evalset.id,
            status="queued",
            created_at="2024-01-01T00:00:00Z",
        )

        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.judge_with_llm", new_callable=AsyncMock) as mock_judge, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check, \
             patch("app.runner.async_save_run", new_callable=AsyncMock) as mock_save:

            mock_call.return_value = ("礼貌的回复", 50, False)
            mock_judge.return_value = (0.8, 20, "judge raw response")
            mock_check.return_value = (True, "")

            result = await execute_run(run, project, evalset)

            assert result.status == "completed"
            assert result.results[0].passed is True  # 0.8 >= 0.5

    @pytest.mark.asyncio
    async def test_execute_run_judge_unavailable(self, sample_project, sample_evalset):
        """Judge 不可用时跳过 llm_judge case"""
        from app.runner import execute_run
        from app.models import EvalRun

        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)

        # 保留 llm_judge case
        evalset.cases = [c for c in evalset.cases if c.id == "case-005"]

        run = EvalRun(
            id="run-judge-skip-001",
            project_id=project.id,
            evalset_id=evalset.id,
            status="queued",
            created_at="2024-01-01T00:00:00Z",
        )

        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check, \
             patch("app.runner.async_save_run", new_callable=AsyncMock):

            mock_call.return_value = ("回复内容", 50, False)
            mock_check.return_value = (False, "llm_unavailable: 网络错误")

            result = await execute_run(run, project, evalset)

            assert result.status == "completed"
            assert "[SKIPPED]" in result.results[0].actual_output
            assert result.results[0].skipped_reason is not None

    @pytest.mark.asyncio
    async def test_execute_run_api_error(self, sample_project, sample_evalset):
        """API 错误处理"""
        from app.runner import execute_run
        from app.models import EvalRun
        from app.judge import NetworkError

        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)

        evalset.cases = [c for c in evalset.cases if c.id == "case-001"]

        run = EvalRun(
            id="run-error-001",
            project_id=project.id,
            evalset_id=evalset.id,
            status="queued",
            created_at="2024-01-01T00:00:00Z",
        )

        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check, \
             patch("app.runner.async_save_run", new_callable=AsyncMock):

            mock_call.side_effect = NetworkError("网络不可达")
            mock_check.return_value = (True, "")

            result = await execute_run(run, project, evalset)

            assert result.status == "completed"
            assert "[ERROR]" in result.results[0].actual_output

    @pytest.mark.asyncio
    async def test_execute_run_failed_status(self, sample_project, sample_evalset):
        """执行失败状态"""
        from app.runner import execute_run
        from app.models import EvalRun

        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)

        evalset.cases = [c for c in evalset.cases if c.id == "case-001"]

        run = EvalRun(
            id="run-fail-001",
            project_id=project.id,
            evalset_id=evalset.id,
            status="queued",
            created_at="2024-01-01T00:00:00Z",
        )

        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check, \
             patch("app.runner.async_save_run", new_callable=AsyncMock):

            # 模拟一个未捕获的异常
            mock_call.side_effect = RuntimeError("未知错误")
            mock_check.return_value = (True, "")

            with pytest.raises(RuntimeError):
                await execute_run(run, project, evalset)


class TestTokenMissing:
    """token_missing 标记测试（异常路径用 _token_missing_on_error，成功路径由 call_target 返回）"""

    def test_no_response_parsing_not_missing_on_error(self, sample_project):
        """未配置 response_parsing：异常时也不标记缺失"""
        project = Project(**sample_project)
        assert _token_missing_on_error(project) is False

    def test_with_response_parsing_missing_on_error(self, sample_project):
        """配置了 response_parsing：异常时标记缺失"""
        sample_project["target_config"]["response_parsing"] = {
            "output_paths": ["$.choices[0].message.content"],
        }
        project = Project(**sample_project)
        assert _token_missing_on_error(project) is True

    def test_with_token_config_missing_on_error(self, sample_project):
        """配置了 token_paths：异常时标记缺失"""
        sample_project["target_config"]["response_parsing"] = {
            "output_paths": ["$.choices[0].message.content"],
            "token_paths": ["$.usage.total_tokens"],
        }
        project = Project(**sample_project)
        assert _token_missing_on_error(project) is True

    @pytest.mark.asyncio
    async def test_run_sets_token_missing(self, sample_project, sample_evalset):
        """run_evalset 在 CaseResult 上设置 token_missing"""
        sample_project["target_config"]["response_parsing"] = {
            "output_paths": ["$.choices[0].message.content"],
        }
        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)
        # 只保留一个 enabled exact case
        evalset.cases = [c for c in evalset.cases if c.id == "case-001"]

        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            # response_parsing 配了但无 token 配置 → count_tokens 返回 missing=True
            mock_call.return_value = ("你好！", 0, True)
            mock_check.return_value = (True, "")

            run = await run_evalset(project, evalset)
            assert run.results[0].token_missing is True

    @pytest.mark.asyncio
    async def test_run_no_parsing_not_missing(self, sample_project, sample_evalset):
        """未配置 response_parsing 时 token_missing 为 False"""
        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)
        evalset.cases = [c for c in evalset.cases if c.id == "case-001"]

        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            mock_call.return_value = ("你好！", 50, False)
            mock_check.return_value = (True, "")

            run = await run_evalset(project, evalset)
            assert run.results[0].token_missing is False


class TestBug1HardTimeout:
    """BUG-1 防御：call_target / judge_with_llm 外层硬超时 + async_save_run 异步落盘"""

    @pytest.mark.asyncio
    async def test_call_target_hard_timeout_raises_network_error(self):
        """call_target 硬超时映射为 NetworkError("评测调用超时...")"""
        from app.runner import _call_target_with_hard_timeout, HARD_CALL_TIMEOUT_SECONDS
        from app.judge import NetworkError
        import asyncio

        async def hang_forever(**kwargs):
            # 模拟"连接活跃但永不返回"——sleep 时间远大于硬超时
            await asyncio.sleep(HARD_CALL_TIMEOUT_SECONDS + 30)
            return ("never", 0, False)

        with patch("app.runner.call_target", new=hang_forever):
            # 把硬超时改小到 0.05s 以加速测试
            with patch("app.runner.HARD_CALL_TIMEOUT_SECONDS", 0.05):
                with pytest.raises(NetworkError) as exc_info:
                    await _call_target_with_hard_timeout(base_url="", api_key="", model="")
            assert "评测调用超时" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_judge_with_llm_hard_timeout_raises_network_error(self):
        """judge_with_llm 硬超时映射为 NetworkError("Judge 调用超时...")"""
        from app.runner import _judge_with_llm_with_hard_timeout
        from app.judge import NetworkError
        import asyncio

        async def hang_forever(**kwargs):
            await asyncio.sleep(60)
            return (0.5, 0)

        with patch("app.runner.judge_with_llm", new=hang_forever):
            with patch("app.runner.HARD_CALL_TIMEOUT_SECONDS", 0.05):
                with pytest.raises(NetworkError) as exc_info:
                    await _judge_with_llm_with_hard_timeout(base_url="", api_key="", model="")
            assert "Judge 调用超时" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_call_target_hard_timeout_propagates_to_case_result(self, sample_project, sample_evalset):
        """硬超时被映射为 NetworkError，execute_run 捕获后写 [ERROR] 而非挂起"""
        from app.runner import execute_run
        from app.models import EvalRun
        import asyncio

        project = Project(**sample_project)
        evalset = EvalSet(**sample_evalset)
        evalset.cases = [c for c in evalset.cases if c.id == "case-001"]

        run = EvalRun(
            id="run-hard-timeout-001",
            project_id=project.id,
            evalset_id=evalset.id,
            status="queued",
            created_at="2024-01-01T00:00:00Z",
        )

        async def hang_forever(**kwargs):
            await asyncio.sleep(60)
            return ("never", 0, False)

        with patch("app.runner.call_target", new=hang_forever), \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check, \
             patch("app.runner.async_save_run", new_callable=AsyncMock), \
             patch("app.runner.HARD_CALL_TIMEOUT_SECONDS", 0.05):
            mock_check.return_value = (True, "")
            result = await execute_run(run, project, evalset)

        assert result.status == "completed"
        assert "[ERROR]" in result.results[0].actual_output
        assert "评测调用超时" in result.results[0].actual_output
        assert result.results[0].passed is False

    @pytest.mark.asyncio
    async def test_async_save_run_runs_in_thread_pool(self):
        """async_save_run 真异步：在线程池执行，不阻塞事件循环"""
        from app.storage import async_save_run, _save_run_sync
        from app.models import EvalRun
        import asyncio
        import threading
        from unittest.mock import patch

        run = EvalRun(
            id="run-async-001",
            project_id="proj-async",
            evalset_id="es-async",
            status="queued",
            created_at="2024-01-01T00:00:00Z",
        )

        main_thread = threading.get_ident()
        captured_thread_ids = []

        original = _save_run_sync

        def spy(run_arg):
            captured_thread_ids.append(threading.get_ident())
            return original(run_arg)

        with patch("app.storage._save_run_sync", side_effect=spy):
            await async_save_run(run)

        # 应该在线程池执行，主线程 ID 不应等于执行线程 ID
        assert len(captured_thread_ids) == 1
        assert captured_thread_ids[0] != main_thread, \
            "async_save_run 应在线程池执行（线程 ID 不等于主线程）"

    @pytest.mark.asyncio
    async def test_save_run_sync_still_works_directly(self):
        """同步 save_run 仍可被 main.py startup 等同步上下文调用"""
        from app.storage import save_run
        from app.models import EvalRun

        run = EvalRun(
            id="run-sync-001",
            project_id="proj-sync",
            evalset_id="es-sync",
            status="failed",
            created_at="2024-01-01T00:00:00Z",
            error="服务重启导致任务中断",
        )
        save_run(run)
        # 文件应该存在
        from app.storage import RUNS_DIR
        assert (RUNS_DIR / "proj-sync" / "run-sync-001.json").exists()

