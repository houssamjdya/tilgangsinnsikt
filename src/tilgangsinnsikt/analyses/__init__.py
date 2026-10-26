"""Analyses that read the model tables and write findings."""

from tilgangsinnsikt.analyses import lifecycle, peer_group, sod
from tilgangsinnsikt.analyses.base import REGISTRY, Finding

__all__ = ["REGISTRY", "Finding", "lifecycle", "peer_group", "sod"]
