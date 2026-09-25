"""Plotting utilities: colors, fonts, sizes, save helper.

Ported from icicle-dev's ``icicle.utils.visualization.style`` so
snowmageddon figures match icicle's manuscript style.

Usage::

    from snowmageddon.plotting.style import set_style, get_palette, make_fig, save_fig

    set_style("manuscript")          # call once at top of notebook/script
    fig, ax = make_fig("default")
    ...
    save_fig(fig, "my_figure")       # saves my_figure.svg + my_figure.pdf
"""

import warnings
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import seaborn as sns
from pypalettes import load_cmap

_cmap = load_cmap("X90", cmap_type="continuous")
_palette: list[str] = list(_cmap.colors) + ["#D3D3D3", "#A9A9A9", "#808080"]
sns.set_palette(_palette)

warnings.filterwarnings("ignore")

spec_colors = {
    "true_spec": "#000000",
    "pred_spec": _palette[2],
    "glacier_spec": "#1f77b4",
}

cmap = _cmap
palette = _palette

FIGSIZE = {
    "default": (3, 2.5),  # single panel, rectangle
    "square": (3, 3),  # parity / scatter
    "wide": (6, 2.5),  # wide two-panel (rare)
    "tall": (3, 4),  # tall single panel
}


def get_palette() -> list[str]:
    """Return the active discrete color palette."""
    return _palette


def get_cmap():
    """Return the continuous colormap."""
    return _cmap


def make_fig(
    size: str | tuple = "default",
    nrows: int = 1,
    ncols: int = 1,
    sharex: bool = False,
    sharey: bool = False,
    **subplot_kwargs,
) -> tuple:
    """Create a figure where the *axes area* matches the requested FIGSIZE.

    Unlike ``plt.subplots(figsize=FIGSIZE[...])`` — which sizes the whole figure
    including labels and whitespace — this function adds padding so the plot area
    itself is exactly the requested size.
    """
    plot_w, plot_h = FIGSIZE[size] if isinstance(size, str) else size

    pad_left = 0.55
    pad_right = 0.10
    pad_bottom = 0.45
    pad_top = 0.15

    fig_w = plot_w * ncols + pad_left + pad_right
    fig_h = plot_h * nrows + pad_bottom + pad_top

    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(fig_w, fig_h),
        sharex=sharex,
        sharey=sharey,
        **subplot_kwargs,
    )

    left = pad_left / fig_w
    right = 1.0 - pad_right / fig_w
    bottom = pad_bottom / fig_h
    top = 1.0 - pad_top / fig_h
    fig.subplots_adjust(left=left, right=right, bottom=bottom, top=top)

    return fig, axes


def set_style(style: str = "manuscript") -> None:
    """Set global matplotlib/seaborn style.

    Calling this once at the top of a notebook/script is sufficient.
    Do NOT override rcParams afterwards — let the style do its job.

    Args:
        style: One of ``"manuscript"``, ``"presentation"``, ``"poster"``.
    """
    _SIZE = {
        "manuscript": {
            "font": 10,
            "label": 10,
            "title": 10,
            "tick": 9,
            "legend": 9,
            "major_tick": 3,
        },
        "presentation": {
            "font": 12,
            "label": 12,
            "title": 12,
            "tick": 11,
            "legend": 11,
            "major_tick": 4,
        },
        "poster": {
            "font": 12,
            "label": 12,
            "title": 12,
            "tick": 11,
            "legend": 11,
            "major_tick": 4,
        },
    }
    if style not in _SIZE:
        raise KeyError(f"Style '{style}' not recognized. Choose: {list(_SIZE)}")

    sz = _SIZE[style]

    settings: dict = {
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "font.family": "sans-serif",
        "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
        "mathtext.fontset": "custom",
        "mathtext.rm": "Arial",
        "mathtext.it": "Arial:italic",
        "mathtext.bf": "Arial:bold",
        "mathtext.sf": "Arial",
        "font.size": sz["font"],
        "axes.labelsize": sz["label"],
        "axes.titlesize": sz["title"],
        "xtick.labelsize": sz["tick"],
        "ytick.labelsize": sz["tick"],
        "legend.fontsize": sz["legend"],
        "legend.title_fontsize": sz["legend"],
        "figure.figsize": FIGSIZE["default"],
        "figure.dpi": 300,
        "figure.facecolor": "white",
        "figure.autolayout": False,
        "axes.spines.top": True,
        "axes.spines.right": True,
        "axes.linewidth": 1.4,
        "axes.edgecolor": "black",
        "axes.labelcolor": "black",
        "axes.axisbelow": True,
        "axes.xmargin": 0.02,
        "axes.ymargin": 0.02,
        "xtick.direction": "in",
        "ytick.direction": "in",
        "xtick.major.size": sz["major_tick"],
        "ytick.major.size": sz["major_tick"],
        "xtick.major.width": 1.2,
        "ytick.major.width": 1.2,
        "xtick.minor.size": 1.5,
        "ytick.minor.size": 1.5,
        "xtick.minor.width": 0.5,
        "ytick.minor.width": 0.5,
        "xtick.minor.visible": True,
        "ytick.minor.visible": True,
        "xtick.top": False,
        "ytick.right": False,
        "xtick.color": "black",
        "ytick.color": "black",
        "lines.linewidth": 1.0,
        "lines.markersize": 4,
        "hatch.linewidth": 0.5,
        "grid.linewidth": 0.5,
        "legend.frameon": False,
        "legend.fancybox": False,
        "legend.facecolor": "none",
        "legend.edgecolor": "none",
        "legend.handlelength": 1.5,
        "legend.handletextpad": 0.4,
        "text.color": "black",
        "axes.prop_cycle": plt.cycler("color", _palette[:8]),
    }

    for k, v in settings.items():
        mpl.rcParams[k] = v


def save_fig(
    fig: "plt.Figure", name: str, output_dir: str | Path = ".", dpi: int = 300
) -> None:
    """Save figure as .svg and .pdf (both vector, for LaTeX inclusion)."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    kwargs = dict(bbox_inches="tight", transparent=False)
    fig.savefig(out / f"{name}.svg", **kwargs)
    fig.savefig(out / f"{name}.pdf", **kwargs)
