"""Sovereign AI Ops backend: FastAPI + LangGraph. Entry points: backend.web:app (console and API),
backend.llmops.gateway:app (model gateway), python -m backend.core.backup."""
import os as _os
from pathlib import Path as _Path


def _load_fleet_addresses() -> None:
    """The fleet's addresses live in .env (not in the repository). systemd passes .env to the services; this makes
    the same addresses available to tests and scripts run by hand. Only fleet-address keys are read."""
    env = _Path(__file__).resolve().parents[1] / ".env"
    try:
        lines = env.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and (key.startswith("AIOPS_NODE_") or key == "AIOPS_LAN_NET"):
            _os.environ.setdefault(key, value.strip().strip("\"'"))


_load_fleet_addresses()
