"""Fixed generation settings and in-process replay of validated model steps."""
from collections import OrderedDict
from dataclasses import asdict, dataclass
import hashlib
import json

from agent_core import AgentError, Call, READ_ONLY_TOOLS, Turn


class ModelReplay:
    """A bounded cache shared by dialogs in one run with fixed model settings."""

    def __init__(self):
        self.steps = OrderedDict()

    async def step(self, model, tools):
        context = json.dumps([model.history, tools], ensure_ascii=False, sort_keys=True).encode("utf-8")
        key = hashlib.sha256(context).digest()
        saved = self.steps.get(key)
        if saved is not None:
            self.steps.move_to_end(key)
            turn, additions = json.loads(saved)
            model.history.extend(additions)
            return Turn([Call(**call) for call in turn["calls"]], turn["text"]), True

        before = len(model.history)
        turn = await model.step(tools)
        if (turn.calls or turn.text.strip()) and all(call.name in READ_ONLY_TOOLS for call in turn.calls):
            saved = json.dumps([asdict(turn), model.history[before:]], ensure_ascii=False).encode("utf-8")
            if len(saved) <= 256 * 1024:
                self.steps[key] = saved
                if len(self.steps) > 128:
                    self.steps.popitem(last=False)
        return turn, False


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
        if provider == "compatible":
            if model in {"openrouter/free", "openrouter/auto"} or model.endswith((":nitro", ":floor", ":online")):
                raise AgentError("Для --deterministic укажите конкретный ID модели вместо автоматического маршрутизатора.")
            from urllib.parse import urlsplit
            if urlsplit(base_url).hostname == "openrouter.ai" and not self.model_provider:
                raise AgentError("Для --deterministic с OpenRouter укажите --model-provider: точный slug провайдера выбранной модели.")


def add_generation_arguments(parser):
    parser.add_argument("--deterministic", action="store_true", help="Replay identical model steps in this run; fix model routing and sampling settings where supported")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--model-provider", default="", help="OpenRouter provider slug for deterministic routing")
    parser.add_argument("--max-output-tokens", type=int, default=4096, help="Limit generated tokens per model call (128..4096)")


def generation_from_args(args):
    config = Generation(getattr(args, "deterministic", False), getattr(args, "seed", 42),
                        getattr(args, "model_provider", ""), getattr(args, "max_output_tokens", 4096))
    config.validate(args.provider, args.model, getattr(args, "base_url", ""))
    return config
