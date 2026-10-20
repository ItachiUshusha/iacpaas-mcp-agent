import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import iacpaas_client as api
import study_materials as study
from domain import BASE, Fault, Selection, Service, project_graph


class StudyTests(unittest.TestCase):
    def test_new_material_resource_does_not_receive_units_snapshot(self):
        service = Service("snapshot")
        resource = next(row for row in service.catalog if row.title == "База свойств материалов")
        with self.assertRaises(Fault) as error:
            service.read(Selection(path=resource.path))
        self.assertEqual(error.exception.code, "SNAPSHOT_SCOPE")

    def test_ontology_projection_preserves_constraints_and_value_type(self):
        raw = {"id": 1, "type": "КОРЕНЬ", "successors": [{"id": 2, "name": "Тестовая характеристика",
               "meta": "Характеристика", "specifier": "set", "restrictor": "new", "valtype": "STRING",
               "comment": "Синтетический пример для теста, не данные IACPaaS"}]}
        root, _ = project_graph(raw, 10)
        node = root.successors[0]
        self.assertEqual(node.specifier, "set")
        self.assertEqual(node.restrictor, "new")
        self.assertEqual(node.valtype, "STRING")
        self.assertIn("Синтетический", node.comment)

    def test_catalog_uses_only_authenticated_matching_roots(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temp:
            base = Path(temp)
            (base / "catalog.json").write_text("[]", encoding="utf-8")
            output = base / "collected"
            output.mkdir()

            def export(path, depth, headers):
                title = path.split("/", 1)[1]
                return {"title": title, "code": "test-resource-" + title, "id": 1,
                        "type": "КОРЕНЬ", "json_type": "universal", "ontology": "Тестовая онтология$;",
                        "successors": [{"id": 2, "name": "Тестовые данные", "meta": "Тестовый тип"}] if depth else []}

            with patch.object(study, "BASE", base), patch.object(study.api, "export_resource", side_effect=export):
                manifest = study.collect({"X-API-KEY": "never-save-test-key"}, 3, output)
            catalog = json.loads((base / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(len(catalog), 3)
            self.assertTrue(all(r.get("fragment_file") for r in manifest["resources"]))
            self.assertTrue((output / "FACTS.md").is_file())
            combined = "".join(p.read_text(encoding="utf-8") for p in output.iterdir())
            self.assertNotIn("never-save-test-key", combined)

    def test_failed_export_does_not_register_guessed_ids(self):
        with tempfile.TemporaryDirectory(dir=BASE) as temp:
            base = Path(temp)
            (base / "catalog.json").write_text("[]", encoding="utf-8")
            output = base / "collected"
            output.mkdir()
            with patch.object(study, "BASE", base), patch.object(study.api, "export_resource", side_effect=api.ClientError("Нет доступа")):
                manifest = study.collect({}, 1, output)
            self.assertEqual(json.loads((base / "catalog.json").read_text(encoding="utf-8")), [])
            self.assertTrue(all("error" in r for r in manifest["resources"]))


if __name__ == "__main__":
    unittest.main()
