"""Incremental log tailing on temp files: offsets, partial lines, rotation, big files, timestamps, levels, search."""

from datetime import datetime

from conftest import T0, FakeClock, make_config

from cassandra_hoard.db import Database
from cassandra_hoard.logs import FIRST_READ_BYTES, LogStore, level_of, parse_line_ts
from cassandra_hoard.registry import Service


def make_store(tmp_path, services=(), **overrides):
    config = make_config(tmp_path, **overrides)
    db = Database(config.db_path)
    clock = FakeClock(T0)
    return LogStore(db, config, lambda: list(services), clock), db, clock


def stamp(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S,123")


def test_parse_ts_and_levels():
    assert parse_line_ts("2026-09-24 04:00:12,345 INFO x") == datetime(2026, 9, 24, 4, 0, 12, 345000).timestamp()
    assert parse_line_ts("[2026/09/24 04:00:12] boot") == datetime(2026, 9, 24, 4, 0, 12).timestamp()
    assert parse_line_ts("no time here") is None
    assert level_of("2026-09-24 ERROR boom") == "error"
    assert level_of("Traceback (most recent call last):") == "error"
    assert level_of("ValueError: bad") == "error"
    assert level_of("llama_model_load: error: failed to load") == "error"
    assert level_of("WARNING: low disk") == "warning"
    assert level_of("DEBUG tick") == "debug"
    assert level_of("GET /api/health 200") == "info"
    assert level_of("0 errors found") == "info"


def test_incremental_tail_partial_lines_and_rotation(tmp_path):
    log = tmp_path / "svc.log"
    svc = Service("svc", "Svc", "user", "http://127.0.0.1:9", log_globs=[str(log)])
    store, db, clock = make_store(tmp_path, [svc])
    log.write_text(f"{stamp(T0 - 60)} INFO one\n{stamp(T0 - 30)} ERROR two\npartial", encoding="utf-8")
    assert store.tick() == 2
    assert store.tick() == 0  # unchanged file: nothing new
    with log.open("a", encoding="utf-8") as fh:
        fh.write(" line done\ncontinuation without time\n")
    assert store.tick() == 2
    rows = db.query("SELECT line, level, ts, ts_parsed FROM log_lines ORDER BY id")
    assert [r["line"] for r in rows] == [f"{stamp(T0 - 60)} INFO one", f"{stamp(T0 - 30)} ERROR two", "partial line done", "continuation without time"]
    assert rows[1]["level"] == "error" and rows[0]["ts_parsed"] == 1 and rows[3]["ts_parsed"] == 0
    # Rotation: the file shrinks → read again from the start.
    log.write_text("fresh start\n", encoding="utf-8")
    assert store.tick() == 1
    assert db.one("SELECT line FROM log_lines ORDER BY id DESC LIMIT 1")["line"] == "fresh start"


def test_first_read_of_a_big_file_only_takes_the_tail(tmp_path):
    log = tmp_path / "big.log"
    line = "x" * 99 + "\n"
    log.write_text(line * 2000 + "LAST LINE\n", encoding="utf-8")
    svc = Service("big", "Big", "user", "http://127.0.0.1:9", log_globs=[str(tmp_path / "*.log")])
    store, db, _ = make_store(tmp_path, [svc])
    added = store.tick()
    assert added <= FIRST_READ_BYTES // 100 + 1 and added > 100
    assert db.one("SELECT line FROM log_lines ORDER BY id DESC LIMIT 1")["line"] == "LAST LINE"
    assert all(len(r["line"]) == 99 for r in db.query("SELECT line FROM log_lines WHERE line != 'LAST LINE'"))


def test_tag_by_file_name_extra_globs_and_search(tmp_path):
    logs = tmp_path / "hub" / "data" / "logs"
    logs.mkdir(parents=True)
    (logs / "argus.log").write_text(f"{stamp(T0 - 10)} ERROR CUDA error: out of memory\n", encoding="utf-8")
    (logs / "unknown-thing.log").write_text(f"{stamp(T0 - 5)} INFO hello\n", encoding="utf-8")
    extra = tmp_path / "comfy.txt"
    extra.write_text(f"{stamp(T0 - 3)} WARNING slow step\n", encoding="utf-8")
    argus = Service("argus", "Argus's Hoard", "app", "http://127.0.0.1:5183")
    hub = Service("hoardhub", "Hoard Hub", "app", "http://127.0.0.1:8810", log_globs=[str(logs / "*.log")])
    store, db, clock = make_store(tmp_path, [argus, hub], log_globs=[f"comfyui={extra}"])
    assert store.tick() == 3
    tags = {r["service"] for r in db.query("SELECT service FROM log_lines")}
    assert tags == {"argus", "hoardhub", "comfyui"}
    assert [r["service"] for r in store.search("cuda memory")] == ["argus"]
    assert [r["service"] for r in store.search("", level="warning")] == ["comfyui", "argus"]
    assert store.search("", service="hoardhub")[0]["line"].endswith("hello")
    assert store.search("", since=T0 - 4) and len(store.search("", until=T0 - 6)) == 1
    assert store.search("100%_literal") == []
    tail = store.tail_for("argus", T0, 5)
    assert len(tail) == 1 and tail[0]["level"] == "error"


def test_prune_by_age_and_cap(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("".join(f"{stamp(T0 - 20 * 86400 + i)} INFO old {i}\n" for i in range(3)) + "".join(f"{stamp(T0 - 100 + i)} INFO new {i}\n" for i in range(10)), encoding="utf-8")
    svc = Service("a", "A", "user", "http://127.0.0.1:9", log_globs=[str(log)])
    store, db, _ = make_store(tmp_path, [svc], log_max_lines=1000)
    store.tick()
    assert store.prune() == 3
    store.config.log_max_lines = 4
    store.prune()
    assert [r["line"][-5:] for r in db.query("SELECT line FROM log_lines ORDER BY ts")] == ["new 6", "new 7", "new 8", "new 9"]
