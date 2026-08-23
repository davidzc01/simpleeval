"""F 节（T1-1~T1-5）后端功能测试

覆盖：
- T1-1: 项目删除 + 评测集清空
- T1-2: case tags + case_filter 筛选 + 导入 tags
- T1-3: Judge 双模式（openai_compatible / custom）
- T1-4: judge_token 计入评测成本 + token_per_pass 新口径
- T1-5: EvalCase.checks 多字段验证
"""

import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from app.main import app
from fastapi.testclient import TestClient

from app.models import (
    Project, EvalSet, EvalCase, EvalCheck, CaseFilter,
    RunEvalRequest, EvalRun, ResponseParsing,
)
from app.runner import _apply_case_filter, _extract_check_field, _build_run_result, CaseResult


@pytest.fixture
def client():
    return TestClient(app)


# ============== T1-1: 项目删除 + 评测集清空 ==============

class TestProjectDelete:
    """T1-1: 项目删除（DELETE /api/projects/{pid}）"""

    def test_delete_project_success(self, client, tmp_path):
        """删除项目 → 项目 + 关联评测集 + runs 全部删除"""
        from app import storage
        storage.PROJECTS_DIR = tmp_path / "projects"
        storage.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        storage.EVALSETS_DIR = tmp_path / "evalsets"
        storage.EVALSETS_DIR.mkdir(parents=True, exist_ok=True)
        storage.RUNS_DIR = tmp_path / "runs"
        storage.RUNS_DIR.mkdir(parents=True, exist_ok=True)

        # 创建项目
        resp = client.post("/api/projects", json={"name": "删除测试", "task_shape": "general"})
        assert resp.status_code == 201
        pid = resp.json()["id"]

        # 创建评测集
        resp2 = client.post("/api/evalsets", json={
            "project_id": pid, "name": "测试集", "cases": []
        })
        assert resp2.status_code == 201

        # 删除项目
        resp3 = client.delete(f"/api/projects/{pid}")
        assert resp3.status_code == 200
        assert resp3.json()["deleted"] == pid

        # 确认项目不存在
        resp4 = client.get(f"/api/projects/{pid}")
        assert resp4.status_code == 404

    def test_delete_project_not_found(self, client):
        """删除不存在的项目 → 404"""
        resp = client.delete("/api/projects/nonexistent")
        assert resp.status_code == 404


class TestEvalsetClear:
    """T1-1 / B-22 修正版: 评测集清空（PUT cases: []）"""

    def test_clear_evalset_cases(self, client, tmp_path):
        """清空评测集 = PUT 全量 cases 为空数组"""
        from app import storage
        storage.PROJECTS_DIR = tmp_path / "projects"
        storage.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        storage.EVALSETS_DIR = tmp_path / "evalsets"
        storage.EVALSETS_DIR.mkdir(parents=True, exist_ok=True)

        # 创建项目 + 评测集
        resp = client.post("/api/projects", json={"name": "清空测试", "task_shape": "general"})
        pid = resp.json()["id"]
        resp2 = client.post("/api/evalsets", json={
            "project_id": pid, "name": "测试集",
            "cases": [{"id": "c1", "case_name": "c1", "input": "hi", "eval_type": "exact", "expected_output": "ok"}]
        })
        eid = resp2.json()["id"]

        # PUT cases: []
        resp3 = client.put(f"/api/evalsets/{eid}", json={
            "id": eid, "project_id": pid, "name": "测试集", "cases": []
        })
        assert resp3.status_code == 200
        assert resp3.json()["cases"] == []


# ============== T1-2: case tags + case_filter ==============

