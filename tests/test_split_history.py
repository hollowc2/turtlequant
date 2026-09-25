from __future__ import annotations

import gzip
import importlib.util
from pathlib import Path

from turtlequant.history import HISTORY_JSONL

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "split_history.py"
_SPEC = importlib.util.spec_from_file_location("split_history", _PATH)
split_history = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(split_history)


def test_split_keeps_trades_archives_diagnostics_and_backs_up(tmp_path):
    lines = [
        '{"event":"open","market_id":"m"}',
        '{"event":"scan_summary","parse_attempted":3}',
        '{"event":"shadow_quote","reason":"x"}',
        "not json",
        '{"event":"close","market_id":"m","pnl":1}',
    ]
    (tmp_path / HISTORY_JSONL).write_text("\n".join(lines) + "\n")

    assert split_history.main(["--state-dir", str(tmp_path), "--dry-run"]) == 0
    assert len((tmp_path / HISTORY_JSONL).read_text().splitlines()) == 5  # dry-run is read-only

    counts = split_history.split(tmp_path, dry_run=False, drop_diagnostics=False)

    assert counts == {"trade": 3, "diagnostic": 2, "unparseable": 1}
    assert (tmp_path / HISTORY_JSONL).read_text().splitlines() == [lines[0], lines[3], lines[4]]
    (archive,) = tmp_path.glob("turtlequant-diagnostics-archive-*.jsonl.gz")
    assert gzip.open(archive, "rt").read().splitlines() == [lines[1], lines[2]]
    (backup,) = tmp_path.glob(f"{HISTORY_JSONL}.bak-*")
    assert len(backup.read_text().splitlines()) == 5
