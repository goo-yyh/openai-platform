#!/usr/bin/env python3
"""Local gateway lifecycle and isolated Codex entrypoint (Python standard library)."""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import sys

from common import BASE_URL, ROOT, codex_config, codex_env, compose, credentials, load_env, redact, request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "up", "down", "status", "validate", "codex"])
    args, extra = parser.parse_known_args()
    if extra and args.command != "codex":
        parser.error("Extra arguments are only accepted by codex")
    env = load_env()
    if args.command == "init":
        path = ROOT / ".env"
        path.chmod(0o600)
        if not env.get("GATEWAY_TOKEN"):
            token = secrets.token_hex(32)
            lines = path.read_text().splitlines()
            lines = [line for line in lines if not re.match(r"^\s*(?:export\s+)?GATEWAY_TOKEN\s*=", line)]
            lines.append("GATEWAY_TOKEN=" + token)
            path.write_text("\n".join(lines) + "\n")
            env = load_env()
        credentials(env)
        codex_config(env)
        print("Initialized: .env permissions 0600; gateway token available; isolated Codex config ready.")
    elif args.command in ("up", "validate"):
        credentials(env)
        (ROOT / ".env").chmod(0o600)
        compose(env, "config", "--quiet")
        compose(env, "run", "--rm", "--no-deps", "gateway", "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
        if args.command == "validate":
            print("Compose and Caddy configuration: valid")
            return
        # Config is a read-only bind mount; recreate so edits are picked up with admin disabled.
        compose(env, "up", "-d", "--force-recreate", "--wait", "--wait-timeout", "40")
        with request(env, "/healthz", token=False, timeout=5) as r:
            if r.status != 200:
                raise RuntimeError("Gateway health check failed")
        print("Gateway ready: " + BASE_URL + "/v1 (loopback only)")
    elif args.command == "down":
        compose(env, "down")
        print("Gateway stopped")
    elif args.command == "status":
        print(redact(compose(env, "ps").stdout, env).strip())
    elif args.command == "codex":
        credentials(env)
        binary = shutil.which("codex")
        if not binary:
            raise RuntimeError("Codex CLI is not installed")
        with request(env, "/healthz", token=False, timeout=5) as r:
            if r.status != 200:
                raise RuntimeError("Start the gateway first")
        home = codex_config(env)
        if extra[:1] == ["--"]:
            extra = extra[1:]
        os.execvpe(binary, [binary, *extra], codex_env(env, home))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            detail = redact(str(exc), load_env())
        except Exception:
            detail = type(exc).__name__
        print("Error: " + detail, file=sys.stderr)
        sys.exit(1)
