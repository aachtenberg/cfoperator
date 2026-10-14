"""Tracing is opt-in, and off means absent (CFOP-316).

The acceptance line is "no exporter, no network calls, no OTel import cost
when disabled" -- the no-phone-home promise. That holds only while exactly one
module names opentelemetry and imports it lazily, so the guards here are about
the *class* of regression: any new module importing the SDK at top level, or
any import path that loads it in a process that never turned tracing on.

The span-content rules (no prompts, no completions, no tool text) are held in
agent/test_llm_tracing.py, through the real tool loop.
"""

import os
import re
import subprocess
import sys

import pytest

from repo_paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))

from cfshared import config as shared_config  # noqa: E402
from cfshared import tracing  # noqa: E402

FIXTURE = REPO_ROOT / 'tests' / 'fixtures' / 'production_shaped_config.yaml'
_OTEL_IMPORT = re.compile(r'^\s*(?:import|from)\s+opentelemetry\b', re.MULTILINE)
_SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.claude'}


@pytest.fixture(autouse=True)
def _tracing_off():
    tracing.reset()
    yield
    tracing.reset()


def test_tracing_is_off_in_the_merged_default():
    """Through load_config, not a hand-built dict: the merge is what every
    consumer sees, and the production-shaped config names no tracing block."""
    cfg = shared_config.load_config(str(FIXTURE))
    assert cfg['observability']['tracing'] == {'enabled': False}
    assert tracing.init_tracing(cfg) is False
    assert not tracing.enabled()


def test_only_the_tracing_module_imports_opentelemetry():
    """One module names the SDK. A second one importing it at top level
    would load it in every process, enabled or not."""
    offenders = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, REPO_ROOT)
            if rel == os.path.join('cfshared', 'tracing.py') or name.startswith('test_'):
                continue
            with open(path, encoding='utf-8', errors='replace') as f:
                if _OTEL_IMPORT.search(f.read()):
                    offenders.append(rel)
    assert offenders == [], (
        f"{offenders} import opentelemetry; only cfshared/tracing.py may, and "
        f"only inside init_tracing(), so a disabled process never loads it")


def test_tracing_module_imports_the_sdk_only_inside_functions():
    """The lazy half of the rule, inside the one allowed module."""
    src = (REPO_ROOT / 'cfshared' / 'tracing.py').read_text()
    top_level = [l for l in src.splitlines()
                 if re.match(r'(?:import|from)\s+opentelemetry\b', l)]
    assert top_level == []


def test_a_disabled_agent_process_never_loads_opentelemetry():
    """The whole agent and console import graph, then a span and an annotate
    with tracing off. Nothing may pull the SDK in -- not our code, and not a
    dependency that would quietly make the promise false."""
    code = (
        "import sys\n"
        "import agent, web_server\n"
        "from cfshared import tracing\n"
        "assert tracing.init_tracing({'observability': {'tracing': {'enabled': False}}}) is False\n"
        "with tracing.span('x', {'a': 1}) as s:\n"
        "    s.set_attribute('b', 2)\n"
        "    tracing.annotate({'c': 3})\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('opentelemetry'))\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    env = dict(os.environ,
               PYTHONPATH=os.pathsep.join([str(REPO_ROOT / 'agent'), str(REPO_ROOT)]))
    proc = subprocess.run([sys.executable, '-c', code], cwd=str(REPO_ROOT), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"opentelemetry loaded with tracing disabled: {proc.stdout.strip()} {proc.stderr[-2000:]}")


def test_disabled_span_is_a_noop_that_still_propagates_exceptions():
    with pytest.raises(ValueError):
        with tracing.span('x') as s:
            assert s.is_recording() is False
            raise ValueError('boom')


def test_enabled_without_the_sdk_runs_untraced(monkeypatch):
    """A config that asks for tracing on an image without the SDK warns and
    carries on; it must not take the agent down."""
    import builtins
    real_import = builtins.__import__

    def no_otel(name, *args, **kwargs):
        if name.startswith('opentelemetry'):
            raise ImportError(f'No module named {name!r}')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_otel)
    assert tracing.init_tracing({'observability': {'tracing': {'enabled': True}}}) is False
    assert not tracing.enabled()


# --- alert text --------------------------------------------------------------

def test_alert_text_is_redacted_then_clipped():
    summary = ('DB auth failing: postgres://cfop:hunter2@db:5432/x '
               'password=hunter2 ' + 'x' * 500)
    out = tracing.alert_text(summary)
    assert 'hunter2' not in out
    assert len(out) <= tracing.ALERT_TEXT_LIMIT + 1   # + the ellipsis


