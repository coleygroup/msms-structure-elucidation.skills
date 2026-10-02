"""MCP server exposing ms-pred (ICEBERG, GLACIER), FRIGID, MIST and atlas predictions.

It runs on the host that holds the models. A client on another machine starts it over ssh,
so stdio carries the protocol across. Model tools submit a job to the configured backend
(local or Slurm), wait up to `wait_seconds`, and then return either the result or a job id
for `get_job`.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from msms_structure_elucidation import pubchem
from msms_structure_elucidation.config import settings
from msms_structure_elucidation.mcp import tasks
from msms_structure_elucidation.mcp.backends import make_backend
from msms_structure_elucidation.mcp.jobs import ACTIVE, JobStore
from msms_structure_elucidation.spectrum import inspect_ms

INSTRUCTIONS = (
    'MS/MS structure elucidation models. Before scoring or predicting, confirm with the user whether '
    'collision energies are NCE or absolute eV; never guess the unit. Spectra are ms-pred .ms text '
    '(>compound, >parentmass, >ionization, >formula, then >collision <energy> blocks of "m/z intensity" lines). '
    'Model tools may return a job id instead of a result; poll it with get_job. Call server_info first to see '
    'which models this server has configured.')

server = MCPServer('msms-structure-elucidation', instructions=INSTRUCTIONS)
_RUNTIME: dict = {}

Unit = Annotated[Literal['NCE', 'eV'], Field(description='User-confirmed unit of the collision energies; ask if unknown')]
SpectrumText = Annotated[str | None, Field(description='Experimental spectrum as .ms text')]
SpectrumPath = Annotated[str | None, Field(description='Path of a .ms file on the server host, instead of spectrum_ms')]
Wait = Annotated[int | None, Field(description='Seconds to wait for the result before returning a job id; '
                                               'default from the server config')]
Model = Annotated[Literal['iceberg', 'glacier'], Field(description='Forward model')]


def tool(function):
    """Register a tool; expected failures reach the client as their message rather than a generic error."""
    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            raise ToolError(str(exc)) from exc
    return server.tool()(wrapper)


def configure(backend: str | None = None, config: str | None = None, job_root: str | None = None) -> None:
    """Load settings and the job backend; children inherit MSMS_CONFIG."""
    if config:
        os.environ['MSMS_CONFIG'] = str(Path(config).expanduser().resolve())
    loaded = settings(None)
    mcp_settings = loaded.get('mcp', {})
    root = Path(job_root or mcp_settings.get('job_root') or 'results/mcp_jobs').expanduser()
    if not root.is_absolute():
        root = tasks.REPO / root
    store = JobStore(root)
    _RUNTIME.update(config=loaded, store=store,
                    backend=make_backend(backend or mcp_settings.get('backend') or 'local', store, loaded),
                    wait_seconds=int(mcp_settings.get('wait_seconds', 600)))


def _runtime() -> dict:
    if not _RUNTIME:
        configure()
    return _RUNTIME


def _spectrum_inputs(params: dict, spectrum_ms: str | None, spectrum_path: str | None,
                     unit: str = 'eV') -> dict[str, str]:
    """Validate the spectrum now, so a malformed one fails before any job starts."""
    if bool(spectrum_ms) == bool(spectrum_path):
        raise ValueError('Pass exactly one of spectrum_ms (the .ms text) or spectrum_path (a file on the server)')
    if spectrum_path:
        path = Path(spectrum_path).expanduser().resolve()
        inspect_ms(path, unit)
        params['spectrum_path'] = str(path)
        params['spectrum_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / tasks.INLINE_SPECTRUM
        probe.write_text(spectrum_ms)
        inspect_ms(probe, unit)
    return {tasks.INLINE_SPECTRUM: spectrum_ms}


def _require(task: str, **options) -> None:
    missing = tasks.missing_assets(task, _runtime()['config'], **options)
    if missing:
        raise ValueError(f'{task} is not configured on this server; missing {", ".join(missing)}. '
                         'Rerun `msms-structure-elucidation setup` on the model host (see the msms-setup skill).')


def _response(job_id: str, detail: str = 'summary') -> dict:
    runtime = _runtime()
    store = runtime['store']
    status = runtime['backend'].poll(job_id)
    request = store.request(job_id)
    out = {'job_id': job_id, 'task': request['task'], 'state': status['state'], 'job_dir': str(store.path(job_id))}
    for key in ('devices', 'slurm_job_id', 'host', 'started', 'finished'):
        if status.get(key):
            out[key] = status[key]
    if status['state'] in ACTIVE and status.get('scheduler_state'):
        out['scheduler_state'] = status['scheduler_state']
    if status['state'] == 'done':
        result = store.result(job_id)
        out['result'] = result if detail == 'full' else tasks.summarize(request['task'], result, request['params'])
        out['result_path'] = str(store.path(job_id) / 'result.json')
    elif status['state'] == 'failed':
        out['error'] = status.get('error')
        out['log_tail'] = store.log_tail(job_id, 3000)
    elif status['state'] in ACTIVE:
        out['next'] = f'call get_job(job_id="{job_id}", wait_seconds=...) for the result'
    return out


async def _wait(job_id: str, wait_seconds: int, ctx: Context | None) -> None:
    backend = _runtime()['backend']
    deadline = time.monotonic() + max(0, wait_seconds)
    start = time.monotonic()
    while time.monotonic() < deadline:
        status = await asyncio.to_thread(backend.poll, job_id)
        if status['state'] not in ACTIVE:
            return
        if ctx is not None:
            await ctx.report_progress(time.monotonic() - start, wait_seconds)
        await asyncio.sleep(min(3.0, max(0.0, deadline - time.monotonic())))


async def _submit(task: str, params: dict, inputs: dict, wait_seconds: int | None, ctx: Context | None) -> dict:
    runtime = _runtime()
    job_id, reused = runtime['store'].create(task, params, inputs, kind=tasks.KINDS[task])
    if not reused:
        await asyncio.to_thread(runtime['backend'].submit, job_id)
    await _wait(job_id, runtime['wait_seconds'] if wait_seconds is None else wait_seconds, ctx)
    response = await asyncio.to_thread(_response, job_id)
    response['reused_earlier_job'] = reused
    return response


@tool
async def server_info() -> dict:
    """Where this server runs, its job backend and GPUs, and which models are configured."""
    runtime = _runtime()
    config = runtime['config']
    models = {task: {'ready': not missing, 'missing': missing}
              for task in tasks.KINDS for missing in [tasks.missing_assets(task, config)]}
    models['predict_spectra(glacier)'] = {'ready': not tasks.missing_assets('predict_spectra', config, model='glacier'),
                                          'missing': tasks.missing_assets('predict_spectra', config, model='glacier')}
    return {**await asyncio.to_thread(runtime['backend'].describe), 'job_root': str(runtime['store'].root),
            'config': config.get('_local_config_path') or config['_config_path'], 'models': models,
            'collision_energy_rule': 'Ask the user whether energies are NCE or eV before any model call.'}


@tool
async def predict_spectra(
        ctx: Context,
        smiles: Annotated[list[str], Field(description='Molecules to simulate', min_length=1)],
        collision_energies: Annotated[list[float], Field(description='Collision energies to predict', min_length=1)],
        collision_unit: Unit,
        adduct: Annotated[str, Field(description='Precursor adduct, such as [M+H]+')] = '[M+H]+',
        instrument: Annotated[str | None, Field(description='Orbitrap, QTOF, IT-FT or Unknown; '
                                                         'default models.simulator.instrument')] = None,
        model: Model = 'iceberg',
        include_fragments: Annotated[bool, Field(description='Annotate peaks with fragment formula and SMILES')] = True,
        max_peaks: Annotated[int, Field(description='Strongest peaks returned per energy', ge=1)] = 50,
        wait_seconds: Wait = None) -> dict:
    """Forward-predict MS/MS spectra (and fragment assignments) for SMILES with ICEBERG or GLACIER."""
    _require('predict_spectra', model=model)
    params = {'smiles': smiles, 'collision_energies': collision_energies, 'collision_unit': collision_unit,
              'adduct': adduct, 'instrument': instrument, 'model': model,
              'include_fragments': include_fragments, 'max_peaks': max_peaks}
    return await _submit('predict_spectra', params, {}, wait_seconds, ctx)


@tool
async def score_candidates(
        ctx: Context,
        smiles: Annotated[list[str], Field(description='Candidate structures to rank', min_length=1)],
        collision_unit: Unit,
        spectrum_ms: SpectrumText = None, spectrum_path: SpectrumPath = None,
        formula: Annotated[str | None, Field(description='Neutral formula; candidates with another formula are rejected. '
                                                         'Default: >formula in the spectrum')] = None,
        instrument: Annotated[str | None, Field(description='Instrument; default from the spectrum or config')] = None,
        model: Model = 'iceberg', top_k: Annotated[int, Field(ge=1)] = 10,
        wait_seconds: Wait = None) -> dict:
    """Simulate candidate SMILES and rank them by entropy similarity to an experimental spectrum."""
    _require('score_candidates', model=model)
    params = {'smiles': smiles, 'collision_unit': collision_unit, 'formula': formula, 'instrument': instrument,
              'model': model, 'top_k': top_k}
    inputs = _spectrum_inputs(params, spectrum_ms, spectrum_path, collision_unit)
    return await _submit('score_candidates', params, inputs, wait_seconds, ctx)


@tool
async def retrieve_atlas(
        ctx: Context,
        collision_unit: Unit, spectrum_ms: SpectrumText = None, spectrum_path: SpectrumPath = None,
        formula: Annotated[str | None, Field(description='Neutral formula; otherwise inferred with MSBuddy')] = None,
        instrument: Annotated[str | None, Field(description='Instrument; default from the spectrum or config')] = None,
        top_k: Annotated[int, Field(ge=1)] = 10, wait_seconds: Wait = None) -> dict:
    """Rank structures from the public ICEBERG PubChem atlas against an experimental spectrum."""
    params = {'collision_unit': collision_unit, 'formula': formula, 'instrument': instrument, 'top_k': top_k}
    inputs = _spectrum_inputs(params, spectrum_ms, spectrum_path, collision_unit)
    return await _submit('retrieve_atlas', params, inputs, wait_seconds, ctx)


@tool
async def generate_structures_frigid(
        ctx: Context,
        formula: Annotated[str, Field(description='Neutral molecular formula of the unknown')],
        spectrum_ms: SpectrumText = None, spectrum_path: SpectrumPath = None,
        adduct: Annotated[str | None, Field(description='Default: >ionization in the spectrum')] = None,
        instrument: Annotated[str | None, Field(description='Instrument label passed to FRIGID')] = None,
        top_k: Annotated[int, Field(ge=1)] = 10,
        num_rounds: Annotated[int, Field(description='ICEBERG refinement rounds; 0 is FRIGID-base', ge=0)] = 0,
        wait_seconds: Wait = None) -> dict:
    """Generate formula-constrained candidate structures de novo from a spectrum with FRIGID."""
    _require('generate_structures_frigid', num_rounds=num_rounds)
    params = {'formula': formula, 'adduct': adduct, 'instrument': instrument, 'top_k': top_k, 'num_rounds': num_rounds}
    inputs = _spectrum_inputs(params, spectrum_ms, spectrum_path)
    return await _submit('generate_structures_frigid', params, inputs, wait_seconds, ctx)


@tool
async def predict_fingerprint_mist(
        ctx: Context,
        formula: Annotated[str, Field(description='Neutral molecular formula of the unknown')],
        spectrum_ms: SpectrumText = None, spectrum_path: SpectrumPath = None,
        adduct: Annotated[str | None, Field(description='Default: >ionization in the spectrum')] = None,
        instrument: Annotated[str | None, Field(description='Instrument label passed to MIST')] = None,
        threshold: Annotated[float, Field(description='Bit probability counted as on', gt=0, lt=1)] = 0.172,
        wait_seconds: Wait = None) -> dict:
    """Predict a 4096-bit molecular fingerprint from a spectrum with FRIGID's MIST encoder."""
    _require('predict_fingerprint_mist')
    params = {'formula': formula, 'adduct': adduct, 'instrument': instrument, 'threshold': threshold}
    inputs = _spectrum_inputs(params, spectrum_ms, spectrum_path)
    return await _submit('predict_fingerprint_mist', params, inputs, wait_seconds, ctx)


