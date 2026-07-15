"""Command-line validation, graph inspection, and compatibility conversion."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Mapping, cast

import jsonschema

from .authoring import (
    DEFAULT_GEMINI_AUTHOR_MODEL,
    AuthoringBenchmarkEvidence,
    AuthoringBenchmarkLimits,
    AuthoringEvidenceVerification,
    GeminiAuthor,
    GeminiAuthorError,
    GeminiOperationalExecutor,
    OperationalProviderError,
    RecordingAuthor,
    finalize_presentation_review_draft,
    load_reference_authoring_corpus,
    presentation_review_draft,
    run_authoring_benchmark,
    run_authoring_operational_evidence,
    sign_authoring_evidence,
    sign_presentation_review,
    verify_authoring_evidence,
    verify_authoring_evidence_signature,
    verify_authoring_operational_evidence,
    verify_authoring_promotion,
    verify_presentation_review,
    verify_presentation_review_signature,
)
from .checker import check_flow, check_json
from .cli_parser import CliHandlers, CommandHandler, build_parser
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

logger = logging.getLogger(__name__)


def _load_json_mapping(document_path: str | Path) -> dict[str, Any]:
    path = Path(document_path)
    document = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not document:
        raise ValueError(f"JSON document {path} must be a non-empty object")
    return document


def _read_key_material(key_path: str | Path) -> bytes:
    path = Path(key_path)
    resolved_path = path.resolve(strict=False)
    if any(
        candidate_path.name == ".env" or candidate_path.name.startswith(".env.")
        for candidate_path in (path, resolved_path)
    ):
        raise ValueError("key material must not be loaded from an environment file")
    return path.read_bytes()


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


def _create_gemini_operational_executor(model: str) -> GeminiOperationalExecutor:
    return GeminiOperationalExecutor(model=model)


def _write_authoring_benchmark_evidence(
    arguments: argparse.Namespace,
    model_author: GeminiAuthor,
) -> int:
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
    with model_author:
        recording_author = RecordingAuthor(model_author)
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
            provider=model_author.provenance(),
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


def _authoring_benchmark_gemini(arguments: argparse.Namespace) -> int:
    output_path = Path(arguments.output)
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    return _write_authoring_benchmark_evidence(
        arguments,
        _create_gemini_author(arguments.model),
    )


def _write_operational_evidence(
    arguments: argparse.Namespace,
    operational_executor_factory: Callable[[], GeminiOperationalExecutor],
) -> int:
    output_path = Path(arguments.output)
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    serialized_authoring_evidence, verified_authoring_evidence = _verified_authoring_evidence(
        arguments.path, arguments.repository_revision
    )
    operational_executor = operational_executor_factory()
    with operational_executor:
        operational_evidence = run_authoring_operational_evidence(
            serialized_authoring_evidence,
            verified_authoring_evidence,
            provider=operational_executor.provenance(),
            executor=operational_executor,
        )
    _write_text_exclusive(output_path, f"{operational_evidence.to_json()}\n")
    print(
        f"Wrote report-only operational evidence to {output_path} "
        f"[digest={operational_evidence.digest}, "
        f"matched_probes={operational_evidence.matched_probes}, "
        f"total_probes={operational_evidence.total_probes}]"
    )
    return 0


def _operational_benchmark_gemini(arguments: argparse.Namespace) -> int:
    if Path(arguments.output).exists():
        raise FileExistsError(f"output already exists: {arguments.output}")
    return _write_operational_evidence(
        arguments,
        lambda: _create_gemini_operational_executor(arguments.model),
    )


def _operational_evidence_verify(arguments: argparse.Namespace) -> int:
    serialized_authoring_evidence, verified_authoring_evidence = _verified_authoring_evidence(
        arguments.authoring_evidence,
        arguments.repository_revision,
    )
    verification = verify_authoring_operational_evidence(
        Path(arguments.path).read_text(encoding="utf-8"),
        serialized_authoring_evidence,
        verified_authoring_evidence,
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
            f"OK: {arguments.path} is valid report-only operational evidence "
            f"[digest={verification.digest}, "
            f"matched_probes={verification.matched_probes}, "
            f"total_probes={verification.total_probes}]"
        )
    return 0


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


def _authoring_evidence_sign(arguments: argparse.Namespace) -> int:
    output_path = Path(arguments.output)
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    evidence_signature = sign_authoring_evidence(
        Path(arguments.path).read_text(encoding="utf-8"),
        _read_key_material(arguments.private_key),
        expected_repository_revision=arguments.repository_revision,
    )
    _write_text_exclusive(output_path, f"{evidence_signature.to_json()}\n")
    print(
        f"Wrote authoring evidence signature to {output_path} "
        f"[evidence_digest={evidence_signature.evidence_digest}, "
        f"key_id={evidence_signature.key_id}]"
    )
    return 0


def _authoring_evidence_signature_verify(arguments: argparse.Namespace) -> int:
    authentication = verify_authoring_evidence_signature(
        Path(arguments.path).read_text(encoding="utf-8"),
        Path(arguments.signature).read_text(encoding="utf-8"),
        _read_key_material(arguments.public_key),
        expected_repository_revision=arguments.repository_revision,
    )
    if arguments.json_output:
        print(
            json.dumps(
                authentication.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        print(
            f"OK: {arguments.path} is authenticated authoring evidence "
            f"[digest={authentication.evidence.digest}, key_id={authentication.key_id}]"
        )
    return 0


def _verified_authoring_evidence(
    evidence_path: str | Path,
    expected_repository_revision: str | None,
) -> tuple[str, AuthoringEvidenceVerification]:
    serialized_evidence = Path(evidence_path).read_text(encoding="utf-8")
    evidence_verification = verify_authoring_evidence(
        serialized_evidence,
        expected_repository_revision=expected_repository_revision,
    )
    return serialized_evidence, evidence_verification


def _authoring_presentation_review_init(arguments: argparse.Namespace) -> int:
    output_path = Path(arguments.output)
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    serialized_evidence, evidence_verification = _verified_authoring_evidence(
        arguments.path,
        arguments.repository_revision,
    )
    review_draft = presentation_review_draft(
        serialized_evidence,
        evidence_verification,
    )
    _write_text_exclusive(output_path, _dump_json_mapping(review_draft.to_dict()))
    print(
        f"Wrote authoring presentation review draft to {output_path} "
        f"[evidence_digest={review_draft.benchmark_evidence_digest}, "
        f"subjects={len(review_draft.subjects)}]"
    )
    return 0


def _authoring_presentation_review_finalize(arguments: argparse.Namespace) -> int:
    output_path = Path(arguments.output)
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    serialized_evidence, evidence_verification = _verified_authoring_evidence(
        arguments.path,
        arguments.repository_revision,
    )
    presentation_review = finalize_presentation_review_draft(
        Path(arguments.draft).read_text(encoding="utf-8"),
        serialized_evidence,
        evidence_verification,
    )
    _write_text_exclusive(output_path, f"{presentation_review.to_json()}\n")
    print(
        f"Wrote authoring presentation review to {output_path} "
        f"[digest={presentation_review.digest}, passed={str(presentation_review.passed).lower()}]"
    )
    return 0


def _authoring_presentation_review_verify(arguments: argparse.Namespace) -> int:
    serialized_evidence, evidence_verification = _verified_authoring_evidence(
        arguments.path,
        arguments.repository_revision,
    )
    review_verification = verify_presentation_review(
        Path(arguments.review).read_text(encoding="utf-8"),
        serialized_evidence,
        evidence_verification,
    )
    if arguments.json_output:
        print(
            json.dumps(
                review_verification.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        print(
            f"OK: {arguments.review} is a valid authoring presentation review "
            f"[digest={review_verification.digest}, "
            f"passed={str(review_verification.passed).lower()}]"
        )
    return 0


def _authoring_presentation_review_sign(arguments: argparse.Namespace) -> int:
    output_path = Path(arguments.output)
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")
    review_signature = sign_presentation_review(
        Path(arguments.review).read_text(encoding="utf-8"),
        Path(arguments.path).read_text(encoding="utf-8"),
        _read_key_material(arguments.private_key),
        expected_repository_revision=arguments.repository_revision,
    )
    _write_text_exclusive(output_path, f"{review_signature.to_json()}\n")
    print(
        f"Wrote authoring presentation review signature to {output_path} "
        f"[review_digest={review_signature.presentation_review_digest}, "
        f"key_id={review_signature.key_id}]"
    )
    return 0


def _authoring_presentation_review_signature_verify(arguments: argparse.Namespace) -> int:
    authentication = verify_presentation_review_signature(
        Path(arguments.review).read_text(encoding="utf-8"),
        Path(arguments.path).read_text(encoding="utf-8"),
        Path(arguments.signature).read_text(encoding="utf-8"),
        _read_key_material(arguments.public_key),
        expected_repository_revision=arguments.repository_revision,
    )
    if arguments.json_output:
        print(
            json.dumps(
                authentication.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        print(
            f"OK: {arguments.review} is an authenticated authoring presentation review "
            f"[digest={authentication.presentation_review.digest}, "
            f"key_id={authentication.key_id}]"
        )
    return 0


def _authoring_promotion_verify(arguments: argparse.Namespace) -> int:
    promotion_verification = verify_authoring_promotion(
        Path(arguments.path).read_text(encoding="utf-8"),
        Path(arguments.review).read_text(encoding="utf-8"),
        Path(arguments.signature).read_text(encoding="utf-8"),
        _read_key_material(arguments.public_key),
        expected_repository_revision=arguments.repository_revision,
    )
    if arguments.json_output:
        print(
            json.dumps(
                promotion_verification.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    else:
        print(
            "Authoring promotion eligibility: "
            f"{str(promotion_verification.eligible).lower()} "
            f"[evidence_digest={promotion_verification.authentication.evidence.digest}, "
            "presentation_review_digest="
            f"{promotion_verification.authentication.presentation_review.digest}]"
        )
    return 0 if promotion_verification.eligible else 1


def _parser() -> argparse.ArgumentParser:
    return build_parser(
        CliHandlers(
            validate=_validate,
            check=_check,
            normalize=_normalize,
            intermediate_representation=_ir,
            graph=_graph,
            mermaid=_mermaid,
            rasa_export=_rasa_export,
            rasa_import=_rasa_import,
            open_workflow_export=_open_workflow_export,
            open_workflow_import=_open_workflow_import,
            authoring_benchmark_gemini=_authoring_benchmark_gemini,
            operational_benchmark_gemini=_operational_benchmark_gemini,
            operational_evidence_verify=_operational_evidence_verify,
            authoring_evidence_verify=_authoring_evidence_verify,
            authoring_evidence_sign=_authoring_evidence_sign,
            authoring_evidence_signature_verify=_authoring_evidence_signature_verify,
            authoring_presentation_review_init=_authoring_presentation_review_init,
            authoring_presentation_review_finalize=_authoring_presentation_review_finalize,
            authoring_presentation_review_verify=_authoring_presentation_review_verify,
            authoring_presentation_review_sign=_authoring_presentation_review_sign,
            authoring_presentation_review_signature_verify=(
                _authoring_presentation_review_signature_verify
            ),
            authoring_promotion_verify=_authoring_promotion_verify,
            default_gemini_model=DEFAULT_GEMINI_AUTHOR_MODEL,
        )
    )


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
    except (GeminiAuthorError, OperationalProviderError) as provider_error:
        log_id = log_event(
            logger,
            logging.ERROR,
            "Model provider failed",
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
