"""Explicit text-provider presets. Speech and safety review remain local.

Cloud mode is opt-in through server configuration. It has not been exercised
against paid vendor accounts; local mode is the acceptance-tested default.
No arbitrary API URL or browser-submitted credentials are accepted.
"""
import httpx

from .providers import LocalProviders, ProviderError

ENDPOINTS = {
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    "deepseek": "https://api.deepseek.com/chat/completions",
}


class ConfiguredProviders(LocalProviders):
    def __init__(self, settings):
        super().__init__(settings)
        self.text_provider = settings.llm_provider
        self.cloud_model = settings.cloud_model
        self.cloud_key = settings.cloud_api_key
        self.cloud = httpx.AsyncClient(timeout=settings.model_timeout_s, trust_env=False, follow_redirects=False)
        self.mode = "local" if self.text_provider == "local" else "hybrid"

    async def generate(self, messages, mode="story"):
        if self.text_provider == "local":
            return await super().generate(messages, mode)
        payload = {"model": self.cloud_model, "messages": messages, "stream": False, "max_tokens": 160, "temperature": 0.6}
        if self.text_provider == "qwen":
            payload["enable_thinking"] = False
        else:
            payload["thinking"] = {"type": "disabled"}
        response = await self.cloud.post(ENDPOINTS[self.text_provider], json=payload, headers={"Authorization": "Bearer " + self.cloud_key})
        if not response.is_success:
            raise ProviderError("Configured text provider failed")
        try:
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
            return content.strip()
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise ProviderError("Malformed text provider result") from exc

    async def health(self):
        result = await super().health()
        result["local_only"] = self.mode == "local"
        result["text_provider"] = self.text_provider
        result["cloud_live_verified"] = False if self.mode == "hybrid" else None
        return result

    async def close(self):
        await self.cloud.aclose()
        await super().close()
