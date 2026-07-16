"""GitHub Pages workflow security contracts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAGES_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "pages.yml"
IMMUTABLE_ACTION_PATTERN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _pages_workflow() -> dict[str, Any]:
    workflow = yaml.load(PAGES_WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return cast(dict[str, Any], workflow)


def test_pages_actions_are_pinned_to_immutable_commits() -> None:
    workflow = _pages_workflow()
    action_references = [
        step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    ]

    assert action_references
    assert all(IMMUTABLE_ACTION_PATTERN.fullmatch(reference) for reference in action_references)


def test_pages_deploys_only_main_with_minimum_permissions() -> None:
    workflow = _pages_workflow()
    deployment_job = workflow["jobs"]["deploy"]
    verification_job = workflow["jobs"]["verify"]

    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert deployment_job["needs"] == "build"
    assert deployment_job["permissions"] == {"pages": "write", "id-token": "write"}
    assert deployment_job["environment"]["name"] == "github-pages"
    assert verification_job["needs"] == "deploy"
    assert "--verify-url" in verification_job["steps"][-1]["run"]
