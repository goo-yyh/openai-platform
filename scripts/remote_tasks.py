#!/usr/bin/env python3
"""SSH bootstrap worker. Reads a JSON payload from stdin; prints redacted metadata."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

REPOSITORY = "https://github.com/goo-yyh/openai-platform.git"


def run(args, cwd=None, timeout=120):
    result = subprocess.run(args, cwd=cwd, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): " + (result.stderr or result.stdout)[-1800:])
    return result.stdout.strip()


def runtime(project):
    template = '{"health":{{json .State.Health.Status}},"running":{{json .State.Running}},"ports":{{json .HostConfig.PortBindings}},"log_driver":{{json .HostConfig.LogConfig.Type}},"read_only":{{json .HostConfig.ReadonlyRootfs}},"restart_policy":{{json .HostConfig.RestartPolicy.Name}},"image_id":{{json .Image}}}'
    # Select only runtime metadata; never read/print the container's environment.
    info = json.loads(run(["docker", "inspect", "--format", template, "openai-platform-gateway-1"]))
    autosave = run(["docker", "exec", "openai-platform-gateway-1", "sh", "-c", "test ! -e /config/caddy/autosave.json && test ! -e /data/caddy/autosave.json && echo absent"])
    return {
        "commit": run(["git", "rev-parse", "HEAD"], project),
        **info,
        "caddy_version": run(["docker", "exec", "openai-platform-gateway-1", "caddy", "version"]).split()[0],
        "autosaved_config": autosave != "absent",
    }


def main(payload):
    project = Path(payload.get("path") or "services/openai-platform").expanduser()
    if not project.is_absolute():
        project = Path.home() / project
    if project.is_symlink():
        raise RuntimeError("Deployment path must not be a symlink")
    operation = payload["operation"]
    if operation == "status":
        return runtime(project)
    if operation == "down":
        run(["python3", "scripts/gateway.py", "down"], project)
        return {"stopped": True}
    if operation != "deploy":
        raise RuntimeError("Unsupported remote operation")

    for command in (["docker", "version", "--format", "{{.Server.Version}}"], ["docker", "compose", "version"], ["git", "--version"]):
        run(command)
    if not project.exists():
        # Refuse a conflicting listener/container before creating a deployment.
        import socket
        with socket.socket() as sock:
            sock.settimeout(2)
            if sock.connect_ex(("127.0.0.1", 8787)) == 0:
                raise RuntimeError("Remote port 8787 is already in use")
        existing = run(["docker", "ps", "-a", "--filter", "name=^/openai-platform-gateway-1$", "--format", "{{.Names}}"])
        if existing:
            raise RuntimeError("A gateway container already exists without the expected checkout")
        project.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--branch", "main", "--single-branch", REPOSITORY, str(project)], timeout=180)
    else:
        if not (project / ".git").is_dir():
            raise RuntimeError("Existing deployment directory is not a Git checkout")
        if run(["git", "remote", "get-url", "origin"], project) != REPOSITORY:
            raise RuntimeError("Existing checkout has an unexpected origin")
        if run(["git", "status", "--porcelain"], project):
            raise RuntimeError("Remote checkout has uncommitted files; refusing to overwrite")
        if run(["git", "branch", "--show-current"], project) != "main":
            raise RuntimeError("Remote checkout is not on main")
        run(["git", "pull", "--ff-only", "origin", "main"], project, timeout=180)
    commit = run(["git", "rev-parse", "HEAD"], project)
    if commit != payload["expected_commit"]:
        raise RuntimeError("Remote main differs from the expected local commit")

    values = payload["gateway_env"]
    if set(values) != {"OPENAI_API_KEY", "GATEWAY_TOKEN", "CODEX_MODEL"}:
        raise RuntimeError("Unexpected remote environment variable")
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]+", value):
            raise RuntimeError(f"Invalid value format for {name}")
    envfile = project / ".env"
    if envfile.is_symlink():
        raise RuntimeError("Remote .env must not be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".env.deploy-", dir=project)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("".join(name + "=" + value + "\n" for name, value in values.items()))
        os.chmod(temporary, 0o600)
        os.replace(temporary, envfile)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    # Pull first so configuration validation has no image download timeout.
    image = next(line.split("image:", 1)[1].strip() for line in (project / "compose.yaml").read_text().splitlines() if line.strip().startswith("image:"))
    run(["docker", "pull", image], timeout=600)
    run(["python3", "scripts/gateway.py", "up"], project, timeout=180)
    result = runtime(project)
    result["remote_env_variables"] = list(values)
    result["remote_env_mode"] = oct(envfile.stat().st_mode & 0o777)
    result["project_path_setting"] = payload.get("path") or "services/openai-platform"
    return result


if __name__ == "__main__":
    data = json.load(sys.stdin)
    try:
        print(json.dumps({"ok": True, "result": main(data)}))
    except Exception as exc:
        message = str(exc)
        for value in data.get("gateway_env", {}).values():
            if value:
                message = message.replace(value, "[REDACTED]")
        print(json.dumps({"ok": False, "error": message}))
        sys.exit(1)
