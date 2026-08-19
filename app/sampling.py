"""采样稳定性计算（pass@k / pass^k）

纯函数，无副作用。基于全部历史 run 聚合：
- case 级公式：
  - pass@k = 1 - C(n-c, k) / C(n, k)：k 次采样中至少一次通过的概率（潜力上界）
  - pass^k = C(c, k) / C(n, k)：k 次采样全部通过的概率（稳定下界）
  - n = 该 case 的采样数（出现且未被跳过的 run 数），c = 通过数
- 评测集级：各 case 等权均值，仅纳入 n ≥ k 的 case
- coverage = 参与 k 值计算的 case 数（n ≥ k 的 case 数）

冲突规则：pass@k 永远 ≥ pass^k（数学保证）。夹缝宽度 = 不确定性。
"""

from __future__ import annotations

import math
from typing import Optional

from .models import EvalRun
from .storage import list_runs

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


def _aggregate_runs(runs: list[EvalRun]) -> dict[str, list[bool]]:
    """把多次 run 的 results 按 case_name 聚合成 pass/fail 序列。

    - 只纳入 status == "completed" 的 run
    - 跳过 skipped_reason 非空的 case（不计入 n）
    - 同一 run 内同名 case 只取第一条（防御重复）
    """
    case_records: dict[str, list[bool]] = {}
    for run in runs:
        if run.status != "completed":
            continue
        seen_in_run: set[str] = set()
        for r in run.results:
            name = r.case_name
            if name in seen_in_run:
                continue
            seen_in_run.add(name)
            if r.skipped_reason:
                continue
            case_records.setdefault(name, []).append(r.passed)
    return case_records


def compute_project_sampling(project_id: str) -> dict:
    """计算项目级的 pass@k / pass^k。

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
