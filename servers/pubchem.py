"""
MCP server — PubChem structure lookup.

Runs in the `default` pixi environment (no extra deps needed beyond requests).

Start manually:
    pixi run python servers/pubchem.py
"""

import json
import urllib.request
import urllib.parse
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("pubchem")

PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"


def _get(url: str) -> dict | list | None:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="pubchem_isomers",
            description=(
                "Fetch structural isomers and synonyms from PubChem for a given molecular "
                "formula or SMILES. Useful for expanding the candidate set when retrieval and "
                "de novo agree on a formula but not a specific structure, or for finding all "
                "database-known compounds with a given precursor mass. "
                "Returns CID, canonical SMILES, InChIKey, molecular formula, and IUPAC name "
                "for each match."
            ),
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Molecular formula (e.g. C8H10N4O2) or SMILES string",
                    },
                    "query_type": {
                        "type": "string",
                        "enum": ["formula", "smiles"],
                        "description": "How to interpret the query (default: formula)",
                        "default": "formula",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of hits to return (default: 20)",
                        "default": 20,
                    },
                },
            },
        ),
        Tool(
            name="pubchem_compound",
            description=(
                "Look up a single compound in PubChem by CID, name, InChIKey, or SMILES. "
                "Returns canonical SMILES, InChIKey, molecular formula, molecular weight, "
                "IUPAC name, and synonyms. Use to validate or enrich a candidate structure "
                "identified by retrieval or de novo prediction."
            ),
            inputSchema={
                "type": "object",
                "required": ["identifier"],
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "CID (numeric), compound name, InChIKey, or SMILES",
                    },
                    "id_type": {
                        "type": "string",
                        "enum": ["cid", "name", "inchikey", "smiles"],
                        "description": "Identifier type (default: name)",
                        "default": "name",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "pubchem_isomers":
        return await _isomers(arguments)
    if name == "pubchem_compound":
        return await _compound(arguments)
    raise ValueError(f"Unknown tool: {name}")


async def _isomers(arguments: dict) -> list[TextContent]:
    query = arguments["query"]
    query_type = arguments.get("query_type", "formula")
    max_results = arguments.get("max_results", 20)

    encoded = urllib.parse.quote(query)
    if query_type == "formula":
        cids_url = f"{PUBCHEM_BASE}/compound/formula/{encoded}/cids/JSON"
    else:
        cids_url = f"{PUBCHEM_BASE}/compound/smiles/{encoded}/cids/JSON"

    try:
        cids_data = _get(cids_url)
    except Exception as e:
        return [
            TextContent(
                type="text", text=json.dumps({"success": False, "error": str(e)})
            )
        ]

    cids = cids_data.get("IdentifierList", {}).get("CID", [])[:max_results]
    if not cids:
        return [
            TextContent(
                type="text", text=json.dumps({"success": True, "compounds": []})
            )
        ]

    props_url = (
        f"{PUBCHEM_BASE}/compound/cid/{','.join(str(c) for c in cids)}"
        "/property/CanonicalSMILES,InChIKey,MolecularFormula,MolecularWeight,IUPACName/JSON"
    )
    try:
        props = _get(props_url)
    except Exception as e:
        return [
            TextContent(
                type="text", text=json.dumps({"success": False, "error": str(e)})
            )
        ]

    compounds = props.get("PropertyTable", {}).get("Properties", [])
    return [
        TextContent(
            type="text", text=json.dumps({"success": True, "compounds": compounds})
        )
    ]


async def _compound(arguments: dict) -> list[TextContent]:
    identifier = arguments["identifier"]
    id_type = arguments.get("id_type", "name")
    encoded = urllib.parse.quote(identifier)

    props_url = (
        f"{PUBCHEM_BASE}/compound/{id_type}/{encoded}"
        "/property/CanonicalSMILES,InChIKey,MolecularFormula,MolecularWeight,IUPACName/JSON"
    )
    synonyms_url = f"{PUBCHEM_BASE}/compound/{id_type}/{encoded}/synonyms/JSON"

    try:
        props = _get(props_url)
        props_list = props.get("PropertyTable", {}).get("Properties", [])
    except Exception as e:
        return [
            TextContent(
                type="text", text=json.dumps({"success": False, "error": str(e)})
            )
        ]

    synonyms = []
    try:
        syn_data = _get(synonyms_url)
        synonyms = (
            syn_data.get("InformationList", {})
            .get("Information", [{}])[0]
            .get("Synonym", [])[:10]
        )
    except Exception:
        pass

    result = props_list[0] if props_list else {}
    result["Synonyms"] = synonyms
    return [
        TextContent(type="text", text=json.dumps({"success": True, "compound": result}))
    ]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
