"""OpenRouter conversation loop with audited, allowlisted research calls."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from vatlog.canon import parse_json
from .provider import OpenRouter, ProviderError, SYSTEM
from .research import ResearchTools, result_failed


@dataclass(frozen=True)
class Event:
    """One observed model response or completed tool dispatch; retained chronologically."""
    name: str
    request: bytes
    response: bytes
    started: datetime
    stopped: datetime
    metadata: dict[str, str | int]


async def analyze(provider: OpenRouter, tools: ResearchTools, prompt: str, model: str,
                  log: Callable[[str], None]) -> tuple[list[Event], str, str]:
    """Run until a final textual research answer, without executing model-provided code."""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    events: list[Event] = []
    ids: set[str] = set()
    turn = 0
    while True:
        turn += 1
        log(f"Research model turn {turn}")
        reply = await provider.chat(messages, model, tools.tools, log)
        events.append(Event("openrouter", reply.request, reply.response, reply.started, reply.stopped,
                            {"selected_model": model, "returned_model": reply.model, "turn": turn}))
        messages.append(reply.message)
        calls = reply.message.get("tool_calls", [])
        if not calls:
            return events, reply.message["content"], reply.model
        # Validate the whole requested batch before dispatching any part of it.
        requests = []
        for call in calls:
            try:
                id, function = call["id"], call["function"]
                name = function["name"]
                arguments = parse_json(function["arguments"])
                if (call.get("type") != "function" or not isinstance(id, str) or not id
                        or id in ids or not isinstance(name, str) or not isinstance(arguments, dict)):
                    raise ValueError("invalid tool call")
                ids.add(id)
                requests.append((id, name, arguments))
            except (ValueError, KeyError, TypeError) as exc:
                raise ProviderError("Model returned malformed or duplicate tool calls. No successful session was sealed.") from exc
        for id, name, arguments in requests:
            started = datetime.now(timezone.utc)
            result = await tools.call(name, arguments)
            raw_request = json.dumps({"name": name, "arguments": arguments, "tool_call_id": id}, ensure_ascii=False).encode()
            raw_result = json.dumps(result, ensure_ascii=False).encode()
            events.append(Event(name, raw_request, raw_result, started, datetime.now(timezone.utc),
                                {"tool": name, "tool_call_id": id,
                                 "status": "error" if result_failed(result) else "ok"}))
            messages.append({"role": "tool", "tool_call_id": id, "content": raw_result.decode()})
