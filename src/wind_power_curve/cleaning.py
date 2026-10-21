"""Classify every 10-minute SCADA record so that power curves use normal operation only.

Each record gets one ``state`` (first matching rule wins):

    pre_cod      before the commercial operation date (commissioning data)
    missing      no wind speed or no power
    sensor       anemometer fault: value out of range, frozen signal, or the two anemometers disagree
    stop_grid    turbine stopped by the grid or the park controller (IEC "Out of Electrical Specification",
                 "Requested Shutdown")
    stop         any other stop in the status log, or no output although the wind is well above cut-in
    curtailed    power limited: a power-reduction event in the status log, or blades pitched out while power
                 is low for the wind speed
    icing        cold (<= 2 degC) and power far below the turbine's normal curve
    normal       everything else

Usage:
    flags = classify(scada, status, cod=pd.Timestamp("2016-04-15", tz="UTC"))
    scada["state"] = flags["state"]
"""
from __future__ import annotations

import numpy as np
import pandas as pd

STEP = pd.Timedelta("10min")
GRID_CATEGORIES = {"Out of Electrical Specification", "Requested Shutdown"}
DERATE_PATTERN = r"P red|externally reduced|[Rr]educed power|[Pp]ower reduction|[Pp]ower limit"
STATES = ["pre_cod", "missing", "sensor", "stop_grid", "stop", "curtailed", "icing", "normal"]


def event_mask(timestamps: pd.DatetimeIndex, starts, ends) -> np.ndarray:
    """True for 10-minute intervals [t, t + 10 min) that overlap any event [start, end).

    ``timestamps`` must be sorted. Events still open at export time (end NaT) are counted as one interval.
    """
    # one time unit for all three: asi8 is in the index's own unit (pandas 3 often infers microseconds)
    ts = pd.DatetimeIndex(timestamps).as_unit("ns").asi8
    starts = pd.DatetimeIndex(starts).as_unit("ns")
    ends = pd.DatetimeIndex(ends).as_unit("ns")
    ends = ends.where(~ends.isna(), starts + STEP)
    first = np.searchsorted(ts, (starts - STEP).asi8, side="right")
    last = np.searchsorted(ts, ends.asi8, side="left")
    diff = np.zeros(len(ts) + 1, dtype=np.int64)
    np.add.at(diff, first, 1)
    np.add.at(diff, last, -1)
    return np.cumsum(diff[:-1]) > 0


def status_flags(scada: pd.DataFrame, status: pd.DataFrame) -> pd.DataFrame:
    """Per-record flags from the status log: stop_grid, stop, derate."""
    out = pd.DataFrame(False, index=scada.index, columns=["stop_grid", "stop", "derate"])
    is_stop = status["status"].eq("Stop")
    is_grid = status["iec_category"].isin(GRID_CATEGORIES)
    is_derate = status["message"].fillna("").str.contains(DERATE_PATTERN) | status["iec_category"].eq("Partial Performance")
    groups = {"stop_grid": is_stop & is_grid, "stop": is_stop & ~is_grid, "derate": is_derate & ~is_stop}
    for turbine, idx in scada.groupby(level="turbine").indices.items():
        ts = scada.index.get_level_values("timestamp")[idx]
        for col, sel in groups.items():
            ev = status[sel & status["turbine"].eq(turbine)]
            if len(ev):
                out.iloc[idx, out.columns.get_loc(col)] = event_mask(ts, ev["start"], ev["end"])
    return out


def consistent_wind_speed(df: pd.DataFrame) -> pd.Series:
    """Mean of the two nacelle anemometers where both report, otherwise the SCADA wind speed.

    Until April 2018 the SCADA "Wind speed" signal equals the mean of sensors 1 and 2; from the week of
    9 April 2018 it follows the lower-reading sensor 2 instead, which reads 2-8 % below the mean. Rebuilding the
    mean keeps the wind speed definition the same over the whole period.
    """
    return df[["ws1", "ws2"]].mean(axis=1, skipna=False).fillna(df["ws"])


def frozen(series: pd.Series, n: int = 3) -> pd.Series:
    """True where a value is repeated exactly for at least ``n`` consecutive records (a stuck sensor)."""
    s = series.to_numpy()
    same = np.r_[False, s[1:] == s[:-1]]
    run_id = np.cumsum(~same)
    run_len = pd.Series(run_id).map(pd.Series(run_id).value_counts()).to_numpy()
    return pd.Series((run_len >= n) & ~np.isnan(s), index=series.index)


