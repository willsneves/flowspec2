"""Lossless flowspec2 profile for Open Workflow Specification 1.0.3."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Never

import jsonschema

from flowspec2.compat.models import (
    CompatibilityDiagnostic,
    CompatibilityError,
    CompatibilityReport,
    ConversionOutcome,
)
from flowspec2.compat.tool_profiles import (
    compatibility_tool_names,
    synthetic_compatibility_tool_definition,
)
from flowspec2.compat.yaml import load_yaml_mapping
from flowspec2.compiler import compile_flow
from flowspec2.json_codec import strict_json_loads
from flowspec2.schema import schema as flowspec_schema
from flowspec2.tools import ToolRegistry, default_tool_registry

OPEN_WORKFLOW_PROFILE_ID = "https://wllsena.github.io/flowspec2/profiles/open-workflow-conversation-1"
OPEN_WORKFLOW_SCHEMA_VERSION = "1.0.3"

_OPEN_WORKFLOW_DSL_VERSION = OPEN_WORKFLOW_SCHEMA_VERSION
_OPEN_WORKFLOW_FORMAT = "open-workflow/1.0.3"
_FLOWSPEC_FORMAT = "flowspec/2"
_DEFAULT_NAMESPACE = "flowspec2"
_OFFICIAL_SCHEMA_COMMIT = "9b5b1da29e9d4fff2358580241e11aab22704a16"
_PROFILE_SCHEMA_PATH = (
    Path(__file__).with_name("schemas") / "open-workflow-conversation-1.schema.json"
)
_VENDOR_SCHEMA_DIRECTORY = Path(__file__).with_name("schemas") / "vendor"
_OFFICIAL_SCHEMA_PATH = _VENDOR_SCHEMA_DIRECTORY / "open-workflow-1.0.3.workflow.yaml"
_OFFICIAL_SCHEMA_LICENSE_PATH = _VENDOR_SCHEMA_DIRECTORY / "open-workflow-1.0.3.LICENSE"
_OFFICIAL_SCHEMA_PROVENANCE_PATH = _VENDOR_SCHEMA_DIRECTORY / "open-workflow-1.0.3.provenance.json"
_STEP_KINDS = (
    "slot",
    "confirm",
    "derive",
    "terminal",
    "use",
    "await_external",
)

_profile_schema_cache: dict[str, Any] | None = None
_official_schema_cache: dict[str, Any] | None = None


async def _compatibility_profile_tool(**_inputs: Any) -> dict[str, Any]:
    """Stand in for a preserved tool while the adapter checks compilation only."""

    return {}


def _compatibility_tool_registry(flow_document: Mapping[str, Any]) -> ToolRegistry:
    """Register only tools named by the preserved artifact under review."""

    registry = ToolRegistry()
    known_definitions = default_tool_registry().definitions
    for tool_name in compatibility_tool_names(flow_document):
        registry.register(
            tool_name,
            _compatibility_profile_tool,
            definition=known_definitions.get(tool_name)
            or synthetic_compatibility_tool_definition(
                tool_name,
                flow_document,
                version="open-workflow-profile",
                description=(
                    "Synthetic compile-only contract derived from the preserved "
                    "Open Workflow profile."
                ),
            ),
        )
    return registry


def _load_json_mapping(document_path: Path) -> dict[str, Any]:
    loaded_document = strict_json_loads(document_path.read_text(encoding="utf-8"))
    if not isinstance(loaded_document, dict):
        raise RuntimeError(f"expected a JSON object in {document_path}")
    return dict(loaded_document)


def _verify_vendor_artifact(
    artifact_path: Path,
    *,
    expected_sha256: object,
) -> None:
    if not isinstance(expected_sha256, str):
        raise RuntimeError(f"missing SHA-256 provenance for {artifact_path.name}")
    actual_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"vendored Open Workflow artifact failed integrity verification: {artifact_path.name}"
        )


def _official_schema() -> dict[str, Any]:
    global _official_schema_cache
    cached_schema = _official_schema_cache
    if cached_schema is None:
        provenance = _load_json_mapping(_OFFICIAL_SCHEMA_PROVENANCE_PATH)
        if provenance.get("version") != OPEN_WORKFLOW_SCHEMA_VERSION:
            raise RuntimeError("vendored Open Workflow schema version does not match provenance")
        if provenance.get("source_commit") != _OFFICIAL_SCHEMA_COMMIT:
            raise RuntimeError("vendored Open Workflow schema commit does not match provenance")
        _verify_vendor_artifact(
            _OFFICIAL_SCHEMA_PATH,
            expected_sha256=provenance.get("schema_sha256"),
        )
        _verify_vendor_artifact(
            _OFFICIAL_SCHEMA_LICENSE_PATH,
            expected_sha256=provenance.get("license_sha256"),
        )
        cached_schema = load_yaml_mapping(_OFFICIAL_SCHEMA_PATH)
        jsonschema.Draft202012Validator.check_schema(cached_schema)
        _official_schema_cache = cached_schema
    return cached_schema


def official_open_workflow_schema() -> dict[str, Any]:
    """Return a defensive copy of the verified official Open Workflow schema."""

    return deepcopy(_official_schema())


def _profile_schema() -> dict[str, Any]:
    global _profile_schema_cache
    cached_schema = _profile_schema_cache
    if cached_schema is None:
        cached_schema = strict_json_loads(_PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(cached_schema)
        _profile_schema_cache = cached_schema
    return cached_schema


def _json_path(error: jsonschema.ValidationError) -> str:
    source_path = "$"
    for path_component in error.absolute_path:
        if isinstance(path_component, int):
            source_path += f"[{path_component}]"
        else:
            source_path += f".{path_component}"
    return source_path


def _raise_conversion_diagnostics(
    *,
    source_format: str,
    target_format: str,
    diagnostics: tuple[CompatibilityDiagnostic, ...],
) -> Never:
    report = CompatibilityReport(
        source_format=source_format,
        target_format=target_format,
        diagnostics=diagnostics,
    )
    raise CompatibilityError(report, allow_lossy=False)


def _raise_conversion_error(
    *,
    source_format: str,
    target_format: str,
    code: str,
    source_path: str,
    message: str,
) -> Never:
    _raise_conversion_diagnostics(
        source_format=source_format,
        target_format=target_format,
        diagnostics=(
            CompatibilityDiagnostic(
                severity="error",
                code=code,
                source_path=source_path,
                message=message,
            ),
        ),
    )


def _step_kind(step: Mapping[str, Any]) -> str | None:
    declared_kinds = tuple(step_kind for step_kind in _STEP_KINDS if step_kind in step)
    return declared_kinds[0] if len(declared_kinds) == 1 else None


def _task_name(path_index: int, step_kind: str) -> str:
    return f"path-{path_index}-{step_kind}"


def _normalize_flow_name(flow_id: str) -> str:
    return flow_id.replace("_", "-")


def _valid_entry_schema(entry_schema: Mapping[str, Any]) -> bool:
    try:
        jsonschema.Draft202012Validator.check_schema(dict(entry_schema))
    except jsonschema.SchemaError:
        return False
    return True


def _profile_validation_error(
    message: str,
    *path: str | int,
) -> jsonschema.ValidationError:
    return jsonschema.ValidationError(message, path=path)


def _validation_error_sort_key(
    error: jsonschema.ValidationError,
) -> tuple[tuple[tuple[int, int | str], ...], str]:
    path_components: list[tuple[int, int | str]] = []
    for path_component in error.absolute_path:
        if isinstance(path_component, int):
            path_components.append((0, path_component))
        else:
            path_components.append((1, str(path_component)))
    return tuple(path_components), error.message


def _sorted_validation_errors(
    errors: tuple[jsonschema.ValidationError, ...],
) -> tuple[jsonschema.ValidationError, ...]:
    return tuple(sorted(errors, key=_validation_error_sort_key))


def _official_validation_errors(
    workflow_document: Mapping[str, Any],
) -> tuple[jsonschema.ValidationError, ...]:
    errors = tuple(
        jsonschema.Draft202012Validator(_official_schema()).iter_errors(workflow_document)
    )
    return _sorted_validation_errors(errors)


def _merge_official_validation_errors(
    profile_errors: tuple[jsonschema.ValidationError, ...],
    official_errors: tuple[jsonschema.ValidationError, ...],
) -> tuple[jsonschema.ValidationError, ...]:
    profile_error_keys = {(tuple(error.absolute_path), error.message) for error in profile_errors}
    additional_official_errors = tuple(
        error
        for error in official_errors
        if (tuple(error.absolute_path), error.message) not in profile_error_keys
    )
    return _sorted_validation_errors(profile_errors + additional_official_errors)


def _flowspec_validation_errors(
    flow_document: Mapping[str, Any],
) -> tuple[jsonschema.ValidationError, ...]:
    errors = tuple(jsonschema.Draft202012Validator(flowspec_schema()).iter_errors(flow_document))
    return _sorted_validation_errors(errors)


def _compilation_error(flow_document: Mapping[str, Any]) -> str | None:
    try:
        compile_flow(
            deepcopy(dict(flow_document)),
            tools=_compatibility_tool_registry(flow_document),
        )
    except (KeyError, StopIteration, TypeError, ValueError) as error:
        return str(error) or error.__class__.__name__
    return None


def _profile_semantic_errors(
    workflow_document: Mapping[str, Any],
    semantic_errors: list[jsonschema.ValidationError] | None = None,
) -> tuple[jsonschema.ValidationError, ...]:
    semantic_errors = semantic_errors if semantic_errors is not None else []
    document = workflow_document["document"]
    flowspec_metadata = document["metadata"]["flowspec2"]
    original_flow_id = flowspec_metadata["original_flow_id"]
    sections = flowspec_metadata["sections"]

    if document["name"] != _normalize_flow_name(original_flow_id):
        semantic_errors.append(
            _profile_validation_error(
                "document.name must be the underscore-to-hyphen normalization of "
                "document.metadata.flowspec2.original_flow_id",
                "document",
                "name",
            )
        )
    if document["version"] != sections["version"]:
        semantic_errors.append(
            _profile_validation_error(
                "document.version must match the preserved flowspec2 version",
                "document",
                "version",
            )
        )

    for path_index, task_item in enumerate(workflow_document["do"]):
        task_name, task_definition = next(iter(task_item.items()))
        declared_path_index = task_definition["metadata"]["path_index"]
        if declared_path_index != path_index:
            semantic_errors.append(
                _profile_validation_error(
                    "task metadata.path_index must equal its position in the sequential task list",
                    "do",
                    path_index,
                    task_name,
                    "metadata",
                    "path_index",
                )
            )

        call_name = task_definition["call"]
        call_step_kind = call_name.removeprefix("flowspec2.")
        preserved_step = task_definition["with"]["step"]
        preserved_step_kind = _step_kind(preserved_step)
        if preserved_step_kind != call_step_kind:
            semantic_errors.append(
                _profile_validation_error(
                    "custom call must match the single flowspec2 kind in with.step",
                    "do",
                    path_index,
                    task_name,
                    "call",
                )
            )

        expected_task_name = _task_name(path_index, call_step_kind)
        if task_name != expected_task_name:
            semantic_errors.append(
                _profile_validation_error(
                    f"task name must be {expected_task_name!r}",
                    "do",
                    path_index,
                )
            )

    input_definition = workflow_document.get("input")
    preserved_entry_schema = sections["route"].get("entry_args_schema")
    if (
        input_definition is None
        and isinstance(preserved_entry_schema, Mapping)
        and _valid_entry_schema(preserved_entry_schema)
    ):
        semantic_errors.append(
            _profile_validation_error(
                "input is required when the preserved route.entry_args_schema is a valid JSON Schema",
                "input",
            )
        )
    if input_definition is not None:
        input_schema = input_definition["schema"]["document"]
        try:
            jsonschema.Draft202012Validator.check_schema(input_schema)
        except jsonschema.SchemaError as error:
            semantic_errors.append(
                _profile_validation_error(
                    f"input.schema.document is not a valid JSON Schema: {error.message}",
                    "input",
                    "schema",
                    "document",
                )
            )

        if input_schema != preserved_entry_schema:
            semantic_errors.append(
                _profile_validation_error(
                    "input.schema.document must match the preserved route.entry_args_schema",
                    "input",
                    "schema",
                    "document",
                )
            )

    return _sorted_validation_errors(tuple(semantic_errors))


def _profile_validation_errors(
    workflow_document: Mapping[str, Any],
) -> tuple[jsonschema.ValidationError, ...]:
    profile_structural_errors = tuple(
        jsonschema.Draft202012Validator(_profile_schema()).iter_errors(workflow_document)
    )
    official_errors = _official_validation_errors(workflow_document)
    semantic_errors: list[jsonschema.ValidationError] = []
    try:
        _profile_semantic_errors(workflow_document, semantic_errors)
    except (AttributeError, IndexError, KeyError, StopIteration, TypeError):
        # Invalid container shapes can make semantic paths unreadable.
        profile_errors = _sorted_validation_errors(
            profile_structural_errors + tuple(semantic_errors)
        )
        return _merge_official_validation_errors(profile_errors, official_errors)
    profile_errors = _sorted_validation_errors(profile_structural_errors + tuple(semantic_errors))
    return _merge_official_validation_errors(profile_errors, official_errors)


def _validation_diagnostics(
    errors: tuple[jsonschema.ValidationError, ...],
    *,
    code: str,
) -> tuple[CompatibilityDiagnostic, ...]:
    return tuple(
        CompatibilityDiagnostic(
            severity="error",
            code=code,
            source_path=_json_path(error),
            message=error.message,
        )
        for error in errors
    )


def _raise_validation_errors(
    errors: tuple[jsonschema.ValidationError, ...],
    *,
    source_format: str,
    target_format: str,
    code: str,
) -> Never:
    _raise_conversion_diagnostics(
        source_format=source_format,
        target_format=target_format,
        diagnostics=_validation_diagnostics(errors, code=code),
    )


def validate_open_workflow_profile(
    workflow_document: Mapping[str, Any],
) -> None:
    """Validate the official schema and the exact flowspec2 conversational profile."""

    validation_errors = _profile_validation_errors(workflow_document)
    if validation_errors:
        raise validation_errors[0]


def validate_official_open_workflow(
    workflow_document: Mapping[str, Any],
) -> None:
    """Validate a document only against the verified official Open Workflow schema."""

    validation_errors = _official_validation_errors(workflow_document)
    if validation_errors:
        raise validation_errors[0]


def export_open_workflow(
    flow_document: Mapping[str, Any],
    *,
    namespace: str = _DEFAULT_NAMESPACE,
) -> ConversionOutcome[dict[str, Any]]:
    """Export a valid flowspec2 document as the lossless Open Workflow profile."""

    source_flow = deepcopy(dict(flow_document))
    source_errors = _flowspec_validation_errors(source_flow)
    if source_errors:
        _raise_validation_errors(
            source_errors,
            source_format=_FLOWSPEC_FORMAT,
            target_format=_OPEN_WORKFLOW_FORMAT,
            code="open_workflow.invalid_flowspec",
        )
    if compilation_error := _compilation_error(source_flow):
        _raise_conversion_error(
            source_format=_FLOWSPEC_FORMAT,
            target_format=_OPEN_WORKFLOW_FORMAT,
            code="open_workflow.non_executable_flowspec",
            source_path="$",
            message=f"flowspec2 compiler rejected the source document: {compilation_error}",
        )

    diagnostics: list[CompatibilityDiagnostic] = []
    path_tasks: list[dict[str, Any]] = []
    for path_index, path_step in enumerate(source_flow["path"]):
        step_kind = _step_kind(path_step)
        if step_kind is None:
            _raise_conversion_error(
                source_format=_FLOWSPEC_FORMAT,
                target_format=_OPEN_WORKFLOW_FORMAT,
                code="open_workflow.invalid_path_step",
                source_path=f"$.path[{path_index}]",
                message="path step must declare exactly one supported flowspec2 step kind",
            )
        task_name = _task_name(path_index, step_kind)
        path_tasks.append(
            {
                task_name: {
                    "call": f"flowspec2.{step_kind}",
                    "with": {"step": deepcopy(path_step)},
                    "metadata": {"path_index": path_index},
                }
            }
        )

    preserved_sections = {
        section_name: deepcopy(section_value)
        for section_name, section_value in source_flow.items()
        if section_name not in {"flow", "path"}
    }
    workflow_document: dict[str, Any] = {
        "document": {
            "dsl": _OPEN_WORKFLOW_DSL_VERSION,
            "namespace": namespace,
            "name": _normalize_flow_name(source_flow["flow"]),
            "version": source_flow["version"],
            "metadata": {
                "flowspec2": {
                    "profile": OPEN_WORKFLOW_PROFILE_ID,
                    "original_flow_id": source_flow["flow"],
                    "sections": preserved_sections,
                }
            },
        },
        "do": path_tasks,
    }

    entry_schema = source_flow["route"].get("entry_args_schema")
    if isinstance(entry_schema, Mapping):
        if _valid_entry_schema(entry_schema):
            workflow_document["input"] = {
                "schema": {
                    "format": "json",
                    "document": deepcopy(dict(entry_schema)),
                }
            }
        else:
            diagnostics.append(
                CompatibilityDiagnostic(
                    severity="warning",
                    code="open_workflow.entry_schema_omitted",
                    source_path="$.route.entry_args_schema",
                    message=(
                        "entry_args_schema is preserved in profile metadata but omitted "
                        "from input because it is not a valid JSON Schema"
                    ),
                )
            )

    generated_profile_errors = _profile_validation_errors(workflow_document)
    if generated_profile_errors:
        _raise_validation_errors(
            generated_profile_errors,
            source_format=_FLOWSPEC_FORMAT,
            target_format=_OPEN_WORKFLOW_FORMAT,
            code="open_workflow.invalid_generated_profile",
        )

    return ConversionOutcome(
        artifact=workflow_document,
        report=CompatibilityReport(
            source_format=_FLOWSPEC_FORMAT,
            target_format=_OPEN_WORKFLOW_FORMAT,
            diagnostics=tuple(diagnostics),
        ),
    )


def import_open_workflow(
    workflow_document: Mapping[str, Any],
) -> ConversionOutcome[dict[str, Any]]:
    """Import only an Open Workflow document declaring the exact profile."""

    source_workflow = deepcopy(dict(workflow_document))
    source_profile_errors = _profile_validation_errors(source_workflow)
    if source_profile_errors:
        _raise_validation_errors(
            source_profile_errors,
            source_format=_OPEN_WORKFLOW_FORMAT,
            target_format=_FLOWSPEC_FORMAT,
            code="open_workflow.invalid_profile",
        )

    flowspec_metadata = source_workflow["document"]["metadata"]["flowspec2"]
    imported_flow = deepcopy(flowspec_metadata["sections"])
    imported_flow["flow"] = flowspec_metadata["original_flow_id"]
    imported_flow["path"] = [
        deepcopy(next(iter(task_item.values()))["with"]["step"])
        for task_item in source_workflow["do"]
    ]

    reconstructed_flow_errors = _flowspec_validation_errors(imported_flow)
    if reconstructed_flow_errors:
        _raise_validation_errors(
            reconstructed_flow_errors,
            source_format=_OPEN_WORKFLOW_FORMAT,
            target_format=_FLOWSPEC_FORMAT,
            code="open_workflow.invalid_reconstructed_flowspec",
        )
    if compilation_error := _compilation_error(imported_flow):
        _raise_conversion_error(
            source_format=_OPEN_WORKFLOW_FORMAT,
            target_format=_FLOWSPEC_FORMAT,
            code="open_workflow.non_executable_reconstructed_flowspec",
            source_path="$.document.metadata.flowspec2",
            message=(
                f"flowspec2 compiler rejected the reconstructed document: {compilation_error}"
            ),
        )

    return ConversionOutcome(
        artifact=imported_flow,
        report=CompatibilityReport(
            source_format=_OPEN_WORKFLOW_FORMAT,
            target_format=_FLOWSPEC_FORMAT,
        ),
    )
