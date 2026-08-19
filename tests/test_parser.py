"""parser.py 纯函数单元测试"""

import json
import pytest

from app.parser import (
    _tokenize_path,
    extract_output,
    count_tokens,
    parse_response,
)
from app.models import ResponseParsing


class TestTokenizePath:
    """JSONPath 词法解析测试"""

    def test_simple_dot_path(self):
        assert _tokenize_path("$.data.reply") == ["data", "reply"]

    def test_no_dollar_prefix(self):
        assert _tokenize_path("data.reply") == ["data", "reply"]

    def test_array_index(self):
        assert _tokenize_path("$.choices[0].message.content") == ["choices", 0, "message", "content"]

    def test_negative_index(self):
        assert _tokenize_path("$.data[-1].text") == ["data", -1, "text"]

    def test_wildcard(self):
        assert _tokenize_path("$.items[*].name") == ["items", "*", "name"]

    def test_root_only(self):
        assert _tokenize_path("$") == []

    def test_empty(self):
        assert _tokenize_path("") == []

    def test_unclosed_bracket_raises(self):
        with pytest.raises(ValueError, match="未闭合"):
            _tokenize_path("$.choices[0.message")

    def test_invalid_index_raises(self):
        with pytest.raises(ValueError, match="无效的数组索引"):
            _tokenize_path("$.choices[abc]")


class TestExtractOutput:
    """输出提取测试"""

    def test_single_path_hit(self):
        data = {"choices": [{"message": {"content": "回复内容"}}]}
        out, found = extract_output(data, ["$.choices[0].message.content"])
        assert out == "回复内容"
        assert found is True

    def test_fallback_chain_first_hit(self):
        data = {"output": "直接输出"}
        out, found = extract_output(data, [
            "$.choices[0].message.content",
            "$.output",
        ])
        assert out == "直接输出"
        assert found is True

    def test_fallback_chain_second_hit(self):
        data = {"data": {"reply": "兜底命中"}}
        out, found = extract_output(data, [
            "$.choices[0].message.content",
            "$.data.reply",
        ])
        assert out == "兜底命中"
        assert found is True

    def test_all_miss(self):
        data = {"other": "x"}
        out, found = extract_output(data, ["$.choices[0].message.content"])
        assert out == ""
        assert found is False

    def test_empty_paths(self):
        out, found = extract_output({"a": 1}, [])
        assert out == ""
        assert found is False

    def test_negative_index(self):
        data = {"data": [{"text": "first"}, {"text": "last"}]}
        out, found = extract_output(data, ["$.data[-1].text"])
        assert out == "last"
        assert found is True

    def test_wildcard_takes_first(self):
        data = {"items": [{"name": "a"}, {"name": "b"}]}
        out, found = extract_output(data, ["$.items[*].name"])
        assert out == "a"
        assert found is True

    def test_wildcard_on_dict(self):
        # 通配符作用于 dict：遍历所有值
        data = {"a": {"name": "first"}, "b": {"name": "second"}}
        out, found = extract_output(data, ["$[*].name"])
        assert found is True
        assert out == "first"

    def test_wildcard_index_out_of_range(self):
        # 数组索引越界：该分支终止，不抛异常
        data = {"items": [{"name": "only"}]}
        out, found = extract_output(data, ["$.items[5].name"])
        assert found is False
        assert out == ""

    def test_non_string_value_stringified(self):
        data = {"score": 0.9}
        out, found = extract_output(data, ["$.score"])
        assert found is True
        assert json.loads(out) == 0.9

    def test_invalid_path_skipped(self):
        data = {"output": "命中"}
        out, found = extract_output(data, ["$.[invalid", "$.output"])
        assert out == "命中"
        assert found is True


class TestCountTokensPaths:
    """token_paths 路径求和测试"""

    def test_single_path(self):
        data = {"usage": {"total_tokens": 100}}
        count, missing = count_tokens(data, token_paths=["$.usage.total_tokens"])
        assert count == 100
        assert missing is False

    def test_multiple_paths_sum(self):
        data = {"usage": {"prompt_tokens": 30, "completion_tokens": 70}}
        count, missing = count_tokens(data, token_paths=[
            "$.usage.prompt_tokens", "$.usage.completion_tokens"
        ])
        assert count == 100
        assert missing is False

    def test_path_miss(self):
        data = {"other": 1}
        count, missing = count_tokens(data, token_paths=["$.usage.total_tokens"])
        assert count == 0
        assert missing is True

    def test_paths_priority_over_fields(self):
        data = {"usage": {"total_tokens": 50}, "extra": {"total_tokens": 999}}
        count, missing = count_tokens(
            data,
            token_paths=["$.usage.total_tokens"],
            token_fields=["total_tokens"],
        )
        assert count == 50
        assert missing is False


