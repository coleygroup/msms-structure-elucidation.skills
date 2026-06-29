"""
Snowmageddon CLI — MS/MS structure elucidation agent.

Usage:
    snowmageddon run --input sample.raw
    snowmageddon run --input sample.mzML --mode cascade
    snowmageddon run --config configs/my_config.yaml
"""

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from snowmageddon.tracer import Tracer

DEFAULT_CONFIG = Path(__file__).parent.parent.parent / "configs" / "default.yaml"
SERVERS_DIR = Path(__file__).parent.parent.parent / "servers"
SKILLS = Path(__file__).parent.parent.parent / ".agents" / "skills"

MODEL = "claude-opus-4-8"

# One entry per MCP server: (server_script, pixi_env)
MCP_SERVERS = [
    (SERVERS_DIR / "simulator_iceberg.py", "simulator-iceberg"),
    (SERVERS_DIR / "retrieval.py", "retrieval"),
    (SERVERS_DIR / "denovo.py", "denovo"),
    (SERVERS_DIR / "pubchem.py", "default"),
]

SYSTEM_PROMPT = """\
You are an expert MS/MS structure elucidation agent. You have four tools:

- simulate_spectrum_iceberg: ICEBERG two-stage DAG + intensity GNN simulator.
  Best for drug-like molecules, natural products, lipids with well-defined fragmentation.
  Use to verify a candidate SMILES against observed spectra.

- retrieve_candidates: Spectral database search via cosine / learned embeddings.
  Best for known compounds likely present in NIST/MassBank/GNPS libraries —
  metabolomics, food safety, pharma QC. Start here for routine identification.

- predict_structure_denovo: De novo structure generation from spectrum alone.
  Best for novel natural products, unknowns absent from libraries.
  Use when retrieval returns no confident hit, or to cross-check retrieval.

- pubchem_isomers / pubchem_compound: PubChem lookup by formula, SMILES, name, or InChIKey.
  Use to expand candidate sets or validate/enrich identified structures.

Strategy:
1. For routine spectra, run retrieve_candidates first. If score ≥ 0.8, verify with
   simulate_spectrum_iceberg and report the result.
2. If retrieval confidence is low (< 0.8) or no hit found, run predict_structure_denovo.
3. Use pubchem_isomers/pubchem_compound to look up metadata or structural isomers as needed.
4. Rank all candidates. Report the top structure with confidence, model source, and caveats.

Always think through your reasoning before calling a tool.\
"""


def load_config(config_path: Path) -> dict:
    return yaml.safe_load(config_path.read_text())


async def _run_agent(spectrum_path: str, out: Path, cfg: dict) -> None:
    """Launch MCP servers, run the Anthropic agentic loop, write trace.jsonl."""
    from anthropic import Anthropic

    client = Anthropic()

    # Build MCP server params for each server
    server_params_list = []
    for script, env in MCP_SERVERS:
        server_params_list.append(
            {
                "command": "pixi",
                "args": ["run", "--environment", env, "python", str(script)],
                "cwd": str(SERVERS_DIR.parent),
            }
        )

    # Collect tools from all MCP servers by starting them and calling list_tools
    tools = await _collect_tools(server_params_list)

    trace_path = out / "trace.jsonl"
    with Tracer(trace_path) as tracer:
        tracer.event(
            "session_start",
            {
                "spectrum": spectrum_path,
                "output_dir": str(out),
                "model": MODEL,
                "tools": [t["name"] for t in tools],
            },
        )

        messages = [
            {
                "role": "user",
                "content": (
                    f"Elucidate the structure of the compound in this MS/MS spectrum: {spectrum_path}\n"
                    f"Write all outputs to: {out}"
                ),
            }
        ]

        # Agentic loop
        while True:
            response = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )

            # Trace every content block
            for block in response.content:
                if block.type == "thinking":
                    tracer.event("thinking", {"text": block.thinking})
                elif block.type == "text":
                    tracer.event("text", {"text": block.text})
                elif block.type == "tool_use":
                    tracer.event(
                        "tool_call",
                        {
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        },
                    )

            if response.stop_reason == "end_turn":
                # Extract final text
                final_text = next(
                    (b.text for b in response.content if b.type == "text"), ""
                )
                tracer.event("final_answer", {"text": final_text})
                click.echo("\n--- Elucidation result ---")
                click.echo(final_text)
                break

            if response.stop_reason != "tool_use":
                tracer.event("unexpected_stop", {"stop_reason": response.stop_reason})
                break

            # Execute all tool calls
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result_content = await _dispatch_tool(
                    block.name, block.input, server_params_list
                )
                tracer.event(
                    "tool_result",
                    {
                        "id": block.id,
                        "name": block.name,
                        "result": result_content,
                    },
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_content,
                    }
                )

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    click.echo(f"\nTrace saved: {trace_path}")


