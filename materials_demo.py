"""Verify registered material resources through actual MCP transports."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

import jsonschema

from agent import child_environment
from domain import BASE
import iacpaas_client as api
from smoke import connect
from study_materials import TARGETS


async def verify(headers, directory):
    env = child_environment()
    if "X-API-KEY" in headers:
        env["IACPAAS_API_KEY"] = headers["X-API-KEY"]
    else:
        env["IACPAAS_TOKEN"] = headers["Authorization"].removeprefix("Bearer ")
    catalog = json.loads((BASE / "catalog.json").read_text(encoding="utf-8"))
    expected = {title for _, title, _ in TARGETS}
    registered = [row for row in catalog if row["title"] in expected]
    if len(registered) != len(TARGETS):
        raise RuntimeError("Сначала выполните сбор данных study_materials.py: не все ресурсы подтверждены и зарегистрированы.")
    report = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "mode": "live", "llm_agent": False, "runs": []}
    for transport in ("stdio", "http"):
        calls = []
        async with connect(transport, "live", env) as session:
            await session.initialize()
            schemas = {t.name: t.outputSchema for t in (await session.list_tools()).tools}
            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                data = response.structuredContent
                if not data:
                    raise RuntimeError("MCP не вернул структурированный результат.")
                jsonschema.validate(data, schemas[name])
                calls.append({"tool": name, "arguments": arguments, "isError": response.isError, "result": data})
                if response.isError or not data["ok"]:
                    error = data.get("error") or {}
                    raise RuntimeError(f"{name}: {error.get('code')} — {error.get('message')}")
                return data["data"]
            listing = await call("list_inforesources", {"query": "материал", "limit": 50})
            if not any(row["title"] == "База свойств материалов" for row in listing["resources"]):
                raise RuntimeError("База материалов не найдена через MCP.")
            for row in registered:
                root = await call("get_inforesource", {"path": row["path"]})
                if root["resource"]["resource_id"] != row["resource_id"] or root["source"] != "live_api":
                    raise RuntimeError("MCP прочитал другой ресурс или не живой источник.")
            data_row = next(row for row in registered if row["title"] == "База свойств материалов")
            await call("get_subgraph", {"path": data_row["path"], "depth": 1, "max_nodes": 200})
            await call("count_graph", {"path": data_row["path"], "depth": 1, "max_nodes": 200})
        report["runs"].append({"transport": transport, "calls": calls})
        print(f"OK: {transport}, materials, {len(calls)} tool calls", flush=True)
    destination = directory / "mcp-materials-verification.json"
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print("Отчёт MCP: " + str(destination), flush=True)
    return report


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        headers = api.auth_headers()
        directory = BASE / "data/materials-demo" / uuid4().hex[:12]
        directory.mkdir(parents=True)
        asyncio.run(verify(headers, directory))
        return 0
    except (api.ClientError, RuntimeError, OSError) as exc:
        print("Ошибка: " + str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
