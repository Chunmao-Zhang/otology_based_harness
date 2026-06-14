"""Ontology workspace tools."""

from .evidence_retriever import evidence_retriever
from .ontology_backend import (
    build_dataset,
    get_schema_outline,
    save_evidence_manifest,
    save_schema,
)
from .schema_validator import schema_validator
from .source_reader import source_reader

WORKSPACE_TOOLS = [
    source_reader,
    evidence_retriever,
    schema_validator,
    save_evidence_manifest,
    save_schema,
    get_schema_outline,
    build_dataset,
]

WORKSPACE_TOOLS_MODE = "extend"
