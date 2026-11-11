"""Check local readiness or test OpenRouter independently of MCP."""
import argparse
import asyncio
import getpass
import importlib.metadata
import json
import os
from pathlib import Path
import shutil

import httpx

from providers import save_model_failure


async def check_openrouter(key):
    base_url = "https://openrouter.ai/api/v1"
    headers = {"Authorization": f"Bearer {key}"}
    payload = {
        "model": "openrouter/free",
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 128,
        "stream": False,
    }
    success = True
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        for method, endpoint, body in (("GET", "/key", None), ("POST", "/chat/completions", payload)):
            url = base_url + endpoint
            try:
                async with client.stream(method, url, headers=headers, json=body) as response:
                    diagnostic = ""
                    if not response.is_success:
                        success = False
                        diagnostic = await save_model_failure(response, url, body or {}, headers)
                    print(f"{method} {endpoint}: HTTP {response.status_code}.{diagnostic}")
            except httpx.HTTPError as exc:
                success = False
                print(f"{method} {endpoint}: сбой соединения ({type(exc).__name__}).")
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openrouter", action="store_true", help="Check the API key and make one free request without MCP tools")
    args = parser.parse_args()
    if args.openrouter:
        key = getpass.getpass("Ключ OpenRouter (ввод скрыт): ").strip()
        if not key:
            parser.error("Ключ не введён.")
        return asyncio.run(check_openrouter(key))
    result = {
        "python_environment": True,
        "mcp_version": importlib.metadata.version("mcp"),
        "vscode_command_found": bool(shutil.which("code")),
        "openai_key_in_process_environment": bool(os.environ.get("OPENAI_API_KEY")),
        "openai_model_configured": bool(os.environ.get("OPENAI_MODEL")),
        "ollama_command_found": bool(shutil.which("ollama")),
        "ollama_service_reachable": False,
        "ollama_models": [],
    }
    extensions = Path.home() / ".vscode/extensions"
    result["copilot_extension_directory_found"] = bool(list(extensions.glob("github.copilot*"))) if extensions.exists() else False
    code_command = shutil.which("code")
    result["copilot_bundled_with_vscode"] = False
    if code_command:
        install = Path(code_command).parent.parent
        candidates = [install / "resources/app/extensions/copilot/package.json"]
        candidates.extend(install.glob("*/resources/app/extensions/copilot/package.json"))
        result["copilot_bundled_with_vscode"] = any(p.is_file() for p in candidates)
    try:
        response = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2, trust_env=False)
        response.raise_for_status()
        result["ollama_models"] = [row["name"] for row in response.json().get("models", [])]
        result["ollama_service_reachable"] = True
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("This checks local availability only, not account entitlement or successful LLM tool calling.")


if __name__ == "__main__":
    raise SystemExit(main())
