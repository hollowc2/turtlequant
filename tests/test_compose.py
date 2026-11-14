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
        body = re.search(rf"^  {name}:\n((?:    .*\n|\s*\n|  #.*\n)*)", services, re.M).group(1)
        # extends carries over init: true from *bot-common.
        assert re.search(r"^      file: docker-compose\.yml$", body, re.M), f"{name} does not extend shadow"
        container = re.search(r"^    container_name: (\S+)$", body, re.M)
        assert container, f"{name} would inherit shadow's container_name"
        assert container.group(1) not in shadow


def test_live_sizes_from_its_own_starting_nav():
    # Without this, live inherits shadow's STARTING_NAV of 1000.
    assert re.search(r'^      STARTING_NAV: "100\.0"$', _live(), re.M)


def test_live_daily_loss_halt_is_scaled_to_its_wallet():
    # The halt is in dollars; shadow's $50 default is half of a $100 wallet.
    assert re.search(r'^      MAX_DAILY_LOSS: "5\.0"$', _live(), re.M)


def test_live_exporter_leaves_shadow_port_free():
    assert "8004:8004" not in _live()
    assert re.search(r'^      STRATEGY: "turtlequant-live"$', _live(), re.M)


def test_live_takes_no_legacy_wallet_aliases():
    # The shared helios .env holds another bot's PRIVATE_KEY.
    for alias in ("PRIVATE_KEY:", "CLOB_API_KEY:", "FUNDER_ADDRESS:", "SIGNATURE_TYPE:"):
        assert not re.search(rf"^      {alias}", _live(), re.M), alias
