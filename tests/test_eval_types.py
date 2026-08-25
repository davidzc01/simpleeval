"""eval_types 模块单元测试"""

import pytest
from app.eval_types import (
    eval_exact,
    eval_contains,
    eval_not_contains,
    eval_length,
    eval_regex,
    eval_json_schema,
    eval_numeric,
    eval_script,
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


# ============== R-4: 新增确定性检测类型 ==============


class TestEvalRegex:
    """R-4: 正则匹配测试"""

    def test_regex_match(self):
        """命中即过"""
        assert eval_regex("hello world", {"pattern": "world"}) is True

    def test_regex_no_match(self):
        """未命中不过"""
        assert eval_regex("hello", {"pattern": "xyz"}) is False

    def test_regex_ignore_case(self):
        """ignore_case=True 时大小写不敏感"""
        assert eval_regex("Hello World", {"pattern": "world", "ignore_case": True}) is True
        assert eval_regex("HELLO WORLD", {"pattern": "world", "ignore_case": True}) is True

    def test_regex_case_sensitive_default(self):
        """默认大小写敏感"""
        assert eval_regex("Hello", {"pattern": "world"}) is False

    def test_regex_invalid_pattern(self):
        """非法 pattern 不过（不抛异常）"""
        assert eval_regex("anything", {"pattern": "[invalid"}) is False

    def test_regex_missing_pattern(self):
        """缺 pattern 参数不过"""
        assert eval_regex("anything", {}) is False
        assert eval_regex("anything", {"pattern": ""}) is False

    def test_regex_anchors(self):
        """^ 与 $ 锚点"""
        assert eval_regex("hello", {"pattern": "^h"}) is True
        assert eval_regex("hello", {"pattern": "^x"}) is False
        assert eval_regex("hello", {"pattern": "o$"}) is True


class TestEvalJsonSchema:
    """R-4: JSON Schema 校验测试"""

    def test_schema_pass(self):
        """符合 schema 通过"""
        params = {"schema": {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}}}
        assert eval_json_schema('{"a": 1}', params) is True

    def test_schema_missing_required(self):
        """缺必填字段不过"""
        params = {"schema": {"type": "object", "required": ["b"]}}
        assert eval_json_schema('{"a": 1}', params) is False

    def test_schema_wrong_type(self):
        """类型不匹配不过"""
        params = {"schema": {"type": "object", "properties": {"a": {"type": "integer"}}}}
        assert eval_json_schema('{"a": "string"}', params) is False

    def test_schema_invalid_json(self):
        """非 JSON 不过"""
        assert eval_json_schema("not json", {"schema": {"type": "object"}}) is False

    def test_schema_missing_param(self):
        """缺 schema 参数不过"""
        assert eval_json_schema('{"a": 1}', {}) is False
        assert eval_json_schema('{"a": 1}', {"schema": "not a dict"}) is False

    def test_schema_array_type(self):
        """数组类型校验"""
        params = {"schema": {"type": "array", "items": {"type": "integer"}, "minItems": 2}}
        assert eval_json_schema("[1, 2, 3]", params) is True
        assert eval_json_schema("[1]", params) is False
        assert eval_json_schema('["a", "b"]', params) is False

    def test_schema_nested_object(self):
        """嵌套对象校验"""
        params = {"schema": {
            "type": "object",
            "properties": {"user": {"type": "object", "required": ["id"]}},
            "required": ["user"],
        }}
        assert eval_json_schema('{"user": {"id": 1}}', params) is True
        assert eval_json_schema('{"user": {}}', params) is False


