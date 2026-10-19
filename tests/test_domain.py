import copy
import json
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from domain import BASE, Fault, Selection, Service, project_graph
from server import respond
import iacpaas_client as api


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.service = Service("snapshot")
        self.path = self.service.catalog[0].path
        self.raw = json.loads((BASE / "fixtures/root.json").read_text(encoding="utf-8"))

    def test_saved_real_root_and_identifiers(self):
        graph = self.service.read(Selection(path=self.path))
        self.assertEqual(graph.resource.resource_id, "4640056526276364782")
        self.assertEqual(graph.root.id, "303808806649860")
        self.assertEqual(graph.counts.node_occurrences, 1)
        self.assertEqual(graph.counts.edge_occurrences, 0)
        self.assertFalse(graph.complete_graph)
        self.assertNotIn("owner_id", graph.model_dump_json())

    def test_snapshot_never_pretends_to_have_descendants(self):
        with self.assertRaises(Fault) as error:
            self.service.read(Selection(path=self.path, depth=1))
        self.assertEqual(error.exception.code, "SNAPSHOT_SCOPE")

    def test_catalog_search_is_explicitly_local(self):
        listing = self.service.list_resources("ЕДИНИЦ", 1)
        self.assertEqual(len(listing.resources), 1)
        self.assertFalse(listing.access_checked)
        self.assertEqual(listing.source, "configured_catalog")
        self.assertEqual(self.service.list_resources("does not exist", 1).resources, [])

    def test_unknown_resource_fails_before_network(self):
        exporter = Mock()
        with self.assertRaises(Fault):
            Service("live", exporter=exporter).read(Selection(path="unknown"))
        exporter.assert_not_called()

    def test_strict_limits(self):
        for fields in ({"depth": 4}, {"depth": -1}, {"depth": True}, {"depth": "1"}, {"max_nodes": 201}, {"max_nodes": 0}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                Selection(path=self.path, **fields)

    def test_live_export_parameters_and_projection(self):
        exporter = Mock(return_value=self.raw)
        service = Service("live", {"X-API-KEY": "test-only"}, exporter)
        graph = service.read(Selection(path=self.path, depth=2, start="known/path"))
        exporter.assert_called_once_with(self.path, depth=2, start="known/path", headers={"X-API-KEY": "test-only"})
        self.assertEqual(graph.source, "live_api")

    def test_count_links_cycles_and_repeated_ids_without_inventing_total(self):
        # Synthetic universal export: repeated node ID + back-reference + external reference.
        raw = {"id": 1, "successors": [{"id": 2, "successors": [{"link": "root;"}]},
                                           {"id": 2}, {"link": "external$/node;"}]}
        _, counts = project_graph(raw, 10)
        self.assertEqual(counts.node_occurrences, 5)
        self.assertEqual(counts.distinct_defined_ids, 2)
        self.assertEqual(counts.link_occurrences, 2)
        self.assertEqual(counts.edge_occurrences, 4)
        self.assertFalse(counts.full_graph_count)

    def test_node_limit_rejects_without_partial_success(self):
        with self.assertRaises(Fault) as error:
            project_graph({"id": 1, "successors": [{"id": 2}]}, 1)
        self.assertEqual(error.exception.code, "RESULT_LIMIT")

    def test_size_limit_rejects_without_partial_success(self):
        raw = copy.deepcopy(self.raw)
        raw["name"] = "x" * 128_001
        service = Service("live", {}, Mock(return_value=raw))
        with self.assertRaises(Fault) as error:
            service.read(Selection(path=self.path))
        self.assertEqual(error.exception.code, "RESULT_LIMIT")

    def test_mismatched_resource_rejected(self):
        raw = dict(self.raw, code="wrong")
        with self.assertRaises(Fault) as error:
            Service("live", {}, Mock(return_value=raw)).read(Selection(path=self.path))
        self.assertEqual(error.exception.code, "RESOURCE_MISMATCH")

    def test_malformed_graph_rejected(self):
        with self.assertRaises(Fault) as error:
            project_graph({"successors": "bad"}, 100)
        self.assertEqual(error.exception.code, "EXPORT_FORMAT")

    def test_missing_credentials_does_not_prompt(self):
        with patch.dict("os.environ", {}, clear=True), patch("builtins.input", side_effect=AssertionError):
            result = respond(lambda: Service("live").read(Selection(path=self.path)))
        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["error"]["code"], "AUTH_REQUIRED")

    def test_fault_and_unexpected_errors_do_not_leak(self):
        result = respond(lambda: (_ for _ in ()).throw(RuntimeError("secret-test-value")))
        self.assertTrue(result.isError)
        self.assertNotIn("secret-test-value", result.model_dump_json())
        service = Service("live", {}, Mock(side_effect=api.ClientError("HTTP 403. Нет доступа.")))
        result = respond(lambda: service.read(Selection(path=self.path)))
        self.assertEqual(result.structuredContent["error"]["code"], "UPSTREAM_ERROR")


if __name__ == "__main__":
    unittest.main()
