# Node availability

## Unreachable versus rebooted
node_unreachable means Spark Monitor on the node did not answer three consecutive polls (about a minute).
The office LAN on all nodes is Wi-Fi; the platform falls back to Tailscale. Unreachable on both paths means the
node is down, rebooting, or off the network. node_rebooted means uptime went backwards: the node restarted.
After a restart, models get 15 minutes to load before 'missing' alerts.

## Power loss behaviour
On power return the platform services (systemd user units with linger), Docker containers with restart policy
unless-stopped/always, and the model systemd units start by themselves. A cold boot also clears a GSP (Xid 119)
hang. Check after a restart: all expected models back, kernel journal free of Xid, containers healthy.
