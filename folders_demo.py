"""Live MCP folder lifecycle check; only delete folders created in this run."""
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


async def verify(headers, parent):
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
    try:
        for transport in ("stdio", "http"):
            name = "MCP-demo-" + uuid4().hex
            path = f"{parent}/{name}" if parent else name
            run = {"transport": transport, "path": path, "calls": []}
            report["runs"].append(run)
            print(f"Проверка {transport}: {path}", flush=True)
            async with connect(transport, "live", env) as session:
                await session.initialize()
                schemas = {t.name: t.outputSchema for t in (await session.list_tools()).tools}
                for tool, arguments in [
                    ("create_folder", {"parent": parent, "name": name, "description": "Temporary MCP lifecycle test"}),
                    ("delete_folder", {"path": path, "confirm_path": path}),
                ]:
                    response = await session.call_tool(tool, arguments)
                    result = response.structuredContent
                    jsonschema.validate(result, schemas[tool])
                    run["calls"].append({"tool": tool, "arguments": arguments, "result": result})
                    if response.isError or not result["ok"]:
                        error = result.get("error") or {}
                        raise RuntimeError(f"{tool}: {error.get('code')} — {error.get('message')}. Проверьте {path} в фонде и корзине.")
                    if result["data"]["path"] != path or result["data"]["source"] != "live_api":
                        raise RuntimeError("Ответ операции не соответствует проверяемой папке.")
            print(f"OK: {transport}, folder create + trash", flush=True)
    finally:
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Отчёт: " + str(destination), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", default="Загрузки")
    args = parser.parse_args()
    try:
        api.folder_path(args.parent, allow_root=True)
        headers = api.auth_headers()
        asyncio.run(verify(headers, args.parent))
    except (api.ClientError, RuntimeError) as exc:
        print("Ошибка: " + str(exc))
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Отменено. Если папка уже создана, проверьте её в IACPaaS.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
