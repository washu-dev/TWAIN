"""RDKit tool-specific parser (Story 6.1).

Demonstrates the "tool-specific parser" slot: a parser that knows the shape and
units of one tool's output. RDKit descriptor calculations produce a dict of
molecular properties (``MolWt``, ``MolLogP``, ``TPSA``, ...). We don't depend on
rdkit here -- the executor is expected to dump the computed descriptors as JSON
(``{"MolWt": 180.16, "MolLogP": 1.31}``) or an already-decoded dict -- and this
parser attaches the known unit for each descriptor.

Adding support for another tool is exactly this file, registered under its own
name; no core logic changes (the pluggability requirement).
"""
from __future__ import annotations

import json
from typing import Dict, Optional

from result_interpreter.extractors.base import (
    OutputParser,
    ParsedField,
    ParsedOutput,
    ParserError,
    register,
)

# Known RDKit descriptors -> physical unit (None = dimensionless).
_RDKIT_UNITS: Dict[str, Optional[str]] = {
    "MolWt": "g/mol",
    "ExactMolWt": "g/mol",
    "MolLogP": None,          # logP is dimensionless
    "MolMR": "cm^3/mol",
    "TPSA": "A^2",
    "NumHDonors": "count",
    "NumHAcceptors": "count",
    "NumRotatableBonds": "count",
    "HeavyAtomCount": "count",
    "FractionCSP3": None,
    "logS": "log10(mol/L)",
}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class RdkitExtractor(OutputParser):
    name = "rdkit"

    def parse(self, content: str, *, properties=None, **_ignored) -> ParsedOutput:
        """Parse an RDKit descriptor dump (JSON string or dict).

        Args:
            properties: optional subset of descriptor names to keep.
        """
        if isinstance(content, str):
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ParserError(f"rdkit parser could not decode input: {exc}") from exc
        else:
            data = content
        if not isinstance(data, dict):
            raise ParserError("rdkit parser expects a dict / JSON object of descriptors")

        wanted = set(properties) if properties else None
        fields = []
        for name, value in data.items():
            if wanted is not None and name not in wanted:
                continue
            if not _is_number(value):
                continue
            fields.append(
                ParsedField(
                    name=name,
                    values=[float(value)],
                    unit=_RDKIT_UNITS.get(name),
                    source=self.name,
                )
            )

        if not fields:
            raise ParserError("rdkit parser found no numeric descriptors")
        if wanted:
            missing = wanted - {f.name for f in fields}
            if missing:
                raise ParserError(f"requested descriptors not found: {sorted(missing)}")
        return ParsedOutput(fields=fields, metadata={"parser": self.name, "tool": "rdkit"})


register(RdkitExtractor())
