"""Container lifecycle event recorder (Phase 3 event ingestion, first piece).

Docker keeps only a small in-memory event buffer, which health-check exec events
from other containers flush within minutes. This streams `docker events` and keeps
the lifecycle events (not exec_*) in the platform database."""
from __future__ import annotations

import json
import subprocess
import time

from backend.core import store

KEEP = {"create", "start", "restart", "stop", "kill", "die", "oom", "pause", "unpause",
        "destroy", "health_status: unhealthy", "health_status: healthy"}


def record_forever() -> None:
    while True:
        try:
            p = subprocess.Popen(["docker", "events", "--filter", "type=container", "--format", "{{json .}}"],
                                 stdout=subprocess.PIPE, text=True)
            for line in p.stdout:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                action = e.get("Action", "")
                if action not in KEEP:
                    continue
                attrs = (e.get("Actor") or {}).get("Attributes") or {}
                name = attrs.get("name", "")
                store.add_container_event(e.get("timeNano", 0) / 1e9 or time.time(), name, action,
                                          {k: attrs[k] for k in ("signal", "exitCode", "image") if k in attrs})
        except Exception as ex:
            store.audit(None, "event_recorder", "error", {"error": repr(ex)})
        time.sleep(5)
