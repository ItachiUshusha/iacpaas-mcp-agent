import json
import unittest
from unittest.mock import Mock, patch

import jsonschema

from agent import child_environment
from agent_core import Agent, Call
from domain import Service
import iacpaas_client as api
from server import respond
from smoke import connect


class FolderTests(unittest.TestCase):
    def test_exact_http_methods_payloads_and_no_permanent_delete(self):
        captured = []
        def opened(request, timeout):
            captured.append(request)
            response = Mock()
            response.read.return_value = b'{"success":true}'
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            return response
        with patch("iacpaas_client.build_opener") as opener:
            opener.return_value.open.side_effect = opened
            api.create_folder("Загрузки", "Учебная папка", description="Тест", headers={"X-API-KEY": "test-only"})
            api.delete_folder("Загрузки/Учебная папка", headers={"X-API-KEY": "test-only"})
        self.assertEqual([r.get_method() for r in captured], ["POST", "DELETE"])
        self.assertTrue(captured[0].full_url.endswith("/api/storage-unit/folder/create"))
        self.assertEqual(json.loads(captured[0].data), {"path": "Загрузки", "name": "Учебная папка", "description": "Тест"})
        self.assertEqual(json.loads(captured[1].data), {"path": "Загрузки/Учебная папка", "mode": "trashIR"})

    def test_only_explicit_success_is_accepted_and_no_retry(self):
        for body in ({}, {"success": False}, {"success": True, "error": "private body"},
                     {"success": True, "platformUnavailable": True}, {"success": 1}):
            with self.subTest(body=body), patch("iacpaas_client.request_json", return_value=body) as request:
                response = respond(lambda: Service("live", {}).create_folder("Загрузки", "Тест"))
                self.assertTrue(response.isError)
                self.assertNotIn("private body", response.model_dump_json())
                request.assert_called_once()

    def test_success_result_and_root_creation(self):
        with patch("iacpaas_client.request_json", return_value={"success": True}):
            result = Service("live", {}).create_folder("", "Учебная папка")
            self.assertEqual(result.path, "Учебная папка")
            self.assertEqual(result.operation, "created")
            self.assertEqual(Service("live", {}).delete_folder(result.path, result.path).operation, "moved_to_trash")

    def test_invalid_paths_fail_before_network(self):
        invalid = ["", "/", "/test", "a/", "a//b", "a/../b", "a/./b", " a", "a /b",
                   "Мой Фонд/a", "a;b", "a$b", "a:b", "a\\b", "a\nb", "x" * 513]
        with patch("iacpaas_client.request_json") as request:
            for path in invalid:
                with self.subTest(path=path):
                    response = respond(lambda: Service("live", {}).delete_folder(path, path))
                    self.assertTrue(response.isError)
            for name in ["", "a/b", "..", " x", "x" * 201]:
                self.assertTrue(respond(lambda: Service("live", {}).create_folder("Загрузки", name)).isError)
            request.assert_not_called()

    def test_protected_paths_confirmation_snapshot_and_auth(self):
        live = Service("live", {})
        with patch("iacpaas_client.request_json") as request:
            for path in ["Загрузки", "Корзина", live.catalog[0].path]:
                result = respond(lambda: live.delete_folder(path, path))
                self.assertEqual(result.structuredContent["error"]["code"], "PROTECTED_PATH")
            result = respond(lambda: live.delete_folder("Загрузки/Тест", "Загрузки/Другая"))
            self.assertEqual(result.structuredContent["error"]["code"], "CONFIRMATION_REQUIRED")
            for operation in [lambda: Service("snapshot").create_folder("", "Тест"),
                              lambda: Service("snapshot").delete_folder("Тест", "Тест")]:
                self.assertEqual(respond(operation).structuredContent["error"]["code"], "LIVE_REQUIRED")
            with patch.dict("os.environ", {}, clear=True), patch("builtins.input", side_effect=AssertionError):
                result = respond(lambda: Service("live").create_folder("", "Тест"))
                self.assertEqual(result.structuredContent["error"]["code"], "AUTH_REQUIRED")
            request.assert_not_called()

    def test_catalog_order_is_independent_of_file_order(self):
        service = Service("snapshot")
        first = service.list_resources("", 2).model_dump()
        service.catalog.reverse()
        self.assertEqual(first, service.list_resources("", 2).model_dump())


class FolderTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_transports_expose_writes_and_reject_snapshot(self):
        for transport in ("stdio", "http"):
            async with connect(transport, "snapshot", child_environment()) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                for name, args in [("create_folder", {"parent": "Загрузки", "name": "Тест"}),
                                   ("delete_folder", {"path": "Загрузки/Тест", "confirm_path": "Загрузки/Тест"})]:
                    self.assertFalse(tools[name].annotations.readOnlyHint)
                    self.assertEqual(tools[name].annotations.destructiveHint, name == "delete_folder")
                    result = await session.call_tool(name, args)
                    jsonschema.validate(result.structuredContent, tools[name].outputSchema)
                    self.assertTrue(result.isError)
                    self.assertEqual(result.structuredContent["error"]["code"], "LIVE_REQUIRED")

    async def test_agent_supports_folder_calls_and_blocks_duplicate_writes(self):
        async with connect("stdio", "snapshot", child_environment()) as session:
            agent = Agent(session, None, Mock())
            await agent.initialize()
            call = Call("1", "create_folder", {"parent": "", "name": "Тест"})
            result = await agent.execute(call)
            self.assertEqual(result["error"]["code"], "LIVE_REQUIRED")
            result = await agent.execute(call)
            self.assertEqual(result["error"]["code"], "WRITE_ALREADY_ATTEMPTED")