def sensor_fault(df: pd.DataFrame) -> pd.Series:
    """Anemometer problems: out-of-range value, frozen reading above 1 m/s, or sensor 1 and 2 disagreeing."""
    ws = df["ws"]
    bad = (ws < 0) | (ws > 40)
    stuck = df.groupby(level="turbine", group_keys=False)["ws"].apply(frozen) & (ws > 1.0)
    pair = (df["ws1"] - df["ws2"]).abs() > np.maximum(1.0, 0.15 * df[["ws1", "ws2"]].mean(axis=1))
    return bad | stuck | pair.fillna(False)


def pitched_out(df: pd.DataFrame, ws_col: str = "ws") -> pd.Series:
    """Blades pitched out while power is low for the wind speed - the signature of derating or curtailment.

    Pitch more than 4 deg above the turbine's median pitch in that wind speed bin while power is below 85 % of
    the bin median power. Below 5 m/s (idling, start-up with feathered blades) this is normal operation.
    The SCADA "Turbine Power setpoint" is not used: it follows the available power, it is not a limit.
    """
    pitched = pd.Series(False, index=df.index)
    for _, idx in df.groupby(level="turbine").indices.items():
        d = df.iloc[idx]
        b = np.floor(d[ws_col] / 0.5 + 0.5) * 0.5
        med_pitch = d["pitch"].groupby(b).transform("median")
        med_power = d["power_kw"].groupby(b).transform("median")
        pitched.iloc[idx] = ((d["pitch"] > med_pitch + 4.0) & (d["power_kw"] < 0.85 * med_power)
                             & (d[ws_col] >= 5.0)).to_numpy()
    return pitched


def typical_curve(ws, power, bin_width: float = 0.5) -> pd.Series:
    """Median power per wind speed bin - a robust provisional curve used to spot icing and outliers."""
    b = np.floor(np.asarray(ws) / bin_width + 0.5) * bin_width
    return pd.Series(np.asarray(power)).groupby(b).median()


def classify(scada: pd.DataFrame, status: pd.DataFrame, cod: pd.Timestamp, ws_col: str = "ws") -> pd.DataFrame:
    """Return flags and the resulting ``state`` for each record (index aligned with ``scada``)."""
    flags = status_flags(scada, status)
    ts = scada.index.get_level_values("timestamp")
    ws, p = scada[ws_col], scada["power_kw"]
    flags["pre_cod"] = ts < cod
    flags["missing"] = ws.isna() | p.isna()
    flags["sensor"] = sensor_fault(scada)
    flags["stop"] |= (p <= 0) & (ws >= 4.5)
    flags["curtailed"] = flags.pop("derate") | pitched_out(scada, ws_col)

    state = pd.Series("normal", index=scada.index)
    for name in reversed(["pre_cod", "missing", "sensor", "stop_grid", "stop", "curtailed"]):
        state[flags[name].to_numpy()] = name

    # icing: compare with each turbine's own warm-weather curve
    flags["icing"] = False
    warm = state.eq("normal") & (scada["temp_c"] > 5)
    for turbine, idx in scada.groupby(level="turbine").indices.items():
        sel_warm = warm.iloc[idx]
        curve = typical_curve(ws.iloc[idx][sel_warm], p.iloc[idx][sel_warm])
        expected = np.interp(ws.iloc[idx], curve.index, curve.values, left=0.0, right=curve.values[-1])
        cold = (scada["temp_c"].iloc[idx] <= 2).to_numpy()
        flags.iloc[idx, flags.columns.get_loc("icing")] = cold & (expected > 100) & (p.iloc[idx].to_numpy() < 0.7 * expected)
    state[(state.eq("normal") & flags["icing"]).to_numpy()] = "icing"
    flags["state"] = pd.Categorical(state, categories=STATES)
    return flags


def bin_outliers(ws, power, bin_width: float = 0.5, k: float = 4.0, floor_kw: float = 25.0) -> np.ndarray:
    """True for points further than k robust standard deviations (MAD) from their bin median.

    Used only when fitting a reference curve; performance tracking keeps all normal records, otherwise the
    underperformance being looked for would be filtered out.
    """
    df = pd.DataFrame({"ws": np.asarray(ws, dtype=float), "p": np.asarray(power, dtype=float)})
    df["bin"] = np.floor(df["ws"] / bin_width + 0.5) * bin_width
    g = df.groupby("bin")["p"]
    med = g.transform("median")
    mad = (df["p"] - med).abs().groupby(df["bin"]).transform("median") * 1.4826
    return ((df["p"] - med).abs() > np.maximum(k * mad, floor_kw)).to_numpy()
