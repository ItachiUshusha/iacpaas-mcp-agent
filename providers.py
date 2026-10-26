"""OpenAI Responses API and local Ollama tool calling. No provider secrets in traces."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx

from agent_core import AgentError, Call, Turn

MAX_MODEL_BYTES = 2_000_000
MAX_CONTEXT_BYTES = 1_000_000


async def post_json(client, url, payload, headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_CONTEXT_BYTES:
        raise AgentError("Контекст диалога превысил 1 МБ. Начните новый запуск с более узким вопросом.")
    try:
        async with client.stream("POST", url, content=body, headers={"Content-Type": "application/json", **(headers or {})}) as response:
            if response.status_code >= 300:
                raise AgentError(f"Модель: HTTP {response.status_code}. Проверьте доступ, имя модели и лимиты провайдера.")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_MODEL_BYTES:
                    raise AgentError("Ответ модели превысил 2 МБ.")
                chunks.append(chunk)
        result = json.loads(b"".join(chunks))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (httpx.HTTPError, ValueError, UnicodeError):
        raise AgentError("Не удалось получить JSON от модели. Проверьте подключение и запущенный сервис.") from None


class OpenAIModel:
    def __init__(self, client, model, key, instructions):
        self.client, self.model, self.key, self.instructions = client, model, key, instructions
        self.history = []

    def add_user(self, text):
        self.history.append({"role": "user", "content": text})

    def add_result(self, call, result):
        self.history.append({"type": "function_call_output", "call_id": call.id,
                             "output": json.dumps(result, ensure_ascii=False)})

    async def step(self, tools):
        definitions = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
                        "parameters": t["inputSchema"], "strict": False} for t in tools]
        result = await post_json(self.client, "https://api.openai.com/v1/responses", {
            "model": self.model, "instructions": self.instructions, "input": self.history,
            "tools": definitions, "parallel_tool_calls": False, "store": False,
            "include": ["reasoning.encrypted_content"], "max_output_tokens": 4096,
        }, {"Authorization": f"Bearer {self.key}"})
        if result.get("status") != "completed" or not isinstance(result.get("output"), list):
            raise AgentError("Модель не завершила ответ. Проверьте лимит вывода и поддержку tool calling.")
        self.history.extend(result["output"])
        calls, text = [], []
        for item in result["output"]:
            if item.get("type") == "function_call":
                if not all(isinstance(item.get(k), str) for k in ("call_id", "name", "arguments")):
                    raise AgentError("Модель вернула некорректный function_call.")
                calls.append(Call(item["call_id"], item["name"], item["arguments"]))
            elif item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text.append(part["text"])
                    elif part.get("type") == "refusal":
                        text.append(part.get("refusal", "Модель отказалась выполнить запрос."))
        return Turn(calls, "\n".join(text))


class OllamaModel:
    def __init__(self, client, model, instructions):
        self.client, self.model = client, model
        self.history = [{"role": "system", "content": instructions}]

    def add_user(self, text):
        self.history.append({"role": "user", "content": text})

    def add_result(self, call, result):
        self.history.append({"role": "tool", "tool_name": call.name, "content": json.dumps(result, ensure_ascii=False)})

    async def step(self, tools):
        definitions = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                        "parameters": t["inputSchema"]}} for t in tools]
        result = await post_json(self.client, "http://127.0.0.1:11434/api/chat", {
            "model": self.model, "messages": self.history, "tools": definitions, "stream": False,
            "options": {"num_predict": 4096},
        })
        message = result.get("message")
        if result.get("done") is not True or not isinstance(message, dict) or message.get("role") != "assistant":
            raise AgentError("Ollama вернула незавершённый или неожиданный ответ.")
        if result.get("done_reason") == "length":
            raise AgentError("Модель Ollama достигла лимита вывода. Итоговый ответ не завершён.")
        self.history.append(message)
        calls = []
        for i, item in enumerate(message.get("tool_calls") or []):
            function = item.get("function", {})
            if not isinstance(function.get("name"), str):
                raise AgentError("Ollama вернула некорректный вызов инструмента.")
            calls.append(Call(str(i), function["name"], function.get("arguments")))
        return Turn(calls, message.get("content") or "")


class CompatibleModel:
    """Chat completions with tool calling, e.g. free models on OpenRouter."""
    def __init__(self, client, model, key, instructions, base_url):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"))) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AgentError("Адрес API должен быть HTTPS либо локальным HTTP, без пароля и параметров в URL.")
        self.client, self.model, self.key = client, model, key
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.history = [{"role": "system", "content": instructions}]

    def add_user(self, text):
        self.history.append({"role": "user", "content": text})

    def add_result(self, call, result):
        self.history.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False)})

    async def step(self, tools):
        definitions = [{"type": "function", "function": {"name": t["name"],
            "description": t.get("description", ""), "parameters": t["inputSchema"]}} for t in tools]
        payload = {"model": self.model, "messages": self.history, "tools": definitions,
                   "stream": False, "max_tokens": 4096}
        if urlsplit(self.url).hostname == "openrouter.ai":
            # Require tool support; never let automatic routing use a paid fallback.
            payload["provider"] = {"require_parameters": True, "max_price": {"prompt": 0, "completion": 0}}
        result = await post_json(self.client, self.url, payload,
                                 {"Authorization": f"Bearer {self.key}"} if self.key else {})
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AgentError("API модели не вернул choices. Проверьте поддержку Chat Completions.")
        choice = choices[0]
        message = choice.get("message")
        if choice.get("finish_reason") not in ("stop", "tool_calls") or not isinstance(message, dict) or message.get("role") != "assistant":
            raise AgentError("Модель не завершила ответ. Проверьте лимит вывода и поддержку инструментов.")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise AgentError("Модель вернула неожиданный формат текста.")
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise AgentError("Модель вернула некорректные tool_calls.")
        calls = []
        for item in raw_calls:
            if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                raise AgentError("Модель вернула некорректный вызов инструмента.")
            f = item["function"]
            if not all(isinstance(v, str) and v for v in (item.get("id"), f.get("name"))) or not isinstance(f.get("arguments"), str):
                raise AgentError("Модель вернула некорректные аргументы инструмента.")
            calls.append(Call(item["id"], f["name"], f["arguments"]))
        if len({c.id for c in calls}) != len(calls):
            raise AgentError("Модель вернула повторяющиеся ID вызовов.")
        # Preserve provider continuation fields, including reasoning_details.
        self.history.append(message)
        return Turn(calls, content or "")
