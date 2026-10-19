"""Read-only readiness check; never prints secrets or submits model requests."""
import importlib.metadata
import json
import os
from pathlib import Path
import shutil

import httpx


def main():
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
    main()