@tool
async def get_job(ctx: Context, job_id: str, wait_seconds: Annotated[int, Field(ge=0)] = 0,
                  detail: Literal['summary', 'full'] = 'summary') -> dict:
    """State of a job, waiting up to wait_seconds; returns its result once done."""
    await _wait(job_id, wait_seconds, ctx)
    return await asyncio.to_thread(_response, job_id, detail)


@tool
async def cancel_job(job_id: str) -> dict:
    """Cancel a queued or running job."""
    status = await asyncio.to_thread(_runtime()['backend'].cancel, job_id)
    return {'job_id': job_id, 'state': status['state']}


@tool
async def list_jobs(limit: Annotated[int, Field(ge=1, le=200)] = 20) -> list[dict]:
    """Recent jobs on this server, newest first."""
    return await asyncio.to_thread(_runtime()['store'].list, limit)


@tool
async def pubchem_isomers(
        query: Annotated[str, Field(description='Molecular formula, such as C8H10N4O2, or a SMILES')],
        query_type: Literal['formula', 'smiles'] = 'formula',
        max_results: Annotated[int, Field(ge=1, le=200)] = 20) -> dict:
    """PubChem compounds for a formula or SMILES: CID, SMILES, InChIKey, formula, weight and IUPAC name."""
    return {'compounds': await asyncio.to_thread(pubchem.isomers, query, query_type, max_results)}


@tool
async def pubchem_compound(
        identifier: Annotated[str, Field(description='CID, name, InChIKey or SMILES')],
        id_type: Literal['cid', 'name', 'inchikey', 'smiles'] = 'name') -> dict:
    """One PubChem compound with its properties and up to ten synonyms."""
    return {'compound': await asyncio.to_thread(pubchem.compound, identifier, id_type)}
