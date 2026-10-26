import json
from pathlib import Path
import tempfile
import unittest

import httpx

from agent_core import Agent, AgentError, Call, Trace, Turn
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

    def add_user(self, question):
        self.questions.append(question)

    def add_result(self, call, result):
        self.results.append(result)

    async def step(self, tools):
        value = next(self.turns)
        return value(self.results) if callable(value) else value


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
        self.assertEqual(offered[0], {"list_inforesources"})
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

    async def test_call_budget_stops_before_executing_batch(self):
        model = ScriptedModel([Turn([Call(str(i), "unknown", {}) for i in range(3)])])
        agent = Agent(None, model, self.trace, max_calls=2)
        with self.assertRaisesRegex(AgentError, "лимит вызовов"):
            await agent.answer("Запрос")
        self.assertEqual(model.results, [])

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
