"""Analyses that read the model tables and write findings."""

from tilgangsinnsikt.analyses import lifecycle, log_anomalies, peer_group, sod
from tilgangsinnsikt.analyses.base import REGISTRY, Finding

__all__ = ["REGISTRY", "Finding", "lifecycle", "log_anomalies", "peer_group", "sod"]
