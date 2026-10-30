#!/usr/bin/env python3
"""Deploy the committed gateway and manage a loopback-only OpenSSH tunnel."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile

from common import ROOT, credentials, load_env, redact, save_json


class Server:
    def __init__(self, env=None):
        self.config = env or load_env()
        cfg = self.config
        for name in ("IP", "NAME", "PASSWORD"):
            if not cfg.get(name):
                raise RuntimeError(f"Set {name} in .env first")
        ipaddress.ip_address(cfg["IP"])
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", cfg["NAME"]):
            raise RuntimeError("Invalid SSH username")
        port = int(cfg.get("SSH_PORT") or 22)
        if not 1 <= port <= 65535:
            raise RuntimeError("Invalid SSH port")
        self.directory = ROOT / ".local" / "ssh"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.known = self.directory / "known_hosts"
        self.target = cfg["NAME"] + "@" + cfg["IP"]
        identity = hashlib.sha256((self.target + ":" + str(port)).encode()).hexdigest()[:12]
        self.control = self.directory / ("cm-" + identity)
        askpass = self.directory / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf \'%s\\n\' "$DEPLOY_SSH_PASSWORD"\n')
        askpass.chmod(0o700)
        (ROOT / ".env").chmod(0o600)
        self.environment = dict(os.environ)
        # Only SSH receives the password. The remote JSON payload never contains it.
        self.environment.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY="codex:0", DEPLOY_SSH_PASSWORD=cfg["PASSWORD"])
        for name in ("OPENAI_API_KEY", "GATEWAY_TOKEN", "PASSWORD", "SSH_PASSWORD"):
            self.environment.pop(name, None)
        # Reuse an existing pin if available; otherwise OpenSSH performs TOFU once.
        standard_hosts = Path.home() / ".ssh" / "known_hosts"
        if not self.known.exists() and standard_hosts.is_file():
            host = cfg["IP"] if port == 22 else f"[{cfg['IP']}]:{port}"
            previous = subprocess.run(["ssh-keygen", "-F", host, "-f", str(standard_hosts)], capture_output=True, text=True)
            if previous.returncode == 0:
                self.known.write_text(previous.stdout)
        if self.known.exists():
            self.known.chmod(0o600)
        self.options = ["ssh", "-T", "-p", str(port), "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=" + str(self.known), "-o", "ConnectTimeout=15", "-o", "NumberOfPasswordPrompts=1", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]

    def safe(self, message):
        return redact(message, self.config).replace(self.config["IP"], "[SERVER]")

    def call(self, operation, timeout=900):
        payload = {"operation": operation, "path": self.config.get("SERVER_PATH") or "services/openai-platform"}
        if operation == "deploy":
            credentials(self.config)
            if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
                raise RuntimeError("Commit and push local source changes before deploying")
            payload["expected_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            payload["gateway_env"] = {name: self.config[name] for name in ("OPENAI_API_KEY", "GATEWAY_TOKEN")}
            payload["gateway_env"]["CODEX_MODEL"] = self.config.get("CODEX_MODEL") or "gpt-6.1-sol"
        code = (ROOT / "scripts" / "remote_tasks.py").read_text()
        result = subprocess.run(
            [*self.options, "-S", str(self.control), self.target, "python3 -c " + shlex.quote(code)],
            input=json.dumps(payload), env=self.environment, capture_output=True, text=True, timeout=timeout,
        )
        try:
            message = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError(self.safe(result.stderr or "Remote command did not return JSON")) from None
        if result.returncode or not message.get("ok"):
            raise RuntimeError(self.safe(message.get("error", "Remote command failed")))
        return message["result"]

    def tunnel_running(self):
        if not self.control.exists():
            return False
        result = subprocess.run([*self.options, "-S", str(self.control), "-O", "check", self.target], env=self.environment, capture_output=True, timeout=15)
        return result.returncode == 0

    def tunnel(self):
        if self.tunnel_running():
            return {"tunnel": "already running", "endpoint": "http://127.0.0.1:8787/v1"}
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", 8787)) == 0:
                raise RuntimeError("Local port 8787 is occupied. Stop the local gateway before opening the tunnel")
        with tempfile.TemporaryFile() as errors:
            result = subprocess.run(
                [*self.options, "-M", "-S", str(self.control), "-f", "-N", "-o", "ExitOnForwardFailure=yes", "-L", "127.0.0.1:8787:127.0.0.1:8787", self.target],
                env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, timeout=45,
            )
            if result.returncode:
                errors.seek(0)
                raise RuntimeError(self.safe(errors.read().decode(errors="replace")))
        if not self.tunnel_running():
            raise RuntimeError("SSH did not establish the tunnel")
        return {"tunnel": "running", "endpoint": "http://127.0.0.1:8787/v1"}

    def untunnel(self):
        if self.tunnel_running():
            result = subprocess.run([*self.options, "-S", str(self.control), "-O", "exit", self.target], env=self.environment, capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError(self.safe(result.stderr))
        return {"tunnel": "stopped", "remote_service": "unchanged"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["deploy", "status", "tunnel", "untunnel", "down"])
    args = parser.parse_args()
    server = Server()
    if args.command in ("tunnel", "untunnel"):
        result = getattr(server, args.command)()
    else:
        print("Connecting to server...", flush=True)
        result = server.call(args.command)
        if args.command == "deploy":
            save_json(ROOT / "outputs" / "server-deployment.json", result)
    print(server.safe(json.dumps(result, ensure_ascii=False, indent=2)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Error: " + redact(str(exc), load_env()), file=sys.stderr)
        sys.exit(1)
