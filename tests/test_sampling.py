"""采样稳定性（pass@k / pass^k）单元 + 集成测试"""

import pytest
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models import EvalRun, CaseResult, EvalSummary
from app.sampling import (
    comb,
    pass_at_k_case,
    pass_pow_k_case,
    _aggregate_runs,
    compute_project_sampling,
    K_VALUES,
)


# ============== comb ==============

class TestComb:
    def test_basic(self):
        assert comb(5, 2) == 10
        assert comb(10, 3) == 120

    def test_k_zero(self):
        """C(n, 0) = 1"""
        assert comb(5, 0) == 1
        assert comb(0, 0) == 1

    def test_k_eq_n(self):
        """C(n, n) = 1"""
        assert comb(5, 5) == 1
        assert comb(1, 1) == 1

    def test_k_gt_n(self):
        """k > n 返回 0"""
        assert comb(3, 5) == 0

    def test_negative(self):
        """负数返回 0"""
        assert comb(-1, 2) == 0
        assert comb(5, -1) == 0


# ============== pass_at_k_case ==============

class TestPassAtK:
    def test_all_pass(self):
        """全部通过 → pass@k = 1（k 次 sampling 至少一次通过的概率为 1）"""
        # n=5, c=5, k=2: 1 - C(0,2)/C(5,2) = 1 - 0/10 = 1
        assert pass_at_k_case(5, 5, 2) == 1.0

    def test_all_fail(self):
        """全部失败 → pass@k = 0"""
        # n=5, c=0, k=2: 1 - C(5,2)/C(5,2) = 1 - 1 = 0
        assert pass_at_k_case(5, 0, 2) == 0.0

    def test_n_less_than_k(self):
        """n < k 无法计算"""
        assert pass_at_k_case(2, 1, 3) is None
        assert pass_at_k_case(0, 0, 1) is None

    def test_partial(self):
        """部分通过"""
        # n=4, c=2, k=2: 1 - C(2,2)/C(4,2) = 1 - 1/6 = 5/6 ≈ 0.833
        result = pass_at_k_case(4, 2, 2)
        assert result is not None
        assert abs(result - (1 - 1 / 6)) < 1e-9

    def test_k1_pass_at(self):
        """k=1 时 pass@k = c/n（单次采样通过概率）"""
        # n=4, c=3, k=1: 1 - C(1,1)/C(4,1) = 1 - 1/4 = 3/4
        assert pass_at_k_case(4, 3, 1) == 0.75

    def test_range(self):
        """结果在 [0, 1] 区间内"""
        for n in range(1, 8):
            for c in range(0, n + 1):
                for k in range(1, n + 1):
                    v = pass_at_k_case(n, c, k)
                    assert v is not None
                    assert 0.0 <= v <= 1.0


# ============== pass_pow_k_case ==============

class TestPassPowK:
    def test_all_pass(self):
        """全部通过 → pass^k = 1"""
        # n=5, c=5, k=2: C(5,2)/C(5,2) = 1
        assert pass_pow_k_case(5, 5, 2) == 1.0

    def test_all_fail(self):
        """全部失败 → pass^k = 0"""
        # n=5, c=0, k=2: C(0,2)/C(5,2) = 0/10 = 0
        assert pass_pow_k_case(5, 0, 2) == 0.0

    def test_c_less_than_k(self):
        """c < k → C(c, k) = 0 → pass^k = 0"""
        # n=5, c=1, k=2: C(1,2)=0 → 0
        assert pass_pow_k_case(5, 1, 2) == 0.0

    def test_n_less_than_k(self):
        """n < k 无法计算"""
        assert pass_pow_k_case(2, 2, 3) is None

    def test_partial(self):
        """部分通过"""
        # n=4, c=3, k=2: C(3,2)/C(4,2) = 3/6 = 0.5
        assert pass_pow_k_case(4, 3, 2) == 0.5

    def test_k1_pow(self):
        """k=1 时 pass^k = c/n（与 pass@k 相等）"""
        assert pass_pow_k_case(4, 3, 1) == 0.75


# ============== pass@k >= pass^k 不变量 ==============

