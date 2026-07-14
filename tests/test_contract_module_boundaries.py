"""Characterize contract-module entry points and diagnostic stability."""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

from flowspec2.compiler_contracts import (
    bind_await_external,
    validate_await_external_definition,
    validate_await_external_targets,
    validate_entry_args_schema,
    validate_flow_tool_contracts,
)
from flowspec2.semantic_source_contracts import (
    derive_contracts,
    domain_contracts,
    entry_schema_contracts,
    path_contracts,
    predicate_contracts,
    rail_reference_contracts,
    slot_contracts,
    state_writer_contracts,
)
from flowspec2.semantics import semantic_diagnostics

COMPILER_CONTRACT_ENTRY_POINTS = (
    bind_await_external,
    validate_await_external_definition,
    validate_await_external_targets,
    validate_entry_args_schema,
    validate_flow_tool_contracts,
)
SEMANTIC_CONTRACT_ENTRY_POINTS = (
    derive_contracts,
    domain_contracts,
    entry_schema_contracts,
    path_contracts,
    predicate_contracts,
    rail_reference_contracts,
    slot_contracts,
    state_writer_contracts,
)
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "flowspec2"
CONTRACT_MODULE_PATHS = tuple(
    SOURCE_ROOT / module_name
    for module_name in (
        "compiler_contracts.py",
        "compiler_resume_contracts.py",
        "compiler_schema_relations.py",
        "compiler_tool_contracts.py",
        "compiler_value_contracts.py",
        "semantic_derive_contracts.py",
        "semantic_path_contracts.py",
        "semantic_predicate_contracts.py",
        "semantic_schema_contracts.py",
        "semantic_source_contracts.py",
        "semantic_state_contracts.py",
    )
)


def test_contract_facades_keep_explicit_callable_entry_points() -> None:
    assert all(callable(entry_point) for entry_point in COMPILER_CONTRACT_ENTRY_POINTS)
    assert all(callable(entry_point) for entry_point in SEMANTIC_CONTRACT_ENTRY_POINTS)


def test_contract_modules_do_not_import_private_cross_module_symbols() -> None:
    private_imports: list[str] = []
    for contract_module_path in CONTRACT_MODULE_PATHS:
        syntax_tree = ast.parse(contract_module_path.read_text(encoding="utf-8"))
        private_imports.extend(
            f"{contract_module_path.name}:{imported_name.name}"
            for import_statement in ast.walk(syntax_tree)
            if isinstance(import_statement, ast.ImportFrom) and import_statement.level > 0
            for imported_name in import_statement.names
            if imported_name.name.startswith("_")
        )

    assert private_imports == []


def test_semantic_contract_partition_preserves_exact_diagnostic_identity(
    buraco_doc: dict[str, Any],
) -> None:
    invalid_flow = copy.deepcopy(buraco_doc)
    invalid_flow["uses"].append(copy.deepcopy(invalid_flow["uses"][0]))
    invalid_flow["uses"].append({"ref": "unused@1"})

    assert tuple(
        (diagnostic.code, diagnostic.path, diagnostic.message)
        for diagnostic in semantic_diagnostics(invalid_flow)
    ) == (
        (
            "FLOWSPEC_SEMANTIC_DUPLICATE_USE_DECLARATION",
            "/uses/2/ref",
            "subflow 'address@1' is declared more than once",
        ),
        (
            "FLOWSPEC_SEMANTIC_ORPHAN_USE_DECLARATION",
            "/uses/3/ref",
            "subflow 'unused@1' is declared but has no path anchor",
        ),
    )
