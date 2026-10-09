"""JSON output parser (Story 6.1).

Reads a JSON document and pulls out numeric leaves. Nested objects are flattened
to dot-paths (``metrics.logS`` -> field name ``metrics.logS``) and arrays are
indexed (``losses[0]``). Callers can either take every numeric leaf (default) or
request specific dot-paths via ``fields``; a path pointing at a numeric array is
returned as a multi-value series (useful for e.g. a loss curve).
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from result_interpreter.extractors.base import (
    OutputParser,
    ParsedField,
    ParsedOutput,
    ParserError,
    quantity,
    register,
)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _flatten(obj, prefix: str = "", units: Optional[Dict[str, str]] = None
             ) -> Dict[str, List[float]]:
    """Flatten to {dot_path: [values]}. A numeric array collapses to one entry
    holding all its numbers; scalars become length-1 lists. A string written as
    a number with its unit ("-1.99 log10(mol/L)") counts, its unit recorded in
    ``units``."""
    out: Dict[str, List[float]] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(value, path, units))
    elif isinstance(obj, list):
        if obj and all(_is_number(v) for v in obj):
            out[prefix] = [float(v) for v in obj]  # numeric array -> series
        else:
            for idx, value in enumerate(obj):
                out.update(_flatten(value, f"{prefix}[{idx}]", units))
    elif _is_number(obj):
        out[prefix or "value"] = [float(obj)]
    elif isinstance(obj, str):
        found = quantity(obj)
        if found is not None:
            out[prefix or "value"] = [found[0]]
            if found[1] and units is not None:
                units[prefix or "value"] = found[1]
    return out


class JsonExtractor(OutputParser):
    name = "json"

    def parse(
        self,
        content: str,
        *,
        fields: Optional[List[str]] = None,
        units: Optional[Dict[str, str]] = None,
        **_ignored,
    ) -> ParsedOutput:
        """Extract numeric fields from a JSON document.

        Args:
            fields: dot-paths to keep (default: every numeric leaf).
            units: optional {dot_path: unit} annotations.
        """
        if isinstance(content, str):
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ParserError(f"json parser could not decode input: {exc}") from exc
        else:
            data = content  # already-decoded object

        found_units: Dict[str, str] = {}
        flat = _flatten(data, units=found_units)
        if not flat:
            raise ParserError("json parser found no numeric fields")

        units = {**found_units, **(units or {})}
        if fields:
            missing = [f for f in fields if f not in flat]
            if missing:
                raise ParserError(f"requested fields not found: {missing}")
            selected = [(f, flat[f]) for f in fields]
        else:
            selected = list(flat.items())

        parsed_fields = [
            ParsedField(name=name, values=values, unit=units.get(name), source=self.name)
            for name, values in selected
        ]
        return ParsedOutput(fields=parsed_fields, metadata={"parser": self.name})


register(JsonExtractor())
