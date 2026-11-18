# Storage and network

## Disk I/O saturated
disk_io_saturated fires when a node's NVMe drive has I/O in flight for 95% or more of the time for about 15
minutes. Every node has one NVMe drive holding the OS, Docker and the model weights. Loading a large model
(a 35B FP8 model is about 35 GB, Qwen3-Omni about 70 GB) saturates the drive for several minutes, and a large
download writes continuously, so check first for a model server in "loading" state or a download in progress
(get_model_servers, get_node_logs). Saturation that continues with no load or download points to swapping
(memory pressure: check memory), a runaway logger, or a failing drive (check disk_error incidents and the kernel
messages in get_disk_and_network). Recommend identifying the process with iotop or pidstat on the node; never stop
processes automatically.

## Disk errors in the kernel log
disk_error is raised from kernel messages: NVMe controller timeouts or resets, block I/O errors, filesystem errors,
or a filesystem remounted read-only. A filesystem remounted read-only stops every service that writes (Docker,
model servers writing caches, logs): treat it as critical. An NVMe timeout or reset that occurs once may be
transient; repeated ones mean the drive or its firmware is failing. Recommend: back up anything not replicated
(the platform database is backed up to Node-3), check drive health with `sudo nvme smart-log /dev/nvme0` (media
errors, percentage used, critical warning) and escalate to hardware support. These incidents do not clear on their
own; a person closes them after checking the drive.

## Network interface errors
net_errors fires when a physical interface's receive or transmit error counters rise on three consecutive scans
(about a minute). Errors (not drops) mean frames were corrupted or could not be sent: on the ConnectX ports
(enp1s0f*, enP2p1s0f*) suspect the cable or transceiver of the direct node-to-node link; on Wi-Fi, interference.
Drops alone are normal (frames for protocols the node does not use) and are only exported as metrics.

## Link down
net_link_down fires when a physical interface that was up goes down. Roles on this fleet: wlP9s9 is Wi-Fi and
carries every node's office network and internet traffic, including SSH, the console and model downloads. The
ConnectX ports enp1s0f1np1 and enP2p1s0f1np1 are the direct links between nodes, used for multi-node model serving
(tensor parallel over RoCE). enP7s7 (wired Ethernet) and the other ConnectX ports are unplugged on every node, so
they are never up and never alert. A Wi-Fi link down usually means the node is also unreachable from the platform;
a ConnectX link down breaks any model served across two nodes. If the link was taken down on purpose, close the
incident: the platform stops tracking that interface.

## NIC reset
net_nic_reset is raised from "NETDEV WATCHDOG" kernel messages: the driver found the transmit queue stuck and reset
the interface. One occurrence is usually recovered by the driver; repeated ones point to the driver, firmware or
hardware. Recommend checking the driver and firmware versions and escalating if it repeats.

## Wi-Fi signal
net_wifi_degraded fires when the averaged Wi-Fi signal stays at or below -75 dBm for about 3 minutes. Every node
normally sits between -60 and -68 dBm. A weak signal lowers the bit rate and raises retries, which slows model
downloads, log shipping and the console, and can drop SSH sessions. Usual causes: the access point was moved or
restarted, interference, or the node was moved. Retries and failed frames are exported as metrics
(aiops_wifi_tx_retries_total, aiops_wifi_tx_failed_total) for comparison with the node's own history.
