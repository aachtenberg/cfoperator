"""The investigation-loop sim (CFOP-570) scores models on fault scenarios as
well as the healthy one, and can render the prompt production sends.

Guarded here: the harness's copy of the production template has the same
static lines as agent.py's, both ways; the healthy Loki scenario every
committed result was measured on does not move; every canned tool is a real
tool; and a model that always answers "resolved" no longer scores perfectly.
"""

from repo_paths import REPO_ROOT
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys

sys.path.insert(0, str(REPO_ROOT))
sys.path.append(os.path.join(str(REPO_ROOT), "agent"))

_spec = importlib.util.spec_from_file_location(
    "empty_response_sim", REPO_ROOT / "benchmarks" / "empty_response_sim.py")
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)

# sha256 of the July prompt + canned results behind every committed
# empty_response_sim_*.jsonl, taken from the harness before CFOP-570.
# Changing either makes old and new runs incomparable; add a scenario instead.
LEGACY_FINGERPRINT = "7ee65b1e645e14d795370386e69053c5180ca4b4d8e5c8ea09c104a195cb5348"


def _static_lines(template_source):
    """Template lines with no interpolation: the glue a copy can drift on."""
    return {line.strip() for line in template_source.splitlines()
            if line.strip() and '{' not in line and '}' not in line}


def _agent_template():
    src = (REPO_ROOT / "agent" / "agent.py").read_text()
    m = re.search(r'system_prompt = f"""(You are CFOperator investigating an infrastructure '
                  r'alert\..*?)"""', src, re.S)
    assert m, "agent.py's investigation prompt moved; update this guard"
    return m.group(1)


def _harness_template():
    src = inspect.getsource(sim.production_prompt)
    m = re.search(r'return f"""(.*?)"""', src, re.S)
    assert m
    return m.group(1)


def test_the_production_prompt_matches_agent_py_line_for_line():
    agent_lines = _static_lines(_agent_template())
    harness_lines = _static_lines(_harness_template())
    assert agent_lines, "no static lines found in agent.py's template"
    assert harness_lines == agent_lines, (
        f"only in agent.py: {sorted(agent_lines - harness_lines)}; "
        f"only in the sim: {sorted(harness_lines - agent_lines)}")


def test_the_production_prompt_renders_with_the_fix_contract():
    sc = sim.SCENARIOS['oom-killed']
    prompt = sim.production_prompt(sc['trigger'], sc['alert_info'], sc['similar_text'])
    assert '<<< DATA START >>> trigger' in prompt
    assert '"targets"' in prompt and '"observed"' in prompt


def test_the_healthy_loki_scenario_is_unchanged():
    blob = sim.SYSTEM_PROMPT + sim.USER_MESSAGE + json.dumps(sim.CANNED, sort_keys=True)
    assert hashlib.sha256(blob.encode()).hexdigest() == LEGACY_FINGERPRINT


def test_every_canned_tool_is_a_real_tool():
    with open(REPO_ROOT / "benchmarks" / "investigation_tool_schemas.json") as f:
        names = {t['function']['name'] for t in json.load(f)}
    for scenario_name, sc in sim.SCENARIOS.items():
        unknown = set(sc['canned']) - names
        assert not unknown, f"{scenario_name}: {sorted(unknown)} would silently get the default"


def test_expected_statuses_are_ones_the_parser_can_return():
    parseable = {'resolved', 'needs_action', 'monitoring', 'escalated'}
    for scenario_name, sc in sim.SCENARIOS.items():
        assert sc['expected'] and sc['expected'] <= parseable, scenario_name
        assert sc['evidence'] and all(sc['evidence']), scenario_name


def test_always_resolved_no_longer_scores_perfectly():
    answer = "Everything looks fine.\nSTATUS: resolved\nRECOMMENDATION: No action needed"
    scores = [sim.score_answer(answer, sc)['status_ok'] for sc in sim.SCENARIOS.values()]
    assert any(scores) and not all(scores)


def test_a_right_answer_scores_ok_and_cites_the_evidence():
    answer = ("The container was OOMKilled (exit 137) at its 512Mi limit.\n"
              "STATUS: needs_action\nRECOMMENDATION: raise limits.memory to 1Gi")
    s = sim.score_answer(answer, sim.SCENARIOS['oom-killed'])
    assert s['status_ok'] and s['cites_evidence'] and not s['tool_markup_in_answer']


def test_tool_calls_written_as_text_are_flagged():
    for leaked in ('<tool_call>\n<function=loki_query>', '<tool_call>loki_query<arg_key>q</arg_key>'):
        assert sim.score_answer(leaked, sim.SCENARIOS['loki-healthy'])['tool_markup_in_answer']
