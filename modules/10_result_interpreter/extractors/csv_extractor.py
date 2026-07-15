"""CSV / TSV output parser (Story 6.1).

The most common tool output: a delimited table. This parser reads the text,
picks numeric columns (all of them, or a caller-selected subset by header name),
and returns one :class:`ParsedField` per column. The delimiter is sniffed from
the content by default, so the same parser handles ``.csv`` and ``.tsv``.

Header units of the form ``"logS (mol/L)"`` or ``"energy [eV]"`` are recognized:
the field name becomes ``logS`` and the unit ``mol/L``.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Dict, List, Optional

from result_interpreter.extractors.base import (
    OutputParser,
    ParsedField,
    ParsedOutput,
    ParserError,
    register,
)

_UNIT_RE = re.compile(r"^\s*(.*?)\s*[\(\[]\s*([^\)\]]+?)\s*[\)\]]\s*$")


def _split_unit(header: str) -> tuple[str, Optional[str]]:
    """"logS (mol/L)" -> ("logS", "mol/L"); "energy" -> ("energy", None)."""
    match = _UNIT_RE.match(header)
    if match:
        return match.group(1), match.group(2)
    return header.strip(), None


def _to_float(cell: str) -> Optional[float]:
    cell = cell.strip()
    if not cell:
        return None
    try:
        return float(cell)
    except ValueError:
        return None


class CsvExtractor(OutputParser):
    name = "csv"

    def parse(
        self,
        content: str,
        *,
        columns: Optional[List[str]] = None,
        delimiter: Optional[str] = None,
        **_ignored,
    ) -> ParsedOutput:
        """Extract numeric columns from delimited text.

        Args:
            columns: header names to keep (default: every numeric column).
            delimiter: force a delimiter; otherwise sniff ``,`` vs ``\\t`` vs ``;``.
        """
        text = content.strip("\n")
        if not text.strip():
            raise ParserError("csv parser received empty content")

        if delimiter is None:
            delimiter = self._sniff_delimiter(text)

        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [row for row in reader if row]
        if len(rows) < 2:
            raise ParserError("csv parser needs a header row and at least one data row")

        header = rows[0]
        names_units = [_split_unit(h) for h in header]

        wanted = set(columns) if columns else None
        # collect per-column numeric values
        columns_values: Dict[int, List[float]] = {i: [] for i in range(len(header))}
        for row in rows[1:]:
            for i in range(len(header)):
                cell = row[i] if i < len(row) else ""
                value = _to_float(cell)
                if value is not None:
                    columns_values[i].append(value)

        fields: List[ParsedField] = []
        for i, (name, unit) in enumerate(names_units):
            if wanted is not None and name not in wanted:
                continue
            values = columns_values[i]
            if not values:
                if wanted is not None and name in wanted:
                    raise ParserError(f"requested column {name!r} has no numeric values")
                continue  # skip non-numeric columns when auto-selecting
            fields.append(ParsedField(name=name, values=values, unit=unit, source=self.name))

        if not fields:
            raise ParserError("csv parser found no numeric columns")

        if wanted:
            missing = wanted - {f.name for f in fields}
            if missing:
                raise ParserError(f"requested columns not found: {sorted(missing)}")

        return ParsedOutput(fields=fields, metadata={"parser": self.name, "delimiter": delimiter})

    @staticmethod
    def _sniff_delimiter(text: str) -> str:
        first_line = text.splitlines()[0]
        candidates = {"\t": first_line.count("\t"), ",": first_line.count(","), ";": first_line.count(";")}
        best = max(candidates, key=candidates.get)
        return best if candidates[best] > 0 else ","


register(CsvExtractor())
