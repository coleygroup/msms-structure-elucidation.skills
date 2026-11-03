# MCP execution modules

`server.py` exposes model tools, `tasks.py` bridges scientific environments, `jobs.py` stores durable requests, and `runner.py` executes one job. `pipeline.py` separates CPU preparation from GPU groups. `backends.py` manages local/Slurm execution, `kubernetes.py` finite Kubernetes Jobs, `fleet.py` endpoint selection and verified collection, and `endpoint.py` the file-transfer agent, and `metrics.py` sampled GPU activity.

Configuration and operational requirements are in [MCP execution](../../../docs/mcp-execution.md) and [prediction servers](../../../docs/prediction-servers.md). No site-specific endpoint configuration belongs in these modules. Test scheduling, transport failures, scientific cache reuse and terminal receipts before running large workloads.

`scheduling.py` owns detached local dispatch; `admission.py` manages production canaries and environment holds. Workers publish state without driving the scheduler. Compact live-job and parent/child indexes keep closed history out of scheduling scans.

`cpu.py` plans independent feature concurrency from explicit core/RAM budgets and reports preparation, scoring, prediction waits and scheduler queues separately. `plan_cpu_concurrency` and CLI `plan-cpu` are read-only; apply measured host settings through setup and a controlled coordinator reload.
