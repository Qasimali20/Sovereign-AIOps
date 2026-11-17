from backend.monitoring import boards
from backend.core import config

# fleet addresses come from .env (placeholders without it)
N3 = config.NODE_BY_NAME["Node-3"].lan_ip


def _fake(series):
    return lambda expr, start, end, step: series


def test_node_panels_keep_fixed_node_order_and_slots(monkeypatch):
    res = [{"metric": {"node": n}, "values": [[1, "1"], [2, "2"]]} for n in ("Node-3", "Node-1", "Node-4", "Node-2")]
    monkeypatch.setattr(boards.promql, "query_range", _fake(res))
    p = boards._panel(boards.Panel("cpu", "CPU", "x", "percent"), 0, 2, 1)
    assert [s["name"] for s in p["series"]] == ["Node-1", "Node-2", "Node-3", "Node-4"]
    assert [s["slot"] for s in p["series"]] == [0, 1, 2, 3]
    assert p["points"][0] == [0.0, None, None, None, None]          # grid start: explicit gap
    assert p["points"][1] == [1.0, 1.0, 1.0, 1.0, 1.0]


def test_model_panels_show_busiest_four_and_fold_the_rest(monkeypatch):
    res = [{"metric": {"model": f"org/m{i}"}, "values": [[1, str(i)]]} for i in range(7)]
    monkeypatch.setattr(boards.promql, "query_range", _fake(res))
    add = boards._panel(boards.Panel("t", "T", "x", "tps", by="model", additive=True), 0, 1, 1)
    assert [s["name"] for s in add["series"]] == ["m6", "m5", "m4", "m3", "Other"]
    assert add["points"][1][-1] == 0 + 1 + 2 and add["hidden"] == []
    lat = boards._panel(boards.Panel("l", "L", "x", "s", by="model"), 0, 1, 1)
    assert len(lat["series"]) == 4 and lat["hidden"] == ["m2", "m1", "m0"]


def test_nan_dropped_and_unnamed_upstreams_named_by_node(monkeypatch):
    res = [{"metric": {"model": f"?http://{N3}:8105"}, "values": [[1, "NaN"], [2, "3"]]}]
    monkeypatch.setattr(boards.promql, "query_range", _fake(res))
    p = boards._panel(boards.Panel("g", "G", "x", "s", by="model"), 0, 2, 1)
    assert p["series"][0]["name"] == "Node-3:8105 (no model name)"
    assert p["points"] == [[0.0, None], [1.0, None], [2.0, 3.0]]


def test_prometheus_failure_is_per_panel(monkeypatch):
    def boom(*a):
        raise RuntimeError("down")
    monkeypatch.setattr(boards.promql, "query_range", boom)
    p = boards._panel(boards.Panel("cpu", "CPU", "x", "percent"), 0, 1, 1)
    assert p["error"] and p["points"] == []


def test_timeline_marks_restart_where_uptime_drops(monkeypatch):
    up = [{"metric": {"node": "Node-1"}, "values": [[0, "1"], [60, "1"], [120, "1"]]}]
    uptime = [{"metric": {"node": "Node-1"}, "values": [[0, "5000"], [60, "5060"], [120, "30"]]}]
    monkeypatch.setattr(boards.promql, "query_range", lambda expr, *a: uptime if "uptime" in expr else up)
    p = boards._panel(boards.Panel("up", "A", "aiops_node_up", "short", kind="timeline"), 0, 120, 60)
    assert p["events"] == [{"series": 0, "t": 90.0, "kind": "restart"}]


def test_history_under_old_node_names_merges_into_new(monkeypatch):
    res = [{"metric": {"node": "DGX-1"}, "values": [[0, "5"]]}, {"metric": {"node": "Node-1"}, "values": [[1, "7"]]}]
    monkeypatch.setattr(boards.promql, "query_range", _fake(res))
    p = boards._panel(boards.Panel("cpu", "CPU", "x", "percent"), 0, 1, 1)
    assert [s["name"] for s in p["series"]] == ["Node-1"] and p["points"] == [[0.0, 5.0], [1.0, 7.0]]