class TestEvalNumeric:
    """R-4: 数值比较测试"""

    def test_gt(self):
        assert eval_numeric("5", {"operator": "gt", "value": 3}) is True
        assert eval_numeric("3", {"operator": "gt", "value": 3}) is False

    def test_gte(self):
        assert eval_numeric("3", {"operator": "gte", "value": 3}) is True
        assert eval_numeric("2", {"operator": "gte", "value": 3}) is False

    def test_lt(self):
        assert eval_numeric("2", {"operator": "lt", "value": 3}) is True
        assert eval_numeric("3", {"operator": "lt", "value": 3}) is False

    def test_lte(self):
        assert eval_numeric("3", {"operator": "lte", "value": 3}) is True
        assert eval_numeric("4", {"operator": "lte", "value": 3}) is False

    def test_eq(self):
        assert eval_numeric("3", {"operator": "eq", "value": 3}) is True
        assert eval_numeric("3.0", {"operator": "eq", "value": 3}) is True
        assert eval_numeric("4", {"operator": "eq", "value": 3}) is False

    def test_neq(self):
        assert eval_numeric("4", {"operator": "neq", "value": 3}) is True
        assert eval_numeric("3", {"operator": "neq", "value": 3}) is False

    def test_non_numeric_actual(self):
        """非数字不过"""
        assert eval_numeric("not a number", {"operator": "gt", "value": 3}) is False

    def test_unknown_operator(self):
        """未知算子不过"""
        assert eval_numeric("5", {"operator": "between", "value": 3}) is False

    def test_missing_value(self):
        """缺 value 不过"""
        assert eval_numeric("5", {"operator": "gt"}) is False

    def test_float_comparison(self):
        """浮点比较"""
        assert eval_numeric("3.14", {"operator": "gt", "value": 3.0}) is True
        assert eval_numeric("3.14", {"operator": "lt", "value": 3.2}) is True


