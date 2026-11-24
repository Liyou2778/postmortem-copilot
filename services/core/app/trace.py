"""Trace projection over the event journal.

A true trace is not stored separately: the event journal already contains every
state transition and every tool INTENT/RESULT with timestamps and sequence
numbers. This module folds those events into OTel-shaped spans, so the trace can
never disagree with the audit log.

Span fields are intentionally close to the OTel trace model (32-hex trace id,
16-hex span id, parent, start/end unix nanoseconds, status, attributes), which
makes an OTLP exporter a small adapter later rather than a rewrite.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable

from .events import EventType

_TERMINAL = {
    EventType.STEP_SUCCEEDED, EventType.STEP_FAILED, EventType.STEP_TIMEOUT,
    EventType.STEP_SKIPPED, EventType.STEP_UNKNOWN,
}


def _epoch(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _sid(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _status(event_type: EventType) -> str:
    if event_type == EventType.STEP_SUCCEEDED:
        return "OK"
    if event_type in _TERMINAL:
        return "ERROR"
    return "UNSET"


def build_trace(events: Iterable[Any], task_id: str) -> dict[str, Any]:
    ordered = sorted(events, key=lambda event: event.seq)
    trace_id = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:32]
    if not ordered:
        return {"task_id": task_id, "trace_id": trace_id, "spans": []}

    task_span_id = _sid(task_id, "task")
    task_start = _epoch(ordered[0].created_at)
    task_end = _epoch(ordered[-1].created_at)
    task_status = "UNSET"
    spans: list[dict[str, Any]] = []

    # Latest plan gives step -> role.
    roles: dict[str, str] = {}
    step_start: dict[str, float] = {}
    step_end: dict[str, float] = {}
    step_status: dict[str, str] = {}
    step_attributes: dict[str, dict[str, Any]] = {}
    intents: dict[str, dict[str, Any]] = {}

    def add_span(name: str, span_id: str, parent: str, start: float, end: float,
                 kind: str, status: str, attributes: dict[str, Any]) -> None:
        spans.append({
            "name": name,
            "span_id": span_id,
            "parent_span_id": parent,
            "trace_id": trace_id,
            "kind": kind,
            "start_time_unix_nano": int(start * 1e9),
            "end_time_unix_nano": int(end * 1e9),
            "start": _iso(start),
            "end": _iso(end),
            "duration_ms": round(max(0.0, end - start) * 1000.0, 2),
            "status": status,
            "attributes": attributes,
        })

    for event in ordered:
        etype = event.type
        payload = event.payload or {}
        at = _epoch(event.created_at)
        if etype == EventType.TASK_DELIVERED:
            task_status = "OK"
        elif etype in (EventType.TASK_FAILED, EventType.TASK_CANCELLED):
            task_status = "ERROR"
        elif etype == EventType.TASK_PLANNED:
            for step in payload.get("steps") or []:
                if step.get("step_id"):
                    roles[step["step_id"]] = str(step.get("role") or "")
        elif etype == EventType.STEP_STARTED:
            sid = str(payload.get("step_id") or "")
            step_start[sid] = at
            step_attributes[sid] = {
                "role": roles.get(sid, ""),
                "owner": payload.get("owner"),
                "fence": payload.get("fence"),
                "attempt": payload.get("attempt"),
            }
        elif etype == EventType.STEP_LEASED:
            sid = str(payload.get("step_id") or "")
            step_start.setdefault(sid, at)
            step_attributes.setdefault(sid, {}).update({
                "owner": payload.get("owner"),
                "fence": payload.get("fence"),
            })
        elif etype in _TERMINAL:
            sid = str(payload.get("step_id") or "")
            step_end[sid] = at
            step_status[sid] = _status(etype)
        elif etype == EventType.INVOCATION_INTENT:
            key = str(payload.get("idempotency_key") or "")
            intents[key] = {
                "step_id": str(payload.get("step_id") or ""),
                "tool": str(payload.get("tool") or ""),
                "start": at,
            }
        elif etype == EventType.INVOCATION_RESULT:
            key = str(payload.get("idempotency_key") or "")
            intent = intents.pop(key, None)
            if intent:
                add_span(
                    name=f"tool {intent['tool']}",
                    span_id=_sid(task_id, "tool", key),
                    parent=_sid(task_id, "step", intent["step_id"]),
                    start=intent["start"],
                    end=at,
                    kind="CLIENT",
                    status="OK" if payload.get("status") == "OK" else "ERROR",
                    attributes={
                        "tool": intent["tool"],
                        "step_id": intent["step_id"],
                        "idempotency_key": key,
                    },
                )

    for sid, start in step_start.items():
        end = step_end.get(sid, task_end)
        add_span(
            name=f"step {sid}",
            span_id=_sid(task_id, "step", sid),
            parent=task_span_id,
            start=start,
            end=end,
            kind="INTERNAL",
            status=step_status.get(sid, "UNSET"),
            attributes={"step_id": sid, **step_attributes.get(sid, {})},
        )
    # Dangling intents (worker died between INTENT and RESULT) are spans too:
    # they are exactly what a human needs to see in a trace.
    for key, intent in intents.items():
        add_span(
            name=f"tool {intent['tool']} (dangling)",
            span_id=_sid(task_id, "tool", key),
            parent=_sid(task_id, "step", intent["step_id"]),
            start=intent["start"],
            end=task_end,
            kind="CLIENT",
            status="ERROR",
            attributes={
                "tool": intent["tool"], "step_id": intent["step_id"],
                "idempotency_key": key, "dangling": True,
            },
        )

    add_span(
        name="analysis task",
        span_id=task_span_id,
        parent="",
        start=task_start,
        end=task_end,
        kind="SERVER",
        status=task_status,
        attributes={"task_id": task_id, "events": len(ordered)},
    )
    spans.sort(key=lambda span: (span["start_time_unix_nano"], span["name"]))
    return {
        "task_id": task_id,
        "trace_id": trace_id,
        "spans": spans,
        "summary": {
            "spans": len(spans),
            "duration_ms": round(max(0.0, task_end - task_start) * 1000.0, 2),
            "status": task_status,
        },
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")

# ----------------------------------------------------------------- OTLP export
_KIND = {"INTERNAL": 1, "SERVER": 2, "CLIENT": 3, "PRODUCER": 4, "CONSUMER": 5}
_STATUS = {"UNSET": 0, "OK": 1, "ERROR": 2}


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": "" if value is None else str(value)}


def to_otlp(trace: dict[str, Any], service_name: str = "postmortem-copilot") -> dict:
    """Project the internal OTel-shaped trace into OTLP/HTTP JSON."""
    spans = []
    for span in trace.get("spans") or []:
        item = {
            "traceId": span["trace_id"],
            "spanId": span["span_id"],
            "name": span["name"],
            "kind": _KIND.get(span.get("kind", "INTERNAL"), 1),
            "startTimeUnixNano": str(span["start_time_unix_nano"]),
            "endTimeUnixNano": str(span["end_time_unix_nano"]),
            "status": {"code": _STATUS.get(span.get("status", "UNSET"), 0)},
            "attributes": [
                {"key": str(key), "value": _otlp_value(value)}
                for key, value in (span.get("attributes") or {}).items()
            ],
        }
        if span.get("parent_span_id"):
            item["parentSpanId"] = span["parent_span_id"]
        spans.append(item)
    return {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": service_name}},
            ]},
            "scopeSpans": [{
                "scope": {"name": "postmortem-copilot", "version": "1.0"},
                "spans": spans,
            }],
        }]
    }


def export_otlp(trace: dict[str, Any], endpoint: str,
                headers: dict[str, str] | None = None,
                timeout: float = 5.0,
                service_name: str = "postmortem-copilot") -> dict[str, Any]:
    """Best-effort OTLP/HTTP export. Returns {ok, status, error}."""
    if not endpoint:
        return {"ok": False, "status": None, "error": "OTEL exporter endpoint not configured"}
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    payload = json.dumps(to_otlp(trace, service_name)).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = getattr(response, "status", 0)
            response.read()
        return {"ok": 200 <= status < 300, "status": status, "error": ""}
    except Exception as exc:  # noqa: BLE001 - observability must not break the API
        return {"ok": False, "status": None, "error": str(exc)}
