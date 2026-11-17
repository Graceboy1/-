"""Verify a session model against the same protocol used by the research desk."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from .agent import ModelConfig, ModelFailure, generate_plan
from .seats import BASELINE


# Browser input is not a generic outbound proxy. Admin-managed custom endpoints
# (including local model servers) can instead use the existing environment preset.
BROWSER_MODEL_BASES = frozenset({
    "https://api.deepseek.com",
    "https://api.deepseek.com/v1",
    "https://api.openai.com/v1",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
})
VERIFY_TIMEOUT_SECONDS = 28


def _browser_config(base_url: str | None, model: str | None, api_key: str | None) -> ModelConfig:
    base, name, key = (value.strip() if isinstance(value, str) else "" for value in (base_url, model, api_key))
    if not (1 <= len(base) <= 300 and 1 <= len(name) <= 120 and 1 <= len(key) <= 1024):
        raise ModelFailure("请完整填写 API 地址、模型名和 API Key。", 400)
    if any(not 33 <= ord(char) <= 126 for char in name + key) or key.lower().startswith("replace-"):
        raise ModelFailure("模型名或 API Key 格式不正确，请检查是否有空格或仍是占位值。", 400)

    try:
        if any(not 33 <= ord(char) <= 126 for char in base) or any(char in base for char in "?#\\"):
            raise ValueError
        parts = urlsplit(base)
        host = parts.hostname
        if (parts.scheme != "https" or not host or parts.netloc.lower() not in (host, f"{host}:443")
                or parts.port not in (None, 443)):
            raise ValueError
        canonical = f"https://{host}{parts.path.rstrip('/')}"
        if canonical not in BROWSER_MODEL_BASES:
            raise ValueError
    except ValueError:
        raise ModelFailure("网页只接受受支持的 HTTPS API 基础地址（DeepSeek、OpenAI 或通义千问），不含 /chat/completions。自定义服务请使用服务器预设。", 400) from None
    return ModelConfig(canonical, name, key)


async def verify_workspace_model(
    *, base_url: str | None = None, model: str | None = None, api_key: str | None = None,
    use_server_config: bool = False,
) -> ModelConfig:
    """Test tool calling and JSON output using only pre-cutoff synthetic evidence."""
    if use_server_config:
        if any(value is not None for value in (base_url, model, api_key)):
            raise ModelFailure("请选择服务器预设或手动填写 API，不要同时提交。", 400)
        config = ModelConfig.from_env()
        if config is None:
            raise ModelFailure("服务器未配置可用的模型预设。", 400)
    else:
        config = _browser_config(base_url, model, api_key)

    try:
        snapshot = BASELINE.read_evidence(BASELINE.cases["normal-fill"])
        plan, _ = await asyncio.wait_for(generate_plan(config, snapshot), timeout=VERIFY_TIMEOUT_SECONDS)
        result = BASELINE.validate(plan, snapshot)
    except asyncio.TimeoutError:
        raise ModelFailure("验证模型超时，请检查服务连通性后重试。", 504) from None
    except ModelFailure as exc:
        # The existing adapter constructs static user-facing messages; do not
        # retain a provider exception chain, response, or submitted credential.
        raise ModelFailure(exc.detail, exc.status) from None
    except Exception:
        raise ModelFailure("验证模型失败，请检查地址、模型名和 API Key 后重试。") from None
    if not result["ok"]:
        raise ModelFailure("模型已响应，但测试预案没有通过独立校验。请重试或改用支持工具调用和 JSON 输出的模型。", 502)
    return config
