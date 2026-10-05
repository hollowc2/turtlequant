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
