"""Ontology workspace tools."""

from .evidence_retriever import evidence_retriever
from .schema_validator import schema_validator
from .source_reader import source_reader

WORKSPACE_TOOLS = [
    source_reader,
    evidence_retriever,
    schema_validator,
]

WORKSPACE_TOOLS_MODE = "extend"
