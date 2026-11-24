"""Alert ingestion, deduplication and notification.

Design notes (why it looks like this):

* **Alertmanager-compatible.** The de-facto standard in big-tech incident
  pipelines is Prometheus Alertmanager posting to a webhook receiver. The parser
  therefore accepts Alertmanager's payload shape first, and falls back to a
  minimal generic shape so any other system (Grafana, 云监控, custom) can feed us.

* **Accept then work.** The endpoint returns 202 immediately and runs the
  analysis in a background task. A 16-second analysis inside the alert webhook
  would make the alerting pipeline time out and retry, producing duplicates.

* **Dedup before enqueue.** One incident typically fires dozens of alerts. A
  sliding window keyed by service keeps the system from spawning dozens of
  identical analyses. This is the alert-storm problem every SRE team hits.

* **Notify best-effort.** Delivery failures must never lose the analysis, so
  notifications are fire-and-forget with errors logged only.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Alert:
    service: str
    name: str
    severity: str = "warning"
    summary: str = ""
    starts_at: str = ""
    source: str = "alertmanager"

    def title(self) -> str:
        return f"[{self.severity.upper()}] {self.service} · {self.name}"


def _labels_to_service(labels: dict[str, Any]) -> str:
    """Best-effort service identity from common label conventions."""
    for key in ("service", "service_name", "app", "job", "instance", "target"):
        value = labels.get(key)
        if value:
            return str(value)
    return ""


def parse_alertmanager(payload: dict[str, Any]) -> list[Alert]:
    alerts: list[Alert] = []
    for item in payload.get("alerts", []) or []:
        labels = item.get("labels") or {}
        annotations = item.get("annotations") or {}
        service = _labels_to_service(labels)
        if not service:
            continue
        alerts.append(Alert(
            service=service,
            name=str(labels.get("alertname") or labels.get("alert_name") or "unknown"),
            severity=str(labels.get("severity") or "warning"),
            summary=str(annotations.get("summary") or annotations.get("description") or ""),
            starts_at=str(item.get("startsAt") or ""),
            source="alertmanager",
        ))
    return alerts


def parse_generic(payload: dict[str, Any]) -> list[Alert]:
    """Minimal shape: {"service": "...", "alert": "...", "severity": "..."}"""
    service = payload.get("service") or payload.get("service_name")
    if not service:
        return []
    return [Alert(
        service=str(service),
        name=str(payload.get("alert") or payload.get("alertname") or "manual"),
        severity=str(payload.get("severity") or "warning"),
        summary=str(payload.get("summary") or payload.get("message") or ""),
        starts_at=str(payload.get("starts_at") or ""),
        source=str(payload.get("source") or "generic"),
    )]


def parse(payload: dict[str, Any]) -> list[Alert]:
    alerts = parse_alertmanager(payload)
    return alerts or parse_generic(payload)


class Deduper:
    """Sliding-window dedup keyed by service.

    Process-local on purpose: a single instance serves alerts, and a restart
    losing the window is acceptable (worst case: one duplicate analysis).
    """

    def __init__(self, window_s: int = 1800) -> None:
        self._window = max(0, window_s)
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def accept(self, key: str) -> bool:
        if self._window <= 0:
            return True
        now = time.time()
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < self._window:
                return False
            self._seen[key] = now
            # opportunistic cleanup so the dict cannot grow unbounded
            if len(self._seen) > 2000:
                cutoff = now - self._window
                self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
            return True


class Notifier:
    """Posts to DingTalk / Feishu / any webhook that accepts a JSON body.

    Both DingTalk and Feishu robot webhooks are extremely common in Chinese
    internet companies, so the payload is chosen by host rather than requiring a
    separate channel abstraction. A generic URL receives a plain JSON envelope.
    """

    def __init__(self, urls: list[str], timeout_s: float = 8.0) -> None:
        self._urls = [u for u in urls if u]
        self._timeout = timeout_s
        self._error = ""

    @property
    def enabled(self) -> bool:
        return bool(self._urls)

    @property
    def error(self) -> str:
        return self._error

    def _body(self, url: str, title: str, text: str) -> str:
        if "dingtalk" in url:
            return json.dumps({"msgtype": "markdown",
                               "markdown": {"title": title, "text": f"### {title}\n\n{text}"}},
                              ensure_ascii=False)
        if "feishu" in url or "larksuite" in url:
            return json.dumps({"msg_type": "text", "content": {"text": f"{title}\n\n{text}"}},
                              ensure_ascii=False)
        return json.dumps({"title": title, "text": text}, ensure_ascii=False)

    def send(self, title: str, text: str) -> int:
        """Returns how many channels accepted the message."""
        if not self._urls:
            return 0
        sent = 0
        try:
            import httpx
        except ImportError:  # pragma: no cover
            return 0
        for url in self._urls:
            try:
                with httpx.Client(timeout=self._timeout) as c:
                    r = c.post(url, content=self._body(url, title, text),
                               headers={"Content-Type": "application/json"})
                if r.status_code < 400:
                    sent += 1
                else:
                    self._error = f"{url[:40]} {r.status_code}: {r.text[:100]}"
                    log.warning("notify failed: %s", self._error)
            except Exception as exc:  # noqa: BLE001
                self._error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("notify failed: %s", self._error)
        return sent


@dataclass
class IncidentResult:
    task_id: str
    service: str
    status: str
    report_url: str = ""
    claims: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def report_message(result: IncidentResult) -> tuple[str, str]:
    """Compose the notification title and body for a finished incident.

    The title must be readable by a human on call — internal status codes such
    as ``ok`` never belong in a chat message.
    """
    label = {"ok": "分析完成，待复核", "failed": "分析失败"}.get(result.status,
                                                                result.status)
    title = f"复盘完成｜{result.service}｜{label}"
    if result.status == "ok":
        body = (f"- 结论数：{result.claims}\n- 报告：{result.report_url}\n"
                f"- 任务：`{result.task_id[:8]}`\n"
                f"- 下一步：打开报告点「复核通过并交付」即完成闭环")
    else:
        body = (f"自动分析失败：`{result.task_id[:8]}` 需要人工查看。\n"
                f"日志：journalctl -u copilot -n 50")
    return title, body
