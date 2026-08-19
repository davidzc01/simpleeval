"""响应解析层（纯函数，无副作用）

实现 JSONPath 子集：
- 点分路径：$.a.b.c
- 数组索引（含负索引）：$.choices[0]、$.data[-1]
- 通配符 [*]：$.items[*].name（展开为多个值）
- 不支持过滤表达式 [?(@.x==1)]（过滤需求由 token_scope 承担）

冲突规则：
- token_paths 与 token_fields 同时给出：token_paths 优先
- 全部留空：输出 = 完整响应原文；token = 不统计（missing=True）
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Union

from .models import ResponseParsing


# ============== JSONPath 子集解析 ==============

_TOKEN_RE = re.compile(r"""
    (?P<dot>\.)?              # 可选的点分隔符
    (?P<key>[^.\[]+)          # 字段名
    |
    \[(?P<idx>-?\d+)\]       # 数组索引（含负索引）
    |
    \[\*\]                   # 通配符
""", re.VERBOSE)


def _tokenize_path(path: str) -> list:
    """将 JSONPath 字符串解析为 token 序列。

    返回元素类型：
    - str: 字段名
    - int: 数组索引（含负数）
    - '*': 通配符

    示例：
    - "$.choices[0].message.content" -> ['choices', 0, 'message', 'content']
    - "$.data[-1].text" -> ['data', -1, 'text']
    - "$.items[*].name" -> ['items', '*', 'name']
    - "$.a.b" -> ['a', 'b']
    """
    path = path.strip()
    if path.startswith("$"):
        path = path[1:]
    # 去掉开头的点
    if path.startswith("."):
        path = path[1:]

    tokens: list = []
    pos = 0
    while pos < len(path):
        # 跳过分隔符
        if path[pos] == ".":
            pos += 1
            continue

        # 通配符 [*]
        if path[pos:pos + 3] == "[*]":
            tokens.append("*")
            pos += 3
            continue

        # 数组索引 [n] 或 [-n]
        if path[pos] == "[":
            end = path.find("]", pos)
            if end == -1:
                raise ValueError(f"JSONPath 语法错误：未闭合的 '[' 于路径 '{path}'")
            idx_str = path[pos + 1:end]
            if not re.match(r"^-?\d+$", idx_str):
                raise ValueError(f"JSONPath 语法错误：无效的数组索引 '{idx_str}'")
            tokens.append(int(idx_str))
            pos = end + 1
            continue

        # 字段名（到下一个 . 或 [ 之前）
        m = re.match(r"[^.\[]+", path[pos:])
        if m:
            tokens.append(m.group())
            pos += m.end()
            continue

        # 无法识别的字符
        raise ValueError(f"JSONPath 语法错误：无法解析 '{path[pos:]}' 于路径 '{path}'")

    return tokens


def _resolve(data: Any, tokens: list) -> list:
    """根据 token 序列解析数据，返回所有命中值列表。

    通配符 '*' 会展开为多个值，因此返回列表而非单值。
    路径上的任一步未命中则该分支终止（不抛异常）。
    """
    if not tokens:
        return [data]

    results: list = []
    head, rest = tokens[0], tokens[1:]

    if head == "*":
        # 通配符：遍历所有元素
        if isinstance(data, dict):
            for v in data.values():
                results.extend(_resolve(v, rest))
        elif isinstance(data, list):
            for v in data:
                results.extend(_resolve(v, rest))
        return results

    if isinstance(head, int):
        # 数组索引
        if not isinstance(data, list):
            return []
        idx = head
        if idx < 0:
            idx += len(data)
        if 0 <= idx < len(data):
            results.extend(_resolve(data[idx], rest))
        return results

    # 字段名
    if isinstance(data, dict) and head in data:
        results.extend(_resolve(data[head], rest))
    return results


# ============== 输出提取 ==============

def extract_output(data: Any, output_paths: list[str]) -> tuple[str, bool]:
    """从响应数据中提取输出文本。

    - output_paths 从上到下依次尝试，第一个命中生效（fallback 链）
    - 全部未命中返回 ("", False)
    - output_paths 为空返回 ("", False)（调用方决定是否用完整响应兜底）

    Returns:
        (output, found): output 为提取到的字符串值（非字符串会被 stringify），
                         found 表示是否命中任一路径
    """
    if not output_paths:
        return ("", False)

    for path in output_paths:
        try:
            tokens = _tokenize_path(path)
        except ValueError:
            continue

        values = _resolve(data, tokens)
        if values:
            v = values[0]
            if isinstance(v, str):
                return (v, True)
            return (json.dumps(v, ensure_ascii=False), True)

    return ("", False)


# ============== Token 计数 ==============

def _coerce_token(value: Any) -> int:
    """将提取到的 token 值转为 int，非数字返回 0。"""
    try:
        if isinstance(value, bool):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sum_paths(data: Any, token_paths: list[str]) -> tuple[int, bool]:
    """对所有 token_paths 命中值求和。返回 (sum, found_any)。"""
    total = 0
    found_any = False
    for path in token_paths:
        try:
            tokens = _tokenize_path(path)
        except ValueError:
            continue
        for v in _resolve(data, tokens):
            found_any = True
            total += _coerce_token(v)
    return total, found_any


def _sum_fields(data: Any, token_fields: list[str], token_scope: Optional[dict] = None) -> tuple[int, bool]:
    """全树递归匹配同名字段并求和。

    - 无 token_scope：整树递归求和所有 token_fields
    - 有 token_scope：先筛出匹配 scope（含所有 {k:v}）的节点，
      再在命中节点子树内递归求和（命中后不再向下检查 scope，避免重复计数）
    - 返回 (sum, found_any)
    """
    if not token_fields:
        return 0, False

    field_set = set(token_fields)
    total = 0
    found_any = False

    def _matches_scope(node: Any) -> bool:
        if not token_scope:
            return True
        if not isinstance(node, dict):
            return False
        for k, v in token_scope.items():
            if node.get(k) != v:
                return False
        return True

    def _sum_subtree(node: Any) -> None:
        nonlocal total, found_any
        if isinstance(node, dict):
            for k, v in node.items():
                if k in field_set:
                    found_any = True
                    total += _coerce_token(v)
                _sum_subtree(v)
        elif isinstance(node, list):
            for v in node:
                _sum_subtree(v)

    def _walk(node: Any) -> None:
        nonlocal total, found_any
        if _matches_scope(node):
            _sum_subtree(node)
            return
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)
    return total, found_any


def count_tokens(
    data: Any,
    token_paths: Optional[list[str]] = None,
    token_fields: Optional[list[str]] = None,
    token_scope: Optional[dict] = None,
) -> tuple[int, bool]:
    """统计 token 用量。

    冲突规则：token_paths 与 token_fields 同时给出时 token_paths 优先。
    全部留空返回 (0, True)（missing=True，表示未配置/未命中）。

    Returns:
        (count, missing): missing=True 表示无法可靠统计 token
    """
    token_paths = token_paths or []
    token_fields = token_fields or []

    if token_paths:
        # paths 优先
        total, found = _sum_paths(data, token_paths)
        if found:
            return total, False
        # paths 给了但全部未命中 → missing
        return 0, True

    if token_fields:
        total, found = _sum_fields(data, token_fields, token_scope)
        if found:
            return total, False
        return 0, True

    # 全部留空：不统计
    return 0, True


# ============== 顶层便捷函数 ==============

def parse_response(raw: str, parsing: Optional[ResponseParsing]) -> dict:
    """解析原始响应字符串，返回输出、token 统计与缺失标记。

    供 POST /test/parsing 端点和 call_target 使用。

    Args:
        raw: HTTP 响应原文
        parsing: 响应解析配置，None 时按"全部留空"语义处理

    Returns:
        {"output": str, "token_used": int, "token_missing": bool,
         "output_found": bool}
    """
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # 非 JSON：output=原文，token 无法统计
        return {"output": raw, "token_used": 0, "token_missing": True, "output_found": False}

    if parsing is None:
        parsing = ResponseParsing()

    output, output_found = extract_output(data, parsing.output_paths)
    token, token_missing = count_tokens(
        data, parsing.token_paths, parsing.token_fields, parsing.token_scope
    )

    return {
        "output": output,
        "token_used": token,
        "token_missing": token_missing,
        "output_found": output_found,
    }