class TestInvariant:
    def test_at_ge_pow(self):
        """pass@k 永远 ≥ pass^k（数学不变量，k=1 时两者相等）"""
        eps = 1e-9
        for n in range(1, 10):
            for c in range(0, n + 1):
                for k in range(1, n + 1):
                    at = pass_at_k_case(n, c, k)
                    pow_v = pass_pow_k_case(n, c, k)
                    assert at is not None and pow_v is not None
                    assert at >= pow_v - eps, f"pass@k < pass^k at n={n} c={c} k={k}: {at} < {pow_v}"


# ============== _aggregate_runs ==============

def _mkrun(rid, pid, evalset_id, case_results, status="completed"):
    """构造测试用 run"""
    return EvalRun(
        id=rid, project_id=pid, evalset_id=evalset_id,
        status=status, created_at="2026-08-18T10:00:00Z",
        results=case_results,
        summary=EvalSummary(
            pass_rate=sum(1 for r in case_results if r.passed) / max(len(case_results), 1),
            total_token=0, total_latency_ms=0,
            token_per_pass=0, latency_p50=0, latency_p95=0,
        ),
    )


class TestAggregateRuns:
    def setup_method(self):
        self.pid = "proj-aggregate"

    def test_empty(self):
        assert _aggregate_runs([]) == {}

    def test_skips_non_completed(self):
        """running / queued 的 run 不计入"""
        cr = [CaseResult(case_name="c1", actual_output="o", passed=True, score=1.0)]
        runs = [
            _mkrun("r1", self.pid, "es", cr, status="running"),
            _mkrun("r2", self.pid, "es", cr, status="queued"),
            _mkrun("r3", self.pid, "es", cr, status="failed"),
        ]
        # 全是非 completed，没有记录
        assert _aggregate_runs(runs) == {}

    def test_completed_only(self):
        cr = [CaseResult(case_name="c1", actual_output="o", passed=True, score=1.0)]
        run = _mkrun("r1", self.pid, "es", cr, status="completed")
        result = _aggregate_runs([run])
        assert result == {"c1": [True]}

    def test_skipped_case_excluded_from_n(self):
        """skipped 的 case 不计入 n（采样数）"""
        cr1 = [
            CaseResult(case_name="c1", actual_output="o", passed=True, score=1.0),
            CaseResult(case_name="c2", actual_output="[SKIPPED]", passed=False, score=0.0, skipped_reason="llm_unavailable"),
        ]
        cr2 = [
            CaseResult(case_name="c1", actual_output="o", passed=False, score=0.0),
            CaseResult(case_name="c2", actual_output="ok", passed=True, score=1.0),
        ]
        runs = [_mkrun("r1", self.pid, "es", cr1), _mkrun("r2", self.pid, "es", cr2)]
        result = _aggregate_runs(runs)
        # c1 在两次 run 都出现且未跳过 → n=2
        assert result["c1"] == [True, False]
        # c2 第一次跳过不计，第二次计入 → n=1
        assert result["c2"] == [True]

    def test_dedup_same_name_in_one_run(self):
        """同一 run 内同名 case 只取第一条（防御重复）"""
        cr = [
            CaseResult(case_name="c1", actual_output="o1", passed=True, score=1.0),
            CaseResult(case_name="c1", actual_output="o2", passed=False, score=0.0),  # 应被忽略
        ]
        run = _mkrun("r1", self.pid, "es", cr)
        result = _aggregate_runs([run])
        assert result["c1"] == [True]  # 只取第一条


# ============== compute_project_sampling ==============

DATA_DIR = Path(__file__).parent.parent / "data"


@pytest.fixture
def isolated_storage():
    """每个测试用独立的数据目录，避免互相污染"""
    import app.storage as storage
    original = storage.DATA_DIR
    test_dir = Path(__file__).parent.parent / "data_test_sampling"
    storage.DATA_DIR = test_dir
    storage.PROJECTS_DIR = test_dir / "projects"
    storage.EVALSETS_DIR = test_dir / "evalsets"
    storage.RUNS_DIR = test_dir / "runs"
    storage.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    storage.EVALSETS_DIR.mkdir(parents=True, exist_ok=True)
    storage.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    yield test_dir
    storage.DATA_DIR = original
    storage.PROJECTS_DIR = original / "projects"
    storage.EVALSETS_DIR = original / "evalsets"
    storage.RUNS_DIR = original / "runs"
    if test_dir.exists():
        shutil.rmtree(test_dir)


