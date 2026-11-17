---
always: true
---
# DGX Spark (GB10) hardware facts

## Unified memory
Nodes are NVIDIA DGX Spark (GB10) with 128 GB unified CPU/GPU memory. nvidia-smi does not report GPU
memory on GB10; GPU memory pressure shows up as system memory. vLLM servers reserve a fixed fraction
(mem_fraction / gpu_memory_utilization) at start, so high but flat memory use on a node serving models is
expected, not a leak. A leak shows as memory climbing over time with no new model loads.