async def _collect_tools(server_params_list: list) -> list[dict]:
    """
    Start each MCP server briefly to collect its tool definitions, then return them
    as Anthropic tool dicts. Falls back gracefully if a server fails to start.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    all_tools = []
    for params in server_params_list:
        server_params = StdioServerParameters(
            command=params["command"],
            args=params["args"],
            env=None,
        )
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_response = await session.list_tools()
                    for t in tools_response.tools:
                        all_tools.append(
                            {
                                "name": t.name,
                                "description": t.description,
                                "input_schema": t.inputSchema,
                            }
                        )
        except Exception as exc:
            click.echo(
                f"Warning: could not connect to MCP server {params['args'][-1]}: {exc}",
                err=True,
            )

    return all_tools


async def _dispatch_tool(
    tool_name: str, tool_input: dict, server_params_list: list
) -> str:
    """Route a tool call to the correct MCP server and return the text result."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    for params in server_params_list:
        server_params = StdioServerParameters(
            command=params["command"],
            args=params["args"],
            env=None,
        )
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_response = await session.list_tools()
                    if not any(t.name == tool_name for t in tools_response.tools):
                        continue
                    result = await session.call_tool(tool_name, tool_input)
                    texts = [c.text for c in result.content if hasattr(c, "text")]
                    return "\n".join(texts)
        except Exception:
            continue

    return json.dumps({"error": f"No server handles tool: {tool_name}"})


async def _run_simulate(smiles: str, out: Path, cfg: dict) -> None:
    """Call simulate_spectrum_iceberg directly via MCP, no agent loop."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    sim_cfg = cfg.get("models", {}).get("simulator", {})
    server_params = StdioServerParameters(
        command="pixi",
        args=[
            "run",
            "--environment",
            sim_cfg.get("env", "simulator-iceberg"),
            "python",
            str(SERVERS_DIR / "simulator_iceberg.py"),
        ],
        env=None,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_input = {
                "smiles": smiles,
                "output_dir": str(out),
                "gen_ckpt": sim_cfg.get(
                    "gen_ckpt", "downloads/iceberg_dag_gen_msg_best.ckpt"
                ),
                "inten_ckpt": sim_cfg.get(
                    "inten_ckpt", "downloads/iceberg_dag_inten_msg_best.ckpt"
                ),
                "collision_energies": sim_cfg.get("collision_energies", [20, 40]),
                "adduct": sim_cfg.get("adduct", "[M+H]+"),
                "instrument": sim_cfg.get("instrument", "Orbitrap"),
            }
            if sim_cfg.get("cuda_devices"):
                tool_input["cuda_devices"] = sim_cfg["cuda_devices"]
            result = await session.call_tool("simulate_spectrum_iceberg", tool_input)
            text = next((c.text for c in result.content if hasattr(c, "text")), "{}")
            data = json.loads(text)

    if data.get("success"):
        click.echo(f"Spectrum plot: {data['spectrum_plot']}")
        click.echo(f"Fragments:     {out / 'fragments.json'}")
        click.echo(f"Output dir:    {out}")
    else:
        click.echo(f"Error: {data.get('error', 'unknown')}", err=True)
        raise SystemExit(1)


@click.group()
def main():
    """Snowmageddon: MS/MS structure elucidation agent."""


@main.command()
@click.option("--input", "input_file", required=True, help="Input spectrum or raw file")
@click.option(
    "--output-dir", default="results", show_default=True, help="Output directory"
)
@click.option(
    "--mode",
    type=click.Choice(["parallel", "cascade"]),
    default=None,
    help="Override orchestration mode from config (passed to system prompt context)",
)
@click.option(
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG),
    show_default=True,
    help="Config YAML",
)
def run(input_file: str, output_dir: str, mode: str | None, config_path: str):
    """Run the full elucidation workflow on a spectrum or raw file."""
    cfg = load_config(Path(config_path))
    mode = mode or cfg.get("mode", "parallel")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(output_dir) / ts
    out.mkdir(parents=True, exist_ok=True)

    input_path = Path(input_file)
    click.echo(f"Input: {input_path}  |  Mode: {mode}  |  Output: {out}")

    # Preprocess if needed
    supported_formats = {".mzml", ".mgf", ".msp"}
    if input_path.suffix.lower() not in supported_formats:
        click.echo("Preprocessing raw file...")
        result = subprocess.run(
            [
                "pixi",
                "run",
                "--environment",
                cfg.get("preprocessing", {}).get("env", "preprocess"),
                "python",
                str(SKILLS / "msms-preprocess" / "scripts" / "run.py"),
                "--input",
                str(input_path),
                "--output",
                str(out / (input_path.stem + ".mzML")),
                "--format",
                "mzML",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        spectrum_path = result.stdout.strip()
    else:
        spectrum_path = str(input_path)

    click.echo(f"Spectrum: {spectrum_path}")
    asyncio.run(_run_agent(spectrum_path, out, cfg))


@main.command()
@click.option("--smiles", required=True, help="Candidate molecule as SMILES")
@click.option(
    "--output-dir", default="results", show_default=True, help="Output directory"
)
@click.option(
    "--config",
    "config_path",
    default=str(DEFAULT_CONFIG),
    show_default=True,
    help="Config YAML",
)
@click.option("--gen-ckpt", default=None, help="Override generator checkpoint path")
@click.option("--inten-ckpt", default=None, help="Override intensity checkpoint path")
def simulate(
    smiles: str,
    output_dir: str,
    config_path: str,
    gen_ckpt: str | None,
    inten_ckpt: str | None,
):
    """Predict an MS/MS spectrum for a SMILES string using ICEBERG."""
    cfg = load_config(Path(config_path))
    if gen_ckpt:
        cfg.setdefault("models", {}).setdefault("simulator", {})["gen_ckpt"] = gen_ckpt
    if inten_ckpt:
        cfg.setdefault("models", {}).setdefault("simulator", {})["inten_ckpt"] = (
            inten_ckpt
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(output_dir) / ts
    out.mkdir(parents=True, exist_ok=True)

    click.echo(f"SMILES: {smiles}  |  Output: {out}")
    asyncio.run(_run_simulate(smiles, out, cfg))
