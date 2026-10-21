"""Static charts (PNG) for the README.

Usage:
    from wind_power_curve import plots
    plots.power_curves(curves, Path("docs/img/power_curves.png"))
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from wind_power_curve.performance import fit_cos2  # noqa: E402
from wind_power_curve.power_curve import MM92_CURVE, interpolate  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]  # validated categorical order
SURFACE, INK, INK_2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
CONTEXT = "#c3c2b7"  # recessive gray for context points

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "axes.titlecolor": INK, "axes.titlesize": 11,
    "axes.titleweight": "bold", "axes.titlelocation": "left", "axes.spines.top": False,
    "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2,
    "legend.frameon": False, "legend.labelcolor": INK_2, "lines.linewidth": 2.0,
    "lines.solid_capstyle": "round", "font.size": 9.5,
    "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"],
})


def _save(fig, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def cleaning_example(df: pd.DataFrame, turbine: int, year: int, curve: pd.DataFrame, path: Path):
    """Scatter of one turbine-year coloured by the cleaning state, with the baseline reference curve."""
    d = df.xs(turbine, level="turbine")
    d = d[d.index.year == year]
    groups = [("normal operation", ["normal"], CONTEXT, 2),
              ("stopped (fault, maintenance, grid, park)", ["stop", "stop_grid"], SERIES[0], 6),
              ("curtailed / derated", ["curtailed"], SERIES[1], 6),
              ("icing or anemometer fault", ["icing", "sensor"], SERIES[2], 6)]
    fig, ax = plt.subplots(figsize=(9, 5))
    for label, states, color, size in groups:
        g = d[d["state"].isin(states)]
        if len(g):
            share = len(g) / len(d)
            pct = f"{share:.1%}" if share >= 0.001 else "<0.1%"
            ax.scatter(g["ws_n"], g["power_kw"], s=size, color=color, lw=0, alpha=0.6 if size == 2 else 0.8,
                       label=f"{label} ({pct})", rasterized=True)
    ax.plot(curve["ws"], curve["power_kw"], color=INK, label="reference curve (baseline year, binned)")
    ax.set_xlim(0, 22)
    ax.set_xlabel("Wind speed, density normalised [m/s]")
    ax.set_ylabel("Power [kW]")
    ax.set_title(f"Turbine {turbine}, {year}: 10-minute records by cleaning state")
    ax.legend(loc="upper left", markerscale=3)
    _save(fig, path)


def power_curves(curves: dict[int, pd.DataFrame], baseline_year: int, path: Path):
    """Baseline binned curves of all turbines vs the catalogue curve, and their deviation from it."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7.5), sharex=True, height_ratios=[1.4, 1])
    ax1.plot(MM92_CURVE.index, MM92_CURVE.values, color=INK, lw=2, label="MM92 catalogue curve")
    ax2.axhline(0, color=INK, lw=1.2)
    for i, (turbine, c) in enumerate(sorted(curves.items())):
        color = SERIES[i % len(SERIES)]
        ax1.plot(c["ws"], c["power_kw"], color=color, lw=1.6, label=f"T{turbine}")
        # compare at the catalogue's own 1 m/s points: interpolating the coarse catalogue curve adds artefacts
        ws = MM92_CURVE.index[(MM92_CURVE.index >= 3) & (MM92_CURVE.index <= min(16, c["ws"].max()))]
        measured = interpolate(c["ws"], c["power_kw"], ws)
        ax2.plot(ws, measured - MM92_CURVE.loc[ws].to_numpy(), color=color, lw=1.6, marker="o", ms=4,
                 label=f"T{turbine}")
    ax1.set_ylabel("Power [kW]")
    ax1.set_title(f"Measured power curves, {baseline_year} (IEC binning, 0.5 m/s, density normalised to 1.225 kg/m³)")
    ax1.legend(ncol=2, loc="lower right")
    ax2.set_ylabel("Measured - catalogue [kW]")
    ax2.set_title("Difference at the catalogue points")
    ax2.set_xlabel("Wind speed, density normalised [m/s] (nacelle anemometer)")
    ax2.set_xlim(2, 20)
    _save(fig, path)


def performance_index(monthly: pd.DataFrame, threshold: float, path: Path):
    """Small multiples per turbine: monthly neighbour index and 12-month rolling power curve index."""
    turbines = sorted(monthly.index.get_level_values("turbine").unique())
    fig, axes = plt.subplots(2, 3, figsize=(12, 6.2), sharex=True, sharey=True)
    for ax, turbine in zip(axes.flat, turbines):
        m = monthly.xs(turbine, level="turbine")
        t = m.index.to_timestamp()
        nb = m["index_nb"].where(m["n_nb"] >= 500)
        roll = m["index_curve"].where(m["n_curve"] >= 500).rolling(12, min_periods=9).mean()
        ax.axhline(1.0, color=AXIS, lw=1.0)
        ax.axhline(threshold, color=MUTED, lw=0.8)
        ax.plot(t, nb, color=SERIES[0], lw=1.6, marker="o", ms=2.5, label="vs neighbours (monthly)")
        ax.plot(t, roll, color=SERIES[1], lw=2.0, label="vs own power curve (12-month mean)")
        ax.set_title(f"Turbine {turbine}")
        ax.set_ylim(0.85, 1.12)
    for ax in axes[:, 0]:
        ax.set_ylabel("Actual / expected energy")
    for ax in axes[1]:
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.0))
    fig.suptitle(" ", fontsize=4)  # room for the legend
    _save(fig, path)


