"""LLM-as-Judge 调用（OpenAI Compatible）+ 被评测 API 调用"""

import json
import hashlib
import httpx
import re
from typing import Optional, Union

from .models import AuthConfig, ResponseMapping, ResponseParsing, JudgeConfig
from .parser import extract_output, count_tokens, _unpack_output


class APIError(Exception):
    """API 调用错误基类"""
    def __init__(self, message: str, status_code: Optional[int] = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class NetworkError(APIError):
    """网络错误（连接失败、超时等）"""
    pass


class ResponseFormatError(APIError):
    """API 返回格式错误"""
    pass


class MissingVariableError(ValueError):
    """模板含未定义的 {占位符} 且 variables 没有对应键"""


def compute_judge_fingerprint(judge_config: Optional[JudgeConfig]) -> Optional[str]:
    """Q-1: 对解析后的实际 Judge 配置取稳定 hash（不含 secret 值）

    指纹覆盖：api_type / base_url / model / prompt_template / auth.type
    不含：api_key、auth 凭据值（安全考虑）
    返回 12 位 hex；judge_config 为 None 时返回 None（旧 run 兼容）
    """
    if not judge_config:
        return None
    fields = {
        "api_type": judge_config.api_type,
        "base_url": judge_config.base_url,
        "model": judge_config.model or "",
        "prompt_template": judge_config.prompt_template or "",
        "auth_type": judge_config.auth.type if judge_config.auth else "none",
    }
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def render_request_template(
    template: str,
    prompt: str,
    variables: Optional[dict] = None,
    case_name: str = "",
    task_shape: str = "",
    default_missing: Optional[str] = None,
) -> str:
    """渲染请求模板：{input}/{case_name}/{task_shape}/{key} 占位符用 JSON 转义后的值替换。

    - {input} → prompt（case.input）
    - {case_name} → case_name
    - {task_shape} → task_shape
    - {key} → variables[key]（键名任意，数量任意，由用户在模板里决定）

    直接 replace 会把原始换行符/引号拼进 JSON 字符串值，导致 json.loads
    报 Invalid control character；用 json.dumps 转义后去掉首尾引号，
    得到可安全嵌入 JSON 模板的字符串。模板含其它花括号（如 FastGPT
    的 variables 结构）不受影响。

    缺少变量（模板有 {key} 但 variables 无对应键）抛 MissingVariableError，
    由调用方决定是否阻断。
    """
    def _esc(v: str) -> str:
        return json.dumps(v, ensure_ascii=False)[1:-1]

    result = template
    # 标准三变量
    result = result.replace("{input}", _esc(prompt))
    result = result.replace("{case_name}", _esc(case_name))
    result = result.replace("{task_shape}", _esc(task_shape))
    # 自定义变量：{key}
    if variables:
        for k, v in variables.items():
            # 值统一 stringify（dict/list 用 json.dumps 后去引号；标量直接 str）
            if isinstance(v, (dict, list)):
                esc = json.dumps(v, ensure_ascii=False)
                # dict/list 嵌入模板应是结构而非字符串，不剥引号
                result = result.replace("{" + k + "}", esc)
            else:
                result = result.replace("{" + k + "}", _esc(str(v)))
    # 检测剩余 {identifier} 占位符（未定义变量）
    import re
    remaining = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", result)
    if remaining:
        if default_missing is not None:
            # 测试连接等宽松场景：未定义变量用占位值填充
            for name in remaining:
                result = result.replace("{" + name + "}", _esc(default_missing))
            return result
        raise MissingVariableError(
            f"模板含未定义的占位符: {remaining[0]}（variables 缺少该键）。"
            f"如需支持，请在 case.variables 里提供。"
        )
    return result


def _build_headers(api_key: str, auth: AuthConfig) -> dict:
    """构建请求头（认证语义：一次只生效一种认证）

    - bearer → Authorization: Bearer <token>
    - api_key → 自定义头（默认 X-API-Key），不带 Authorization
    - headers → 仅自定义头集合
    - cookie → 仅 cookies（由 _build_cookies 单独处理），不带 Authorization
    - none → api_key 有值发 Bearer（OpenAI 兼容默认），空则完全无认证头
    """
    headers = {}

    if auth.type == "bearer" and auth.bearer_token:
        headers["Authorization"] = f"Bearer {auth.bearer_token}"
    elif auth.type == "api_key" and auth.api_key_value:
        header_name = auth.api_key_header or "X-API-Key"
        headers[header_name] = auth.api_key_value
    elif auth.type == "headers":
        for h in auth.headers:
            headers[h.get("name", "")] = h.get("value", "")
    elif auth.type == "cookie":
        pass  # 仅 cookies
    elif api_key:
        # 默认（none）：OpenAI 兼容行为，有 key 才发 Bearer
        headers["Authorization"] = f"Bearer {api_key}"

    return headers


def _build_cookies(auth: AuthConfig) -> dict:
    """构建 Cookie"""
    cookies = {}
    for c in auth.cookies:
        cookies[c.get("name", "")] = c.get("value", "")
    return cookies


def _extract_response(raw_response: str, response_mapping: list[ResponseMapping]) -> str:
    """根据映射提取响应内容"""
    if not response_mapping:
        return raw_response

    try:
        data = json.loads(raw_response)
    except json.JSONDecodeError:
        return raw_response

    # 简单的 JSONPath 实现（支持 $.xxx.yyy 格式）
    results = []
    for mapping in response_mapping:
        path = mapping.jsonpath.lstrip("$.")
        parts = path.split(".")
        current = data
        try:
            for part in parts:
                current = current[part]
            results.append(f"{mapping.name}: {current}")
        except (KeyError, TypeError):
            results.append(f"{mapping.name}: [提取失败]")

    return "\n".join(results) if results else raw_response


async def call_target(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    request_template: str = "{input}",
    auth: Optional[AuthConfig] = None,
    response_mapping: Optional[list[ResponseMapping]] = None,
    response_parsing: Optional[ResponseParsing] = None,
    api_type: str = "openai_compatible",
    variables: Optional[dict] = None,
    case_name: str = "",
    task_shape: str = "",
    default_missing: Optional[str] = None,
) -> tuple[str, int, bool]:
    """调用被评测 API，返回 (输出文本, 消耗 token 数, token_missing 标志)

    解析优先级：
    1. response_parsing（四键模型，parser 层接管）
    2. response_mapping（旧设计，_extract_response 兼容）
    3. OpenAI 兼容默认（choices[0].message.content + usage.total_tokens）

    B-13/B-21: variables + case_name + task_shape 传给 render_request_template，
    模板里的 {key}/{case_name}/{task_shape} 占位符被替换。
    """
    auth = auth or AuthConfig()
    response_mapping = response_mapping or []

    # 构建请求体 + URL
    if api_type == "custom":
        # custom 模式：纯模板渲染，不注入 model/messages，URL 不补 /chat/completions
        try:
            rendered = render_request_template(
                request_template, prompt,
                variables=variables, case_name=case_name, task_shape=task_shape,
                default_missing=default_missing
            )
            body = json.loads(rendered)
        except json.JSONDecodeError:
            raise ResponseFormatError(f"custom 模式 request_template 不是合法 JSON")
        url = base_url
    else:
        # openai_compatible 模式（默认）
        if request_template == "{input}":
            body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            }
        else:
            try:
                rendered = render_request_template(
                    request_template, prompt,
                    variables=variables, case_name=case_name, task_shape=task_shape,
                    default_missing=default_missing
                )
                body = json.loads(rendered)
                if "model" not in body:
                    body["model"] = model
                if "messages" not in body and "prompt" not in body:
                    body["messages"] = [{"role": "user", "content": prompt}]
            except json.JSONDecodeError:
                body = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                }
        url = f"{base_url.rstrip('/')}/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                url,
                headers=_build_headers(api_key, auth),
                cookies=_build_cookies(auth),
                json=body,
            )
            resp.raise_for_status()
            raw_response = resp.text
    except httpx.TimeoutException as e:
        raise NetworkError(f"API 请求超时: {e}")
    except httpx.ConnectError as e:
        raise NetworkError(f"API 连接失败: {e}")
    except httpx.HTTPStatusError as e:
        raise APIError(f"API 返回错误状态码 {e.response.status_code}: {e.response.text}", e.response.status_code)
    except (KeyError, ValueError, TypeError) as e:
        raise ResponseFormatError(f"API 返回格式错误: {e}")

    # 解析响应：response_parsing 优先，其次 response_mapping，最后 OpenAI 默认
    try:
        data = resp.json()
    except (ValueError, TypeError) as e:
        raise ResponseFormatError(f"API 返回非 JSON: {e}")

    if response_parsing is not None:
        # 四键模型：parser 层接管输出与 token
        out, out_found = extract_output(data, response_parsing.output_paths)
        if not out_found:
            if not response_parsing.output_paths:
                # A-1: output_paths 为空 → 输出 = 完整响应原文
                out = raw_response
            else:
                out = "[PARSE_ERROR] 未命中任何输出路径"
        else:
            # B-14: content 二次解包（此前只在 parse_response 生效，评测主链路漏接）
            out = _unpack_output(
                out, response_parsing.output_unpack_json, response_parsing.output_field
            )
        token_used, token_missing = count_tokens(
            data, response_parsing.token_paths,
            response_parsing.token_fields, response_parsing.token_scope,
        )
        return out, token_used, token_missing

    if response_mapping:
        # 旧设计：_extract_response 兼容
        output = _extract_response(json.dumps(data), response_mapping)
        token_used = data.get("usage", {}).get("total_tokens", 0)
        return output, token_used, False

    # OpenAI 兼容默认
    try:
        token_used = data.get("usage", {}).get("total_tokens", 0)
        raw_output = data["choices"][0]["message"]["content"]
    except (KeyError, TypeError) as e:
        raise ResponseFormatError(f"无法解析 API 响应: {e}")

    return raw_output, token_used, False


