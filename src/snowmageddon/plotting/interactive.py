"""Interactive plotly mass spectrum plotting, styled to match the static
icicle-derived look in ``snowmageddon.plotting.mass_spectra``."""

import base64
import io
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
import plotly.graph_objects as go

from snowmageddon.plotting.style import spec_colors

# plotly's color validator rejects 8-digit (RGBA) hex strings that pypalettes emits.
_PRED_COLOR = spec_colors["pred_spec"][:7]
_TRUE_COLOR = spec_colors["true_spec"][:7]
_GLACIER_COLOR = spec_colors["glacier_spec"][:7]


def plot_mass_spectrum_interactive(
    mz_values: np.ndarray,
    intensities: np.ndarray,
    title: Optional[str] = None,
    precursor_mz: Optional[float] = None,
) -> go.Figure:
    """Plot a single MS2 spectrum as an interactive m/z-vs-intensity stem plot."""
    intensities = np.asarray(intensities, dtype=float)
    intensities = intensities / np.max(intensities)
    mz_values = np.asarray(mz_values, dtype=float)

    # Stems as one line trace with None-separated segments (fast for many peaks).
    stem_x = np.repeat(mz_values, 3)
    stem_y = np.empty(len(mz_values) * 3)
    stem_y[0::3] = 0
    stem_y[1::3] = intensities
    stem_y[2::3] = np.nan

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=stem_x,
            y=stem_y,
            mode="lines",
            line=dict(color=_PRED_COLOR, width=1.5),
            hoverinfo="skip",
            showlegend=False,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=mz_values,
            y=intensities,
            mode="markers",
            marker=dict(color=_PRED_COLOR, size=4),
            name="peaks",
            hovertemplate="m/z=%{x:.4f}<br>rel. intensity=%{y:.3f}<extra></extra>",
            showlegend=False,
        )
    )

    if precursor_mz is not None:
        fig.add_vline(
            x=precursor_mz,
            line=dict(color="#808080", width=1.5, dash="dash"),
            annotation_text=f"precursor {precursor_mz:.4f}",
            annotation_position="top right",
        )

    max_mz = precursor_mz if precursor_mz is not None else np.max(mz_values)
    fig.update_layout(
        title=title,
        xaxis_title="m/z",
        yaxis_title="Relative Intensity",
        xaxis=dict(range=[0, max_mz + 10]),
        yaxis=dict(range=[0, 1.1]),
        template="plotly_white",
        showlegend=False,
        width=600,
        height=450,
    )
    return fig


_MAX_FRAGMENT_IMAGES_PER_TRACE = 30


def render_fragment_image_b64(
    smiles: str, size: tuple[int, int] = (200, 150)
) -> Optional[str]:
    """Render a fragment SMILES to a base64-encoded PNG data URI, for hover
    tooltips. Returns None if RDKit can't parse/draw the fragment.

    A fragment ion from a broken bond can have valences RDKit's default
    sanitizer rejects (e.g. a radical carbon where a bond was cut) even
    though the SMILES itself is well-formed kekulized output — falls back to
    an unsanitized parse (skip valence/aromaticity checks, keep the given
    bond orders) rather than dropping the image entirely."""
    try:
        from rdkit import Chem
        from rdkit.Chem import Draw

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            mol = Chem.MolFromSmiles(smiles, sanitize=False)
            if mol is None:
                return None
            try:
                mol.UpdatePropertyCache(strict=False)
                Chem.SanitizeMol(
                    mol,
                    sanitizeOps=Chem.SANITIZE_ALL
                    ^ Chem.SANITIZE_KEKULIZE
                    ^ Chem.SANITIZE_PROPERTIES,
                )
            except Exception:
                pass
        img = Draw.MolToImage(mol, size=size)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def render_molecule_with_highlight_b64(
    molecule_smiles: str,
    atom_indices: Sequence[int],
    size: tuple[int, int] = (220, 170),
) -> Optional[str]:
    """Render the whole parent molecule with ``atom_indices`` highlighted in
    red (atoms and the bonds between them), as a base64 PNG data URI. Used
    instead of drawing the fragment as its own standalone molecule: a single
    GLACIER fragmentation event can legitimately break more than one bond at
    once (e.g. a retro-cyclization releasing two separate pieces from one
    ring-opening), so the predicted atom set is sometimes two or more
    disjoint pieces — real, physically meaningful output, not something to
    correct or hide. Drawing a multi-piece fragment as its own isolated
    SMILES reads as a malformed molecule; showing the same highlight inside
    the full parent skeleton keeps every atom in its real bonding context, so
    a multi-piece result reads as exactly what it is (two highlighted
    regions), not as an error. A bond is only highlighted when both its
    endpoint atoms are — same "present edges" convention ms-pred's own
    ``FragmentEngine.get_present_edges``/``get_draw_dict`` use for ICEBERG
    fragment figures (``ms_pred.common.plot_utils.export_mol_highlight``)."""
    try:
        from rdkit import Chem
        from rdkit.Chem.Draw import rdMolDraw2D

        mol = Chem.MolFromSmiles(molecule_smiles)
        if mol is None:
            return None
        atom_set = set(atom_indices)
        highlight_bonds = [
            bond.GetIdx()
            for bond in mol.GetBonds()
            if bond.GetBeginAtomIdx() in atom_set and bond.GetEndAtomIdx() in atom_set
        ]
        d = rdMolDraw2D.MolDraw2DCairo(*size)
        rdMolDraw2D.PrepareAndDrawMolecule(
            d,
            mol,
            highlightAtoms=list(atom_indices),
            highlightBonds=highlight_bonds,
            highlightAtomColors={i: (1.0, 0.2, 0.2) for i in atom_indices},
            highlightBondColors={i: (1.0, 0.2, 0.2) for i in highlight_bonds},
        )
        d.FinishDrawing()
        return "data:image/png;base64," + base64.b64encode(d.GetDrawingText()).decode()
    except Exception:
        return None


