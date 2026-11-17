# Model serving (vLLM)

## Load times and status
A healthy vLLM load of the 35B FP8 model reaches 'ready' in about 4 minutes; small models (ASR, TTS,
embedding, reranker) in under 2. Servers report status 'loading' until /v1/models answers. A load stuck far
longer than 15 minutes, especially stalled after a few weight shards, has previously been a GPU firmware
(GSP, Xid 119) hang that only a full power-off cleared; check get_gpu_kernel_events first.

## Model server disappeared
model_server_missing means a server that was serving is no longer visible to Spark Monitor (its process is
gone). Causes in order of frequency here: an operator stopped it (docker stop / systemctl stop), the process
crashed (check the unit journal or container logs), the node rebooted and the model has not come back. Model
units on Node-1, Node-2 and Node-4 are systemd services enabled at boot; Node-3 models are Docker containers with
restart policy unless-stopped. The platform never restarts production models: escalate with the exact unit or
container name.

## High first-token latency
model_latency_high means p95 time-to-first-token has been above the limit for about 3 minutes with at least 2
requests a minute arriving (below that the p95 is one or two requests, often the console agent's own long
diagnosis prompts). Usual causes:
queueing (waiting > 0: concurrency above what the server admits), KV cache near 100% (long prompts or many
parallel requests evict and pre-empt), a co-located heavy workload on the same GPU, or a stalling GPU (see GPU
stall). Compare running, waiting and kv_cache in the server record; the gateway can spread load to a replica
for aliases with more than one model (chat).

## Queue stuck
model_queue_stuck means requests are waiting and none running: the engine has stopped scheduling. This is a
hang, not load. Check GPU driver events and GPU utilisation; if the GPU is idle with requests queued, treat as
an engine or GPU hang and escalate. It fires only when requests wait above the server's resting level for about 3
minutes while no request completes, so leftover counts from vLLM-Omni (which does not always clear finished
requests from its gauges) do not raise it.

## Quality regression
quality_regression means a model's newest evaluation score fell well below its own recent level (at least 25
points under the median of its last runs, and under 75%). The platform evaluates every model every 6 hours with
small fixed test sets: chat answers checked exactly, embedding and reranker retrieval of the right document,
transcription and OCR error rates against known text, and speech round-tripped through whisper-turbo. The
incident detail lists the failed cases with what the model answered. First establish whether the model changed:
the Models registry shows when a server's identity last changed (model path, context length, engine version), and
a restart or a new deployment just before the drop is the usual cause (wrong checkpoint, changed quantisation,
changed chat template or sampling defaults). A drop on one case only can be a flaky check rather than a real
regression: compare with the previous run's answers. Then check whether the server is under memory pressure or
pre-empting (KV cache near 100%), which truncates answers. The platform never redeploys or rolls back production
models: escalate with the model, node, the failed cases and when the identity last changed.
