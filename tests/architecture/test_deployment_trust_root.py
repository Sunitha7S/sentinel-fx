"""The deployed image carries the reviewed quality trust root and nothing else (ADR 0015 §4).

The image bakes in ``config/quality`` at the path the loader reads (``QUALITY_CONFIG_DIR``),
verifies it against the pinned trust root while building (the build fails otherwise), and
installs no test tooling, so a fixture registry authorises nothing at run time. Nothing is
mounted from the host. These tests read the Dockerfile; they do not build the image.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from sentinel.config.quality_loader import APPROVALS_DIRNAME, QUALITY_CONFIG_DIR

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
COMPOSE = ROOT / "docker" / "compose.yaml"


def _instructions() -> list[tuple[str, str]]:
    lines = []
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            keyword, _, rest = line.partition(" ")
            lines.append((keyword.upper(), rest.strip()))
    return lines


def _index(instructions: list[tuple[str, str]], keyword: str, pattern: str) -> int:
    hits = [
        i for i, (k, rest) in enumerate(instructions) if k == keyword and re.search(pattern, rest)
    ]
    assert len(hits) == 1, f"expected exactly one {keyword} matching {pattern!r}, found {hits}"
    return hits[0]


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_image_bakes_in_the_packaged_configuration_where_the_loader_reads_it() -> None:
    steps = _instructions()
    workdir = Path(next(rest for k, rest in steps if k == "WORKDIR"))
    assert QUALITY_CONFIG_DIR.relative_to(ROOT) == Path("config/quality")
    copy = _index(steps, "COPY", r"^config/quality\s")
    assert steps[copy][1] == "config/quality ./config/quality"
    assert "--chown" not in steps[copy][1]  # root-owned: read-only to the runtime user
    _index(steps, "COPY", r"^src \./src$")  # the loader resolves config/ next to src/
    assert workdir == Path("/app")
    configs = [rest for k, rest in steps if k in {"COPY", "ADD"} and "config" in rest]
    assert configs == ["config/quality ./config/quality"]  # nothing else from config/


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_build_verifies_the_trust_root_before_the_runtime_user_exists() -> None:
    steps = _instructions()
    copy = _index(steps, "COPY", r"^config/quality\s")
    verify = _index(steps, "RUN", r"\bsentinel quality-registry$")
    user = _index(steps, "USER", r"^sentinel$")
    assert copy < verify < user
    assert not any(k in {"VOLUME", "ADD"} for k, _ in steps)  # no host content at run time


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_image_installs_no_test_runner() -> None:
    """Fixture registries authorise only inside a pytest session; the image has no pytest."""
    steps = _instructions()
    sync = _index(steps, "RUN", r"\buv sync\b")
    assert "--no-dev" in steps[sync][1].split()
    assert "--frozen" in steps[sync][1].split()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = project["project"]["dependencies"]
    assert not any(re.match(r"pytest\b", d) for d in runtime)
    assert any(re.match(r"pytest\b", d) for d in project["dependency-groups"]["dev"])


def test_compose_does_not_mount_configuration_into_the_application() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "config/quality" not in text
    assert "/app" not in text


def test_the_packaged_directory_holds_only_files_the_loader_reads() -> None:
    """Every shipped file is covered by the trust root; nothing unpinned rides along."""
    for path in QUALITY_CONFIG_DIR.rglob("*"):
        rel = path.relative_to(QUALITY_CONFIG_DIR)
        if path.is_dir():
            assert rel == Path(APPROVALS_DIRNAME), rel
            continue
        assert path.suffix == ".yaml", rel
        assert rel.parent in (Path(), Path(APPROVALS_DIRNAME)), rel
