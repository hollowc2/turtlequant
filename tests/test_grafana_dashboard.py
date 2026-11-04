import json
import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = json.loads((ROOT / "grafana/dashboards/turtlequant.json").read_text())


def _panels(panels: list[dict]) -> Iterator[dict]:
    for panel in panels:
        yield panel
        yield from _panels(panel.get("panels", []))


def test_every_queried_turtlequant_metric_is_exported():
    exporter = (ROOT / "scripts/grafana_exporter.py").read_text()
    exported = set(re.findall(r'"(turtlequant_[a-z0-9_]+)"', exporter))
    queried = {
        name
        for panel in _panels(DASHBOARD["panels"])
        for target in panel.get("targets", [])
        for name in re.findall(r"turtlequant_[a-z0-9_]+", target["expr"])
    }
    assert queried
    assert queried - exported == set()


def test_dashboard_keeps_its_uid_and_documented_rows():
    assert DASHBOARD["uid"] == "turtlequant-monitor"
    rows = {panel["title"] for panel in DASHBOARD["panels"] if panel["type"] == "row"}
    # README.md and docs/OPS.md send operators to this row by name.
    assert "Phase 1 Shadow Soak" in rows


def test_panel_ids_are_unique_and_rows_do_not_overlap():
    panels = list(_panels(DASHBOARD["panels"]))
    ids = [panel["id"] for panel in panels]
    assert len(ids) == len(set(ids))
    cells = set()
    for panel in DASHBOARD["panels"]:
        pos = panel["gridPos"]
        for x in range(pos["x"], pos["x"] + pos["w"]):
            for y in range(pos["y"], pos["y"] + pos["h"]):
                assert (x, y) not in cells, panel["title"]
                cells.add((x, y))