def lost_energy(losses: pd.DataFrame, path: Path):
    """Horizontal stacked bars: losses by cause as % of potential energy, per turbine, whole period."""
    cols = [("lost_downtime_mwh", "downtime (faults, maintenance)"),
            ("lost_grid_or_park_stop_mwh", "grid / park controller stops"),
            ("lost_curtailment_mwh", "curtailment / derating"),
            ("lost_icing_mwh", "icing")]
    tot = losses.groupby(level="turbine").sum()
    potential = tot["actual_mwh"] + tot[[c for c, _ in cols]].sum(axis=1)
    share = tot[[c for c, _ in cols]].div(potential, axis=0) * 100
    fig, ax = plt.subplots(figsize=(9, 4.2))
    y = np.arange(len(share))[::-1]
    left = np.zeros(len(share))
    for i, (c, label) in enumerate(cols):
        ax.barh(y, share[c], left=left, height=0.55, color=SERIES[i], edgecolor=SURFACE, linewidth=2, label=label)
        left += share[c].to_numpy()
    for yy, total in zip(y, left):
        ax.text(total + 0.05, yy, f"{total:.1f} %", va="center", color=INK_2)
    ax.set_yticks(y, [f"Turbine {t}" for t in share.index])
    ax.set_xlabel("Lost energy [% of potential production]")
    ax.set_title("Where the energy went: estimated losses by cause")
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, left.max() * 1.12)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=4)
    _save(fig, path)


def wind_speed_signal(df: pd.DataFrame, path: Path):
    """Weekly median of the SCADA wind speed relative to the mean of the two anemometers, per turbine."""
    d = df[df["power_kw"] > 100]
    ratio = (d["ws_scada"] / d[["ws1", "ws2"]].mean(axis=1, skipna=False)).unstack("turbine")
    weekly = ratio.resample("W").median().dropna(how="all")
    fig, ax = plt.subplots(figsize=(10, 4))
    for i, turbine in enumerate(weekly.columns):
        ax.plot(weekly.index, weekly[turbine], color=SERIES[i % len(SERIES)], lw=1.6, label=f"T{turbine}")
    change = pd.Timestamp("2018-04-09", tz="UTC")
    ax.axvline(change, color=MUTED, lw=1.0)
    ax.annotate("signal definition changes\n(week of 9 April 2018)", xy=(change, 1.005), xytext=(8, 0),
                textcoords="offset points", color=INK_2, va="bottom")
    ax.set_ylabel("SCADA wind speed / mean of anemometers")
    ax.set_title("The SCADA wind speed stops being the mean of the two anemometers")
    ax.set_ylim(0.93, 1.03)
    ax.legend(ncol=6, loc="lower left")
    _save(fig, path)


def yaw_misalignment(df: pd.DataFrame, yaw: pd.DataFrame, example: int, path: Path):
    """Left: power ratio vs vane angle for one turbine (all years) with the cos² fit. Right: offsets per turbine."""
    sel = (df["state"].eq("normal") & df["ws_n"].between(5, 10) & df["vane"].abs().le(12) & df["p_ratio"].notna())
    d = df[sel.to_numpy()].xs(example, level="turbine")
    ratio = d["power_kw"] / d["p_ratio"]
    keep = ratio.between(0.3, 1.7)
    x, r = d["vane"][keep], ratio[keep]
    binned = r.groupby(np.round(x)).agg(["mean", "size"])
    binned = binned[binned["size"] >= 30]
    theta, a, amp = fit_cos2(x, r)
    xs = np.linspace(-12, 12, 100)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2), width_ratios=[1, 1.1])
    ax1.plot(binned.index, binned["mean"], "o", color=SERIES[0], ms=5, label="mean per 1° bin")
    ax1.plot(xs, a + amp * np.cos(2 * np.deg2rad(xs - theta)), color=INK, lw=1.6, label="cos² fit")
    ax1.axvline(theta, color=MUTED, lw=1.0)
    ax1.set_xlabel("Nacelle vane angle [°]")
    ax1.set_ylabel("Power / neighbour-based expected")
    ax1.set_title(f"Turbine {example}, all years: peak at {theta:+.1f}°")
    ax1.legend(loc="lower center")

    yy = yaw.reset_index()
    pooled = yy[yy["year"].astype(str) == "all"].set_index("turbine")
    yearly = yy[yy["year"].astype(str) != "all"]
    ax2.axhline(0, color=AXIS, lw=1.0)
    for i, t in enumerate(pooled.index):
        ys = yearly.loc[yearly["turbine"] == t, "offset_deg"].clip(-15, 15)
        ax2.plot(np.full(len(ys), i + 0.18), ys, "o", color=CONTEXT, ms=4, label="single year" if i == 0 else None)
        ax2.vlines(i, max(pooled.loc[t, "p05"], -15), min(pooled.loc[t, "p95"], 15), color=SERIES[0], lw=2)
        ax2.plot(i, pooled.loc[t, "offset_deg"], "o", color=SERIES[0], ms=6,
                 label="all years, 5-95 % bootstrap" if i == 0 else None)
    ax2.set_xticks(range(len(pooled)), [f"T{t}" for t in pooled.index])
    ax2.set_ylim(-15, 15)
    ax2.set_ylabel("Estimated static yaw offset [°]")
    ax2.set_title("Offset per turbine")
    ax2.grid(axis="x", visible=False)
    ax2.legend(loc="upper right")
    _save(fig, path)
