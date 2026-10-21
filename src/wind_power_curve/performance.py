"""Reference curves, underperformance tracking, static yaw misalignment and lost energy.

Every turbine is compared with two references, both learned in a baseline year:

* ``p_curve`` - its own binned power curve evaluated at its own (density normalised) nacelle wind speed.
  Simple, but it moves with the anemometer and with the season (stability and turbulence change the
  nacelle-anemometer power curve by several percent between summer and winter).
* ``p_ratio`` - the median power of the other turbines running normally at the same time, multiplied by a
  ratio learned per 10 deg wind direction sector (absorbing wakes, terrain and hub height differences).
  Independent of the turbine's anemometer and of the season, but blind to problems shared by the whole farm.
  Defined only in partial load, where power responds to performance problems.

A drop in both indices points to a real performance loss; a change in only the power curve index points to
the anemometer rather than the turbine.

For lost energy during stops and curtailment a third estimate, ``p_nb``, is used: the turbine's reference
curve evaluated at a wind speed estimated from the neighbours' anemometers (valid over the whole curve).

Usage:
    curves = reference_curves(df, baseline=(t0, t1))
    df["p_curve"] = expected_from_curves(df, curves)
    df["p_ratio"] = neighbour_power(df, baseline=(t0, t1))
    df["p_nb"] = expected_from_curves(df, curves, ws_col="ws_nb")  # after df["ws_nb"] = neighbour_wind_speed(...)
    monthly = performance_index(df, "M")
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import theilslopes

from wind_power_curve.cleaning import bin_outliers
from wind_power_curve.power_curve import bin_power_curve, interpolate

SECTOR_DEG = 10.0
PARTIAL_LOAD_KW = (100.0, 1500.0)
MIN_NEIGHBOURS = 3
LOSS_STATES = {"stop": "downtime", "stop_grid": "grid_or_park_stop", "curtailed": "curtailment", "icing": "icing"}


def _in_period(index: pd.Index, period) -> np.ndarray:
    ts = index.get_level_values("timestamp") if isinstance(index, pd.MultiIndex) else index
    return np.asarray((ts >= period[0]) & (ts < period[1]))


def _stack(wide: pd.DataFrame, index: pd.MultiIndex) -> pd.Series:
    """timestamp x turbine frame -> series on the (turbine, timestamp) index of the SCADA frame."""
    long = wide.rename_axis(columns="turbine").stack().reorder_levels(["turbine", "timestamp"])
    return long.reindex(index)


def reference_curves(df: pd.DataFrame, baseline, ws_col: str = "ws_n") -> dict[int, pd.DataFrame]:
    """Binned curve per turbine from normal-operation records in the baseline period, outliers removed."""
    curves = {}
    sel = df[df["state"].eq("normal").to_numpy() & _in_period(df.index, baseline)]
    for turbine, d in sel.groupby(level="turbine"):
        d = d.dropna(subset=[ws_col, "power_kw"])
        keep = ~bin_outliers(d[ws_col], d["power_kw"])
        curves[turbine] = bin_power_curve(d[ws_col][keep], d["power_kw"][keep])
    return curves


def expected_from_curves(df: pd.DataFrame, curves: dict[int, pd.DataFrame], ws_col: str = "ws_n") -> pd.Series:
    out = pd.Series(np.nan, index=df.index)
    for turbine, idx in df.groupby(level="turbine").indices.items():
        c = curves[turbine]
        out.iloc[idx] = interpolate(c["ws"], c["power_kw"], df[ws_col].iloc[idx])
    return out


def circular_mean_deg(angles: pd.DataFrame) -> pd.Series:
    rad = np.deg2rad(angles)
    return np.rad2deg(np.arctan2(np.sin(rad).mean(axis=1), np.cos(rad).mean(axis=1))) % 360.0


def _neighbours(df: pd.DataFrame, col: str, min_neighbours: int):
    """Wide frame of ``col`` for normal records, farm direction sector, and the neighbour median per turbine."""
    normal = df["state"].eq("normal")
    wide = df[col].where(normal).unstack("turbine")
    direction = circular_mean_deg(df["wind_dir"].where(normal).unstack("turbine")).reindex(wide.index)
    sector = np.floor(direction / SECTOR_DEG) % (360 / SECTOR_DEG)
    medians = {}
    for turbine in wide.columns:
        others = wide.drop(columns=turbine)
        medians[turbine] = others.median(axis=1).where(others.notna().sum(axis=1) >= min_neighbours)
    return wide, sector, medians


def _sector_factor(own: pd.Series, ref: pd.Series, sector: pd.Series, fit: pd.Series) -> pd.Series:
    """sum(own) / sum(ref) per sector over the fit records, mapped onto every timestamp (overall if unseen)."""
    overall = own[fit].sum() / ref[fit].sum()
    k = own[fit].groupby(sector[fit]).sum() / ref[fit].groupby(sector[fit]).sum()
    return sector.map(k).fillna(overall)


def neighbour_power(df: pd.DataFrame, baseline, min_neighbours: int = MIN_NEIGHBOURS) -> pd.Series:
    """Expected power from the neighbours' power (``p_ratio``); NaN outside partial load."""
    power, sector, medians = _neighbours(df, "power_kw", min_neighbours)
    in_base = _in_period(power.index, baseline)
    out = {}
    for turbine, ref in medians.items():
        ref = ref.where(ref.between(*PARTIAL_LOAD_KW))
        own = power[turbine]
        out[turbine] = ref * _sector_factor(own, ref, sector, in_base & own.notna() & ref.notna())
    return _stack(pd.DataFrame(out), df.index)