class TestCountTokensFields:
    """token_fields 递归求和测试"""

    def test_simple_field(self):
        data = {"total_tokens": 100}
        count, missing = count_tokens(data, token_fields=["total_tokens"])
        assert count == 100
        assert missing is False

    def test_recursive_sum(self):
        data = {
            "step1": {"total_tokens": 30},
            "step2": {"total_tokens": 40},
            "step3": {"total_tokens": 30},
        }
        count, missing = count_tokens(data, token_fields=["total_tokens"])
        assert count == 100
        assert missing is False

    def test_nested_list(self):
        data = {"trace": [{"total_tokens": 10}, {"total_tokens": 20}]}
        count, missing = count_tokens(data, token_fields=["total_tokens"])
        assert count == 30
        assert missing is False

    def test_multiple_fields(self):
        data = {
            "a": {"toolCallInputTokens": 10, "toolCallOutputTokens": 5},
            "b": {"toolCallInputTokens": 20, "toolCallOutputTokens": 15},
        }
        count, missing = count_tokens(data, token_fields=["toolCallInputTokens", "toolCallOutputTokens"])
        assert count == 50
        assert missing is False

    def test_field_miss(self):
        data = {"other": 1}
        count, missing = count_tokens(data, token_fields=["total_tokens"])
        assert count == 0
        assert missing is True


class TestCountTokensScope:
    """token_scope 过滤测试"""

    def test_scope_filters_nodes(self):
        data = {
            "modules": [
                {"moduleType": "tools", "total_tokens": 30},
                {"moduleType": "tools", "total_tokens": 20},
                {"moduleType": "chat", "total_tokens": 999},
            ]
        }
        count, missing = count_tokens(
            data,
            token_fields=["total_tokens"],
            token_scope={"moduleType": "tools"},
        )
        assert count == 50
        assert missing is False

    def test_scope_no_match(self):
        data = {"modules": [{"moduleType": "chat", "total_tokens": 100}]}
        count, missing = count_tokens(
            data,
            token_fields=["total_tokens"],
            token_scope={"moduleType": "tools"},
        )
        assert count == 0
        assert missing is True

    def test_scope_multiple_keys(self):
        data = {
            "modules": [
                {"moduleType": "tools", "status": "done", "total_tokens": 10},
                {"moduleType": "tools", "status": "pending", "total_tokens": 999},
                {"moduleType": "tools", "status": "done", "total_tokens": 20},
            ]
        }
        count, missing = count_tokens(
            data,
            token_fields=["total_tokens"],
            token_scope={"moduleType": "tools", "status": "done"},
        )
        assert count == 30
        assert missing is False


class TestCountTokensEdge:
    """边界情况"""

    def test_all_empty_returns_missing(self):
        count, missing = count_tokens({"a": 1})
        assert count == 0
        assert missing is True

    def test_bool_treated_as_zero(self):
        # JSON true/false 不应计入 token
        data = {"total_tokens": True}
        count, missing = count_tokens(data, token_fields=["total_tokens"])
        assert count == 0
        assert missing is False  # 字段命中了，只是值为 bool

    def test_non_numeric_value(self):
        data = {"total_tokens": "abc"}
        count, missing = count_tokens(data, token_fields=["total_tokens"])
        assert count == 0
        assert missing is False  # 命中字段，值非数字按 0 处理


class TestParseResponse:
    """顶层便捷函数测试"""

    def test_openai_compatible(self):
        raw = json.dumps({
            "choices": [{"message": {"content": "回复"}}],
            "usage": {"total_tokens": 42},
        })
        parsing = ResponseParsing(
            output_paths=["$.choices[0].message.content"],
            token_paths=["$.usage.total_tokens"],
        )
        result = parse_response(raw, parsing)
        assert result["output"] == "回复"
        assert result["token_used"] == 42
        assert result["token_missing"] is False
        assert result["output_found"] is True

    def test_fastgpt_workflow_trace(self):
        raw = json.dumps({
            "data": [
                {"text": "first"},
                {"pluginOutput": {"text": "final"}, "total_tokens": 30},
            ]
        })
        parsing = ResponseParsing(
            output_paths=["$.data[-1].pluginOutput.text", "$.data[-1].text"],
            token_fields=["total_tokens"],
        )
        result = parse_response(raw, parsing)
        assert result["output"] == "final"
        assert result["token_used"] == 30
        assert result["token_missing"] is False

    def test_all_empty_config(self):
        """A-1: output_paths 为空 → 输出 = 完整响应原文"""
        raw = json.dumps({"anything": "x"})
        result = parse_response(raw, ResponseParsing())
        assert result["output"] == raw  # 全部留空 = 原文
        assert result["token_used"] == 0
        assert result["token_missing"] is True
        assert result["output_found"] is True  # 原文兜底算命中

    def test_output_all_miss(self):
        raw = json.dumps({"other": "x"})
        parsing = ResponseParsing(output_paths=["$.choices[0].message.content"])
        result = parse_response(raw, parsing)
        assert result["output_found"] is False
        assert result["token_missing"] is True

    def test_non_json_input(self):
        result = parse_response("not json at all", ResponseParsing(output_paths=["$.a"]))
        assert result["output"] == "not json at all"
        assert result["token_missing"] is True
        assert result["output_found"] is False

    def test_fallback_chain_value(self):
        raw = json.dumps({"output": "fallback hit"})
        parsing = ResponseParsing(
            output_paths=[
                "$.choices[0].message.content",
                "$.output",
            ]
        )
        result = parse_response(raw, parsing)
        assert result["output"] == "fallback hit"
        assert result["output_found"] is True

    def test_none_parsing_treats_as_empty(self):
        raw = json.dumps({"a": 1})
        result = parse_response(raw, None)
        assert result["token_missing"] is True
