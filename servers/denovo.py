"""
MCP server — de novo structure prediction.

Runs in the `denovo` pixi environment.

Start manually:
    pixi run --environment denovo python servers/denovo.py
"""

import json
import subprocess
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPT = PROJECT_ROOT / ".agents" / "skills" / "msms-denovo" / "scripts" / "run.py"

server = Server("denovo")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="predict_structure_denovo",
            description=(
                "Generate candidate molecular structures de novo directly from an experimental "
                "MS/MS spectrum, without requiring a reference database. "
                "Best suited for: novel natural products, previously unreported metabolites, "
                "unknowns in environmental or toxicological samples, and any case where "
                "database retrieval returns no confident hit. Also valuable as a cross-check "
                "against retrieval results — agreement between retrieval and de novo on the "
                "same SMILES strongly supports a correct identification. "
                "More computationally expensive than retrieval; use when retrieval confidence "
                "is low or when genuine novelty is suspected. "
                "Returns top-k candidate SMILES with confidence scores."
            ),
            inputSchema={
                "type": "object",
                "required": ["spectrum", "output"],
                "properties": {
                    "spectrum": {
                        "type": "string",
                        "description": "Path to query spectrum file (mzML or MGF)",
                    },
                    "output": {
                        "type": "string",
                        "description": "Path for output JSON file with ranked candidate SMILES",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of candidate structures to generate (default: 10)",
                        "default": 10,
                    },
                    "checkpoint": {
                        "type": "string",
                        "description": "Model checkpoint path (overrides config)",
                        "default": "",
                    },
                },
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "predict_structure_denovo":
        raise ValueError(f"Unknown tool: {name}")

    cmd = [
        "pixi",
        "run",
        "--environment",
        "denovo",
        "python",
        str(SCRIPT),
        "--spectrum",
        arguments["spectrum"],
        "--output",
        arguments["output"],
        "--top_k",
        str(arguments.get("top_k", 10)),
    ]
    if arguments.get("checkpoint"):
        cmd += ["--checkpoint", arguments["checkpoint"]]

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
