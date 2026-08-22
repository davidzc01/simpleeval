"""U-10: input 组与验证组对称化测试

- EvalCase.get_validations()：旧字段合成 + 新 validations 直接返回
- EvalCheck 新增 output_requirement 字段
- runner._evaluate_case：旧 case 行为零迁移；新 validations 全部通过才算过
- CSV 导入/导出：validations 列 round-trip
"""

import json
import io
import pytest
import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    EvalCase, EvalCheck, Project, JudgeConfig, TargetConfig,
)


@pytest.fixture
def client():
    return TestClient(app)


def _mk_project():
    return Project(
        id="p1", name="test", task_shape="general",
        judge_config=JudgeConfig(base_url="", api_key="", model=""),
        target_config=TargetConfig(base_url="", api_key="", model=None),
    )


# ============== 模型层 ==============

class TestEvalCheckOutputRequirement:
    """EvalCheck 新增 output_requirement 字段"""

    def test_field_exists_default_none(self):
        c = EvalCheck(name="x", eval_type="llm_judge")
        assert c.output_requirement is None

    def test_field_assignable(self):
        c = EvalCheck(name="x", eval_type="llm_judge", output_requirement="要有礼貌")
        assert c.output_requirement == "要有礼貌"

    def test_name_optional_default_empty(self):
        """U-10：name 不再强制"""
        c = EvalCheck(eval_type="exact", expected="ok")
        assert c.name == ""


class TestEvalCaseGetValidations:
    """EvalCase.get_validations() 合成逻辑"""

    def test_synthesize_when_validations_none(self):
        """旧 case（无 validations）→ 合成 [主验证] + checks"""
        case = EvalCase(
            id="c1", case_name="A", input="x",
            eval_type="exact", expected_output="ok",
            eval_params={"k": "v"},
            checks=[EvalCheck(name="chk1", field="result", eval_type="contains", eval_params={"substring": "x"})],
        )
        v = case.get_validations()
        assert len(v) == 2
        # 主验证
        assert v[0].field == ""
        assert v[0].eval_type == "exact"
        assert v[0].expected == "ok"
        assert v[0].eval_params == {"k": "v"}
        # checks 接续
        assert v[1].name == "chk1"
        assert v[1].field == "result"

    def test_synthesize_llm_judge_picks_output_requirement(self):
        """llm_judge case → 合成主验证带 output_requirement"""
        case = EvalCase(
            id="c1", case_name="A", input="x",
            eval_type="llm_judge", output_requirement="要有礼貌",
        )
        v = case.get_validations()
        assert v[0].eval_type == "llm_judge"
        assert v[0].output_requirement == "要有礼貌"
        assert v[0].expected is None

    def test_validations_returned_directly_when_set(self):
        """新结构：validations 非空 → 直接返回"""
        custom = [
            EvalCheck(name="主", field="", eval_type="contains", eval_params={"substring": "x"}),
            EvalCheck(name="ev", field="evidence", eval_type="exact", expected="true"),
        ]
        case = EvalCase(
            id="c1", case_name="A", input="x",
            eval_type="exact", expected_output="legacy",  # 旧字段应被忽略
            validations=custom,
        )
        v = case.get_validations()
        assert len(v) == 2
        assert v[0].name == "主"
        assert v[1].field == "evidence"

    def test_synthesize_no_checks(self):
        """无 checks 时只合成主验证"""
        case = EvalCase(id="c1", case_name="A", input="x", eval_type="exact", expected_output="ok")
        v = case.get_validations()
        assert len(v) == 1
        assert v[0].field == ""


# ============== runner 层 ==============

