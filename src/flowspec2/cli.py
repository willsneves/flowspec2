"""Command-line validation, graph inspection, and compatibility conversion."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Callable, Mapping, Never, cast

import jsonschema

from .authoring import (
    DEFAULT_GEMINI_AUTHOR_MODEL,
    AuthoringBenchmarkEvidence,
    AuthoringBenchmarkLimits,
    GeminiAuthor,
    GeminiAuthorError,
    RecordingAuthor,
    load_reference_authoring_corpus,
    run_authoring_benchmark,
    verify_authoring_evidence,
)
from .checker import check_flow, check_json
from .compat.models import CompatibilityError, CompatibilityReport
from .compat.open_workflow import export_open_workflow, import_open_workflow
from .compat.rasa import RasaBundle, export_rasa, import_rasa
from .compat.yaml import (
    dumps_yaml_mapping,
    load_yaml_mapping,
)
from .compiler import compile_flow
from .diagnostics import FlowCheckReport
from .ir import build_flow_ir, normalize_flow
from .json_codec import strict_json_loads
from .observability import log_event
from .profiles import reference_profile
from .schema import load_flow

CommandHandler = Callable[[argparse.Namespace], int]
logger = logging.getLogger(__name__)


class _CorrelatedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        log_id = log_event(
            logger,
            logging.ERROR,
            "CLI argument parsing failed",
            operation="parse_arguments",
            context={"parser": self.prog},
        )
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error [log_id={log_id}]: {message}\n")


def _load_json_mapping(document_path: str | Path) -> dict[str, Any]:
    path = Path(document_path)
    document = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not document:
        raise ValueError(f"JSON document {path} must be a non-empty object")
    return document


def _load_structured_mapping(document_path: str | Path) -> dict[str, Any]:
    path = Path(document_path)
    if path.suffix.lower() == ".json":
        return _load_json_mapping(path)
    if path.suffix.lower() in {".yaml", ".yml"}:
        return load_yaml_mapping(path)
    raise ValueError(f"unsupported document extension for {path}; use .json, .yaml, or .yml")


def _dump_json_mapping(document: Mapping[str, Any]) -> str:
    return f"{json.dumps(dict(document), ensure_ascii=False, allow_nan=False, indent=2)}\n"


def _dump_structured_mapping(
    document: Mapping[str, Any],
    output_path: str | Path,
) -> str:
    path = Path(output_path)
    if path.suffix.lower() == ".json":
        return _dump_json_mapping(document)
    if path.suffix.lower() in {".yaml", ".yml"}:
        return dumps_yaml_mapping(document)
    raise ValueError(f"unsupported output extension for {path}; use .json, .yaml, or .yml")


def _dump_flow_document(
    document: Mapping[str, Any],
    output_path: str | Path,
) -> str:
    path = Path(output_path)
    if path.suffix.lower() != ".json":
        raise ValueError(f"flowspec2 output must use the .json extension: {path}")
    return _dump_json_mapping(document)


def _write_text_exclusive(output_path: str | Path, content: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as output_file:
            output_file.write(content)
        try:
            os.link(temporary_path, path)
        except FileExistsError as error:
            raise FileExistsError(f"output already exists: {path}") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_rasa_bundle(output_directory: str | Path, bundle: RasaBundle) -> None:
    directory = Path(output_directory)
    if directory.exists():
        raise FileExistsError(f"Rasa output directory already exists: {directory}")
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary_directory = Path(
        tempfile.mkdtemp(
            dir=directory.parent,
            prefix=f".{directory.name}.",
        )
    )
    try:
        (temporary_directory / "flows.yml").write_text(
            dumps_yaml_mapping(bundle.flows),
            encoding="utf-8",
        )
        (temporary_directory / "domain.yml").write_text(
            dumps_yaml_mapping(bundle.domain),
            encoding="utf-8",
        )
        temporary_directory.rename(directory)
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise


def _print_report(report: CompatibilityReport) -> None:
    for diagnostic in report.diagnostics:
        log_id = log_event(
            logger,
            logging.ERROR if diagnostic.severity == "error" else logging.WARNING,
            "Compatibility diagnostic",
            operation="compatibility_conversion",
            context={
                "diagnostic_code": diagnostic.code,
                "source_format": report.source_format,
                "source_path": diagnostic.source_path,
                "target_format": report.target_format,
            },
        )
        print(
            f"{diagnostic.severity.upper()} {diagnostic.code} [log_id={log_id}] "
            f"{diagnostic.source_path}: {diagnostic.message}",
            file=sys.stderr,
        )


def _print_flow_diagnostics(flow_report: FlowCheckReport, *, operation: str) -> None:
    for diagnostic in flow_report.diagnostics:
        log_id = log_event(
            logger,
            logging.ERROR if diagnostic.severity == "error" else logging.WARNING,
            "Flow diagnostic",
            operation=operation,
            context={
                "diagnostic_code": diagnostic.code,
                "source_path": diagnostic.path,
            },
        )
        source_path = diagnostic.path or "<root>"
        suggested_fix = (
            f" Suggested fix: {diagnostic.suggested_fix}"
            if diagnostic.suggested_fix is not None
            else ""
        )
        print(
            f"{diagnostic.severity.upper()} {diagnostic.code} [log_id={log_id}] "
            f"{source_path}: {diagnostic.message}{suggested_fix}",
            file=sys.stderr,
        )


def _check(arguments: argparse.Namespace) -> int:
    serialized_flow = Path(arguments.path).read_text(encoding="utf-8")
    flow_report = check_json(serialized_flow)
    if arguments.json_output:
        print(
            json.dumps(
                flow_report.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        _print_flow_diagnostics(flow_report, operation="check")
        if flow_report.is_valid:
            print(f"OK: {arguments.path} is valid flowspec/2 and compiles")
    return 0 if flow_report.is_valid else 1


def _validate(arguments: argparse.Namespace) -> int:
    serialized_flow = Path(arguments.path).read_text(encoding="utf-8")
    flow_report = check_json(serialized_flow, compile_document=False)
    _print_flow_diagnostics(flow_report, operation="validate")
    if flow_report.has_errors:
        return 1
    print(f"OK: {arguments.path} is valid flowspec/2")
    return 0


def _normalize(arguments: argparse.Namespace) -> int:
    flow_document = _load_json_mapping(arguments.path)
    flow_report = check_flow(flow_document, compile_document=False)
    if flow_report.has_errors:
        _print_flow_diagnostics(flow_report, operation="normalize")
        return 1
    print(
        json.dumps(
            normalize_flow(flow_document),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _ir(arguments: argparse.Namespace) -> int:
    flow_document = _load_json_mapping(arguments.path)
    flow_report = check_flow(flow_document, compile_document=False)
    if flow_report.has_errors:
        _print_flow_diagnostics(flow_report, operation="ir")
        return 1
    print(
        json.dumps(
            build_flow_ir(flow_document).to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _graph(arguments: argparse.Namespace) -> int:
    flow_document = load_flow(arguments.path)
    compiled_flow = compile_flow(flow_document)
    graph_view = compiled_flow.graph.get_graph()
    print(
        f"flow: {flow_document['flow']} v{flow_document['version']}  "
        f"(entry: {compiled_flow.entry_node_id})"
    )
    print("nodes:")
    for graph_node in graph_view.nodes:
        print(f"  - {graph_node}")
    return 0


def _mermaid(arguments: argparse.Namespace) -> int:
    flow_document = load_flow(arguments.path)
    compiled_flow = compile_flow(flow_document)
    print(compiled_flow.graph.get_graph().draw_mermaid())
    return 0


def _rasa_export(arguments: argparse.Namespace) -> int:
    flow_document = _load_json_mapping(arguments.path)
    conversion = export_rasa(
        flow_document,
        allow_lossy=arguments.allow_lossy,
    )
    _print_report(conversion.report)
    _write_rasa_bundle(arguments.output_dir, conversion.artifact)
    print(f"Wrote Rasa bundle to {arguments.output_dir}")
    return 0


def _rasa_import(arguments: argparse.Namespace) -> int:
    flows_document = load_yaml_mapping(arguments.path)
    domain_document = load_yaml_mapping(arguments.domain)
    conversion = import_rasa(
        flows_document,
        domain_document,
        flow_id=arguments.flow,
        flow_version=arguments.version,
        allow_lossy=arguments.allow_lossy,
    )
    _print_report(conversion.report)
    _write_text_exclusive(
        arguments.output,
        _dump_flow_document(conversion.artifact, arguments.output),
    )
    print(f"Wrote flowspec2 document to {arguments.output}")
    return 0


def _open_workflow_export(arguments: argparse.Namespace) -> int:
    flow_document = _load_json_mapping(arguments.path)
    conversion = export_open_workflow(
        flow_document,
        namespace=arguments.namespace,
    )
    _print_report(conversion.report)
    serialized_workflow = _dump_structured_mapping(
        conversion.artifact,
        arguments.output,
    )
    _write_text_exclusive(arguments.output, serialized_workflow)
    print(f"Wrote Open Workflow profile to {arguments.output}")
    return 0


def _open_workflow_import(arguments: argparse.Namespace) -> int:
    workflow_document = _load_structured_mapping(arguments.path)
    conversion = import_open_workflow(workflow_document)
    _print_report(conversion.report)
    _write_text_exclusive(
        arguments.output,
        _dump_flow_document(conversion.artifact, arguments.output),
    )
    print(f"Wrote flowspec2 document to {arguments.output}")
    return 0


def _create_gemini_author(model: str) -> GeminiAuthor:
    return GeminiAuthor(model=model)


def _authoring_benchmark_gemini(arguments: argparse.Namespace) -> int:
    output_path = Path(arguments.output)
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    if not arguments.repository_revision.strip():
        raise ValueError("repository revision must be non-empty")
    benchmark_limits = AuthoringBenchmarkLimits(
        max_correction_rounds=arguments.max_correction_rounds,
    )
    corpus = load_reference_authoring_corpus()
    flow_profile = reference_profile()
    with _create_gemini_author(arguments.model) as gemini_author:
        recording_author = RecordingAuthor(gemini_author)
        benchmark_report = run_authoring_benchmark(
            arguments.benchmark_identifier,
            corpus.cases,
            recording_author,
            limits=benchmark_limits,
            profile=flow_profile,
        )
        evidence = AuthoringBenchmarkEvidence(
            package_version=package_version("flowspec2"),
            repository_revision=arguments.repository_revision,
            benchmark_limits=benchmark_limits,
            corpus=corpus,
            profile_identifier=flow_profile.identifier,
            profile_digest=flow_profile.digest,
            provider=gemini_author.provenance(),
            report=benchmark_report,
            captures=recording_author.captures,
        )
    _write_text_exclusive(arguments.output, f"{evidence.to_json()}\n")
    print(
        f"Wrote authoring evidence to {arguments.output} "
        f"[digest={evidence.digest}, "
        f"successful_cases={benchmark_report.successful_cases}, "
        f"total_cases={benchmark_report.total_cases}]"
    )
    return 0 if benchmark_report.successful_cases == benchmark_report.total_cases else 1


def _authoring_evidence_verify(arguments: argparse.Namespace) -> int:
    verification = verify_authoring_evidence(
        Path(arguments.path).read_text(encoding="utf-8"),
        expected_repository_revision=arguments.repository_revision,
    )
    if arguments.json_output:
        print(
            json.dumps(
                verification.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        print(
            f"OK: {arguments.path} is valid authoring evidence "
            f"[digest={verification.digest}, "
            f"successful_cases={verification.successful_cases}, "
            f"total_cases={verification.total_cases}]"
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = _CorrelatedArgumentParser(
        prog="flowspec2",
        description="Validate, inspect, and convert flowspec2 documents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate a flowspec2 document",
    )
    validate_parser.add_argument("path")
    validate_parser.set_defaults(handler=_validate)

    check_parser = subparsers.add_parser(
        "check",
        help="validate and compile-check a flowspec2 document",
    )
    check_parser.add_argument("path")
    check_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a deterministic machine-readable report",
    )
    check_parser.set_defaults(handler=_check)

    normalize_parser = subparsers.add_parser(
        "normalize",
        help="emit the canonical normalized flowspec2 document",
    )
    normalize_parser.add_argument("path")
    normalize_parser.set_defaults(handler=_normalize)

    ir_parser = subparsers.add_parser(
        "ir",
        help="emit the canonical intermediate representation",
    )
    ir_parser.add_argument("path")
    ir_parser.set_defaults(handler=_ir)

    graph_parser = subparsers.add_parser("graph", help="list compiled graph nodes")
    graph_parser.add_argument("path")
    graph_parser.set_defaults(handler=_graph)

    mermaid_parser = subparsers.add_parser(
        "mermaid",
        help="render the compiled graph as Mermaid",
    )
    mermaid_parser.add_argument("path")
    mermaid_parser.set_defaults(handler=_mermaid)

    rasa_export_parser = subparsers.add_parser(
        "rasa-export",
        help="export the bounded Rasa CALM portable profile",
    )
    rasa_export_parser.add_argument("path")
    rasa_export_parser.add_argument("--output-dir", required=True)
    rasa_export_parser.add_argument("--allow-lossy", action="store_true")
    rasa_export_parser.set_defaults(handler=_rasa_export)

    rasa_import_parser = subparsers.add_parser(
        "rasa-import",
        help="import one flow from the bounded Rasa CALM portable profile",
    )
    rasa_import_parser.add_argument("path", help="flows.yml path")
    rasa_import_parser.add_argument("--domain", required=True, help="domain.yml path")
    rasa_import_parser.add_argument("--flow", help="flow id; required for multi-flow files")
    rasa_import_parser.add_argument("--version", default="1.0.0", help="flowspec2 version")
    rasa_import_parser.add_argument("--output", required=True)
    rasa_import_parser.add_argument("--allow-lossy", action="store_true")
    rasa_import_parser.set_defaults(handler=_rasa_import)

    open_export_parser = subparsers.add_parser(
        "open-workflow-export",
        help="export the lossless Open Workflow conversational profile",
    )
    open_export_parser.add_argument("path")
    open_export_parser.add_argument("--output", required=True)
    open_export_parser.add_argument("--namespace", default="flowspec2")
    open_export_parser.set_defaults(handler=_open_workflow_export)

    open_import_parser = subparsers.add_parser(
        "open-workflow-import",
        help="import the exact Open Workflow conversational profile",
    )
    open_import_parser.add_argument("path")
    open_import_parser.add_argument("--output", required=True)
    open_import_parser.set_defaults(handler=_open_workflow_import)

    authoring_benchmark_parser = subparsers.add_parser(
        "authoring-benchmark-gemini",
        help="run the packaged AI-authoring corpus through Gemini",
    )
    authoring_benchmark_parser.add_argument(
        "--allow-network",
        action="store_true",
        required=True,
        help="explicitly permit Gemini API requests for this invocation",
    )
    authoring_benchmark_parser.add_argument(
        "--benchmark-identifier",
        default="gemini_reference",
    )
    authoring_benchmark_parser.add_argument("--model", default=DEFAULT_GEMINI_AUTHOR_MODEL)
    authoring_benchmark_parser.add_argument("--repository-revision", required=True)
    authoring_benchmark_parser.add_argument("--max-correction-rounds", type=int, default=2)
    authoring_benchmark_parser.add_argument("--output", required=True)
    authoring_benchmark_parser.set_defaults(handler=_authoring_benchmark_gemini)

    evidence_verify_parser = subparsers.add_parser(
        "authoring-evidence-verify",
        help="verify and deterministically replay an authoring evidence artifact",
    )
    evidence_verify_parser.add_argument("path")
    evidence_verify_parser.add_argument(
        "--repository-revision",
        help="require the artifact to name this repository revision",
    )
    evidence_verify_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a deterministic machine-readable verification summary",
    )
    evidence_verify_parser.set_defaults(handler=_authoring_evidence_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    command_arguments = argv if argv is not None else sys.argv[1:]
    try:
        arguments = _parser().parse_args(command_arguments)
    except SystemExit as system_exit:
        if isinstance(system_exit.code, int):
            return system_exit.code
        return 0 if system_exit.code is None else 1

    try:
        handler = cast(CommandHandler, arguments.handler)
        return handler(arguments)
    except CompatibilityError as compatibility_error:
        _print_report(compatibility_error.report)
        return 1
    except GeminiAuthorError as provider_error:
        log_id = log_event(
            logger,
            logging.ERROR,
            "Authoring provider failed",
            operation=str(getattr(arguments, "command", "unknown")),
            context={
                "provider": "google_gemini",
                "model": str(getattr(arguments, "model", "unknown")),
            },
        )
        print(f"ERROR [log_id={log_id}]: {provider_error}", file=sys.stderr)
        return 1
    except (
        json.JSONDecodeError,
        jsonschema.ValidationError,
        OSError,
        RuntimeError,
        ValueError,
    ) as command_error:
        log_id = log_event(
            logger,
            logging.ERROR,
            "CLI command failed",
            operation=str(getattr(arguments, "command", "unknown")),
            context={"error_type": type(command_error).__name__},
            exc_info=True,
        )
        print(f"ERROR [log_id={log_id}]: {command_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
