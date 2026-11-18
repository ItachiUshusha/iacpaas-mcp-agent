import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from agent_core import Agent, AgentError, Call, Trace, Turn
from answer_evidence import collect_evidence
from domain import BASE
from providers import OpenAIModel, OllamaModel
from smoke import connect
from agent import child_environment


class ScriptedModel:
    """Explicit test double; never presented as actual model intelligence."""
    def __init__(self, turns):
        self.turns = iter(turns)
        self.results = []
        self.questions = []
        self.answers = []

    def add_user(self, question):
        self.questions.append(question)

    def add_assistant(self, text):
        self.answers.append(text)

    def add_result(self, call, result):
        self.results.append(result)

    async def step(self, tools):
        value = next(self.turns)
        return value(self.results) if callable(value) else value


def live_search(reference="Enew", digest="same-export"):
    return {"ok": True, "data": {
        "resource": {"title": "База газов", "path": "Загрузки/База газов", "resource_id": "test"},
        "source": "live_api", "requested_depth": 10, "export_sha256": digest,
        "query": "N", "scanned_nodes": 12, "total_matches": 1, "matches_truncated": False,
        "matches": [{"evidence_id": reference, "pointer": "/successors/0", "trail": ["Марка А", "N"],
                     "fields": {"name": "N"}, "children": [], "children_truncated": False}]
    }}


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=BASE)
        self.trace = Trace(Path(self.temp.name), ["secret-test-token"])

    async def asyncTearDown(self):
        self.trace.close()
        self.temp.cleanup()

    async def test_multistep_discovery_read_recovery_both_real_transports(self):
        for transport in ("stdio", "http"):
            with self.subTest(transport=transport):
                def read_known(results):
                    path = results[-1]["data"]["resources"][0]["path"]
                    return Turn([Call("2", "get_inforesource", {"path": path})])

                def invalid_depth(results):
                    path = results[-1]["data"]["resource"]["path"]
                    return Turn([Call("3", "get_subgraph", {"path": path, "depth": 99})])

                def correct_depth(results):
                    self.assertEqual(results[-1]["error"]["code"], "INVALID_ARGUMENT")
                    path = results[0]["data"]["resources"][0]["path"]
                    return Turn([Call("4", "count_graph", {"path": path, "depth": 0})])

                model = ScriptedModel([Turn([Call("1", "list_inforesources", {"query": "Система"})]),
                                       read_known, invalid_depth, correct_depth, Turn(text="Снимок: 1 вершина, 0 связей.")])
                async with connect(transport, "snapshot", child_environment()) as session:
                    agent = Agent(session, model, self.trace)
                    await agent.initialize()
                    answer = await agent.answer("Найди ресурс и посчитай корень")
                self.assertIn("1 вершина", answer)
                self.assertEqual(model.results[-1]["data"]["counts"]["node_occurrences"], 1)
                self.assertEqual(model.results[1]["data"]["source"], "saved_snapshot")

    async def test_policy_unknown_path_and_bad_json_no_mcp_execution(self):
        async with connect("stdio", "snapshot", child_environment()) as session:
            agent = Agent(session, None, self.trace)
            await agent.initialize()
            for call, expected in (
                (Call("1", "delete_resource", {"path": "x"}), "POLICY_DENIED"),
                (Call("2", "get_inforesource", {"path": "guessed"}), "PATH_NOT_DISCOVERED"),
                (Call("3", "list_inforesources", "not-json"), "INVALID_ARGUMENT"),
                (Call("4", "list_inforesources", {"extra": "hidden"}), "INVALID_ARGUMENT"),
            ):
                result = await agent.execute(call)
                self.assertEqual(result["error"]["code"], expected)

    async def test_graph_tools_are_offered_only_after_path_discovery(self):
        offered = []
        class DiscoveryModel:
            def add_user(self, text):
                pass
            def add_result(self, call, result):
                self.path = result["data"]["resources"][0]["path"]
            async def step(self, tools):
                offered.append({t["name"] for t in tools})
                if len(offered) == 1:
                    return Turn([Call("1", "list_inforesources", {"query": "Система единиц"})])
                return Turn(text="Ресурс найден.")
        async with connect("stdio", "snapshot", child_environment()) as session:
            agent = Agent(session, DiscoveryModel(), self.trace)
            await agent.initialize()
            await agent.answer("Найди ресурс")
        self.assertEqual(offered[0], {"list_inforesources", "list_fund_resources", "search_fund_content",
                                      "create_folder", "delete_folder"})
        self.assertIn("count_graph", offered[1])

    async def test_clarification_preserves_conversation(self):
        model = ScriptedModel([Turn(text="Какой ресурс нужно прочитать?"), Turn(text="Название принято.")])
        agent = Agent(None, model, self.trace)
        self.assertIn("ресурс", await agent.answer("Прочитай граф"))
        await agent.answer("Система единиц")
        self.assertEqual(model.questions, ["Прочитай граф", "Система единиц"])

    async def test_bounded_loop(self):
        model = ScriptedModel([Turn([Call("1", "unknown", {})])] * 5)
        agent = Agent(None, model, self.trace, max_rounds=2)
        with self.assertRaisesRegex(AgentError, "лимит шагов"):
            await agent.answer("Повторяй")
        self.assertEqual(len(model.results), 2)

    async def test_read_budget_returns_citable_scope_when_model_keeps_searching(self):
        path = "Загрузки/База газов"
        calls = []
        search = {"resource": {"title": "База газов", "path": path, "resource_id": "test"},
                  "source": "live_api", "requested_depth": 10, "export_sha256": "abc",
                  "query": "примес", "scanned_nodes": 12, "total_matches": 0,
                  "matches_truncated": False, "matches": []}

        class SearchingModel(ScriptedModel):
            async def step(self, tools):
                calls.append(tools)
                return await super().step(tools)

        model = SearchingModel([
            Turn([Call("1", "search_inforesource", {"path": path, "query": "примес"})]),
            Turn([Call("2", "search_inforesource", {"path": path, "query": "гелий"})]),
        ])
        agent = Agent(None, model, self.trace, max_rounds=1)
        with patch.object(agent, "execute", return_value={"ok": True, "data": search}) as execute:
            answer = await agent.answer("Какие общие примеси у водорода и гелия?")
        execute.assert_awaited_once()
        self.assertEqual(calls[-1], [])
        self.assertIn("Совпадений: 0", answer)
        self.assertEqual(agent.last_grounding["status"], "cited")
        source, = agent.last_grounding["sources"]
        self.assertEqual(source["resource"]["path"], path)
        self.assertEqual(source["fields"]["scanned_nodes"], 12)

    async def test_call_budget_stops_before_executing_batch(self):
        model = ScriptedModel([Turn([Call(str(i), "unknown", {}) for i in range(3)])])
        agent = Agent(None, model, self.trace, max_calls=2)
        with self.assertRaisesRegex(AgentError, "лимит вызовов"):
            await agent.answer("Запрос")
        self.assertEqual(model.results, [])

    async def test_gas_question_cannot_claim_search_without_fresh_reads(self):
        model = ScriptedModel([Turn(text="В базе газов примесей нет.")] * 2)
        agent = Agent(None, model, self.trace)
        answer = await agent.answer("Какие примеси у аргона?")
        self.assertIn("не получены записи", answer)
        self.assertNotIn("примесей нет", answer)

    async def test_json_pointer_is_not_sent_to_mcp_as_start(self):
        session = Mock()
        session.call_tool = AsyncMock()
        agent = Agent(session, None, self.trace)
        agent.tools = {name: {"inputSchema": {"type": "object"}} for name in ("get_subgraph", "count_graph")}
        agent.known_paths.add("Загрузки/База газов")
        for name in agent.tools:
            result = await agent.execute(Call("bad", name, {
                "path": "Загрузки/База газов", "start": "/successors/0/successors/1", "depth": 3}))
            self.assertEqual(result["error"]["code"], "INVALID_START_PATH")
            self.assertIn("analyze_gas_composition", result["error"]["hint"])
        session.call_tool.assert_not_awaited()

    async def test_specific_read_does_not_force_unrelated_fund_search(self):
        question = "Сравни ограничения содержания азота в двух марках аргона"
        model = ScriptedModel([
            Turn([Call("read", "search_inforesource", {"path": "Загрузки/База газов", "query": "N"})]),
            Turn(text="Запись азота [Enew].")])
        agent = Agent(None, model, self.trace)
        agent.tools = {"search_fund_content": {}}
        with patch.object(agent, "execute", return_value=live_search()) as execute:
            answer = await agent.answer(question)
        self.assertEqual(answer, "Запись азота [Enew].")
        self.assertEqual(model.questions, [question])
        execute.assert_awaited_once()
        self.assertEqual(agent.last_grounding["status"], "cited")

    async def test_label_trail_and_unconfirmed_platform_path_never_reach_mcp(self):
        session = Mock()
        session.call_tool = AsyncMock()
        agent = Agent(session, None, self.trace)
        agent.tools = {"get_subgraph": {"inputSchema": {"type": "object"}}}
        agent.known_paths.add("Загрузки/База технологических газов")
        trail = "База технологических газов/Моногазы/Газы на основе Аргона/Аргон газообразный высокой чистоты марка 6.0"
        for start, expected in ((trail, "INVALID_START_PATH"), (trail + ";", "UNCONFIRMED_START")):
            result = await agent.execute(Call("bad", "get_subgraph", {
                "path": "Загрузки/База технологических газов", "start": start, "depth": 3}))
            self.assertEqual(result["error"]["code"], expected)
        session.call_tool.assert_not_awaited()

    async def test_model_graph_schema_allows_only_resource_root(self):
        offered = []
        class CapturingModel(ScriptedModel):
            async def step(self, tools):
                offered.extend(tools)
                return Turn(text="Корень ресурса доступен.")
        async with connect("stdio", "snapshot", child_environment()) as session:
            agent = Agent(session, CapturingModel([]), self.trace)
            await agent.initialize()
            agent.known_paths.add("Загрузки/Система единиц физических величин")
            await agent.answer("Прочитай корень")
        for name in ("get_subgraph", "count_graph"):
            definition = next(t for t in offered if t["name"] == name)
            self.assertEqual(definition["inputSchema"]["properties"]["start"]["enum"], [""])
            self.assertNotIn("enum", agent.tools[name]["inputSchema"]["properties"]["start"])
        self.assertIn("read_export_node", {t["name"] for t in offered})

    async def test_identical_fresh_export_revalidates_previous_answer_sources(self):
        model = ScriptedModel([
            Turn([Call("read", "search_inforesource", {"path": "Загрузки/База газов", "query": "N"})]),
            Turn(text="Примесь из предыдущей записи [Eold].")])
        agent = Agent(None, model, self.trace)
        old = live_search("Eold")["data"]
        agent.evidence = {r["evidence_id"]: r for r in collect_evidence(old)}
        with patch.object(agent, "execute", return_value=live_search()):
            await agent.answer("Разбери состав ранее найденного аргона")
        self.assertEqual(agent.last_grounding["status"], "cited")
        source, = agent.last_grounding["sources"]
        self.assertEqual(source["evidence_id"], "Eold")
        self.assertTrue(source["revalidated"])
        self.assertEqual(len(model.questions), 1)

    async def test_changed_export_requires_corrected_answer_and_preserves_question(self):
        question = "Сравни содержание азота в двух марках аргона"
        model = ScriptedModel([
            Turn([Call("read", "search_inforesource", {"path": "Загрузки/База газов", "query": "N"})]),
            Turn(text="Устаревший предел [Eold]."),
            Turn(text="Обновлённый предел [Enew].")])
        agent = Agent(None, model, self.trace)
        agent.evidence = {r["evidence_id"]: r for r in collect_evidence(live_search("Eold")["data"])}
        with patch.object(agent, "execute", return_value=live_search(digest="changed-export")):
            answer = await agent.answer(question)
        self.assertEqual(answer, "Обновлённый предел [Enew].")
        self.assertEqual(agent.last_grounding["status"], "cited")
        self.assertIn(question, model.questions[-1])
        self.assertIn("Eold", model.questions[-1])
        self.assertIn("Enew", model.questions[-1])

    async def test_unknown_citations_are_repaired_once_and_never_silently_accepted(self):
        model = ScriptedModel([
            Turn([Call("read", "search_inforesource", {})]),
            Turn(text="Неподтверждённое [Efake]."),
            Turn(text="Всё ещё неподтверждённое [Efake].")])
        agent = Agent(None, model, self.trace)
        with patch.object(agent, "execute", return_value=live_search()):
            await agent.answer("Проверь примеси аргона")
        self.assertEqual(len(model.questions), 2)
        self.assertEqual(agent.last_grounding["status"], "invalid_citations")
        self.assertEqual(agent.last_grounding["unknown_citations"], ["Efake"])

    async def test_missing_data_recovery_keeps_original_component(self):
        question = "Сравни верхние пределы содержания азота в аргоновых газах"
        model = ScriptedModel([Turn(text="Ответ без чтения"),
                               Turn([Call("read", "search_inforesource", {"query": "N"})]),
                               Turn(text="Азот [Enew].")])
        agent = Agent(None, model, self.trace)
        with patch.object(agent, "execute", return_value=live_search()):
            await agent.answer(question)
        self.assertIn(question, model.questions[1])
        self.assertNotIn("Передай названия газов", model.questions[1])
        self.assertEqual(agent.last_grounding["status"], "cited")

    async def test_budget_finalizer_repairs_citations_without_losing_original_question(self):
        question = "Сравни содержание азота в аргоновых газах"
        model = ScriptedModel([Turn([Call("read", "search_inforesource", {})]),
                               Turn(text="Старый вывод [Efake]."),
                               Turn(text="Подтверждённая запись [Enew].")])
        agent = Agent(None, model, self.trace, max_rounds=1)
        with patch.object(agent, "execute", return_value=live_search()):
            answer = await agent.answer(question)
        self.assertEqual(answer, "Подтверждённая запись [Enew].")
        self.assertTrue(all(question in message for message in model.questions))
        self.assertEqual(agent.last_grounding["status"], "cited")

    async def test_repeated_gas_class_comparison_uses_live_analysis_without_model(self):
        title = "База технологических газов"
        resource = {"title": title, "path": f"Загрузки/{title}", "resource_id": "test"}

        def ref(identifier, *trail):
            return {"evidence_id": identifier, "trail": [title, "Моногазы", *trail]}

        left_group = "Газы на основе Водорода"
        right_group = "Газы на основе Аргона"
        comparison = {"resource": resource, "source": "live_api", "requested_depth": 10,
                      "groups": [{"group": ref("Ea1", left_group), "records": []},
                                 {"group": ref("Eb2", right_group), "records": []}],
                      "common_impurities": [{"component": "X",
                                             "left": ref("Ec3", left_group, "Марка А", "X"),
                                             "right": ref("Ed4", right_group, "Марка Б", "X")}]}
        model = ScriptedModel([])
        agent = Agent(None, model, self.trace)
        agent.tools = {"list_inforesources": {}, "analyze_gas_composition": {}, "search_fund_content": {}}
        question = "Какие общие примеси указаны у газов на основе водорода и аргона? Покажи исходные записи с обеих сторон"
        results = [{"ok": True, "data": {"resources": [resource]}}, {"ok": True, "data": comparison}]
        with patch.object(agent, "execute", side_effect=results * 2) as execute:
            first = await agent.answer(question)
            second = await agent.answer(question)
        self.assertEqual(first, second)
        self.assertEqual(model.answers, [first, second])
        calls = [args.args[0] for args in execute.await_args_list]
        self.assertEqual([call.name for call in calls], ["list_inforesources", "analyze_gas_composition"] * 2)
        self.assertEqual(calls[1].arguments, {"path": resource["path"], "base": "водорода", "compare_with": "аргона"})
        self.assertIn("«Марка А» [Ec3] · «Марка Б» [Ed4]", second)
        self.assertEqual(agent.last_grounding["status"], "cited")
        self.assertEqual(len(agent.last_grounding["sources"]), 4)

    async def test_trace_redacts_known_secrets_and_credentials(self):
        self.trace.add("test", text="value secret-test-token", data={"Authorization": "Bearer hidden", "password": "hidden"})
        text = self.trace.path.read_text(encoding="utf-8")
        self.assertNotIn("secret-test-token", text)
        self.assertNotIn("hidden", text)
        self.assertIn("[REDACTED]", text)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_roundtrip_retains_reasoning_and_call_ids(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertFalse(body["store"])
            self.assertFalse(body["parallel_tool_calls"])
            if len(requests) == 1:
                return httpx.Response(200, json={"status": "completed", "output": [
                    {"type": "reasoning", "id": "r1", "summary": [], "encrypted_content": "test-encrypted"},
                    {"type": "function_call", "call_id": "call1", "name": "list_inforesources", "arguments": "{}"}]})
            self.assertEqual(body["input"][-1]["call_id"], "call1")
            self.assertEqual(body["input"][1]["encrypted_content"], "test-encrypted")
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Готово"}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = OpenAIModel(client, "test-model", "test-secret", "instructions")
            model.add_user("Вопрос")
            turn = await model.step([])
            model.add_result(turn.calls[0], {"ok": True})
            self.assertEqual((await model.step([])).text, "Готово")

    async def test_ollama_roundtrip(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertNotIn("authorization", request.headers)
            if len(requests) == 1:
                return httpx.Response(200, json={"done": True, "message": {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "list_inforesources", "arguments": {"query": "единицы"}}}]}})
            self.assertEqual(body["messages"][-1]["tool_name"], "list_inforesources")
            return httpx.Response(200, json={"done": True, "message": {"role": "assistant", "content": "Ответ"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = OllamaModel(client, "test-model", "instructions")
            model.add_user("Вопрос")
            turn = await model.step([])
            model.add_result(turn.calls[0], {"ok": True})
            self.assertEqual((await model.step([])).text, "Ответ")

    async def test_provider_http_error_body_not_exposed(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="secret-body"))) as client:
            model = OpenAIModel(client, "test-model", "test-secret", "instructions")
            with self.assertRaises(AgentError) as error:
                await model.step([])
            self.assertIn("401", str(error.exception))
            self.assertNotIn("secret", str(error.exception))

    async def test_incomplete_response_not_presented_as_success(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "incomplete", "output": []}))) as client:
            with self.assertRaises(AgentError):
                await OpenAIModel(client, "test", "test", "instructions").step([])


if __name__ == "__main__":
    unittest.main()