class TestComputeProjectSampling:
    def test_no_runs(self, isolated_storage):
        """没有 run 时返回空态"""
        result = compute_project_sampling("proj-empty")
        assert result["total_runs"] == 0
        assert result["total_cases"] == 0
        assert result["k_values"] == [1, 2, 3]
        # 所有 k 的 value 都是 None，coverage 都是 0
        for entry in result["pass_at_k"]:
            assert entry["value"] is None
            assert entry["coverage"] == 0
        for entry in result["pass_pow_k"]:
            assert entry["value"] is None
            assert entry["coverage"] == 0

    def test_single_run_single_case(self, isolated_storage):
        """1 run 1 case：k=1 可计算，k=2/3 不可"""
        from app.storage import save_run
        cr = [CaseResult(case_name="c1", actual_output="o", passed=True, score=1.0)]
        save_run(_mkrun("r1", "proj-1", "es", cr))

        result = compute_project_sampling("proj-1")
        assert result["total_runs"] == 1
        assert result["total_cases"] == 1

        # k=1: n=1, c=1, pass@1 = 1 - C(0,1)/C(1,1) = 1
        assert result["pass_at_k"][0] == {"k": 1, "value": 1.0, "coverage": 1}
        assert result["pass_pow_k"][0] == {"k": 1, "value": 1.0, "coverage": 1}

        # k=2, 3: n=1 < k，无 eligible case
        assert result["pass_at_k"][1] == {"k": 2, "value": None, "coverage": 0}
        assert result["pass_at_k"][2] == {"k": 3, "value": None, "coverage": 0}

    def test_three_runs_two_cases(self, isolated_storage):
        """3 run 2 case：所有 k 都可计算"""
        from app.storage import save_run
        # case A: 3 次都通过（c=3, n=3）
        # case B: 2 通过 1 失败（c=2, n=3）
        runs = [
            _mkrun("r1", "proj-2", "es", [
                CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
                CaseResult(case_name="B", actual_output="o", passed=True, score=1.0),
            ]),
            _mkrun("r2", "proj-2", "es", [
                CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
                CaseResult(case_name="B", actual_output="o", passed=False, score=0.0),
            ]),
            _mkrun("r3", "proj-2", "es", [
                CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
                CaseResult(case_name="B", actual_output="o", passed=True, score=1.0),
            ]),
        ]
        for r in runs:
            save_run(r)

        result = compute_project_sampling("proj-2")
        assert result["total_runs"] == 3
        assert result["total_cases"] == 2

        # k=3, case A: n=3, c=3 → pass@3 = 1, pass^3 = 1
        # k=3, case B: n=3, c=2 → pass@3 = 1 - C(1,3)/C(3,3) = 1 - 0/1 = 1, pass^3 = C(2,3)/C(3,3) = 0/1 = 0
        # 平均 pass@3 = (1+1)/2 = 1, pass^3 = (1+0)/2 = 0.5
        assert result["pass_at_k"][2]["k"] == 3
        assert abs(result["pass_at_k"][2]["value"] - 1.0) < 1e-9
        assert abs(result["pass_pow_k"][2]["value"] - 0.5) < 1e-9
        assert result["pass_at_k"][2]["coverage"] == 2

    def test_pass_at_ge_pass_pow_invariant(self, isolated_storage):
        """聚合后 pass@k ≥ pass^k 仍然成立"""
        from app.storage import save_run
        import random
        random.seed(42)
        runs = []
        for i in range(6):
            cr = []
            for case_name in ["c1", "c2", "c3", "c4"]:
                passed = random.random() > 0.3
                cr.append(CaseResult(case_name=case_name, actual_output="o", passed=passed, score=1.0 if passed else 0.0))
            runs.append(_mkrun(f"r{i}", "proj-inv", "es", cr))
        for r in runs:
            save_run(r)

        result = compute_project_sampling("proj-inv")
        for i, k in enumerate(K_VALUES):
            at = result["pass_at_k"][i]["value"]
            pow_v = result["pass_pow_k"][i]["value"]
            if at is not None and pow_v is not None:
                assert at >= pow_v, f"k={k}: pass@k={at} < pass^k={pow_v}"

    def test_partial_coverage(self, isolated_storage):
        """只有部分 case 有足够采样时，coverage 反映实际"""
        from app.storage import save_run
        # case A 出现 3 次，case B 只出现 1 次
        runs = [
            _mkrun("r1", "proj-pc", "es", [
                CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
                CaseResult(case_name="B", actual_output="o", passed=True, score=1.0),
            ]),
            _mkrun("r2", "proj-pc", "es", [
                CaseResult(case_name="A", actual_output="o", passed=False, score=0.0),
                # B 没出现
            ]),
            _mkrun("r3", "proj-pc", "es", [
                CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
            ]),
        ]
        for r in runs:
            save_run(r)

        result = compute_project_sampling("proj-pc")
        # k=3: 只有 A 满足 n>=3，B 只有 n=1
        assert result["pass_at_k"][2]["coverage"] == 1
        assert result["pass_pow_k"][2]["coverage"] == 1


