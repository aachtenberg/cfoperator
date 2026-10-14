"""Opt-in OpenTelemetry tracing (CFOP-316).

Off by default, and off means absent: this is the only module that names
``opentelemetry``, and it imports the SDK inside ``init_tracing()`` only once
``observability.tracing.enabled`` is true. Until then ``span()`` hands back a
no-op, nothing is exported and no connection is ever opened -- the same
no-phone-home promise the rest of the agent keeps. tests/test_tracing.py holds
both halves: no other module may import opentelemetry, and a disabled process
never loads it.

What goes on a span is decided by the caller, and the rule is shapes, not
text: provider, model, token counts, finish reasons, tool names, durations.
Never a prompt, a completion, a tool's arguments or its output -- those are
the operator's infrastructure and go to whoever reads the trace store. The
one piece of free text, an alert's summary, goes through ``alert_text()``.

Exceptions get the same treatment. The SDK's default is to record one as a
span event carrying its message, and a message is provider- or tool-written
text that can quote anything, so ``span()`` records the class name only.
"""

import functools
import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional

from cfshared.tool_args import redact_tool_args

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_NAME = 'cfoperator'

# An alert summary is the only operator-written text a span carries. Long
# enough to recognise the alert in a trace list, short enough that nothing
# pasted into an annotation rides along whole.
ALERT_TEXT_LIMIT = 200
# Span attribute strings that are not free text (model, tool and provider
# names) still come from config or from a model's output; cap them anyway.
ATTR_LIMIT = 256

_tracer = None


class _NoopSpan:
    """What ``span()`` yields while tracing is off: every call does nothing."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        pass

    def is_recording(self) -> bool:
        return False


_NOOP = _NoopSpan()


def enabled() -> bool:
    """True once ``init_tracing`` has installed an exporter."""
    return _tracer is not None


def init_tracing(config: Optional[Mapping[str, Any]]) -> bool:
    """Start exporting spans if ``observability.tracing.enabled`` is true.

    Endpoint and service name come from the config when given, else from the
    standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_SERVICE_NAME``, which the
    exporter and resource read themselves -- so a deploy can point at Tempo
    without a config edit. Returns whether tracing is on. A missing SDK is a
    warning, never a crash: the agent runs untraced rather than not at all.
    """
    global _tracer
    observability = (config or {}).get('observability') or {}
    cfg = observability.get('tracing') or {}
    if not cfg.get('enabled'):
        return False
    if _tracer is not None:
        return True
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logger.warning(f"[TRACING] observability.tracing.enabled is true but the "
                       f"OpenTelemetry SDK is not installed ({e}); running untraced")
        return False

    service_name = (cfg.get('service_name') or os.getenv('OTEL_SERVICE_NAME')
                    or DEFAULT_SERVICE_NAME)
    provider = TracerProvider(resource=Resource.create({'service.name': service_name}))
    endpoint = (cfg.get('endpoint') or '').rstrip('/')
    # An explicit endpoint is the collector/Tempo base URL, like the env var;
    # the exporter's own argument wants the full signal path.
    exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces") if endpoint else OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    # Global as well, so CFOP-317's auto-instrumentation lands in the same
    # provider rather than a second, unexported one.
    trace.set_tracer_provider(provider)
    use_provider(provider)
    logger.info(f"[TRACING] exporting spans as service.name={service_name} to "
                f"{endpoint or os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT') or 'the OTLP default'}")
    return True


def use_provider(provider) -> None:
    """Trace through ``provider`` (tests hand in one with an in-memory exporter)."""
    global _tracer
    _tracer = provider.get_tracer('cfoperator')


def reset() -> None:
    """Back to off. For tests; the running agent never turns tracing off."""
    global _tracer
    _tracer = None


def _clean(attributes: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Drop None values and clip strings; OTel rejects None outright."""
    out = {}
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and len(value) > ATTR_LIMIT:
            value = value[:ATTR_LIMIT] + '…'
        elif isinstance(value, (list, tuple)):
            value = [str(v)[:ATTR_LIMIT] for v in value if v is not None]
        out[key] = value
    return out


@contextmanager
def span(name: str, attributes: Optional[Mapping[str, Any]] = None) -> Iterator[Any]:
    """A span around the block, or a no-op while tracing is off.

    An exception leaving the block marks the span ERROR with ``error.type``
    set to its class and is re-raised; its message is not recorded (see the
    module docstring).
    """
    if _tracer is None:
        yield _NOOP
        return
    from opentelemetry.trace import Status, StatusCode
    with _tracer.start_as_current_span(
            name[:ATTR_LIMIT], attributes=_clean(attributes),
            record_exception=False, set_status_on_exception=False) as s:
        try:
            yield s
        except Exception as e:
            s.set_attribute('error.type', type(e).__name__)
            s.set_status(Status(StatusCode.ERROR, type(e).__name__))
            raise


def traced(name: str, count_attr: Optional[str] = None):
    """Decorator form of ``span()`` for a function that is one phase of
    something larger (CFOP-385: the sweep's phases). With ``count_attr``, a
    sized result's length goes on the span under that name -- how many
    findings a phase produced, never what they say."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with span(name):
                result = fn(*args, **kwargs)
                if count_attr and hasattr(result, '__len__'):
                    annotate({count_attr: len(result)})
                return result
        return inner
    return wrap


def annotate(attributes: Mapping[str, Any]) -> None:
    """Set attributes on whichever span is current; nothing while off.

    Lets a function add what it learned to the span its caller opened, so a
    long body does not have to be re-indented under a ``with``.
    """
    if _tracer is None:
        return
    from opentelemetry import trace
    current = trace.get_current_span()
    if current.is_recording():
        current.set_attributes(_clean(attributes))


def mark_error(error_type: str) -> None:
    """Mark the current span failed without an exception leaving it -- for a
    call that reports failure in its return value. Class-shaped, like the
    exception path: a fixed type name, never a message."""
    if _tracer is None:
        return
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
    current = trace.get_current_span()
    if current.is_recording():
        current.set_attribute('error.type', error_type)
        current.set_status(Status(StatusCode.ERROR, error_type))


def alert_text(text: Any, limit: int = ALERT_TEXT_LIMIT) -> Optional[str]:
    """An alert's free text, fit for a span: secret-shaped spans redacted
    with the same rules tool arguments get, then clipped to ``limit``."""
    if text is None or text == '':
        return None
    return redact_tool_args(str(text), limit)


def alert_attributes(alert: Mapping[str, Any]) -> Dict[str, Any]:
    """The span attributes that identify an alert, all passed through
    ``alert_text`` -- an alertname is operator-written too."""
    alert = alert or {}
    details = alert.get('details') if isinstance(alert.get('details'), dict) else {}
    labels = alert.get('labels') or details.get('labels') or {}
    if not isinstance(labels, dict):
        labels = {}
    summary = (alert.get('summary')
               or (alert.get('annotations') or {}).get('summary'))
    return {
        'cfop.alert.name': alert_text(details.get('alertname') or labels.get('alertname'), 64),
        'cfop.alert.severity': alert_text(alert.get('severity') or labels.get('severity'), 32),
        'cfop.alert.source': alert_text(alert.get('source'), 32),
        'cfop.alert.id': alert_text(alert.get('alert_id'), 128),
        'cfop.alert.summary': alert_text(summary),
    }
