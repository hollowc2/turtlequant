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


def _row(title: str) -> list[dict]:
    """Panels under a top-level row, in dashboard order."""
    panels = DASHBOARD["panels"]
    start = next(i for i, panel in enumerate(panels) if panel["type"] == "row" and panel["title"] == title)
    end = next((i for i in range(start + 1, len(panels)) if panels[i]["type"] == "row"), len(panels))
    return panels[start + 1:end]


def test_live_vs_shadow_row_follows_account_and_ignores_the_strategy_selector():
    rows = [panel["title"] for panel in DASHBOARD["panels"] if panel["type"] == "row"]
    assert rows[rows.index("Account") + 1] == "Live vs Shadow"
    panels = _row("Live vs Shadow")
    assert {panel["title"] for panel in panels} == {
        "Return Since Start",
        "Wallet vs Books (Live)",
        "Decision Funnel per Hour",
        "Size-Floor Outcomes per Hour",
        "Order Integrity",
        "Live Fills vs Shadow",
    }
    for panel in panels:
        exprs = [target["expr"] for target in panel["targets"]]
        assert not any("$strategy" in expr for expr in exprs), panel["title"]
        # Each panel compares the bots, or is explicitly about live (wallet, fills).
        both = all('strategy=~"turtlequant|turtlequant-live"' in expr for expr in exprs)
        live = all('strategy="turtlequant-live"' in expr for expr in exprs)
        assert both or (live and "Live" in panel["title"]), panel["title"]


def test_live_vs_shadow_panels_use_the_dashboard_datasource():
    for panel in _row("Live vs Shadow"):
        assert panel["datasource"] == {"type": "prometheus", "uid": "prometheus"}
        assert all(target["datasource"] == panel["datasource"] for target in panel["targets"])