# ============== API 端点 ==============

@pytest.fixture
def client():
    return TestClient(app)


class TestSamplingAPI:
    def test_project_not_found(self, client):
        """项目不存在返回 404"""
        r = client.get("/api/projects/proj-notexist/sampling")
        assert r.status_code == 404

    def test_empty_project(self, client, isolated_storage):
        """新项目无 run：空态响应"""
        from app.storage import save_project
        from app.models import Project
        save_project(Project(
            id="proj-sampling-empty",
            name="empty",
            task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        ))
        r = client.get("/api/projects/proj-sampling-empty/sampling")
        assert r.status_code == 200
        data = r.json()
        assert data["total_runs"] == 0
        assert data["total_cases"] == 0
        assert data["k_values"] == [1, 2, 3]
        assert len(data["pass_at_k"]) == 3
        assert len(data["pass_pow_k"]) == 3
        assert data["pass_at_k"][0]["value"] is None

    def test_with_runs(self, client, isolated_storage):
        """有 run 的完整流程"""
        from app.storage import save_project, save_run
        from app.models import Project
        pid = "proj-sampling-full"
        save_project(Project(
            id=pid, name="full", task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        ))
        # 3 个 run，2 个 case
        save_run(_mkrun("r1", pid, "es", [
            CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
            CaseResult(case_name="B", actual_output="o", passed=False, score=0.0),
        ]))
        save_run(_mkrun("r2", pid, "es", [
            CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
            CaseResult(case_name="B", actual_output="o", passed=True, score=1.0),
        ]))
        save_run(_mkrun("r3", pid, "es", [
            CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
            CaseResult(case_name="B", actual_output="o", passed=True, score=1.0),
        ]))

        r = client.get(f"/api/projects/{pid}/sampling")
        assert r.status_code == 200
        data = r.json()
        assert data["total_runs"] == 3
        assert data["total_cases"] == 2
        # k=1: case A (n=3,c=3) pass@1=1.0, pass^1=1.0
        #      case B (n=3,c=2) pass@1=2/3, pass^1=2/3
        # 平均 pass@1 = (1 + 2/3)/2 = 5/6
        assert abs(data["pass_at_k"][0]["value"] - (5 / 6)) < 1e-9
        assert data["pass_at_k"][0]["coverage"] == 2
        # pass@k >= pass^k
        for i in range(3):
            at = data["pass_at_k"][i]["value"]
            pow_v = data["pass_pow_k"][i]["value"]
            if at is not None and pow_v is not None:
                assert at >= pow_v

    def test_skipped_cases_excluded(self, client, isolated_storage):
        """skipped 的 case 不计入采样数"""
        from app.storage import save_project, save_run
        from app.models import Project
        pid = "proj-sampling-skip"
        save_project(Project(
            id=pid, name="skip", task_shape="general",
            judge_config={"base_url": "", "api_key": "", "model": ""},
            target_config={"base_url": "", "api_key": "", "model": ""},
        ))
        # r1: A 通过, B 跳过
        # r2: A 通过, B 通过
        save_run(_mkrun("r1", pid, "es", [
            CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
            CaseResult(case_name="B", actual_output="[SKIPPED]", passed=False, score=0.0, skipped_reason="llm_unavailable"),
        ]))
        save_run(_mkrun("r2", pid, "es", [
            CaseResult(case_name="A", actual_output="o", passed=True, score=1.0),
            CaseResult(case_name="B", actual_output="o", passed=True, score=1.0),
        ]))

        r = client.get(f"/api/projects/{pid}/sampling")
        data = r.json()
        # k=2: case A n=2 可计算, case B n=1（一次 skipped）不可计算
        # 所以 coverage=1（只有 A）
        assert data["pass_at_k"][1]["coverage"] == 1
