"""Small bounded function-calling runtime, independent of provider and MCP transport."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

import jsonschema

from answer_evidence import collect_evidence, audit_answer, revalidate_evidence
from domain import Fault, validate_concept_start

READ_TOOLS = {"list_inforesources", "get_inforesource", "get_subgraph", "count_graph"}
SEARCH_TOOLS = {"search_inforesource", "compare_node_children", "rank_records_by_percentage",
                "search_fund_content", "analyze_gas_composition", "read_export_node"}
DISCOVERY_TOOLS = {"list_inforesources", "list_fund_resources", "search_fund_content"}
READ_ONLY_TOOLS = READ_TOOLS | SEARCH_TOOLS | DISCOVERY_TOOLS
WRITE_TOOLS = {"create_folder", "delete_folder"}
ALLOWED_TOOLS = READ_ONLY_TOOLS | WRITE_TOOLS
COMPOSITION_QUERY = re.compile(r"(?:примес|состав|содержан|процент|доля|долю|чистот|на основе)", re.IGNORECASE)
GAS_QUERY = re.compile(r"(?:газ|аргон|водород|гелий|гелия)", re.IGNORECASE)
GAS_DATABASE = "База технологических газов"
FUND_LIST_QUERY = re.compile(
    r"(?:(?:покажи|перечисли|выведи|какие)\s+)?"
    r"(?:базы(?:\s+данных)?|ресурсы|список\s+(?:баз|ресурсов))"
    r"(?:\s+(?:тебе\s+)?(?:доступны|есть))?\s+"
    r'(?:из\s+моего\s+фонда|в\s+[«"]?мо[её]м\s+фонде[»"]?|'
    r'(?:в|из)\s+[«"]?мой\s+фонд[»"]?)[?!.]?',
    re.IGNORECASE,
)
GAS_COMPARISON_QUERY = re.compile(
    r"(?:какие\s+)?общие\s+примеси\s+(?:(?:указаны|есть)\s+)?у\s+газов\s+на\s+основе\s+"
    r"(?P<base>[^\W\d_]+)\s+и\s+(?P<other>[^\W\d_]+)[?.]?"
    r"(?:\s+покажи\s+исходные\s+записи\s+с\s+обеих\s+сторон[.!]?)?",
    re.IGNORECASE,
)


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


@dataclass
class RetryContext:
    question: str
    history_length: int
    paths: set
    evidence: dict
    resources: dict


def failure(code, message, hint):
    return {"ok": False, "data": None, "error": {"code": code, "message": message, "hint": hint}}


def matches_folder_request(question, tool, arguments):
    """Only the current user message may authorize a mutation and its target."""
    def literal(value):
        value = re.escape(value)
        return rf'(?:«{value}»|"{value}"|`{value}`|{value})'

    prefix = r"(?i:(?:пожалуйста[\s,]+)?)"
    suffix = r"(?i:(?:\s+(?:в\s+)?IACPaaS)?)[.!]?"
    folder = r"(?i:(?:(?:тестовую|пустую|учебную)\s+)?(?:папку|каталог)\s+)"
    if tool == "delete_folder":
        path = literal(arguments["path"])
        command = rf"(?:(?i:удали(?:те)?\s+){folder}{path}|(?i:перемести(?:те)?\s+)(?:{folder})?{path}(?i:\s+в\s+корзину))"
    else:
        parent, name = arguments["parent"], arguments["name"]
        if parent:
            target = rf"(?:{literal(parent + '/' + name)}|{literal(name)}(?i:\s+(?:внутри|в\s+папке|в)\s+){literal(parent)})"
        else:
            target = literal(name) + r"(?i:(?:\s+в\s+корне(?:\s+фонда)?)?)"
        command = rf"(?i:создай(?:те)?\s+){folder}{target}"
        if description := arguments.get("description"):
            command += rf"(?i:\s+с\s+описанием\s+){literal(description)}"
    return re.fullmatch(prefix + command + suffix, question.strip()) is not None


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
    def __init__(self, session, model, trace, max_rounds=8, max_calls=16, *, replay=None):
        self.session, self.model, self.trace = session, model, trace
        self.max_rounds, self.max_calls = max_rounds, max_calls
        self.replay = replay
        self.retry_context = None
        self.write_in_turn = False
        self.user_request = ""
        self.tools = {}
        self.known_paths = set()
        self.known_resources = {}
        self.attempted_writes = set()
        self.evidence = {}
        self.last_grounding = audit_answer('', {})

    async def initialize(self):
        await self.session.initialize()
        for tool in sorted((await self.session.list_tools()).tools, key=lambda t: t.name):
            if tool.name not in ALLOWED_TOOLS:
                continue
            expected_read = tool.name in READ_ONLY_TOOLS
            if not tool.annotations or tool.annotations.readOnlyHint is not expected_read:
                raise AgentError("Аннотации MCP не соответствуют политике инструмента.")
            item = tool.model_dump(mode="json")
            item["inputSchema"]["additionalProperties"] = False
            self.tools[tool.name] = item
        if not READ_TOOLS.issubset(self.tools):
            raise AgentError("MCP-сервер не предоставил четыре ожидаемых инструмента чтения.")
        self.trace.add("tools_discovered", names=sorted(self.tools))

    async def execute(self, call):
        if call.name not in self.tools:
            return failure("POLICY_DENIED", "Инструмент не разрешён.", "Используйте только опубликованные инструменты чтения и каталогов.")
        try:
            arguments = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
            jsonschema.validate(arguments, self.tools[call.name]["inputSchema"])
        except (ValueError, jsonschema.ValidationError, TypeError):
            return failure("INVALID_ARGUMENT", "Аргументы не соответствуют JSON Schema.", "Исправьте аргументы по схеме; глубина 0..3, max_nodes 1..200.")
        if call.name in READ_ONLY_TOOLS - DISCOVERY_TOOLS and arguments.get("path") not in self.known_paths:
            supplied = arguments.get("path", "").strip().casefold()
            candidates = sorted(path for path in self.known_paths if supplied and supplied in {
                path.casefold(), path.rsplit("/", 1)[-1].casefold(),
                self.known_resources.get(path, {}).get("title", "").strip().casefold(),
            })
            if len(candidates) == 1:
                canonical = candidates[0]
                self.trace.add("resource_path_resolved", tool=call.name,
                               supplied=arguments["path"], path=canonical)
                arguments = {**arguments, "path": canonical}
                call.arguments = (json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                                  if isinstance(call.arguments, str) else arguments)
            elif candidates:
                return failure("PATH_AMBIGUOUS", "Название соответствует нескольким подтверждённым ресурсам.",
                               "Выберите точный path: " + "; ".join(candidates[:10]))
            else:
                known = "; ".join(sorted(self.known_paths)[:10])
                return failure("PATH_NOT_DISCOVERED", "Ресурс ещё не найден в каталоге или фонде.",
                               "Сначала вызовите list_inforesources или list_fund_resources и используйте полный path. "
                               + ("Уже подтверждены: " + known if known else "Не подменяйте путь названием базы."))
        if call.name in {"get_subgraph", "count_graph"}:
            try:
                validate_concept_start(arguments.get("start", ""))
            except Fault as exc:
                return failure(exc.code, exc.message, exc.hint)
            if arguments.get("start"):
                return failure("UNCONFIRMED_START", "Агент не передаёт придуманные пути вершин в IACPaaS.",
                               "Оставьте start пустым для чтения корня. Найденную запись читайте через "
                               "read_export_node с pointer и export_sha256, если инструмент доступен; "
                               "для составов газов также доступен analyze_gas_composition.")
        if call.name in WRITE_TOOLS:
            if not matches_folder_request(self.user_request, call.name, arguments):
                return failure("CONFIRMATION_REQUIRED", "Для изменения нужна прямая команда пользователя с точным путём папки.",
                               "Например: «Создай папку Загрузки/Демо» или «Перемести папку Загрузки/Демо в корзину».")
            signature = (call.name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            if signature in self.attempted_writes:
                return failure("WRITE_ALREADY_ATTEMPTED", "Этот запрос изменения уже отправлялся в текущем диалоге.",
                               "Проверьте папку в IACPaaS. Для намеренного повторения начните новый диалог.")
            self.attempted_writes.add(signature)
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
        if call.name in DISCOVERY_TOOLS:
            for row in result["data"]["resources"]:
                self.known_paths.add(row["path"])
                self.known_resources[row["path"]] = row
        return result

    def finish(self, text, evidence=None):
        self.last_grounding = audit_answer(text, self.evidence if evidence is None else evidence, self.evidence)
        self.trace.add("answer_grounding", **self.last_grounding)
        self.trace.add("assistant", text=text)
        return text

    def request_completion(self, instruction):
        # Recovery must retain the actual task, rather than replace it with a generic search.
        self.model.add_user(instruction + "\n\nИсходный вопрос пользователя:\n" + self.user_request)

    def citation_feedback(self, text, evidence):
        ledger = self.evidence if evidence is None else evidence
        audit = audit_answer(text, ledger, self.evidence)
        if not audit["invalid_citations"]:
            return None
        self.trace.add("answer_repair", invalid_citations=audit["invalid_citations"],
                       historical_citations=audit["historical_citations"])
        invalid = ", ".join(audit["invalid_citations"][:20])
        available = ", ".join(sorted(ledger)[:80]) or "нет"
        return (f"Исправь ответ: ссылки {invalid} не подтверждены для текущего вопроса. "
                "Ссылки из истории требуют повторного чтения соответствующего источника. "
                "При изменении данных используй новые значения и новые ссылки. "
                "Не удаляй ссылки, сохраняя неподтверждённые утверждения. "
                "Если составы ещё не прочитаны, получи их через analyze_gas_composition "
                "или подходящий поиск по конкретным компонентам вопроса. "
                f"Подтверждённые ссылки текущего вопроса: {available}.")

    async def model_step(self, tools, **position):
        definitions = []
        for tool in tools:
            if tool.get("name") in {"get_subgraph", "count_graph"}:
                schema = tool["inputSchema"]
                properties = {**schema["properties"], "start": {"type": "string", "enum": [""], "default": ""}}
                tool = {**tool, "inputSchema": {**schema, "properties": properties},
                        "description": "Read/count from the resource root with start=''. "
                                       "For a found record use read_export_node if available; "
                                       "for gas compositions use analyze_gas_composition. " + tool.get("description", "")}
            if tool.get("name") in READ_ONLY_TOOLS - DISCOVERY_TOOLS and self.known_paths:
                schema = tool["inputSchema"]
                properties = {**schema["properties"]}
                properties["path"] = {**properties["path"], "enum": sorted(self.known_paths)}
                tool = {**tool, "inputSchema": {**schema, "properties": properties}}
            definitions.append(tool)
        if self.replay is not None and not self.write_in_turn:
            turn, replayed = await self.replay.step(self.model, definitions)
            position["replayed"] = replayed
        else:
            turn = await self.model.step(definitions)
        self.write_in_turn |= any(call.name in WRITE_TOOLS for call in turn.calls)
        self.trace.add("model_turn", tool_calls=len(turn.calls), **position)
        return turn

    async def list_current_fund(self):
        async def read_fund(parent):
            name = "list_fund_resources"
            arguments = {"parent": parent, "limit": 50}
            result = await self.execute(Call(name, name, arguments))
            self.trace.add("tool_call", name=name, arguments=arguments, result=result)
            return result

        root = await read_fund("")
        result = root
        if not root["ok"] and root["error"]["code"] == "UPSTREAM_ERROR":
            result = await read_fund("Загрузки")
        if not result["ok"]:
            if result is not root:
                return ("Не удалось прочитать корень «Моего Фонда» и папку «Загрузки»."
                        "\n\nПроверьте, открываются ли они под вашей учётной записью на сайте IACPaaS. "
                        "Подробности по обоим запросам есть в журнале вызовов.")
            error = result["error"]
            details = " ".join(filter(None, (error["message"], error.get("hint"))))
            return f"Не удалось получить список «Моего Фонда» ({error['code']}). {details}"

        data = result["data"]
        resources = data["resources"]
        lines = ["**Мой Фонд IACPaaS**"]
        if data["parent"]:
            lines.append(f"Прочитана папка «{data['parent']}». Корень фонда сейчас недоступен для чтения.")
        if resources:
            lines += ["", f"Ресурсов в ответе MCP — **{len(resources)}**.", ""]
            lines.extend(f"- {row['title']} (путь `{row['path']}`)" for row in resources)
        else:
            lines.extend(("", "В прочитанных папках ресурсов не найдено."))
        if data["truncated"]:
            lines.extend(("", "Список ограничен лимитом чтения MCP."))
        if data["skipped_folders"]:
            skipped = ", ".join(f"«{path}»" for path in data["skipped_folders"])
            lines.extend(("", f"Не удалось прочитать папки {skipped}."))
        lines.extend(("", "Источник — текущий вызов `list_fund_resources`."))
        return "\n".join(lines)

    async def compare_gas_classes(self, base, other):
        async def run(name, arguments):
            result = await self.execute(Call(name, name, arguments))
            self.trace.add("tool_call", name=name, arguments=arguments, result=result)
            return result

        listing = await run("list_inforesources", {"query": GAS_DATABASE})
        if not listing["ok"]:
            return listing["error"]["message"], {}
        resource = next((row for row in listing["data"]["resources"] if row["title"] == GAS_DATABASE), None)
        if resource is None:
            return f"В каталоге MCP не найдена «{GAS_DATABASE}».", {}
        result = await run("analyze_gas_composition", {"path": resource["path"], "base": base, "compare_with": other})
        if not result["ok"]:
            error = result["error"]
            return f"Сравнение по IACPaaS не удалось. {error['message']} {error['hint']}", {}

        data = result["data"]
        evidence = {item["evidence_id"]: item for item in collect_evidence(data)}
        groups = [row["group"] for row in data["groups"]]
        common = data["common_impurities"]
        labels = ", ".join(row["component"] for row in common)
        summary = f"**Общие примеси ({len(common)})** — {labels}." if common else "Общих примесей в прочитанных записях не найдено."
        lines = [summary, "", "Сравнены классы " + " и ".join(
            f"«{group['trail'][-1]}» [{group['evidence_id']}]" for group in groups) + "."]
        if common:
            lines += ["", "Каждый компонент указан с верхним пределом хотя бы в одной марке каждого класса.", ""]
            for row in common:
                sources = [f"«{ref['trail'][len(group['trail'])]}» [{ref['evidence_id']}]"
                           for group, ref in zip(groups, (row["left"], row["right"]))]
                lines.append(f"- **{row['component']}** — " + " · ".join(sources))
        lines += ["", f"Источник — «{resource['title']}», живой API IACPaaS, глубина {data['requested_depth']}."]
        return "\n".join(lines), evidence

    async def finish_reads(self, searches, evidence=None):
        self.request_completion(
            "Заверши ответ на исходный вопрос по уже полученным данным IACPaaS. "
            "Используй ссылки [E...] на записи или результаты поиска. "
            "Ограничь вывод проверенными фрагментами. Если искомые данные не получены, "
            "укажи, что найдено и чего не хватает для ответа."
        )
        try:
            turn = await self.model_step([], final=True)
        except AgentError:
            turn = None
        if turn is not None:
            if not turn.calls and turn.text.strip():
                feedback = self.citation_feedback(turn.text, evidence)
                if feedback:
                    self.request_completion(feedback + " Новые инструменты недоступны; "
                                            "оставь только подтверждённые выводы и укажи недостающие данные.")
                    try:
                        corrected = await self.model_step([], final=True, citation_repair=True)
                    except AgentError:
                        corrected = None
                    if corrected is not None and not corrected.calls and corrected.text.strip():
                        return self.finish(corrected.text, evidence)
                    if corrected is not None:
                        for call in corrected.calls:
                            self.model.add_result(call, failure("READ_FINISHED", "Поиск завершён.",
                                                                "Инструменты больше недоступны."))
                return self.finish(turn.text, evidence)
            for call in turn.calls:
                self.model.add_result(call, failure("READ_FINISHED", "Поиск завершён.", "Подведи итог по результатам."))
        lines = ["Поиск в IACPaaS завершён. Подтвердить запрошенный вывод по полученным данным не удалось."]
        if searches:
            lines.append("")
            for item in list(searches.values())[:8]:
                data = item["fields"]
                lines.append(f"- {item['resource']['title']}, запрос «{data['query']}», "
                             f"глубина {item['requested_depth']}. Совпадений: {data['total_matches']} "
                             f"[{item['evidence_id']}].")
        return self.finish("\n".join(lines), evidence)

    async def answer(self, question):
        if len(question) > 8000:
            raise AgentError("Вопрос длиннее 8000 символов. Сократите запрос.")
        self.write_in_turn = False
        if self.replay is None:
            return await self.answer_turn(question)

        previous = self.retry_context
        self.retry_context = None
        if previous is not None and previous.question == question:
            # Treat an immediate repeat as a retry of the same turn, including tool discovery.
            del self.model.history[previous.history_length:]
            self.known_paths = previous.paths.copy()
            self.known_resources = previous.resources.copy()
            self.evidence = previous.evidence.copy()
        context = RetryContext(question, len(self.model.history), self.known_paths.copy(),
                               self.evidence.copy(), self.known_resources.copy())
        answer = await self.answer_turn(question)
        if not self.write_in_turn:
            self.retry_context = context
        return answer

    async def answer_turn(self, question):
        self.user_request = question
        self.trace.add("user", text=question)
        self.model.add_user(question)
        if "list_fund_resources" in self.tools and FUND_LIST_QUERY.fullmatch(question.strip()):
            answer = await self.list_current_fund()
            self.model.add_assistant(answer)
            self.last_grounding = None
            self.trace.add("assistant", text=answer)
            return answer
        comparison = GAS_COMPARISON_QUERY.fullmatch(question.strip())
        if comparison and {"list_inforesources", "analyze_gas_composition"} <= self.tools.keys():
            answer, evidence = await self.compare_gas_classes(**comparison.groupdict())
            self.evidence.update(evidence)
            self.model.add_assistant(answer)
            return self.finish(answer, evidence)
        calls = 0
        successful_read = False
        searches = {}
        needs_live_data = bool(COMPOSITION_QUERY.search(question) and GAS_QUERY.search(question))
        turn_evidence = {} if needs_live_data else None
        no_live_data = ("Для этого вопроса не получены записи составов из IACPaaS. "
                        "Проверьте журнал вызовов и доступ к ресурсу с составами.")
        retried_without_data = False
        retried_citations = False
        for round_index in range(self.max_rounds):
            # The model must discover exact paths before being offered graph reads.
            visible = [self.tools[name] for name in sorted(self.tools)
                       if self.known_paths or name in DISCOVERY_TOOLS | WRITE_TOOLS]
            turn = await self.model_step(visible, round=round_index + 1)
            if not turn.calls:
                if not turn.text.strip():
                    raise AgentError("Модель не вернула ни ответ, ни вызов инструмента.")
                if needs_live_data and not successful_read:
                    if not retried_without_data and round_index + 1 < self.max_rounds:
                        retried_without_data = True
                        self.request_completion(
                            "Для ответа нужны свежие данные IACPaaS по исходному вопросу. "
                            "Для составов моногазов используй analyze_gas_composition после нахождения базы. "
                            "В остальных случаях ищи конкретные компоненты и ограничения из вопроса. "
                            "Не заменяй задачу общим поиском слова «примесь» или отчётом по всем ресурсам.")
                        continue
                    if not successful_read:
                        return self.finish(no_live_data, turn_evidence)
                if not retried_citations and round_index + 1 < self.max_rounds:
                    feedback = self.citation_feedback(turn.text, turn_evidence)
                    if feedback:
                        retried_citations = True
                        self.request_completion(feedback)
                        continue
                return self.finish(turn.text, turn_evidence)
            if calls + len(turn.calls) > self.max_calls:
                if not successful_read:
                    if needs_live_data:
                        for call in turn.calls:
                            self.model.add_result(call, failure("READ_FINISHED", "Лимит вызовов достигнут.", "Заверши поиск."))
                        return self.finish(no_live_data, turn_evidence)
                    raise AgentError("Достигнут лимит вызовов инструментов. Уточните или сузьте запрос.")
                for call in turn.calls:
                    self.model.add_result(call, failure("READ_FINISHED", "Лимит вызовов достигнут.", "Подведи итог по результатам."))
                break
            for call in turn.calls:
                result = await self.execute(call)
                calls += 1
                self.trace.add("tool_call", name=call.name, arguments=call.arguments, result=result)
                refs = collect_evidence(result.get("data")) if result.get("ok") else []
                self.evidence.update({r["evidence_id"]: r for r in refs})
                if turn_evidence is not None:
                    fresh = {r["evidence_id"]: r for r in refs if r["source"] == "live_api"}
                    turn_evidence.update(revalidate_evidence(self.evidence, fresh))
                if refs and call.name in READ_TOOLS | SEARCH_TOOLS and (
                    not needs_live_data or any(ref["source"] == "live_api" for ref in refs)
                ):
                    successful_read = True
                if call.name in {"search_inforesource", "search_fund_content"}:
                    searches.update({r["evidence_id"]: r for r in refs if r["pointer"] == "/matches"})
                # Metadata added to model context is separate from the validated MCP result.
                context_result = dict(result)
                if refs:
                    context_result["evidence_refs"] = [{"id": r["evidence_id"], "pointer": r["pointer"],
                                                      "trail": r["trail"]} for r in refs]
                self.model.add_result(call, context_result)
        if successful_read:
            return await self.finish_reads(searches, turn_evidence)
        if needs_live_data:
            return self.finish(no_live_data, turn_evidence)
        raise AgentError("Достигнут лимит шагов модели; итоговый ответ не получен. Сузьте запрос.")
