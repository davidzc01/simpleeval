"""LLM-as-Judge 调用（OpenAI Compatible）+ 被评测 API 调用"""

import json
import httpx
import re
from typing import Optional, Union

from .models import AuthConfig, ResponseMapping


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


def _build_headers(api_key: str, auth: AuthConfig) -> dict:
    """构建请求头"""
    headers = {"Authorization": f"Bearer {api_key}"}

    if auth.type == "bearer" and auth.bearer_token:
        headers["Authorization"] = f"Bearer {auth.bearer_token}"
    elif auth.type == "api_key" and auth.api_key_value:
        header_name = auth.api_key_header or "X-API-Key"
        headers[header_name] = auth.api_key_value
    elif auth.type == "headers":
        for h in auth.headers:
            headers[h.get("name", "")] = h.get("value", "")

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
) -> tuple[str, int]:
    """调用被评测 API，返回 (输出文本, 消耗 token 数)"""
    auth = auth or AuthConfig()
    response_mapping = response_mapping or []

    # 构建请求体
    if request_template == "{input}":
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
    else:
        try:
            body = json.loads(request_template.replace("{input}", prompt))
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

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
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

    # 提取 token 使用量
    try:
        data = resp.json()
        token_used = data.get("usage", {}).get("total_tokens", 0)
        raw_output = data["choices"][0]["message"]["content"]
    except (KeyError, TypeError) as e:
        raise ResponseFormatError(f"无法解析 API 响应: {e}")

    # 应用响应映射
    output = _extract_response(json.dumps(data), response_mapping) if response_mapping else raw_output

    return output, token_used


async def judge_with_llm(
    base_url: str,
    api_key: str,
    model: str,
    requirement: str,
    output: str,
    judge_prompt: Optional[str] = None,
) -> float:
    """用 Judge 模型打分，返回 0-1 的分数"""
    DEFAULT_JUDGE_PROMPT = (
        "你是一个评测者。请判断被评测模型的输出是否满足要求。\n"
        "要求：{requirement}\n"
        "被评测输出：{output}\n"
        "只回答一个 0 到 1 之间的数字，表示满足程度（1 为完全满足，0 为完全不满足）。不要输出其他内容。"
    )

    prompt_template = judge_prompt or DEFAULT_JUDGE_PROMPT
    prompt = prompt_template.format(requirement=requirement, output=output)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
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

    # 只取数字
    try:
        score = float(raw)
    except ValueError:
        # 兜底：尝试从字符串里抠第一个数字
        m = re.search(r"[-+]?\d*\.?\d+", raw)
        score = float(m.group()) if m else 0.0
    return max(0.0, min(1.0, score))
