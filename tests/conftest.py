"""Record the unit-test outcome as an evaluation run (shown on the dashboard)."""
import os

# a test that starts the app must never start the background detector: it would scan the live fleet, open incidents
# and run scheduled model evaluations against real servers
os.environ.setdefault("AIOPS_DETECTOR", "0")


def pytest_sessionfinish(session, exitstatus):
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr is None or session.config.option.collectonly:
        return
    passed = len(tr.stats.get("passed", []))
    failed = len(tr.stats.get("failed", [])) + len(tr.stats.get("error", []))
    if passed + failed == 0 or session.config.args not in (["tests"], [], [str(session.config.rootpath)]) \
            and not any(a.rstrip("/").endswith("tests") for a in session.config.args):
        return                          # only full-suite runs are recorded, not single-file runs
    from backend.core import evals
    evals.record("unit-tests", passed, passed + failed, meta={"exit_status": int(exitstatus)})
