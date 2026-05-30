"""DeepSeek V4 大模型调用封装 —— 基于 OpenAI SDK。

Thinking 模式参数说明（以 DeepSeek 官方 thinking_mode 文档 2026-05 为准）:
    - 开启 Thinking: extra_body={"thinking": {"type": "enabled"}}
    - 关闭 Thinking: extra_body={"thinking": {"type": "disabled"}}
    - 推理强度: reasoning_effort="high"（普通）或 "max"（最强）
    - 注意: Thinking 模式下 temperature/top_p 不生效（不报错但被忽略）
    - json_mode + Thinking 可共存（response_format 在思考完成后约束输出格式）

    ▶ 若官方参数变更，修改以下两处:
       1. _build_kwargs() 中的 extra_body["thinking"]["type"] 取值 ("enabled"/"disabled")
       2. _build_kwargs() 中的 reasoning_effort 取值 ("high"/"max")

提供:
    call_model(system_prompt, user_content, thinking, json_mode) -> str
"""

from __future__ import annotations

from openai import OpenAI

from config import get_settings

# ======================================================================
# 异常
# ======================================================================


class LLMError(Exception):
    """LLM 调用失败时抛出，附带可读原因。"""
    pass


# ======================================================================
# 客户端（单例）
# ======================================================================

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """返回全局唯一的 OpenAI 客户端实例（指向 DeepSeek API）。"""
    global _client
    if _client is None:
        settings = get_settings()
        _client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
    return _client


# ======================================================================
# 参数构建
# ======================================================================

def _build_kwargs(
    system_prompt: str,
    user_content: str,
    *,
    thinking: bool,
    json_mode: bool,
) -> dict:
    """组装 chat.completions.create(**kwargs) 的参数。

    Args:
        system_prompt: 系统提示词。
        user_content:  用户消息（已组装好的上下文）。
        thinking:      是否启用 Thinking 推理模式。
        json_mode:     是否要求模型输出纯 JSON。

    Returns:
        可直接解包传给 client.chat.completions.create(**kwargs) 的参数字典。
    """
    settings = get_settings()

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    kwargs: dict = {
        "model": settings.deepseek_model,
        "messages": messages,
        "max_tokens": 32_768,  # DeepSeek V4 最大输出 384K，这里设一个合理上限
    }

    # ---- Thinking 模式 ----
    # 参数来源: DeepSeek Thinking Mode 官方文档
    #   https://api-docs.deepseek.com/guides/thinking_mode
    if thinking:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        kwargs["reasoning_effort"] = "high"  # "high" | "max"
        # Thinking 模式下 temperature 不生效，但 SDK 需要传一个值
        kwargs["temperature"] = 1.0
    else:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        kwargs["temperature"] = 0.2  # Non-Thinking 用低温度，输出更确定

    # ---- JSON 模式 ----
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    return kwargs


# ======================================================================
# 公共 API
# ======================================================================

def call_model(
    system_prompt: str,
    user_content: str,
    *,
    thinking: bool = False,
    json_mode: bool = False,
) -> str:
    """调用 DeepSeek V4 模型，返回模型输出的纯文本内容。

    Args:
        system_prompt: 系统提示词（指定角色与输出要求）。
        user_content:  用户消息（PR 变更上下文）。
        thinking:      是否启用 Thinking 推理链（默认 False）。
        json_mode:     是否要求 JSON 输出（默认 False）。

    Returns:
        模型返回的 message.content 字符串。

    Raises:
        LLMError: 网络错误、超时、API 错误、返回空内容等。
    """
    settings = get_settings()
    client = _get_client()

    kwargs = _build_kwargs(
        system_prompt=system_prompt,
        user_content=user_content,
        thinking=thinking,
        json_mode=json_mode,
    )

    try:
        response = client.chat.completions.create(
            timeout=float(settings.request_timeout_sec),
            **kwargs,
        )
    except Exception as exc:
        # openai SDK 可能抛出多种异常:
        #   openai.APITimeoutError, openai.APIConnectionError,
        #   openai.APIError, openai.RateLimitError, ...
        raise LLMError(
            f"DeepSeek API 调用失败: {type(exc).__name__}: {exc}\n"
            f"请检查:\n"
            f"  1) DEEPSEEK_API_KEY 是否有效\n"
            f"  2) 网络是否能访问 {settings.deepseek_base_url}\n"
            f"  3) 模型名 {settings.deepseek_model} 是否可用"
        ) from exc

    # 提取内容
    choice = response.choices[0]
    content = choice.message.content

    # Thinking 模式下可打印推理链长度（调试用）
    reasoning = getattr(choice.message, "reasoning_content", None)
    if reasoning:
        # 推理链存在但不会出现在最终 content 中，仅记一条 debug 级信息
        pass  # 如需要可在此打日志

    if content is None:
        # 极罕见情况: 模型返回了 reasoning 但 content 为空
        raise LLMError(
            "模型返回空内容（可能为 API 内部错误或 Thinking 模式异常）。"
            f"reasoning_content 长度: {len(reasoning) if reasoning else 0}"
        )

    return content


# ======================================================================
# 自测
# ======================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("LLMClient 自测")
    print("=" * 70)

    # ---- 测试 1: Non-Thinking 简单调用 ----
    print("\n--- 测试 1: Non-Thinking 调用 ---")
    try:
        result = call_model(
            system_prompt="你是一个代码助手。请用中文回答。",
            user_content="用一句话解释什么是 Python GIL。",
            thinking=False,
            json_mode=False,
        )
        print(f"返回内容 ({len(result)} 字符):")
        print(result[:300])
    except LLMError as e:
        print(f"[FAIL] {e}")

    # ---- 测试 2: Non-Thinking + JSON 模式 ----
    print("\n--- 测试 2: Non-Thinking + JSON 模式 ---")
    try:
        result = call_model(
            system_prompt="你是 JSON 生成器。只输出 JSON 对象，不要任何额外文字。",
            user_content='生成一个 JSON 对象，包含字段: name="Alice", age=30, city="Beijing"。',
            thinking=False,
            json_mode=True,
        )
        print(f"返回内容 ({len(result)} 字符):")
        print(result[:300])
    except LLMError as e:
        print(f"[FAIL] {e}")

    # ---- 测试 3: Thinking 模式 ----
    print("\n--- 测试 3: Thinking 调用 ---")
    try:
        result = call_model(
            system_prompt="你是一位资深代码审查专家。",
            user_content="下面这段代码有什么潜在的逻辑问题？\n\n"
                         "```python\n"
                         "def divide(a, b):\n"
                         "    return a / b\n"
                         "```",
            thinking=True,
            json_mode=False,
        )
        print(f"返回内容 ({len(result)} 字符):")
        print(result[:400])
    except LLMError as e:
        print(f"[FAIL] {e}")

    print("\n" + "=" * 70)
    print("自测完成 ✓")
    print("=" * 70)
