"""Interactive or one-question standalone LLM agent over the unchanged MCP API."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import getpass
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_core import Agent, AgentError, Trace
from providers import OpenAIModel, OllamaModel
from smoke import connect
import iacpaas_client as api

BASE = Path(__file__).resolve().parent


def child_environment():
    keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE", "COMSPEC", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env["PYTHONUTF8"] = "1"
    return env


@asynccontextmanager
async def connection(args, env):
    if args.mcp_url:
        parsed = urlsplit(args.mcp_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AgentError("--mcp-url должен быть локальным HTTP-адресом без секретов, например http://127.0.0.1:8765/mcp.")
        async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
            async with streamable_http_client(args.mcp_url, http_client=client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                    yield session
    else:
        async with connect(args.transport, args.mode, env) as session:
            yield session


async def run(args, env, key, trace):
    instructions = (BASE / "agent_instructions.md").read_text(encoding="utf-8")
    async with httpx.AsyncClient(timeout=180, follow_redirects=False, trust_env=args.provider == "openai") as client:
        model = OpenAIModel(client, args.model, key, instructions) if args.provider == "openai" else OllamaModel(client, args.model, instructions)
        async with connection(args, env) as session:
            agent = Agent(session, model, trace)
            await agent.initialize()
            if args.question:
                print(await agent.answer(args.question))
                return
            print("Агент готов. Введите вопрос; /exit — завершить. Для уточнения просто ответьте следующим сообщением.")
            while True:
                question = input("\nВы: ").strip()
                if question == "/exit":
                    return
                if question:
                    print("\nАгент: " + await agent.answer(question))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["openai", "ollama"], required=True)
    parser.add_argument("--model", help="Exact model ID; alternatively OPENAI_MODEL or OLLAMA_MODEL")
    parser.add_argument("--mode", choices=["snapshot", "live"], default="snapshot")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--mcp-url", help="Connect to an already-running local HTTP server; its mode and login apply")
    parser.add_argument("--question", help="One question instead of interactive chat")
    args = parser.parse_args()
    args.model = args.model or os.environ.get("OPENAI_MODEL" if args.provider == "openai" else "OLLAMA_MODEL")
    if not args.model:
        parser.error("Укажите --model с точным ID доступной модели. Для Ollama имена показывает ollama list.")
    key = ""
    trace = None
    try:
        if args.provider == "openai":
            key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not key and sys.stdin.isatty():
                key = getpass.getpass("OpenAI API key (ввод скрыт): ").strip()
            if not key:
                raise AgentError("Для OpenAI API нужен ключ OPENAI_API_KEY или скрытый ввод в терминале.")
        # Model credentials never reach the MCP child process.
        env = child_environment()
        secrets = [key]
        if args.mode == "live" and not args.mcp_url:
            headers = api.auth_headers()
            if "X-API-KEY" in headers:
                env["IACPAAS_API_KEY"] = headers["X-API-KEY"]
            else:
                env["IACPAAS_TOKEN"] = headers["Authorization"].removeprefix("Bearer ")
            secrets.extend(headers.values())
            secrets.extend(env.get(k, "") for k in ("IACPAAS_TOKEN", "IACPAAS_API_KEY"))
        trace = Trace(BASE / "data/traces", secrets)
        trace.add("run", provider=args.provider, model=args.model, transport="external_http" if args.mcp_url else args.transport,
                  mode="server_selected" if args.mcp_url else args.mode, llm_agent=True)
        asyncio.run(run(args, env, key, trace))
        trace.add("run_end", status="completed")
    except (KeyboardInterrupt, EOFError):
        if trace:
            trace.add("run_end", status="cancelled")
        print("\nВыполнение отменено.", file=sys.stderr)
        return 130
    except Exception as exc:
        def leaves(error):
            for child in getattr(error, "exceptions", []):
                yield from leaves(child)
            if not getattr(error, "exceptions", None):
                yield error
        messages = [str(e) for e in leaves(exc) if isinstance(e, (AgentError, api.ClientError))]
        message = "; ".join(dict.fromkeys(messages)) or "Сбой запуска агента/MCP. Проверьте окружение, сервер и подключение."
        if trace:
            trace.add("run_end", status="failed", message=message)
        print("Ошибка: " + message, file=sys.stderr)
        return 1
    finally:
        if trace:
            trace.close()
            print(f"Трасса: {trace.path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
