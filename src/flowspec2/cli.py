"""Tiny CLI: validate a flow document or print its compiled node graph.

    flowspec2 validate examples/reparo_luminaria.flow.json
    flowspec2 graph    examples/reparo_luminaria.flow.json
"""

from __future__ import annotations

import sys

from .compiler import compile_flow
from .schema import load_flow


def _validate(path: str) -> int:
    load_flow(path)  # raises on invalid
    print(f"OK: {path} is valid flowspec/2")
    return 0


def _graph(path: str) -> int:
    doc = load_flow(path)
    compiled = compile_flow(doc)
    g = compiled.graph.get_graph()
    print(f"flow: {doc['flow']} v{doc['version']}  (entry: {compiled.entry_node_id})")
    print("nodes:")
    for node in g.nodes:
        print(f"  - {node}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2 or argv[0] not in ("validate", "graph"):
        print(__doc__)
        return 2
    cmd, path = argv
    return _validate(path) if cmd == "validate" else _graph(path)


if __name__ == "__main__":
    raise SystemExit(main())
