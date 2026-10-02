"""Start the MCP server over stdio: python -m msms_structure_elucidation.mcp [--backend local|slurm]."""
from __future__ import annotations

import argparse
import sys
import threading
import time

PUMP_SECONDS = 10


def _pump_forever(backend) -> None:
    """Start queued local jobs as GPUs free up, even while no client is polling."""
    while True:
        time.sleep(PUMP_SECONDS)
        backend.pump()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='msms-structure-elucidation mcp', description=__doc__)
    parser.add_argument('--backend', choices=['local', 'slurm'], help='job backend; default mcp.backend in the config')
    parser.add_argument('--config', help='workflow config; default configs/default.yaml overlaid with configs/local.yaml')
    parser.add_argument('--job-root', help='job directory root; default mcp.job_root')
    args = parser.parse_args(argv)
    from msms_structure_elucidation.mcp import server
    server.configure(args.backend, args.config, args.job_root)
    backend = server._RUNTIME['backend']
    if hasattr(backend, 'pump'):
        threading.Thread(target=_pump_forever, args=(backend,), daemon=True).start()
    server.server.run('stdio')
    return 0


if __name__ == '__main__':
    sys.exit(main())
