"""SIGTERM ends the agent promptly and leaves no orphaned traces (CFOP-394).

The agent is PID 1 in its container and had no SIGTERM handler, so the signal
did nothing: every deploy waited the full grace period for a SIGKILL, and any
span still open -- a sweep cut short -- was never exported, leaving a trace
Grafana lists as "<root span not yet received>" forever.

These run a real process and send it a real SIGTERM, exporting through a file
so the parent can read what the child sent. PID 1's signal semantics cannot be
reproduced here; what is tested is that a handler exists and does the right
thing once the signal is delivered.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(AGENT_DIR)

CHILD = textwrap.dedent('''
    import json, os, signal, sys, threading, time
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
    from cfshared import tracing
    import importlib
    agent_module = importlib.import_module('agent.agent')   # cwd is the repo root: agent is the package

    out_path, mode, grace = sys.argv[1], sys.argv[2], float(sys.argv[3])

    class FileExporter(SpanExporter):
        def export(self, spans):
            with open(out_path, 'a') as f:
                for s in spans:
                    f.write(json.dumps({'name': s.name, 'status': s.status.status_code.name,
                                        'error_type': (s.attributes or {}).get('error.type')}) + '\\n')
            return SpanExportResult.SUCCESS

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(FileExporter()))
    tracing.use_provider(provider)
    agent_module.install_shutdown_handler(grace)

    def work(name, seconds):
        with tracing.span(name):
            time.sleep(seconds)

    if mode == 'open':
        threading.Thread(target=work, args=('sweep', 60), daemon=True).start()
    elif mode == 'finishing':
        threading.Thread(target=work, args=('investigation', 0.8), daemon=True).start()
    time.sleep(0.3)
    print('READY', flush=True)
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(60)                         # the handler must end the process, not this
    print('STILL ALIVE', flush=True)
''')


def _run(tmp_path, mode, grace):
    out = tmp_path / 'spans.jsonl'
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([AGENT_DIR, REPO_ROOT]),
               CFOP_NO_DOTENV='1')
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, '-c', CHILD, str(out), mode, str(grace)],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=45)
    elapsed = time.monotonic() - t0
    spans = [json.loads(l) for l in out.read_text().splitlines()] if out.exists() else []
    return proc, elapsed, spans


def test_an_idle_agent_exits_promptly_on_sigterm(tmp_path):
    proc, elapsed, spans = _run(tmp_path, 'idle', grace=20)
    assert 'READY' in proc.stdout and 'STILL ALIVE' not in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode == 0
    assert elapsed < 15, f"took {elapsed:.1f}s: an idle agent should not wait out the grace"
    assert spans == []


def test_work_still_open_at_the_deadline_is_exported_as_interrupted(tmp_path):
    """A sweep cut short by a deploy: its root used to be lost, leaving the
    trace without one. Now it is ended and exported, saying why."""
    proc, elapsed, spans = _run(tmp_path, 'open', grace=1)
    assert 'STILL ALIVE' not in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode == 0
    assert spans == [{'name': 'sweep', 'status': 'ERROR', 'error_type': 'Interrupted'}]


def test_work_that_finishes_inside_the_grace_period_finishes(tmp_path):
    """An investigation about to finish is allowed to, and is exported as
    the ordinary span it is -- not cut off."""
    proc, elapsed, spans = _run(tmp_path, 'finishing', grace=10)
    assert 'STILL ALIVE' not in proc.stdout, proc.stdout + proc.stderr
    assert spans == [{'name': 'investigation', 'status': 'UNSET', 'error_type': None}]
    assert elapsed < 15


def test_tracing_off_the_helpers_are_no_ops():
    from cfshared import tracing
    tracing.reset()
    assert tracing.active_count() == 0
    assert tracing.interrupt_open_spans() == 0
    assert tracing.flush() is True


def test_shutdown_grace_comes_from_ooda_shutdown_grace_seconds():
    sys.path.insert(0, AGENT_DIR)
    from agent import CFOperator

    def op(cfg):
        o = CFOperator.__new__(CFOperator)
        o.config = cfg
        return o
    assert op({})._shutdown_grace_seconds() == 20.0
    assert op({'ooda': {'shutdown_grace_seconds': 5}})._shutdown_grace_seconds() == 5.0
    assert op({'ooda': {'shutdown_grace_seconds': -3}})._shutdown_grace_seconds() == 0.0
    assert op({'ooda': {'shutdown_grace_seconds': 'soon'}})._shutdown_grace_seconds() == 20.0
