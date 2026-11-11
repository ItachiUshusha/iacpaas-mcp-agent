"""Explicit live check of folder creation through one MCP transport."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4

import jsonschema

from agent import child_environment
from domain import BASE
import iacpaas_client as api
from smoke import connect


def error_leaves(error):
    for nested in getattr(error, "exceptions", ()):
        yield from error_leaves(nested)
    if not getattr(error, "exceptions", ()):
        yield error


async def verify(headers, parent, transport):
    env = child_environment()
    if "X-API-KEY" in headers:
        env["IACPAAS_API_KEY"] = headers["X-API-KEY"]
    else:
        env["IACPAAS_TOKEN"] = headers["Authorization"].removeprefix("Bearer ")
    report = {"mode": "live", "llm_agent": False,
              "verified_at_utc": datetime.now(timezone.utc).isoformat(), "runs": []}
    directory = BASE / "data/folders-demo"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (uuid4().hex + ".json")
    name = "MCP-demo-" + uuid4().hex
    path = f"{parent}/{name}" if parent else name
    run = {"transport": transport, "path": path, "calls": []}
    report["runs"].append(run)
    print(f"Проверка {transport}: {path}", flush=True)
    try:
        async with connect(transport, "live", env) as session:
            await session.initialize()
            schemas = {t.name: t.outputSchema for t in (await session.list_tools()).tools}
            arguments = {"parent": parent, "name": name, "description": "Temporary MCP creation test"}
            response = await session.call_tool("create_folder", arguments)
            result = response.structuredContent
            jsonschema.validate(result, schemas["create_folder"])
            run["calls"].append({"tool": "create_folder", "arguments": arguments, "result": result})
            if response.isError or not result["ok"]:
                error = result.get("error") or {}
                raise RuntimeError(f"create_folder: {error.get('code')} — {error.get('message')}. Проверьте {path} в фонде.")
            if result["data"]["path"] != path or result["data"]["source"] != "live_api" or result["data"]["operation"] != "created":
                raise RuntimeError("Ответ операции не соответствует проверяемой папке.")
        print(f"OK: {transport}, folder created. Удалите тестовую папку в интерфейсе IACPaaS.", flush=True)
    finally:
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Отчёт: " + str(destination), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", default="Загрузки")
    parser.add_argument("--create-only", action="store_true", help="Create one test folder; remove it manually in IACPaaS")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--resume-report", help=argparse.SUPPRESS)  # Old commands fail clearly before login.
    args = parser.parse_args()
    if args.resume_report is not None or not args.create_only:
        print("Ошибка: старый сценарий trashIR отключён. Для проверки создания используйте --create-only.")
        return 1
    try:
        api.folder_path(args.parent, allow_root=True)
        headers = api.auth_headers()
        asyncio.run(verify(headers, args.parent, args.transport))
    except (api.ClientError, RuntimeError, ValueError, OSError) as exc:
        print("Ошибка: " + str(exc))
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Отменено. Если папка уже создана, проверьте её в IACPaaS.")
        return 130
    except BaseExceptionGroup as exc:
        leaves = list(error_leaves(exc))
        if any(isinstance(e, (EOFError, KeyboardInterrupt)) for e in leaves):
            print("Отменено. Проверьте уже созданную папку в IACPaaS.")
            return 130
        messages = [str(e) for e in leaves if isinstance(e, (api.ClientError, RuntimeError))]
        print("Ошибка: " + ("; ".join(dict.fromkeys(messages)) or "Сбой проверки MCP. Проверьте сохранённый отчёт и папку в IACPaaS."))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
