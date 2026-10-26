"""Small bounded function-calling runtime, independent of provider and MCP transport."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import jsonschema

ALLOWED_TOOLS = {"list_inforesources", "get_inforesource", "get_subgraph", "count_graph"}


class AgentError(Exception):
    """A curated, credential-free message suitable for the terminal."""


@dataclass
class Call:
    id: str
    name: str
    arguments: object


@dataclass
class Turn:
    calls: list[Call] = field(default_factory=list)
    text: str = ""


def failure(code, message, hint):
    return {"ok": False, "data": None, "error": {"code": code, "message": message, "hint": hint}}


class Trace:
    def __init__(self, directory: Path, secrets=()):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"agent-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:8]}.jsonl"
        self.secrets = [s for s in secrets if s]
        self.stream = self.path.open("x", encoding="utf-8")

    def clean(self, value):
        if isinstance(value, dict):
            sensitive = {"password", "token", "accesstoken", "authorization", "apikey", "xapikey", "cookie", "refreshtoken"}
            return {k: "[REDACTED]" if k.lower().replace("_", "").replace("-", "") in sensitive else self.clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
        return value

    def add(self, event, **data):
        record = {"time_utc": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        self.stream.write(json.dumps(self.clean(record), ensure_ascii=False) + "\n")
        self.stream.flush()

    def close(self):
        self.stream.close()


class Agent:
    def __init__(self, session, model, trace, max_rounds=8, max_calls=16):
        self.session, self.model, self.trace = session, model, trace
        self.max_rounds, self.max_calls = max_rounds, max_calls
        self.tools = {}
        self.known_paths = set()

    async def initialize(self):
        await self.session.initialize()
        for tool in (await self.session.list_tools()).tools:
            if tool.name not in ALLOWED_TOOLS:
                continue
            if not tool.annotations or tool.annotations.readOnlyHint is not True:
                raise AgentError("Сервер не подтвердил read-only для ожидаемого инструмента.")
            item = tool.model_dump(mode="json")
            item["inputSchema"]["additionalProperties"] = False
            self.tools[tool.name] = item
        if set(self.tools) != ALLOWED_TOOLS:
            raise AgentError("MCP-сервер не предоставил четыре ожидаемых инструмента чтения.")
        self.trace.add("tools_discovered", names=sorted(self.tools))

    async def execute(self, call):
        if call.name not in self.tools:
            return failure("POLICY_DENIED", "Инструмент запрещён политикой чтения.", "Используйте один из опубликованных инструментов; запись недоступна.")
        try:
            arguments = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
            jsonschema.validate(arguments, self.tools[call.name]["inputSchema"])
        except (ValueError, jsonschema.ValidationError, TypeError):
            return failure("INVALID_ARGUMENT", "Аргументы не соответствуют JSON Schema.", "Исправьте аргументы по схеме; глубина 0..3, max_nodes 1..200.")
        if call.name != "list_inforesources" and arguments.get("path") not in self.known_paths:
            return failure("PATH_NOT_DISCOVERED", "Путь ещё не подтверждён каталогом.", "Сначала вызовите list_inforesources, затем используйте точный path из результата.")
        try:
            response = await self.session.call_tool(call.name, arguments)
        except Exception:
            return failure("MCP_CONNECTION", "Сбой соединения с MCP-сервером.", "Проверьте, что сервер запущен; не подменяйте отсутствующий результат догадкой.")
        result = response.structuredContent
        if result is None:
            return failure("MCP_TOOL_ERROR", "Инструмент вернул ошибку без структурированных данных.", "Проверьте параметры по схеме и журнал сервера.")
        try:
            jsonschema.validate(result, self.tools[call.name]["outputSchema"])
        except (jsonschema.ValidationError, TypeError):
            return failure("MCP_OUTPUT_SCHEMA", "Ответ нарушает выходную схему.", "Прекратите опираться на этот ответ; проверьте версию сервера.")
        if response.isError or not result.get("ok"):
            if result.get("ok"):
                return failure("MCP_TOOL_ERROR", "Сервер отметил результат как ошибку.", "Не используйте его как успешные данные.")
            return result
        if call.name == "list_inforesources":
            self.known_paths.update(row["path"] for row in result["data"]["resources"])
        return result

    async def answer(self, question):
        if len(question) > 8000:
            raise AgentError("Вопрос длиннее 8000 символов. Сократите запрос.")
        self.trace.add("user", text=question)
        self.model.add_user(question)
        calls = 0
        for round_index in range(self.max_rounds):
            # The model must discover exact paths before being offered graph reads.
            visible = list(self.tools.values()) if self.known_paths else [self.tools["list_inforesources"]] if self.tools else []
            turn = await self.model.step(visible)
            self.trace.add("model_turn", round=round_index + 1, tool_calls=len(turn.calls))
            if not turn.calls:
                if not turn.text.strip():
                    raise AgentError("Модель не вернула ни ответ, ни вызов инструмента.")
                self.trace.add("assistant", text=turn.text)
                return turn.text
            if calls + len(turn.calls) > self.max_calls:
                raise AgentError("Достигнут лимит вызовов инструментов. Уточните или сузьте запрос.")
            for call in turn.calls:
                result = await self.execute(call)
                calls += 1
                self.trace.add("tool_call", name=call.name, arguments=call.arguments, result=result)
                self.model.add_result(call, result)
        raise AgentError("Достигнут лимит шагов модели; итоговый ответ не получен. Сузьте запрос.")