class TestCaseFilter:
    """T1-2: 按 tags 筛选 case"""

    def test_filter_no_tags_returns_all(self):
        """无 filter 或 tags 为空 → 返回全部"""
        cases = [
            EvalCase(id="c1", case_name="a", input="x", eval_type="exact", tags=["regression"]),
            EvalCase(id="c2", case_name="b", input="x", eval_type="exact", tags=[]),
        ]
        result = _apply_case_filter(cases, None)
        assert len(result) == 2

        result2 = _apply_case_filter(cases, CaseFilter(tags=[], mode="any"))
        assert len(result2) == 2

    def test_filter_any_mode(self):
        """mode=any → 含任一标签即入选（OR）"""
        cases = [
            EvalCase(id="c1", case_name="a", input="x", eval_type="exact", tags=["regression", "smoke"]),
            EvalCase(id="c2", case_name="b", input="x", eval_type="exact", tags=["smoke"]),
            EvalCase(id="c3", case_name="c", input="x", eval_type="exact", tags=["production"]),
        ]
        cf = CaseFilter(tags=["regression", "smoke"], mode="any")
        result = _apply_case_filter(cases, cf)
        assert len(result) == 2  # c1 + c2
        assert {c.id for c in result} == {"c1", "c2"}

    def test_filter_all_mode(self):
        """mode=all → 含全部标签才入选（AND）"""
        cases = [
            EvalCase(id="c1", case_name="a", input="x", eval_type="exact", tags=["regression", "smoke"]),
            EvalCase(id="c2", case_name="b", input="x", eval_type="exact", tags=["smoke"]),
            EvalCase(id="c3", case_name="c", input="x", eval_type="exact", tags=["production"]),
        ]
        cf = CaseFilter(tags=["regression", "smoke"], mode="all")
        result = _apply_case_filter(cases, cf)
        assert len(result) == 1  # only c1
        assert result[0].id == "c1"

    def test_filter_no_match(self):
        """标签不匹配 → 空列表"""
        cases = [
            EvalCase(id="c1", case_name="a", input="x", eval_type="exact", tags=["regression"]),
        ]
        cf = CaseFilter(tags=["production"], mode="any")
        result = _apply_case_filter(cases, cf)
        assert len(result) == 0

    def test_filter_case_with_empty_tags_excluded(self):
        """c.tags 为空列表时，无论 any/all 模式都应被过滤掉（不被误纳入结果）

        验证 `c.tags` 简化后（移除冗余 `or []`）行为不变：
        - `t in []` → False，all/any 推导都正确返回 False
        """
        cases = [
            EvalCase(id="c_empty", case_name="empty", input="x", eval_type="exact", tags=[]),
            EvalCase(id="c_match", case_name="match", input="x", eval_type="exact", tags=["smoke"]),
        ]
        # any 模式：空 tags 的 case 不含任何目标 tag，应被排除
        cf_any = CaseFilter(tags=["smoke"], mode="any")
        result_any = _apply_case_filter(cases, cf_any)
        assert {c.id for c in result_any} == {"c_match"}

        # all 模式：空 tags 的 case 同样不含目标 tag，应被排除
        cf_all = CaseFilter(tags=["smoke"], mode="all")
        result_all = _apply_case_filter(cases, cf_all)
        assert {c.id for c in result_all} == {"c_match"}

    def test_filter_case_no_tags(self):
        """case 无 tags 字段时按空列表处理"""
        cases = [
            EvalCase(id="c1", case_name="a", input="x", eval_type="exact"),
        ]
        cf = CaseFilter(tags=["regression"], mode="any")
        result = _apply_case_filter(cases, cf)
        assert len(result) == 0