def neighbour_wind_speed(df: pd.DataFrame, baseline, min_neighbours: int = MIN_NEIGHBOURS,
                         fit_range=(4.0, 12.0)) -> pd.Series:
    """Wind speed at each turbine from the median of its neighbours' normalised wind speeds, per-sector speed-up."""
    ws, sector, medians = _neighbours(df, "ws_n", min_neighbours)
    in_base = _in_period(ws.index, baseline)
    out = {}
    for turbine, ref in medians.items():
        own = ws[turbine]
        out[turbine] = ref * _sector_factor(own, ref, sector, in_base & own.notna() & ref.between(*fit_range))
    return _stack(pd.DataFrame(out), df.index)


def performance_index(df: pd.DataFrame, freq: str = "M") -> pd.DataFrame:
    """Actual / expected energy per turbine and month ("M") or year ("Y"), normal operation in partial load.

    Yearly values remove the seasonal cycle of the power curve index.
    """
    normal = df["state"].eq("normal") & df["power_kw"].notna()
    period = df.index.get_level_values("timestamp").tz_localize(None).to_period(freq)
    turbine = df.index.get_level_values("turbine")
    cols = []
    # p_ratio is already restricted to partial load (of the neighbours) - the same records its ratios were fitted on
    for ref, name, window in (("p_curve", "curve", df["p_curve"].between(*PARTIAL_LOAD_KW)),
                              ("p_ratio", "nb", df["p_ratio"].notna())):
        sel = (normal & window).to_numpy()
        g = df[sel].groupby([turbine[sel], period[sel]])
        cols += [(g["power_kw"].sum() / g[ref].sum()).rename(f"index_{name}"), g.size().rename(f"n_{name}")]
    out = pd.concat(cols, axis=1)
    out.index.names = ["turbine", "month" if freq == "M" else "year"]
    return out


def index_trend(monthly: pd.DataFrame, col: str = "index_nb", min_records: int = 500) -> pd.DataFrame:
    """Theil-Sen slope of a monthly index per turbine, in % per year, with its 95 % confidence interval."""
    rows = []
    n_col = col.replace("index_", "n_")
    for turbine, d in monthly.groupby(level="turbine"):
        d = d[d[n_col] >= min_records][col].dropna()
        years = np.array([p.ordinal / 12.0 for p in d.index.get_level_values("month")])
        slope, _, lo, hi = theilslopes(d.to_numpy(), years)
        rows.append({"turbine": turbine, "slope_pct_per_year": 100 * slope, "ci_low": 100 * lo,
                     "ci_high": 100 * hi, "months": len(d)})
    return pd.DataFrame(rows).set_index("turbine")


def flag_underperformance(monthly: pd.DataFrame, threshold: float = 0.97, min_records: int = 500) -> pd.DataFrame:
    """Episodes of two or more consecutive months with the neighbour index below ``threshold``.

    Each episode is cross-checked with the power curve index relative to the farm median of the same month
    (which removes the seasonal and signal effects all turbines share). If that relative index is also low, the
    turbine converted less wind into power - a performance problem. If it is normal, the turbine saw less wind
    than its neighbours rather than using it badly (flow, wake or shear effect).
    """
    m = monthly[monthly["n_nb"] >= min_records].copy()
    m["curve_rel"] = m["index_curve"] / m["index_curve"].groupby(level="month").transform("median")
    episodes = []
    for turbine, d in m.groupby(level="turbine"):
        run = []
        for month, below in zip(d.index.get_level_values("month"), d["index_nb"] < threshold):
            if below and (not run or month == run[-1] + 1):
                run.append(month)
                continue
            if len(run) >= 2:
                episodes.append((turbine, run))
            run = [month] if below else []
        if len(run) >= 2:
            episodes.append((turbine, run))
    rows = []
    for turbine, run in episodes:
        sel = m.loc[[(turbine, p) for p in run]]
        rows.append({"turbine": turbine, "start": str(run[0]), "end": str(run[-1]), "months": len(run),
                     "mean_index_nb": sel["index_nb"].mean(), "mean_index_curve_rel": sel["curve_rel"].mean(),
                     "confirmed_by_power_curve": bool(sel["curve_rel"].mean() < threshold)})
    return pd.DataFrame(rows, columns=["turbine", "start", "end", "months", "mean_index_nb", "mean_index_curve_rel",
                                       "confirmed_by_power_curve"])


