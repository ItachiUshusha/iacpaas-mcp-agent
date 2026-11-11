"""Explicit, best-effort reproducible generation configuration."""
from dataclasses import dataclass

from agent_core import AgentError


@dataclass(frozen=True)
class Generation:
    deterministic: bool = False
    seed: int = 42
    model_provider: str = ""
    max_output_tokens: int = 4096

    def validate(self, provider, model, base_url=""):
        if not 128 <= self.max_output_tokens <= 4096:
            raise AgentError("--max-output-tokens должен быть от 128 до 4096.")
        if not self.deterministic:
            return
        if provider == "openai":
            raise AgentError("Воспроизводимый режим реализован для Ollama и compatible API; Responses API не имеет общего контракта seed.")
        if provider == "compatible":
            if model in {"openrouter/free", "openrouter/auto"} or model.endswith((":nitro", ":floor", ":online")):
                raise AgentError("Для --deterministic укажите конкретный ID модели вместо автоматического маршрутизатора.")
            from urllib.parse import urlsplit
            if urlsplit(base_url).hostname == "openrouter.ai" and not self.model_provider:
                raise AgentError("Для --deterministic с OpenRouter укажите --model-provider: точный slug провайдера выбранной модели.")


def add_generation_arguments(parser):
    parser.add_argument("--deterministic", action="store_true", help="Best-effort repeatability: temperature=0, fixed seed, fixed model/provider")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model-provider", default="", help="OpenRouter provider slug for deterministic routing")
    parser.add_argument("--max-output-tokens", type=int, default=4096, help="Limit generated tokens per model call (128..4096)")


def generation_from_args(args):
    config = Generation(getattr(args, "deterministic", False), getattr(args, "seed", 42),
                        getattr(args, "model_provider", ""), getattr(args, "max_output_tokens", 4096))
    config.validate(args.provider, args.model, getattr(args, "base_url", ""))
    return config