class TestImportTags:
    """T1-2: 导入支持 tags 列"""

    def test_import_csv_with_tags_json(self, client, tmp_path):
        """CSV 导入含 tags 列（JSON 字符串数组）"""
        from app import storage
        storage.PROJECTS_DIR = tmp_path / "projects"
        storage.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        storage.EVALSETS_DIR = tmp_path / "evalsets"
        storage.EVALSETS_DIR.mkdir(parents=True, exist_ok=True)

        # 创建项目 + 评测集
        resp = client.post("/api/projects", json={"name": "tags导入", "task_shape": "general"})
        pid = resp.json()["id"]
        resp2 = client.post("/api/evalsets", json={
            "project_id": pid, "name": "测试集", "cases": []
        })
        eid = resp2.json()["id"]

        # JSON 数组格式导入（更可靠）
        json_content = json.dumps([{
            "case_name": "c1", "input": "hi", "eval_type": "exact",
            "expected_output": "ok", "tags": ["regression", "smoke"]
        }])
        resp3 = client.post(
            f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace",
            data={"file_content": json_content}
        )
        assert resp3.status_code == 200
        cases = resp3.json()["evalset"]["cases"]
        assert len(cases) == 1
        assert cases[0]["tags"] == ["regression", "smoke"]

    def test_import_csv_with_tags_comma(self, client, tmp_path):
        """CSV tags 列分号分隔"""
        from app import storage
        storage.PROJECTS_DIR = tmp_path / "projects"
        storage.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        storage.EVALSETS_DIR = tmp_path / "evalsets"
        storage.EVALSETS_DIR.mkdir(parents=True, exist_ok=True)

        resp = client.post("/api/projects", json={"name": "tags逗号", "task_shape": "general"})
        pid = resp.json()["id"]
        resp2 = client.post("/api/evalsets", json={
            "project_id": pid, "name": "测试集", "cases": []
        })
        eid = resp2.json()["id"]

        # 分号分隔避免 CSV 逗号冲突
        csv_content = "case_name,input,eval_type,expected_output,tags\nc1,hi,exact,ok,regression;smoke"
        resp3 = client.post(
            f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace",
            data={"file_content": csv_content}
        )
        assert resp3.status_code == 200
        cases = resp3.json()["evalset"]["cases"]
        assert set(cases[0]["tags"]) == {"regression", "smoke"}


class TestRunEvalWithFilter:
    """T1-2: POST /api/runs 支持 case_filter"""

    def test_run_with_case_filter(self, client, tmp_path):
        """发起评测带 case_filter"""
        from app import storage
        storage.PROJECTS_DIR = tmp_path / "projects"
        storage.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        storage.EVALSETS_DIR = tmp_path / "evalsets"
        storage.EVALSETS_DIR.mkdir(parents=True, exist_ok=True)
        storage.RUNS_DIR = tmp_path / "runs"
        storage.RUNS_DIR.mkdir(parents=True, exist_ok=True)

        resp = client.post("/api/projects", json={"name": "filter评测", "task_shape": "general"})
        pid = resp.json()["id"]
        resp2 = client.post("/api/evalsets", json={
            "project_id": pid, "name": "测试集",
            "cases": [
                {"id": "c1", "case_name": "a", "input": "hi", "eval_type": "exact", "expected_output": "ok", "tags": ["regression"]},
                {"id": "c2", "case_name": "b", "input": "hi", "eval_type": "exact", "expected_output": "ok", "tags": ["smoke"]},
            ]
        })
        eid = resp2.json()["id"]

        with patch("app.routes.execute_run", new_callable=AsyncMock) as mock_exec:
            resp3 = client.post("/api/runs", json={
                "project_id": pid, "evalset_id": eid,
                "case_filter": {"tags": ["regression"], "mode": "any"}
            })
            assert resp3.status_code == 201
            # execute_run 应被调用，第 4 个参数是 case_filter
            call_args = mock_exec.call_args
            assert call_args is not None

    def test_run_with_filter_no_match_422(self, client, tmp_path):
        """标签不匹配任何 case → 422 no_enabled_cases"""
        from app import storage
        storage.PROJECTS_DIR = tmp_path / "projects"
        storage.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        storage.EVALSETS_DIR = tmp_path / "evalsets"
        storage.EVALSETS_DIR.mkdir(parents=True, exist_ok=True)

        resp = client.post("/api/projects", json={"name": "无匹配", "task_shape": "general"})
        pid = resp.json()["id"]
        resp2 = client.post("/api/evalsets", json={
            "project_id": pid, "name": "测试集",
            "cases": [
                {"id": "c1", "case_name": "a", "input": "hi", "eval_type": "exact", "expected_output": "ok", "tags": ["regression"]},
            ]
        })
        eid = resp2.json()["id"]

        resp3 = client.post("/api/runs", json={
            "project_id": pid, "evalset_id": eid,
            "case_filter": {"tags": ["production"], "mode": "any"}
        })
        assert resp3.status_code == 422


# ============== T1-3: Judge 双模式 ==============

