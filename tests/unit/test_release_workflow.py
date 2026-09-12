"""Release trust boundaries and executable version/distribution validation regressions."""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/publish.yml"
ASSETS = {
    "gryphon/templates/admin.html",
    "gryphon/static/admin.css",
    "gryphon/static/admin.js",
    "gryphon/static/analytics.js",
    "gryphon/compiler/templates/function.py.j2",
    "gryphon/compiler/templates/top_level_functions.py.j2",
    "gryphon/__main__.py",
}
PINS = {
    "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "astral-sh/setup-uv": "6b9c6063abd6010835644d4c2e1bef4cf5cd0fca",
}


def _mapping(value: object) -> dict[str, object]:
    """Narrow YAML objects without accepting implicit scalar coercion."""
    assert isinstance(value, dict) and all(isinstance(key, str) for key in value)
    return cast("dict[str, object]", value)


def _workflow() -> dict[str, object]:
    """Keep YAML 1.1 from treating the GitHub Actions event key as boolean true."""
    return _mapping(yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader))


def _job(name: str) -> dict[str, object]:
    """Return a named job from the actual shipped workflow."""
    return _mapping(_mapping(_workflow()["jobs"])[name])


def _steps(job: str = "build") -> list[dict[str, object]]:
    """Return structurally checked workflow steps."""
    steps = _job(job)["steps"]
    assert isinstance(steps, list)
    return [_mapping(step) for step in steps]


def _step(name: str, job: str = "build") -> dict[str, object]:
    """Locate one unique named step, failing on ambiguous workflow structure."""
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _python(name: str) -> str:
    """Extract the actual heredoc rather than duplicating its validation logic."""
    command = str(_step(name)["run"])
    script = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    ast.parse(script)
    return script


