"""Fail if a tracked file holds a real network address or a credential.

Fleet addresses live only in the untracked .env; files in the repository use placeholders. Allowed addresses:
the placeholder ranges the code uses (10.0.0.x LAN, 100.64.0.x Tailscale), loopback and wildcard, the
documentation ranges used by the security lab (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), network masks
and CIDR bases, and a few well-known example addresses used in tests.
Run: python ops/scripts/check_repo_hygiene.py (CI runs it on every push)."""
from __future__ import annotations

import ipaddress
import re
import subprocess
import sys

ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/24", "100.64.0.0/24", "127.0.0.0/8", "0.0.0.0/32", "255.255.255.0/24",
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4",
    # well-known examples in tests: public resolvers, Docker's default bridge, a generic home LAN
    "8.8.8.8/32", "1.1.1.1/32", "172.17.0.0/16", "192.168.1.0/24")]
# network bases written as CIDR in docs and config (e.g. 100.64.0.0/10, 172.16.0.0/12) are ranges, not hosts
CIDR_OK = re.compile(r"^(10\.0\.0\.0|100\.64\.0\.0|172\.16\.0\.0|172\.17\.0\.0|192\.168\.0\.0|169\.254\.0\.0|0\.0\.0\.0)$")
IPV4 = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(/\d{1,2})?(?![\w.])")
SECRETS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]
SKIP = re.compile(r"(^|/)(package-lock\.json|uv\.lock)$|\.(png|jpe?g|gif|ico|woff2?|wav|pdf)$")


def problems(path: str, text: str) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in IPV4.finditer(line):
            ip, cidr = m.group(1), m.group(2)
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue                                  # version strings like 580.159.03.1 are not addresses
            if any(addr in net for net in ALLOWED_NETS) or (cidr and CIDR_OK.match(ip)):
                continue
            out.append(f"{path}:{n}: address {ip}{cidr or ''}")
        for what, rx in SECRETS:
            if rx.search(line):
                out.append(f"{path}:{n}: {what}")
    return out


def main() -> int:
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.splitlines()
    found = []
    for f in files:
        if SKIP.search(f):
            continue
        try:
            text = open(f, encoding="utf-8").read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        found += problems(f, text)
    for p in found:
        print(p)
    print(f"{len(files)} files checked, {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