class TestJudgeDualMode:
    """T1-3: judge_with_llm 双模式"""

    @pytest.mark.asyncio
    async def test_judge_openai_compatible_returns_tuple(self):
        """openai_compatible 模式返回 (score, token)"""
        from app.judge import judge_with_llm
        mock_response = {
            "choices": [{"message": {"content": "0.9"}}],
            "usage": {"total_tokens": 42}
        }
        with patch("httpx.AsyncClient") as mock_client:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_response
            mock_resp.raise_for_status = MagicMock()
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)

            score, token, _raw = await judge_with_llm(
                base_url="https://api.example.com/v1",
                api_key="key", model="gpt-4o-mini",
                requirement="测试", output="输出",
            )
            assert score == 0.9
            assert token == 42

    @pytest.mark.asyncio
    async def test_judge_custom_mode(self):
        """custom 模式用 request_template + response_parsing 提取分数"""
        from app.judge import judge_with_llm
        mock_response = {
            "data": {"score": "0.75"},
            "usage": {"total_tokens": 15}
        }
        with patch("httpx.AsyncClient") as mock_client:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_response
            mock_resp.raise_for_status = MagicMock()
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)

            score, token, _raw = await judge_with_llm(
                base_url="https://api.example.com/api",
                api_key="key", model="",
                requirement="测试", output="输出",
                api_type="custom",
                request_template='{"query":"{input}"}',
                response_parsing=ResponseParsing(
                    output_paths=["$.data.score"],
                    token_paths=["$.usage.total_tokens"],
                ),
            )
            assert score == 0.75
            assert token == 15

    @pytest.mark.asyncio
    async def test_judge_custom_no_template_raises(self):
        """custom 模式无 request_template → ResponseFormatError"""
        from app.judge import judge_with_llm, ResponseFormatError
        with pytest.raises(ResponseFormatError):
            await judge_with_llm(
                base_url="https://api.example.com/api",
                api_key="key", model="",
                requirement="测试", output="输出",
                api_type="custom",
                request_template=None,
            )

    @pytest.mark.asyncio
    async def test_judge_custom_no_response_parsing_raises(self):
        """T1-3: custom 模式无 response_parsing → ResponseFormatError（兜底校验）

        custom 模式假定用户自定义 API，响应不一定遵循 OpenAI 格式，
        若 response_parsing 缺失，原代码会 fallback 到 OpenAI 默认解析（错误结果或模糊错误）。
        前置校验给清晰错误信息。
        """
        from app.judge import judge_with_llm, ResponseFormatError
        with pytest.raises(ResponseFormatError, match="custom 模式 response_parsing 必填"):
            await judge_with_llm(
                base_url="https://api.example.com/api",
                api_key="key", model="",
                requirement="测试", output="输出",
                api_type="custom",
                request_template='{"q": "{input}"}',
                response_parsing=None,
            )

    @pytest.mark.asyncio
    async def test_judge_openai_compat_no_model_raises(self):
        """T1-3: openai_compatible 模式无 model → ResponseFormatError（兜底校验）"""
        from app.judge import judge_with_llm, ResponseFormatError
        with pytest.raises(ResponseFormatError, match="openai_compatible 模式 model 必填"):
            await judge_with_llm(
                base_url="https://api.example.com/api",
                api_key="key", model="",
                requirement="测试", output="输出",
                api_type="openai_compatible",
            )

    @pytest.mark.asyncio
    async def test_judge_openai_compat_blank_model_raises(self):
        """T1-3: openai_compatible 模式 model 为空白字符串 → ResponseFormatError"""
        from app.judge import judge_with_llm, ResponseFormatError
        with pytest.raises(ResponseFormatError, match="openai_compatible 模式 model 必填"):
            await judge_with_llm(
                base_url="https://api.example.com/api",
                api_key="key", model="   ",
                requirement="测试", output="输出",
                api_type="openai_compatible",
            )


