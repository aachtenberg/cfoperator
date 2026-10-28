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
import hashlib
import json
import logging
import os
import threading
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

# Where an inbound trace context rides on an alert dict between the HTTP
# route that received it and the root span opened later, on another thread
# (CFOP-317: /v1/investigate enqueues, and the investigation starts on the
# worker). run_investigation and run_triage pop it before anything else
# sees the alert.
TRACE_CARRIER_KEY = '_trace_carrier'
_CARRIER_HEADERS = ('traceparent', 'tracestate')

_tracer = None
_provider = None
# Spans span() has open, so a shutdown can end what is still in flight
# (CFOP-394). A span never ended is never exported, and its trace shows in
# Grafana as "<root span not yet received>" forever.
_open_spans = set()
_open_lock = threading.Lock()


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


def _service_name(cfg: Mapping[str, Any], default: str) -> str:
    """Config, then the standard env var, then the process's own default.

    The agent and the event runtime read one config file (both mount it), so
    a name set there names both; only an unset one lets each process name
    itself, which is what tells their spans apart in a trace (CFOP-317).
    """
    return cfg.get('service_name') or os.getenv('OTEL_SERVICE_NAME') or default


def init_tracing(config: Optional[Mapping[str, Any]],
                 default_service_name: str = DEFAULT_SERVICE_NAME) -> bool:
    """Start exporting spans if ``observability.tracing.enabled`` is true.

    Endpoint and service name come from the config when given, else from the
    standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_SERVICE_NAME``, which the
    exporter and resource read themselves -- so a deploy can point at Tempo
    without a config edit. ``default_service_name`` is what a process calls
    itself when neither says. Returns whether tracing is on. A missing SDK is
    a warning, never a crash: the agent runs untraced rather than not at all.
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

    service_name = _service_name(cfg, default_service_name)
    provider = TracerProvider(resource=Resource.create({'service.name': service_name}))
    endpoint = (cfg.get('endpoint') or '').rstrip('/')
    # An explicit endpoint is the collector/Tempo base URL, like the env var;
    # the exporter's own argument wants the full signal path.
    exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces") if endpoint else OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    # Global as well, so any library instrumentation added later lands in the
    # same provider rather than a second, unexported one. None is installed
    # today; the spans are all hand-made.
    trace.set_tracer_provider(provider)
    use_provider(provider)
    logger.info(f"[TRACING] exporting spans as service.name={service_name} to "
                f"{endpoint or os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT') or 'the OTLP default'}")
    return True


def use_provider(provider) -> None:
    """Trace through ``provider`` (tests hand in one with an in-memory exporter)."""
    global _tracer, _provider
    _provider = provider
    _tracer = provider.get_tracer('cfoperator')


def reset() -> None:
    """Back to off. For tests; the running agent never turns tracing off."""
    global _tracer, _provider
    _tracer = None
    _provider = None
    with _open_lock:
        _open_spans.clear()


def active_count() -> int:
    """How many spans are open right now -- in-flight traced work."""
    with _open_lock:
        return len(_open_spans)


def flush(timeout_seconds: float = 5.0) -> bool:
    """Export whatever the batch processor is holding. True when there is
    nothing to export or it all went; never raises."""
    if _provider is None:
        return True
    try:
        return bool(_provider.force_flush(int(timeout_seconds * 1000)))
    except Exception as e:
        logger.warning(f"[TRACING] flush failed: {type(e).__name__}")
        return False


def interrupt_open_spans(error_type: str = 'Interrupted') -> int:
    """End every span still open as ERROR with ``error.type`` (CFOP-394).

    For process shutdown: a span that is never ended is never exported, so a
    sweep cut short by a deploy left a trace with no root. Ending it says what
    happened. The thread that opened it may still try to end it later; the
    SDK ignores a second end. Returns how many were ended.
    """
    if _tracer is None:
        return 0
    from opentelemetry.trace import Status, StatusCode
    with _open_lock:
        spans = list(_open_spans)
        _open_spans.clear()
    ended = 0
    for s in spans:
        if s.is_recording():
            s.set_attribute('error.type', error_type)
            s.set_status(Status(StatusCode.ERROR, error_type))
            s.end()
            ended += 1
    return ended


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
def span(name: str, attributes: Optional[Mapping[str, Any]] = None,
         parent: Optional[Mapping[str, str]] = None) -> Iterator[Any]:
    """A span around the block, or a no-op while tracing is off.

    ``parent`` is a W3C carrier (``traceparent`` and friends, as
    ``carrier_from`` returns them): the span then continues that trace as a
    child of the remote span instead of starting one of its own (CFOP-317).

    An exception leaving the block marks the span ERROR with ``error.type``
    set to its class and is re-raised; its message is not recorded (see the
    module docstring).
    """
    if _tracer is None:
        yield _NOOP
        return
    from opentelemetry.trace import Status, StatusCode
    context = None
    if parent:
        from opentelemetry.propagate import extract
        context = extract(parent)
    s = None
    try:
        with _tracer.start_as_current_span(
                name[:ATTR_LIMIT], context=context, attributes=_clean(attributes),
                record_exception=False, set_status_on_exception=False) as s:
            with _open_lock:
                _open_spans.add(s)
            try:
                yield s
            except Exception as e:
                if s.is_recording():
                    s.set_attribute('error.type', type(e).__name__)
                    s.set_status(Status(StatusCode.ERROR, type(e).__name__))
                raise
    finally:
        # Only once the context has ended (and so exported) the span: a
        # shutdown waiting on active_count() must not see 0 before then.
        if s is not None:
            with _open_lock:
                _open_spans.discard(s)


def inject(headers: Dict[str, str]) -> Dict[str, str]:
    """Put the current span's W3C ``traceparent`` on outbound headers, so the
    process on the other side continues this trace (CFOP-317). Nothing is
    added while tracing is off or no span is open. Returns the same dict."""
    if _tracer is not None:
        from opentelemetry.propagate import inject as _inject
        _inject(headers)
    return headers


def carrier_from(headers: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """The W3C trace headers out of an inbound request, as a plain dict that
    a later ``span(parent=...)`` can continue -- or None when there are none.
    No SDK involved, so a route may call it whether tracing is on or off."""
    out: Dict[str, str] = {}
    for key, value in headers.items():
        if str(key).lower() in _CARRIER_HEADERS and value:
            out[str(key).lower()] = str(value)
    return out or None


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
    name = details.get('alertname') or labels.get('alertname')
    if not name and alert.get('source') == SWEEP_SOURCE:
        # A sweep finding is model output, not a rule, so it has no
        # alertname (CFOP-392). A fixed name lets the spans be grouped; the
        # fingerprint below tells one finding from another.
        name = 'sweep-finding'
    resource = '/'.join(str(alert.get(k)) for k in ('namespace', 'resource_type', 'resource_name')
                        if alert.get(k))
    return {
        'cfop.alert.name': alert_text(name, 64),
        'cfop.alert.severity': alert_text(alert.get('severity') or labels.get('severity'), 32),
        'cfop.alert.source': alert_text(alert.get('source'), 32),
        'cfop.alert.id': alert_text(alert.get('alert_id'), 128),
        'cfop.alert.summary': alert_text(summary),
        'cfop.alert.resource': alert_text(resource, 128),
        'cfop.alert.fingerprint': alert_fingerprint(alert) if summary else None,
    }


SWEEP_SOURCE = 'cfoperator-sweep'


def alert_fingerprint(alert: Mapping[str, Any]) -> str:
    """The alert's identity as event_runtime's dedupe sees it (CFOP-392).

    The alert's own ``fingerprint`` if it has one, else the same digest
    event_runtime's ``Alert.effective_fingerprint()`` computes, so a span
    can be matched to the runtime's dedupe and a recurring sweep finding
    groups across sweeps. A hash, so no text. tests/test_tracing.py holds
    it equal to the runtime's, so the two cannot drift apart.
    """
    if alert.get('fingerprint'):
        return str(alert['fingerprint'])[:16]
    stable = {key: alert.get(key) for key in
              ('source', 'severity', 'summary', 'namespace', 'resource_type', 'resource_name')}
    # The summary alert_attributes shows, including a raw Alertmanager
    # payload's annotations.summary; otherwise two such alerts with different
    # summaries would share a fingerprint (CodeRabbit on #329). A runtime
    # alert has it top-level, so its digest is unchanged.
    stable['summary'] = stable['summary'] or (alert.get('annotations') or {}).get('summary')
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]