class TestEvaluateCaseValidations:
    """runner._evaluate_case 在 U-10 重写下的行为"""

    def _run(self, project, case, actual, judge_available=True, judge_error=""):
        from app.runner import _evaluate_case
        async def _do():
            return await _evaluate_case(project, case, actual, 0, False, judge_available, judge_error)
        return asyncio.new_event_loop().run_until_complete(_do())

    def test_legacy_case_zero_migration_no_checks(self):
        """旧 case（无 validations、无 checks）→ check_results 仍为空（零迁移）"""
        project = _mk_project()
        case = EvalCase(id="c1", case_name="A", input="x", eval_type="exact", expected_output="ok")
        passed, score, skipped, jt, checks, _ = self._run(project, case, "ok")
        assert passed is True
        assert score == 1.0
        assert checks == []  # 旧行为：无 check_results

    def test_legacy_case_zero_migration_empty_list_treated_as_legacy(self):
        """validations=[]（空列表）应与 None 一致：走零迁移，主验证不入 check_results

        回归 explicit_validations 与 get_validations() 的判定一致性：
        get_validations() 用 `if self.validations:`（空列表为假 → 合成），
        若 explicit_validations 用 `is not None`（空列表为真），会错误把主验证加入 check_results。
        """
        project = _mk_project()
        case = EvalCase(
            id="c1", case_name="A", input="x",
            eval_type="exact", expected_output="ok",
            validations=[],  # 显式空列表
        )
        passed, score, skipped, jt, checks, _ = self._run(project, case, "ok")
        assert passed is True
        # 空列表应等价于 None：零迁移，check_results 不含主验证
        assert checks == []

    def test_legacy_case_zero_migration_with_checks(self):
        """旧 case 带 checks → check_results 只含 checks 的项（不增加主验证条目）"""
        project = _mk_project()
        case = EvalCase(
            id="c1", case_name="A", input="x",
            eval_type="contains", eval_params={"substring": "退款"},
            checks=[
                EvalCheck(name="chk1", field="result", eval_type="exact", expected="true"),
            ],
        )
        # actual 含 "退款" + result=true → 主验证过 + chk1 过
        actual = json.dumps({"result": True, "msg": "支持退款"}, ensure_ascii=False)
        passed, score, skipped, jt, checks, _ = self._run(project, case, actual)
        assert passed is True
        # 旧行为：只 check_results，主验证不入
        assert len(checks) == 1
        assert checks[0]["name"] == "chk1"

    def test_new_validations_all_pass(self):
        """新结构 validations：所有项通过 → passed=True，check_results 含全部项"""
        project = _mk_project()
        case = EvalCase(
            id="c1", case_name="A", input="x",
            eval_type="contains", eval_params={"substring": "x"},  # 旧字段，新结构下应被忽略
            validations=[
                EvalCheck(name="主", field="", eval_type="contains", eval_params={"substring": "退款"}),
                EvalCheck(name="ev", field="evidence", eval_type="contains", eval_params={"substring": "退款"}),
            ],
        )
        actual = json.dumps({"evidence": "支持退款"}, ensure_ascii=False)
        passed, score, skipped, jt, checks, _ = self._run(project, case, actual)
        assert passed is True
        assert len(checks) == 2
        assert checks[0]["name"] == "主"
        assert checks[1]["name"] == "ev"
        assert all(c["passed"] for c in checks)

    def test_new_validations_sub_fails(self):
        """新结构 validations：第二条失败 → passed=False"""
        project = _mk_project()
        case = EvalCase(
            id="c1", case_name="A", input="x",
            eval_type="exact", expected_output="legacy",
            validations=[
                EvalCheck(name="主", field="", eval_type="contains", eval_params={"substring": "退款"}),
                EvalCheck(name="ev", field="evidence", eval_type="exact", expected="true"),
            ],
        )
        # actual 含"退款" → 主过；evidence="false" → sub 失败
        actual = json.dumps({"evidence": "false", "msg": "支持退款"}, ensure_ascii=False)
        passed, score, skipped, jt, checks, _ = self._run(project, case, actual)
        assert passed is False
        assert len(checks) == 2
        assert checks[0]["passed"] is True
        assert checks[1]["passed"] is False

    def test_new_validations_main_fails(self):
        """主验证失败 → passed=False，子项仍执行"""
        project = _mk_project()
        case = EvalCase(
            id="c1", case_name="A", input="x",
            validations=[
                EvalCheck(name="主", field="", eval_type="exact", expected="ok"),
                EvalCheck(name="ev", field="result", eval_type="exact", expected="true"),
            ],
        )
        actual = json.dumps({"result": True}, ensure_ascii=False)
        passed, score, skipped, jt, checks, _ = self._run(project, case, actual)
        assert passed is False
        assert checks[0]["passed"] is False
        assert checks[1]["passed"] is True

    def test_sub_validation_fallback_name_numbered_from_1(self):
        """无 name/field 的子验证 fallback 名应从「验证-1」开始，而非「验证-2」

        回归：enumerate(validations[1:], start=1) 的 i 从 1 开始，
        若用 f"验证-{i+1}" 则第一个子验证变成「验证-2」（主验证是「主输出验证」不是「验证-1」，
        序号跳跃）。应改为 f"验证-{i}" 使子验证从 1 起编号。
        """
        project = _mk_project()
        case = EvalCase(
            id="c1", case_name="A", input="x",
            validations=[
                EvalCheck(name="主", field="", eval_type="exact", expected="ok"),
                EvalCheck(name="", field="", eval_type="exact", expected="ok"),  # 无 name/field → fallback
                EvalCheck(name="", field="", eval_type="exact", expected="ok"),  # 无 name/field → fallback
            ],
        )
        passed, score, skipped, jt, checks, _ = self._run(project, case, "ok")
        assert passed is True
        assert len(checks) == 3
        # 主验证
        assert checks[0]["name"] == "主"
        # 子验证 fallback 名从「验证-1」开始
        assert checks[1]["name"] == "验证-1", f"应为「验证-1」，实际: {checks[1]['name']}"
        assert checks[2]["name"] == "验证-2", f"应为「验证-2」，实际: {checks[2]['name']}"

    def test_new_validations_llm_judge_unavailable_skips(self):
        """新结构主验证 llm_judge + judge 不可用 → skip，check_results 含主验证失败项"""
        project = _mk_project()
        case = EvalCase(
            id="c1", case_name="A", input="x",
            validations=[
                EvalCheck(name="主", field="", eval_type="llm_judge", output_requirement="r"),
                EvalCheck(name="ev", field="result", eval_type="exact", expected="true"),
            ],
        )
        passed, score, skipped, jt, checks, _ = self._run(project, case, "ok", judge_available=False, judge_error="judge unavailable")
        assert passed is False
        assert skipped != ""
        # 新结构：主验证入 check_results（标记为 fail）
        assert len(checks) == 1
        assert checks[0]["passed"] is False

    def test_new_validations_judge_token_accumulated(self):
        """新结构多 llm_judge validations 的 token 都累加"""
        from app.models import Project as P, JudgeConfig as JC, TargetConfig as TC
        project = P(
            id="p1", name="t", task_shape="general",
            judge_config=JC(base_url="https://j", api_key="k", model="m"),
            target_config=TC(base_url="", api_key="", model=None),
        )
        case = EvalCase(
            id="c1", case_name="A", input="x",
            validations=[
                EvalCheck(name="j1", field="r1", eval_type="llm_judge"),
                EvalCheck(name="j2", field="r2", eval_type="llm_judge"),
            ],
        )
        actual = json.dumps({"r1": "ok", "r2": "ok"})
        call_count = [0]
        async def fake_judge(**kwargs):
            call_count[0] += 1
            return (0.9, [11, 22][call_count[0] - 1])
        with patch("app.runner.judge_with_llm", new=fake_judge):
            passed, score, skipped, jt, checks, _ = self._run(project, case, actual)
        assert jt == 33
        assert len(checks) == 2
        assert all(c["passed"] for c in checks)

    def test_legacy_case_with_checks_behavior_unchanged(self):
        """重要回归：旧 case + checks 行为与原 _evaluate_case 完全一致"""
        project = _mk_project()
        case = EvalCase(
            id="c1", case_name="A", input="x",
            eval_type="contains", eval_params={"substring": "退款"},
            checks=[
                EvalCheck(name="chk1", field="result", eval_type="exact", expected="true"),
                EvalCheck(name="chk2", field="evidence", eval_type="contains", eval_params={"substring": "不存在"}),
            ],
        )
        actual = json.dumps({"result": True, "evidence": "支持退款"}, ensure_ascii=False)
        passed, score, skipped, jt, checks, _ = self._run(project, case, actual)
        # 主验证通过 + chk1 通过 + chk2 失败 → passed=False
        assert passed is False
        # check_results 只含 checks（不含主验证）
        assert len(checks) == 2
        assert checks[0]["name"] == "chk1"
        assert checks[0]["passed"] is True
        assert checks[1]["name"] == "chk2"
        assert checks[1]["passed"] is False


