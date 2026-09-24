"""nvidia-smi parsing, the GPU timeline view, and time parsing."""

from datetime import datetime, timedelta

import pytest

from cassandra_hoard import views
from cassandra_hoard.gpu import GpuReader, parse_nvidia_smi
from cassandra_hoard.times import duration, parse_time, window

CANNED = """0, 20480, 24576, 87
1, 812 MiB, 12288 MiB, 3 %
2, [N/A], 8192, [N/A]
garbage line
3, 100, 8192, [Not Supported]
"""


def test_parse_canned_nvidia_smi():
    samples = parse_nvidia_smi(CANNED)
    assert [(s.gpu, s.mem_used_mb, s.mem_total_mb, s.util_pct) for s in samples] == [(0, 20480, 24576, 87), (1, 812, 12288, 3), (3, 100, 8192, None)]
    assert samples[0].mem_pct == 83.3 and samples[1].to_dict()["mem_free_mb"] == 11476
    assert parse_nvidia_smi("") == []


def test_reader_without_nvidia_smi(monkeypatch):
    monkeypatch.setattr("cassandra_hoard.gpu.find_nvidia_smi", lambda: None)
    reader = GpuReader()
    assert reader() == [] and reader.error == "nvidia-smi not found"


def test_gpu_timeline_series_peaks_and_freest(harness):
    from cassandra_hoard.gpu import GpuSample

    harness.tick()
    harness.gpu.samples = [GpuSample(0, 23000, 24000, 100.0), GpuSample(1, 1000, 12000, 0.0)]
    harness.tick()
    harness.gpu.samples = [GpuSample(0, 3000, 24000, 10.0), GpuSample(1, 1000, 12000, 0.0)]
    harness.tick()
    now = harness.clock.now
    data = views.gpu_timeline(harness.services.db, now - 3600, now + 1, None, 10)
    g0, g1 = data["gpus"]
    assert data["samples"] == 6 and g0["peak_mem"]["mem_used_mb"] == 23000 and g0["peak_util"]["util_pct"] == 100.0
    assert g0["now"]["mem_free_mb"] == 21000 and g1["now"]["mem_free_mb"] == 11000
    assert len(g0["series"]) == 1 and g0["series"][0][1] == 95.8  # three samples in one bucket: max 95.8 %
    only = views.gpu_timeline(harness.services.db, now - 3600, now, 1, 10)
    assert [g["gpu"] for g in only["gpus"]] == [1]


def test_parse_time_forms():
    now = datetime(2026, 9, 24, 10, 30).timestamp()
    assert parse_time("now", now) == now
    assert parse_time("2h", now) == now - 7200 and parse_time("-30m", now) == now - 1800 and parse_time("1d ago", now) == now - 86400
    assert parse_time("04:00", now) == datetime(2026, 9, 24, 4, 0).timestamp()
    assert parse_time("23:15", now) == datetime(2026, 9, 23, 23, 15).timestamp()  # future today → yesterday
    assert parse_time("2026-09-24T04:00", now) == datetime(2026, 9, 24, 4, 0).timestamp()
    assert parse_time("2026-09-24", now) == datetime(2026, 9, 24).timestamp()
    assert parse_time("yesterday", now) == (datetime(2026, 9, 24) - timedelta(days=1)).timestamp()
    assert parse_time(1790000000, now) == 1790000000.0 and parse_time("1790000000", now) == 1790000000.0
    assert parse_time("", now) is None and parse_time(None, now) is None
    with pytest.raises(ValueError):
        parse_time("last tuesday-ish", now)
    with pytest.raises(ValueError):
        parse_time("25:00", now)


def test_window_and_duration():
    now = datetime(2026, 9, 24, 10, 30).timestamp()
    at = datetime(2026, 9, 24, 4, 0).timestamp()
    assert window(at="04:00", window_min=10, now=now) == (at - 600, at + 600)
    assert window(now=now) == (now - 86400, now)
    assert window(since="1h", until="3h", now=now) == (now - 3 * 3600, now - 3600)  # swapped into order
    assert duration(30) == "30 s" and duration(600) == "10 min" and duration(7200) == "2.0 h" and duration(3 * 86400) == "3.0 d"
