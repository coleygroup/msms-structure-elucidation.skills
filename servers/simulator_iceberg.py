"""
MCP server — ICEBERG MS/MS simulator (ms-pred, coleygroup).

Runs in the `simulator-iceberg` pixi environment. The CLI launches this as a
subprocess and connects to it via stdio MCP transport.

Start manually:
    pixi run --environment simulator-iceberg python servers/simulator_iceberg.py
"""

import json
import subprocess
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPT = (
    PROJECT_ROOT
    / ".agents"
    / "skills"
    / "msms-sim-iceberg"
    / "scripts"
    / "predict_msms.py"
)

server = Server("simulator-iceberg")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="simulate_spectrum_iceberg",
            description=(
                "Predict an MS/MS spectrum for a candidate molecule using the ICEBERG "
                "two-stage DAG + intensity GNN simulator (Coley group, ms-pred). "
                "Best suited for: small organic molecules, natural products, lipids, "
                "and drug-like compounds with well-defined fragmentation pathways. "
                "Returns predicted peaks (m/z, intensity), fragment SMILES assignments, "
                "and a plot path. Use this to verify that a candidate SMILES is consistent "
                "with an observed experimental spectrum, or to generate reference spectra "
                "for database-building. Requires downloaded ICEBERG checkpoints."
            ),
            inputSchema={
                "type": "object",
                "required": ["smiles", "output_dir"],
                "properties": {
                    "smiles": {
                        "type": "string",
                        "description": "Candidate molecule as a SMILES string",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Directory to write spectrum.png, fragments.json, input_configs.yaml",
                    },
                    "gen_ckpt": {
                        "type": "string",
                        "description": "Path to ICEBERG generator checkpoint (.ckpt). Defaults to downloads/iceberg_dag_gen_msg_best.ckpt",
                        "default": "downloads/iceberg_dag_gen_msg_best.ckpt",
                    },
                    "inten_ckpt": {
                        "type": "string",
                        "description": "Path to ICEBERG intensity checkpoint (.ckpt). Defaults to downloads/iceberg_dag_inten_msg_best.ckpt",
                        "default": "downloads/iceberg_dag_inten_msg_best.ckpt",
                    },
                    "collision_energies": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Collision energies in eV (default: [20, 40])",
                        "default": [20, 40],
                    },
                    "adduct": {
                        "type": "string",
                        "description": "Ionization adduct, e.g. [M+H]+ or [M-H]- (default: [M+H]+)",
                        "default": "[M+H]+",
                    },
                    "instrument": {
                        "type": "string",
                        "enum": ["Orbitrap", "QTOF"],
                        "description": "Instrument type (default: Orbitrap)",
                        "default": "Orbitrap",
                    },
                    "cuda_devices": {
                        "type": "string",
                        "description": "CUDA device IDs e.g. '0' or '0,1'. Omit or null for CPU.",
                    },
                },
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "simulate_spectrum_iceberg":
        raise ValueError(f"Unknown tool: {name}")

    smiles = arguments["smiles"]
    output_dir = Path(arguments["output_dir"])
    gen_ckpt = Path(
        arguments.get("gen_ckpt", "downloads/iceberg_dag_gen_msg_best.ckpt")
    )
    inten_ckpt = Path(
        arguments.get("inten_ckpt", "downloads/iceberg_dag_inten_msg_best.ckpt")
    )
    collision_energies = arguments.get("collision_energies", [20, 40])
    adduct = arguments.get("adduct", "[M+H]+")
    instrument = arguments.get("instrument", "Orbitrap")
    cuda_devices = arguments.get("cuda_devices")

    cmd = [
        "pixi",
        "run",
        "--environment",
        "simulator-iceberg",
        "python",
        str(SCRIPT),
        "--smiles",
        smiles,
        "--gen_ckpt",
        str(gen_ckpt),
        "--inten_ckpt",
        str(inten_ckpt),
        "--collision_energies",
        *[str(e) for e in collision_energies],
        "--adduct",
        adduct,
        "--instrument",
        instrument,
        "--output_dir",
        str(output_dir),
    ]
    if cuda_devices:
        cmd += ["--cuda_devices", cuda_devices]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

    fragments_path = output_dir / "fragments.json"
    fragments = None
    if fragments_path.exists():
        with open(fragments_path) as f:
            fragments = json.load(f)

    if result.returncode != 0:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "error": result.stderr,
                        "stdout": result.stdout,
                    }
                ),
            )
        ]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "smiles": smiles,
                    "adduct": adduct,
                    "instrument": instrument,
                    "collision_energies": collision_energies,
                    "output_dir": str(output_dir),
                    "spectrum_plot": str(output_dir / "spectrum.png"),
                    "fragments": fragments,
                    "stdout": result.stdout,
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