def _fragment_images_for_peaks(
    atom_indices_per_peak: Optional[Sequence[Optional[Sequence[int]]]],
    intensities: np.ndarray,
    molecule_smiles: Optional[str],
) -> list[Optional[str]]:
    """One rendered image (or None) per peak — the parent molecule with that
    peak's fragment atoms highlighted in red — capped to the top
    ``_MAX_FRAGMENT_IMAGES_PER_TRACE`` most intense *unique* atom sets per
    trace (GLACIER emits up to 100 peaks/sample but many repeat the same
    fragment, so this keeps the HTML small without dropping variety)."""
    if atom_indices_per_peak is None or not molecule_smiles:
        return [None] * len(intensities)

    order = np.argsort(-intensities)
    rendered: dict[tuple, Optional[str]] = {}
    images: list[Optional[str]] = [None] * len(intensities)
    for i in order:
        inds = atom_indices_per_peak[i] if i < len(atom_indices_per_peak) else None
        if not inds:
            continue
        key = tuple(sorted(inds))
        if key not in rendered:
            if len(rendered) >= _MAX_FRAGMENT_IMAGES_PER_TRACE:
                continue
            rendered[key] = render_molecule_with_highlight_b64(molecule_smiles, inds)
        images[i] = rendered[key]
    return images


def _stem_trace(
    mz_values: np.ndarray,
    intensities: np.ndarray,
    color: str,
    name: str,
    sign: float = 1.0,
    fragment_smiles: Optional[Sequence[Optional[str]]] = None,
    atom_indices_per_peak: Optional[Sequence[Optional[Sequence[int]]]] = None,
    molecule_smiles: Optional[str] = None,
):
    """One stems+markers pair of plotly traces, optionally mirrored (sign=-1).
    ``fragment_smiles`` (one per peak, GLACIER traces only) is shown as hover
    text. ``atom_indices_per_peak`` + ``molecule_smiles`` render the parent
    molecule with that peak's fragment atoms highlighted in red, shown as a
    hover image via the JS hover-sync block ``save_spectra_vs_glacier_html``
    injects — preferred over drawing the fragment standalone, since a single
    fragmentation event can legitimately span two or more disjoint pieces
    (see ``render_molecule_with_highlight_b64``)."""
    stem_x = np.repeat(mz_values, 3)
    stem_y = np.empty(len(mz_values) * 3)
    stem_y[0::3] = 0
    stem_y[1::3] = sign * intensities
    stem_y[2::3] = np.nan

    lines = go.Scatter(
        x=stem_x,
        y=stem_y,
        mode="lines",
        line=dict(color=color, width=1.5),
        hoverinfo="skip",
        showlegend=False,
    )

    images = _fragment_images_for_peaks(
        atom_indices_per_peak, intensities, molecule_smiles
    )
    customdata = np.array(
        [
            [inten, smi or "", img or ""]
            for inten, smi, img in zip(
                intensities,
                fragment_smiles or [None] * len(intensities),
                images,
            )
        ],
        dtype=object,
    )
    hover_bits = ["m/z=%{x:.4f}", "rel. intensity=%{customdata[0]:.3f}"]
    if fragment_smiles is not None:
        hover_bits.append("fragment=%{customdata[1]}")
    markers = go.Scatter(
        x=mz_values,
        y=sign * intensities,
        mode="markers",
        marker=dict(color=color, size=4),
        name=name,
        hovertemplate="<br>".join(hover_bits) + "<extra></extra>",
        customdata=customdata,
        showlegend=True,
    )
    return lines, markers


