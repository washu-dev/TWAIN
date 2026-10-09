"""Pluggable output-parser framework for the result interpreter (Story 6.1).

Tool outputs come in many shapes -- CSV/TSV tables, JSON blobs, free-form log
lines, or a tool-specific dict of properties. Each parser here turns one of
those raw forms into a common intermediate: a :class:`ParsedOutput` holding one
or more named numeric :class:`ParsedField` series. The metric normalizer then
collapses that into a single primary/secondary metric view (see
``metric_normalizer.py``).

Parsers are *pluggable*: register a new one with :func:`register` (or the
``@register`` decorator on an instance) and it becomes reachable via
:func:`get_parser` / :func:`available_parsers` without touching any core logic.
The four built-in parsers (``csv``, ``json``, ``log``, ``rdkit``) are lazily
imported the first time the registry is queried, so importing this module has no
side effects and no import cycles.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# A value written with its unit: "-1.9919 log10(mol/L)", "1.3101 (dimensionless)",
# "180.159 g/mol". Generated scripts print these as often as bare numbers (run
# ec48cda0 vs 4e51dd7d: the same ESOL result, one as a string with its unit, one
# as a float), and reading only the floats made the run fail "more often than
# not". The number must lead; the rest is the unit and may not hold another
# number on its own ("1 to 2" stays unreadable).
_QUANTITY = re.compile(
    r"^\s*([-+\u2212]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+\u2212]?\d+)?)\s*(\S.*?)?\s*$")
_LONE_NUMBER = re.compile(r"(?:^|\s)[-+]?(?:\d+\.?\d*|\.\d+)(?:\s|$)")


def quantity(text) -> Optional[Tuple[float, Optional[str]]]:
    """``(value, unit)`` from a number written with an optional unit, else None."""
    if not isinstance(text, str):
        return None
    match = _QUANTITY.match(text)
    if not match:
        return None
    unit = (match.group(2) or "").strip() or None
    if unit and (len(unit) > 40 or _LONE_NUMBER.search(unit)):
        return None
    try:
        value = float(match.group(1).replace("\u2212", "-"))
    except ValueError:
        return None
    if unit and unit.startswith("(") and unit.endswith(")"):
        unit = unit[1:-1].strip() or None
    return value, unit


class ParserError(Exception):
    """Raised when a parser cannot extract anything usable from its input."""


@dataclass
class ParsedField:
    """One named numeric series pulled from a tool output.

    ``values`` always holds at least one number. A scalar reading (a single log
    match, one JSON field) is a length-1 series; a CSV column or a per-epoch loss
    curve is a longer one. ``unit`` is whatever the parser could infer (often
    ``None`` -- the normalizer lets callers supply units explicitly).
    """

    name: str
    values: List[float]
    unit: Optional[str] = None
    source: str = ""  # the parser that produced this field

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("ParsedField.name must be a non-empty string")
        if not isinstance(self.values, list) or not self.values:
            raise ValueError(f"ParsedField {self.name!r} must have a non-empty values list")
        coerced: List[float] = []
        for v in self.values:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"ParsedField {self.name!r} values must be numbers, got {v!r}")
            coerced.append(float(v))
        self.values = coerced


@dataclass
class ParsedOutput:
    """The parser-agnostic result of reading one tool output."""

    fields: List[ParsedField]
    metadata: Dict[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.fields, list) or not self.fields:
            raise ParserError("no numeric fields could be extracted from the output")
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate field names in ParsedOutput: {names}")

    def field_names(self) -> List[str]:
        return [f.name for f in self.fields]

    def get(self, name: str) -> Optional[ParsedField]:
        return next((f for f in self.fields if f.name == name), None)


class OutputParser(ABC):
    """Base class for a pluggable parser. Subclasses set ``name`` and ``parse``."""

    #: registry key (e.g. "csv", "json", "log", "rdkit")
    name: str = ""

    @abstractmethod
    def parse(self, content: str, **options) -> ParsedOutput:
        """Turn raw ``content`` (file text / stdout) into a :class:`ParsedOutput`."""
        raise NotImplementedError

    def parse_file(self, path, **options) -> ParsedOutput:
        """Convenience: read ``path`` as UTF-8 text and parse it."""
        text = Path(path).read_text(encoding="utf-8")
        out = self.parse(text, **options)
        out.metadata.setdefault("source_path", str(path))
        return out


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
_REGISTRY: Dict[str, OutputParser] = {}
_BUILTINS_LOADED = False


def register(parser: OutputParser) -> OutputParser:
    """Register a parser instance under its ``name`` (returns it, so it can be
    used as a decorator on a module-level instance)."""
    if not getattr(parser, "name", ""):
        raise ValueError("parser must define a non-empty 'name'")
    _REGISTRY[parser.name] = parser
    return parser


def _load_builtins() -> None:
    """Import the shipped parsers once so they self-register. Kept lazy to avoid
    an import cycle (each parser imports this module)."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from result_interpreter.extractors import (  # noqa: F401
        csv_extractor,
        json_extractor,
        log_extractor,
        rdkit_extractor,
    )


def get_parser(name: str) -> OutputParser:
    """Return the registered parser for ``name`` (e.g. ``"csv"``)."""
    _load_builtins()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ParserError(
            f"no parser registered for {name!r}; available: {available_parsers()}"
        ) from None


def available_parsers() -> List[str]:
    """Sorted list of registered parser names."""
    _load_builtins()
    return sorted(_REGISTRY)
