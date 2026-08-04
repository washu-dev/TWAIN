"""Cluster connection/scheduler profiles for the execution adapter (module 08).

A :class:`ClusterProfile` is the typed, validated description of *where* and
*how* a run is submitted to a Slurm cluster: its login nodes, billing account,
the Lmod modules to load, scratch storage root, and the available partitions
(with their wall-clock limits and GPU capability). Profiles are version-control
data under ``configs/clusters/<name>.json`` (e.g. the WashU RIS Compute2
cluster) so cluster specifics never get hardcoded into the adapter.

The dataclasses follow the same coercion + ``__post_init__`` validation pattern
as the rest of the pipeline contracts (``intent_spec``, ``execution_plan``,
``graph_builder``): a plain dict in, a validated typed object out, or a
``ValueError`` describing the first problem.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union


def _repo_root() -> Path:
    """Locate the repository root by walking up to the ``pixi.toml`` marker.

    Depth-independent so the loader keeps working regardless of how deeply the
    module is nested under ``modules/NN_*``.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pixi.toml").is_file():
            return parent
    return here.parents[2]  # fallback: modules/08_execution_adapter -> repo root


CLUSTERS_DIR = _repo_root() / "configs" / "clusters"


@dataclass
class Partition:
    """One Slurm partition/queue and the constraints relevant to scheduling."""

    name: str
    max_minutes: Optional[float] = None  # None => no wall-clock limit ("infinite")
    gpus: bool = False

    def __post_init__(self):
        if not self.name or type(self.name) is not str:
            raise ValueError("Partition name must be a non-empty str")
        if self.max_minutes is not None and type(self.max_minutes) not in (int, float):
            raise ValueError("Partition max_minutes must be a number or None")
        if self.max_minutes is not None and self.max_minutes <= 0:
            raise ValueError("Partition max_minutes must be positive when set")
        if type(self.gpus) is not bool:
            raise ValueError("Partition gpus must be a bool")

    def admits(self, *, minutes: float, needs_gpu: bool) -> bool:
        """True if a job of ``minutes`` wall time (and GPU need) fits this partition."""
        if needs_gpu and not self.gpus:
            return False
        if self.max_minutes is not None and minutes > self.max_minutes:
            return False
        return True


@dataclass
class ClusterProfile:
    name: str
    login_nodes: List[str]
    account: str
    partitions: List[Union[Partition, dict]]
    accounts: List[str] = field(default_factory=list)
    modules: List[str] = field(default_factory=list)
    gpu_modules: List[str] = field(default_factory=list)
    gpu_type: Optional[str] = None
    storage_root: Optional[str] = None
    # Root of pre-provisioned Python environments on cluster storage
    # (<envs_root>/<tool>/bin/python). Jobs prefer these over building a venv,
    # which is how compiled calculators (GPAW needs libxc) run on compute nodes.
    envs_root: Optional[str] = None
    default_partition: Optional[str] = None
    gpu_partition: Optional[str] = None
    short_partition: Optional[str] = None
    # Per-node resource ceilings (from `sinfo -e -o '%P %c %m %G'` on the live
    # cluster) for the partitions jobs actually land on. Surfaced on the
    # approval card so the researcher knows how far the editable Slurm request
    # can go; None = unknown (the card then shows no maximum).
    max_cpus_per_node: Optional[int] = None
    max_gpus_per_node: Optional[int] = None
    max_ram_gb: Optional[int] = None
    description: str = ""

    def __post_init__(self):
        if not self.name or type(self.name) is not str:
            raise ValueError("ClusterProfile name must be a non-empty str")
        if type(self.login_nodes) is not list or not self.login_nodes or any(
            type(h) is not str or not h for h in self.login_nodes
        ):
            raise ValueError("ClusterProfile login_nodes must be a non-empty list of str")
        if not self.account or type(self.account) is not str:
            raise ValueError("ClusterProfile account must be a non-empty str")

        if type(self.partitions) is not list or not self.partitions:
            raise ValueError("ClusterProfile partitions must be a non-empty list")
        coerced: List[Partition] = []
        seen = set()
        for p in self.partitions:
            if type(p) is dict:
                p = Partition(**p)
            elif type(p) is not Partition:
                raise ValueError("partitions items must be of type dict or Partition")
            if p.name in seen:
                raise ValueError(f"Duplicate partition name: {p.name!r}")
            seen.add(p.name)
            coerced.append(p)
        self.partitions = coerced

        for attr in ("modules", "gpu_modules", "accounts"):
            value = getattr(self, attr)
            if type(value) is not list or any(type(m) is not str for m in value):
                raise ValueError(f"ClusterProfile {attr} must be a list of str")
        if self.gpu_type is not None and type(self.gpu_type) is not str:
            raise ValueError("ClusterProfile gpu_type must be a str or None")
        if self.envs_root is not None and type(self.envs_root) is not str:
            raise ValueError("ClusterProfile envs_root must be a str or None")
        for attr in ("max_cpus_per_node", "max_gpus_per_node", "max_ram_gb"):
            value = getattr(self, attr)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"ClusterProfile {attr} must be a positive int or None")

        # Named partitions, when set, must actually exist in the partition list.
        for attr in ("default_partition", "gpu_partition", "short_partition"):
            value = getattr(self, attr)
            if value is not None and value not in seen:
                raise ValueError(
                    f"ClusterProfile {attr}={value!r} is not a defined partition"
                )

    # ------------------------------------------------------------------ lookups
    def partition(self, name: str) -> Partition:
        for p in self.partitions:
            if p.name == name:
                return p
        raise KeyError(f"unknown partition {name!r}")

    @classmethod
    def from_dict(cls, data: dict) -> "ClusterProfile":
        return cls(**data)

    @classmethod
    def load(cls, name_or_path: str) -> "ClusterProfile":
        """Load a profile by cluster name (``configs/clusters/<name>.json``) or path."""
        path = Path(name_or_path)
        if not path.suffix:  # a bare cluster name
            path = CLUSTERS_DIR / f"{name_or_path}.json"
        if not path.is_file():
            raise FileNotFoundError(f"cluster profile not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
