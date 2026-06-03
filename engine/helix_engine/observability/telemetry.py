from __future__ import annotations
import os

_initialized = False


def configure_telemetry(service_name: str, app=None, sql_engine=None) -> None:
    global _initialized
    if _initialized:
        if app is not None:
            _instrument_app(app)
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except Exception as e:
        print(f"[helix.observability] OTel SDK unavailable: {e}")
        return

    resource = Resource.create({
        "service.name": service_name,
        "service.namespace": "helix",
        "deployment.environment": os.getenv("HELIX_ENV", "development"),
    })
    provider = TracerProvider(resource=resource)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
            )
        except Exception as e:
            print(f"[helix.observability] OTLP exporter disabled: {e}")

    if os.getenv("HELIX_OTEL_CONSOLE") == "1":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)

    if app is not None:
        _instrument_app(app)
    if sql_engine is not None:
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument(engine=sql_engine)
        except Exception as e:
            print(f"[helix.observability] SQLAlchemy instrumentation skipped: {e}")

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except Exception as e:
        print(f"[helix.observability] httpx instrumentation skipped: {e}")

    _initialized = True


def _instrument_app(app) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:
        print(f"[helix.observability] FastAPI instrumentation skipped: {e}")


def get_tracer(name: str):
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except Exception:
        from contextlib import contextmanager
        class _Noop:
            def start_as_current_span(self, *a, **kw):
                @contextmanager
                def _c():
                    yield None
                return _c()
        return _Noop()
