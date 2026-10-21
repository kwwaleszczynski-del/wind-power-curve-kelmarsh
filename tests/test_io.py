import hashlib
import zipfile

import numpy as np
import pandas as pd
import pytest

from wind_power_curve import scada, zenodo

DATA_CSV = """# This file was exported by Greenbyte at 2022-01-27 10:33:05.
#
# Turbine: Kelmarsh 2
# Time zone: UTC
#
# Date and time,Wind speed (m/s),Wind direction (°),Power (kW),Blade angle (pitch position) A (°),Blade angle (pitch position) B (°),Blade angle (pitch position) C (°),Some other signal (V)
2017-01-01 00:00:00,7.5,210.0,812.3,0.1,0.2,0.3,400
2017-01-01 00:10:00,NaN,211.0,NaN,0.0,0.0,0.0,401
"""
STATUS_CSV = """# This file was exported by Greenbyte at 2022-01-27 10:52:06.
#
# Turbine: Kelmarsh 2
#
Timestamp start,Timestamp end,Duration,Status,Code,Message,Comment,Service contract category,IEC category
2017-01-01 00:05:00,2017-01-01 01:00:00,00:55:00,Stop,111,Emergency stop nacelle,,Emergency stop switch (Nacelle) (11),Forced outage
2017-01-01 02:00:00,-,-,Informational,0,System OK,,Warnings (27),Full Performance
"""
STATIC_CSV = """﻿Wind Farm,Title,Alternative Title,Identity,Manufacturer,Model,Rated power (kW),Hub Height (m),Rotor Diameter (m),Latitude,Longitude,Elevation (m),Country,Commercial Operations Date
Kelmarsh,Kelmarsh 2,KWF2,X,Senvion,MM92,2050,78.5,92,52.4,-0.9,156.6,UK,15/04/2016
"""


@pytest.fixture
def data_dir(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    with zipfile.ZipFile(raw / "Kelmarsh_SCADA_2017_3083.zip", "w") as z:
        z.writestr("Turbine_Data_Kelmarsh_2_2017-01-01_-_2018-01-01_229.csv", DATA_CSV.encode("utf-8"))
        z.writestr("Status_Kelmarsh_2_2017-01-01_-_2018-01-01_229.csv", STATUS_CSV.encode("utf-8"))
    (raw / "Kelmarsh_WT_static.csv").write_text(STATIC_CSV, encoding="utf-8")
    return tmp_path


def test_load_archive_and_cache(data_dir):
    df, status = scada.load([2017], data_dir)
    assert df.index.names == ["turbine", "timestamp"]
    assert df.index[0] == (2, pd.Timestamp("2017-01-01 00:00", tz="UTC"))
    assert df["power_kw"].iloc[0] == pytest.approx(812.3, rel=1e-6)
    assert df["pitch"].iloc[0] == pytest.approx(0.2, rel=1e-5)
    assert np.isnan(df["ws"].iloc[1]) and np.isnan(df["vane"]).all()  # absent signal -> NaN column
    assert status["status"].tolist() == ["Stop", "Informational"]
    assert pd.isna(status["end"].iloc[1])  # open event
    assert (data_dir / "processed" / "scada_2017.parquet").exists()
    # second call reads the Parquet cache
    df2, _ = scada.load([2017], data_dir)
    pd.testing.assert_frame_equal(df, df2)


def test_read_static(data_dir):
    st = scada.read_static(data_dir)
    assert st.loc[2, "hub_height_m"] == 78.5
    assert st.loc[2, "cod"] == pd.Timestamp("2016-04-15", tz="UTC")


def test_missing_archive_gives_clear_error(data_dir):
    with pytest.raises(FileNotFoundError, match="fetch"):
        scada.load([2019], data_dir)


class FakeResponse:
    def __init__(self, payload=None, content=b""):
        self._payload, self._content = payload, content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    def iter_content(self, chunk):
        for i in range(0, len(self._content), 4):
            yield self._content[i:i + 4]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, content: bytes):
        self.headers, self.content, self.calls = {}, content, []

    def get(self, url, stream=False, timeout=None):
        self.calls.append(url)
        if url == zenodo.RECORD_URL:
            files = [{"key": n, "size": len(self.content), "checksum": "md5:" + hashlib.md5(self.content).hexdigest(),
                      "links": {"self": f"https://example.org/{n}"}}
                     for n in ("Kelmarsh_WT_static.csv", "Kelmarsh_SCADA_2017_3083.zip", "Kelmarsh_SCADA_2018_3084.zip")]
            return FakeResponse(payload={"files": files})
        return FakeResponse(content=self.content)


def test_select_files():
    files = {n: {} for n in ("Kelmarsh_WT_static.csv", "Kelmarsh_SCADA_2017_3083.zip", "Kelmarsh_PMU_4459.zip")}
    assert zenodo.select_files(files, [2017]) == ["Kelmarsh_WT_static.csv", "Kelmarsh_SCADA_2017_3083.zip"]
    with pytest.raises(ValueError, match="2030"):
        zenodo.select_files(files, [2030])


def test_download_verifies_checksum_and_caches(tmp_path):
    session = FakeSession(b"0123456789abcdef")
    paths = zenodo.download([2018], tmp_path, session=session)
    assert [p.name for p in paths] == ["Kelmarsh_WT_static.csv", "Kelmarsh_SCADA_2018_3084.zip"]
    assert paths[1].read_bytes() == b"0123456789abcdef"
    n_calls = len(session.calls)
    zenodo.download([2018], tmp_path, session=session)  # cached: only the metadata request
    assert len(session.calls) == n_calls + 1


def test_download_rejects_bad_checksum(tmp_path):
    session = FakeSession(b"payload")
    info = {"size": 7, "md5": "0" * 32, "url": "https://example.org/x"}
    with pytest.raises(IOError, match="checksum"):
        zenodo.download_file(session, info, tmp_path / "x.zip")
    assert not (tmp_path / "x.zip").exists() and not (tmp_path / "x.zip.part").exists()