class TestEvalScript:
    """R-4: 受限表达式求值测试"""

    def test_actual_len(self):
        """访问 actual 长度"""
        assert eval_script("hello", {"code": "len(actual) > 3"}) is True
        assert eval_script("hi", {"code": "len(actual) > 3"}) is False

    def test_actual_string_compare(self):
        """actual 字符串比较"""
        assert eval_script("ok", {"code": 'actual == "ok"'}) is True
        assert eval_script("no", {"code": 'actual == "ok"'}) is False

    def test_cross_field_basic(self):
        """跨字段：fields.result == "true" and len(fields.evidence) >= 2"""
        fields = {"result": "true", "evidence": ["a", "b"]}
        assert eval_script("true", {"code": 'fields.result == "true" and len(fields.evidence) >= 2'},
                           fields=fields) is True
        # evidence 不足 2 条 → 不过
        fields_short = {"result": "true", "evidence": ["a"]}
        assert eval_script("true", {"code": 'fields.result == "true" and len(fields.evidence) >= 2'},
                           fields=fields_short) is False
        # result != "true" → 不过
        fields_fail = {"result": "false", "evidence": ["a", "b"]}
        assert eval_script("false", {"code": 'fields.result == "true" and len(fields.evidence) >= 2'},
                           fields=fields_fail) is False

    def test_cross_field_numeric(self):
        """跨字段：fields.score > 0.8"""
        assert eval_script("0.9", {"code": "fields.score > 0.8"},
                           fields={"score": 0.9}) is True
        assert eval_script("0.5", {"code": "fields.score > 0.8"},
                           fields={"score": 0.5}) is False

    def test_cross_field_missing_key(self):
        """fields 缺键 → AttributeError → 不过"""
        assert eval_script("x", {"code": 'fields.missing == "x"'}, fields={}) is False

    def test_in_operator(self):
        """in 运算符"""
        assert eval_script("a", {"code": 'actual in ["a", "b", "c"]'}) is True
        assert eval_script("d", {"code": 'actual in ["a", "b", "c"]'}) is False

    def test_string_method_allowed(self):
        """白名单字符串方法可用"""
        assert eval_script("  hello  ", {"code": 'actual.strip() == "hello"'}) is True
        assert eval_script("HELLO", {"code": 'actual.lower() == "hello"'}) is True

    def test_arithmetic(self):
        """算术运算"""
        # len("hello")=5, 5+5=10, 10>5 → True
        assert eval_script("hello", {"code": "len(actual) + 5 > 5"}) is True
        # len("hello")=5, 5*2=10, 10==4 → False
        assert eval_script("hello", {"code": "len(actual) * 2 == 4"}) is False

    def test_no_code_returns_false(self):
        """缺 code 不过"""
        assert eval_script("hello", {}) is False
        assert eval_script("hello", {"code": ""}) is False

    def test_syntax_error_returns_false(self):
        """语法错误不过"""
        # 未闭合括号 → SyntaxError → 不过
        assert eval_script("hello", {"code": "len(actual >"}) is False
        # 赋值语句末行无返回表达式 → 不过（语句集模式下 x=1 合法但无判定值）
        assert eval_script("hello", {"code": "x = 1"}) is False

    def test_truthy_result(self):
        """truthy 结果判定"""
        assert eval_script("hello", {"code": "len(actual)"}) is True  # 非零
        assert eval_script("", {"code": "len(actual)"}) is False  # 零

    # ---- 安全测试：禁止语法 ----

    def test_security_import_rejected(self):
        """__import__ 调用被拒绝"""
        assert eval_script("x", {"code": '__import__("os")'}) is False

    def test_security_lambda_rejected(self):
        """lambda 被拒绝"""
        assert eval_script("x", {"code": "(lambda: 1)()"}) is False

    def test_security_list_comprehension_rejected(self):
        """列表推导式被拒绝"""
        assert eval_script("x", {"code": "[i for i in range(10)]"}) is False

    def test_security_dict_comprehension_rejected(self):
        """字典推导式被拒绝"""
        assert eval_script("x", {"code": "{k: v for k, v in fields.items()}"}) is False

    def test_security_dunder_attribute_rejected(self):
        """双下划线属性访问被拒绝（防 __class__/__globals__）"""
        assert eval_script("x", {"code": "actual.__class__"}) is False
        assert eval_script("x", {"code": "actual.__class__.__bases__"}) is False

    def test_security_arbitrary_function_rejected(self):
        """非白名单函数被拒绝（如 open / eval / exec）"""
        assert eval_script("x", {"code": 'open("/etc/passwd")'}) is False
        assert eval_script("x", {"code": 'eval("1+1")'}) is False

    def test_security_arbitrary_method_rejected(self):
        """非白名单方法被拒绝（如 .items() on dict — 仅字符串方法在白名单）"""
        # fields 的 .items() 不在白名单 → 不过
        assert eval_script("x", {"code": "fields.items()"}, fields={"a": 1}) is False

    def test_security_no_infinite_loop_via_recursion(self):
        """递归型表达式无法构造（lambda 已禁用），AST 拒绝即不过"""
        # 不能写 lambda（已禁用），所以递归调用根本无法构造 → 不过
        assert eval_script("x", {"code": "(lambda f: f(f))(lambda f: f(f))"}) is False

    # ---- R-4 语句集模式：if/elif/else + 变量赋值 + 多行 ----

    def test_statement_mode_variable_assignment(self):
        """多行：中间变量赋值 + 末行表达式判定"""
        code = "x = fields.a\ny = fields.b\nx + y > 5"
        assert eval_script("ignored", {"code": code}, fields={"a": 2, "b": 4}) is True
        assert eval_script("ignored", {"code": code}, fields={"a": 1, "b": 2}) is False  # 3 > 5 False

    def test_statement_mode_if_else(self):
        """if/else 分支 + 末行返回"""
        code = (
            'if fields.score >= 0.8:\n'
            '    grade = "pass"\n'
            'else:\n'
            '    grade = "fail"\n'
            'grade == "pass"'
        )
        assert eval_script("ignored", {"code": code}, fields={"score": 0.9}) is True
        assert eval_script("ignored", {"code": code}, fields={"score": 0.5}) is False

    def test_statement_mode_elif_chain(self):
        """elif 链（AST 嵌套 If）"""
        code = (
            'if fields.level == 1:\n'
            '    cat = "A"\n'
            'elif fields.level == 2:\n'
            '    cat = "B"\n'
            'else:\n'
            '    cat = "C"\n'
            'cat == "B"'
        )
        assert eval_script("x", {"code": code}, fields={"level": 2}) is True
        assert eval_script("x", {"code": code}, fields={"level": 1}) is False
        assert eval_script("x", {"code": code}, fields={"level": 3}) is False

    def test_statement_mode_no_return_expr_returns_false(self):
        """末行非表达式（纯赋值/if 块）→ 无判定返回值 → 不过"""
        # 末行是赋值 → 无 Expr → False
        assert eval_script("x", {"code": "x = 1"}, fields={}) is False
        # 末行是 if 块（无后续表达式）→ 无 Expr → False
        assert eval_script("x", {"code": 'if fields.a > 0:\n    x = 1'}, fields={"a": 1}) is False

    def test_statement_mode_complex_cross_field(self):
        """复杂跨字段：变量 + if + 字段访问 + 末行返回"""
        code = (
            'ev_count = len(fields.evidence)\n'
            'if fields.confidence > 0.7:\n'
            '    ok = ev_count >= 2\n'
            'else:\n'
            '    ok = ev_count >= 4\n'
            'ok and fields.result == "true"'
        )
        # 高置信 + 2 条 evidence + result=true → True
        assert eval_script("x", {"code": code},
                          fields={"confidence": 0.9, "evidence": ["a", "b"], "result": "true"}) is True
        # 高置信 + 1 条 evidence → False（ev_count < 2）
        assert eval_script("x", {"code": code},
                          fields={"confidence": 0.9, "evidence": ["a"], "result": "true"}) is False
        # 低置信 + 2 条 evidence → False（需 >= 4）
        assert eval_script("x", {"code": code},
                          fields={"confidence": 0.5, "evidence": ["a", "b"], "result": "true"}) is False
        # 高置信 + 2 条 evidence + result=false → False
        assert eval_script("x", {"code": code},
                          fields={"confidence": 0.9, "evidence": ["a", "b"], "result": "false"}) is False

    def test_statement_mode_for_loop_rejected(self):
        """for 循环被拒绝（防死循环）"""
        code = "total = 0\nfor i in fields.evidence:\n    total = total + 1\ntotal >= 2"
        assert eval_script("x", {"code": code}, fields={"evidence": ["a", "b"]}) is False

    def test_statement_mode_while_loop_rejected(self):
        """while 循环被拒绝"""
        code = 'i = 0\nwhile i < 10:\n    i = i + 1\ni == 10'
        assert eval_script("x", {"code": code}, fields={}) is False

    def test_statement_mode_attribute_assignment_rejected(self):
        """属性赋值被拒（防上下文污染：fields.x = 1）"""
        code = 'fields.x = 1\nfields.x == 1'
        assert eval_script("x", {"code": code}, fields={}) is False

    def test_statement_mode_function_def_rejected(self):
        """函数定义被拒"""
        code = 'def f():\n    return 1\nf() == 1'
        assert eval_script("x", {"code": code}, fields={}) is False


class TestRunRuleBasedR4Dispatch:
    """R-4: run_rule_based 分发新类型"""

    def test_regex_dispatch(self):
        assert run_rule_based("regex", "hello", None, {"pattern": "ell"}) is True

    def test_json_schema_dispatch(self):
        assert run_rule_based("json_schema", '{"a": 1}', None,
                              {"schema": {"type": "object"}}) is True

    def test_numeric_dispatch(self):
        assert run_rule_based("numeric", "5", None, {"operator": "gt", "value": 3}) is True

    def test_script_dispatch(self):
        assert run_rule_based("script", "hello", None, {"code": "len(actual) > 3"}) is True

    def test_script_with_fields_kwarg(self):
        """fields 关键字参数能传入"""
        fields = {"a": 1, "b": 2}
        assert run_rule_based("script", "1", None,
                              {"code": "fields.a + fields.b == 3"}, fields=fields) is True

    def test_script_fields_none_default(self):
        """fields=None 时仍可访问 actual"""
        assert run_rule_based("script", "hello", None, {"code": "len(actual) > 3"}) is True

