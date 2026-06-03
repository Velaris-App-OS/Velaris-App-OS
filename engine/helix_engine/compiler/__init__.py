"""
helix_engine.compiler — BPMN 2.0 Compiler Pipeline
====================================================

Quick start::

    from helix_engine.compiler import BPMNCompiler, CompilationError

    compiler = BPMNCompiler()
    result = compiler.compile(bpmn_xml)
    process = result.process  # BPMNProcess IR

For individual phases::

    from helix_engine.compiler.parser import BPMNParser
    from helix_engine.compiler.validator import Validator
    from helix_engine.compiler.optimizer import Optimizer
"""

from helix_engine.compiler.compiler import (
    BPMNCompiler,
    CompilationError,
    CompilationResult,
)

__all__ = ["BPMNCompiler", "CompilationError", "CompilationResult"]
