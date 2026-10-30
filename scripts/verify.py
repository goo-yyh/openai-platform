#!/usr/bin/env python3
"""Live gateway checks; 3 short Responses calls, no user files sent upstream."""
from __future__ import annotations

import collections
import datetime
import json
import secrets
import sys
import time

from common import ROOT, compose, credentials, load_env, redact, request, save_json


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def stream_response(env, payload):
    started = time.monotonic()
    kinds = collections.Counter()
    first_at = None
    final = None
    with request(env, "/v1/responses", payload) as response:
        if response.status != 200:
            error = json.loads(response.read()).get("error", {})
            raise RuntimeError(f"Responses HTTP {response.status}: {error.get('code') or error.get('type')}")
        check("text/event-stream" in response.headers.get("Content-Type", ""), "Missing SSE Content-Type")
        request_id = response.headers.get("x-request-id")
        data_lines = []
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data == "[DONE]":
                    continue
                event = json.loads(data)
                kind = event.get("type", "unknown")
                kinds[kind] += 1
                if first_at is None:
                    first_at = time.monotonic() - started
                if kind in ("error", "response.failed", "response.incomplete"):
                    raise RuntimeError(f"Unsuccessful streaming event: {kind}")
                if kind == "response.completed":
                    final = event["response"]
    elapsed = time.monotonic() - started
    check(final is not None and final.get("status") == "completed", "SSE did not finish with response.completed")
    check(kinds["response.created"] == 1, "Missing response.created")
    check(first_at is not None and elapsed > first_at, "Streaming timing not observed")
    return final, {
        "http_status": 200,
        "request_id": request_id,
        "returned_model": final.get("model"),
        "first_event_seconds": round(first_at, 3),
        "total_seconds": round(elapsed, 3),
        "event_counts": dict(kinds),
        "usage": final.get("usage"),
    }


def output_text(response):
    return "".join(
        part.get("text", "")
        for item in response.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") == "output_text"
    )


def main():
    env = load_env()
    credentials(env)
    model = env.get("CODEX_MODEL") or "gpt-6.1-sol"
    report = {
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": "Local HTTP/SSE gateway; live OpenAI API. WebSocket transport not tested.",
        "requested_model": model,
        "checks": [],
        "passed": False,
    }
    target = ROOT / "outputs" / "verification.json"

    def record(name, **details):
        report["checks"].append({"name": name, "passed": True, **details})
        print("PASS " + name, flush=True)
        save_json(target, report)

    try:
        for name, path, token, expected in (
            ("health", "/healthz", False, 200),
            ("missing_token_rejected", "/v1/models", False, 401),
            ("wrong_token_rejected", "/v1/models", "invalid-gateway-token", 401),
            ("non_api_path_rejected", "/not-an-api", True, 404),
        ):
            with request(env, path, token=token, timeout=15) as response:
                body = response.read()
                check(response.status == expected, f"{name}: expected {expected}, got {response.status}")
                check(env["OPENAI_API_KEY"].encode() not in body, "Upstream key appeared in response")
                record(name, http_status=response.status)

        with request(env, "/v1/models") as response:
            check(response.status == 200, f"Model discovery failed: HTTP {response.status}")
            models = json.load(response)
            check(model in [item["id"] for item in models["data"]], "Configured model absent from API project")
            record("authenticated_upstream_model_list", http_status=200, selected_model=model)

        with request(env, "/v1/responses", {"model": "gateway-verification-nonexistent-model", "input": "test", "store": False}) as response:
            error = json.load(response).get("error", {})
            check(response.status in (400, 404), "Upstream invalid-model status was not preserved")
            record("upstream_error_preserved", http_status=response.status, error_code=error.get("code"), request_id=response.headers.get("x-request-id"))

        nonce = secrets.token_hex(8)
        history = [{"role": "user", "content": f"Call gateway_probe exactly once with value {nonce}. After its result, reply with exactly GATEWAY_OK:{nonce}."}]
        common = {"model": model, "stream": True, "store": False, "reasoning": {"effort": "low"}, "max_output_tokens": 512, "include": ["reasoning.encrypted_content"]}
        first, evidence = stream_response(env, {
            **common, "input": history,
            "tools": [{"type": "function", "name": "gateway_probe", "description": "Return the supplied verification value.", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}, "strict": True}],
            "tool_choice": {"type": "function", "name": "gateway_probe"},
            "parallel_tool_calls": False,
        })
        calls = [item for item in first["output"] if item["type"] == "function_call"]
        check(len(calls) == 1 and calls[0]["name"] == "gateway_probe", "Function call was lost or changed")
        check(json.loads(calls[0]["arguments"]) == {"value": nonce}, "Function arguments were changed")
        check(evidence["event_counts"].get("response.function_call_arguments.delta", 0) > 0, "Missing streaming function arguments")
        record("streaming_function_call", **evidence)

        history.extend(first["output"])
        history.append({"type": "function_call_output", "call_id": calls[0]["call_id"], "output": json.dumps({"value": nonce})})
        second, evidence = stream_response(env, {**common, "input": history})
        check(output_text(second).strip() == "GATEWAY_OK:" + nonce, "Tool-result roundtrip did not return the expected value")
        record("function_output_roundtrip", **evidence)

        history.extend(second["output"])
        history.append({"role": "user", "content": "Reply with only the original gateway_probe value from the earlier turn."})
        third, evidence = stream_response(env, {**common, "input": history})
        check(output_text(third).strip() == nonce, "Follow-up history was not preserved")
        record("stateless_followup", **evidence)

        # Only return booleans/paths; never inspect or print container environment.
        runtime = compose(env, "exec", "-T", "gateway", "sh", "-c", "test ! -e /config/caddy/autosave.json && test ! -e /data/caddy/autosave.json && echo no-autosaved-config")
        check("no-autosaved-config" in runtime.stdout, "Caddy configuration was persisted")
        record("no_autosaved_config")
        report["passed"] = True
        print("All gateway checks passed. Evidence: outputs/verification.json", flush=True)
    except Exception as exc:
        report["failure"] = redact(str(exc), env)
        print("FAIL " + report["failure"], file=sys.stderr)
    finally:
        report["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_json(target, report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
