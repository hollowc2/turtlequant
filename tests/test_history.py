from turtlequant.history import HISTORY_JSONL, append_history, load_history


def test_append_history_preserves_legacy_events(tmp_path):
    (tmp_path / "turtlequant-history.json").write_text('[{"event":"open"}]')

    append_history(tmp_path, {"event": "close", "pnl": 1})

    assert (tmp_path / HISTORY_JSONL).read_text() == '{"event":"close","pnl":1}\n'
    assert load_history(tmp_path) == [{"event": "open"}, {"event": "close", "pnl": 1}]


def test_diagnostics_go_to_rotated_file_and_trades_stay_in_history(tmp_path):
    from turtlequant.history import DIAGNOSTICS_JSONL, _append_diagnostic, diagnostics_paths

    append_history(tmp_path, {"event": "open", "market_id": "m"})
    append_history(tmp_path, {"event": "scan_summary", "parse_attempted": 3})

    assert load_history(tmp_path) == [{"event": "open", "market_id": "m"}]
    assert (tmp_path / DIAGNOSTICS_JSONL).read_text() == '{"event":"scan_summary","parse_attempted":3}\n'

    live = tmp_path / DIAGNOSTICS_JSONL
    for i in range(20):
        _append_diagnostic(live, f'{{"i":{i}}}\n' + " " * 40 + "\n", max_bytes=200, backups=2)

    paths = diagnostics_paths(tmp_path)
    assert [p.name for p in paths] == [f"{DIAGNOSTICS_JSONL}.2", f"{DIAGNOSTICS_JSONL}.1", DIAGNOSTICS_JSONL]
    assert all(p.stat().st_size <= 200 for p in paths)


def test_load_history_skips_only_an_unterminated_last_line(tmp_path):
    import pytest

    (tmp_path / HISTORY_JSONL).write_text('{"event":"open"}\n{"event":"clo')
    assert load_history(tmp_path) == [{"event": "open"}]

    (tmp_path / HISTORY_JSONL).write_text('{"event":"open"}\n{"event":"clo\n{"event":"close"}\n')
    with pytest.raises(ValueError):
        load_history(tmp_path)
