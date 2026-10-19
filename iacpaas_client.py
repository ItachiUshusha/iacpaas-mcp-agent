"""Stage 1: verify IACPaaS HTTP API and export a bounded universal graph."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import sys
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE_URL = "https://iacpaas.dvo.ru"
TIMEOUT = 40
MAX_BYTES = 2_000_000


class ClientError(Exception):
    """An actionable error whose message contains no server body or credentials."""


class ExportFormatError(ClientError):
    def __init__(self, response):
        super().__init__("Не удалось распознать граф в ответе экспорта.")
        self.response = response


def normalize_export(response):
    """Accept a graph, a file envelope, or a result envelope; validate the graph."""
    candidate = response
    if candidate.get("success") is False or candidate.get("error") or candidate.get("platformUnavailable") is True:
        raise ClientError("IACPaaS сообщил об ошибке экспорта. Проверьте путь ресурса и доступ к нему.")
    if "json_type" not in candidate:
        if isinstance(candidate.get("filename"), str) and "data" in candidate:
            # Observed IACPaaS export: filename + resource code + JSON in data.
            # The outer code is a resource identifier, not an HTTP status code.
            candidate = candidate["data"]
        elif "result" in candidate:
            candidate = candidate["result"]
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate.lstrip("\ufeff"))
            except ValueError:
                raise ExportFormatError(response) from None
    if not isinstance(candidate, dict):
        raise ExportFormatError(response)
    if candidate.get("json_type") != "universal" or candidate.get("type") != "КОРЕНЬ":
        raise ExportFormatError(response)
    return candidate


def redact_export(value):
    """Redact credential fields if an unexpected export response contains them."""
    sensitive = {"password", "accesstoken", "refreshtoken", "authorization", "cookie", "setcookie", "xapikey", "apikey", "token"}
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower().replace("-", "").replace("_", "") in sensitive else redact_export(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_export(item) for item in value]
    if isinstance(value, str):
        # Export envelopes may carry JSON as a string in data or result.
        try:
            embedded = json.loads(value.lstrip("\ufeff"))
        except ValueError:
            return value
        if isinstance(embedded, (dict, list)):
            return json.dumps(redact_export(embedded), ensure_ascii=False)
    return value


def save_export_diagnostic(response, directory):
    """Store only the export response; never sign-in data or request headers."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"export-diagnostic-{uuid4().hex[:12]}.json"
    fields = {key[:80]: type(value).__name__ for key, value in list(response.items())[:32]}
    diagnostic = {"endpoint": "/api/data/export/user-item", "field_types": fields, "response": redact_export(response)}
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(diagnostic, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return destination, fields


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("Сервер перенаправил запрос. Адрес API нужно проверить.")


def request_json(endpoint, *, headers=None, payload=None, query=None):
    url = BASE_URL + endpoint
    if query:
        url += "?" + urlencode(query)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json"}
    request_headers.update(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers)
    try:
        with build_opener(NoRedirect()).open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        messages = {
            400: "Проверьте путь и параметры запроса.",
            401: "Проверьте учётные данные или срок действия токена.",
            403: "Нет разрешения на эту операцию или ресурс.",
            404: "Ресурс либо адрес API не найден.",
            429: "Слишком много запросов. Повторите позже.",
        }
        raise ClientError(f"HTTP {exc.code}. " + messages.get(exc.code, "Ошибка сервера IACPaaS.")) from None
    except (URLError, TimeoutError, socket.timeout):
        raise ClientError("Не удалось подключиться к IACPaaS. Проверьте сеть и разрешение на сетевой доступ.") from None
    if len(raw) > MAX_BYTES:
        raise ClientError("Результат превышает 2 МБ. Уменьшите глубину экспорта.")
    try:
        result = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ClientError("Сервер вернул не JSON. Проверьте доступность API.") from None
    if not isinstance(result, dict):
        raise ClientError("Сервер вернул неожиданный формат JSON.")
    if result.get("success") is False or result.get("error"):
        raise ClientError("IACPaaS сообщил об ошибке. Проверьте права доступа и путь ресурса в «Моём Фонде».")
    return result


def auth_headers(*, interactive=True):
    api_key = os.environ.get("IACPAAS_API_KEY", "").strip()
    token = os.environ.get("IACPAAS_TOKEN", "").strip()
    if api_key and token:
        raise ClientError("Задайте только один способ доступа: IACPAAS_API_KEY или IACPAAS_TOKEN.")
    if api_key:
        return {"X-API-KEY": api_key}
    if token:
        return {"Authorization": f"Bearer {token}"}
    if not interactive or not sys.stdin.isatty():
        raise ClientError("Запустите export в обычном терминале для входа либо задайте IACPAAS_API_KEY/IACPAAS_TOKEN.")
    username = input("Логин IACPaaS: ").strip()
    if not username:
        raise ClientError("Логин не указан.")
    password = getpass.getpass("Пароль IACPaaS (ввод скрыт): ")
    if not password:
        raise ClientError("Пароль не указан.")
    response = request_json("/api/signin", payload={"username": username, "password": password})
    token = response.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise ClientError("Ответ входа не содержит accessToken. Требуется проверить контракт API.")
    return {"Authorization": f"Bearer {token}"}


def check_api():
    spec = request_json("/v3/api-docs")
    paths = spec.get("paths", {})
    required = {"/api/signin": "post", "/api/data/export/user-item": "get"}
    if not isinstance(paths, dict) or any(method not in paths.get(path, {}) for path, method in required.items()):
        raise ClientError("OpenAPI больше не содержит ожидаемых операций. Нужно обновить клиент.")
    print("Соединение с IACPaaS: OK")
    print("Вход и экспорт присутствуют в OpenAPI: OK")
    print("Доступ к пользовательскому графу ещё не проверен.")
    return spec


def export_resource(path, *, depth=0, start=None, headers=None):
    path = path.strip()
    if not path or "://" in path:
        raise ClientError("Нужен путь ресурса внутри «Моего Фонда», а не ссылка на сайт.")
    if "/Мой Фонд/" in path:
        raise ClientError("Укажите относительный путь без e-mail и префикса «Мой Фонд».")
    if not isinstance(depth, int) or isinstance(depth, bool) or not 0 <= depth <= 10:
        raise ClientError("Глубина должна быть целым числом от 0 до 10.")
    query = {"path": path, "json-type": "universal", "export-depth": depth, "compress": "false", "no-blob-data": "true"}
    if start:
        query["start-target-concept-path"] = start
    result = request_json("/api/data/export/user-item", headers=headers, query=query)
    return normalize_export(result)


def save_resource(resource, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(resource, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        raise ClientError("Файл уже существует. Задайте другое имя через --output.") from None
    print(f"Экспорт получен. Файл: {destination.resolve()}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Первый этап: подключение к IACPaaS и чтение ресурса.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Проверить публичную OpenAPI; вход не нужен.")
    export = sub.add_parser("export", help="Войти и прочитать ресурс из своего фонда.")
    export.add_argument("--path", required=True, help="Относительный путь внутри «Моего Фонда».")
    export.add_argument("--depth", type=int, default=0, help="Глубина 0–10; по умолчанию только корень.")
    export.add_argument("--start", help="Необязательный путь к начальной вершине.")
    export.add_argument("--output", type=Path, default=Path(__file__).parent / "data" / "resource.json")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            check_api()
        else:
            if not 0 <= args.depth <= 10:
                raise ClientError("Глубина должна быть от 0 до 10.")
            headers = auth_headers()
            resource = export_resource(args.path, depth=args.depth, start=args.start, headers=headers)
            save_resource(resource, args.output)
    except ExportFormatError as exc:
        try:
            destination, fields = save_export_diagnostic(exc.response, Path(__file__).parent / "data")
        except OSError:
            print("Ошибка: формат экспорта не распознан; не удалось сохранить диагностику.", file=sys.stderr)
            return 1
        print("Ошибка: формат экспорта не распознан.", file=sys.stderr)
        print("Поля ответа и их типы: " + json.dumps(fields, ensure_ascii=False), file=sys.stderr)
        print(f"Диагностика экспорта сохранена: {destination.resolve()}", file=sys.stderr)
        print("Запрос входа и заголовки авторизации в этот файл не записываются.", file=sys.stderr)
        return 1
    except (ClientError, OSError) as exc:
        # OSError messages can contain local paths but never request headers/body.
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nВыполнение отменено.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
