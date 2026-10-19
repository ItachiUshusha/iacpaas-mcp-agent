"""MCP stdio and Streamable HTTP share the same domain service."""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Annotated, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, ValidationError

from domain import (CountReport, Depth, Error, Fault, Graph, Listing, NodeLimit,
                    ResourcePath, Result, Selection, Service, StartPath)
import iacpaas_client as api


def respond(operation: Callable) -> CallToolResult:
    try:
        result = Result(ok=True, data=operation())
    except Fault as exc:
        result = Result(ok=False, error=Error(code=exc.code, message=exc.message, hint=exc.hint))
    except ValidationError:
        result = Result(ok=False, error=Error(code="INVALID_ARGUMENT", message="Некорректные параметры.", hint="Сверьте аргументы с inputSchema инструмента."))
    except Exception:
        # Do not forward raw exception messages, headers, local paths or credentials.
        result = Result(ok=False, error=Error(code="INTERNAL_ERROR", message="Внутренняя ошибка адаптера.", hint="Проверьте конфигурацию и локальные тесты сервера."))
    data = result.model_dump(mode="json")
    return CallToolResult(isError=not result.ok, structuredContent=data,
                          content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))])


def create_server(service: Service, port=8765) -> FastMCP:
    server = FastMCP("IACPaaS учебный сервер", host="127.0.0.1", port=port,
                     stateless_http=True, json_response=True, log_level="WARNING",
                     transport_security=TransportSecuritySettings(
                         enable_dns_rebinding_protection=True,
                         allowed_hosts=["127.0.0.1:*", "localhost:*"],
                         allowed_origins=["http://127.0.0.1:*", "http://localhost:*"]))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

    @server.tool(annotations=read)
    async def list_inforesources(
        query: Annotated[str, Field(max_length=200, strict=True)] = "",
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 20,
    ) -> Annotated[CallToolResult, Result[Listing]]:
        """Search configured resources by title/path. This is a local catalog, not a live listing of the user's fund; access is checked only when reading."""
        return respond(lambda: service.list_resources(query, limit))

    @server.tool(annotations=read)
    async def get_inforesource(path: ResourcePath) -> Annotated[CallToolResult, Result[Graph]]:
        """Read metadata and root of a configured resource (depth=0). Use exact path returned by list_inforesources. IDs are strings."""
        return await asyncio.to_thread(respond, lambda: service.read(Selection(path=path)))

    @server.tool(annotations=read)
    async def get_subgraph(path: ResourcePath, depth: Depth = 1, start: StartPath = "",
                           max_nodes: NodeLimit = 100) -> Annotated[CallToolResult, Result[Graph]]:
        """Read a bounded universal export, depth 0..3. Empty start means resource root; otherwise use a known IACPaaS concept path. Ancestors may be included. Snapshot mode has only depth=0."""
        return await asyncio.to_thread(respond, lambda: service.read(Selection(path=path, depth=depth, start=start, max_nodes=max_nodes)))

    @server.tool(annotations=read)
    async def count_graph(path: ResourcePath, depth: Depth = 0, start: StartPath = "",
                          max_nodes: NodeLimit = 100) -> Annotated[CallToolResult, Result[CountReport]]:
        """Count occurrences, defined IDs and links in a bounded export, including ancestors. Never a full graph count. Link targets are not resolved; depth=0 normally yields one root and zero edges."""
        return await asyncio.to_thread(respond, lambda: service.count(Selection(path=path, depth=depth, start=start, max_nodes=max_nodes)))

    return server


def main():
    parser = argparse.ArgumentParser(description="IACPaaS read-only MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--mode", choices=["live", "snapshot"], default="live")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--login", action="store_true", help="Interactive login before HTTP startup; credentials remain in memory")
    parser.add_argument("--schemas", action="store_true", help="Print tool schemas without starting server")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    if args.login and (args.transport != "http" or args.mode != "live" or args.schemas):
        parser.error("--login requires --transport http --mode live")
    try:
        headers = api.auth_headers() if args.login else None
    except api.ClientError as exc:
        parser.exit(1, str(exc) + "\n")
    server = create_server(Service(args.mode, headers), args.port)
    if args.schemas:
        schemas = asyncio.run(server.list_tools())
        print(json.dumps([t.model_dump(mode="json") for t in schemas], ensure_ascii=False, indent=2))
    else:
        server.run(transport="stdio" if args.transport == "stdio" else "streamable-http")


if __name__ == "__main__":
    main()
