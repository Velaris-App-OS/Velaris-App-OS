"""
BPMN Compiler — Pipeline Facade
================================

This is the single entry point for compiling BPMN 2.0 XML into a
validated, optimized ``BPMNProcess`` IR.

Pipeline::

    BPMN XML
      │
      ▼
    ┌──────────┐
    │  Parser  │  XML → BPMNProcess (raw IR)
    └────┬─────┘
         │
         ▼
    ┌────────────┐
    │ Validator  │  Check for structural/semantic errors
    └────┬───────┘
         │
         ▼
    ┌────────────┐
    │ Optimizer  │  Remove dead paths, simplify graph
    └────┬───────┘
         │
         ▼
    BPMNProcess (ready for runtime/temporal)

Usage::

    from helix_engine.compiler import BPMNCompiler, CompilationError

    compiler = BPMNCompiler()
    try:
        result = compiler.compile(bpmn_xml_string)
        process = result.process  # BPMNProcess IR
    except CompilationError as e:
        print(f"Compilation failed: {e.errors}")

Each phase is a separate module you can also use independently::

    from helix_engine.compiler.parser import BPMNParser
    from helix_engine.compiler.validator import Validator
    from helix_engine.compiler.optimizer import Optimizer
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from helix_ir.models.process import BPMNProcess

from helix_engine.compiler.parser import BPMNParser
from helix_engine.compiler.validator import Validator, ValidationResult
from helix_engine.compiler.optimizer import Optimizer, OptimizationReport

logger = structlog.get_logger()


class CompilationError(Exception):
    """
    Raised when compilation fails due to validation errors.

    ``errors`` contains the list of specific problems found.
    """
    def __init__(self, errors: list[str], process_id: str | None = None):
        self.errors = errors
        self.process_id = process_id
        summary = "; ".join(errors[:3])
        if len(errors) > 3:
            summary += f" ... and {len(errors) - 3} more"
        super().__init__(f"Compilation failed for '{process_id}': {summary}")


@dataclass
class CompilationResult:
    """
    Everything the compiler produces — the process IR plus metadata.

    Inspect ``validation`` and ``optimization`` for debugging.
    """
    process: BPMNProcess
    validation: ValidationResult
    optimization: OptimizationReport


class BPMNCompiler:
    """
    Compiles BPMN 2.0 XML into a validated, optimized ``BPMNProcess``.

    This is the main entry point for the compiler pipeline.  It chains
    the parser, validator, and optimizer in sequence and returns a
    ``CompilationResult`` with the process IR and diagnostics.

    Args:
        strict: If True (default), raise ``CompilationError`` on validation errors.
                If False, return the result even with errors (for tooling/debugging).
    """

    def __init__(self, strict: bool = True):
        self.strict = strict
        self.parser = BPMNParser()
        self.validator = Validator()
        self.optimizer = Optimizer()

    def compile(
        self,
        bpmn_xml: str | bytes,
        process_index: int = 0,
    ) -> CompilationResult:
        """
        Compile BPMN XML through the full pipeline.

        Args:
            bpmn_xml: Raw BPMN 2.0 XML content (string or bytes).
            process_index: Which ``<process>`` to compile if the file
                           contains multiple (default: first).

        Returns:
            ``CompilationResult`` with the process IR, validation result,
            and optimization report.

        Raises:
            CompilationError: If validation finds errors and ``strict=True``.
            ParseError: If the XML is structurally unparseable.
        """
        # ── Phase 1: Parse ────────────────────────────────────────
        processes = self.parser.parse(bpmn_xml)

        if process_index >= len(processes):
            raise CompilationError(
                [f"Requested process index {process_index}, "
                 f"but document contains {len(processes)} process(es)"],
            )

        process = processes[process_index]
        logger.info("compile_phase_1_done", process=process.id,
                     elements=len(process.elements), flows=len(process.flows))

        # ── Phase 2: Validate ─────────────────────────────────────
        validation = self.validator.validate(process)

        if validation.errors and self.strict:
            raise CompilationError(validation.errors, process_id=process.id)

        logger.info("compile_phase_2_done", process=process.id,
                     valid=validation.is_valid,
                     warnings=len(validation.warnings))

        # ── Phase 3: Optimize ─────────────────────────────────────
        optimization = self.optimizer.optimize(process)

        logger.info("compile_phase_3_done", process=process.id,
                     changes=optimization.had_changes)

        # ── Done ──────────────────────────────────────────────────
        logger.info("compilation_complete", process=process.id,
                     elements=len(process.elements), flows=len(process.flows))

        return CompilationResult(
            process=process,
            validation=validation,
            optimization=optimization,
        )

    def compile_all(self, bpmn_xml: str | bytes) -> list[CompilationResult]:
        """Compile ALL processes in a BPMN document."""
        processes = self.parser.parse(bpmn_xml)
        return [
            self.compile(bpmn_xml, process_index=i)
            for i in range(len(processes))
        ]