def fit_cos2(x_deg, y) -> tuple[float, float, float]:
    """Least-squares fit of y = A + R cos(2 (x - theta)); returns (theta [deg], A, R)."""
    x = np.deg2rad(np.asarray(x_deg, dtype=float))
    design = np.column_stack([np.ones_like(x), np.cos(2 * x), np.sin(2 * x)])
    a, b, c = np.linalg.lstsq(design, np.asarray(y, dtype=float), rcond=None)[0]
    return float(np.rad2deg(np.arctan2(c, b) / 2)), float(a), float(np.hypot(b, c))


def yaw_misalignment(df: pd.DataFrame, ws_range=(5.0, 10.0), vane_lim: float = 12.0, n_boot: int = 200,
                     min_records: int = 3000, seed: int = 0) -> pd.DataFrame:
    """Static yaw misalignment per turbine and year (plus all years pooled) from power ratio vs. vane angle.

    In partial load power falls roughly with cos^2 of the inflow angle. The model
    r = A + R cos(2 (x - theta)) - linear in A, B = R cos 2theta, C = R sin 2theta - is fitted to
    r = P / p_ratio (power relative to the neighbours) against the vane reading x. It peaks at x = theta, the vane
    angle where the turbine produces most; with a perfectly aligned vane that is 0 deg, so theta estimates the
    static offset. Normalising by the turbine's own anemometer instead gives a large spurious offset, because
    the nacelle anemometer reading itself depends on the inflow angle. 5-95 % interval: bootstrap over days.
    """
    rng = np.random.default_rng(seed)
    sel = (df["state"].eq("normal") & df["ws_n"].between(*ws_range) & df["vane"].abs().le(vane_lim)
           & df["p_ratio"].notna())
    d = df.loc[sel, ["vane", "power_kw", "p_ratio"]].copy()
    d["ratio"] = d["power_kw"] / d["p_ratio"]
    d = d[d["ratio"].between(0.3, 1.7)]
    ts = d.index.get_level_values("timestamp")
    d["year"], d["day"] = ts.year, ts.normalize()

    def peak(x, y):
        theta, a, r = fit_cos2(x, y)
        if r <= 0:
            return np.nan, np.nan
        return theta, 1 - (a + r * np.cos(2 * np.deg2rad(theta))) / (a + r)  # loss at vane = 0 vs the optimum

    groups = list(d.groupby([d.index.get_level_values("turbine"), "year"]))
    groups += [((turbine, "all"), g) for turbine, g in d.groupby(level="turbine")]  # pooled over all years
    rows = []
    for (turbine, year), g in groups:
        if len(g) < min_records:
            continue
        x, y = g["vane"].to_numpy(), g["ratio"].to_numpy()
        offset, loss = peak(x, y)
        days = list(g.groupby("day").indices.values())
        boots = [peak(x[p], y[p])[0] for p in
                 (np.concatenate([days[k] for k in rng.integers(0, len(days), len(days))]) for _ in range(n_boot))]
        rows.append({"turbine": turbine, "year": year, "offset_deg": offset,
                     "p05": np.nanpercentile(boots, 5), "p95": np.nanpercentile(boots, 95),
                     "loss_pct": 100 * loss, "records": len(g)})
    return pd.DataFrame(rows).set_index(["turbine", "year"])


def lost_energy(df: pd.DataFrame) -> pd.DataFrame:
    """Energy per turbine and year in MWh: actual, losses by cause, and the performance gap.

    Losses = shortfall (expected - actual, if positive) in stop / curtailed / icing records, with expected power
    ``p_nb`` (fallback: own reference curve). The performance gap is the signed shortfall against ``p_ratio`` in
    normal partial-load records: positive means producing less than the baseline relation to the neighbours.
    Records without wind or power data ("missing") get no loss estimate; their hours are reported instead.
    """
    expected = df["p_nb"].fillna(df["p_curve"])
    power = df["power_kw"]
    state = df["state"].astype(str)
    valid = ~state.eq("pre_cod")
    keys = [df.index.get_level_values("turbine")[valid], df.index.get_level_values("timestamp").year[valid]]

    # during SCADA gaps the 10-minute energy counter often still has the production
    energy = (power.clip(lower=0) / 6000.0).fillna(df["energy_kwh"] / 1000.0)
    out = pd.DataFrame({"actual_mwh": energy[valid].groupby(keys).sum()})
    shortfall = ((expected - power.fillna(0.0)).clip(lower=0) / 6000.0)
    for st, name in LOSS_STATES.items():
        out[f"lost_{name}_mwh"] = shortfall.where(state.eq(st), 0.0)[valid].groupby(keys).sum()
    gap = ((df["p_ratio"] - power) / 6000.0).where(state.eq("normal"))
    out["performance_gap_mwh"] = gap[valid].groupby(keys).sum()
    out["missing_hours"] = state.eq("missing")[valid].groupby(keys).sum() / 6.0
    for c in ("gb_lost_downtime_kwh", "gb_lost_curtailment_kwh", "gb_lost_performance_kwh"):
        out[c.replace("_kwh", "_mwh")] = df[c][valid].groupby(keys).sum() / 1000.0
    lost = out[[f"lost_{n}_mwh" for n in LOSS_STATES.values()]].sum(axis=1)
    out["energy_availability"] = out["actual_mwh"] / (out["actual_mwh"] + lost)
    out.index.names = ["turbine", "year"]
    return out
