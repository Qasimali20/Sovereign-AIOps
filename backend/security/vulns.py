"""CVE relevance from installed packages, decided deterministically and offline.

For each catalogue entry the verdict comes from the node itself:
  not_installed  the package is absent
  patched        the CVE id appears in the installed package's Debian changelog (Ubuntu backports fixes
                 without changing the upstream version, so the changelog is the authority, not the version)
  not_affected   the real upstream version is outside the affected range ('+really' versions resolved)
  vulnerable     in range and no fix recorded: an incident
The model never decides relevance; it explains a verdict that code already reached, with its sources."""
from __future__ import annotations

import json
import re

from backend.core import config
from backend.monitoring import kernel

CATALOGUE_PATH = config.ROOT / "knowledge" / "vulnerabilities" / "catalogue.json"


def catalogue() -> dict:
    return json.loads(CATALOGUE_PATH.read_text())


def upstream(version: str) -> str:
    """Debian version -> real upstream version: drop epoch and revision, resolve '+really'."""
    v = version.split(":", 1)[-1]
    if "+really" in v:
        v = v.split("+really", 1)[1]
    return re.split(r"[-+~]", v, maxsplit=1)[0]


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def assess(entry: dict, installed: str | None, changelog_mentions: int) -> dict:
    """Pure verdict for one CVE on one host."""
    base = {k: entry[k] for k in ("cve", "name", "package", "severity", "technique", "references")}
    if not installed:
        return base | {"status": "not_installed"}
    up = upstream(installed)
    if changelog_mentions > 0:
        return base | {"status": "patched", "installed": installed, "upstream": up,
                       "basis": f"{entry['cve']} is listed in the {entry['package']} changelog"}
    lo, hi = entry["affected_upstream"]
    if not (vkey(lo) <= vkey(up) < vkey(hi)):
        return base | {"status": "not_affected", "installed": installed, "upstream": up,
                       "basis": f"upstream {up} is outside the affected range [{lo}, {hi})"}
    return base | {"status": "vulnerable", "installed": installed, "upstream": up,
                   "basis": f"upstream {up} is within [{lo}, {hi}) and no fix is recorded in the changelog",
                   "summary": entry["summary"]}


def _command(entries: list[dict]) -> str:
    """One shell line per package: version (single-quoted format so the shell does not expand it)
    and, per CVE, how many times the package changelog mentions it."""
    parts = []
    for p in sorted({e["package"] for e in entries}):
        cves = " ".join(e["cve"] for e in entries if e["package"] == p)
        parts.append("v=$(dpkg-query -W -f='${Version}' " + p + " 2>/dev/null); "
                     "f=/usr/share/doc/" + p + "/changelog.Debian.gz; "
                     "for c in " + cves + "; do echo \"" + p + "|$v|$c|$(zgrep -c $c $f 2>/dev/null || echo 0)\"; done")
    return "; ".join(parts)


def scan_node(node: config.Node) -> dict:
    entries = catalogue()["entries"]
    try:
        out = kernel.run(kernel.access_for(node), _command(entries)).stdout
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "results": []}
    seen = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 4:
            seen[parts[2]] = (parts[1] or None, int(parts[3] or 0))
    results = [assess(e, *seen.get(e["cve"], (None, 0))) for e in entries]
    return {"ok": True, "results": results}


def scan_lab(packages: dict[str, str], patched: dict[str, list[str]]) -> dict:
    """The lab host's inventory lives in lab state (it is a simulated endpoint)."""
    return {"ok": True, "results": [assess(e, packages.get(e["package"]), int(e["cve"] in patched.get(e["package"], [])))
                                    for e in catalogue()["entries"]]}
