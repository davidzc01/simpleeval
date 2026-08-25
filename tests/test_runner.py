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


# ============== R-4: 新增确定性检测类型端到端集成 ==============

class TestR4RunnerIntegration:
    """R-4: 4 种新 eval_type 走完整 run_evalset 流程，验证 runner 分发与 fields 上下文"""

    def _make_project(self, sample_project):
        return Project(**sample_project)

    def _make_evalset(self, sample_evalset, cases):
        data = dict(sample_evalset)
        data["cases"] = cases
        return EvalSet(**data)

    @pytest.mark.asyncio
    async def test_regex_pass_and_fail(self, sample_project, sample_evalset):
        """regex 类型：命中即过"""
        from app.models import EvalCase
        project = self._make_project(sample_project)
        evalset = self._make_evalset(sample_evalset, [
            EvalCase(id="c1", case_name="regex-hit", input="hi", eval_type="regex",
                     eval_params={"pattern": "hello"}, enabled=True),
            EvalCase(id="c2", case_name="regex-miss", input="hi", eval_type="regex",
                     eval_params={"pattern": "xyz"}, enabled=True),
        ])
        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            mock_call.return_value = ("hello world", 50, False)
            mock_check.return_value = (True, "")
            run = await run_evalset(project, evalset)
        results = {r.case_id: r for r in run.results}
        assert results["c1"].passed is True
        assert results["c2"].passed is False

    @pytest.mark.asyncio
    async def test_json_schema_pass_and_fail(self, sample_project, sample_evalset):
        """json_schema 类型：合法 JSON + 符合 schema 通过"""
        from app.models import EvalCase
        project = self._make_project(sample_project)
        schema = {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}}
        evalset = self._make_evalset(sample_evalset, [
            EvalCase(id="c1", case_name="schema-pass", input="hi", eval_type="json_schema",
                     eval_params={"schema": schema}, enabled=True),
            EvalCase(id="c2", case_name="schema-missing-field", input="hi", eval_type="json_schema",
                     eval_params={"schema": schema}, enabled=True),
            EvalCase(id="c3", case_name="schema-not-json", input="hi", eval_type="json_schema",
                     eval_params={"schema": schema}, enabled=True),
        ])
        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            # 依次返回三个不同的 actual（用 side_effect 列表）
            mock_call.side_effect = [
                ('{"status": "ok"}', 50, False),
                ('{"foo": "bar"}', 50, False),
                ("not a json", 50, False),
            ]
            mock_check.return_value = (True, "")
            run = await run_evalset(project, evalset)
        results = {r.case_id: r for r in run.results}
        assert results["c1"].passed is True   # 符合 schema
        assert results["c2"].passed is False  # 缺 required 字段 status
        assert results["c3"].passed is False  # 非 JSON

    @pytest.mark.asyncio
    async def test_numeric_pass_and_fail(self, sample_project, sample_evalset):
        """numeric 类型：数值比较"""
        from app.models import EvalCase
        project = self._make_project(sample_project)
        evalset = self._make_evalset(sample_evalset, [
            EvalCase(id="c1", case_name="num-pass", input="hi", eval_type="numeric",
                     eval_params={"operator": "gt", "value": 3}, enabled=True),
            EvalCase(id="c2", case_name="num-fail", input="hi", eval_type="numeric",
                     eval_params={"operator": "gt", "value": 100}, enabled=True),
            EvalCase(id="c3", case_name="num-non-numeric", input="hi", eval_type="numeric",
                     eval_params={"operator": "gt", "value": 3}, enabled=True),
        ])
        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            mock_call.side_effect = [
                ("42", 50, False),     # 42 > 3 → True
                ("2", 50, False),      # 2 > 100 → False
                ("not a number", 50, False),
            ]
            mock_check.return_value = (True, "")
            run = await run_evalset(project, evalset)
        results = {r.case_id: r for r in run.results}
        assert results["c1"].passed is True
        assert results["c2"].passed is False
        assert results["c3"].passed is False

    @pytest.mark.asyncio
    async def test_script_actual_only(self, sample_project, sample_evalset):
        """script 类型：仅用 actual，无 fields"""
        from app.models import EvalCase
        project = self._make_project(sample_project)
        evalset = self._make_evalset(sample_evalset, [
            EvalCase(id="c1", case_name="script-pass", input="hi", eval_type="script",
                     eval_params={"code": "len(actual) > 3"}, enabled=True),
            EvalCase(id="c2", case_name="script-fail", input="hi", eval_type="script",
                     eval_params={"code": "len(actual) > 10"}, enabled=True),
        ])
        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            mock_call.return_value = ("hello", 50, False)
            mock_check.return_value = (True, "")
            run = await run_evalset(project, evalset)
        results = {r.case_id: r for r in run.results}
        assert results["c1"].passed is True
        assert results["c2"].passed is False

    @pytest.mark.asyncio
    async def test_script_cross_field(self, sample_project, sample_evalset):
        """script 类型：跨字段判断 fields.result == "true" and len(fields.evidence) >= 2"""
        from app.models import EvalCase
        project = self._make_project(sample_project)
        code = 'fields.result == "true" and len(fields.evidence) >= 2'
        evalset = self._make_evalset(sample_evalset, [
            EvalCase(id="c1", case_name="cross-pass", input="hi", eval_type="script",
                     eval_params={"code": code}, enabled=True),
            EvalCase(id="c2", case_name="cross-short-evidence", input="hi", eval_type="script",
                     eval_params={"code": code}, enabled=True),
            EvalCase(id="c3", case_name="cross-fail-result", input="hi", eval_type="script",
                     eval_params={"code": code}, enabled=True),
        ])
        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            mock_call.side_effect = [
                # actual 是 JSON 对象，runner 会 _parse_fields_for_script 解析为 fields
                ('{"result": "true", "evidence": ["a", "b"]}', 50, False),
                ('{"result": "true", "evidence": ["a"]}', 50, False),  # evidence < 2 → 不过
                ('{"result": "false", "evidence": ["a", "b"]}', 50, False),  # result != true → 不过
            ]
            mock_check.return_value = (True, "")
            run = await run_evalset(project, evalset)
        results = {r.case_id: r for r in run.results}
        assert results["c1"].passed is True
        assert results["c2"].passed is False
        assert results["c3"].passed is False

    @pytest.mark.asyncio
    async def test_script_in_validations_cross_field(self, sample_project, sample_evalset):
        """script 作为 validations 子项（非主验证）走 cross-field 路径"""
        from app.models import EvalCase, EvalCheck
        project = self._make_project(sample_project)
        # 主验证：exact（主输出 == "ok"）
        # 子验证：script 跨字段
        evalset = self._make_evalset(sample_evalset, [
            EvalCase(
                id="c1", case_name="validations-script", input="hi", enabled=True,
                validations=[
                    EvalCheck(name="主输出", field="", eval_type="exact", expected="ok"),
                    EvalCheck(name="跨字段", field="", eval_type="script",
                              eval_params={"code": 'fields.score > 0.8'}),
                ],
            ),
        ])
        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            # actual 是 "ok"（非 JSON）→ fields 解析为空 dict → fields.score AttributeError → 不过
            mock_call.return_value = ("ok", 50, False)
            mock_check.return_value = (True, "")
            run = await run_evalset(project, evalset)
        r = run.results[0]
        # 主验证通过，子验证 script 因 fields.score 不存在 → 不过 → 整体不过
        assert r.passed is False
        # check_results 两条
        assert len(r.check_results) == 2
        assert r.check_results[0]["passed"] is True   # exact 主验证
        assert r.check_results[1]["eval_type"] == "script"
        assert r.check_results[1]["passed"] is False  # fields.score 不存在

    @pytest.mark.asyncio
    async def test_script_cross_field_with_explicit_field(self, sample_project, sample_evalset):
        """script + field="result"：主输出取字段值，fields 仍可访问其他字段"""
        from app.models import EvalCase, EvalCheck
        project = self._make_project(sample_project)
        evalset = self._make_evalset(sample_evalset, [
            EvalCase(
                id="c1", case_name="script-field-extract", input="hi", enabled=True,
                validations=[
                    EvalCheck(name="脚本", field="result", eval_type="script",
                              eval_params={"code": 'actual == "pass" and len(fields.evidence) >= 2'}),
                ],
            ),
        ])
        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            mock_call.return_value = ('{"result": "pass", "evidence": ["a", "b", "c"]}', 50, False)
            mock_check.return_value = (True, "")
            run = await run_evalset(project, evalset)
        r = run.results[0]
        # field="result" → actual = "pass"（提取后）→ actual=="pass" 且 fields.evidence 有 3 条 → 过
        assert r.passed is True

    @pytest.mark.asyncio
    async def test_script_security_rejected_in_runner(self, sample_project, sample_evalset):
        """runner 路径下 script 安全拒绝仍生效（__import__ 被拦）"""
        from app.models import EvalCase
        project = self._make_project(sample_project)
        evalset = self._make_evalset(sample_evalset, [
            EvalCase(id="c1", case_name="evil", input="hi", eval_type="script",
                     eval_params={"code": '__import__("os").system("echo pwned")'}, enabled=True),
        ])
        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            mock_call.return_value = ("anything", 50, False)
            mock_check.return_value = (True, "")
            run = await run_evalset(project, evalset)
        # 被拒绝 → 不过（不执行系统命令）
        assert run.results[0].passed is False

    @pytest.mark.asyncio
    async def test_script_statement_mode_runner(self, sample_project, sample_evalset):
        """R-4 语句集模式：if/else + 变量赋值 + 末行返回，走 runner 完整流程"""
        from app.models import EvalCase
        project = self._make_project(sample_project)
        code = (
            'ev_count = len(fields.evidence)\n'
            'if fields.confidence > 0.7:\n'
            '    ok = ev_count >= 2\n'
            'else:\n'
            '    ok = ev_count >= 4\n'
            'ok and fields.result == "true"'
        )
        evalset = self._make_evalset(sample_evalset, [
            EvalCase(id="c1", case_name="stmt-pass", input="hi", eval_type="script",
                     eval_params={"code": code}, enabled=True),
            EvalCase(id="c2", case_name="stmt-low-conf", input="hi", eval_type="script",
                     eval_params={"code": code}, enabled=True),
        ])
        with patch("app.runner.call_target", new_callable=AsyncMock) as mock_call, \
             patch("app.runner.check_judge_available", new_callable=AsyncMock) as mock_check:
            mock_call.side_effect = [
                # 高置信 + 2 条 evidence + result=true → True
                ('{"confidence": 0.9, "evidence": ["a", "b"], "result": "true"}', 50, False),
                # 低置信 + 2 条 evidence（需 >= 4）→ False
                ('{"confidence": 0.5, "evidence": ["a", "b"], "result": "true"}', 50, False),
            ]
            mock_check.return_value = (True, "")
            run = await run_evalset(project, evalset)
        results = {r.case_id: r for r in run.results}
        assert results["c1"].passed is True
        assert results["c2"].passed is False


