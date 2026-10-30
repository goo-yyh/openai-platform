#!/usr/bin/env python3
"""Run the installed Codex CLI through the gateway against a disposable fixture."""
from __future__ import annotations

import argparse
import datetime
import json
import secrets
import shutil
import subprocess
import sys

from common import BASE_URL, ROOT, codex_config, codex_env, credentials, gateway_url, load_env, redact, save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--remote", action="store_true", help="Verify local Codex through the managed SSH tunnel")
    transport.add_argument("--https", action="store_true", help="Verify Codex through the configured public HTTPS origin")
    args = parser.parse_args()
    env = load_env()
    if args.https:
        if not gateway_url(env).startswith("https://"):
            raise RuntimeError("Set an HTTPS GATEWAY_URL in .env")
    else:
        env["GATEWAY_URL"] = BASE_URL
    credentials(env)
    if args.remote:
        from server import Server
        server = Server(env)
        if not server.tunnel_running() or server.call("status", timeout=60)["health"] != "healthy":
            raise RuntimeError("Open the SSH tunnel to a healthy remote gateway first")
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Codex CLI not installed")
    fixture = ROOT / "work" / ("codex-smoke-" + secrets.token_hex(4))
    fixture.mkdir(parents=True)
    content = "gateway-probe-" + secrets.token_hex(12)
    (fixture / "input.txt").write_text(content + "\n")
    home = codex_config(env)
    prompt = (
        "This is a local gateway acceptance test. Work only in the current directory. "
        "Use a terminal tool to read input.txt. Create result.txt containing the exact "
        "contents of input.txt converted to uppercase, with one trailing newline. "
        "Use a terminal tool to independently check result.txt against input.txt. "
        "Do not read configuration, credentials, parent directories, or use network tools. "
        "Once verified, reply with exactly CODEX_GATEWAY_OK."
    )
    report = {
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "passed": False,
        "model": env.get("CODEX_MODEL") or "gpt-6.1-sol",
        "scope": "Installed Codex CLI, isolated config, local read/write/check through " + ("public HTTPS gateway" if args.https else "remote gateway over SSH" if args.remote else "local HTTP Responses gateway"),
        "gateway_origin": gateway_url(env),
        "fixture": str(fixture.relative_to(ROOT)),
    }
    try:
        proc = subprocess.run(
            [binary, "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-rules", "--sandbox", "workspace-write", "--json", "--color", "never", "-C", str(fixture), prompt],
            cwd=fixture, env=codex_env(env, home), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=240,
        )
        clean_stdout = redact(proc.stdout, env)
        clean_stderr = redact(proc.stderr, env)
        (ROOT / "work" / "codex-smoke.jsonl").write_text(clean_stdout)
        (ROOT / "work" / "codex-smoke.stderr.log").write_text(clean_stderr)
        events = []
        for line in clean_stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        completed_items = [e.get("item", {}) for e in events if e.get("type") == "item.completed"]
        commands = [i for i in completed_items if i.get("type") == "command_execution"]
        messages = [i.get("text", "") for i in completed_items if i.get("type") == "agent_message"]
        report.update({
            "exit_code": proc.returncode,
            "terminal_commands": len(commands),
            "terminal_exit_codes": [i.get("exit_code") for i in commands],
            "result_matches": (fixture / "result.txt").is_file() and (fixture / "result.txt").read_text() == content.upper() + "\n",
            "final_marker_received": any(m.strip() == "CODEX_GATEWAY_OK" for m in messages),
            "turn_completed": any(e.get("type") == "turn.completed" for e in events),
            "usage": [e.get("usage") for e in events if e.get("type") == "turn.completed"],
        })
        report["passed"] = bool(proc.returncode == 0 and len(commands) >= 2 and report["result_matches"] and report["final_marker_received"] and report["turn_completed"])
        if not report["passed"]:
            report["diagnostic"] = clean_stderr[-1500:]
            report["errors"] = [e for e in events if e.get("type") in ("error", "turn.failed")]
    except subprocess.TimeoutExpired:
        report["failure"] = "Codex did not complete within 240 seconds"
    finally:
        report["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(ROOT / "outputs" / ("https-codex-verification.json" if args.https else "server-codex-verification.json" if args.remote else "codex-verification.json"), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Error: " + redact(str(exc), load_env()), file=sys.stderr)
        sys.exit(1)
