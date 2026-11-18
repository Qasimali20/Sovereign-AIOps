"""Background runtime: the detector loop, the event recorder, and the workflow
runner that executes and resumes incident graphs off the request path."""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from backend.core import config, store
from backend.incidents import detector, graph
from backend.monitoring import events, services
from backend.llmops import model_evals, model_registry
from backend.security import events as sec_events, rules as sec_rules

SEC_LOOKBACK_S = 3600
AUDIT_CHECK_S = 600         # re-verify the audit hash chain every 10 minutes
VULN_SCAN_S = 6 * 3600      # CVE relevance scan of every node and the lab
# Incidents the detector can close on its own once it positively observes the condition cleared.
# Threshold kinds: the detector re-evaluates them every scan from fresh data, so "reachable and not firing"
# is itself the positive observation.
METRIC_KINDS = ("high_memory", "disk_usage", "gpu_temperature", "high_cpu", "gpu_stalled", "model_latency_high",
                "model_queue_stuck", "service_unhealthy")      # service_unhealthy: health is re-checked every scan
RECOVERABLE = METRIC_KINDS + ("node_unreachable", "quality_regression", "model_server_missing", "model_load_stalled",
               "svc_container_stopped", "svc_container_unhealthy", "svc_container_restarting",
               "svc_unit_failed", "svc_unit_restarting")
_BUSY = ("executing", "verifying")     # an action owns these; a pending approval expires if the problem clears


def condition_cleared(inc: dict, det: detector.DetectorState, svc: dict[str, dict], firing: set[str],
                      svc_base: services.Baseline | None = None, now: float | None = None) -> str | None:
    """Pure: evidence that the incident's condition is gone, or None. Absence of the alert alone is not enough
    (an unreachable node raises nothing about its models); each kind needs a positive observation."""
    if inc["fingerprint"] in firing:
        return None
    kind, node, res = inc["kind"], inc["node"], inc["resource"]
    if not det.reachable.get(node):
        return None
    if kind == "node_unreachable":
        return f"{node} monitor answering again"
    if kind == "quality_regression":           # raised from the newest evaluation: cleared by a newer, normal one
        return f"latest evaluation of {res} is back to its usual level"
    if kind in METRIC_KINDS:
        return f"{kind.replace('_', ' ')} no longer above threshold on {node}"
    if kind.startswith("model_"):
        return f"{res} serving again on {node}" if res in det.present.get(node, set()) else None
    snap = svc.get(node) or {}
    if not snap.get("ok"):
        return None
    if kind.startswith("svc_container_"):
        c = next((c for c in snap["containers"] if c["name"] == res), None)
        return (f"container {res} running{' and healthy' if c['health'] == 'healthy' else ''}"
                if c and c["state"] == "running" and c["health"] != "unhealthy" else None)
    if kind.startswith("svc_unit_"):
        if any(u["unit"] == res for u in snap["problem_units"]):
            return None
        if svc_base is not None and not services.unit_settled(svc_base, node, res, time.time() if now is None else now):
            return None                         # up between crash-loop attempts is not recovered
        return f"{res} no longer failed" + (f" and stable for {services.UNIT_STABLE_S // 60} min" if svc_base else "")
    return None


def is_model_container(a, svc: dict[str, dict]) -> bool:
    c = next((c for c in (svc.get(a.node) or {}).get("containers", []) if c["name"] == a.resource), None)
    return bool(c and "vllm" in c["image"].lower())


