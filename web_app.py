"""Local browser chat using the same agent and MCP API as the CLI/IDE."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import getpass
import json
import os
from pathlib import Path
import secrets

import httpx
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
import uvicorn

from agent import connection, child_environment
from agent_core import Agent, AgentError, Trace
from providers import CompatibleModel, OllamaModel, OpenAIModel
import iacpaas_client as api

BASE = Path(__file__).resolve().parent


class BrowserTrace(Trace):
    def __init__(self, directory, credentials):
        super().__init__(directory, credentials)
        self.events = []

    def add(self, event, **data):
        super().add(event, **data)
        self.events.append(self.clean({"event": event, **data}))


def create_app(args, env, key, model_factory=None, connector=connection):
    chats = {}
    lock = asyncio.Lock()
    token = secrets.token_urlsafe(32)
    credentials = [key, env.get("IACPAAS_TOKEN", ""), env.get("IACPAAS_API_KEY", "")]
    instructions = (BASE / "agent_instructions.md").read_text(encoding="utf-8")
    ready = args.provider == "ollama" or bool(key)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=180, follow_redirects=False, trust_env=args.provider != "ollama") as client:
            async with connector(args, env) as session:
                app.state.session, app.state.client = session, client
                try:
                    yield
                finally:
                    for agent in chats.values():
                        agent.trace.close()

    async def index(request):
        return FileResponse(BASE / "web/index.html", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"})

    async def asset(request):
        name = request.path_params["name"]
        if name not in ("app.js", "style.css", "markdown.js"):
            return JSONResponse({"error": "Файл не найден"}, status_code=404)
        return FileResponse(BASE / "web" / name, headers={"Cache-Control": "no-store"})

    async def status(request):
        return JSONResponse({"provider": args.provider, "model": args.model,
            "source": "server_selected" if args.mcp_url else args.mode,
            "model_configured": ready, "token": token}, headers={"Cache-Control": "no-store"})

    async def chat(request):
        if request.headers.get("x-chat-token") != token:
            return JSONResponse({"error": "Обновите страницу и повторите запрос."}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"error": "Недопустимый источник запроса."}, status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"error": "Ожидается JSON."}, status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 40_000:
                return JSONResponse({"error": "Слишком большой запрос."}, status_code=413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError
            sid = data.get("session", "")
            question = data.get("question", "")
            if not isinstance(sid, str) or not isinstance(question, str) or not 1 <= len(question.strip()) <= 8000:
                raise ValueError
            if len(sid) > 64:
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            return JSONResponse({"error": "Введите вопрос от 1 до 8000 символов."}, status_code=400)
        async with lock:
            if data.get("reset") is True:
                old = chats.pop(sid, None)
                if old:
                    old.trace.close()
                return JSONResponse({"reset": True})
            if not ready:
                return JSONResponse({"error": "API-ключ модели не задан. Перезапустите с --ask-key и введите ключ в терминале."}, status_code=503)
            if sid and sid not in chats:
                return JSONResponse({"error": "Диалог завершён. Нажмите «Новый диалог»."}, status_code=404)
            if not sid:
                if len(chats) >= 8:
                    return JSONResponse({"error": "Открыто 8 диалогов. Закройте лишние кнопкой «Новый диалог»."}, status_code=429)
                sid = secrets.token_urlsafe(24)
                trace = BrowserTrace(BASE / "data/traces", credentials)
                if model_factory:
                    model = model_factory(request.app.state.client)
                elif args.provider == "compatible":
                    model = CompatibleModel(request.app.state.client, args.model, key, instructions, args.base_url)
                elif args.provider == "ollama":
                    model = OllamaModel(request.app.state.client, args.model, instructions)
                else:
                    model = OpenAIModel(request.app.state.client, args.model, key, instructions)
                chats[sid] = Agent(request.app.state.session, model, trace)
            agent = chats[sid]
            offset = len(agent.trace.events)
            try:
                if not agent.tools:
                    await agent.initialize()
                answer = await agent.answer(question.strip())
                return JSONResponse({"session": sid, "answer": answer,
                    "events": agent.trace.events[offset:]}, headers={"Cache-Control": "no-store"})
            except Exception as exc:
                # Incomplete provider histories must not be reused after a failed tool round.
                message = str(exc) if isinstance(exc, AgentError) else "Сбой агента или MCP. Проверьте сервер и подключение."
                message = agent.trace.clean(message)
                events = agent.trace.events[offset:]
                agent.trace.add("run_end", status="failed", message=message)
                agent.trace.close()
                chats.pop(sid, None)
                return JSONResponse({"error": message, "reset": True, "events": events}, status_code=502)

    app = Starlette(lifespan=lifespan, routes=[Route("/", index), Route("/assets/{name}", asset),
        Route("/api/status", status), Route("/api/chat", chat, methods=["POST"])])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    return app


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", choices=["compatible", "ollama", "openai"], default="compatible")
    p.add_argument("--model", default=os.environ.get("AI_MODEL", "openrouter/free"))
    p.add_argument("--base-url", default=os.environ.get("AI_BASE_URL", "https://openrouter.ai/api/v1"))
    p.add_argument("--mode", choices=["snapshot", "live"], default="snapshot")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--mcp-url")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--ask-key", action="store_true")
    args = p.parse_args()
    if not 1 <= args.port <= 65535:
        p.error("port must be 1..65535")
    key = os.environ.get("OPENAI_API_KEY" if args.provider == "openai" else "AI_API_KEY", "").strip()
    if args.ask_key and args.provider != "ollama" and not key:
        key = getpass.getpass("API key модели (ввод скрыт): ").strip()
    if args.provider == "compatible":
        CompatibleModel(None, args.model, key, "", args.base_url)  # Validate URL before startup.
    env = child_environment()
    if args.mode == "live" and not args.mcp_url:
        headers = api.auth_headers()
        if "X-API-KEY" in headers:
            env["IACPAAS_API_KEY"] = headers["X-API-KEY"]
        else:
            env["IACPAAS_TOKEN"] = headers["Authorization"].removeprefix("Bearer ")
    print(f"Откройте http://127.0.0.1:{args.port} — остановка Ctrl+C", flush=True)
    uvicorn.run(create_app(args, env, key), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