# ============== CSV round-trip ==============

class TestCSVValidationsRoundTrip:
    """U-10: CSV 导入/导出保留 validations 结构"""

    def test_export_includes_validations_column(self, client):
        """CSV 导出含 validations 列"""
        # 建项目 + 评测集
        client.post("/api/projects", json={"name": "rt"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "rt"][0]["id"]
        eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
        eid = eids[0]["id"]
        # 导入带 validations 的 case
        cases = [{
            "id": "c1", "case_name": "A", "input": "x", "eval_type": "exact", "enabled": True,
            "validations": [
                {"name": "主", "field": "", "eval_type": "exact", "expected": "ok"},
                {"name": "ev", "field": "result", "eval_type": "contains", "eval_params": {"substring": "x"}},
            ],
        }]
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace",
                    data={"file_content": json.dumps(cases)})
        # 导出
        r = client.get(f"/api/evalsets/{eid}/export?project_id={pid}")
        content = r.json()["content"]
        # 去掉 BOM
        if content.startswith("\ufeff"):
            content = content[1:]
        # 表头含 validations
        assert "validations" in content.split("\n")[0]
        # 数据行含 validations JSON
        assert "主" in content

    def test_import_validations_round_trip(self, client):
        """导出 CSV 再导入 → validations 结构不丢失"""
        client.post("/api/projects", json={"name": "rt2"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "rt2"][0]["id"]
        eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
        eid = eids[0]["id"]
        # 原始 case 带 2 条 validations
        original = [{
            "id": "c1", "case_name": "A", "input": "x", "eval_type": "exact", "enabled": True,
            "validations": [
                {"name": "主", "field": "", "eval_type": "exact", "expected": "ok"},
                {"name": "ev", "field": "result", "eval_type": "contains", "eval_params": {"substring": "x"}},
            ],
        }]
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace",
                    data={"file_content": json.dumps(original)})
        # 导出
        r = client.get(f"/api/evalsets/{eid}/export?project_id={pid}")
        csv_content = r.json()["content"]
        if csv_content.startswith("\ufeff"):
            csv_content = csv_content[1:]
        # 用第二个评测集 round-trip 导入
        client.post("/api/projects", json={"name": "rt3"})
        projects2 = client.get("/api/projects").json()
        pid2 = [p for p in projects2["projects"] if p["name"] == "rt3"][0]["id"]
        eids2 = client.get(f"/api/projects/{pid2}/evalsets").json()["evalsets"]
        eid2 = eids2[0]["id"]
        r2 = client.post(f"/api/evalsets/{eid2}/import?project_id={pid2}&mode=replace",
                         data={"file_content": csv_content})
        assert r2.status_code == 200
        cases = r2.json()["evalset"]["cases"]
        assert len(cases) == 1
        # validations 结构保留
        c = cases[0]
        assert c.get("validations") is not None
        assert len(c["validations"]) == 2
        assert c["validations"][0]["name"] == "主"
        assert c["validations"][1]["field"] == "result"
        # eval_params 也保留
        assert c["validations"][1]["eval_params"] == {"substring": "x"}

    def test_import_legacy_csv_without_validations_still_works(self, client):
        """旧 CSV（无 validations 列）→ 导入仍正常，validations 为 None（合成时生效）"""
        client.post("/api/projects", json={"name": "legacy"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "legacy"][0]["id"]
        eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
        eid = eids[0]["id"]
        # 旧 CSV 无 validations 列
        legacy_csv = "id,case_name,input,expected_output,eval_type,eval_params,enabled,tags\n" \
                     "c1,A,x,ok,exact,{},true,[]"
        r = client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace",
                        data={"file_content": legacy_csv})
        assert r.status_code == 200
        cases = r.json()["evalset"]["cases"]
        assert cases[0]["validations"] is None
        # 运行时 get_validations() 会自动合成
        ec = EvalCase(**cases[0])
        v = ec.get_validations()
        assert len(v) == 1
        assert v[0].expected == "ok"

    def test_export_empty_list_validations_preserves_as_empty_json(self, client):
        """validations=[] 导出应保留为 "[]"（而非空字符串），round-trip 后仍为 []

        回归：`if case.validations else ""` 对空列表判 False → 输出 ""，
        再导入变成 None，丢失了显式空列表的语义。
        与 tags 列（`if case.tags else "[]"`）保持一致行为。
        """
        import csv as csv_mod
        client.post("/api/projects", json={"name": "empty_vld"})
        projects = client.get("/api/projects").json()
        pid = [p for p in projects["projects"] if p["name"] == "empty_vld"][0]["id"]
        eids = client.get(f"/api/projects/{pid}/evalsets").json()["evalsets"]
        eid = eids[0]["id"]
        # 显式设置 validations=[]
        cases = [{
            "id": "c1", "case_name": "A", "input": "x", "eval_type": "exact",
            "expected_output": "ok", "enabled": True, "validations": [],
        }]
        client.post(f"/api/evalsets/{eid}/import?project_id={pid}&mode=replace",
                    data={"file_content": json.dumps(cases)})
        # 导出
        r = client.get(f"/api/evalsets/{eid}/export?project_id={pid}")
        content = r.json()["content"]
        if content.startswith("\ufeff"):
            content = content[1:]
        # 用 csv 模块正确解析（含引号字段）
        rows = list(csv_mod.reader(io.StringIO(content)))
        header = rows[0]
        vld_idx = header.index("validations")
        data_row = rows[1]
        vld_cell = data_row[vld_idx]
        # validations 列应为 "[]"（JSON 空数组），不是空字符串
        assert vld_cell == "[]", f"validations 列应为 '[]'，实际: {vld_cell!r}"


