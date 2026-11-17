# Resource pressure

## Memory
high_memory fires at 92% of unified memory. On this fleet the usual cause is model servers (each reserves its
memory fraction at start) plus other projects' containers on the same node (Node-1 and Node-3 are shared with
other applications). Check gpu_apps and model servers before suspecting a leak; a leak climbs over time.

## Disk
disk_usage fires at 85%. Usual consumers: Docker images and stopped containers, model weights under /models or
the Hugging Face cache, and logs. Recommend docker system df and du on the model directories; never delete
anything automatically.

## CPU
high_cpu fires when CPU stays at or above 90% for about 3 minutes. GB10 inference runs on the GPU, so sustained
high CPU usually means a CPU-bound workload (tokenisation for very large batches, a speech pre-processing worker,
a build, or a runaway process). Identify the process with top on the node; escalate, do not kill.