class Runtime:
    def __init__(self, scan_seconds: int):
        self.scan_seconds = scan_seconds
        self.started = time.time()
        self.scan_info: dict = {"last": None, "last_alerts": 0, "scans": 0, "error": None}
        self._det = detector.DetectorState()
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._llm_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_INVESTIGATIONS)      # scans mutate detector/baseline/cursor state: one at a time
        self._sec_cursor: dict[str, float] = {}
        self.sec_coverage: dict[str, dict] = {}
        self.services: dict[str, dict] = {}
        self._svc_base = services.Baseline()
        self._pool = ThreadPoolExecutor(max_workers=len(config.NODES), thread_name_prefix="sec")
        self.audit_check: dict = {"ok": None, "checked": None}
        self.vulns: dict[str, dict] = {}
        self._vuln_at = 0.0

    # ------------------------------------------------------------ workflows
    @property
    def kernel_coverage(self) -> dict[str, dict]:
        return dict(self._det.kernel_coverage)

    @property
    def running(self) -> list[str]:
        with self._lock:
            return sorted(self._running)

    def is_running(self, incident_id: str) -> bool:
        with self._lock:
            return incident_id in self._running

    def spawn(self, fn, incident_id: str, *args) -> bool:
        """Run fn(incident_id, *args) in a thread; at most one workflow per incident."""
        with self._lock:
            if incident_id in self._running:
                return False
            self._running.add(incident_id)

        heavy = fn in (graph.run_incident, graph.rerun_incident, graph.continue_incident)   # LLM-bound investigations

        def run():
            try:
                if heavy:
                    with self._llm_slots:            # waits its turn; resumes after approval never queue here
                        fn(incident_id, *args)
                else:
                    fn(incident_id, *args)
            except Exception as e:  # a crashed workflow must surface to a human, never vanish
                store.audit(incident_id, "orchestrator", "workflow_error", {"error": repr(e)})
                store.set_status(incident_id, "needs_human", keep_final=True)
            finally:
                with self._lock:
                    self._running.discard(incident_id)

        threading.Thread(target=run, daemon=True, name=f"wf-{incident_id}").start()
        return True

    def resume(self, incident_id: str, approval_id: str) -> None:
        """Resume after a human decision. The incident is marked awaiting_approval one graph step before the workflow
        parks at its interrupt, so a fast decision can arrive while the thread still runs: retry until it is parked.
        resume_decided() is the backstop (restarts, or a retry that timed out)."""
        if not (graph.parked_at_approval(incident_id) and self.spawn(graph.resume_incident, incident_id, approval_id)):
            threading.Thread(target=self._resume_when_parked, args=(incident_id, approval_id),
                             daemon=True, name=f"resume-{incident_id}").start()

    def _resume_when_parked(self, incident_id: str, approval_id: str, timeout_s: float = 120.0) -> None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if not self.is_running(incident_id) and graph.parked_at_approval(incident_id) \
                    and self.spawn(graph.resume_incident, incident_id, approval_id):
                return
            time.sleep(0.5)

    def resume_decided(self) -> list[str]:
        """Incidents whose approval was decided but whose workflow is still parked: resume them."""
        resumed = []
        for iid, aid in store.decided_but_parked():
            if not self.is_running(iid) and graph.parked_at_approval(iid) and self.spawn(graph.resume_incident, iid, aid):
                store.audit(iid, "orchestrator", "workflow_resumed_after_decision", {"approval_id": aid})
                resumed.append(iid)
        return resumed

    # ------------------------------------------------------------ detection
    def _collect_security(self) -> None:
        now = time.time()

        def one(node):
            since = self._sec_cursor.get(node.name, now - SEC_LOOKBACK_S)
            return node, sec_events.collect(node, since)

        for node, r in self._pool.map(one, config.NODES):
            self.sec_coverage[node.name] = {"ok": r["ok"], "error": r["error"], "checked": now}
            if r["ok"]:
                sec_events.save(r["events"])          # unique key makes overlapping reads idempotent
                # saves are idempotent, so overlap is safe; the cursor must still move when nothing happened
                newest = max((e["t"] for e in r["events"]), default=now) - detector.CURSOR_OVERLAP_S
                self._sec_cursor[node.name] = max(self._sec_cursor.get(node.name, 0.0), newest)

    def scan_once(self) -> list[dict]:
        with self._scan_lock:                   # API "scan now" and the detector thread must not interleave
            return self._scan()

    def _scan(self) -> list[dict]:
        try:
            self._collect_security()
        except Exception as e:
            store.audit(None, "security_collector", "error", {"error": repr(e)})
        svc_alerts = []
        for node, snap in self._pool.map(lambda n: (n, services.collect(n)), config.NODES):
            self.services[node.name] = snap
            svc_alerts += services.alerts(node.name, snap, self._svc_base)
        det_alerts = detector.scan(self._det)
        # A stopped model container is the same event as its model disappearing: keep the model-level incident.
        svc_alerts = [a for a in svc_alerts if not (a.kind == "svc_container_stopped" and self._det.missing.get(a.node)
                                                    and is_model_container(a, self.services))]
        alerts = det_alerts + sec_rules.evaluate(time.time()) + svc_alerts + self._vuln_alerts() + self._eval_alerts()
        self.scan_info.update(last=time.time(), last_alerts=len(alerts),
                              scans=self.scan_info["scans"] + 1, error=None)
        opened = []
        for a in alerts:
            if a.kind == "model_server_missing" and store.latest_status(a.fingerprint) == "closed":
                # a person closed it (server retired on purpose): stop tracking that server instead of re-raising
                self._det.seen_models.get(a.node, set()).discard(a.resource)
                continue
            iid, created = store.open_incident(a.kind, a.node, a.resource, a.title, a.fingerprint, a.detail)
            if created:
                opened.append({"id": iid, "title": a.title})
                pause = store.ai_paused(a.resource)
                if pause:                       # an evaluation's manual arm: recorded, not investigated
                    store.audit(iid, "orchestrator", "investigation_paused", pause)
                    store.set_status(iid, "needs_human", keep_final=True)
                else:
                    self.spawn(graph.run_incident, iid)
        self._recover({a.fingerprint for a in alerts})
        return opened

    def _recover(self, firing: set[str]) -> None:
        for inc in store.open_incidents_of(RECOVERABLE):
            if inc["status"] in _BUSY or self.is_running(inc["id"]):
                continue                     # let the workflow finish; the next scan picks it up
            why = condition_cleared(inc, self._det, self.services, firing, self._svc_base)
            if why:
                if inc["status"] == "awaiting_approval":
                    store.expire_pending(inc["id"], f"problem cleared before a decision: {why}")
                store.mark_recovered(inc["id"], inc["created"], why)

    def _eval_alerts(self) -> list:
        """Start the scheduled model evaluation when due (in its own thread: it takes about a minute), and alert on
        any model whose newest score fell well below its own recent level."""
        model_registry.refresh_if_due()       # cheap identity probe of every model server, every 10 minutes
        last = model_evals.last_run_at()
        if not model_evals.running() and (last is None or time.time() - last > model_evals.INTERVAL_S):
            threading.Thread(target=model_evals.run_all, daemon=True, name="model-evals").start()
        return model_evals.regression_alerts()

    def _vuln_alerts(self) -> list:
        """Re-scan every VULN_SCAN_S (or when the lab inventory changes); alert on every vulnerable verdict."""
        from backend.security import lab, vulns
        from backend.security.rules import SecAlert
        lab_sig = str(lab.state()["packages"]) + str(lab.state()["patched"])
        if time.time() - self._vuln_at > VULN_SCAN_S or self.vulns.get("LAB", {}).get("sig") != lab_sig:
            for node, r in self._pool.map(lambda n: (n.name, vulns.scan_node(n)), config.NODES):
                self.vulns[node] = r
            s = lab.state()
            self.vulns["LAB"] = vulns.scan_lab(s["packages"], s["patched"]) | {"sig": lab_sig}
            self._vuln_at = time.time()
        return [SecAlert("sec_vulnerable_package", node, v["cve"],
                         f"{v['cve']} ({v['name']}) unpatched in {v['package']} on {node}",
                         {"cve": v["cve"], "package": v["package"], "installed": v.get("installed"), "basis": v.get("basis"),
                          "severity": v["severity"], "references": v["references"], "host": "lab-web-01" if node == "LAB" else node,
                          "suggested_techniques": [v["technique"]]})
                for node, r in self.vulns.items() for v in r.get("results", []) if v["status"] == "vulnerable"]

    def check_audit(self, full: bool = False) -> dict:
        """Incremental: only rows since the last verified head (append-only triggers protect older rows).
        `full` re-verifies from the start; the detector loop does that once a day."""
        last = self.audit_check if self.audit_check.get("ok") and not full else {}
        r = store.verify_audit_chain(after_seq=last.get("head_seq", 0), after_hash=last.get("head", store.GENESIS))
        if not r["ok"] and self.audit_check.get("ok") is not False:
            store.audit(None, "integrity_checker", "audit_chain_broken", r)     # once per transition
        self.audit_check = r | {"checked": time.time()}
        return self.audit_check

    def _detector_loop(self) -> None:
        last_audit, last_daily = 0.0, 0.0
        while True:
            if time.time() - last_daily > 86400:          # daily: full chain verification and retention
                try:
                    self.check_audit(full=True)
                    store.prune()
                except Exception as e:
                    store.audit(None, "maintenance", "daily_job_error", {"error": repr(e)})
                last_daily = last_audit = time.time()
            if time.time() - last_audit > AUDIT_CHECK_S:
                try:
                    self.check_audit()
                except Exception as e:
                    self.audit_check = {"ok": False, "error": repr(e), "checked": time.time()}
                last_audit = time.time()
            try:
                self.scan_once()
                self.resume_decided()
            except Exception as e:
                self.scan_info["error"] = repr(e)
                store.audit(None, "detector", "scan_error", {"error": repr(e)})
            time.sleep(self.scan_seconds)

    def recover_interrupted(self) -> list[tuple[str, str]]:
        """Workflows run in threads, so a restart cuts them off. Investigations (read-only so far) continue from
        their checkpoint; anything interrupted while executing or verifying a change goes to a person instead,
        because re-running an action automatically could repeat it."""
        done = []
        with store.conn() as c:
            rows = c.execute("SELECT id, status FROM incidents WHERE status IN "
                             "('open','investigating','executing','verifying')").fetchall()
        for iid, status in rows:
            if self.is_running(iid):
                continue
            if status in ("open", "investigating"):
                store.audit(iid, "orchestrator", "workflow_resumed_after_restart", {"status": status})
                self.spawn(graph.continue_incident, iid)
                done.append((iid, "resumed"))
            else:
                store.audit(iid, "orchestrator", "workflow_interrupted", {"status": status,
                            "reason": "platform restarted during execution or verification; not re-run automatically"})
                store.set_status(iid, "needs_human")
                done.append((iid, "needs_human"))
        done += [(iid, "resumed_after_decision") for iid in self.resume_decided()]
        return done

    def start(self) -> None:
        store.init()
        graph.graph()
        threading.Thread(target=lambda: (time.sleep(5), self.recover_interrupted()), daemon=True, name="recover").start()
        threading.Thread(target=events.record_forever, daemon=True, name="event-recorder").start()
        if os.environ.get("AIOPS_DETECTOR", "1") == "1":
            threading.Thread(target=self._detector_loop, daemon=True, name="detector").start()


runtime = Runtime(int(os.environ.get("AIOPS_SCAN_SECONDS", "20")))
