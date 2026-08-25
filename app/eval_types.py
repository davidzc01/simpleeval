"""规则类评测类型的判定实现（纯函数，便于测试）

R-4: 扩充规则类免疫区——新增 regex / json_schema / numeric / script 四种类型，
全部免 Judge、确定性、恒可比。script 通过 AST 白名单实现跨字段判断。
"""

import ast
import json
import re
import signal
import threading
from typing import Optional, Any


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


# ============== R-4: 新增确定性检测类型 ==============

def eval_regex(actual: str, params: dict) -> bool:
    """正则匹配。params: {"pattern": "...", "ignore_case": bool?}

    re.search 命中即过；非法 pattern 或异常 → 不过。
    """
    pattern = params.get("pattern")
    if not pattern:
        return False
    flags = re.IGNORECASE if params.get("ignore_case") else 0
    try:
        return re.search(pattern, actual, flags) is not None
    except re.error:
        return False


def eval_json_schema(actual: str, params: dict) -> bool:
    """JSON Schema 校验。params: {"schema": dict}

    actual 先 json.loads（失败则不过），再用 jsonschema 校验；
    缺依赖或 schema 非法 → 不过。
    """
    schema = params.get("schema")
    if not isinstance(schema, dict):
        return False
    try:
        obj = json.loads(actual)
    except (json.JSONDecodeError, TypeError):
        return False
    try:
        import jsonschema  # 延迟导入：仅在使用时检查依赖
    except ImportError:
        return False
    try:
        jsonschema.validate(instance=obj, schema=schema)
        return True
    except jsonschema.ValidationError:
        return False
    except jsonschema.SchemaError:
        return False


_NUMERIC_OPS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
    "neq": lambda a, b: a != b,
}


def eval_numeric(actual: str, params: dict) -> bool:
    """数值比较。params: {"operator": "gt|gte|lt|lte|eq|neq", "value": number}

    float(actual) 与 value 比较；非数字或未知算子 → 不过。
    """
    operator = params.get("operator")
    value = params.get("value")
    if operator not in _NUMERIC_OPS or value is None:
        return False
    try:
        num = float(actual)
        cmp_val = float(value)
    except (ValueError, TypeError):
        return False
    try:
        return _NUMERIC_OPS[operator](num, cmp_val)
    except (TypeError, ZeroDivisionError):
        return False


# ---- script: AST 白名单受限求值 ----

# 允许的内建函数（白名单）
_SCRIPT_BUILTINS = {
    "len": len, "str": str, "int": int, "float": float,
    "abs": abs, "min": min, "max": max, "sum": sum, "bool": bool,
    "True": True, "False": False, "None": None,
}

# 允许的字符串方法（白名单）
_SCRIPT_STRING_METHODS = {
    "strip", "lstrip", "rstrip", "lower", "upper", "title",
    "split", "rsplit", "splitlines", "join", "replace",
    "startswith", "endswith", "find", "rfind", "index", "count",
    "isdigit", "isalpha", "isalnum", "isspace", "isnumeric",
    "format", "encode", "decode",
}

# 允许的 AST 节点类型
# R-4 语句集模式：支持 ast.Module / ast.If / ast.Assign（多行 + 条件 + 中间变量）
_SCRIPT_ALLOWED_NODES = (
    ast.Module, ast.If, ast.Assign, ast.Expr,
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.IfExp, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Name, ast.Attribute, ast.Subscript, ast.Call, ast.Index, ast.Slice,
    ast.Load, ast.Store, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Invert,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv, ast.Pow,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
)

# 显式拒绝（即便 AST 能解析也要阻断）
# R-4 语句集模式：禁止循环（for/while，防死循环）、函数/类定义、推导式
_SCRIPT_REJECTED_NODES = (
    ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.Starred, ast.FormattedValue, ast.JoinedStr,
    ast.For, ast.While, ast.AsyncFor,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
    ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith,
    ast.Delete, ast.AugAssign, ast.AnnAssign, ast.Raise, ast.Assert,
    ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue, ast.Try, ast.ExceptHandler,
)


class _FieldMap:
    """fields 上下文：同时支持属性访问（fields.x）与下标（fields["x"]）与 `in` 判断"""

    def __init__(self, d: dict):
        object.__setattr__(self, "_d", d)

    def __getattr__(self, name: str) -> Any:
        d = object.__getattribute__(self, "_d")
        try:
            return d[name]
        except KeyError:
            raise AttributeError(name)

    def __getitem__(self, key: str) -> Any:
        return object.__getattribute__(self, "_d")[key]

    def __contains__(self, item: str) -> bool:
        return item in object.__getattribute__(self, "_d")


