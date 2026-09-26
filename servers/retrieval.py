"""
MCP server — spectral database retrieval.

Runs in the `retrieval` Python environment.

Start manually:
    python servers/retrieval.py
"""

import json
import subprocess
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPT = PROJECT_ROOT / ".agents" / "skills" / "msms-retrieval" / "scripts" / "run.py"

server = Server("retrieval")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="retrieve_candidates",
            description=(
                "Rank candidates from the public ICEBERG PubChem atlas for an experimental .ms "
                "spectrum, using ms-pred entropy similarity. Public NIST structures are excluded."
            ),
            inputSchema={
                "type": "object",
                "required": ["spectrum", "output", "collision_unit"],
                "properties": {
                    "spectrum": {
                        "type": "string",
                        "description": "Path to query spectrum file in ms-pred .ms format",
                    },
                    "output": {
                        "type": "string",
                        "description": "Path for output JSON file with ranked candidates",
                    },
                    "collision_unit": {
                        "type": "string", "enum": ["NCE", "eV"],
                        "description": "User-confirmed unit of experimental collision-energy labels",
                    },
                    "formula": {
                        "type": "string",
                        "description": "Neutral molecular formula; inferred with MSBuddy when omitted",
                    },
                    "atlas_mgf": {
                        "type": "string",
                        "description": "Optional local formula MGF for offline retrieval",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of top candidates to return (default: 10)",
                        "default": 10,
                    },
                },
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "retrieve_candidates":
        raise ValueError(f"Unknown tool: {name}")

    cmd = [
        sys.executable,
        str(SCRIPT),
        "--spectrum",
        arguments["spectrum"],
        "--collision-unit",
        arguments["collision_unit"],
        "--output",
        arguments["output"],
        "--top-k",
        str(arguments.get("top_k", 10)),
    ]
    if arguments.get("formula"):
        cmd += ["--formula", arguments["formula"]]
    if arguments.get("atlas_mgf"):
        cmd += ["--atlas-mgf", arguments["atlas_mgf"]]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

    if result.returncode != 0:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "error": result.stderr,
                    }
                ),
            )
        ]

    output_path = Path(arguments["output"])
    candidates = None
    if output_path.exists():
        with open(output_path) as f:
            candidates = json.load(f)

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "output": arguments["output"],
                    "candidates": candidates,
                }
            ),
        )
    ]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
