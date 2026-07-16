"""Repository automation security contracts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = PROJECT_ROOT / ".github" / "workflows"
DEPENDABOT_PATH = PROJECT_ROOT / ".github" / "dependabot.yml"
IMMUTABLE_ACTION_PATTERN = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _yaml_document(configuration_path: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.load(configuration_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader),
    )


def test_every_workflow_action_is_pinned_to_an_immutable_commit() -> None:
    action_references = [
        step["uses"]
        for workflow_path in sorted(WORKFLOW_DIRECTORY.glob("*.yml"))
        for job in _yaml_document(workflow_path)["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]

    assert action_references
    assert all(IMMUTABLE_ACTION_PATTERN.fullmatch(reference) for reference in action_references)


def test_dependabot_covers_locked_dependencies_and_workflow_actions() -> None:
    dependabot_configuration = _yaml_document(DEPENDABOT_PATH)
    configured_ecosystems = {
        update_configuration["package-ecosystem"]: update_configuration
        for update_configuration in dependabot_configuration["updates"]
    }

    assert dependabot_configuration["version"] == "2"
    assert set(configured_ecosystems) == {"uv", "github-actions"}
    assert all(
        update_configuration["directory"] == "/"
        and update_configuration["schedule"]["interval"] == "weekly"
        for update_configuration in configured_ecosystems.values()
    )