# ============== 跑评测端到端：validations 控制通过 ==============

class TestRunWithValidations:
    """通过 run_evalset 直接调用验证 validations 控制判定（端到端）"""

    def _run_evalset_with_validations(self, validations, actual_json, monkeypatch):
        """辅助：建项目+评测集（带 validations case），mock target，跑 run_evalset"""
        from app.runner import run_evalset
        from app.models import Project, JudgeConfig, TargetConfig, EvalSet

        project = Project(
            id="p1", name="t", task_shape="general",
            judge_config=JudgeConfig(base_url="", api_key="", model=""),
            target_config=TargetConfig(base_url="https://x", api_key="", model="m"),
        )
        case = EvalCase(
            id="c1", case_name="A", input="x", enabled=True,
            validations=validations,
        )
        evalset = EvalSet(id="e1", project_id="p1", name="集", cases=[case])

        async def fake_call_target(*args, **kwargs):
            return actual_json, 10, False
        monkeypatch.setattr("app.runner.call_target", fake_call_target)

        import asyncio
        async def _do():
            return await run_evalset(project, evalset)
        return asyncio.new_event_loop().run_until_complete(_do())

    def test_run_passes_when_all_validations_pass(self, monkeypatch):
        """新结构 case：主验证 + sub 全过 → passed=True"""
        validations = [
            EvalCheck(name="主", field="", eval_type="contains", eval_params={"substring": "ok"}),
            EvalCheck(name="ev", field="result", eval_type="exact", expected="true"),
        ]
        actual = json.dumps({"result": True, "msg": "ok"}, ensure_ascii=False)
        run = self._run_evalset_with_validations(validations, actual, monkeypatch)
        assert run.status == "completed"
        assert len(run.results) == 1
        assert run.results[0].passed is True
        # check_results 含主验证 + sub
        assert len(run.results[0].check_results) == 2
        assert all(c["passed"] for c in run.results[0].check_results)

    def test_run_fails_when_sub_validation_fails(self, monkeypatch):
        """新结构 case：sub 验证失败 → passed=False"""
        validations = [
            EvalCheck(name="主", field="", eval_type="contains", eval_params={"substring": "ok"}),
            EvalCheck(name="ev", field="result", eval_type="exact", expected="false"),
        ]
        actual = json.dumps({"result": True, "msg": "ok"}, ensure_ascii=False)
        run = self._run_evalset_with_validations(validations, actual, monkeypatch)
        assert run.status == "completed"
        assert run.results[0].passed is False
        # 主验证过 + sub 失败
        checks = run.results[0].check_results
        assert len(checks) == 2
        assert checks[0]["passed"] is True
        assert checks[1]["passed"] is False
