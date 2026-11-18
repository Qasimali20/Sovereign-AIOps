#!/usr/bin/env python3
"""Ansible dynamic inventory for the four nodes. Addresses come from the repository's untracked .env
(AIOPS_NODE_<n>="<lan-ip>,<tailscale-ip>"), so no address is stored in the repository.

Groups: fleet (all four), platform (Node-2: console, API, gateway), observability (Node-3: Prometheus, Grafana,
Loki), file_logs (nodes whose model units write log files instead of the journal)."""
import json
import socket
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parents[2] / ".env"
NAMES = {1: "spark-3369", 2: "spark-46ef", 3: "spark-214e", 4: "spark-b969"}


def addresses() -> dict[int, tuple[str, str]]:
    out = {}
    for line in ENV.read_text().splitlines() if ENV.exists() else []:
        key, sep, value = line.strip().partition("=")
        if sep and key.startswith("AIOPS_NODE_") and key[11:].isdigit():
            lan, _, ts = value.strip().strip("\"'").partition(",")
            out[int(key[11:])] = (lan.strip(), ts.strip())
    return out


def inventory() -> dict:
    hosts = {}
    for i, (lan, ts) in sorted(addresses().items()):
        hosts[f"Node-{i}"] = {"ansible_host": lan, "tailscale_ip": ts, "hostname": NAMES.get(i, ""), "node_name": f"Node-{i}"}
        if NAMES.get(i) == socket.gethostname():          # the controller itself: no SSH to ourselves
            hosts[f"Node-{i}"]["ansible_connection"] = "local"
    loki = hosts.get("Node-3", {}).get("ansible_host", "")
    platform_ip = hosts.get("Node-2", {}).get("ansible_host", "")
    return {
        "_meta": {"hostvars": hosts},
        "all": {"children": ["fleet"], "vars": {"loki_host": loki, "platform_ip": platform_ip, "repo_root": str(ENV.parent)}},
        "fleet": {"hosts": sorted(hosts)},
        "platform": {"hosts": [h for h in hosts if h == "Node-2"]},
        "observability": {"hosts": [h for h in hosts if h == "Node-3"]},
        "file_logs": {"hosts": [h for h in hosts if h == "Node-4"]},
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--host":
        print(json.dumps(inventory()["_meta"]["hostvars"].get(sys.argv[2], {})))
    else:
        print(json.dumps(inventory(), indent=1))
