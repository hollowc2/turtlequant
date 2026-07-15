from turtlequant.history import HISTORY_JSONL, append_history, load_history


def test_append_history_preserves_legacy_events(tmp_path):
    (tmp_path / "turtlequant-history.json").write_text('[{"event":"open"}]')

    append_history(tmp_path, {"event": "close", "pnl": 1})

    assert (tmp_path / HISTORY_JSONL).read_text() == '{"event":"close","pnl":1}\n'
    assert load_history(tmp_path) == [{"event": "open"}, {"event": "close", "pnl": 1}]
