"""Read the Greenbyte CSV exports inside the yearly Kelmarsh SCADA archives.

Each ``Kelmarsh_SCADA_<year>_<id>.zip`` holds, per turbine, a 10-minute data file (~300 signals) and a status
(event) log. Only the signals needed for the analysis are kept; each year is cached as Parquet in
``<data_dir>/processed``.

Usage:
    from wind_power_curve.scada import load
    scada, status = load([2017, 2018], data_dir=Path("data"))
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

TIME_COL = "# Date and time"
COLUMNS = {
    "Wind speed (m/s)": "ws",
    "Wind speed, Standard deviation (m/s)": "ws_std",
    "Wind speed Sensor 1 (m/s)": "ws1",
    "Wind speed Sensor 2 (m/s)": "ws2",
    "Wind direction (°)": "wind_dir",
    "Nacelle position (°)": "nacelle_pos",
    "Vane position 1+2 (°)": "vane",
    "Power (kW)": "power_kw",
    "Nacelle ambient temperature (°C)": "temp_c",
    "Blade angle (pitch position) A (°)": "pitch_a",
    "Blade angle (pitch position) B (°)": "pitch_b",
    "Blade angle (pitch position) C (°)": "pitch_c",
    "Rotor speed (RPM)": "rotor_rpm",
    "Energy Export (kWh)": "energy_kwh",
    # the operator's own (Greenbyte) loss allocation, kept only to cross-check our estimates
    "Lost Production to Downtime (kWh)": "gb_lost_downtime_kwh",
    "Lost Production to Curtailment (Total) (kWh)": "gb_lost_curtailment_kwh",
    "Lost Production to Performance (kWh)": "gb_lost_performance_kwh",
}
STATUS_COLUMNS = {
    "Timestamp start": "start",
    "Timestamp end": "end",
    "Status": "status",
    "Code": "code",
    "Message": "message",
    "IEC category": "iec_category",
}
TURBINE_RE = re.compile(r"_Kelmarsh_(\d+)_")
log = logging.getLogger(__name__)


def _header_row(fh: io.TextIOBase, marker: str) -> int:
    """Index of the column header line (exports start with a block of '#' comment lines)."""
    for i, line in enumerate(fh):
        if line.startswith(marker):
            return i
        if i > 50:
            break
    raise ValueError(f"header line starting with {marker!r} not found")


def read_turbine_csv(fh: io.TextIOBase, turbine: int) -> pd.DataFrame:
    """Parse one 10-minute data export; returns a frame indexed by (turbine, timestamp UTC)."""
    text = fh.read()
    skip = _header_row(io.StringIO(text), TIME_COL)
    wanted = {TIME_COL, *COLUMNS}
    df = pd.read_csv(io.StringIO(text), skiprows=skip, usecols=lambda c: c in wanted, na_values=["NaN"])
    out = pd.DataFrame(index=pd.to_datetime(df[TIME_COL]).dt.tz_localize("UTC").rename("timestamp"))
    for src, dst in COLUMNS.items():
        out[dst] = df[src].to_numpy(dtype="float32") if src in df else np.float32(np.nan)
    out["pitch"] = out[["pitch_a", "pitch_b", "pitch_c"]].mean(axis=1)
    out = out.drop(columns=["pitch_a", "pitch_b", "pitch_c"])
    out.insert(0, "turbine", np.int8(turbine))
    return out.set_index("turbine", append=True).swaplevel().sort_index()


def read_status_csv(fh: io.TextIOBase, turbine: int) -> pd.DataFrame:
    """Parse one status/event log into start/end/status/code/message/iec_category rows."""
    text = fh.read()
    skip = _header_row(io.StringIO(text), "Timestamp start")
    df = pd.read_csv(io.StringIO(text), skiprows=skip, usecols=list(STATUS_COLUMNS), dtype=str)
    df = df.rename(columns=STATUS_COLUMNS)
    for c in ("start", "end"):  # events still open at export time have end "-"
        df[c] = pd.to_datetime(df[c], format="%Y-%m-%d %H:%M:%S", errors="coerce").dt.tz_localize("UTC")
    df["code"] = pd.to_numeric(df["code"], errors="coerce")
    df.insert(0, "turbine", np.int8(turbine))
    return df


def read_zip(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    scada, status = [], []
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            base = name.rsplit("/", 1)[-1]
            m = TURBINE_RE.search(base)
            if not m or not base.endswith(".csv"):
                continue
            with z.open(name) as raw:
                fh = io.TextIOWrapper(raw, encoding="utf-8-sig")
                if base.startswith("Turbine_Data"):
                    scada.append(read_turbine_csv(fh, int(m.group(1))))
                elif base.startswith("Status"):
                    status.append(read_status_csv(fh, int(m.group(1))))
    if not scada:
        raise ValueError(f"{path.name}: no turbine data files found")
    return pd.concat(scada).sort_index(), pd.concat(status, ignore_index=True)


def read_static(data_dir: Path) -> pd.DataFrame:
    """Turbine metadata (hub height, elevation, commercial operation date), indexed by turbine number."""
    df = pd.read_csv(data_dir / "raw" / "Kelmarsh_WT_static.csv", encoding="utf-8-sig")
    out = pd.DataFrame({
        "turbine": df["Title"].str.extract(r"(\d+)$", expand=False).astype(int),
        "hub_height_m": df["Hub Height (m)"],
        "elevation_m": df["Elevation (m)"],
        "rated_kw": df["Rated power (kW)"],
        "rotor_diameter_m": df["Rotor Diameter (m)"],
        "cod": pd.to_datetime(df["Commercial Operations Date"], dayfirst=True).dt.tz_localize("UTC"),
    })
    return out.set_index("turbine")


def load(years: list[int], data_dir: Path, refresh: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """SCADA and status data for the given years, from the Parquet cache or the downloaded archives."""
    raw, processed = data_dir / "raw", data_dir / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    scada, status = [], []
    for year in years:
        s_path, e_path = processed / f"scada_{year}.parquet", processed / f"status_{year}.parquet"
        if s_path.exists() and e_path.exists() and not refresh:
            s, e = pd.read_parquet(s_path), pd.read_parquet(e_path)
        else:
            archives = sorted(raw.glob(f"Kelmarsh_SCADA_{year}_*.zip"))
            if not archives:
                raise FileNotFoundError(f"no archive for {year} in {raw}; run the 'fetch' command first")
            s, e = read_zip(archives[0])
            s.to_parquet(s_path)
            e.to_parquet(e_path)
            log.info("%d: %d rows, %d status events", year, len(s), len(e))
        scada.append(s)
        status.append(e)
    scada_all = pd.concat(scada)
    scada_all = scada_all[~scada_all.index.duplicated(keep="first")].sort_index()
    status_all = pd.concat(status, ignore_index=True).drop_duplicates()
    return scada_all, status_all
