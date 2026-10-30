"""Shared helpers. Never source .env as shell code or print secret values."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8787"


def load_env() -> dict[str, str]:
    path = ROOT / ".env"
    if path.is_symlink():
        raise RuntimeError(".env must be a regular file, not a symlink")
    if not path.is_file():
        raise RuntimeError("Create .env with OPENAI_API_KEY first")
    result = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise RuntimeError(f"Unsupported .env syntax on line {number}")
        name, value = match.groups()
        words = shlex.split(value, comments=True, posix=True)
        if len(words) > 1:
            raise RuntimeError(f"Quote the value on .env line {number}")
        if name in result:
            raise RuntimeError(f"Duplicate variable {name} in .env")
        result[name] = words[0] if words else ""
    return result


def redact(text: str, env: dict[str, str]) -> str:
    for name in ("OPENAI_API_KEY", "GATEWAY_TOKEN"):
        if env.get(name):
            text = text.replace(env[name], "[REDACTED]")
    return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}", "[REDACTED]", text)


def credentials(env: dict[str, str]) -> None:
    if not re.fullmatch(r"sk-[A-Za-z0-9_-]{16,}", env.get("OPENAI_API_KEY", "")):
        raise RuntimeError("OPENAI_API_KEY is missing or not a supported key format")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", env.get("GATEWAY_TOKEN", "")):
        raise RuntimeError("Run init to generate a gateway token (at least 32 safe characters)")


def process_env(env: dict[str, str]) -> dict[str, str]:
    result = dict(os.environ)
    result.update({k: env[k] for k in ("OPENAI_API_KEY", "GATEWAY_TOKEN") if k in env})
    return result


def compose(env: dict[str, str], *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    # Prevent shell expansion or Compose interpolation of the user's .env file.
    proc = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "-f", str(ROOT / "compose.yaml"), *args],
        cwd=ROOT, env=process_env(env), capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode:
        raise RuntimeError(redact(proc.stderr or proc.stdout, env)[-2500:])
    return proc


def opener():
    # Local tests must actually hit the local gateway, independent of HTTP_PROXY.
    # Do not follow redirects with a bearer credential.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request(env: dict[str, str], path: str, payload=None, token=True, timeout=180):
    headers = {"Accept-Encoding": "identity"}
    if token is not False:
        headers["Authorization"] = "Bearer " + (env["GATEWAY_TOKEN"] if token is True else token)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers)
    try:
        return opener().open(req, timeout=timeout)
    except urllib.error.HTTPError as response:
        return response


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def codex_config(env: dict[str, str]) -> Path:
    home = ROOT / ".local" / "codex"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    model = env.get("CODEX_MODEL") or "gpt-6.1-sol"
    catalog_line = ""
    # Reuse only model metadata from the local official client cache, never auth/identity.
    # This lets an older CLI use newer model names without generic tool defaults.
    source_cache = Path.home() / ".codex" / "models_cache.json"
    if source_cache.is_file():
        cache = json.loads(source_cache.read_text())
        selected = [item for item in cache.get("models", []) if item.get("slug") == model]
        if selected:
            catalog = home / "models.json"
            save_json(catalog, {"models": selected})
            catalog_line = f"model_catalog_json = {json.dumps(str(catalog))}\n"
    # JSON quoted strings are compatible with these TOML basic strings.
    content = f'''model = {json.dumps(model)}
model_provider = "local_gateway"
{catalog_line}model_reasoning_effort = "low"
web_search = "disabled"
cli_auth_credentials_store = "file"

[model_providers.local_gateway]
name = "Local OpenAI Gateway"
base_url = "{BASE_URL}/v1"
env_key = "CODEX_GATEWAY_TOKEN"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 180000

[shell_environment_policy]
exclude = ["OPENAI_API_KEY", "GATEWAY_TOKEN", "CODEX_GATEWAY_TOKEN"]
'''
    (home / "config.toml").write_text(content)
    return home


def codex_env(env: dict[str, str], home: Path) -> dict[str, str]:
    result = dict(os.environ)
    for name in list(result):
        if name.startswith(("OPENAI_", "CODEX_")) or name == "GATEWAY_TOKEN":
            result.pop(name)
    result["CODEX_HOME"] = str(home)
    result["CODEX_GATEWAY_TOKEN"] = env["GATEWAY_TOKEN"]
    # Rust HTTP clients can pick up macOS system proxies. Explicitly bypass loopback.
    bypass = result.get("NO_PROXY") or result.get("no_proxy") or ""
    bypass = ",".join(part for part in [bypass, "127.0.0.1", "localhost", "::1"] if part)
    result["NO_PROXY"] = bypass
    result["no_proxy"] = bypass
    return result
