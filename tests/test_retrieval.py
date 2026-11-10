import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from answer_evidence import audit_answer, collect_evidence
from domain import Fault, Resource, Service
from retrieval import search_export, compare_export, index_export
from agent import child_environment
from smoke import connect


def fixture():
    # Synthetic: verifies the algorithm, not the chemistry of real products.
    return {"title": "Тестовые газы", "code": "test-gases", "type": "КОРЕНЬ", "id": 1,
            "name": "Тестовые газы", "successors": [
        {"id": 2, "name": "Группа А", "successors": [
            {"id": 3, "name": "Примеси", "successors": [{"id": 4, "name": "X"}, {"id": 5, "name": "Y"}]}]},
        {"id": 6, "name": "Группа Б", "successors": [
            {"id": 7, "name": "Примеси", "successors": [{"id": 8, "name": " y "}, {"id": 9, "name": "Z"}]}]}
    ]}


RESOURCE = Resource(resource_id="test-gases", title="Тестовые газы", path="Загрузки/Тестовые газы")
LEFT = "/successors/0/successors/0"
RIGHT = "/successors/1/successors/0"


class RetrievalTests(unittest.TestCase):
    def test_search_inside_nodes_not_titles_and_preserves_context(self):
        result = search_export(RESOURCE, fixture(), "примес", 10, 20, "saved_snapshot")
        self.assertEqual(result.total_matches, 2)
        self.assertEqual(result.matches[0].pointer, LEFT)
        self.assertEqual(result.matches[0].trail, ["Тестовые газы", "Группа А", "Примеси"])
        self.assertEqual(result.matches[0].children[0]["name"], "X")
        self.assertFalse(result.complete_graph)
        self.assertEqual(result, search_export(RESOURCE, fixture(), "примес", 10, 20, "saved_snapshot"))

    def test_comparison_returns_two_sided_evidence(self):
        raw = fixture()
        _, digest = index_export(raw)
        result = compare_export(RESOURCE, raw, LEFT, RIGHT, digest, 10, "saved_snapshot")
        self.assertEqual([r["label"] for r in result.common], ["Y"])
        self.assertEqual(result.left_only, ["x"])
        self.assertEqual(result.right_only, ["z"])
        self.assertEqual(result.common[0]["left_evidence"][0]["fields"]["id"], "5")
        self.assertEqual(result.common[0]["right_evidence"][0]["fields"]["id"], "8")
        records = collect_evidence(result.model_dump())
        ledger = {r["evidence_id"]: r for r in records}
        common = result.common[0]
        text = "Y [" + common["left_evidence"][0]["evidence_id"] + "] [" + common["right_evidence"][0]["evidence_id"] + "]"
        audit = audit_answer(text, ledger)
        self.assertEqual(audit["status"], "cited")
        self.assertEqual(len(audit["sources"]), 2)
        self.assertIn("не автоматическая", audit["note"])

    def test_stale_pointers_missing_empty_and_linked_lists_fail(self):
        raw = fixture()
        _, digest = index_export(raw)
        changed = copy.deepcopy(raw); changed["successors"].reverse()
        for candidate, left, right, expected in [
                (changed, LEFT, RIGHT, "EXPORT_CHANGED"),
                (raw, "/guessed", RIGHT, "INVALID_ARGUMENT"),
                (raw, LEFT, LEFT, "INVALID_ARGUMENT")]:
            with self.subTest(expected=expected), self.assertRaises(Fault) as error:
                compare_export(RESOURCE, candidate, left, right, digest, 10, "saved_snapshot")
            self.assertEqual(error.exception.code, expected)
        for children, expected in [([], "INSUFFICIENT_DATA"), ([{"link": "somewhere"}], "UNRESOLVED_REFERENCE")]:
            altered = fixture()
            altered["successors"][0]["successors"][0]["successors"] = children
            _, digest = index_export(altered)
            with self.assertRaises(Fault) as error:
                compare_export(RESOURCE, altered, LEFT, RIGHT, digest, 10, "saved_snapshot")
            self.assertEqual(error.exception.code, expected)

    def test_no_match_does_not_claim_complete_search_and_limits_are_visible(self):
        empty = search_export(RESOURCE, fixture(), "not-present", 10, 20, "saved_snapshot")
        self.assertEqual(empty.total_matches, 0)
        self.assertFalse(empty.complete_graph)
        self.assertTrue(empty.warnings)
        limited = search_export(RESOURCE, fixture(), "примес", 10, 1, "saved_snapshot")
        self.assertTrue(limited.matches_truncated)
        self.assertEqual(limited.total_matches, 2)
        with self.assertRaises(Fault):
            search_export(RESOURCE, fixture(), " ", 10, 20, "saved_snapshot")

    def test_citation_checker_does_not_certify_missing_or_fabricated_evidence(self):
        records = collect_evidence(search_export(RESOURCE, fixture(), "примес", 10, 20, "saved_snapshot").model_dump())
        ledger = {r["evidence_id"]: r for r in records}
        self.assertEqual(audit_answer("Всё доказано", ledger)["status"], "uncited")
        self.assertEqual(audit_answer("Да [Efake]", ledger)["status"], "invalid_citations")
        self.assertEqual(audit_answer("Да [Efake]", ledger)["sources"], [])

    def test_only_configured_matching_resource_is_searched(self):
        service = Service("live", {}, exporter=Mock(return_value=fixture()))
        service.catalog = [RESOURCE]
        result = service.search(RESOURCE.path, "примес", 10, 20)
        self.assertEqual(result.total_matches, 2)
        service.exporter.assert_called_once_with(RESOURCE.path, depth=10, start=None, headers={})
        with self.assertRaises(Fault):
            service.search("invented", "примес")

    def test_graph_evidence_preserves_source_and_actual_depth(self):
        graph = {"resource": RESOURCE.model_dump(), "source": "live_api", "requested_depth": 2, "root": fixture()}
        records = collect_evidence(graph)
        self.assertTrue(all(r["source"] == "live_api" and r["requested_depth"] == 2 for r in records))
        self.assertEqual(records, collect_evidence(graph))


class RetrievalTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_is_exposed_over_both_mcp_transports(self):
        for transport in ("stdio", "http"):
            async with connect(transport, "snapshot", child_environment()) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                self.assertTrue(tools["search_inforesource"].annotations.readOnlyHint)
                self.assertTrue(tools["compare_node_children"].annotations.readOnlyHint)
                result = await session.call_tool("search_inforesource", {
                    "path": "Загрузки/Система единиц физических величин", "query": "Система", "depth": 0})
                self.assertFalse(result.isError)
                self.assertEqual(result.structuredContent["data"]["total_matches"], 1)
                self.assertEqual(result.structuredContent["data"]["source"], "saved_snapshot")
                import jsonschema
                jsonschema.validate(result.structuredContent, tools["search_inforesource"].outputSchema)