def _validate_script_ast(tree: ast.AST) -> Optional[str]:
    """AST 白名单校验：返回错误原因字符串；None 表示通过

    R-4 语句集模式：额外校验 Assign 目标（仅允许 Name，禁止属性赋值如 fields.x = 1）
    """
    for node in ast.walk(tree):
        if isinstance(node, _SCRIPT_REJECTED_NODES):
            return f"禁止的语法: {type(node).__name__}"
        if not isinstance(node, _SCRIPT_ALLOWED_NODES):
            return f"禁止的节点: {type(node).__name__}"
        # Name：仅允许白名单内建 + actual + fields + 用户变量（Store 上下文，即赋值左侧）
        # 注意：用户变量在赋值左侧出现时是 Store；在表达式里读取时也是 Load
        # 安全策略：Load 上下文的 Name 必须在白名单或为 actual/fields；
        #           Store 上下文的 Name 任意（用户自定义变量）
        if isinstance(node, ast.Name):
            is_store = isinstance(node.ctx, ast.Store)
            if not is_store and node.id not in _SCRIPT_BUILTINS and node.id not in ("actual", "fields"):
                # 可能是用户先赋值再读取的变量 — 检查是否在模块内被赋值过
                # 简单策略：允许任何 Name（运行时未定义会抛 NameError → 不过）
                # 但仍禁止双下划线防 __builtins__ 等逃逸
                if node.id.startswith("__"):
                    return f"禁止的标识符: {node.id}"
        # Attribute：禁止双下划线（防 __class__/__globals__ 等）
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                return f"禁止的属性访问: {node.attr}"
            # fields.xxx 允许；否则必须是白名单字符串方法
            if not (isinstance(node.value, ast.Name) and node.value.id == "fields"):
                if node.attr not in _SCRIPT_STRING_METHODS:
                    return f"禁止的方法: {node.attr}"
        # Call：func 必须是白名单 Name 或白名单 Attribute
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                if f.id not in _SCRIPT_BUILTINS or not callable(_SCRIPT_BUILTINS.get(f.id)):
                    return f"禁止的函数调用: {f.id}"
            elif isinstance(f, ast.Attribute):
                if f.attr not in _SCRIPT_STRING_METHODS:
                    return f"禁止的方法调用: {f.attr}"
            else:
                return f"禁止的调用形式: {type(f).__name__}"
        # Assign：目标必须是 Name（禁止 fields.x = 1 这类属性赋值，防上下文污染）
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if not isinstance(tgt, ast.Name):
                    return f"禁止的赋值目标: {type(tgt).__name__}（仅允许简单变量）"
    return None


def _eval_with_timeout(code_obj, namespace: dict, timeout: float = 1.0) -> Any:
    """带超时的 eval（signal.SIGALRM 仅主线程生效；非主线程直接 eval）"""
    try:
        main_thread = threading.current_thread() == threading.main_thread()
    except Exception:
        main_thread = False
    if main_thread:
        def _handler(signum, frame):
            raise TimeoutError("script eval timeout")
        old = signal.signal(signal.SIGALRM, _handler)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return eval(code_obj, namespace)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    else:
        return eval(code_obj, namespace)


def eval_script(actual: str, params: dict, fields: Optional[dict] = None) -> bool:
    """受限表达式/语句集求值。params: {"code": "..."}

    R-4 语句集模式（David 追问：支持条件逻辑）：
    - 多行脚本，支持 if/elif/else、中间变量赋值（如 `x = fields.a`）
    - **最后一行表达式**为判定返回值（True/False/数值，真值判断）
    - 表达式能力同前（比较/逻辑/算术/容器/白名单方法）

    上下文提供 `actual`（主输出 str）与 `fields`（解包后各字段 _FieldMap）。
    AST 白名单：允许 ast.If / ast.Assign；禁止循环（for/while）、函数/类定义、
    import、lambda、推导式、属性双下划线、任意函数调用。
    解析失败或执行异常（含超时）→ 不过。
    """
    code = params.get("code")
    if not isinstance(code, str) or not code.strip():
        return False
    # 1. AST 解析（exec 模式：支持多行语句 + 表达式）
    try:
        tree = ast.parse(code.strip(), mode="exec")
    except SyntaxError:
        return False
    if not tree.body:
        return False
    # 2. 白名单校验
    err = _validate_script_ast(tree)
    if err is not None:
        return False
    # 3. 拆分：最后一条若是 ast.Expr（表达式语句）→ 作为返回值；前面语句 exec
    last = tree.body[-1]
    if isinstance(last, ast.Expr):
        exec_stmts = tree.body[:-1]
        return_expr = last.value
    else:
        # 末行非表达式（如纯赋值/if 块）→ 无判定返回值 → 不过
        exec_stmts = tree.body
        return_expr = None
    # 4. 编译 exec 部分 + return 表达式
    try:
        exec_code_obj = compile(ast.Module(body=exec_stmts, type_ignores=[]), "<script-exec>", "exec")
        if return_expr is not None:
            ret_code_obj = compile(ast.Expression(body=return_expr), "<script-ret>", "eval")
        else:
            ret_code_obj = None
    except (SyntaxError, ValueError, TypeError):
        return False
    # 5. 受限命名空间执行
    field_map = _FieldMap(fields or {})
    namespace = {"__builtins__": dict(_SCRIPT_BUILTINS), "actual": actual, "fields": field_map}
    try:
        if exec_code_obj is not None:
            _eval_with_timeout(exec_code_obj, namespace)
        if ret_code_obj is None:
            return False
        result = _eval_with_timeout(ret_code_obj, namespace)
    except (TimeoutError, RecursionError, MemoryError, Exception):
        return False
    return bool(result)


def run_rule_based(
    eval_type: str,
    actual: str,
    expected: Optional[str],
    params: dict,
    fields: Optional[dict] = None,
) -> bool:
    """规则类评测分发（不含 llm_judge，那个走 judge.py）

    R-4 扩展：regex / json_schema / numeric / script。
    script 类型需要 `fields`（解析后的多字段 dict）以支持跨字段判断。
    """
    if eval_type == "exact":
        return eval_exact(actual, expected)
    if eval_type == "contains":
        return eval_contains(actual, params)
    if eval_type == "not_contains":
        return eval_not_contains(actual, params)
    if eval_type == "length":
        return eval_length(actual, params)
    if eval_type == "regex":
        return eval_regex(actual, params)
    if eval_type == "json_schema":
        return eval_json_schema(actual, params)
    if eval_type == "numeric":
        return eval_numeric(actual, params)
    if eval_type == "script":
        return eval_script(actual, params, fields)
    raise ValueError(f"未知的规则类评测类型: {eval_type}")
