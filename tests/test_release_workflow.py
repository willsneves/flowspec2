"""Release workflow security and build-once contracts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
IMMUTABLE_ACTION_PATTERN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _release_workflow() -> dict[str, Any]:
    workflow = yaml.load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return cast(dict[str, Any], workflow)


def test_release_actions_are_pinned_to_immutable_commits() -> None:
    workflow = _release_workflow()
    action_references = [
        step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    ]

    assert action_references
    assert all(IMMUTABLE_ACTION_PATTERN.fullmatch(reference) for reference in action_references)


def test_pypi_publication_uses_only_oidc_and_the_verified_artifacts() -> None:
    workflow = _release_workflow()
    publish_job = workflow["jobs"]["publish-pypi"]

    assert publish_job["needs"] == "build"
    assert publish_job["environment"]["name"] == "pypi"
    assert publish_job["permissions"] == {"contents": "read", "id-token": "write"}
    assert "password" not in RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8").lower()
    assert any(
        step.get("run") == "make release-check RELEASE_ARTIFACTS=build/release"
        for step in publish_job["steps"]
    )


def test_github_release_waits_for_successful_pypi_publication() -> None:
    workflow = _release_workflow()
    github_release_job = workflow["jobs"]["publish-github"]

    assert github_release_job["needs"] == "publish-pypi"
    release_command = github_release_job["steps"][-1]["run"]
    assert "--verify-tag" in release_command
    assert "build/release/*" in release_command
