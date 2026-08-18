"""LLM-as-Judge 调用（OpenAI Compatible）+ 被评测 API 调用"""

import httpx
import re


class APIError(Exception):
    """API 调用错误基类"""
    def __init__(self, message: str, status_code: int | None = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class NetworkError(APIError):
    """网络错误（连接失败、超时等）"""
    pass


class ResponseFormatError(APIError):
    """API 返回格式错误"""
    pass


DEFAULT_JUDGE_PROMPT = (
    "你是一个评测者。请判断被评测模型的输出是否满足要求。\n"
    "要求：{requirement}\n"
    "被评测输出：{output}\n"
    "只回答一个 0 到 1 之间的数字，表示满足程度（1 为完全满足，0 为完全不满足）。不要输出其他内容。"
)


async def call_target(base_url: str, api_key: str, model: str, prompt: str) -> tuple[str, int]:
    """调用被评测 API，返回 (输出文本, 消耗 token 数)"""
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
        raise NetworkError(f"API 请求超时: {e}")
    except httpx.ConnectError as e:
        raise NetworkError(f"API 连接失败: {e}")
    except httpx.HTTPStatusError as e:
        raise APIError(f"API 返回错误状态码 {e.response.status_code}: {e.response.text}", e.response.status_code)
    except (KeyError, ValueError, TypeError) as e:
        raise ResponseFormatError(f"API 返回格式错误: {e}")

    try:
        output = data["choices"][0]["message"]["content"]
        token_used = data.get("usage", {}).get("total_tokens", 0)
        return output, token_used
    except (KeyError, TypeError) as e:
        raise ResponseFormatError(f"无法解析 API 响应: {e}")


async def judge_with_llm(
    base_url: str,
    api_key: str,
    model: str,
    requirement: str,
    output: str,
    judge_prompt: str = DEFAULT_JUDGE_PROMPT,
) -> float:
    """用 Judge 模型打分，返回 0-1 的分数"""
    prompt = judge_prompt.format(requirement=requirement, output=output)
    
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
