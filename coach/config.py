"""Environment and .env settings for the coach."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_KEYS = {
    "AGX_BASE_URL",
    "AGX_API_KEY",
    "RACING_A_KEY",
    "RACING_B_KEY",
    "FPS_A_KEY",
    "FPS_B_KEY",
    "OMLX_BASE_URL",
    "OMLX_MODEL",
    "OMLX_API_KEY",
    "OMLX_TEMP",
    "OMLX_TOP_P",
    "OMLX_TOP_K",
    "POLL_SECONDS",
    "AGX_MAX_UPLOADS_PER_DAY",
    "UI_HOST",
    "UI_TOKEN",
    "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "OPENAI_API_KEY",
    "LLM_TEMP", "LLM_TOP_P", "LLM_TOP_K", "LLM_TOKEN_FIELD", "LLM_SEND_SAMPLING", "LLM_FALLBACK_MODELS",
}


@dataclass
class Config:
    base_url: str
    api_key: str = field(repr=False)
    racing_a: str = field(repr=False)
    racing_b: str = field(repr=False)
    fps_a: str = field(repr=False)
    fps_b: str = field(repr=False)
    omlx_url: str
    omlx_model: str
    omlx_key: str = field(repr=False)
    poll_seconds: int
    max_uploads: int
    omlx_temp: float = 0.3
    omlx_top_p: float = 0.95
    omlx_top_k: int = 20
    ui_host: str = "127.0.0.1"
    ui_token: str = field(default="", repr=False)
    llm_provider: str = "omlx"
    llm_token_field: str = "max_tokens"
    llm_send_sampling: bool = True
    llm_fallback_models: tuple[str, ...] = ()

    def account_key(self, game: str, slot: str) -> str:
        specific = {
            ("racing", "A"): self.racing_a,
            ("racing", "B"): self.racing_b,
            ("fps", "A"): self.fps_a,
            ("fps", "B"): self.fps_b,
        }[(game, slot)]
        if specific:
            return specific
        if slot == "A":
            return self.api_key
        return ""


def load(env_file: Path | None = None) -> Config:
    values = {}
    env_file = env_file if env_file is not None else ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in os.environ.items():
        if key in _KEYS and value != "":
            values[key] = value
    provider = values.get('LLM_PROVIDER', 'omlx').strip().lower()
    if provider not in ('omlx','openai','openai_compatible','openrouter'):
        raise ValueError('LLM_PROVIDER must be omlx, openai, openai_compatible, or openrouter')
    local = provider == 'omlx'
    defaults = {'omlx':'http://127.0.0.1:8000/v1','openai':'https://api.openai.com/v1',
                'openrouter':'https://openrouter.ai/api/v1','openai_compatible':''}
    url = values.get('LLM_BASE_URL') or (values.get('OMLX_BASE_URL') if local else None) or defaults[provider]
    model = values.get('LLM_MODEL') or (values.get('OMLX_MODEL') if local else None) or ('Qwen3.8-27B-4bit' if local else '')
    key = values.get('LLM_API_KEY') or (values.get('OMLX_API_KEY','') if local else values.get('OPENAI_API_KEY','') if provider == 'openai' else '')
    if not url or not model:
        raise ValueError('Set LLM_BASE_URL and LLM_MODEL for your model provider')
    if not local and not key:
        raise ValueError('Set LLM_API_KEY for your model provider (OPENAI_API_KEY also works for openai)')
    token_field = values.get('LLM_TOKEN_FIELD') or ('max_completion_tokens' if provider == 'openai' else 'max_tokens')
    if token_field not in ('max_tokens','max_completion_tokens'):
        raise ValueError('LLM_TOKEN_FIELD must be max_tokens or max_completion_tokens')
    fallback = values.get('LLM_FALLBACK_MODELS', '')
    fallback_models = tuple(x.strip() for x in fallback.split(',') if x.strip())
    if 'LLM_FALLBACK_MODELS' not in values and local:
        fallback_models = ('Qwen3.8-27B-4bit','Qwen3.8-27B-DFlash2-4bit','mlx-community--Qwen3.5-2B-4bit')
    return Config(
        base_url=values.get("AGX_BASE_URL", "https://agx.cool"),
        api_key=values.get("AGX_API_KEY", ""),
        racing_a=values.get("RACING_A_KEY", ""),
        racing_b=values.get("RACING_B_KEY", ""),
        fps_a=values.get("FPS_A_KEY", ""),
        fps_b=values.get("FPS_B_KEY", ""),
        # Retain these attributes for existing integrations and saved configurations.
        omlx_url=url.rstrip('/'), omlx_model=model, omlx_key=key,
        poll_seconds=max(15, int(values.get("POLL_SECONDS", "60") or 60)),
        max_uploads=max(0, int(values.get("AGX_MAX_UPLOADS_PER_DAY", "0") or 0)),
        omlx_temp=min(2.0, max(0.0, float(values.get('LLM_TEMP') or values.get("OMLX_TEMP", "0.3") or 0.3))),
        omlx_top_p=min(1.0, max(0.05, float(values.get('LLM_TOP_P') or values.get("OMLX_TOP_P", "0.95") or 0.95))),
        omlx_top_k=max(1, int(values.get('LLM_TOP_K') or values.get("OMLX_TOP_K", "20") or 20)),
        ui_host=values.get("UI_HOST", "127.0.0.1") or "127.0.0.1",
        ui_token=values.get("UI_TOKEN", "") or "",
        llm_provider=provider, llm_token_field=token_field,
        llm_send_sampling=values.get('LLM_SEND_SAMPLING', 'false' if provider == 'openai' else 'true').lower() in ('true','1','yes'),
        llm_fallback_models=fallback_models,
    )
