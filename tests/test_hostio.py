"""Disk I/O, NIC errors, link state, Wi-Fi and storage/NIC kernel messages: parsing, rates and rules."""
from backend.core import config
from backend.incidents import detector, runtime
from backend.monitoring import hostio, metrics

N = config.NODE_BY_NAME["Node-4"]
DISKSTATS = (" 259       0 nvme0n1 1000 0 2000 10 500 0 4000 20 0 {io} 30 0 0 0 0 0 0\n"
             " 259       1 nvme0n1p1 10 0 20 1 5 0 40 2 0 3 3 0 0 0 0 0 0\n"
             "   7       0 loop0 1 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
NETDEV = ("Inter-|   Receive                                                |  Transmit\n"
          " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
          "    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
          "wlP9s9: 9000 90 {rxerr} 4 0 0 0 0 8000 80 0 1 0 0 0 0\n"
          "docker0: 1 1 5 5 0 0 0 0 1 1 5 5 0 0 0 0\n")
LINKS = "wlP9s9 up\nenp1s0f1np1 {cx}\nenP7s7 down\ndocker0 down\nveth1 up\n"
WIFI = ("iface wlP9s9\nStation aa:bb (on wlP9s9)\n\ttx retries:\t7025\n\ttx failed:\t2\n\tbeacon loss:\t0\n"
        "\tsignal:  \t-65 [-73, -65] dBm\n\tsignal avg:\t{sig} [-72, -62] dBm\n\ttx bitrate:\t286.7 MBit/s HE-MCS 11\n"
        "\tconnected time:\t7795 seconds\n")
KERNEL = ("1794985000.100000 spark-b969 kernel: nvme nvme0: I/O 12 QID 3 timeout, aborting\n"
          "1794985001.200000 spark-b969 kernel: EXT4-fs (nvme0n1p2): Remounting filesystem read-only\n"
          "1794985002.300000 spark-b969 kernel: mlx5_core 0000:01:00.1 enp1s0f1np1: Link is Down\n"
          "1794985003.400000 spark-b969 kernel: NETDEV WATCHDOG: wlP9s9 (mt7925e): transmit queue 0 timed out\n")


def raw(io=100, rxerr=0, cx="up", sig=-62, kernel=""):
    s = hostio._SPLIT
    return f"{DISKSTATS.format(io=io)}{s}\n{NETDEV.format(rxerr=rxerr)}{s}\n{LINKS.format(cx=cx)}{s}\n{WIFI.format(sig=sig)}{s}\n{kernel}"


def sample(t, **kw):
    return {"ok": True, "t": t, **hostio.parse(raw(**kw))}


def test_parse_keeps_whole_disks_and_physical_interfaces_only():
    p = hostio.parse(raw())
    assert list(p["disks"]) == ["nvme0n1"] and p["disks"]["nvme0n1"]["read_bytes"] == 2000 * 512
    assert list(p["nics"]) == ["wlP9s9"] and p["nics"]["wlP9s9"]["rx_dropped"] == 4
    assert p["links"] == {"wlP9s9": "up", "enp1s0f1np1": "up", "enP7s7": "down"}
    assert p["wifi"]["wlP9s9"] | {} == {"tx_retries": 7025, "tx_failed": 2, "beacon_loss": 0, "signal_dbm": -65,
                                         "signal_avg_dbm": -62, "tx_mbps": 286.7, "connected_s": 7795}


def test_kernel_messages_classified_by_problem_and_device():
    ks = hostio.parse(raw(kernel=KERNEL))["kernel"]
    assert [(k["what"], k["device"]) for k in ks] == [
        ("NVMe controller timeout or reset", "nvme0"), ("filesystem remounted read-only", "nvme0n1p2"),
        ("network link down", "enp1s0f1np1"), ("NIC transmit watchdog timeout", "wlP9s9")]
    kinds = {(a.kind, a.resource) for a in detector.io_kernel_alerts(N, ks)}
    assert kinds == {("disk_error", "nvme0"), ("disk_error", "nvme0n1p2"), ("net_nic_reset", "wlP9s9")}   # link: from state


def test_rates_and_counter_reset():
    a, b = sample(0, io=100), sample(20, io=100 + 19_500)
    r = hostio.rates(a, b)
    assert abs(r["disks"]["nvme0n1"]["busy_ratio"] - 0.975) < 1e-9
    assert hostio.rates(b, sample(40, io=5))["disks"]["nvme0n1"]["busy_ratio"] is None     # reboot: counter went back
    assert hostio.rates(None, a) == {"disks": {}, "nics": {}, "wifi": {}}


def test_disk_saturation_needs_long_sustain():
    streak, up = {}, set()
    out = []
    for i in range(config.DISK_BUSY_SCANS):
        a, b = sample(i * 20, io=i * 20_000), sample(i * 20 + 20, io=(i + 1) * 20_000)
        out = detector.io_alerts(N, b, hostio.rates(a, b), streak, up)
        if i < config.DISK_BUSY_SCANS - 1:
            assert not [x for x in out if x.kind == "disk_io_saturated"]
    assert [x.resource for x in out if x.kind == "disk_io_saturated"] == ["nvme0n1"]


def test_nic_errors_link_down_and_weak_wifi():
    streak, up = {}, set()
    prev = sample(0)
    for i in range(1, config.NET_ERROR_SCANS + 1):
        cur = sample(i * 20, rxerr=i * 3)
        out = detector.io_alerts(N, cur, hostio.rates(prev, cur), streak, up)
        prev = cur
    assert [a.resource for a in out if a.kind == "net_errors"] == ["wlP9s9"]
    # a link seen up that goes down alerts; one never seen up (unplugged port) never does
    down = sample(100, cx="down")
    out = detector.io_alerts(N, down, hostio.rates(prev, down), streak, up)
    (a,) = [a for a in out if a.kind == "net_link_down"]
    assert a.resource == "enp1s0f1np1" and "ConnectX" in a.detail["role"]
    for i in range(config.SUSTAIN_SCANS):
        out = detector.io_alerts(N, sample(200 + i, sig=-80), {"disks": {}, "nics": {}, "wifi": {}}, streak, up)
    assert [a.kind for a in out] == ["net_wifi_degraded"]


def test_io_incident_cleared_only_with_a_fresh_sample():
    d = detector.DetectorState()
    d.reachable["Node-4"] = True
    i = {"kind": "net_link_down", "node": "Node-4", "resource": "enp1s0f1np1", "fingerprint": "net_link_down:Node-4:enp1s0f1np1"}
    assert runtime.condition_cleared(i, d, {}, set()) is None            # collector not read: unknown, not cleared
    d.io_ok["Node-4"] = True
    assert "link up" in runtime.condition_cleared(i, d, {}, set())


def test_exporter_renders_io_series():
    a, b = sample(0, io=100), sample(20, io=10_100)
    text = metrics.render({}, {}, {"incidents": {}, "approvals_pending": 0, "policy": {}, "executions": {}, "verifications": {}},
                          {}, {"Node-4": {"ok": True, "sample": b, "rates": hostio.rates(a, b)}})
    assert 'aiops_disk_busy_ratio{node="Node-4",device="nvme0n1"} 0.5' in text
    assert 'aiops_network_link_up{node="Node-4",interface="enP7s7",state="down"} 0' in text
    assert 'aiops_wifi_signal_dbm{node="Node-4",interface="wlP9s9"} -62' in text
    assert 'aiops_network_errors_total{node="Node-4",interface="wlP9s9",direction="rx"} 0' in text
