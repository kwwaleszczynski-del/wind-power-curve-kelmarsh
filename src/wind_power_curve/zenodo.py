"""Download the open Kelmarsh wind farm dataset from Zenodo.

Record: "Kelmarsh wind farm data", C. Plumley and R. Takeuchi, Cubico Sustainable Investments,
https://doi.org/10.5281/zenodo.16807551 (concept DOI 10.5281/zenodo.5841833), licence CC-BY-4.0.

Files are downloaded once into ``<data_dir>/raw`` and verified against the MD5 checksums published by Zenodo.

Usage:
    from wind_power_curve.zenodo import download
    paths = download(years=[2019, 2020], data_dir=Path("data"))
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import requests

RECORD_ID = "16807551"
RECORD_URL = f"https://zenodo.org/api/records/{RECORD_ID}"
STATIC_FILES = ("Kelmarsh_WT_static.csv", "Kelmarsh_WT_dataSignalMapping.csv")
SCADA_PATTERN = re.compile(r"Kelmarsh_SCADA_(\d{4})_\d+\.zip")
CHUNK = 1 << 20
log = logging.getLogger(__name__)


def list_files(session: requests.Session) -> dict[str, dict]:
    """Return {file name: {"size", "md5", "url"}} for the Zenodo record."""
    r = session.get(RECORD_URL, timeout=60)
    r.raise_for_status()
    out = {}
    for f in r.json()["files"]:
        algo, _, digest = f["checksum"].partition(":")
        out[f["key"]] = {"size": f["size"], "md5": digest if algo == "md5" else None, "url": f["links"]["self"]}
    return out


def select_files(files: dict[str, dict], years: list[int]) -> list[str]:
    """Static metadata files plus the yearly SCADA archives for the requested years."""
    by_year = {int(m.group(1)): name for name in files if (m := SCADA_PATTERN.fullmatch(name))}
    missing = sorted(set(years) - set(by_year))
    if missing:
        raise ValueError(f"no SCADA archive for {missing}; available years: {sorted(by_year)}")
    return [n for n in STATIC_FILES if n in files] + [by_year[y] for y in sorted(years)]


def download_file(session: requests.Session, info: dict, dest: Path) -> Path:
    """Stream one file to ``dest`` (via a .part file) and check size and MD5."""
    if dest.exists() and dest.stat().st_size == info["size"]:
        log.info("cached %s", dest.name)
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s (%.1f MB)", dest.name, info["size"] / 1e6)
    h, done, next_report = hashlib.md5(), 0, 0.25
    with session.get(info["url"], stream=True, timeout=120) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(CHUNK):
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if info["size"] and done / info["size"] >= next_report:
                    log.info("  %s %3.0f %%", dest.name, 100 * done / info["size"])
                    next_report += 0.25
    if done != info["size"] or (info["md5"] and h.hexdigest() != info["md5"]):
        tmp.unlink()
        raise IOError(f"{dest.name}: size/checksum mismatch ({done} bytes, md5 {h.hexdigest()})")
    tmp.replace(dest)
    return dest


def download(years: list[int], data_dir: Path, session: requests.Session | None = None) -> list[Path]:
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "wind-power-curve-kelmarsh (open-source analysis)")
    raw = data_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    files = list_files(session)
    return [download_file(session, files[name], raw / name) for name in select_files(files, years)]
