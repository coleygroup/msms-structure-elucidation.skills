"""Mass spectrum plotting, ported from icicle-dev's visualization style."""

from typing import Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
from rdkit import Chem
from rdkit.Chem import Draw

from snowmageddon.plotting.style import FIGSIZE, make_fig, spec_colors


def create_spectrum_figure(
    figsize: Tuple[int, int], n_subplots: int = 1, share_x: bool = True
) -> Tuple[plt.Figure, list]:
    """Creates a figure with the proper styling for mass spectra plots.

    ``figsize`` is the *axes area* per subplot, not the total figure size.
    """
    fig, axes = make_fig(figsize, nrows=n_subplots, sharex=share_x)
    if n_subplots == 1:
        axes = [axes]

    for ax in axes:
        ax.grid(False)
        ax.axhline(0, color="black", linewidth=1.0)
        ax.set_ylim(0, 1.1)
        ax.set_yticks([0, 0.5, 1.0])

        for spine in ax.spines.values():
            spine.set_color("black")
            spine.set_linewidth(1.4)

        ax.xaxis.set_minor_locator(plt.NullLocator())
        ax.yaxis.set_minor_locator(plt.NullLocator())
        ax.xaxis.set_major_locator(plt.MaxNLocator(nbins=5))

    return fig, axes


def add_molecule_inset(
    ax: plt.Axes,
    smiles: str,
    position: Tuple[float, float] = (0.05, 0.65),
    size: Tuple[float, float] = (0.3, 0.3),
) -> None:
    """Adds a molecule visualization inset to the plot."""
    mol = Chem.MolFromSmiles(smiles)
    img = Draw.MolToImage(mol)
    img_array = np.asarray(img)
    ax_inset = ax.inset_axes([*position, *size])
    ax_inset.imshow(img_array)
    ax_inset.axis("off")


def plot_spectrum_stems(
    ax: plt.Axes,
    mz_values: np.ndarray,
    intensities: np.ndarray,
    color: str = "black",
    alpha: float = 0.7,
    linewidth: float = 1,
    label: Optional[str] = None,
    negative: bool = False,
) -> None:
    """Plots spectrum stems with consistent styling."""
    intensities_to_plot = -intensities if negative else intensities
    markerline, stemlines, baseline = ax.stem(
        mz_values,
        intensities_to_plot,
        markerfmt=" ",
        basefmt=" ",
        label=label if label else None,
    )
    plt.setp(stemlines, color=color, alpha=alpha, linewidth=linewidth)


def plot_mass_spectrum_vs_glacier(
    mz_values: Optional[np.ndarray],
    intensities: Optional[np.ndarray],
    glacier_mz: np.ndarray,
    glacier_intensities: np.ndarray,
    smiles: Optional[str] = None,
    title: Optional[str] = None,
    precursor_mz: Optional[float] = None,
    figsize: Tuple[int, int] = FIGSIZE["default"],
    similarity_text: Optional[str] = None,
) -> plt.Figure:
    """Mirror plot: observed spectrum (up, black) vs. GLACIER-simulated
    spectrum (down, blue) in one panel. Pass ``mz_values=None`` for
    unmatched samples (GLACIER-only, nothing plotted above the axis).
    ``similarity_text`` (e.g. "entropy=0.68, cosine=0.82") is appended to
    the title as a subtitle when there's an observed spectrum to compare."""
    fig, [ax] = create_spectrum_figure(figsize)

    if smiles:
        add_molecule_inset(ax, smiles)

    if mz_values is not None and len(mz_values):
        intensities = intensities / np.max(intensities)
        plot_spectrum_stems(
            ax, mz_values, intensities, color=spec_colors["true_spec"], label="observed"
        )

    glacier_intensities = glacier_intensities / np.max(glacier_intensities)
    plot_spectrum_stems(
        ax,
        glacier_mz,
        glacier_intensities,
        color=spec_colors["glacier_spec"],
        label="simulated",
        negative=True,
    )

    ax.set_ylim(-1.1, 1.1)
    ax.set_yticks([-1.0, -0.5, 0, 0.5, 1.0])
    ax.set_yticklabels(["1.0", "0.5", "0", "0.5", "1.0"])

    if precursor_mz is not None:
        ax.axvline(
            precursor_mz,
            color="0.5",
            linewidth=1.0,
            linestyle="--",
            label=f"precursor {precursor_mz:.4f}",
        )

    ax.legend(loc="upper right", fontsize="small")
    ax.set_xlabel("m/z")
    ax.set_ylabel("Relative Intensity")

    all_mz = list(glacier_mz) + (list(mz_values) if mz_values is not None else [])
    max_mz = precursor_mz if precursor_mz is not None else max(all_mz)
    ax.set_xlim(0, max_mz + 10)

    if title and similarity_text:
        title = f"{title}\n{similarity_text}"
    elif similarity_text:
        title = similarity_text
    if title:
        fig.suptitle(title, y=0.98)

    return fig


def plot_mass_spectrum(
    mz_values: np.ndarray,
    intensities: np.ndarray,
    smiles: Optional[str] = None,
    title: Optional[str] = None,
    precursor_mz: Optional[float] = None,
    figsize: Tuple[int, int] = FIGSIZE["default"],
) -> plt.Figure:
    """Plot a single MS2 spectrum as m/z-vs-intensity stems, icicle-styled."""
    fig, [ax] = create_spectrum_figure(figsize)

    intensities = intensities / np.max(intensities)

    if smiles:
        add_molecule_inset(ax, smiles)

    plot_spectrum_stems(ax, mz_values, intensities, color=spec_colors["pred_spec"])

    if precursor_mz is not None:
        ax.axvline(
            precursor_mz,
            color="0.5",
            linewidth=1.0,
            linestyle="--",
            label=f"precursor {precursor_mz:.4f}",
        )
        ax.legend(loc="upper right")

    ax.set_xlabel("m/z")
    ax.set_ylabel("Relative Intensity")

    max_mz = precursor_mz if precursor_mz is not None else np.max(mz_values)
    ax.set_xlim(0, max_mz + 10)

    if title:
        fig.suptitle(title, y=0.98)

    return fig
