"""Safe YAML I/O shared by compatibility profiles."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import yaml
from yaml.nodes import MappingNode, ScalarNode


class YamlDocumentError(ValueError):
    """Raised when a compatibility YAML document is unsafe or malformed."""


class DuplicateYamlKeyError(YamlDocumentError):
    """Raised when a YAML mapping repeats a key."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects PyYAML's last-key-wins behavior."""


_YAML_BOOLEAN_TAG = "tag:yaml.org,2002:bool"
_YAML_FLOAT_TAG = "tag:yaml.org,2002:float"
_YAML_INTEGER_TAG = "tag:yaml.org,2002:int"
_YAML_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_YAML_1_1_SCALAR_TAGS = {
    _YAML_BOOLEAN_TAG,
    _YAML_FLOAT_TAG,
    _YAML_INTEGER_TAG,
    _YAML_TIMESTAMP_TAG,
}
_UniqueKeySafeLoader.yaml_implicit_resolvers = {
    initial_character: [
        resolver for resolver in resolvers if resolver[0] not in _YAML_1_1_SCALAR_TAGS
    ]
    for initial_character, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_UniqueKeySafeLoader.add_implicit_resolver(  # type: ignore[no-untyped-call]
    _YAML_BOOLEAN_TAG,
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)
_UniqueKeySafeLoader.add_implicit_resolver(  # type: ignore[no-untyped-call]
    _YAML_INTEGER_TAG,
    re.compile(r"^[-+]?(?:0o[0-7]+|0x[0-9a-fA-F]+|[0-9]+)$"),
    list("-+0123456789"),
)
_UniqueKeySafeLoader.add_implicit_resolver(  # type: ignore[no-untyped-call]
    _YAML_FLOAT_TAG,
    re.compile(
        r"^(?:"
        r"[-+]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+))"
        r"|[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)"
        r"|[-+]?\.(?:inf|Inf|INF)"
        r"|\.(?:nan|NaN|NAN)"
        r")$"
    ),
    list("-+0123456789."),
)


def _construct_yaml_1_2_integer(
    loader: _UniqueKeySafeLoader,
    node: ScalarNode,
) -> int:
    scalar = loader.construct_scalar(node)
    sign = -1 if scalar.startswith("-") else 1
    unsigned_scalar = scalar[1:] if scalar[:1] in {"-", "+"} else scalar
    if unsigned_scalar.startswith("0o"):
        return sign * int(unsigned_scalar[2:], 8)
    if unsigned_scalar.startswith("0x"):
        return sign * int(unsigned_scalar[2:], 16)
    return sign * int(unsigned_scalar, 10)


_UniqueKeySafeLoader.add_constructor(
    _YAML_INTEGER_TAG,
    _construct_yaml_1_2_integer,
)


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise YamlDocumentError(
                f"unhashable YAML mapping key at line {key_node.start_mark.line + 1}"
            ) from exc
        if duplicate:
            raise DuplicateYamlKeyError(
                f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def loads_yaml_mapping(text: str, *, source: str = "<string>") -> dict[str, Any]:
    """Parse one YAML mapping without object construction or duplicate keys."""

    try:
        document = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except YamlDocumentError:
        raise
    except yaml.YAMLError as exc:
        raise YamlDocumentError(f"invalid YAML document {source}: {exc}") from exc
    if not isinstance(document, dict):
        raise YamlDocumentError(f"YAML document {source} must be a non-empty mapping")
    if not document:
        raise YamlDocumentError(f"YAML document {source} must be a non-empty mapping")
    return document


def load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    """Read one UTF-8 YAML mapping from disk."""

    document_path = Path(path)
    return loads_yaml_mapping(
        document_path.read_text(encoding="utf-8"),
        source=str(document_path),
    )


def dumps_yaml_mapping(document: Mapping[str, Any]) -> str:
    """Serialize a mapping as stable, Unicode-preserving safe YAML."""

    return yaml.safe_dump(
        dict(document),
        allow_unicode=True,
        sort_keys=False,
    )