def _run_python(name: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run workflow validation without checkout imports or ambient operator configuration."""
    return subprocess.run(
        [sys.executable, "-I", "-c", _python(name)],
        cwd=cwd,
        env={"PATH": os.defpath, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_release_events_require_canonical_repository_and_tag() -> None:
    """Neither PR code nor a branch-selected manual run may reach either release job."""
    events = _mapping(_workflow()["on"])
    assert set(events) == {"release", "workflow_dispatch"}
    assert _mapping(events["release"])["types"] == ["published"]
    assert events["workflow_dispatch"] == ""
    for name in ("build", "publish"):
        condition = str(_job(name)["if"])
        assert "github.repository == 'hypen-code/gryphon'" in condition
        assert "github.ref_type == 'tag'" in condition
        assert "startsWith(github.ref, 'refs/tags/v')" in condition
        assert "github.event_name == 'workflow_dispatch'" in condition
        assert "github.event_name == 'release'" in condition and "github.event.action == 'published'" in condition
        assert "!github.event.release.draft" in condition and "!github.event.release.prerelease" in condition
    checkout = next(step for step in _steps() if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert _mapping(checkout["with"]) == {
        "ref": "${{ github.sha }}",
        "fetch-depth": "0",
        "persist-credentials": "false",
    }
    immutable = str(_step("Verify immutable tag checkout")["run"])
    assert 'git rev-parse "refs/tags/$RELEASE_TAG^{commit}"' in immutable
    assert "git rev-parse HEAD" in immutable and immutable.count('== "$GITHUB_SHA"') == 2


def test_release_permissions_isolate_oidc_from_repository_execution() -> None:
    """Only the protected publisher gets OIDC, with no checkout, build, or repository Python."""
    assert _mapping(_workflow()["permissions"]) == {"contents": "read"}
    assert "permissions" not in _job("build") and "environment" not in _job("build")
    publisher = _job("publish")
    assert _mapping(publisher["permissions"]) == {"contents": "read", "id-token": "write"}
    assert _mapping(publisher["environment"])["name"] == "pypi"
    assert publisher["needs"] == "build" and "needs.build.result == 'success'" in str(publisher["if"])
    assert len(_steps("publish")) == 3
    command = str(_step("Publish checked distributions with PyPI OIDC", "publish")["run"])
    assert "uv publish --no-config --trusted-publishing always" in command
    assert "--publish-url https://upload.pypi.org/legacy/" in command
    assert '"dist/gryphon_runtime-${RELEASE_TAG#v}.tar.gz"' in command
    assert '"dist/gryphon_runtime-${RELEASE_TAG#v}-py3-none-any.whl"' in command
    assert all(word not in command for word in ("python", "uv run", "uv build", "dist/*", "--token", "--password"))
    text = WORKFLOW.read_text()
    assert "secrets." not in text and text.count("id-token:") == 1
    assert "continue-on-error" not in text and "always()" not in text


def test_release_actions_are_verified_full_commit_pins_without_shared_caches() -> None:
    """Only reviewed immutable action revisions and the established uv version are used."""
    for step in [*_steps(), *_steps("publish")]:
        if "uses" not in step:
            continue
        action, revision = str(step["uses"]).split("@")
        assert re.fullmatch(r"[0-9a-f]{40}", revision) and PINS[action] == revision
        if action == "astral-sh/setup-uv":
            assert _mapping(step["with"])["version"] == "0.12.9"
            assert _mapping(step["with"])["enable-cache"] == "false"
    assert _mapping(_workflow()["concurrency"])["cancel-in-progress"] == "false"


def test_release_quality_gates_use_locked_dev_and_saas_before_upload() -> None:
    """Every full quality gate precedes the build and upload without a bypass condition."""
    commands = {
        "Ruff lint": "ruff check src/ tests/",
        "Ruff format": "ruff format --check src/ tests/",
        "Strict typing": "mypy --strict src/ tests/",
        "Tests with mandatory coverage": "pytest --cov=gryphon --cov-fail-under=90",
    }
    steps = _steps()
    build_index = steps.index(_step("Build sdist and wheel"))
    assert (
        _step("Install locked development and hosted environment")["run"] == "uv sync --locked --extra dev --extra saas"
    )
    assert "libpq5" in str(_step("Install system libpq")["run"])
    for name, command in commands.items():
        step = _step(name)
        assert step["run"] == f"uv run --frozen --extra dev --extra saas {command}"
        assert steps.index(step) < build_index and "if" not in step
    assert "uv build --no-sources --exclude-newer" in str(steps[build_index]["run"])
    assert "7 days ago" in str(steps[build_index]["run"])
    assert "upload-artifact@" in str(steps[-1]["uses"])


def test_release_artifacts_are_exact_same_run_and_not_merged() -> None:
    """Avoid wildcard artifact sources, cross-run substitution, and upload-on-failure."""
    upload = _steps()[-1]
    assert _mapping(upload["with"]) == {
        "name": "pypi-distributions",
        "path": "${{ runner.temp }}/release-dist/",
        "if-no-files-found": "error",
        "include-hidden-files": "false",
        "retention-days": "7",
    }
    download = _steps("publish")[1]
    assert _mapping(download["with"]) == {"name": "pypi-distributions", "path": "dist", "merge-multiple": "false"}
    assert "if" not in upload
    assert _steps().index(_step("Installed wheel automatic stdio and MCP smoke")) < _steps().index(upload)


@pytest.mark.parametrize("tag", ["main", "v2.0", "v2.0.0rc1", "v02.0.0", "v2.0.0\n", "v2.0.0; echo unsafe", "v2.0.0"])
def test_release_tag_guard_accepts_only_real_version_syntax(tag: str) -> None:
    """Execute the pre-checkout shell gate, including shell metacharacter rejection."""
    command = str(_step("Require a version tag")["run"])
    assert "${{" not in command
    result = subprocess.run(
        ["bash", "-c", command], env={"PATH": os.defpath, "RELEASE_TAG": tag, "EVENT_TAG": tag}, check=False
    )
    assert (result.returncode == 0) == (tag == "v2.0.0")


@pytest.mark.parametrize("tag,package_version", [("v2.0.0", "2.0.0"), ("v2.0.1", "2.0.0"), ("v2.0.0", "2.0.1")])
def test_release_source_version_mismatches_fail_closed(tmp_path: Path, tag: str, package_version: str) -> None:
    """Run the shipped AST/TOML validator against matching and mismatching fixture sources."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname="gryphon-runtime"\nversion="2.0.0"\n')
    package = tmp_path / "src/gryphon"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'__version__ = "{package_version}"\n')
    result = _run_python("Verify source versions", tmp_path, RELEASE_TAG=tag)
    assert (result.returncode == 0) == (tag == "v2.0.0" and package_version == "2.0.0"), result.stderr


