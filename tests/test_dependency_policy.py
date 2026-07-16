"""Public dependency compatibility contracts."""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_RUNTIME_UPPER_BOUNDS = {
    "cryptography": "<50",
    "jsonschema": "<5",
    "langgraph": "<2",
    "pydantic": "<3",
    "pyyaml": "<7",
}
EXPECTED_OPTIONAL_RUNTIME_UPPER_BOUNDS = {
    "http": {"httpx": "<1"},
    "llm": {"google-genai": "<3"},
}


def _dependency_map(dependencies: list[str]) -> dict[str, tuple[str, ...]]:
    dependency_contracts: dict[str, tuple[str, ...]] = {}
    for dependency in dependencies:
        specifier_index = next(
            index for index, character in enumerate(dependency) if character in "<>=!~"
        )
        dependency_name = dependency[:specifier_index]
        specifiers = tuple(dependency[specifier_index:].split(","))
        assert dependency_name not in dependency_contracts
        dependency_contracts[dependency_name] = specifiers
    return dependency_contracts


def _assert_bounded_dependencies(
    dependencies: list[str],
    expected_upper_bounds: dict[str, str],
) -> None:
    dependency_contracts = _dependency_map(dependencies)

    assert set(dependency_contracts) == set(expected_upper_bounds)
    for dependency_name, specifiers in dependency_contracts.items():
        assert any(specifier.startswith(">") for specifier in specifiers)
        assert expected_upper_bounds[dependency_name] in specifiers
        assert all(not specifier.startswith("==") for specifier in specifiers)


def test_public_dependency_ranges_are_explicit_and_bounded() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        project_document = tomllib.load(project_file)

    project_configuration = project_document["project"]

    _assert_bounded_dependencies(
        project_configuration["dependencies"],
        EXPECTED_RUNTIME_UPPER_BOUNDS,
    )
    for extra_name, expected_upper_bounds in EXPECTED_OPTIONAL_RUNTIME_UPPER_BOUNDS.items():
        _assert_bounded_dependencies(
            project_configuration["optional-dependencies"][extra_name],
            expected_upper_bounds,
        )


def test_development_runtime_dependencies_match_public_extras() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        project_document = tomllib.load(project_file)

    optional_dependencies = project_document["project"]["optional-dependencies"]
    development_dependencies = _dependency_map(optional_dependencies["dev"])

    for extra_name in EXPECTED_OPTIONAL_RUNTIME_UPPER_BOUNDS:
        for dependency_name, specifiers in _dependency_map(
            optional_dependencies[extra_name]
        ).items():
            assert development_dependencies[dependency_name] == specifiers
