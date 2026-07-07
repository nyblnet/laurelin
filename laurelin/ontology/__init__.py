"""Ontology layer: YAML definitions, object queries, links, actions, edits."""

from laurelin.ontology.loader import load_ontology
from laurelin.ontology.service import OntologyService

__all__ = ["load_ontology", "OntologyService"]
