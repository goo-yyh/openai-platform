#!/usr/bin/env python3
"""Add one gateway virtual host to an existing system Caddy service on Linux."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if result.returncode:
        # Existing server configuration may contain unrelated credentials. Do not dump it.
        raise RuntimeError(f"{args[0]} exited with status {result.returncode}")
    return result.stdout


def hosts(value):
    found = set()
    if isinstance(value, dict):
        if isinstance(value.get("host"), list):
            found.update(item for item in value["host"] if isinstance(item, str))
        for item in value.values():
            found.update(hosts(item))
    elif isinstance(value, list):
        for item in value:
            found.update(hosts(item))
    return found


def write_candidate(path, content, stat):
    fd, name = tempfile.mkstemp(prefix=".openai-platform-", dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    os.chmod(name, stat.st_mode & 0o777)
    os.chown(name, stat.st_uid, stat.st_gid)
    return Path(name)


def configure(domain):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Run this worker through server.py https on the Linux server")
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
        raise RuntimeError("Invalid domain")
    path = Path("/etc/caddy/Caddyfile")
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Expected a regular /etc/caddy/Caddyfile")
    original = path.read_text()
    stat = path.stat()
    before = json.loads(run(["caddy", "adapt", "--config", str(path), "--adapter", "caddyfile"]))
    before_hosts = hosts(before)
    begin = f"# BEGIN openai-platform {domain}"
    end = f"# END openai-platform {domain}"
    block = f'''{begin}
{domain} {{
    log {{
        output discard
    }}
    reverse_proxy 127.0.0.1:8787 {{
        transport http {{
            dial_timeout 5s
            response_header_timeout 180s
        }}
    }}
}}
{end}'''
    if begin in original:
        if original.count(begin) != 1 or original.count(end) != 1:
            raise RuntimeError("Ambiguous managed Caddy block")
        updated = re.sub(re.escape(begin) + r".*?" + re.escape(end), lambda _: block, original, count=1, flags=re.S)
    else:
        if domain in before_hosts:
            raise RuntimeError("Domain already exists in an unmanaged Caddy route")
        updated = original.rstrip() + "\n\n" + block + "\n"
    candidate = write_candidate(path, updated, stat)
    backup = None
    try:
        after = json.loads(run(["caddy", "adapt", "--config", str(candidate), "--adapter", "caddyfile"]))
        if not before_hosts.issubset(hosts(after)):
            raise RuntimeError("Candidate would remove an existing virtual host")
        run(["runuser", "-u", "caddy", "--", "caddy", "validate", "--config", str(candidate), "--adapter", "caddyfile"])
        if path.read_text() != original:
            raise RuntimeError("Caddyfile changed concurrently; refusing to replace it")
        if updated != original:
            backup_dir = path.parent / "openai-platform-backups"
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            digest = hashlib.sha256(original.encode()).hexdigest()[:8]
            backup = backup_dir / f"Caddyfile-{stamp}-{digest}"
            backup.write_text(original)
            backup.chmod(0o600)
            os.replace(candidate, path)
            try:
                run(["systemctl", "reload", "caddy"])
            except Exception:
                rollback = write_candidate(path, original, stat)
                os.replace(rollback, path)
                run(["systemctl", "reload", "caddy"])
                raise RuntimeError("Reload failed; previous Caddyfile restored") from None
        with urllib.request.urlopen("http://127.0.0.1:2019/config/", timeout=10) as response:
            live = json.load(response)
        if domain not in hosts(live) or not before_hosts.issubset(hosts(live)):
            raise RuntimeError("Active Caddy routes do not match the expected virtual hosts")
        return {
            "domain": domain,
            "api_base": f"https://{domain}/v1",
            "config_file": str(path),
            "backup_file": str(backup) if backup else None,
            "previous_hosts": sorted(before_hosts),
            "current_hosts": sorted(hosts(live)),
            "existing_hosts_preserved": True,
            "service": run(["systemctl", "is-active", "caddy"]).strip(),
            "certificate_status": "Pending external HTTPS verification",
        }
    finally:
        candidate.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        print(json.dumps({"ok": True, "result": configure(sys.argv[1])}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)
