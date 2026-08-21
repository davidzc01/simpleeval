"""采样稳定性计算（pass@k / pass^k）

纯函数，无副作用。基于全部历史 run 聚合：
- case 级公式：
  - pass@k = 1 - C(n-c, k) / C(n, k)：k 次采样中至少一次通过的概率（潜力上界）
  - pass^k = C(c, k) / C(n, k)：k 次采样全部通过的概率（稳定下界）
  - n = 该 case 的采样数（出现且未被跳过的 run 数），c = 通过数
- 评测集级：各 case 等权均值，仅纳入 n ≥ k 的 case
- coverage = 参与 k 值计算的 case 数（n ≥ k 的 case 数）

冲突规则：pass@k 永远 ≥ pass^k（数学保证）。夹缝宽度 = 不确定性。

T3-2: 评测集内容更新（PUT/replace 导入）后，旧 run 的采样历史失效。
聚合时只纳入 run.created_at >= evalset.content_updated_at 的 run；
content_updated_at 为空（旧数据）时全部纳入（现状行为）。
"""

from __future__ import annotations

import math
from typing import Optional

from .models import EvalRun
from .storage import list_runs, list_evalsets, get_evalset

K_VALUES = [1, 2, 3]
# coverage 低于总 case 数 * 此比例时，前端标灰提示采样不足
COVERAGE_WARN_RATIO = 0.6


def comb(n: int, k: int) -> int:
    """二项式系数 C(n, k)，负数或 k > n 返回 0。"""
    if n < 0 or k < 0:
        return 0
    if k > n:
        return 0
    return math.comb(n, k)


def pass_at_k_case(n: int, c: int, k: int) -> Optional[float]:
    """单 case 的 pass@k = 1 - C(n-c, k) / C(n, k)。

    Returns:
        None 表示无法计算（n < k 或分母为 0）；否则 [0, 1] 内的浮点数。
    """
    if n < k or n <= 0:
        return None
    c = max(0, min(c, n))  # 钳位，防御非法输入
    denom = comb(n, k)
    if denom == 0:
        return None
    numer = comb(n - c, k)
    return 1.0 - numer / denom


def pass_pow_k_case(n: int, c: int, k: int) -> Optional[float]:
    """单 case 的 pass^k = C(c, k) / C(n, k)。

    Returns:
        None 表示无法计算；否则 [0, 1] 内的浮点数。
    """
    if n < k or n <= 0:
        return None
    c = max(0, min(c, n))
    denom = comb(n, k)
    if denom == 0:
        return None
    numer = comb(c, k)
    return numer / denom


def _filter_runs_by_content_updated_at(
    runs: list[EvalRun], content_updated_at: Optional[str]
) -> list[EvalRun]:
    """T3-2: 按 evalset.content_updated_at 过滤 runs。

    - content_updated_at 为空（旧数据）→ 全部纳入（现状行为）
    - 否则只纳入 run.created_at >= content_updated_at 的 run
      （ISO 8601 字符串字典序与时间序一致，可直接字符串比较；
       旧 run 无 created_at 时按不达标处理）
    """
    if not content_updated_at:
        return runs
    return [r for r in runs if (r.created_at or "") >= content_updated_at]


def _aggregate_runs(runs: list[EvalRun]) -> dict[str, list[bool]]:
    """把多次 run 的 results 按 case_name 聚合成 pass/fail 序列（项目级，旧契约）。

    - 只纳入 status == "completed" 的 run
    - 跳过 skipped_reason 非空的 case（不计入 n）
    - T3-1: 同一 run 内同一 case 多次采样（sample_index 1..k）全量并入——
      k 次采样 = k 条独立观测。仅在 sample_index 为 None（k=1 单次）时
      对同名 case 做防御性去重（防 runner 重复产出）。

    T3-1 之前（无 sample_index 字段）的旧 run：所有 case 都按 k=1 处理，
    同名去重保持现状，向后兼容。
    """
    case_records: dict[str, list[bool]] = {}
    for run in runs:
        if run.status != "completed":
            continue
        seen_in_run: set[str] = set()
        for r in run.results:
            name = r.case_name
            # 仅 k=1 单次采样做防御性去重；k>1 的多次采样（sample_index 已设）全量纳入
            if r.sample_index is None:
                if name in seen_in_run:
                    continue
                seen_in_run.add(name)
            if r.skipped_reason:
                continue
            case_records.setdefault(name, []).append(r.passed)
    return case_records


def _aggregate_runs_by_case_id(runs: list[EvalRun]) -> dict[str, dict]:
    """T2-1: 把多次 run 的 results 按 case_id（fallback case_name）聚合。

    Returns:
        {case_key: {"case_id": str|None, "case_name": str, "records": list[bool]}}
        case_key = case_id if case_id else case_name（旧 run 无 case_id 时 fallback）

    T3-1: 同一 run 内同一 case 的 k 次采样（不同 sample_index）会全量并入，
    每条样本都计入 n 与 c——这是采样稳定性设计：k 次采样 = k 条独立观测。
    仅在 sample_index 为 None（k=1 单次）时对同 case_key 做防御性去重。
    """
    case_records: dict[str, dict] = {}
    for run in runs:
        if run.status != "completed":
            continue
        seen_in_run: set[str] = set()
        for r in run.results:
            # case_id 优先，无则 fallback case_name
            case_key = r.case_id if r.case_id else r.case_name
            # 仅 k=1 单次采样做防御性去重；k>1 的多次采样全量纳入
            if r.sample_index is None:
                if case_key in seen_in_run:
                    continue
                seen_in_run.add(case_key)
            if r.skipped_reason:
                continue
            entry = case_records.setdefault(
                case_key, {"case_id": r.case_id, "case_name": r.case_name, "records": []}
            )
            entry["records"].append(r.passed)
            # 若旧 run 无 case_id，entry["case_id"] 可能是 None；后续新 run 有则补
            if entry["case_id"] is None and r.case_id:
                entry["case_id"] = r.case_id
    return case_records


