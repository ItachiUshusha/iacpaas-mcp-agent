import json
import unittest
from unittest.mock import Mock, patch

import httpx
import jsonschema

from agent import child_environment
from agent_core import Agent, Call
from answer_evidence import collect_evidence
from domain import Selection, Service
import iacpaas_client as api
from server import respond
from smoke import connect


class FolderTests(unittest.TestCase):
    def test_search_fund_checks_content_under_generic_name_and_reports_failed_exports(self):
        folder = "Учебные материалы"
        path = f"{folder}/Справочник"
        failed_path = f"{folder}/Закрытая база"
        fund = Mock()
        fund.list_items.side_effect = {
            "": [{"path": "/" + folder, "title": folder, "nodeType": "folder"}],
            folder: [
                {"path": f"{folder} / Справочник", "title": "Справочник", "nodeType": "storage unit", "id": "101"},
                {"path": f"{folder} / Закрытая база", "title": "Закрытая база", "nodeType": "storage unit", "id": "102"},
            ],
        }.__getitem__

        def export(path, **options):
            if path == failed_path:
                raise api.ClientError("Нет доступа.")
            return {"code": 101, "title": "Справочник", "name": "Справочник", "successors": [
                {"name": "Газы", "successors": [{"name": "на основе водорода"},
                                               {"name": "на основе аргона"},
                                               {"name": "Примеси"}]},
            ]}

        service = Service("live", {}, Mock(side_effect=export), fund_session=fund)
        self.assertEqual(service.list_fund_resources("", "газ", 12).resources, [])
        result = service.search_fund_content("", ["водород", "аргон", "примес"])
        by_path = {item.resource.path: item for item in result.results}
        success, skipped = by_path[path], by_path[failed_path]
        self.assertEqual(success.resource.path, path)
        self.assertEqual([report.total_matches for report in success.reports], [1, 1, 1])
        self.assertEqual(skipped.error.code, "UPSTREAM_ERROR")
        self.assertEqual(service.exporter.call_count, 2)
        evidence = collect_evidence(result.model_dump())
        self.assertTrue(any(item["trail"][-1] == "на основе аргона" for item in evidence))

    def test_fund_discovery_and_live_search_outside_configured_catalog(self):
        folder = "Учебные материалы"
        path = f"{folder}/Газы"
        fund = Mock()
        fund.list_items.side_effect = {
            "": [{"path": "/" + folder, "title": folder, "nodeType": "folder"},
                 {"path": "Корзина", "title": "Корзина", "nodeType": "folder"}],
            folder: [{"path": f"{folder} / Газы", "title": "Газы", "nodeType": "storage unit",
                      "id": "9999999999999999999"},
                     {"path": "Другой раздел / Газы", "title": "Газы", "nodeType": "storage unit",
                      "id": "42"}],
        }.__getitem__
        exporter = Mock(return_value={"code": 9999999999999999999, "title": "Газы",
                                      "name": "Газы", "successors": [{"name": "Аргон"}]})
        service = Service("live", {}, exporter, fund_session=fund)
        self.assertFalse(service.list_resources("Газы", 10).resources)
        listing = service.list_fund_resources("", "газ", 10)
        self.assertEqual([row.path for row in listing.resources], [path])
        self.assertEqual(listing.scanned_folders, 2)
        self.assertFalse(listing.truncated)
        search = service.search(path, "аргон", 10)
        self.assertEqual(search.source, "live_api")
        self.assertEqual(search.matches[0].trail, ["Газы", "Аргон"])
        self.assertEqual(service.read(Selection(path=path)).resource.resource_id, "9999999999999999999")
        exporter.return_value = {"code": 42, "title": "Другая база"}
        self.assertEqual(respond(lambda: service.search(path, "аргон", 10))
                         .structuredContent["error"]["code"], "RESOURCE_MISMATCH")

    def test_creation_payload_and_no_network_without_fund_login(self):
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
            result = respond(lambda: Service("live").delete_folder("Загрузки/Учебная папка", "Загрузки/Учебная папка"))
            self.assertTrue(result.isError)
            self.assertEqual(result.structuredContent["error"]["code"], "FUND_LOGIN_REQUIRED")
        self.assertEqual([r.get_method() for r in captured], ["POST"])
        self.assertTrue(captured[0].full_url.endswith("/api/storage-unit/folder/create"))
        self.assertEqual(json.loads(captured[0].data), {"path": "Загрузки", "name": "Учебная папка", "description": "Тест"})

    def test_recycle_uses_authenticated_fund_session_and_only_exact_folder(self):
        path = "Загрузки/Тест"
        items = [{"path": path, "title": "Тест", "nodeType": "folder"},
                 {"path": path + "-другая", "title": "Тест-другая", "nodeType": "folder"}]
        deleted = []

        def handler(request):
            if request.url.path == "/login":
                return httpx.Response(302, headers={"Location": "/", "Set-Cookie": "SESSION=test-session; Secure; Path=/"})
            self.assertEqual(request.headers.get("cookie"), "SESSION=test-session")
            if request.url.path == "/fund/structure":
                self.assertEqual(request.url.params["my"], "true")
                return httpx.Response(200, json={"success": True, "list": items if request.url.params["path"] == "Загрузки" else []})
            self.assertEqual((request.method, request.url.path), ("POST", "/fund/deleteItem"))
            deleted.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True})

        with httpx.Client(transport=httpx.MockTransport(handler), base_url=api.BASE_URL) as client:
            fund = api.FundSession(client)
            fund.login("test-user", "test-password")
            service = Service("live", {}, fund_session=fund)
            result = service.delete_folder(path, path)
            self.assertEqual(result.operation, "moved_to_trash")
            items[0]["nodeType"] = "storageUnit"
            refusal = respond(lambda: service.delete_folder(path, path))
            self.assertTrue(refusal.isError)
        self.assertEqual(deleted, [{"my": True, "recycle": True,
                                    "selected": [{"path": path, "title": "Тест", "nodeType": "folder"}]}])

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
            agent.user_request = "Покажи, как создать папку Тест"
            self.assertEqual((await agent.execute(call))["error"]["code"], "CONFIRMATION_REQUIRED")
            agent.user_request = "Создай папку Другая"
            self.assertEqual((await agent.execute(call))["error"]["code"], "CONFIRMATION_REQUIRED")
            agent.user_request = "Создай папку Тест"
            result = await agent.execute(call)
            self.assertEqual(result["error"]["code"], "LIVE_REQUIRED")
            result = await agent.execute(call)
            self.assertEqual(result["error"]["code"], "WRITE_ALREADY_ATTEMPTED")

            deletion = Call("2", "delete_folder", {"path": "Загрузки/Тест", "confirm_path": "Загрузки/Тест"})
            agent.user_request = "Перемести папку Загрузки/Тест-другая в корзину"
            self.assertEqual((await agent.execute(deletion))["error"]["code"], "CONFIRMATION_REQUIRED")
            agent.user_request = "Перемести папку «Загрузки/Тест» в корзину IACPaaS"
            self.assertEqual((await agent.execute(deletion))["error"]["code"], "LIVE_REQUIRED")