def plot_mass_spectrum_vs_glacier_interactive(
    mz_values: Optional[np.ndarray],
    intensities: Optional[np.ndarray],
    glacier_mz: np.ndarray,
    glacier_intensities: np.ndarray,
    title: Optional[str] = None,
    precursor_mz: Optional[float] = None,
    glacier_fragment_smiles: Optional[Sequence[Optional[str]]] = None,
    glacier_atom_indices: Optional[Sequence[Optional[Sequence[int]]]] = None,
    similarity_text: Optional[str] = None,
    molecule_smiles: Optional[str] = None,
) -> go.Figure:
    """Mirror plot: observed (up, black) vs. GLACIER-simulated (down, blue)
    in one interactive panel. ``mz_values=None`` for unmatched samples.
    ``glacier_atom_indices`` (one atom-index list per GLACIER peak) renders
    the parent molecule with that peak's fragment atoms highlighted red, on
    hover — needs ``molecule_smiles``. ``glacier_fragment_smiles`` is only
    used as accompanying hover text (the standalone fragment SMILES — reads
    poorly as its own molecule when the predicted fragment is a legitimate
    multi-piece break, see ``render_molecule_with_highlight_b64``).
    ``similarity_text`` (e.g. "entropy=0.68, cosine=0.82") is appended to
    the panel title when there's an observed spectrum to compare against.
    ``molecule_smiles``, when given, also renders the parent molecule as a
    fixed image in the panel's top-left corner (always visible)."""
    fig = go.Figure()

    if similarity_text and title:
        title = f"{title}<br><sup>{similarity_text}</sup>"
    elif similarity_text:
        title = f"<sup>{similarity_text}</sup>"

    has_observed = mz_values is not None and len(mz_values)
    if has_observed:
        intensities = np.asarray(intensities, dtype=float)
        intensities = intensities / np.max(intensities)
        for trace in _stem_trace(
            np.asarray(mz_values, dtype=float),
            intensities,
            _TRUE_COLOR,
            "observed",
            sign=1.0,
        ):
            fig.add_trace(trace)

    glacier_mz = np.asarray(glacier_mz, dtype=float)
    glacier_intensities = np.asarray(glacier_intensities, dtype=float)
    glacier_intensities = glacier_intensities / np.max(glacier_intensities)
    for trace in _stem_trace(
        glacier_mz,
        glacier_intensities,
        _GLACIER_COLOR,
        "GLACIER (simulated)",
        sign=-1.0,
        fragment_smiles=glacier_fragment_smiles,
        atom_indices_per_peak=glacier_atom_indices,
        molecule_smiles=molecule_smiles,
    ):
        fig.add_trace(trace)

    if precursor_mz is not None:
        fig.add_vline(
            x=precursor_mz,
            line=dict(color="#808080", width=1.5, dash="dash"),
            annotation_text=f"precursor {precursor_mz:.4f}",
            annotation_position="top right",
        )

    molecule_image = (
        render_fragment_image_b64(molecule_smiles, size=(180, 140))
        if molecule_smiles
        else None
    )
    layout_images = (
        [
            dict(
                source=molecule_image,
                xref="paper",
                yref="paper",
                x=0.0,
                y=1.0,
                sizex=0.28,
                sizey=0.28,
                xanchor="left",
                yanchor="top",
            )
        ]
        if molecule_image
        else []
    )

    all_mz = list(glacier_mz) + (list(mz_values) if has_observed else [])
    max_mz = precursor_mz if precursor_mz is not None else max(all_mz)
    fig.update_layout(
        title=title,
        xaxis_title="m/z",
        yaxis_title="Relative Intensity (observed up / GLACIER down)",
        xaxis=dict(range=[0, max_mz + 10]),
        yaxis=dict(
            range=[-1.1, 1.1],
            tickvals=[-1, -0.5, 0, 0.5, 1],
            ticktext=["1.0", "0.5", "0", "0.5", "1.0"],
        ),
        images=layout_images,
        template="plotly_white",
        showlegend=True,
        width=700,
        height=500,
    )
    return fig


