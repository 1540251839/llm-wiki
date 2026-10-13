"""Runtime ignore rules must not hide similarly named source directories."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def ignore_repository(tmp_path):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is unavailable for repository ignore regressions")
    repository = tmp_path / "ignore-contract"
    repository.mkdir()
    subprocess.run(
        [git, "init", "--template=", "-q", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    shutil.copyfile(Path(__file__).parents[1] / ".gitignore", repository / ".gitignore")
    return git, repository


@pytest.mark.parametrize(
    "relative_path",
    [
        "data/researchwiki.db",
        "data/private/deepseek-key",
        "data/syntheses/topic.json",
        "data/synthesis_checkpoints/chunk.json",
        "raw/papers/source.pdf",
        "wiki/papers/paper.md",
        "wiki/manifest.json",
        "logs/researchwiki.log",
        ".env",
        ".env.local",
    ],
)
def test_root_runtime_data_and_local_credentials_are_ignored(ignore_repository, relative_path):
    git, repository = ignore_repository
    result = subprocess.run(
        [git, "-c", "core.excludesFile=", "-C", str(repository), "check-ignore", "--no-index", "--", relative_path],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == relative_path


@pytest.mark.parametrize(
    "relative_path",
    [
        "researchwiki/wiki/entities.py",
        "researchwiki/data/contract.py",
        "researchwiki/raw/reader.py",
        "researchwiki/logs/parser.py",
        "tests/data/fixture.json",
        "docs/wiki/design.md",
        ".env.example",
    ],
)
def test_nested_source_directories_and_empty_example_remain_publishable(ignore_repository, relative_path):
    git, repository = ignore_repository
    result = subprocess.run(
        [git, "-c", "core.excludesFile=", "-C", str(repository), "check-ignore", "--no-index", "--", relative_path],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not result.stdout.strip()
