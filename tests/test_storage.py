"""storage 模块单元测试"""

import pytest
from pathlib import Path
import tempfile
import shutil

from app.models import Project, EvalSet, EvalCase, EvalRun, CaseResult, EvalSummary
from app.storage import (
    save_project, get_project, list_projects,
    save_evalset, get_evalset, list_evalsets, get_evalset_dir,
    save_run, get_run, list_runs, get_run_dir,
    get_project_last_run, get_project_trend,
)


class TestProjectStorage:
    """项目存储测试"""

    def test_save_and_get_project(self, setup_test_env):
        """保存和获取项目"""
        project = Project(
            id="proj-test-001",
            name="测试项目",
            task_shape="general",
            judge_config={"base_url": "https://api.example.com", "api_key": "key", "model": "gpt-4"},
            target_config={"base_url": "https://api.example.com", "api_key": "key", "model": "gpt-3.5"},
        )

        save_project(project)
        result = get_project("proj-test-001")

        assert result is not None
        assert result.id == "proj-test-001"
        assert result.name == "测试项目"

    def test_get_nonexistent_project(self, setup_test_env):
        """获取不存在的项目"""
        result = get_project("nonexistent")
        assert result is None

    def test_list_projects(self, setup_test_env):
        """列出所有项目"""
        # 创建多个项目
        for i in range(3):
            project = Project(
                id=f"proj-{i}",
                name=f"项目{i}",
                task_shape="general",
                judge_config={"base_url": "", "api_key": "", "model": ""},
                target_config={"base_url": "", "api_key": "", "model": ""},
            )
            save_project(project)

        projects = list_projects()
        assert len(projects) == 3


class TestEvalSetStorage:
    """评测集存储测试"""

    def test_save_and_get_evalset(self, setup_test_env):
        """保存和获取评测集"""
        # 先创建项目
        project = Project(
            id="proj-test-001",
            name="测试项目",
            task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        )
        save_project(project)

        evalset = EvalSet(
            id="evalset-test-001",
            project_id="proj-test-001",
            name="测试评测集",
            cases=[
                EvalCase(
                    id="case-1",
                    case_name="测试case",
                    input="你好",
                    expected_output="你好！",
                    eval_type="exact",
                )
            ]
        )

        save_evalset(evalset)
        result = get_evalset("evalset-test-001", "proj-test-001")

        assert result is not None
        assert result.id == "evalset-test-001"
        assert len(result.cases) == 1

    def test_get_nonexistent_evalset(self, setup_test_env):
        """获取不存在的评测集"""
        result = get_evalset("nonexistent", "proj-test-001")
        assert result is None

    def test_list_evalsets_by_project(self, setup_test_env):
        """按项目列出评测集"""
        # 创建项目
        project = Project(
            id="proj-test-001",
            name="测试项目",
            task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        )
        save_project(project)

        # 创建多个评测集
        for i in range(3):
            evalset = EvalSet(
                id=f"evalset-{i}",
                project_id="proj-test-001",
                name=f"评测集{i}",
                cases=[]
            )
            save_evalset(evalset)

        evalsets = list_evalsets(project_id="proj-test-001")
        assert len(evalsets) == 3

    def test_list_all_evalsets(self, setup_test_env):
        """列出所有评测集"""
        # 创建多个项目及其评测集
        for i in range(2):
            project = Project(
                id=f"proj-{i}",
                name=f"项目{i}",
                task_shape="general",
                judge_config={"base_url": "", "api_key": "", "model": ""},
                target_config={"base_url": "", "api_key": "", "model": ""},
            )
            save_project(project)

            evalset = EvalSet(
                id=f"evalset-{i}",
                project_id=f"proj-{i}",
                name=f"评测集{i}",
                cases=[]
            )
            save_evalset(evalset)

        evalsets = list_evalsets()
        assert len(evalsets) == 2


