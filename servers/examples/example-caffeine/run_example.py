#!/usr/bin/env python3
"""
Example: PubChem lookup for caffeine (C8H10N4O2).

Demonstrates both tools in servers/pubchem.py without starting the MCP server.

Run from project root:
    # Env: default
    pixi run --environment default python servers/examples/example-caffeine/run_example.py
"""

import asyncio
import json
import sys
import unittest.mock

sys.path.insert(0, "servers")

import mcp.server.stdio as _s

_s.stdio_server = unittest.mock.MagicMock()

import pubchem as pc


async def main() -> None:
    print("=== pubchem_isomers: formula C8H10N4O2 (max 5) ===")
    r = await pc._isomers({"query": "C8H10N4O2", "max_results": 5})
    data = json.loads(r[0].text)
    for c in data["compounds"]:
        print(f"  CID {c['CID']:>7}  {c.get('IUPACName', '')[:50]}")
    print(f"  → {len(data['compounds'])} hits shown (2229 total in PubChem)")

    print("\n=== pubchem_compound: caffeine by name ===")
    r2 = await pc._compound({"identifier": "caffeine", "id_type": "name"})
    data2 = json.loads(r2[0].text)
    c = data2["compound"]
    print(f"  CID        {c['CID']}")
    print(f"  Formula    {c['MolecularFormula']}")
    print(f"  MW         {c['MolecularWeight']} Da")
    print(f"  SMILES     {c['ConnectivitySMILES']}")
    print(f"  InChIKey   {c['InChIKey']}")
    print(f"  Synonyms   {', '.join(c.get('Synonyms', [])[:5])}")


if __name__ == "__main__":
    asyncio.run(main())