class TestTestJudgeDualMode:
    """T1-3: POST /api/test/judge 双模式"""

    def test_test_judge_custom_mode(self, client):
        """test/judge 支持 custom 模式"""
        with patch("app.routes.judge_with_llm", new_callable=AsyncMock) as mock_judge:
            mock_judge.return_value = (0.7, 12, "raw judge output")
            resp = client.post("/api/test/judge", json={
                "base_url": "https://api.example.com/api",
                "api_key": "key",
                "model": "",
                "prompt_template": "判断",
                "input": "hi",
                "output_requirement": "ok",
                "actual_output": "ok",
                "api_type": "custom",
                "request_template": '{"query":"{input}"}',
                "response_parsing": {"output_paths": ["$.data.score"]},
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["score"] == 0.7
            assert data["token_used"] == 12


# ============== T1-4: judge_token ==============

class TestJudgeToken:
    """T1-4: Judge token 计入评测成本"""

    def test_summary_has_judge_token(self):
        """EvalSummary 含 judge_token 字段"""
        results = [
            CaseResult(case_name="c1", actual_output="ok", passed=True, token_used=100, judge_token=30),
            CaseResult(case_name="c2", actual_output="ok", passed=True, token_used=50, judge_token=20),
        ]
        run = _build_run_result("run-1", "p1", "e1", results)
        assert run.summary.judge_token == 50

    def test_token_per_pass_includes_judge_token(self):
        """token_per_pass = 通过数 / ((target_token + judge_token)/10000)"""
        results = [
            CaseResult(case_name="c1", actual_output="ok", passed=True, token_used=100, judge_token=100),
        ]
        run = _build_run_result("run-1", "p1", "e1", results)
        # passed=1, total_token=100, judge_token=100, cost=200
        # token_per_pass = 1 / (200/10000) = 1 / 0.02 = 50
        assert run.summary.token_per_pass == 50.0

    def test_judge_token_zero_for_rule_based(self):
        """规则类 case judge_token=0"""
        results = [
            CaseResult(case_name="c1", actual_output="ok", passed=True, token_used=100, judge_token=0),
        ]
        run = _build_run_result("run-1", "p1", "e1", results)
        assert run.summary.judge_token == 0

    def test_last_run_summary_has_judge_token(self, client, tmp_path):
        """last_run 摘要含 judge_token"""
        from app import storage
        storage.PROJECTS_DIR = tmp_path / "projects"
        storage.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        storage.EVALSETS_DIR = tmp_path / "evalsets"
        storage.EVALSETS_DIR.mkdir(parents=True, exist_ok=True)
        storage.RUNS_DIR = tmp_path / "runs"
        storage.RUNS_DIR.mkdir(parents=True, exist_ok=True)

        resp = client.post("/api/projects", json={"name": "jt", "task_shape": "general"})
        pid = resp.json()["id"]

        # 直接造一个 run 文件
        run = EvalRun(
            id="run-jt-1", project_id=pid, evalset_id="e1",
            status="completed", created_at="2026-01-01T00:00:00Z",
            results=[CaseResult(case_name="c1", actual_output="ok", passed=True, token_used=100, judge_token=30)],
            summary={
                "pass_rate": 1.0, "total_token": 100, "total_latency_ms": 100.0,
                "token_per_pass": 50.0, "latency_p50": 100.0, "latency_p95": 100.0,
                "judge_token": 30,
            }
        )
        storage.save_run(run)

        resp2 = client.get(f"/api/projects/{pid}")
        assert resp2.status_code == 200
        last_run = resp2.json().get("last_run", {})
        assert last_run["judge_token"] == 30


# ============== T1-5: EvalCase.checks 多字段验证 ==============

class TestExtractCheckField:
    """T1-5: _extract_check_field 从 actual_output 提取字段"""

    def test_empty_field_returns_original(self):
        """field 为空 → 返回原文"""
        assert _extract_check_field("hello", "") == "hello"

    def test_extract_from_json(self):
        """从 JSON 字符串提取字段"""
        actual = json.dumps({"result": True, "evidence": "支持退款"})
        assert _extract_check_field(actual, "result") == "true"
        assert _extract_check_field(actual, "evidence") == "支持退款"

    def test_extract_nested_field(self):
        """嵌套点路径"""
        actual = json.dumps({"a": {"b": {"c": "hello"}}})
        assert _extract_check_field(actual, "a.b.c") == "hello"

    def test_non_json_returns_original(self):
        """非 JSON → 返回原文"""
        assert _extract_check_field("plain text", "result") == "plain text"

    def test_field_not_found_returns_original(self):
        """路径未命中 → 返回原文"""
        actual = json.dumps({"result": True})
        assert _extract_check_field(actual, "missing") == actual

    def test_extract_number_stringified(self):
        """数字 → stringify"""
        actual = json.dumps({"score": 42})
        assert _extract_check_field(actual, "score") == "42"

    def test_extract_list_index_path(self):
        """list 下标路径（items.0.name）—— 修复点路径取值容错性不足

        旧实现遇 list 直接返回原文；新实现遇 list + 数字段则按下标取值。
        """
        actual = json.dumps({"items": [{"name": "first"}, {"name": "second"}]})
        assert _extract_check_field(actual, "items.0.name") == "first"
        assert _extract_check_field(actual, "items.1.name") == "second"

    def test_extract_list_index_out_of_range(self):
        """list 下标越界 → 返回原文兜底"""
        actual = json.dumps({"items": [{"name": "first"}]})
        assert _extract_check_field(actual, "items.5.name") == actual

    def test_extract_list_with_non_digit_segment(self):
        """list + 非数字段 → 返回原文（不能取值）"""
        actual = json.dumps({"items": [{"name": "first"}]})
        assert _extract_check_field(actual, "items.foo.name") == actual

    def test_extract_nested_list_dict_mix(self):
        """混合嵌套：dict → list → dict → 标量"""
        actual = json.dumps({"data": [{"scores": [10, 20, 30]}]})
        assert _extract_check_field(actual, "data.0.scores.1") == "20"

    def test_extract_dict_to_json(self):
        """dict → json.dumps"""
        actual = json.dumps({"data": {"x": 1}})
        result = _extract_check_field(actual, "data")
        assert json.loads(result) == {"x": 1}


class TestChecksEvaluation:
    """T1-5: case.checks 多字段验证"""

    def test_case_passes_with_all_checks_pass(self):
        """主验证 + 所有 checks 通过 → passed=True"""
        from app.runner import _evaluate_case
        from app.models import Project, JudgeConfig, TargetConfig
        project = Project(
            id="p1", name="test", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
        )
        # 主验证用 contains（actual 是 JSON，检查是否含 "退款"）
        case = EvalCase(
            id="c1", case_name="test", input="hi",
            eval_type="contains", eval_params={"substring": "退款"},
            checks=[
                EvalCheck(name="check1", field="result", eval_type="exact", expected="true"),
                EvalCheck(name="check2", field="evidence", eval_type="contains", eval_params={"substring": "退款"}),
            ]
        )
        actual = json.dumps({"result": True, "evidence": "支持退款"}, ensure_ascii=False)

        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            _evaluate_case(project, case, actual, 0, False, True, "")
        )
        passed, score, skipped, jt, checks, _ = result
        assert passed is True
        assert len(checks) == 3  # 主验证 + 2 checks
        assert all(c["passed"] for c in checks)

    def test_case_fails_when_check_fails(self):
        """主验证通过但 check 失败 → passed=False"""
        from app.runner import _evaluate_case
        from app.models import Project, JudgeConfig, TargetConfig
        project = Project(
            id="p1", name="test", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
        )
        case = EvalCase(
            id="c1", case_name="test", input="hi",
            eval_type="contains", eval_params={"substring": "退款"},
            checks=[
                EvalCheck(name="check1", field="result", eval_type="exact", expected="true"),
                EvalCheck(name="check2", field="evidence", eval_type="contains", eval_params={"substring": "不存在的词"}),
            ]
        )
        actual = json.dumps({"result": True, "evidence": "支持退款"}, ensure_ascii=False)

        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            _evaluate_case(project, case, actual, 0, False, True, "")
        )
        passed, score, skipped, jt, checks, _ = result
        assert passed is False
        assert len(checks) == 3  # 主验证 + 2 checks
        assert checks[0]["passed"] is True  # 主验证通过
        assert checks[1]["passed"] is True  # check1 通过
        assert checks[2]["passed"] is False  # check2 失败

    def test_case_without_checks_behaves_normal(self):
        """无 checks → 行为不变"""
        from app.runner import _evaluate_case
        from app.models import Project, JudgeConfig, TargetConfig
        project = Project(
            id="p1", name="test", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="", api_key="", model=None),
        )
        case = EvalCase(
            id="c1", case_name="test", input="hi",
            eval_type="exact", expected_output="ok",
        )
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            _evaluate_case(project, case, "ok", 0, False, True, "")
        )
        passed, score, skipped, jt, checks, _ = result
        assert passed is True
        assert len(checks) == 1  # 仅主验证

    def test_check_results_in_case_result(self):
        """CaseResult 含 check_results"""
        cr = CaseResult(
            case_name="c1", actual_output="ok", passed=True,
            check_results=[{"name": "chk1", "passed": True, "score": 1.0}],
        )
        assert len(cr.check_results) == 1
        assert cr.check_results[0]["name"] == "chk1"

    def test_multiple_llm_judge_checks_accumulate_tokens(self):
        """T1-4 回归（Issue1）：多个 llm_judge check 的 token 都应正确累加

        Issue1 报告 _ 变量名会导致累加错误值；改名 chk_judge_token 后，
        每个 llm_judge check 调 judge_with_llm 返回的 token 都被正确加到 judge_token。
        """
        from app.runner import _evaluate_case
        from app.models import Project, JudgeConfig, TargetConfig
        from unittest.mock import AsyncMock, patch

        project = Project(
            id="p1", name="test", task_shape="general",
            judge_config=JudgeConfig(
                base_url="https://j.example.com", api_key="k",
                model="gpt-4-judge", api_type="openai_compatible",
            ),
            target_config=TargetConfig(base_url="", api_key="", model=None),
        )
        case = EvalCase(
            id="c1", case_name="test", input="hi",
            eval_type="contains", eval_params={"substring": "x"},
            checks=[
                EvalCheck(name="j1", field="result", eval_type="llm_judge"),
                EvalCheck(name="j2", field="evidence", eval_type="llm_judge"),
                EvalCheck(name="j3", field="summary", eval_type="llm_judge"),
            ]
        )
        actual = json.dumps({"result": "ok", "evidence": "ok", "summary": "ok", "msg": "x marks the spot"})

        # mock judge_with_llm：3 次调用分别返回不同的 token（11, 22, 33），合计应 66
        call_count = [0]
        async def fake_judge(**kwargs):
            call_count[0] += 1
            return (0.9, [11, 22, 33][call_count[0] - 1], "judge raw response")

        import asyncio
        with patch("app.runner.judge_with_llm", new=fake_judge):
            result = asyncio.get_event_loop().run_until_complete(
                _evaluate_case(project, case, actual, 0, True, True, "")
            )
        passed, score, skipped, jt, checks, _ = result
        # 3 个 check 都通过（score 0.9 >= JUDGE_THRESHOLD=0.5）
        assert all(c["passed"] for c in checks), f"checks: {checks}"
        # Issue1 关键断言：judge_token = 11 + 22 + 33 = 66（而非旧 _ 变量名可能产生的错误值）
        assert jt == 66, f"judge_token 累加错误：实际 {jt}，期望 66"


class TestEvalCheckModel:
    """T1-5: EvalCheck 模型"""

    def test_evalcheck_creation(self):
        """EvalCheck 基本创建"""
        ec = EvalCheck(name="test", field="result", eval_type="exact", expected="true")
        assert ec.name == "test"
        assert ec.field == "result"
        assert ec.eval_type == "exact"

    def test_evalcase_with_checks(self):
        """EvalCase 含 checks 字段"""
        case = EvalCase(
            id="c1", case_name="test", input="hi", eval_type="exact",
            checks=[EvalCheck(name="chk", field="result", eval_type="exact", expected="true")]
        )
        assert case.checks is not None
        assert len(case.checks) == 1
