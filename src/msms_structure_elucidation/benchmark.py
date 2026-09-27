"""Time one ICEBERG inference trial and record peak GPU memory.

Run with the ms-pred interpreter from the ms-pred checkout; prints ``RESULT_JSON=``.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ENERGIES = [10, 20, 30, 40, 50]
# Drug- and metabolite-sized molecules so batches resemble real candidate lists.
SMILES = [
    'CC(=O)Oc1ccccc1C(=O)O', 'CN1C=NC2=C1C(=O)N(C(=O)N2C)C', 'CC(C)Cc1ccc(cc1)C(C)C(=O)O',
    'CN1CCC23C4C1CC5=C2C(=C(C=C5)O)OC3C(C=C4)O', 'OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O',
    'CC(C)NCC(O)COc1cccc2ccccc12', 'COc1ccc2[nH]cc(CCNC(C)=O)c2c1', 'NC(Cc1ccc(O)cc1)C(=O)O',
    'CCN(CC)CC(=O)Nc1c(C)cccc1C', 'O=C(O)CCc1c[nH]c2ccccc12', 'CC12CCC3C(CCC4=CC(=O)CCC34C)C1CCC2O',
    'CN1CCN(CC1)C(=O)OC1N(C(=O)c2nccnc12)c1ccc(Cl)cn1', 'O=C(O)C1=CN(C2CC2)c2cc(N3CCNCC3)c(F)cc2C1=O',
    'Clc1ccc(cc1)C(c1ccccc1)N1CCN(CCOCC(=O)O)CC1', 'CC(C)(C)NCC(O)c1ccc(O)c(CO)c1',
    'COc1cc(C=CC(=O)CC(=O)C=Cc2ccc(O)c(OC)c2)ccc1O', 'Oc1cc(O)c2C(=O)C(=C(Oc2c1)c1ccc(O)c(O)c1)O',
    'CC(=O)NC1C(O)CC(OC1C(O)C(O)CO)(O)C(=O)O', 'CCCCCCCCCCCCCCCC(=O)OCC(O)COP(=O)([O-])OCC[N+](C)(C)C',
    'CC1=C(C(=O)OC2CCCC2)C(c2cccc(c2)[N+](=O)[O-])C(=C(N1)C)C(=O)OC',
    'CN(C)CCCN1c2ccccc2CCc2ccc(Cl)cc12', 'CC(C)Cc1ccc(C(C)C(=O)NC(Cc2ccccc2)C(=O)O)cc1',
    'OC(=O)CC(O)(CC(=O)O)C(=O)O', 'NCCCC[C@H](N)C(=O)O',
]

SUBSTITUENTS = ['C', 'CC', 'OC', 'Cl', 'F', 'N', 'O', 'C(F)(F)F', 'CCC', 'OCC']
LEADING = ['C', 'CC', 'CO', 'Cl', 'F', 'N', 'O', 'FC(F)(F)', 'CCC', 'CCO']  # same groups, written as a prefix
AMINES = ['C', 'CC', 'CCC', 'C1CC1', 'Cc2ccccc2', 'CCO', 'CCN(C)C', 'C(C)C']


def molecules(count: int) -> list[str]:
    """Return count unique SMILES; ms-pred deduplicates repeated structures."""
    # Aryl-piperazine benzamides, MW about 330-520, similar in size to typical atlas candidates.
    library = SMILES + [f'{a}c1ccc(N2CCN(CC2)c2ccc({b})cc2)cc1C(=O)N{r}'
                        for r in AMINES for a in LEADING for b in SUBSTITUENTS]
    if count > len(library):
        raise ValueError(f'at most {len(library)} benchmark molecules are available')
    return library[:count]


def gpu_memory(devices: str) -> dict[int, tuple[float, float]]:
    """Return {index: (used GiB, total GiB)} for the visible devices."""
    smi = shutil.which('nvidia-smi')
    if not smi or not devices:
        return {}
    run = subprocess.run([smi, '--query-gpu=index,memory.used,memory.total', '--format=csv,noheader,nounits',
                          '-i', devices], capture_output=True, text=True)
    if run.returncode:
        return {}
    rows = [[part.strip() for part in line.split(',')] for line in run.stdout.strip().splitlines()]
    return {int(i): (int(used) / 1024, int(total) / 1024) for i, used, total in rows}


def trial(args) -> dict:
    from ms_pred.iceberg.iceberg_elucidation import iceberg_prediction

    smiles = molecules(args.num_smiles)
    peak: dict[int, float] = {}
    totals: dict[int, float] = {}
    done = threading.Event()

    def poll():
        while not done.is_set():
            for index, (used, total) in gpu_memory(args.cuda_devices).items():
                peak[index] = max(peak.get(index, 0.0), used)
                totals[index] = total
            done.wait(0.5)

    watcher = threading.Thread(target=poll, daemon=True)
    watcher.start()
    start = time.perf_counter()
    save_dir, _ = iceberg_prediction(candidate_smiles=smiles, collision_energies=ENERGIES, nce=False,
        adduct='[M+H]+', instrument='Orbitrap', exp_name=f'msms_benchmark_{args.batch_size}_{args.num_smiles}',
        python_path=sys.executable, gen_ckpt=args.gen_checkpoint, inten_ckpt=args.inten_checkpoint,
        cuda_devices=args.cuda_devices or None, batch_size=args.batch_size,
        num_gpu_workers=args.num_gpu_workers, num_cpu_workers=args.num_cpu_workers, force_recompute=True)
    seconds = time.perf_counter() - start
    done.set()
    watcher.join()
    ok = (Path(save_dir) / 'iceberg_run_successful').is_file()
    fraction = max((peak[i] / totals[i] for i in peak), default=None)
    spectra = len(smiles) * len(ENERGIES)
    return {'batch_size': args.batch_size, 'ok': ok, 'seconds': round(seconds, 1), 'spectra': spectra,
            'spectra_per_second': round(spectra / seconds, 2) if ok else None,
            'peak_memory_gb': {str(i): round(v, 2) for i, v in peak.items()},
            'peak_memory_fraction': round(fraction, 3) if fraction is not None else None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gen-checkpoint', required=True)
    parser.add_argument('--inten-checkpoint', required=True)
    parser.add_argument('--cuda-devices', default='')
    parser.add_argument('--batch-size', type=int, required=True)
    parser.add_argument('--num-gpu-workers', type=int, default=1)
    parser.add_argument('--num-cpu-workers', type=int, default=1)
    parser.add_argument('--num-smiles', type=int, default=96)
    print('RESULT_JSON=' + json.dumps(trial(parser.parse_args())))
