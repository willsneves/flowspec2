"""Public dependency compatibility contracts."""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_RUNTIME_DEPENDENCIES = {
    "cryptography>=49.0.0,<50",
    "jsonschema>=4.20,<5",
    "langgraph>=1.2.6,<2",
    "pydantic>=2.7,<3",
    "pyyaml>=6.0.3,<7",
}
EXPECTED_OPTIONAL_RUNTIME_DEPENDENCIES = {
    "http": ["httpx>=0.27,<1"],
    "llm": ["google-genai>=1.0,<3"],
}


def test_public_dependency_ranges_are_explicit_and_bounded() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        project_document = tomllib.load(project_file)

    project_configuration = project_document["project"]

    assert set(project_configuration["dependencies"]) == EXPECTED_RUNTIME_DEPENDENCIES
    for extra_name, expected_dependencies in EXPECTED_OPTIONAL_RUNTIME_DEPENDENCIES.items():
        assert project_configuration["optional-dependencies"][extra_name] == expected_dependencies


def test_development_runtime_dependencies_match_public_extras() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        project_document = tomllib.load(project_file)

    development_dependencies = set(project_document["project"]["optional-dependencies"]["dev"])

    assert {
        dependency
        for dependencies in EXPECTED_OPTIONAL_RUNTIME_DEPENDENCIES.values()
        for dependency in dependencies
    } <= development_dependencies
