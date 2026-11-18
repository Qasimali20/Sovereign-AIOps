"""Dependency graph of the fleet and failure-impact analysis.

Vertices: nodes, model servers (from the fleet monitor), models (all replicas of one served name), gateway routes
(config.MODEL_ALIASES, in fallback order), container stacks (grouped by name prefix), and the platform's own
components with what they need. Each vertex lists requirement groups; a group is satisfied while any member is up.
`evaluate` propagates states (up / degraded / down) to a fixed point; `impact` compares the graph with a set of
vertices forced down against the graph as it is, so the answer is "what changes if X fails", given what is already
broken. Pure over its inputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.core import config

UP, DEGRADED, DOWN = "up", "degraded", "down"
RANK = {UP: 0, DEGRADED: 1, DOWN: 2}


@dataclass
class Req:
    any_of: list[str]
    effect: str = DOWN            # state when every member is down
    partial: bool = False         # some member down -> degraded (lost redundancy, or a fallback in use)
    why: str = ""


@dataclass
class Vertex:
    id: str
    type: str                     # node | server | model | route | component | stack
    label: str
    node: str | None = None
    status: str = UP              # observed: a server not ready, a node unreachable
    reqs: list[Req] = field(default_factory=list)


_STACK = re.compile(r"^([a-z0-9]+)")
_MODEL_IMAGE = re.compile(r"vllm|sglang|ollama|tritonserver", re.I)    # model servers: already vertices of their own


def build(fleet: dict[str, dict | None], services: dict[str, dict] | None = None) -> dict[str, Vertex]:
    g: dict[str, Vertex] = {}
    add = lambda v: g.setdefault(v.id, v)                                                         # noqa: E731
    replicas: dict[str, list[str]] = {}
    for n in config.NODES:
        d = fleet.get(n.name)
        add(Vertex(f"node:{n.name}", "node", n.name, n.name, UP if d else DOWN))
        for s in (d or {}).get("vllm") or []:
            sid = f"server:{n.name}/{s['name']}@{s['port']}"
            ok = s.get("status") == "ready" or (s.get("engine") == "ollama" and s.get("status") == "idle")   # loads on demand
            add(Vertex(sid, "server", f"{s['name']} :{s['port']}", n.name, UP if ok else DOWN,
                       [Req([f"node:{n.name}"], why=f"runs on {n.name}")]))
            replicas.setdefault(s["name"], []).append(sid)
        stacks: dict[str, int] = {}
        for c in ((services or {}).get(n.name) or {}).get("containers") or []:
            if c.get("state") == "running" and not _MODEL_IMAGE.search(c.get("image", "")) \
                    and (m := _STACK.match(c["name"])) and m[1] != "aiops":
                stacks[m[1]] = stacks.get(m[1], 0) + 1
        for name, count in stacks.items():
            add(Vertex(f"stack:{n.name}/{name}", "stack", f"{name} ({count} containers)", n.name, UP,
                       [Req([f"node:{n.name}"], why=f"runs on {n.name}")]))
    for model, sids in replicas.items():
        add(Vertex(f"model:{model}", "model", model, None, UP,
                   [Req(sids, partial=len(sids) > 1, why=f"{len(sids)} replica(s)")]))
    for alias, models in config.MODEL_ALIASES.items():
        live = [f"model:{m}" for m in models if m in replicas]
        add(Vertex(f"route:{alias}", "route", f"gateway route '{alias}'", None, UP if live else DOWN,
                   [Req(["component:gateway"], why="served by the gateway")]
                   + ([Req(live, partial=len(live) > 1, why=" then ".join(m.split(":", 1)[1] for m in live))] if live else [])))
    n2, n3 = "node:Node-2", "node:Node-3"
    direct = next((s for s in replicas.get(config.LLM_ENDPOINTS[-1].model, []) if s.startswith("server:Node-2/")), None)
    for cid, label, node, reqs in [
        ("gateway", "model gateway", n2, []),
        ("console", "console and API", n2, []),
        ("agent", "incident agent (diagnosis)", n2,
         [Req(["route:chat"] + ([direct] if direct else []), partial=bool(direct), why="chat route, or Node-2 directly")]),
        ("rag", "runbook retrieval", n2, [Req(["route:embed"], effect=DEGRADED, why="falls back to keyword search"),
                                          Req(["route:rerank"], effect=DEGRADED, why="falls back to embedding order")]),
        ("evals", "quality evaluations", n2, [Req([f"route:{a}"], effect=DEGRADED, why=f"{a} suite")
                                              for a in config.MODEL_ALIASES]),
        ("prometheus", "Prometheus (metric history)", n3, []),
        ("loki", "Loki (log search)", n3, []),
        ("grafana", "Grafana", n3, [Req(["component:prometheus"], why="reads Prometheus")]),
    ]:
        add(Vertex(f"component:{cid}", "component", label, node.split(":")[1], UP, [Req([node], why=f"runs on {node[5:]}")] + reqs))
    g["component:agent"].reqs.append(Req(["component:loki", "component:prometheus"], effect=DEGRADED, partial=True,
                                         why="log and metric evidence"))
    return g


def evaluate(g: dict[str, Vertex], forced_down: set[str] = frozenset()) -> dict[str, dict]:
    """State and reason of every vertex. Requirements form a DAG, so |V| passes reach the fixed point."""
    st = {v: (DOWN, "fails" if v in forced_down else "observed down") if v in forced_down or g[v].status == DOWN
          else (UP, "") for v in g}
    for _ in range(len(g)):
        changed = False
        for v in g.values():
            if v.id in forced_down or g[v.id].status == DOWN:
                continue
            state, why = UP, ""
            for r in v.reqs:
                members = [st[m][0] for m in r.any_of if m in st]
                if not members or all(s == DOWN for s in members):
                    s, w = r.effect, f"{r.why}: none available"
                elif r.partial and any(s != UP for s in members) or all(s != UP for s in members):
                    s, w = DEGRADED, f"{r.why}: " + ", ".join(f"{m.split(':', 1)[1]} {st[m][0]}" for m in r.any_of
                                                              if st.get(m, (UP,))[0] != UP)
                else:
                    continue
                if RANK[s] > RANK[state]:
                    state, why = s, w
            if st[v.id] != (state, why):
                st[v.id], changed = (state, why), True
        if not changed:
            break
    return {v: {"state": s, "why": w} for v, (s, w) in st.items()}


def impact(g: dict[str, Vertex], target: str) -> list[dict]:
    """Vertices that get worse if `target` fails (or stay degraded for a new reason, e.g. now on a fallback), worst first."""
    before, after = evaluate(g), evaluate(g, {target})
    out = [{"id": v, "type": g[v].type, "label": g[v].label, "node": g[v].node, "before": before[v]["state"], **after[v]}
           for v in g if v != target and (RANK[after[v]["state"]] > RANK[before[v]["state"]]
                                          or after[v]["state"] != UP and after[v]["why"] != before[v]["why"])]
    return sorted(out, key=lambda x: (-RANK[x["state"]], x["type"], x["id"]))


def view(g: dict[str, Vertex]) -> dict:
    states = evaluate(g)
    return {"vertices": [{"id": v.id, "type": v.type, "label": v.label, "node": v.node, **states[v.id],
                          "requires": [{"any_of": r.any_of, "effect": r.effect, "why": r.why} for r in v.reqs]}
                         for v in g.values()]}


def for_incident(kind: str, node: str, resource: str) -> str | None:
    """The graph vertex an incident is about, if any."""
    if kind.startswith(("node_", "gpu_", "high_", "disk_", "net_", "capacity_")) or kind == "config_drift":
        return f"node:{node}"
    if kind.startswith("model_") and "@" in resource:
        return f"server:{node}/{resource.partition(':')[0]}"
    if kind.startswith("svc_container_") and (m := _STACK.match(resource)):
        return f"stack:{node}/{m[1]}"
    return None
