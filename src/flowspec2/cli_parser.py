"""Argument grammar for the flowspec2 command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Never

from .observability import log_event

CommandHandler = Callable[[argparse.Namespace], int]
logger = logging.getLogger(__name__)


class CorrelatedArgumentParser(argparse.ArgumentParser):
    """Argument parser whose user-facing failures carry a correlation ID."""

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


@dataclass(frozen=True)
class CliHandlers:
    """Handlers and defaults injected into the declarative argument grammar."""

    validate: CommandHandler
    check: CommandHandler
    normalize: CommandHandler
    intermediate_representation: CommandHandler
    graph: CommandHandler
    mermaid: CommandHandler
    rasa_export: CommandHandler
    rasa_import: CommandHandler
    open_workflow_export: CommandHandler
    open_workflow_import: CommandHandler
    authoring_benchmark_gemini: CommandHandler
    operational_benchmark_gemini: CommandHandler
    operational_evidence_verify: CommandHandler
    authoring_evidence_verify: CommandHandler
    authoring_evidence_sign: CommandHandler
    authoring_evidence_signature_verify: CommandHandler
    authoring_presentation_review_init: CommandHandler
    authoring_presentation_review_finalize: CommandHandler
    authoring_presentation_review_verify: CommandHandler
    authoring_presentation_review_sign: CommandHandler
    authoring_presentation_review_signature_verify: CommandHandler
    authoring_promotion_verify: CommandHandler
    default_gemini_model: str


def build_parser(cli_handlers: CliHandlers) -> argparse.ArgumentParser:
    """Build a fresh parser bound to the supplied command handlers."""

    parser = CorrelatedArgumentParser(
        prog="flowspec2",
        description="Validate, inspect, and convert flowspec2 documents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate a flowspec2 document",
    )
    validate_parser.add_argument("path")
    validate_parser.set_defaults(handler=cli_handlers.validate)

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
    check_parser.set_defaults(handler=cli_handlers.check)

    normalize_parser = subparsers.add_parser(
        "normalize",
        help="emit the canonical normalized flowspec2 document",
    )
    normalize_parser.add_argument("path")
    normalize_parser.set_defaults(handler=cli_handlers.normalize)

    intermediate_representation_parser = subparsers.add_parser(
        "ir",
        help="emit canonical contracts with subflow topology abstracted",
    )
    intermediate_representation_parser.add_argument("path")
    intermediate_representation_parser.set_defaults(
        handler=cli_handlers.intermediate_representation
    )

    graph_parser = subparsers.add_parser("graph", help="list compiled graph nodes")
    graph_parser.add_argument("path")
    graph_parser.set_defaults(handler=cli_handlers.graph)

    mermaid_parser = subparsers.add_parser(
        "mermaid",
        help="render the fully expanded compiled graph as Mermaid",
    )
    mermaid_parser.add_argument("path")
    mermaid_parser.set_defaults(handler=cli_handlers.mermaid)

    rasa_export_parser = subparsers.add_parser(
        "rasa-export",
        help="export the bounded Rasa CALM portable profile",
    )
    rasa_export_parser.add_argument("path")
    rasa_export_parser.add_argument("--output-dir", required=True)
    rasa_export_parser.add_argument("--allow-lossy", action="store_true")
    rasa_export_parser.set_defaults(handler=cli_handlers.rasa_export)

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
    rasa_import_parser.set_defaults(handler=cli_handlers.rasa_import)

    open_export_parser = subparsers.add_parser(
        "open-workflow-export",
        help="export the lossless Open Workflow conversational profile",
    )
    open_export_parser.add_argument("path")
    open_export_parser.add_argument("--output", required=True)
    open_export_parser.add_argument("--namespace", default="flowspec2")
    open_export_parser.set_defaults(handler=cli_handlers.open_workflow_export)

    open_import_parser = subparsers.add_parser(
        "open-workflow-import",
        help="import the exact Open Workflow conversational profile",
    )
    open_import_parser.add_argument("path")
    open_import_parser.add_argument("--output", required=True)
    open_import_parser.set_defaults(handler=cli_handlers.open_workflow_import)

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
    authoring_benchmark_parser.add_argument(
        "--model",
        default=cli_handlers.default_gemini_model,
    )
    authoring_benchmark_parser.add_argument("--repository-revision", required=True)
    authoring_benchmark_parser.add_argument("--max-correction-rounds", type=int, default=2)
    authoring_benchmark_parser.add_argument("--output", required=True)
    authoring_benchmark_parser.set_defaults(handler=cli_handlers.authoring_benchmark_gemini)

    operational_gemini_parser = subparsers.add_parser(
        "operational-benchmark-gemini",
        help="probe final authored flows through Gemini routing and extraction",
    )
    operational_gemini_parser.add_argument("path")
    operational_gemini_parser.add_argument(
        "--allow-network",
        action="store_true",
        required=True,
        help="explicitly permit Gemini API requests for this invocation",
    )
    operational_gemini_parser.add_argument(
        "--model",
        default=cli_handlers.default_gemini_model,
    )
    operational_gemini_parser.add_argument("--repository-revision", required=True)
    operational_gemini_parser.add_argument("--output", required=True)
    operational_gemini_parser.set_defaults(handler=cli_handlers.operational_benchmark_gemini)

    operational_verify_parser = subparsers.add_parser(
        "operational-evidence-verify",
        help="verify and replay report-only operational model evidence",
    )
    operational_verify_parser.add_argument("path")
    operational_verify_parser.add_argument("--authoring-evidence", required=True)
    operational_verify_parser.add_argument("--repository-revision")
    operational_verify_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a deterministic machine-readable verification summary",
    )
    operational_verify_parser.set_defaults(handler=cli_handlers.operational_evidence_verify)

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
    evidence_verify_parser.set_defaults(handler=cli_handlers.authoring_evidence_verify)

    evidence_sign_parser = subparsers.add_parser(
        "authoring-evidence-sign",
        help="sign a verified authoring evidence digest with Ed25519",
    )
    evidence_sign_parser.add_argument("path")
    evidence_sign_parser.add_argument("--private-key", required=True)
    evidence_sign_parser.add_argument("--repository-revision")
    evidence_sign_parser.add_argument("--output", required=True)
    evidence_sign_parser.set_defaults(handler=cli_handlers.authoring_evidence_sign)

    signature_verify_parser = subparsers.add_parser(
        "authoring-evidence-signature-verify",
        help="authenticate authoring evidence with a trusted Ed25519 public key",
    )
    signature_verify_parser.add_argument("path")
    signature_verify_parser.add_argument("--signature", required=True)
    signature_verify_parser.add_argument("--public-key", required=True)
    signature_verify_parser.add_argument("--repository-revision")
    signature_verify_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a deterministic machine-readable authentication summary",
    )
    signature_verify_parser.set_defaults(handler=cli_handlers.authoring_evidence_signature_verify)

    presentation_review_init_parser = subparsers.add_parser(
        "authoring-presentation-review-init",
        help="create a source-bound human-review packet from verified authoring evidence",
    )
    presentation_review_init_parser.add_argument("path")
    presentation_review_init_parser.add_argument("--repository-revision")
    presentation_review_init_parser.add_argument("--output", required=True)
    presentation_review_init_parser.set_defaults(
        handler=cli_handlers.authoring_presentation_review_init
    )

    presentation_review_finalize_parser = subparsers.add_parser(
        "authoring-presentation-review-finalize",
        help="finalize human decisions into a canonical presentation review",
    )
    presentation_review_finalize_parser.add_argument("path")
    presentation_review_finalize_parser.add_argument("--draft", required=True)
    presentation_review_finalize_parser.add_argument("--repository-revision")
    presentation_review_finalize_parser.add_argument("--output", required=True)
    presentation_review_finalize_parser.set_defaults(
        handler=cli_handlers.authoring_presentation_review_finalize
    )

    presentation_review_verify_parser = subparsers.add_parser(
        "authoring-presentation-review-verify",
        help="verify a presentation review against exact authoring evidence",
    )
    presentation_review_verify_parser.add_argument("path")
    presentation_review_verify_parser.add_argument("--review", required=True)
    presentation_review_verify_parser.add_argument("--repository-revision")
    presentation_review_verify_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a deterministic machine-readable verification summary",
    )
    presentation_review_verify_parser.set_defaults(
        handler=cli_handlers.authoring_presentation_review_verify
    )

    presentation_review_sign_parser = subparsers.add_parser(
        "authoring-presentation-review-sign",
        help="authenticate a verified presentation review with Ed25519",
    )
    presentation_review_sign_parser.add_argument("path")
    presentation_review_sign_parser.add_argument("--review", required=True)
    presentation_review_sign_parser.add_argument("--private-key", required=True)
    presentation_review_sign_parser.add_argument("--repository-revision")
    presentation_review_sign_parser.add_argument("--output", required=True)
    presentation_review_sign_parser.set_defaults(
        handler=cli_handlers.authoring_presentation_review_sign
    )

    presentation_signature_verify_parser = subparsers.add_parser(
        "authoring-presentation-review-signature-verify",
        help="authenticate a presentation review with a trusted Ed25519 public key",
    )
    presentation_signature_verify_parser.add_argument("path")
    presentation_signature_verify_parser.add_argument("--review", required=True)
    presentation_signature_verify_parser.add_argument("--signature", required=True)
    presentation_signature_verify_parser.add_argument("--public-key", required=True)
    presentation_signature_verify_parser.add_argument("--repository-revision")
    presentation_signature_verify_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a deterministic machine-readable authentication summary",
    )
    presentation_signature_verify_parser.set_defaults(
        handler=cli_handlers.authoring_presentation_review_signature_verify
    )

    promotion_verify_parser = subparsers.add_parser(
        "authoring-promotion-verify",
        help="evaluate promotion eligibility from authenticated authoring artifacts",
    )
    promotion_verify_parser.add_argument("path")
    promotion_verify_parser.add_argument("--review", required=True)
    promotion_verify_parser.add_argument("--signature", required=True)
    promotion_verify_parser.add_argument("--public-key", required=True)
    promotion_verify_parser.add_argument("--repository-revision")
    promotion_verify_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a deterministic machine-readable promotion summary",
    )
    promotion_verify_parser.set_defaults(handler=cli_handlers.authoring_promotion_verify)

    return parser
