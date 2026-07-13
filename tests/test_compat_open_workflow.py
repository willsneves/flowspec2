"""Open Workflow Specification conversational-profile compatibility."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from flowspec2.compat.models import CompatibilityError
from flowspec2.compat.open_workflow import (
    OPEN_WORKFLOW_PROFILE_ID,
    OPEN_WORKFLOW_SCHEMA_VERSION,
    export_open_workflow,
    import_open_workflow,
    official_open_workflow_schema,
    validate_official_open_workflow,
    validate_open_workflow_profile,
)
from flowspec2.compiler import compile_flow

_FIXTURE_DIRECTORY = Path(__file__).with_name("fixtures") / "open_workflow"
_PORTABLE_FLOW_PATH = _FIXTURE_DIRECTORY / "portable.flow.json"
_ARBITRARY_WORKFLOW_PATH = _FIXTURE_DIRECTORY / "arbitrary.workflow.json"
_SCHEMA_DIRECTORY = Path(__file__).parents[1] / "src" / "flowspec2" / "compat" / "schemas"
_PROFILE_SCHEMA_PATH = _SCHEMA_DIRECTORY / "open-workflow-conversation-1.schema.json"
_VENDOR_SCHEMA_DIRECTORY = _SCHEMA_DIRECTORY / "vendor"
_OFFICIAL_SCHEMA_PATH = _VENDOR_SCHEMA_DIRECTORY / "open-workflow-1.0.3.workflow.yaml"
_OFFICIAL_LICENSE_PATH = _VENDOR_SCHEMA_DIRECTORY / "open-workflow-1.0.3.LICENSE"
_OFFICIAL_PROVENANCE_PATH = _VENDOR_SCHEMA_DIRECTORY / "open-workflow-1.0.3.provenance.json"


def _load_json_object(document_path: Path) -> dict[str, Any]:
    loaded_document = json.loads(document_path.read_text(encoding="utf-8"))
    assert isinstance(loaded_document, dict)
    return loaded_document


def _portable_flow() -> dict[str, Any]:
    return _load_json_object(_PORTABLE_FLOW_PATH)


def _schema_references(schema_fragment: Any) -> tuple[str, ...]:
    if isinstance(schema_fragment, dict):
        local_references = (
            (schema_fragment["$ref"],) if isinstance(schema_fragment.get("$ref"), str) else ()
        )
        nested_references = tuple(
            reference
            for nested_fragment in schema_fragment.values()
            for reference in _schema_references(nested_fragment)
        )
        return local_references + nested_references
    if isinstance(schema_fragment, list):
        return tuple(
            reference
            for nested_fragment in schema_fragment
            for reference in _schema_references(nested_fragment)
        )
    return ()


def test_profile_schema_is_valid_draft_2020_12() -> None:
    profile_schema = _load_json_object(_PROFILE_SCHEMA_PATH)

    jsonschema.Draft202012Validator.check_schema(profile_schema)


def test_official_schema_provenance_and_integrity_are_pinned() -> None:
    provenance = _load_json_object(_OFFICIAL_PROVENANCE_PATH)

    assert provenance["version"] == OPEN_WORKFLOW_SCHEMA_VERSION
    assert provenance["source_commit"] == "9b5b1da29e9d4fff2358580241e11aab22704a16"
    assert provenance["license_spdx"] == "Apache-2.0"
    assert (
        hashlib.sha256(_OFFICIAL_SCHEMA_PATH.read_bytes()).hexdigest()
        == (provenance["schema_sha256"])
    )
    assert (
        hashlib.sha256(_OFFICIAL_LICENSE_PATH.read_bytes()).hexdigest()
        == (provenance["license_sha256"])
    )


def test_official_schema_is_valid_self_contained_and_defensively_copied() -> None:
    official_schema = official_open_workflow_schema()

    jsonschema.Draft202012Validator.check_schema(official_schema)
    schema_references = _schema_references(official_schema)
    assert schema_references
    assert all(reference.startswith("#") for reference in schema_references)

    official_schema["type"] = "array"
    assert official_open_workflow_schema()["type"] == "object"


def test_export_uses_exact_profile_and_preserves_every_path_step() -> None:
    source_flow = _portable_flow()

    conversion = export_open_workflow(source_flow, namespace="citizen-services")
    exported_workflow = conversion.artifact

    assert conversion.report.diagnostics == ()
    assert exported_workflow["document"] == {
        "dsl": "1.0.3",
        "namespace": "citizen-services",
        "name": "payment-status",
        "version": "1.2.0",
        "metadata": {
            "flowspec2": {
                "profile": OPEN_WORKFLOW_PROFILE_ID,
                "original_flow_id": "payment_status",
                "sections": {
                    section_name: section_value
                    for section_name, section_value in source_flow.items()
                    if section_name not in {"flow", "path"}
                },
            }
        },
    }
    assert exported_workflow["input"] == {
        "schema": {
            "format": "json",
            "document": source_flow["route"]["entry_args_schema"],
        }
    }

    expected_step_kinds = (
        "slot",
        "confirm",
        "derive",
        "use",
        "await_external",
        "terminal",
    )
    for path_index, (task_item, expected_step_kind) in enumerate(
        zip(exported_workflow["do"], expected_step_kinds, strict=True)
    ):
        task_name, task_definition = next(iter(task_item.items()))
        assert task_name == f"path-{path_index}-{expected_step_kind}"
        assert task_definition == {
            "call": f"flowspec2.{expected_step_kind}",
            "with": {"step": source_flow["path"][path_index]},
            "metadata": {"path_index": path_index},
        }

    validate_open_workflow_profile(exported_workflow)
    validate_official_open_workflow(exported_workflow)


def test_export_and_import_round_trip_without_mutating_inputs() -> None:
    source_flow = _portable_flow()
    source_flow_snapshot = deepcopy(source_flow)

    exported_workflow = export_open_workflow(source_flow).artifact
    exported_workflow_snapshot = deepcopy(exported_workflow)
    imported_flow = import_open_workflow(exported_workflow)

    assert imported_flow.artifact == source_flow
    assert imported_flow.report.diagnostics == ()
    assert source_flow == source_flow_snapshot
    assert exported_workflow == exported_workflow_snapshot
    compile_flow(imported_flow.artifact)


def test_invalid_entry_schema_is_preserved_but_omitted_from_ows_input() -> None:
    source_flow = _portable_flow()
    source_flow["route"]["entry_args_schema"] = {"type": 42}

    conversion = export_open_workflow(source_flow)

    assert "input" not in conversion.artifact
    assert tuple(diagnostic.code for diagnostic in conversion.report.diagnostics) == (
        "open_workflow.entry_schema_omitted",
    )
    assert import_open_workflow(conversion.artifact).artifact == source_flow


def test_import_rejects_arbitrary_open_workflow_document() -> None:
    arbitrary_workflow = _load_json_object(_ARBITRARY_WORKFLOW_PATH)

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(arbitrary_workflow)

    assert captured_error.value.report.diagnostics[0].code == ("open_workflow.invalid_profile")


def test_import_rejects_wrong_profile_identifier() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    exported_workflow["document"]["metadata"]["flowspec2"]["profile"] = (
        "https://example.invalid/profile"
    )

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    assert captured_error.value.report.diagnostics[0].code == ("open_workflow.invalid_profile")


def test_import_aggregates_profile_schema_errors_in_path_order() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    exported_workflow["document"]["dsl"] = "2.0.0"
    exported_workflow["document"]["name"] = "different-name"
    exported_workflow["document"]["metadata"]["flowspec2"]["profile"] = (
        "https://example.invalid/profile"
    )

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    diagnostics = captured_error.value.report.diagnostics
    assert len(diagnostics) == 3
    assert all(diagnostic.code == "open_workflow.invalid_profile" for diagnostic in diagnostics)
    assert tuple(diagnostic.source_path for diagnostic in diagnostics) == tuple(
        sorted(diagnostic.source_path for diagnostic in diagnostics)
    )


def test_import_keeps_semantic_findings_when_structure_is_also_invalid() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    exported_workflow["document"]["name"] = "different-name"
    del exported_workflow["input"]["schema"]

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    assert {diagnostic.source_path for diagnostic in captured_error.value.report.diagnostics} == {
        "$.document.name",
        "$.input",
    }


def test_import_aggregates_profile_semantic_errors_in_path_order() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    exported_workflow["document"]["name"] = "different-name"
    exported_workflow["document"]["version"] = "9.0.0"
    first_task_name, first_task = next(iter(exported_workflow["do"][0].items()))
    first_task["call"] = "flowspec2.confirm"
    first_task["metadata"]["path_index"] = 4
    assert first_task_name == "path-0-slot"

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    diagnostics = captured_error.value.report.diagnostics
    assert len(diagnostics) == 5
    assert tuple(diagnostic.source_path for diagnostic in diagnostics) == tuple(
        sorted(diagnostic.source_path for diagnostic in diagnostics)
    )


def test_import_rejects_unknown_custom_call() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    first_task = next(iter(exported_workflow["do"][0].values()))
    first_task["call"] = "flowspec2.branch"

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    assert captured_error.value.report.diagnostics[0].code == ("open_workflow.invalid_profile")


def test_import_rejects_reordered_path_index_metadata() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    second_task = next(iter(exported_workflow["do"][1].values()))
    second_task["metadata"]["path_index"] = 0

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    assert captured_error.value.report.diagnostics[0].code == ("open_workflow.invalid_profile")


def test_import_rejects_call_that_disagrees_with_preserved_step() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    first_task = next(iter(exported_workflow["do"][0].values()))
    first_task["call"] = "flowspec2.confirm"

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    assert captured_error.value.report.diagnostics[0].code == ("open_workflow.invalid_profile")


def test_import_rejects_input_schema_that_disagrees_with_metadata() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    exported_workflow["input"]["schema"]["document"] = {"type": "string"}

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    assert captured_error.value.report.diagnostics[0].code == ("open_workflow.invalid_profile")


def test_import_requires_canonical_input_projection_for_valid_entry_schema() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    del exported_workflow["input"]

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    diagnostic = captured_error.value.report.diagnostics[0]
    assert diagnostic.code == "open_workflow.invalid_profile"
    assert diagnostic.source_path == "$.input"


def test_import_reports_invalid_reconstructed_flowspec() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    exported_workflow["document"]["metadata"]["flowspec2"]["sections"]["domains"] = {}

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    assert captured_error.value.report.diagnostics[0].code == (
        "open_workflow.invalid_reconstructed_flowspec"
    )


def test_import_aggregates_reconstructed_flowspec_errors() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    preserved_sections = exported_workflow["document"]["metadata"]["flowspec2"]["sections"]
    preserved_sections["domains"] = {}
    preserved_sections["route"]["description"] = ""

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    diagnostics = captured_error.value.report.diagnostics
    assert len(diagnostics) == 2
    assert all(
        diagnostic.code == "open_workflow.invalid_reconstructed_flowspec"
        for diagnostic in diagnostics
    )
    assert tuple(diagnostic.source_path for diagnostic in diagnostics) == (
        "$.domains",
        "$.route.description",
    )


def test_profile_validator_rejects_fields_outside_the_exact_subset() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    exported_workflow["document"]["title"] = "Not part of this profile"

    validate_official_open_workflow(exported_workflow)
    with pytest.raises(jsonschema.ValidationError):
        validate_open_workflow_profile(exported_workflow)


def test_export_reports_invalid_ows_name_without_truncating_identity() -> None:
    source_flow = _portable_flow()
    source_flow["flow"] = "a" * 64

    with pytest.raises(CompatibilityError) as captured_error:
        export_open_workflow(source_flow)

    diagnostic = captured_error.value.report.diagnostics[0]
    assert diagnostic.code == "open_workflow.invalid_generated_profile"
    assert diagnostic.source_path == "$.document.name"


def test_export_aggregates_invalid_flowspec_errors() -> None:
    source_flow = _portable_flow()
    source_flow["domains"] = {}
    source_flow["route"]["description"] = ""
    source_flow["path"] = []

    with pytest.raises(CompatibilityError) as captured_error:
        export_open_workflow(source_flow)

    diagnostics = captured_error.value.report.diagnostics
    assert len(diagnostics) == 3
    assert all(diagnostic.code == "open_workflow.invalid_flowspec" for diagnostic in diagnostics)
    assert tuple(diagnostic.source_path for diagnostic in diagnostics) == (
        "$.domains",
        "$.path",
        "$.route.description",
    )


def test_export_rejects_schema_valid_flow_with_duplicate_compiled_node_ids() -> None:
    source_flow = _portable_flow()
    source_flow["path"][1]["step"] = source_flow["path"][0]["step"]

    with pytest.raises(CompatibilityError) as captured_error:
        export_open_workflow(source_flow)

    diagnostic = captured_error.value.report.diagnostics[0]
    assert diagnostic.code == "open_workflow.non_executable_flowspec"
    assert "duplicate node ids" in diagnostic.message


def test_import_rejects_profile_with_duplicate_compiled_node_ids() -> None:
    exported_workflow = export_open_workflow(_portable_flow()).artifact
    first_step = next(iter(exported_workflow["do"][0].values()))["with"]["step"]
    second_step = next(iter(exported_workflow["do"][1].values()))["with"]["step"]
    second_step["step"] = first_step["step"]

    with pytest.raises(CompatibilityError) as captured_error:
        import_open_workflow(exported_workflow)

    diagnostic = captured_error.value.report.diagnostics[0]
    assert diagnostic.code == ("open_workflow.non_executable_reconstructed_flowspec")
    assert "duplicate node ids" in diagnostic.message
