"""Power curves following the method of IEC 61400-12-1: air density normalisation, 0.5 m/s binning, AEP.

The standard measures wind speed with a met mast in front of the turbine. Here the nacelle anemometer is used
instead (as SCADA-based analyses must), so the binning and normalisation follow IEC 61400-12-1 but the absolute
curve carries the nacelle anemometer's bias; comparisons between turbines and over time are the main use.

Usage:
    rho = air_density(temp_c=df["temp_c"], elevation_m=230.0)
    ws_n = normalise_wind_speed(df["wind_speed"], rho)
    curve = bin_power_curve(ws_n, df["power_kw"])
    aep = aep_rayleigh(curve["ws"], curve["power_kw"], mean_ws=7.0)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RHO_0 = 1.225  # kg/m3, IEC reference density
R_DRY = 287.05  # J/(kg K)
BIN_WIDTH = 0.5
CUT_OUT = 25.0
HOURS_PER_YEAR = 8760.0

# Senvion MM92 (2050 kW) catalogue power curve at 1.225 kg/m3, kW, 1 m/s steps.
# Source: Open Energy Platform wind turbine library as distributed with windpowerlib (oedb/power_curves.csv),
# originally from windenergie-im-binnenland.de. This is the published sales curve, not a contractual warranty.
MM92_CURVE = pd.Series(
    [0.0, 22.0, 93.1, 207.2, 388.2, 642.7, 991.2, 1355.7, 1780.0, 2012.6, 2048.0]
    + [2055.0] * 13,
    index=np.arange(2.0, 26.0),
    name="power_kw",
).rename_axis("ws")
RATED_KW = 2050.0


def standard_pressure(elevation_m) -> np.ndarray:
    """ISO standard atmosphere pressure [Pa] at a height above sea level [m]."""
    return 101325.0 * (1.0 - 2.25577e-5 * np.asarray(elevation_m, dtype=float)) ** 5.25588


def air_density(temp_c, pressure_pa=None, elevation_m=None) -> np.ndarray:
    """Dry-air density [kg/m3] from temperature and pressure (standard atmosphere if pressure is unknown).

    IEC 61400-12-1 also corrects for humidity; that changes density by well under 1 % at UK temperatures and
    no humidity signal is available, so it is ignored.
    """
    if pressure_pa is None:
        if elevation_m is None:
            raise ValueError("give pressure_pa or elevation_m")
        pressure_pa = standard_pressure(elevation_m)
    return np.asarray(pressure_pa, dtype=float) / (R_DRY * (np.asarray(temp_c, dtype=float) + 273.15))


def normalise_wind_speed(ws, rho, rho_ref: float = RHO_0):
    """IEC 61400-12-1 normalisation for active-power-controlled turbines: V_n = V * (rho / rho_ref)^(1/3)."""
    return ws * (rho / rho_ref) ** (1.0 / 3.0)


def bin_power_curve(ws, power, bin_width: float = BIN_WIDTH, min_count: int = 3) -> pd.DataFrame:
    """Method of bins: bins centred on multiples of ``bin_width``; mean wind speed and power in each bin.

    Bins with fewer than ``min_count`` 10-minute records (30 minutes, the IEC minimum) are dropped.
    ``u_a_kw`` is the statistical (category A) uncertainty of the bin mean power, std / sqrt(n).
    """
    df = pd.DataFrame({"ws": np.asarray(ws, dtype=float), "power_kw": np.asarray(power, dtype=float)}).dropna()
    df["bin"] = np.floor(df["ws"] / bin_width + 0.5) * bin_width
    g = df.groupby("bin")
    out = pd.DataFrame({
        "ws": g["ws"].mean(),
        "power_kw": g["power_kw"].mean(),
        "std_kw": g["power_kw"].std(ddof=1),
        "n": g.size(),
    })
    out["u_a_kw"] = out["std_kw"] / np.sqrt(out["n"])
    return out[out["n"] >= min_count]


def database_complete(curve: pd.DataFrame, rated_kw: float, cut_in: float = 3.0) -> bool:
    """IEC completeness check: bins from 1 m/s below cut-in to 1.5 x the speed at 85 % rated power, and >= 180 h."""
    above = curve.index[curve["power_kw"] >= 0.85 * rated_kw]
    if len(above) == 0:
        return False
    needed = np.arange(cut_in - 1.0, 1.5 * curve.loc[above[0], "ws"] + 1e-9, BIN_WIDTH)
    needed = np.round(needed / BIN_WIDTH) * BIN_WIDTH
    return bool(set(needed) <= set(curve.index)) and curve["n"].sum() / 6.0 >= 180.0


def interpolate(curve_ws, curve_kw, ws, cut_out: float = CUT_OUT) -> np.ndarray:
    """Power from a curve at arbitrary wind speeds: linear in between, last value held up to cut-out, 0 outside."""
    xs, ys = np.asarray(curve_ws, dtype=float), np.asarray(curve_kw, dtype=float)
    ws = np.asarray(ws, dtype=float)
    p = np.interp(ws, xs, ys, left=0.0, right=ys[-1])
    p[(ws > cut_out) | (ws < xs[0])] = 0.0
    p[np.isnan(ws)] = np.nan
    return p


def aep_rayleigh(curve_ws, curve_kw, mean_ws: float, cut_out: float = CUT_OUT, step: float = 0.1) -> float:
    """Annual energy [MWh] for a Rayleigh wind speed distribution with the given mean, IEC style.

    The curve is extended with its last value up to cut-out (IEC "AEP-extrapolated").
    """
    v = np.arange(0.0, cut_out + step / 2, step)
    p = interpolate(curve_ws, curve_kw, v, cut_out)
    cdf = 1.0 - np.exp(-np.pi / 4.0 * (v / mean_ws) ** 2)
    return float(HOURS_PER_YEAR * np.sum(np.diff(cdf) * (p[1:] + p[:-1]) / 2.0) / 1000.0)
