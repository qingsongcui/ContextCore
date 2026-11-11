"""Official endpoint hints, not account entitlement or host-compatibility claims.

Read-only local data. Listing these presets never contacts a provider, resolves a
credential, installs a host configuration or runs a model. URLs were checked
against the linked primary documentation on 2026-10-26.
"""
from app.services.provider_checks import SUPPORTED


OFFICIAL_PROVIDER_PRESETS = (
    {
        "id": "openai", "name": "OpenAI", "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "gpt-5",
        "category": "official",
        "source_url": "https://developers.openai.com/api/reference/overview/",
    },
    {
        "id": "anthropic", "name": "Anthropic", "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "supported_endpoints": ["messages"],
        "default_model": "claude-sonnet-4-20250514",
        "category": "official",
        "source_url": "https://platform.claude.com/docs/en/api/overview",
    },
    {
        "id": "openrouter", "name": "OpenRouter", "protocol": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "anthropic/claude-sonnet-4",
        "category": "aggregator",
        "source_url": "https://openrouter.ai/docs/ quickstart",
    },
    {
        "id": "deepseek", "name": "DeepSeek", "protocol": "openai",
        "base_url": "https://api.deepseek.com",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "deepseek-chat",
        "category": "cn_cloud",
        "api_format_source": "https://api-docs.deepseek.com/guides/responses_api/",
        "source_url": "https://api-docs.deepseek.com/",
    },
    {
        "id": "gemini", "name": "Google Gemini", "protocol": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "supported_endpoints": ["generateContent"],
        "default_model": "gemini-2.5-pro",
        "category": "official",
        "source_url": "https://ai.google.dev/api",
    },
    {
        "id": "siliconflow", "name": "SiliconFlow (硅基流动)", "protocol": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "deepseek-ai/DeepSeek-V3",
        "category": "cn_cloud",
        "source_url": "https://docs.siliconflow.cn/",
    },
    {
        "id": "volcengine-ark", "name": "Volcengine Ark (火山引擎豆包)", "protocol": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "doubao-seed-1-6-250615",
        "category": "cn_cloud",
        "source_url": "https://www.volcengine.com/docs/82379",
    },
    {
        "id": "aliyun-bailian", "name": "Aliyun Bailian (阿里云百炼 / 千问)", "protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "qwen3-coder-plus",
        "category": "cn_cloud",
        "source_url": "https://help.aliyun.com/zh/model-studio/",
    },
    {
        "id": "zhipu-glm", "name": "Zhipu GLM (智谱 BigModel)", "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "glm-4.5",
        "category": "cn_cloud",
        "source_url": "https://open.bigmodel.cn/dev/howuse/introduction",
    },
    {
        "id": "moonshot-kimi", "name": "Moonshot Kimi (月之暗面)", "protocol": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "kimi-k2-0711-preview",
        "category": "cn_cloud",
        "source_url": "https://platform.moonshot.cn/docs/",
    },
    {
        "id": "minimax", "name": "MiniMax (稀宇科技)", "protocol": "openai",
        "base_url": "https://api.minimax.chat/v1",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "MiniMax-M1",
        "category": "cn_cloud",
        "source_url": "https://platform.minimaxi.com/document/",
    },
    {
        "id": "xai-grok", "name": "xAI Grok", "protocol": "openai",
        "base_url": "https://api.x.ai/v1",
        "supported_endpoints": ["chat/completions", "responses"],
        "default_model": "grok-4",
        "category": "official",
        "source_url": "https://docs.x.ai/",
    },
)


def list_presets() -> dict:
    return {
        "presets": [{**item, "model_catalog_supported": item["protocol"] in SUPPORTED}
                    for item in OFFICIAL_PROVIDER_PRESETS],
        "network_access": False,
        "account_access": "not_checked",
        "host_compatibility": "requires_selected_host_preview",
    }

