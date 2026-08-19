"""eval_types 模块单元测试"""

import pytest
from app.eval_types import (
    eval_exact,
    eval_contains,
    eval_not_contains,
    eval_length,
    run_rule_based,
)


class TestEvalExact:
    """精确匹配测试"""

    def test_exact_match(self):
        """完全匹配应该返回 True"""
        assert eval_exact("你好！", "你好！") is True

    def test_exact_match_with_strip(self):
        """带空格的字符串应该 strip 后比较"""
        assert eval_exact("  你好！  ", "你好！") is True

    def test_exact_no_match(self):
        """不匹配应该返回 False"""
        assert eval_exact("你好", "你好！") is False

    def test_exact_case_sensitive(self):
        """应该区分大小写"""
        assert eval_exact("Hello", "hello") is False

    def test_exact_with_none_expected(self):
        """expected 为 None 应该返回 False"""
        assert eval_exact("任何内容", None) is False


class TestEvalContains:
    """包含判断测试"""

    def test_contains_substring(self):
        """包含子串应该返回 True"""
        assert eval_contains("今天天气很好", {"substring": "天气"}) is True

    def test_contains_full_match(self):
        """完全匹配也算包含"""
        assert eval_contains("你好", {"substring": "你好"}) is True

    def test_not_contains_substring(self):
        """不包含子串应该返回 False"""
        assert eval_contains("今天天气很好", {"substring": "寒冷"}) is False

    def test_contains_empty_substring(self):
        """空子串（空字符串被任何字符串包含）"""
        # Python 中 "" in "任何内容" 返回 True
        assert eval_contains("任何内容", {"substring": ""}) is True

    def test_contains_missing_param(self):
        """缺少 substring 参数默认为空字符串"""
        # params.get("substring", "") 返回 ""，"" in "任何内容" 为 True
        assert eval_contains("任何内容", {}) is True


class TestEvalNotContains:
    """不包含判断测试"""

    def test_not_contains_success(self):
        """不包含子串应该返回 True"""
        assert eval_not_contains("今天天气很好", {"substring": "寒冷"}) is True

    def test_not_contains_failure(self):
        """包含子串应该返回 False"""
        assert eval_not_contains("今天天气很好", {"substring": "天气"}) is False

    def test_not_contains_empty_substring(self):
        """空子串在所有非空字符串中（Python 行为）"""
        # Python 中 "" in "任何内容" 返回 True，所以 not_contains 返回 False
        assert eval_not_contains("任何内容", {"substring": ""}) is False


class TestEvalLength:
    """长度判断测试"""

    def test_length_in_range(self):
        """长度在范围内应该返回 True"""
        assert eval_length("你好", {"min": 1, "max": 10}) is True

    def test_length_at_min(self):
        """长度等于最小值应该返回 True"""
        assert eval_length("你", {"min": 1, "max": 10}) is True

    def test_length_at_max(self):
        """长度等于最大值应该返回 True"""
        assert eval_length("你好你好你", {"min": 1, "max": 5}) is True

    def test_length_below_min(self):
        """长度小于最小值应该返回 False"""
        assert eval_length("", {"min": 1, "max": 10}) is False

    def test_length_above_max(self):
        """长度大于最大值应该返回 False"""
        assert eval_length("你好你好你好", {"min": 1, "max": 5}) is False

    def test_length_no_min(self):
        """没有最小值限制"""
        assert eval_length("短", {"max": 10}) is True
        assert eval_length("很长的文本" * 100, {"max": 10}) is False

    def test_length_no_max(self):
        """没有最大值限制"""
        assert eval_length("很长的文本", {"min": 1}) is True


class TestRunRuleBased:
    """规则调度测试"""

    def test_exact_type(self):
        """exact 类型调用 eval_exact"""
        assert run_rule_based("exact", "你好！", "你好！", {}) is True

    def test_contains_type(self):
        """contains 类型调用 eval_contains"""
        assert run_rule_based("contains", "今天天气很好", None, {"substring": "天气"}) is True

    def test_not_contains_type(self):
        """not_contains 类型调用 eval_not_contains"""
        assert run_rule_based("not_contains", "今天天气很好", None, {"substring": "寒冷"}) is True

    def test_length_type(self):
        """length 类型调用 eval_length"""
        assert run_rule_based("length", "你好", None, {"min": 1, "max": 10}) is True

    def test_unknown_type(self):
        """未知类型应该抛出异常"""
        with pytest.raises(ValueError) as exc_info:
            run_rule_based("unknown_type", "内容", None, {})
        assert "未知的规则类评测类型" in str(exc_info.value)
