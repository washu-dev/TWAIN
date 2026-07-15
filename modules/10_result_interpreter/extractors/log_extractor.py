"""Log / stdout parser (Story 6.1).

Many tools just print results to stdout or a log file. This parser greps lines
for caller-supplied patterns and extracts the numeric value from each match.

Patterns are given as ``{metric_name: regex}`` where the regex has either a
named group ``(?P<value>...)`` or a first capture group holding the number. If
no patterns are supplied, a sensible default pulls ``key = number`` /
``key: number`` pairs (with optional trailing unit) out of every line.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from result_interpreter.extractors.base import (
    OutputParser,
    ParsedField,
    ParsedOutput,
    ParserError,
    register,
)

_NUMBER = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"
# key: value  /  key = value   (optionally followed by a unit token)
_DEFAULT_LINE = re.compile(
    rf"^\s*(?P<name>[A-Za-z_][\w .\-/]*?)\s*[:=]\s*(?P<value>{_NUMBER})\s*(?P<unit>[A-Za-z%/]+)?\s*$"
)


def _extract_number(match: re.Match) -> Optional[float]:
    """Prefer a ``value`` named group, else the first capture group."""
    text = None
    if "value" in match.groupdict() and match.group("value") is not None:
        text = match.group("value")
    elif match.groups():
        text = match.group(1)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class LogExtractor(OutputParser):
    name = "log"

    def parse(
        self,
        content: str,
        *,
        patterns: Optional[Dict[str, str]] = None,
        units: Optional[Dict[str, str]] = None,
        **_ignored,
    ) -> ParsedOutput:
        """Grep numeric values out of log text.

        Args:
            patterns: {metric_name: regex}. Each regex should capture the number
                via ``(?P<value>...)`` or a first group. A metric may match on
                several lines -> a multi-value series (e.g. loss per epoch).
            units: optional {metric_name: unit} annotations.
        """
        units = units or {}
        if patterns:
            return self._parse_with_patterns(content, patterns, units)
        return self._parse_default(content, units)

    def _parse_with_patterns(self, content, patterns, units) -> ParsedOutput:
        compiled = {name: re.compile(rx) for name, rx in patterns.items()}
        collected: Dict[str, List[float]] = {name: [] for name in patterns}
        for line in content.splitlines():
            for name, rx in compiled.items():
                match = rx.search(line)
                if match:
                    value = _extract_number(match)
                    if value is not None:
                        collected[name].append(value)

        fields = [
            ParsedField(name=name, values=values, unit=units.get(name), source=self.name)
            for name, values in collected.items()
            if values
        ]
        if not fields:
            raise ParserError(f"log parser matched no numeric values for patterns {list(patterns)}")
        empty = [name for name, values in collected.items() if not values]
        return ParsedOutput(
            fields=fields,
            metadata={"parser": self.name, "unmatched_patterns": empty},
        )

    def _parse_default(self, content, units) -> ParsedOutput:
        collected: Dict[str, List[float]] = {}
        inferred_units: Dict[str, str] = {}
        for line in content.splitlines():
            match = _DEFAULT_LINE.match(line)
            if not match:
                continue
            name = match.group("name").strip()
            value = _extract_number(match)
            if value is None:
                continue
            collected.setdefault(name, []).append(value)
            unit = match.group("unit")
            if unit and name not in inferred_units:
                inferred_units[name] = unit

        fields = [
            ParsedField(
                name=name,
                values=values,
                unit=units.get(name, inferred_units.get(name)),
                source=self.name,
            )
            for name, values in collected.items()
        ]
        if not fields:
            raise ParserError("log parser found no 'key: value' numeric pairs")
        return ParsedOutput(fields=fields, metadata={"parser": self.name})


register(LogExtractor())
