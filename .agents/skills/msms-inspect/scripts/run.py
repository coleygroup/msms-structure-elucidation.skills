"""
Interactive LC-MS/MS chromatogram inspector.

Shows TIC, then on click opens MS1 at that RT, then on click opens MS2 for a precursor.
Optionally exports the selected precursor as a .ms file.

Usage:
    python run.py --input sample.mzML
    python run.py --input sample.mzML --export-dir results/precursors/

Requirements:
    - Env: preprocess
"""

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


def load_mzml(path: Path):
    try:
        from pyteomics import mzml
    except ImportError:
        sys.exit("pyteomics not installed.")

    ms1, ms2 = [], []
    with mzml.MzML(str(path)) as reader:
        for s in reader:
            level = s.get("ms level")
            rt = s.get("scanList", {}).get("scan", [{}])[0].get("scan start time", 0)
            rt_s = float(rt) * 60
            mz_arr = s.get("m/z array", np.array([]))
            int_arr = s.get("intensity array", np.array([]))
            if level == 1:
                ms1.append({"rt": rt_s, "mz": mz_arr, "intensity": int_arr})
            elif level == 2:
                prec = s.get("precursorList", {}).get("precursor", [{}])[0]
                sel = prec.get("selectedIonList", {}).get("selectedIon", [{}])[0]
                act = prec.get("activation", {})
                ms2.append(
                    {
                        "rt": rt_s,
                        "precursor_mz": sel.get("selected ion m/z", 0),
                        "precursor_intensity": sel.get("peak intensity", 0),
                        "ce": act.get("collision energy", ""),
                        "mz": mz_arr,
                        "intensity": int_arr,
                    }
                )
    return ms1, ms2


def build_tic(ms1):
    rts = np.array([s["rt"] for s in ms1])
    tics = np.array([s["intensity"].sum() for s in ms1])
    return rts, tics


def nearest_ms1(ms1, rt):
    rts = np.array([s["rt"] for s in ms1])
    return ms1[int(np.argmin(np.abs(rts - rt)))]


def ms2_for_precursor(ms2, precursor_mz, tol=0.01):
    return [s for s in ms2 if abs(s["precursor_mz"] - precursor_mz) <= tol]


def write_ms_file(
    path: Path, stem: str, precursor_mz: float, rt: float, scans: list
) -> Path:
    from collections import defaultdict

    spectra_by_ce = defaultdict(list)
    for s in scans:
        label = f"collision {s['ce']} eV" if s["ce"] else "collision"
        spectra_by_ce[label].extend(zip(s["mz"].tolist(), s["intensity"].tolist()))

    lines = [
        f">compound {stem}_mz{round(precursor_mz, 4)}",
        f">parentmass {round(precursor_mz, 4)}",
        ">ionization [M+H]+",
        f">rt {round(rt, 1)}s",
    ]
    for label, peaks in spectra_by_ce.items():
        lines += ["", f">{label}"]
        for mz, inten in sorted(peaks):
            lines.append(f"{mz}\t{inten}")

    out = path / f"mz{round(precursor_mz, 4)}.ms"
    out.write_text("\n".join(lines) + "\n")
    return out


