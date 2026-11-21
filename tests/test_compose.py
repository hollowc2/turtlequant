"""Guards on the compose files' process handling.

Python as a container's PID 1 never reaps children, so a healthcheck killed
at its timeout stays a zombie; on a CPU-starved helios that left 1,122 behind
the exporter. Plain-text checks: the image ships no YAML parser.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = sorted(ROOT.glob("docker-compose*.yml"))


def _block(text: str, header: str) -> str:
    """Lines under a top-level key, up to the next top-level key."""
    match = re.search(rf"^{re.escape(header)}.*\n((?:[ #].*\n|\n)*)", text, re.M)
    assert match, f"{header} not found"
    return match.group(1)


def test_shared_service_config_runs_an_init():
    common = _block((ROOT / "docker-compose.yml").read_text(), "x-bot-common:")
    assert re.search(r"^  init: true$", common, re.M)


def test_every_service_inherits_the_shared_config():
    services = _block((ROOT / "docker-compose.yml").read_text(), "services:")
    names = re.findall(r"^  ([\w-]+):$", services, re.M)
    assert names
    for name in names:
        if name == "turtlequant-vpn":  # a third-party image, not a copy of a shadow service
            continue
        body = re.search(rf"^  {name}:\n((?:    .*\n|\s*\n|  #.*\n)*)", services, re.M).group(1)
        assert "<<: *bot-common" in body, f"{name} does not merge *bot-common, so has no init"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_healthchecks_do_not_start_python(path):
    # A python start costs ~50x a shell check and timed out under CPU steal.
    for line in path.read_text().splitlines():
        if re.match(r"\s*test:", line):
            assert "python" not in line, f"{path.name}: {line.strip()}"


def _live() -> str:
    return (ROOT / "docker-compose.live.yml").read_text()


def _live_service(name: str) -> str:
    """The text of one service under ``services:`` in the live compose file."""
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\s*\n|  #.*\n)*)", _live(), re.M)
    assert match, f"{name} not found"
    return match.group(1)


def test_live_is_its_own_project():
    # A shared project name would let `up` on either file recreate the other's
    # containers, so live would replace shadow instead of running beside it.
    assert re.search(r"^name: turtlequant-live$", _live(), re.M)


def test_live_services_extend_shadow_and_rename_containers():
    services = _block(_live(), "services:")
    names = re.findall(r"^  ([\w-]+):$", services, re.M)
    shadow = set(re.findall(r"^    container_name: (\S+)$", (ROOT / "docker-compose.yml").read_text(), re.M))
    assert names
    for name in names:
        if name == "turtlequant-vpn":  # a third-party image, not a copy of a shadow service
            continue
        body = re.search(rf"^  {name}:\n((?:    .*\n|\s*\n|  #.*\n)*)", services, re.M).group(1)
        # extends carries over init: true from *bot-common.
        assert re.search(r"^      file: docker-compose\.yml$", body, re.M), f"{name} does not extend shadow"
        container = re.search(r"^    container_name: (\S+)$", body, re.M)
        assert container, f"{name} would inherit shadow's container_name"
        assert container.group(1) not in shadow


def test_live_sizes_from_its_own_starting_nav():
    # Without this, live inherits shadow's STARTING_NAV of 1000.
    assert re.search(r'^      STARTING_NAV: "100\.0"$', _live(), re.M)


def test_live_and_shadow_share_the_daily_loss_halt():
    # It is a share of NAV, so one default fits both; a dollar value would not.
    assert "MAX_DAILY_LOSS" not in _live()


def test_live_exporter_leaves_shadow_port_free():
    assert "8004:8004" not in _live()
    assert re.search(r'^      STRATEGY: "turtlequant-live"$', _live(), re.M)


def test_live_takes_no_legacy_wallet_aliases():
    # The shared helios .env holds another bot's PRIVATE_KEY.
    for alias in ("PRIVATE_KEY:", "CLOB_API_KEY:", "FUNDER_ADDRESS:", "SIGNATURE_TYPE:"):
        assert not re.search(rf"^      {alias}", _live(), re.M), alias


def test_live_exporter_reads_shadow_state_read_only_for_fill_pairing():
    exporter = re.search(r"^  turtlequant-grafana-exporter-live:\n((?:    .*\n|\s*\n)*)", _live(), re.M).group(1)
    assert re.search(r"^      - /opt/turtlequant/state:/opt/turtlequant/shadow-state:ro$", exporter, re.M)
    assert re.search(r'^      PAIR_STATE_DIR: "/opt/turtlequant/shadow-state"$', exporter, re.M)
    # Every state mount on the exporter is read-only.
    for mount in re.findall(r"^      - (/opt/turtlequant/\S+)$", exporter, re.M):
        assert mount.endswith(":ro"), mount


def test_live_bot_trades_only_through_the_vpn():
    # Polymarket rejects orders from helios's own address. The bot must share
    # the VPN container's network, wait for it to be healthy, and publish no
    # ports of its own (a shared namespace cannot).
    bot = _live_service("turtlequant-bot-live")
    assert re.search(r'^    network_mode: "service:turtlequant-vpn"$', bot, re.M)
    assert re.search(r"^      turtlequant-vpn:\n        condition: service_healthy$", bot, re.M)
    assert "ports:" not in bot


def test_vpn_credentials_come_from_the_env_file_not_the_repo():
    vpn = _live_service("turtlequant-vpn")
    assert '"${PIA_USERNAME:-}"' in vpn and '"${PIA_PASSWORD:-}"' in vpn
    assert re.search(r"image: qmcgaw/gluetun:v\d+\.\d+\.\d+$", vpn, re.M)  # pinned, never :latest
