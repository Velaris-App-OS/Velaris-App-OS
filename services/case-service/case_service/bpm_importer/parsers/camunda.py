"""Camunda parser — thin wrapper around the full BPMN 2.0 parser.

All logic has moved to bpmn2.py which handles Camunda, jBPM, Flowable,
IBM BAW, Oracle BPM, Bizagi, and Bonitasoft.
"""
from case_service.bpm_importer.parsers.bpmn2 import parse_files  # noqa: F401
