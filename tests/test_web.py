import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

from agent import child_environment
from agent_core import AgentError, Call
from providers import CompatibleModel
from web_app import create_app


class CompatibleTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_roundtrip_free_routing_and_continuation(self):
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            msg = {"role": "assistant", "content": "Готово"} if len(payloads) == 2 else {
                "role": "assistant", "content": None, "reasoning_details": [{"type": "opaque"}],
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_inforesources", "arguments": "{}"}}]}
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop" if len(payloads) == 2 else "tool_calls", "message": msg}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = CompatibleModel(client, "openrouter/free", "test-secret", "rules", "https://openrouter.ai/api/v1")
            model.add_user("Найди ресурс")
            tools = [{"name": "list_inforesources", "inputSchema": {"type": "object"}}]
            turn = await model.step(tools)
            model.add_result(turn.calls[0], {"ok": True})
            self.assertEqual((await model.step(tools)).text, "Готово")
        self.assertEqual(payloads[0]["provider"]["max_price"], {"prompt": 0, "completion": 0})
        self.assertEqual(payloads[1]["messages"][-1]["tool_call_id"], "c1")
        self.assertIn("reasoning_details", payloads[1]["messages"][-2])

    async def test_truncated_response_and_unsafe_url_rejected(self):
        with self.assertRaises(AgentError):
            CompatibleModel(None, "m", "key", "", "http://example.com/v1")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,
            json={"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "partial"}}]}))) as client:
            with self.assertRaises(AgentError):
                await CompatibleModel(client, "m", "k", "", "https://example.com/v1").step([])


class WebTests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(provider="compatible", model="openrouter/free", base_url="https://openrouter.ai/api/v1",
                               mcp_url=None, transport="stdio", mode="snapshot")

    def test_browser_chat_real_mcp_mocked_cloud_and_followup(self):
        async def model_response(client, url, payload, headers=None):
            history = payload["messages"]
            if history[-1]["role"] == "user":
                name, arguments = "list_inforesources", {"query": "Система единиц"}
            else:
                data = json.loads(history[-1]["content"])
                if "resources" in data.get("data", {}):
                    name, arguments = "count_graph", {"path": data["data"]["resources"][0]["path"], "depth": 0}
                else:
                    self.assertEqual(data["data"]["counts"]["node_occurrences"], 1)
                    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Снимок: 1 вершина, 0 связей."}}]}
            return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": str(len(history)), "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}

        with patch("providers.post_json", side_effect=model_response):
            with TestClient(create_app(self.args(), child_environment(), "test-secret")) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/assets/app.js").status_code, 200)
                self.assertIn("frame-ancestors", client.get("/").headers["content-security-policy"])
                status = client.get("/api/status").json()
                self.assertNotIn("test-secret", json.dumps(status))
                headers = {"X-Chat-Token": status["token"]}
                self.assertEqual(client.post("/api/chat", json={"question": "x"}).status_code, 403)
                response = client.post("/api/chat", headers=headers, json={"question": "Найди ресурс и посчитай корень"})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertIn("1 вершина", result["answer"])
                calls = [e for e in result["events"] if e["event"] == "tool_call"]
                self.assertEqual([c["name"] for c in calls], ["list_inforesources", "count_graph"])
                self.assertEqual(calls[-1]["result"]["data"]["source"], "saved_snapshot")
                self.assertNotIn("test-secret", response.text)
                followup = client.post("/api/chat", headers=headers, json={"question": "Повтори", "session": result["session"]})
                self.assertEqual(followup.json()["session"], result["session"])
                self.assertEqual(client.post("/api/chat", headers=headers, json={"question": "x", "session": result["session"], "reset": True}).status_code, 200)
                self.assertEqual(client.post("/api/chat", headers=headers, json={"question": "x", "session": result["session"]}).status_code, 404)

    def test_missing_key_and_origin_are_explicit(self):
        with TestClient(create_app(self.args(), child_environment(), "")) as client:
            token = client.get("/api/status").json()["token"]
            headers = {"X-Chat-Token": token}
            self.assertEqual(client.post("/api/chat", headers=headers, json={"question": "x"}).status_code, 503)
            self.assertEqual(client.post("/api/chat", headers={**headers, "Origin": "https://other.example"}, json={"question": "x"}).status_code, 403)
            self.assertEqual(client.post("/api/chat", headers=headers, json=[]).status_code, 400)