def run(mzml_path: Path, export_dir: Path | None) -> None:
    print("Loading spectra...")
    ms1, ms2 = load_mzml(mzml_path)
    if not ms1:
        sys.exit("No MS1 spectra found.")

    rts, tics = build_tic(ms1)
    state = {"selected_prec": None, "selected_rt": None}

    fig, axes = plt.subplots(3, 1, figsize=(12, 10))
    fig.suptitle(mzml_path.name, fontsize=11)
    ax_tic, ax_ms1, ax_ms2 = axes

    ax_tic.set_title("TIC — click to inspect MS1")
    ax_tic.set_xlabel("Retention time (s)")
    ax_tic.set_ylabel("Total intensity")
    ax_tic.plot(rts, tics, lw=0.8, color="steelblue")
    tic_vline = ax_tic.axvline(x=rts[0], color="red", lw=1, ls="--", visible=False)

    ax_ms1.set_title("MS1 — click a peak to inspect MS2")
    ax_ms1.set_xlabel("m/z")
    ax_ms1.set_ylabel("Intensity")
    ms1_vline = ax_ms1.axvline(x=0, color="red", lw=1, ls="--", visible=False)

    ax_ms2.set_title("MS2 (no precursor selected)")
    ax_ms2.set_xlabel("m/z")
    ax_ms2.set_ylabel("Intensity")

    def draw_ms1(rt):
        scan = nearest_ms1(ms1, rt)
        state["selected_rt"] = scan["rt"]
        ax_ms1.cla()
        ax_ms1.set_title(f"MS1 at RT={scan['rt']:.1f}s — click a peak to inspect MS2")
        ax_ms1.set_xlabel("m/z")
        ax_ms1.set_ylabel("Intensity")
        if len(scan["mz"]):
            ax_ms1.vlines(scan["mz"], 0, scan["intensity"], lw=0.6, color="steelblue")
        tic_vline.set_xdata([scan["rt"]])
        tic_vline.set_visible(True)

    def draw_ms2(precursor_mz):
        scans = ms2_for_precursor(ms2, precursor_mz)
        state["selected_prec"] = precursor_mz if scans else None
        ax_ms2.cla()
        if not scans:
            ax_ms2.set_title(f"No MS2 found for m/z {precursor_mz:.4f}")
        else:
            all_mz = np.concatenate([s["mz"] for s in scans])
            all_int = np.concatenate([s["intensity"] for s in scans])
            ax_ms2.vlines(all_mz, 0, all_int, lw=0.5, color="darkorange")
            ce_vals = {s["ce"] for s in scans}
            ax_ms2.set_title(
                f"MS2 precursor m/z={precursor_mz:.4f}  "
                f"({len(scans)} scans, CE={ce_vals})  "
                + ("— press E to export .ms" if export_dir else "")
            )
        ax_ms2.set_xlabel("m/z")
        ax_ms2.set_ylabel("Intensity")

    def on_click(event):
        if event.inaxes == ax_tic and event.xdata is not None:
            draw_ms1(event.xdata)
            ax_ms2.cla()
            ax_ms2.set_title("MS2 (click a peak in MS1)")
            fig.canvas.draw_idle()
        elif event.inaxes == ax_ms1 and event.xdata is not None:
            draw_ms2(event.xdata)
            fig.canvas.draw_idle()

    def on_key(event):
        if event.key == "e" and export_dir and state["selected_prec"] is not None:
            export_dir.mkdir(parents=True, exist_ok=True)
            scans = ms2_for_precursor(ms2, state["selected_prec"])
            out = write_ms_file(
                export_dir,
                mzml_path.stem,
                state["selected_prec"],
                state["selected_rt"] or 0,
                scans,
            )
            print(f"Exported: {out}")
            ax_ms2.set_title(ax_ms2.get_title() + f"\n→ saved to {out.name}")
            fig.canvas.draw_idle()

    fig.canvas.mpl_connect("button_press_event", on_click)
    fig.canvas.mpl_connect("key_press_event", on_key)
    plt.tight_layout()
    plt.show()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive LC-MS/MS chromatogram inspector."
    )
    parser.add_argument("--input", required=True, help="Path to .mzML file")
    parser.add_argument(
        "--export-dir",
        default=None,
        help="Directory to export .ms files (press E after selecting MS2)",
    )
    args = parser.parse_args()

    mzml_path = Path(args.input)
    if not mzml_path.exists():
        sys.exit(f"File not found: {mzml_path}")

    export_dir = Path(args.export_dir) if args.export_dir else None
    run(mzml_path, export_dir)


if __name__ == "__main__":
    main()
