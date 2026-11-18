import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from answer_evidence import audit_answer, collect_evidence, revalidate_evidence
from domain import Fault, Resource, Service
from gas_composition import analyze_gas_export
from retrieval import search_export, compare_export, index_export, rank_export, read_node_export
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
    def test_read_found_record_by_pointer_preserves_source_and_evidence(self):
        raw = fixture()
        search = search_export(RESOURCE, raw, "Группа А", 10, 20, "live_api")
        found = search.matches[0]
        result = read_node_export(RESOURCE, raw, found.pointer, search.export_sha256, 10, 6, 200, "live_api")
        self.assertEqual(result.selected.evidence_id, found.evidence_id)
        self.assertEqual([r.fields["id"] for r in result.descendants], ["3", "4", "5"])
        self.assertFalse(result.truncated)
        self.assertEqual(result.export_sha256, search.export_sha256)
        sources = collect_evidence(result.model_dump())
        self.assertEqual(len(sources), 4)
        self.assertTrue(all(r["source"] == "live_api" and r["requested_depth"] == 10 for r in sources))
        self.assertEqual(audit_answer(f"Данные [{found.evidence_id}]", {r["evidence_id"]: r for r in sources})["status"], "cited")

    def test_pointer_read_rejects_changed_export_and_missing_pointer(self):
        raw = fixture()
        _, digest = index_export(raw)
        changed = copy.deepcopy(raw)
        changed["successors"].reverse()
        for candidate, pointer, expected in ((changed, LEFT, "EXPORT_CHANGED"), (raw, "/invented", "INVALID_ARGUMENT")):
            with self.subTest(expected=expected), self.assertRaises(Fault) as error:
                read_node_export(RESOURCE, candidate, pointer, digest, 10, 6, 200, "live_api")
            self.assertEqual(error.exception.code, expected)

    def test_pointer_read_enforces_subtree_limits_and_does_not_follow_links(self):
        raw = fixture()
        raw["successors"][0]["successors"].append({"link": "Other$/record;"})
        _, digest = index_export(raw)
        result = read_node_export(RESOURCE, raw, "/successors/0", digest, 10, 0, 200, "live_api")
        self.assertTrue(result.truncated)
        self.assertEqual(result.descendants, [])
        with self.assertRaises(Fault) as error:
            read_node_export(RESOURCE, raw, "/successors/0", digest, 10, 6, 2, "live_api")
        self.assertEqual(error.exception.code, "RESULT_LIMIT")
        result = read_node_export(RESOURCE, raw, "/successors/0/successors/1", digest, 10, 6, 200, "live_api")
        self.assertEqual(result.selected.fields["link"], "Other$/record;")
        self.assertEqual(result.descendants, [])

    def test_service_pointer_read_uses_full_export_without_platform_start(self):
        raw = fixture()
        _, digest = index_export(raw)
        exporter = Mock(return_value=raw)
        service = Service("live", {}, exporter=exporter)
        service.catalog = [RESOURCE]
        result = service.read_export_node(RESOURCE.path, LEFT, digest, 10, 6, 200)
        self.assertEqual(result.selected.fields["id"], "3")
        exporter.assert_called_once_with(RESOURCE.path, depth=10, start=None, headers={})

    def test_gas_analysis_uses_nested_components_and_preserves_joint_limits(self):
        def node(meta, *children, **fields):
            return dict(meta=meta, successors=list(children), **fields)

        def quantity(bound, percent):
            return [node("%", value="%"), node(bound, node("Числовое значение", value=percent))]

        def record(name, symbol, minimum, impurity):
            return node("Газ", node("Химическое обозначение", value=symbol),
                        node("Объемные доли компонентов",
                             node("Компонент", node("Химический элемент", *quantity("Не менее", minimum), name=symbol), name="1"),
                             node("Компонент", impurity, name="2")), name=name)

        combined = node("Совокупность химических веществ", node("Химическое обозначение", value="X"),
                        node("Химическое обозначение", value="Y"), node("Содержание", *quantity("Не более", 0.001)))
        single = node("Химический элемент", *quantity("Не более", 0.02), name="X")
        raw = fixture()
        raw["successors"] = [node("Класс газов", record("Марка Б", "Ar", 99.9, combined),
                                  record("Марка А", "Ar", 99.99, combined), name="Газы на основе Аргона"),
                             node("Класс газов", record("Марка В", "H", 99.8, single), name="Газы на основе Водорода")]

        ranking = analyze_gas_export(RESOURCE, raw, "Ar", "", 10).groups[0].records
        self.assertEqual([row.record.trail[-1] for row in ranking], ["Марка А", "Марка Б"])
        self.assertEqual((ranking[0].shares[0].relation, ranking[0].shares[0].percent), ("≥", "99.99"))
        self.assertEqual((ranking[0].shares[1].components, ranking[0].shares[1].relation), (["X", "Y"], "≤"))
        compared = analyze_gas_export(RESOURCE, raw, "Ar", "H", 10)
        self.assertEqual(compared, analyze_gas_export(RESOURCE, raw, "Ar", "H", 10))
        self.assertEqual([item.component for item in compared.common_impurities], ["X"])
        match = compared.common_impurities[0]
        sources = {e["evidence_id"] for e in collect_evidence(compared.model_dump())}
        self.assertNotEqual(match.left.evidence_id, match.right.evidence_id)
        self.assertTrue({match.left.evidence_id, match.right.evidence_id} <= sources)

    def test_rank_live_percentages_numerically_and_attach_composition_evidence(self):
        raw = fixture()
        raw["successors"] = [
            {"name": name, "successors": [
                {"name": "Состав", "successors": [
                    {"name": "Доля X", "value": value},
                    {"name": "Доля Y", "value": 1},
                ]}]}
            for name, value in [("B", "98,7 %"), ("A", 99.1), ("C", "99,1"),
                                ("Range", "99–100"), ("Limit", "≥99.2"), ("Invalid", 101)]
        ]
        _, digest = index_export(raw)
        result = rank_export(RESOURCE, raw, "", ["Состав"], ["Доля X"], "volume", digest, 10, 2)
        self.assertEqual([(row.record.trail[-1], row.percent) for row in result.ranking],
                         [("A", "99.1"), ("C", "99.1")])
        self.assertEqual((result.matched_records, result.skipped_records), (3, 3))
        self.assertEqual(result.ranking[0].composition.children[1]["value"], 1)
        ids = {e["evidence_id"] for e in collect_evidence(result.model_dump())}
        self.assertIn(result.ranking[0].percentage.evidence_id, ids)
        self.assertIn(result.ranking[0].composition.evidence_id, ids)

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

    def test_search_can_find_impurities_recorded_only_in_a_comment(self):
        raw = fixture()
        raw["successors"][0]["comment"] = "Примеси по тестовым данным: водород и аргон."
        result = search_export(RESOURCE, raw, "АРГОН", 10, 20, "live_api")
        self.assertEqual(result.total_matches, 1)
        self.assertEqual(result.matches[0].trail, ["Тестовые газы", "Группа А"])
        self.assertIn("водород и аргон", result.matches[0].fields["comment"])

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

    def test_historical_witnesses_require_identical_fresh_export(self):
        raw = fixture()
        old = collect_evidence(search_export(RESOURCE, raw, "X", 10, 20, "live_api").model_dump())
        history = {r["evidence_id"]: r for r in old}
        witness = next(r for r in old if r["pointer"] != "/matches")
        fresh = collect_evidence(search_export(RESOURCE, raw, "Z", 10, 20, "live_api").model_dump())
        ledger = revalidate_evidence(history, {r["evidence_id"]: r for r in fresh})
        self.assertTrue(ledger[witness["evidence_id"]]["revalidated"])
        self.assertEqual(audit_answer(f"X [{witness['evidence_id']}]", ledger)["status"], "cited")
        for field, changed in (("export_sha256", "changed"), ("source", "saved_snapshot"),
                               ("requested_depth", 3), ("export_sha256", None)):
            with self.subTest(field=field, changed=changed):
                altered = [{**r, field: changed} for r in fresh]
                ledger = revalidate_evidence(history, {r["evidence_id"]: r for r in altered})
                self.assertNotIn(witness["evidence_id"], ledger)
        different_resource = [{**r, "resource": {**r["resource"], "resource_id": "other"}} for r in fresh]
        ledger = revalidate_evidence(history, {r["evidence_id"]: r for r in different_resource})
        self.assertNotIn(witness["evidence_id"], ledger)

    def test_citation_audit_distinguishes_historical_and_unknown_references(self):
        audit = audit_answer("Раньше [Eold], неизвестно [Efake]", {}, {"Eold": {}})
        self.assertEqual(audit["status"], "invalid_citations")
        self.assertEqual(audit["historical_citations"], ["Eold"])
        self.assertEqual(audit["unknown_citations"], ["Efake"])
        self.assertIn("предыдущих вопросах", audit["note"])

    def test_grouped_citations_are_all_checked(self):
        ledger = {"Efirst": {}, "Esecond": {}}
        audit = audit_answer("Предел [Efirst, Esecond], повтор [Efirst].", ledger)
        self.assertEqual(audit["status"], "cited")
        self.assertEqual(len(audit["sources"]), 2)
        audit = audit_answer("Предел [Efirst; Efake].", ledger)
        self.assertEqual(audit["status"], "invalid_citations")
        self.assertEqual(audit["unknown_citations"], ["Efake"])

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
                self.assertTrue(tools["read_export_node"].annotations.readOnlyHint)
                result = await session.call_tool("search_inforesource", {
                    "path": "Загрузки/Система единиц физических величин", "query": "Система", "depth": 0})
                self.assertFalse(result.isError)
                self.assertEqual(result.structuredContent["data"]["total_matches"], 1)
                self.assertEqual(result.structuredContent["data"]["source"], "saved_snapshot")
                import jsonschema
                jsonschema.validate(result.structuredContent, tools["search_inforesource"].outputSchema)
                search = result.structuredContent["data"]
                result = await session.call_tool("read_export_node", {
                    "path": search["resource"]["path"], "pointer": search["matches"][0]["pointer"],
                    "export_sha256": search["export_sha256"], "depth": 0, "levels": 0})
                self.assertFalse(result.isError)
                self.assertEqual(result.structuredContent["data"]["selected"]["fields"]["id"], "303808806649860")
                jsonschema.validate(result.structuredContent, tools["read_export_node"].outputSchema)