def compute_evalset_sampling(project_id: str, evalset_id: str) -> dict:
    """T2-1: 评测集级 case 粒度采样分析。

    按 case_id（fallback case_name）分组，返回每 case 的 n/c/pass_rate/pass_at_3/pass_pow_3。
    评测集级均值（compute_project_sampling）保持不变。

    T3-2: 只纳入 run.created_at >= evalset.content_updated_at 的 run。

    Returns:
        {
          "project_id": str,
          "evalset_id": str,
          "total_runs": int,
          "cases": [
            {"case_id": str|None, "case_name": str, "n": int, "c": int,
             "pass_rate": float, "pass_at_3": float|None, "pass_pow_3": float|None}
          ],
        }
    """
    runs = list_runs(project_id)
    # T3-2: 按 content_updated_at 过滤
    evalset = get_evalset(evalset_id, project_id)
    content_updated_at = evalset.content_updated_at if evalset else None
    runs = _filter_runs_by_content_updated_at(runs, content_updated_at)
    completed_runs = [r for r in runs if r.status == "completed"]
    case_records = _aggregate_runs_by_case_id(runs)

    cases_out = []
    for case_key, entry in case_records.items():
        records = entry["records"]
        n = len(records)
        c = sum(1 for p in records if p)
        pass_rate = c / n if n > 0 else 0.0
        pass_at_3 = pass_at_k_case(n, c, 3)
        pass_pow_3 = pass_pow_k_case(n, c, 3)
        cases_out.append({
            "case_id": entry["case_id"],
            "case_name": entry["case_name"],
            "n": n,
            "c": c,
            "pass_rate": round(pass_rate, 4),
            "pass_at_3": round(pass_at_3, 4) if pass_at_3 is not None else None,
            "pass_pow_3": round(pass_pow_3, 4) if pass_pow_3 is not None else None,
        })

    # 默认按 pass^3 升序（最不稳的排最上，失败模式先说话）；None 排最后
    cases_out.sort(key=lambda x: (x["pass_pow_3"] is None, x["pass_pow_3"] if x["pass_pow_3"] is not None else 1.0))

    return {
        "project_id": project_id,
        "evalset_id": evalset_id,
        "total_runs": len(completed_runs),
        "cases": cases_out,
    }


def compute_project_sampling(project_id: str) -> dict:
    """计算项目级的 pass@k / pass^k。

    T3-2: 取该项目第一个评测集的 content_updated_at 作为过滤锚点
    （一项目一评测集 UI 约定；多评测集时取第一个的更新时间）。

    Returns:
        {
          "project_id": str,
          "total_runs": int,           # completed run 数
          "total_cases": int,          # 出现过的唯一 case_name 数
          "k_values": [1, 2, 3],
          "pass_at_k":  [{"k": 1, "value": float|None, "coverage": int}, ...],
          "pass_pow_k": [{"k": 1, "value": float|None, "coverage": int}, ...],
        }
    """
    runs = list_runs(project_id)
    # T3-2: 取第一个评测集的 content_updated_at 过滤
    evalsets = list_evalsets(project_id)
    content_updated_at = evalsets[0].content_updated_at if evalsets else None
    runs = _filter_runs_by_content_updated_at(runs, content_updated_at)
    completed_runs = [r for r in runs if r.status == "completed"]
    case_records = _aggregate_runs(runs)

    total_runs = len(completed_runs)
    total_cases = len(case_records)

    pass_at_k = []
    pass_pow_k = []
    for k in K_VALUES:
        at_values: list[float] = []
        pow_values: list[float] = []
        for case_name, records in case_records.items():
            n = len(records)
            if n < k:
                continue
            c = sum(1 for p in records if p)
            at = pass_at_k_case(n, c, k)
            pow_v = pass_pow_k_case(n, c, k)
            if at is not None:
                at_values.append(at)
            if pow_v is not None:
                pow_values.append(pow_v)

        coverage = len(at_values)
        at_value = sum(at_values) / len(at_values) if at_values else None
        pow_value = sum(pow_values) / len(pow_values) if pow_values else None

        pass_at_k.append({"k": k, "value": at_value, "coverage": coverage})
        pass_pow_k.append({"k": k, "value": pow_value, "coverage": coverage})

    return {
        "project_id": project_id,
        "total_runs": total_runs,
        "total_cases": total_cases,
        "k_values": list(K_VALUES),
        "pass_at_k": pass_at_k,
        "pass_pow_k": pass_pow_k,
    }
