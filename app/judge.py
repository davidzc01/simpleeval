"""LLM-as-Judge 调用（OpenAI Compatible）+ 被评测 API 调用"""

import httpx

DEFAULT_JUDGE_PROMPT = (
    "你是一个评测者。请判断被评测模型的输出是否满足要求。\n"
    "要求：{requirement}\n"
    "被评测输出：{output}\n"
    "只回答一个 0 到 1 之间的数字，表示满足程度（1 为完全满足，0 为完全不满足）。不要输出其他内容。"
)


async def call_target(base_url: str, api_key: str, model: str, prompt: str) -> tuple[str, int]:
    """调用被评测 API，返回 (输出文本, 消耗 token 数)"""
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
        output = data["choices"][0]["message"]["content"]
        token_used = data.get("usage", {}).get("total_tokens", 0)
        return output, token_used


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
        raw = data["choices"][0]["message"]["content"].strip()
        # 只取数字
        try:
            score = float(raw)
        except ValueError:
            # 兜底：尝试从字符串里抠第一个数字
            import re

            m = re.search(r"[-+]?\d*\.?\d+", raw)
            score = float(m.group()) if m else 0.0
        return max(0.0, min(1.0, score))
