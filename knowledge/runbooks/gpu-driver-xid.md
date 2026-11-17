# GPU driver faults (Xid)

## Xid code meanings
GPU driver Xid codes (kernel journal, NVRM): 119 = GSP firmware RPC timeout (GPU hung); 154 = driver
requests a recovery action such as 'GPU Reset Required'; 79 = GPU fell off the bus; 48/95 = uncorrectable
ECC. These are node-level hardware/firmware failures: restarting services does not fix them, and on GB10
the reset is a full power-off. A flood of 'bad register read ... 0xbadf5600' lines accompanies a hung GSP.
13/31/43 are usually caused by the application named in the event.

## Current boot means active
GPU driver evidence covers the CURRENT boot only. Any Xid 119/154/79/48/95 found there is an ACTIVE
failure: the GPU has not been reset since, and it stays unusable or unreliable until a full power-off,
even if utilisation, temperature and model-server status look normal. Never treat it as resolved.

## Known triggers and recovery on this fleet
Known triggers: the desktop session (Xorg / gnome-control-center) and vLLM model loads. A service restart or
warm reboot has not cleared Xid 119 here; a full power-off (cold boot: shut down, remove power ~30 s, power
on) has. A vLLM load stuck after a few weight shards (for example 2 of 42) is the typical symptom.
