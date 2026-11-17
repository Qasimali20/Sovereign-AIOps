# GPU stall (baseline deviation)

## Idle GPU with requests in flight
gpu_stalled fires when a node's GPU reports 0% utilisation for about 3 minutes while a model server on it has
requests running. A healthy server with work in flight keeps the GPU busy, so this deviates from the node's
normal behaviour. Most likely causes: a hung GPU (check get_gpu_kernel_events for Xid 119/154/79), a deadlocked
engine (multi-node tensor parallel waiting on a peer), or a client holding a stream open without reading.
Requests in flight with an idle GPU and no Xid point to the engine; with an Xid, to the GPU (power-off).
The rule counts requests in flight only while the server completes none: queue counts are compared with the
server's resting level (what it reported when it last finished a request). vLLM-Omni TTS servers leave finished
requests counted as running or waiting after busy periods; those leftovers are the resting level and never alert.
The alert detail lists requests_completed and resting_level for each server.