class TestRunStorage:
    """Run 存储测试"""

    def test_save_and_get_run(self, setup_test_env):
        """保存和获取 run"""
        run = EvalRun(
            id="run-test-001",
            project_id="proj-test-001",
            evalset_id="evalset-test-001",
            status="completed",
            created_at="2024-01-01T00:00:00Z",
            results=[],
            summary=EvalSummary(
                pass_rate=0.8,
                total_token=1000,
                total_latency_ms=5000,
                token_per_pass=8.0,
                latency_p50=500,
                latency_p95=1000,
            )
        )

        save_run(run)
        result = get_run("run-test-001", "proj-test-001")

        assert result is not None
        assert result.id == "run-test-001"
        assert result.summary.pass_rate == 0.8

    def test_get_nonexistent_run(self, setup_test_env):
        """获取不存在的 run"""
        result = get_run("nonexistent", "proj-test-001")
        assert result is None

    def test_list_runs_by_project(self, setup_test_env):
        """按项目列出 runs"""
        # 创建项目
        project = Project(
            id="proj-test-001",
            name="测试项目",
            task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        )
        save_project(project)

        # 创建多个 runs
        for i in range(3):
            run = EvalRun(
                id=f"run-{i}",
                project_id="proj-test-001",
                evalset_id="evalset-001",
                status="completed",
                created_at=f"2024-01-0{i+1}T00:00:00Z",
            )
            save_run(run)

        runs = list_runs(project_id="proj-test-001")
        assert len(runs) == 3

    def test_list_runs_sorted(self, setup_test_env):
        """runs 按时间倒序"""
        project = Project(
            id="proj-test-001",
            name="测试项目",
            task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        )
        save_project(project)

        # 按时间顺序创建
        for i in range(3):
            run = EvalRun(
                id=f"run-{i}",
                project_id="proj-test-001",
                evalset_id="evalset-001",
                status="completed",
                created_at=f"2024-01-0{i+1}T00:00:00Z",
            )
            save_run(run)

        runs = list_runs(project_id="proj-test-001")
        # 应该倒序：最新的在前面
        assert runs[0].id == "run-2"
        assert runs[-1].id == "run-0"


class TestProjectTrend:
    """项目趋势测试"""

    def test_get_project_trend(self, setup_test_env):
        """获取项目趋势"""
        project = Project(
            id="proj-test-001",
            name="测试项目",
            task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        )
        save_project(project)

        # 创建多个 runs
        for i in range(5):
            run = EvalRun(
                id=f"run-{i}",
                project_id="proj-test-001",
                evalset_id="evalset-001",
                status="completed",
                created_at=f"2024-01-0{i+1}T00:00:00Z",
                summary=EvalSummary(
                    pass_rate=0.5 + i * 0.1,
                    total_token=1000,
                    total_latency_ms=5000,
                    token_per_pass=5.0,
                    latency_p50=500,
                    latency_p95=1000,
                )
            )
            save_run(run)

        trend = get_project_trend("proj-test-001", limit=8)
        assert len(trend) == 5
        # 时间正序（旧→新）：最早的 run-0 在前，最近的 run-4 在最后
        assert trend[0]["run_id"] == "run-0"
        assert trend[-1]["run_id"] == "run-4"

    def test_get_project_trend_limit(self, setup_test_env):
        """趋势数据限制"""
        project = Project(
            id="proj-test-001",
            name="测试项目",
            task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        )
        save_project(project)

        # 创建 10 个 runs
        for i in range(10):
            run = EvalRun(
                id=f"run-{i}",
                project_id="proj-test-001",
                evalset_id="evalset-001",
                status="completed",
                created_at=f"2024-01-0{i+1}T00:00:00Z",
                summary=EvalSummary(
                    pass_rate=0.5,
                    total_token=1000,
                    total_latency_ms=5000,
                    token_per_pass=5.0,
                    latency_p50=500,
                    latency_p95=1000,
                )
            )
            save_run(run)

        # 限制为 5
        trend = get_project_trend("proj-test-001", limit=5)
        assert len(trend) == 5

    def test_get_project_last_run(self, setup_test_env):
        """获取最近一次 run"""
        project = Project(
            id="proj-test-001",
            name="测试项目",
            task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        )
        save_project(project)

        # 创建 runs
        for i in range(3):
            run = EvalRun(
                id=f"run-{i}",
                project_id="proj-test-001",
                evalset_id="evalset-001",
                status="completed",
                created_at=f"2024-01-0{i+1}T00:00:00Z",
            )
            save_run(run)

        last_run = get_project_last_run("proj-test-001")
        assert last_run is not None
        assert last_run.id == "run-2"  # 最新的是 run-2

    def test_get_project_last_run_none(self, setup_test_env):
        """没有 run 时返回 None"""
        project = Project(
            id="proj-test-001",
            name="测试项目",
            task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        )
        save_project(project)

        last_run = get_project_last_run("proj-test-001")
        assert last_run is None
