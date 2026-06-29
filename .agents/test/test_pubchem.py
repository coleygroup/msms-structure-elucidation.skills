"""
Self-check for servers/pubchem.py — run with:
    pixi run --environment default python .agents/test/test_pubchem.py
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
    failures = []

    # 1. isomers by formula (async ListKey path)
    r = await pc._isomers({"query": "C8H10N4O2", "max_results": 5})
    data = json.loads(r[0].text)
    assert data["success"], f"isomers/formula failed: {data}"
    assert len(data["compounds"]) > 0, "isomers/formula: no compounds returned"
    cids = [c["CID"] for c in data["compounds"]]
    assert 2519 in cids, f"caffeine CID 2519 missing from isomers: {cids}"
    print(f"[PASS] isomers/formula  — {len(data['compounds'])} hits, caffeine present")

    # 2. isomers by SMILES
    r2 = await pc._isomers(
        {
            "query": "Cn1cnc2c1c(=O)n(c(=O)n2C)C",
            "query_type": "smiles",
            "max_results": 3,
        }
    )
    data2 = json.loads(r2[0].text)
    assert data2["success"], f"isomers/smiles failed: {data2}"
    assert any(c["CID"] == 2519 for c in data2["compounds"]), (
        "caffeine not found by SMILES"
    )
    print(f"[PASS] isomers/smiles   — caffeine found by SMILES")

    # 3. compound by name
    r3 = await pc._compound({"identifier": "caffeine", "id_type": "name"})
    data3 = json.loads(r3[0].text)
    assert data3["success"], f"compound/name failed: {data3}"
    c = data3["compound"]
    assert c["InChIKey"] == "RYYVLZVUVIJVGH-UHFFFAOYSA-N", (
        f"wrong InChIKey: {c['InChIKey']}"
    )
    assert "caffeine" in [s.lower() for s in c.get("Synonyms", [])], (
        "caffeine not in synonyms"
    )
    print(f"[PASS] compound/name    — InChIKey correct, synonyms present")

    # 4. compound by InChIKey
    r4 = await pc._compound(
        {"identifier": "RYYVLZVUVIJVGH-UHFFFAOYSA-N", "id_type": "inchikey"}
    )
    data4 = json.loads(r4[0].text)
    assert data4["success"], f"compound/inchikey failed: {data4}"
    assert data4["compound"]["CID"] == 2519, f"wrong CID: {data4['compound']['CID']}"
    print(f"[PASS] compound/inchikey — CID 2519 correct")

    # 5. unknown formula → empty compounds, no crash
    r5 = await pc._isomers({"query": "C99H99N99O99", "max_results": 5})
    data5 = json.loads(r5[0].text)
    assert data5["success"], f"unknown formula should succeed with empty list: {data5}"
    assert data5["compounds"] == [], f"expected empty list, got: {data5['compounds']}"
    print(f"[PASS] isomers/unknown  — empty list, no crash")

    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
