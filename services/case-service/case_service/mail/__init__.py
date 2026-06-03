"""HELIX Email — package named `mail` to avoid shadowing stdlib email."""
from .templates import render_template, TemplateError
from .threader import (
    extract_subject_tag, build_subject_tag, resolve_case_id_from_message,
    build_message_id, build_references_chain,
)
from .parser import parse_rfc822
from .service import EmailService

__all__ = [
    "render_template", "TemplateError",
    "extract_subject_tag", "build_subject_tag", "resolve_case_id_from_message",
    "build_message_id", "build_references_chain",
    "parse_rfc822", "EmailService",
]