def save_spectra_vs_glacier_html(
    entries: Sequence[tuple],
    output_path: str | Path,
) -> None:
    """Write one self-contained HTML file with a dropdown over every sample
    (matched and unmatched), each panel overlaying the observed spectrum
    (black, up) against the GLACIER-simulated spectrum (blue, down).

    Args:
        entries: ``(sample_id, title, mz_values, intensities, glacier_mz,
            glacier_intensities, precursor_mz)`` tuples, optionally followed
            by an 8th element ``glacier_fragment_smiles`` (one per GLACIER
            peak, hover text only), a 9th ``similarity_text`` (e.g.
            "entropy=0.68, cosine=0.82", shown as a subtitle), a 10th
            ``molecule_smiles`` (parent molecule, shown as a fixed image in
            the panel corner) and an 11th ``glacier_atom_indices`` (one
            atom-index list per GLACIER peak — renders the parent molecule
            with that peak's fragment atoms/bonds highlighted red on hover;
            needs ``molecule_smiles``). ``mz_values`` / ``intensities`` may
            be ``None`` for unmatched samples (no observed spectrum) —
            GLACIER's trace is always required.
        output_path: Where to write the HTML file.
    """
    fig = go.Figure()
    labels = []
    n_visible = 0

    for entry in entries:
        sample_id, title, mzs, intens, glacier_mz, glacier_intens, precursor_mz = entry[
            :7
        ]
        glacier_frag_smiles = entry[7] if len(entry) > 7 else None
        similarity_text = entry[8] if len(entry) > 8 else None
        molecule_smiles = entry[9] if len(entry) > 9 else None
        glacier_atom_indices = entry[10] if len(entry) > 10 else None
        if glacier_mz is None or len(glacier_mz) == 0:
            continue

        sub = plot_mass_spectrum_vs_glacier_interactive(
            mz_values=mzs,
            intensities=intens,
            glacier_mz=glacier_mz,
            glacier_intensities=glacier_intens,
            glacier_atom_indices=glacier_atom_indices,
            title=title,
            precursor_mz=precursor_mz,
            glacier_fragment_smiles=glacier_frag_smiles,
            similarity_text=similarity_text,
            molecule_smiles=molecule_smiles,
        )
        n_traces = len(sub.data)
        visible = n_visible == 0
        for trace in sub.data:
            trace.visible = visible
            fig.add_trace(trace)

        labels.append(dict(label=sample_id, sub_layout=sub.layout, n_traces=n_traces))
        n_visible += 1

    trace_counts = [entry["n_traces"] for entry in labels]
    total_traces = sum(trace_counts)
    offsets = np.cumsum([0] + trace_counts[:-1])

    buttons = []
    for i, entry in enumerate(labels):
        visibility = [False] * total_traces
        start = offsets[i]
        visibility[start : start + entry["n_traces"]] = [True] * entry["n_traces"]
        buttons.append(
            dict(
                label=entry["label"],
                method="update",
                args=[
                    {"visible": visibility},
                    {
                        "title": entry["sub_layout"].title,
                        "xaxis.range": list(entry["sub_layout"].xaxis.range),
                        "shapes": entry["sub_layout"].shapes,
                        "annotations": entry["sub_layout"].annotations,
                        "images": entry["sub_layout"].images,
                    },
                ],
            )
        )

    first_layout = labels[0]["sub_layout"] if labels else {}
    fig.update_layout(
        title=first_layout.title if labels else None,
        xaxis_title="m/z",
        yaxis_title="Relative Intensity (observed up / GLACIER down)",
        images=first_layout.images if labels else (),
        xaxis=dict(range=list(first_layout.xaxis.range) if labels else [0, 1]),
        yaxis=dict(
            range=[-1.1, 1.1],
            tickvals=[-1, -0.5, 0, 0.5, 1],
            ticktext=["1.0", "0.5", "0", "0.5", "1.0"],
        ),
        shapes=first_layout.shapes if labels else (),
        annotations=first_layout.annotations if labels else (),
        template="plotly_white",
        showlegend=True,
        width=700,
        height=500,
        updatemenus=[
            dict(
                buttons=buttons,
                direction="down",
                x=1.0,
                xanchor="right",
                y=1.2,
                yanchor="top",
            )
        ],
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(
        output_path,
        post_script=_FRAGMENT_HOVER_JS,
        full_html=True,
        include_plotlyjs=True,
    )


# Floating <img> synced to plotly's hover event, showing customdata[2] (the
# rendered fragment PNG data URI) for whichever peak is under the cursor.
# write_html substitutes {plot_id} with the actual div id string (there is no
# `gd` variable in scope here — that was wrong, plotly.js's own post_script
# hook only gives you the div id, not a bound element).
_FRAGMENT_HOVER_JS = """
var gd = document.getElementById('{plot_id}');
var fragImg = document.createElement("img");
fragImg.style.position = "fixed";
fragImg.style.pointerEvents = "none";
fragImg.style.border = "1px solid #ccc";
fragImg.style.background = "white";
fragImg.style.padding = "2px";
fragImg.style.display = "none";
fragImg.style.zIndex = 1000;
document.body.appendChild(fragImg);

gd.on("plotly_hover", function(evt) {
    var pt = evt.points && evt.points[0];
    if (!pt || !pt.customdata || !pt.customdata[2]) {
        fragImg.style.display = "none";
        return;
    }
    fragImg.src = pt.customdata[2];
    fragImg.style.left = (evt.event.clientX + 15) + "px";
    fragImg.style.top = (evt.event.clientY + 15) + "px";
    fragImg.style.display = "block";
});
gd.on("plotly_unhover", function() {
    fragImg.style.display = "none";
});
"""


def save_spectra_html(
    matched: Sequence[tuple],
    spectrum_peaks: Callable[[dict], tuple[list, list]],
    output_path: str | Path,
) -> None:
    """Write one self-contained HTML file with a dropdown to switch between
    every matched spectrum's interactive stem plot.

    Args:
        matched: ``(sample_id, data_path, raw_prefix, target_mass, feature)``
            tuples, as produced by the tsv/mgf mass-matching cell.
        spectrum_peaks: Parses a ``feature`` dict into ``(mzs, intensities)``.
        output_path: Where to write the HTML file.
    """
    fig = go.Figure()
    labels = []
    traces_per_sample = 2  # stem lines + peak markers
    n_visible = 0

    for sample_id, data_path, raw_prefix, target_mass, feature in matched:
        mzs, intens = spectrum_peaks(feature)
        if not mzs:
            continue

        sub = plot_mass_spectrum_interactive(
            mz_values=np.array(mzs),
            intensities=np.array(intens),
            title=f"{sample_id}  ({data_path}/{raw_prefix})",
            precursor_mz=target_mass,
        )
        visible = n_visible == 0
        for trace in sub.data:
            trace.visible = visible
            fig.add_trace(trace)

        labels.append(
            dict(
                label=sample_id,
                sub_layout=sub.layout,
            )
        )
        n_visible += 1

    n_samples = len(labels)
    buttons = []
    for i, entry in enumerate(labels):
        visibility = [False] * (n_samples * traces_per_sample)
        visibility[i * traces_per_sample : (i + 1) * traces_per_sample] = [True, True]
        buttons.append(
            dict(
                label=entry["label"],
                method="update",
                args=[
                    {"visible": visibility},
                    {
                        "title": entry["sub_layout"].title,
                        "xaxis.range": list(entry["sub_layout"].xaxis.range),
                        "shapes": entry["sub_layout"].shapes,
                        "annotations": entry["sub_layout"].annotations,
                    },
                ],
            )
        )

    first_layout = labels[0]["sub_layout"] if labels else {}
    fig.update_layout(
        title=first_layout.title if labels else None,
        xaxis_title="m/z",
        yaxis_title="Relative Intensity",
        xaxis=dict(range=list(first_layout.xaxis.range) if labels else [0, 1]),
        yaxis=dict(range=[0, 1.1]),
        shapes=first_layout.shapes if labels else (),
        annotations=first_layout.annotations if labels else (),
        template="plotly_white",
        showlegend=False,
        width=700,
        height=500,
        updatemenus=[
            dict(
                buttons=buttons,
                direction="down",
                x=1.0,
                xanchor="right",
                y=1.2,
                yanchor="top",
            )
        ],
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(output_path)
