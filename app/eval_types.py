"""4 种评测类型的判定实现（纯函数，便于测试）"""

from typing import Optional


def eval_exact(actual: str, expected: Optional[str]) -> bool:
    """精确匹配（strip 后比较）"""
    if expected is None:
        return False
    return actual.strip() == expected.strip()


def eval_contains(actual: str, params: dict) -> bool:
    """包含判断。params: {"substring": "..."}"""
    substring = params.get("substring", "")
    return substring in actual


def eval_not_contains(actual: str, params: dict) -> bool:
    """不包含判断。params: {"substring": "..."}"""
    substring = params.get("substring", "")
    return substring not in actual


def eval_length(actual: str, params: dict) -> bool:
    """长度判断。params: {"min": int, "max": int}"""
    length = len(actual)
    min_l = params.get("min", 0)
    max_l = params.get("max", float("inf"))
    return min_l <= length <= max_l


def run_rule_based(eval_type: str, actual: str, expected: Optional[str], params: dict) -> bool:
    """规则类评测（不含 llm_judge，那个走 judge.py）"""
    if eval_type == "exact":
        return eval_exact(actual, expected)
    if eval_type == "contains":
        return eval_contains(actual, params)
    if eval_type == "not_contains":
        return eval_not_contains(actual, params)
    if eval_type == "length":
        return eval_length(actual, params)
    raise ValueError(f"未知的规则类评测类型: {eval_type}")