async def judge_with_llm(
    base_url: str,
    api_key: str,
    model: str,
    requirement: str,
    output: str,
    judge_prompt: Optional[str] = None,
    # T1-3: 双模式字段（可选，不传 = openai_compatible 旧行为）
    api_type: str = "openai_compatible",
    request_template: Optional[str] = None,
    auth: Optional[AuthConfig] = None,
    response_parsing: Optional[ResponseParsing] = None,
) -> tuple[float, int]:
    """用 Judge 模型打分，返回 (0-1 分数, judge 消耗 token 数)

    T1-3 双模式：
    - openai_compatible（默认）：messages + model 注入，旧数据零迁移
    - custom：request_template 渲染后请求，response_parsing 提取分数 + token

    T1-4: 返回值改为 (score, token_used)，token 计入评测成本。
    """
    DEFAULT_JUDGE_PROMPT = (
        "你是一个评测者。请判断被评测模型的输出是否满足要求。\n"
        "要求：{requirement}\n"
        "被评测输出：{output}\n"
        "只回答一个 0 到 1 之间的数字，表示满足程度（1 为完全满足，0 为完全不满足）。不要输出其他内容。"
    )

    prompt_template = judge_prompt or DEFAULT_JUDGE_PROMPT
    prompt_text = prompt_template.format(requirement=requirement, output=output)
    auth = auth or AuthConfig()

    # 前置参数校验（兜底，PUT /projects 已校验但 test_judge 端点可能直接调用）
    if api_type == "custom":
        if not request_template or not request_template.strip():
            raise ResponseFormatError("custom 模式 request_template 必填")
        if response_parsing is None:
            raise ResponseFormatError("custom 模式 response_parsing 必填（用于从自定义 API 响应中提取分数与 token）")
    else:
        if not model or not model.strip():
            raise ResponseFormatError("openai_compatible 模式 model 必填")

    if api_type == "custom":
        # custom 模式：走与 call_target 相同的路径
        try:
            rendered = render_request_template(
                request_template, prompt_text,
                variables=None, case_name="", task_shape="",
                default_missing="test",
            )
            body = json.loads(rendered)
        except json.JSONDecodeError:
            raise ResponseFormatError("custom 模式 request_template 不是合法 JSON")
        url = base_url

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    url,
                    headers=_build_headers(api_key, auth),
                    cookies=_build_cookies(auth),
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as e:
            raise NetworkError(f"Judge API 请求超时: {e}")
        except httpx.ConnectError as e:
            raise NetworkError(f"Judge API 连接失败: {e}")
        except httpx.HTTPStatusError as e:
            raise APIError(f"Judge API 返回错误状态码 {e.response.status_code}: {e.response.text}", e.response.status_code)
        except (KeyError, ValueError, TypeError) as e:
            raise ResponseFormatError(f"Judge API 返回格式错误: {e}")

        # 提取分数与 token（custom 模式必须配 response_parsing，前置守卫已校验）
        raw, found = extract_output(data, response_parsing.output_paths)
        if not found:
            raw = ""
        elif response_parsing.output_unpack_json:
            raw = _unpack_output(raw, response_parsing.output_unpack_json, response_parsing.output_field)
        token_used, _ = count_tokens(
            data, response_parsing.token_paths,
            response_parsing.token_fields, response_parsing.token_scope,
        )
    else:
        # openai_compatible 模式（默认）
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt_text}],
                        "temperature": 0,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as e:
            raise NetworkError(f"Judge API 请求超时: {e}")
        except httpx.ConnectError as e:
            raise NetworkError(f"Judge API 连接失败: {e}")
        except httpx.HTTPStatusError as e:
            raise APIError(f"Judge API 返回错误状态码 {e.response.status_code}: {e.response.text}", e.response.status_code)
        except (KeyError, ValueError, TypeError) as e:
            raise ResponseFormatError(f"Judge API 返回格式错误: {e}")

        try:
            raw = data["choices"][0]["message"]["content"].strip()
        except (KeyError, TypeError) as e:
            raise ResponseFormatError(f"无法解析 Judge 响应内容: {e}")
        token_used = data.get("usage", {}).get("total_tokens", 0)

    # 解析分数
    try:
        score = float(raw)
    except ValueError:
        # 兜底：尝试从字符串里抠第一个数字
        m = re.search(r"[-+]?\d*\.?\d+", raw)
        score = float(m.group()) if m else 0.0
    # W-5 3c: 附带 judge 原始响应字符串（用于 UI 下钻展示）
    return max(0.0, min(1.0, score)), token_used, raw