def test_alert_attributes_cover_both_alert_shapes():
    """event_runtime's flat Alert and a raw Alertmanager payload both name
    the alert; both go through alert_text."""
    flat = tracing.alert_attributes({
        'summary': 'token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked',
        'severity': 'critical', 'source': 'alertmanager', 'alert_id': 'a1',
        'details': {'alertname': 'SecretLeak'}})
    assert flat['cfop.alert.name'] == 'SecretLeak'
    assert flat['cfop.alert.severity'] == 'critical'
    assert 'ghp_abcdefghijklmnopqrstuvwxyz0123456789' not in flat['cfop.alert.summary']

    raw = tracing.alert_attributes({
        'labels': {'alertname': 'PodNotReady', 'severity': 'warning'},
        'annotations': {'summary': 'Pod foo not ready'}})
    assert raw['cfop.alert.name'] == 'PodNotReady'
    assert raw['cfop.alert.severity'] == 'warning'
    assert raw['cfop.alert.summary'] == 'Pod foo not ready'
    assert raw['cfop.alert.id'] is None   # dropped by span(), never sent as None


def test_sweep_findings_get_a_name_a_fingerprint_and_a_resource():
    """CFOP-392: 8 of 10 triage spans had no name -- all sweep findings,
    which have no alertname by nature."""
    finding = {'source': 'cfoperator-sweep', 'severity': 'warning',
               'summary': 'Pod web-7f9 restarted 4 times in 10m',
               'namespace': 'apps', 'resource_type': 'deployment', 'resource_name': 'web',
               'details': {'category': None}, 'fingerprint': None, 'alert_id': 'a1'}
    attrs = tracing.alert_attributes(finding)
    assert attrs['cfop.alert.name'] == 'sweep-finding'
    assert attrs['cfop.alert.resource'] == 'apps/deployment/web'
    assert len(attrs['cfop.alert.fingerprint']) == 16
    # The same finding next sweep is the same fingerprint; a different one is not.
    assert tracing.alert_attributes(dict(finding, alert_id='a2'))['cfop.alert.fingerprint'] == \
        attrs['cfop.alert.fingerprint']
    assert tracing.alert_attributes(dict(finding, summary='Disk full on node-a'))[
        'cfop.alert.fingerprint'] != attrs['cfop.alert.fingerprint']
    # A real alert keeps its own name.
    am = tracing.alert_attributes({'source': 'alertmanager', 'summary': 'x',
                                   'details': {'alertname': 'KubePodCrashLooping'}})
    assert am['cfop.alert.name'] == 'KubePodCrashLooping'
    assert 'cfop.alert.resource' not in {k for k, v in am.items() if v is not None}


def test_fingerprint_matches_event_runtimes_own():
    """The span's fingerprint is only useful if it is the one the runtime
    deduped on. Computed both ways, over the wire shape triage receives."""
    from event_runtime.models import Alert

    for payload in (
        {'source': 'cfoperator-sweep', 'severity': 'warning', 'summary': 'Pod web restarted',
         'namespace': 'apps', 'resource_type': 'deployment', 'resource_name': 'web'},
        {'source': 'cfoperator-sweep', 'severity': 'critical', 'summary': 'Disk 95% on node-a'},
        {'source': 'alertmanager', 'severity': 'info', 'summary': 'x', 'fingerprint': 'abc123'},
    ):
        alert = Alert.from_dict(payload)
        wire = alert.to_dict()                     # what /v1/triage receives
        assert tracing.alert_fingerprint(wire) == alert.effective_fingerprint()[:16], payload


# --- enabled -----------------------------------------------------------------

def test_enabled_span_records_error_type_but_not_the_message():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)

    with pytest.raises(RuntimeError):
        with tracing.span('outer', {'keep': 'yes', 'drop': None, 'long': 'y' * 1000}):
            tracing.annotate({'added': 1})
            raise RuntimeError('provider said: <the whole prompt>')

    (s,) = exporter.get_finished_spans()
    assert s.name == 'outer'
    assert s.attributes['keep'] == 'yes'
    assert 'drop' not in s.attributes
    assert len(s.attributes['long']) <= tracing.ATTR_LIMIT + 1
    assert s.attributes['added'] == 1
    assert s.attributes['error.type'] == 'RuntimeError'
    assert s.status.status_code.name == 'ERROR'
    assert 'prompt' not in (s.status.description or '')
    assert list(s.events) == []
