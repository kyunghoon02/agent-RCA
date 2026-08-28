"""Versioned root-cause identifiers shared by RCA producers and evaluators."""

from __future__ import annotations

from typing import Literal, Tuple, get_args


RootCauseId = Literal[
    "kubernetes.container-oomkilled",
    "kubernetes.image-pull-failure",
    "kubernetes.missing-configmap",
]

ROOT_CAUSE_TAXONOMY_VERSION = "1.0.0"
ROOT_CAUSE_IDS: Tuple[str, ...] = get_args(RootCauseId)
