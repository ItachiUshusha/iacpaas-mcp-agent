import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from domain import BASE
import folders_demo as demo
import iacpaas_client as api


class FolderDiagnosticTests(unittest.TestCase):
    def test_platform_failure_saved_locally_without_credentials_or_raw_body_in_error(self):
        body = {"success": False, "error": "private diagnostic sensitive-token-value", "explanationSafe": "Type is not supported", "accessToken": "sensitive-token-value"}
        response = Mock()
        response.read.return_value = json.dumps(body).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory(dir=BASE) as tmp, patch.object(api, "FOLDER_DIAGNOSTIC_DIR", Path(tmp)), patch.object(api, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(api.ClientError) as error:
                api.create_folder("Загрузки", "MCP-demo-test", headers={"Authorization": "Bearer sensitive-token-value"})
            self.assertNotIn("private diagnostic", str(error.exception))
            self.assertNotIn("sensitive-token-value", str(error.exception))
            self.assertIn("folder-error-", str(error.exception))
            files = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text(encoding="utf-8")
            self.assertNotIn("sensitive-token-value", text)
            result = json.loads(text)["response"]
            self.assertEqual(result["explanationSafe"], "Type is not supported")
            self.assertNotIn("accessToken", result)
            opener.return_value.open.assert_called_once()

    def test_http_failure_captures_json_diagnostic_without_retry(self):
        body = json.dumps({"error": "Permission denied", "explanationSafe": "Cannot create folder"}).encode()
        error = HTTPError("https://iacpaas.dvo.ru/api/storage-unit/folder/create", 400, "Bad request", {}, io.BytesIO(body))
        with tempfile.TemporaryDirectory(dir=BASE) as tmp, patch.object(api, "FOLDER_DIAGNOSTIC_DIR", Path(tmp)), patch.object(api, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(api.ClientError, "HTTP 400"):
                api.create_folder("Загрузки", "MCP-demo-test", headers={"X-API-KEY": "test-key"})
            result = json.loads(next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(result["http_status"], 400)
            self.assertEqual(result["response"]["explanationSafe"], "Cannot create folder")
            opener.return_value.open.assert_called_once()

    def test_exception_group_produces_short_cli_error(self):
        def fail(coroutine):
            coroutine.close()
            raise ExceptionGroup("transport", [ExceptionGroup("session", [RuntimeError("create_folder: UPSTREAM_ERROR")])])
        output = io.StringIO()
        with patch("sys.argv", ["folders_demo.py", "--create-only"]), patch.object(api, "auth_headers", return_value={}), patch.object(demo.asyncio, "run", side_effect=fail), contextlib.redirect_stdout(output):
            self.assertEqual(demo.main(), 1)
        self.assertIn("Ошибка: create_folder: UPSTREAM_ERROR", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_legacy_demo_commands_stop_before_login_or_changes(self):
        for args in ([], ["--resume-report", "old.json"], ["--resume-report", "old.json", "--create-only"]):
            with self.subTest(args=args), patch("sys.argv", ["folders_demo.py", *args]), patch.object(api, "auth_headers") as login, patch.object(demo.asyncio, "run") as run, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(demo.main(), 1)
                self.assertIn("trashIR", out.getvalue())
                login.assert_not_called()
                run.assert_not_called()