def _distribution_fixture(root: Path, mutation: str) -> Path:
    """Produce minimal local archives for positive and tampered-artifact validation."""
    (root / "pyproject.toml").write_text(
        '[project]\nname="gryphon-runtime"\nversion="2.0.0"\nrequires-python=">=3.13"\n'
    )
    dist = root / "dist"
    dist.mkdir()
    stem = "gryphon_runtime-2.0.0"
    version = "2.0.1" if mutation == "version" else "2.0.0"
    metadata = (
        f"Metadata-Version: 2.3\nName: gryphon-runtime\nVersion: {version}\nRequires-Python: >=3.13\n"
        "Provides-Extra: saas\nRequires-Dist: fastmcp==4.0.2\n\nPackage description\n"
    ).encode()
    assets = ASSETS - {"gryphon/templates/admin.html"} if mutation == "asset" else ASSETS
    if mutation == "analytics_asset":
        assets = assets - {"gryphon/static/analytics.js"}
    with zipfile.ZipFile(dist / f"{stem}-py3-none-any.whl", "w") as wheel:
        for asset in assets:
            wheel.writestr(asset, "fixture")
        wheel.writestr(f"{stem}.dist-info/METADATA", metadata)
        entry = "wrong:main" if mutation == "entrypoint" else "gryphon.__main__:main"
        wheel.writestr(f"{stem}.dist-info/entry_points.txt", f"[console_scripts]\ngryphon = {entry}\n")
        wheel.writestr(f"{stem}.dist-info/licenses/LICENSE", "MIT")
    with tarfile.open(dist / f"{stem}.tar.gz", "w:gz") as sdist:
        content = dict.fromkeys(
            [*(f"src/{asset}" for asset in assets), "README.md", "LICENSE", "pyproject.toml"], b"fixture"
        )
        content["PKG-INFO"] = metadata
        for name, data in content.items():
            info = tarfile.TarInfo(f"{stem}/{name}")
            info.size = len(data)
            sdist.addfile(info, io.BytesIO(data))
    if mutation == "extra":
        (dist / "other.whl").write_bytes(b"unreviewed")
    return dist


@pytest.mark.parametrize("mutation", ["none", "version", "asset", "analytics_asset", "entrypoint", "extra"])
def test_release_distribution_validation_rejects_tampering(tmp_path: Path, mutation: str) -> None:
    """Validate actual ZIP/tar fixtures without extracting or executing their contents."""
    dist = _distribution_fixture(tmp_path, mutation)
    result = _run_python("Verify distribution metadata and assets", tmp_path, DIST_DIR=str(dist))
    assert (result.returncode == 0) == (mutation == "none"), result.stderr


def test_release_metadata_and_packaging_assets_are_declared() -> None:
    """Keep source metadata, CLI, version, and the packaged non-Python templates coherent."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["name"] == "gryphon-runtime"
    assert project["project"]["scripts"] == {"gryphon": "gryphon.__main__:main"}
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/gryphon"]
    assert "saas" in project["project"]["optional-dependencies"]
    for asset in ASSETS:
        assert (ROOT / "src" / asset).is_file()
    result = _run_python("Verify source versions", ROOT, RELEASE_TAG=f"v{project['project']['version']}")
    assert result.returncode == 0, result.stderr


def test_release_wheel_smoke_is_isolated_and_exercises_automatic_stdio() -> None:
    """Require a base-only locked wheel install and real MCP discovery, execution, and replay."""
    install = str(_step("Install wheel with locked runtime dependencies")["run"])
    assert "uv export --frozen --no-dev --no-emit-project" in install
    assert "--require-hashes" in install and "--no-deps" in install and "uv pip check" in install
    assert "--extra" not in install and "--editable" not in install
    step = str(_step("Installed wheel automatic stdio and MCP smoke")["run"])
    assert "mktemp -d" in step and 'cd "$SMOKE_DIR"' in step and "env -i HOME=" in step and " -I - " in step
    script = _python("Installed wheel automatic stdio and MCP smoke")
    for invariant in (
        'assert not (root / ".env").exists()',
        "assert not Path(gryphon.__file__).resolve().is_relative_to(checkout)",
        "is_relative_to(Path(sys.prefix))",
        'args=["stdio"]',
        "GRYPHON_SWAGGERS",
        "GRYPHON_STATE_DIR",
        "await client.list_tools()",
        'client.call_tool("get_functions"',
        'client.call_tool("execute_code"',
        'client.call_tool("run_cached_code"',
        '"tool_calls"] == 0',
        "*/compiled/offline/manifest.json",
    ):
        assert invariant in script
    assert "sys.path" not in script and "--env-file" not in script and "PYTHONPATH" not in script
