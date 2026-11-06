"""Score supplied structures and prepare viewer payloads in the scientific CPU runtime."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from msms_structure_elucidation.mcp.jobs import read_json
from msms_structure_elucidation.mcp.tasks import score_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job',required=True)
    args = parser.parse_args()
    folder = Path(args.job)
    config = read_json(folder/'config.json')
    config['_mcp_native_score'] = True
    result = score_candidates(read_json(folder/'request.json')['params'],folder,config,None,sys.stderr)
    print('RESULT_JSON='+json.dumps(result,allow_nan=False))


if __name__=='__main__':
    main()
