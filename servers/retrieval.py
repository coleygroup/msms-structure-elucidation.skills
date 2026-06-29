"""
MCP server — spectral database retrieval.

Runs in the `retrieval` pixi environment.

Start manually:
    pixi run --environment retrieval python servers/retrieval.py
"""

import json
import subprocess
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
                "Search a spectral reference database for candidate structures matching "
                "an experimental MS/MS spectrum using cosine similarity or learned embeddings. "
                "Best suited for: known compound identification where the molecule (or a close "
                "analogue) is likely present in spectral libraries such as NIST, MassBank, or "
                "GNPS. Excels at metabolomics, food safety, environmental monitoring, and "
                "pharmaceutical QC. Less useful for truly novel compounds or natural product "
                "unknowns not covered by public libraries. "
                "Returns top-k candidate structures with SMILES, similarity scores, and DB IDs."
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
                        "description": "Path for output JSON file with ranked candidates",
                    },
                    "db_path": {
                        "type": "string",
                        "description": "Path to spectral database (overrides config)",
                        "default": "",
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
        "pixi",
        "run",
        "--environment",
        "retrieval",
        "python",
        str(SCRIPT),
        "--spectrum",
        arguments["spectrum"],
        "--output",
        arguments["output"],
        "--top_k",
        str(arguments.get("top_k", 10)),
    ]
    if arguments.get("db_path"):
        cmd += ["--db_path", arguments["db_path"]]

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
