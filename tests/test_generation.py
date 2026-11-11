import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from agent_core import AgentError
from generation import Generation
from providers import CompatibleModel, OllamaModel
from domain import BASE, Service, Selection
from server import respond


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_cap_and_local_http_failure_diagnostic(self):
        def reject(request):
            body = json.loads(request.content)
            self.assertEqual(body["max_tokens"], 2048)
            return httpx.Response(400, json={"error": {"code": 400, "message": "Provider detail: Bearer demo-private-key", "metadata": {"provider_name": "Example", "raw": "Unsupported request parameter"}}})
        with tempfile.TemporaryDirectory(dir=BASE) as tmp, patch("providers.MODEL_DIAGNOSTIC_DIR", Path(tmp)):
            async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
                model = CompatibleModel(client, "openrouter/free", "demo-private-key", "rules", "https://openrouter.ai/api/v1", Generation(max_output_tokens=2048))
                model.add_user("Private user question")
                with self.assertRaisesRegex(AgentError, "HTTP 400") as error:
                    await model.step([])
            self.assertNotIn("Provider detail", str(error.exception))
            file = next(Path(tmp).glob("model-error-*.json"))
            contents = file.read_text(encoding="utf-8")
            self.assertNotIn("demo-private-key", contents)
            self.assertNotIn("Private user question", contents)
            self.assertEqual(json.loads(contents)["error"]["raw"], "Unsupported request parameter")
            self.assertIn(file.name, str(error.exception))

    async def test_ollama_seed_and_identical_fresh_requests(self):
        bodies = []
        def handler(request):
            bodies.append(request.content)
            return httpx.Response(200, json={"done": True, "message": {"role": "assistant", "content": "Ответ"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(2):
                model = OllamaModel(client, "fixed-model", "rules", Generation(True, 123))
                model.add_user("Вопрос")
                await model.step([])
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(json.loads(bodies[0])["options"], {"num_predict": 4096, "temperature": 0, "seed": 123})

    async def test_compatible_pins_provider_and_sorts_tools(self):
        bodies = []
        def handler(request):
            bodies.append(request.content)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Ответ"}}]})
        tools = [{"name": n, "inputSchema": {"type": "object"}} for n in ["z", "a"]]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for selected in [tools, list(reversed(tools))]:
                model = CompatibleModel(client, "vendor/fixed-model:free", "test", "rules",
                                        "https://openrouter.ai/api/v1", Generation(True, 42, "fixed-provider"))
                model.add_user("Вопрос")
                await model.step(selected)
        self.assertEqual(bodies[0], bodies[1])
        body = json.loads(bodies[0])
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["seed"], 42)
        self.assertFalse(body["parallel_tool_calls"])
        self.assertEqual(body["provider"]["only"], ["fixed-provider"])
        self.assertFalse(body["provider"]["allow_fallbacks"])
        self.assertEqual(body["provider"]["max_price"], {"prompt": 0, "completion": 0})

    async def test_unsupported_or_automatic_configuration_fails_before_network(self):
        for provider, model, url, pin in [
                ("compatible", "openrouter/free", "https://openrouter.ai/api/v1", "provider"),
                ("compatible", "openrouter/auto", "https://openrouter.ai/api/v1", "provider"),
                ("compatible", "vendor/model", "https://openrouter.ai/api/v1", ""),
                ("openai", "model", "", "")]:
            with self.subTest(provider=provider, model=model), self.assertRaises(AgentError):
                Generation(True, 42, pin).validate(provider, model, url)

    async def test_identical_mcp_reads_and_preserved_graph_order(self):
        service = Service("snapshot")
        selected = Selection(path=service.catalog[0].path)
        a, b = respond(lambda: service.read(selected)), respond(lambda: service.read(selected))
        self.assertEqual(a.content, b.content)
        self.assertEqual(a.structuredContent, b.structuredContent)
        from domain import project_graph
        root, _ = project_graph({"id": 1, "successors": [{"id": 3}, {"id": 2}]}, 10)
        self.assertEqual([n.id for n in root.successors], ["3", "2"])
