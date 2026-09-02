"""Guards for the kind demo (CFOP-31, demo/).

These guard the class of regression, not today's bytes: the demo is a chain
of artifacts (fault manifests -> alert rules -> compose wiring -> stub LLM ->
assertions) written in different languages that silently drift apart. Each
test pins one cross-artifact agreement that, broken, produces a demo that
"runs" and shows nothing:

- the pending-pod fault must stay *eligible* for the add-toleration proposer
  (a toleration or node pin in the manifest turns the remediation beat into a
  decline);
- the alert rules must stay scoped to the fault namespace and fast enough for
  the 15-minute budget;
- the compose override must set the env that actually registers the
  Alertmanager poll source (CFOP_EVENT_RUNTIME_ALERTMANAGER_URL — plain
  ALERTMANAGER_URL only feeds the agent's config, a distinction that already
  cost one debugging round);
- the CI stub's canned output must remain parseable by the real triage/status
  parsers, and its embedding dimension must match the pgvector column;
- the executor callback host baked into config-remediate.yaml must be an
  alias the compose override actually creates on the kind network.
"""

from repo_paths import REPO_ROOT
import re
import sys
from pathlib import Path

import yaml

REPO = REPO_ROOT
DEMO = REPO / "demo"

# The agent package uses bare imports internally (see tests.yml: each suite
# runs with its own directory first on sys.path). This is a root-level test,
# so mirror that arrangement locally for the two agent imports below.
sys.path.insert(0, str(REPO / "agent"))

from agent import CFOperator  # noqa: E402
from embedding_service import EMBEDDING_DIMENSION  # noqa: E402


def _manifest(name: str):
    text = (DEMO / "manifests" / name).read_text().replace("__NAME__", "x")
    return list(yaml.safe_load_all(text))


# ── fault manifests ──────────────────────────────────────────────────────────

def test_pending_pod_stays_toleration_proposer_eligible():
    """agent/remediation.py declines pods that already have tolerations or are
    node-pinned; the taint fault exists to demo the *patch* path."""
    (pod,) = _manifest("pending.yaml")
    spec = pod["spec"]
    assert "tolerations" not in spec, "a toleration makes the proposer decline"
    assert "nodeSelector" not in spec and "nodeName" not in spec, \
        "a node pin is the proposer's decline_pinned case"
    assert pod["metadata"]["namespace"] == "demo-faults"


def test_crashloop_pod_actually_crash_loops():
    (pod,) = _manifest("crashloop.yaml")
    assert pod["spec"]["restartPolicy"] == "Always"
    command = " ".join(pod["spec"]["containers"][0]["command"])
    assert "exit 1" in command


def test_oom_pod_has_a_limit_it_can_exceed():
    (pod,) = _manifest("oom.yaml")
    limits = pod["spec"]["containers"][0]["resources"]["limits"]
    assert re.fullmatch(r"\d+Mi", limits["memory"]) and int(limits["memory"][:-2]) <= 64, \
        "OOM fault needs a small absolute Mi limit to die quickly"


# ── alert rules ──────────────────────────────────────────────────────────────

def _rules():
    (doc,) = _manifest("demo-rules.yaml")
    return doc, [r for g in doc["spec"]["groups"] for r in g["rules"]]


def test_demo_rules_are_scoped_and_fast():
    doc, rules = _rules()
    assert len(rules) == 3
    for rule in rules:
        assert 'namespace="demo-faults"' in rule["expr"], \
            f"{rule['alert']} could fire on non-demo workloads"
        value, unit = int(rule["for"][:-1]), rule["for"][-1]
        seconds = value * {"s": 1, "m": 60}[unit]
        assert seconds <= 120, f"{rule['alert']} too slow for a 15-minute demo"
        assert "{{ $labels.pod }}" in rule["annotations"]["summary"], \
            "the agent's probes need the pod named in the trigger"


def test_demo_rules_release_label_matches_the_helm_release():
    """The chart's ruleSelector matches `release: <helm release>`; up.sh
    installs release `kps`. Rename either alone and the rules silently never
    load — no error, just no alerts."""
    doc, _ = _rules()
    assert doc["metadata"]["labels"]["release"] == "kps"
    assert re.search(r"helm upgrade --install kps\b", (DEMO / "up.sh").read_text())


# ── compose wiring ───────────────────────────────────────────────────────────

def _override():
    return yaml.safe_load((DEMO / "docker-compose.demo.yml").read_text())


def test_override_registers_the_alertmanager_poll_source():
    env = _override()["services"]["event-runtime"]["environment"]
    assert env.get("CFOP_EVENT_RUNTIME_ALERTMANAGER_URL", "").startswith("http"), \
        "event_runtime/bootstrap.py only polls when THIS env is set"


def test_override_joins_both_services_to_the_kind_network():
    o = _override()
    assert o["networks"]["kind"]["external"] is True
    for svc in ("agent", "event-runtime"):
        nets = o["services"][svc]["networks"]
        assert "kind" in nets and "default" in nets, \
            f"{svc} needs kind (cluster) AND default (postgres/agent)"
    assert o["services"]["agent"]["environment"]["KUBECONFIG"]


def test_executor_callback_host_is_an_alias_the_override_creates():
    o = _override()
    aliases = o["services"]["agent"]["networks"]["kind"]["aliases"]
    remediate = yaml.safe_load(
        re.sub(r"\$\{[^}]*\}", "x", (DEMO / "config-remediate.yaml").read_text()))
    # _executor_config() reads remediation.executor — a top-level executor:
    # block is silently ignored (the first cut of this file shipped that way,
    # and this test pinned the dead key right along with it).
    assert "executor" not in remediate, "top-level executor: is dead config"
    executor = remediate["remediation"]["executor"]
    host = re.match(r"https?://([^:/]+)", executor["completion_base_url"]).group(1)
    assert host in aliases, \
        f"executor Jobs will call {host}, but the agent's kind-network aliases are {aliases}"


# ── remediate variant config ─────────────────────────────────────────────────

def test_remediate_config_actually_lifts_the_profile_ceiling():
    remediate = yaml.safe_load(
        re.sub(r"\$\{[^}]*\}", "x", (DEMO / "config-remediate.yaml").read_text()))
    assert remediate["profile"] == "remediate", \
        "flags under profile: investigate are ceilinged off — the variant would demo nothing"
    for flag in ("queue_feed", "queue_drain", "queue_reap", "queue_verify"):
        assert remediate["remediation"].get(flag) is True
    ns = remediate["remediation"]["executor"]["namespace"]
    docs = _manifest("executor-setup.yaml")
    assert any(d["kind"] == "Namespace" and d["metadata"]["name"] == ns for d in docs)
    assert any(d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "cfoperator-executor"
               for d in docs), "agent/_build_executor_manifest defaults to this SA name"


# ── CI stub against the real parsers ─────────────────────────────────────────

def test_stub_triage_response_parses_with_the_real_parser():
    import importlib.util
    spec = importlib.util.spec_from_file_location("llm_stub", DEMO / "ci" / "llm_stub.py")
    stub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stub)

    decision = CFOperator._parse_triage_response(stub.TRIAGE_RESPONSE)
    assert decision and decision["action"] == "investigate"

    # _extract_status falls back to 'monitoring' for an unrecognized token, so
    # asserting on its return value alone is a guard that passes while not
    # guarding (first mutation run of this file proved it: "STATUS: watching"
    # sailed through). Check the explicit token too.
    status_lines = [l for l in stub.INVESTIGATION_RESPONSE.splitlines()
                    if l.lower().startswith("status:")]
    assert status_lines, "stub final answer must carry an explicit STATUS line"
    token = status_lines[-1].split(":", 1)[1].strip()
    assert token in {"resolved", "needs_action", "monitoring", "escalate"}, \
        f"STATUS token {token!r} is outside the prompt's vocabulary"
    assert CFOperator._extract_status(stub.INVESTIGATION_RESPONSE) == "monitoring"

    assert stub.DIM == EMBEDDING_DIMENSION, \
        "stub embeddings would fail the pgvector insert"


def test_console_renders_the_persisted_citations():
    """The memory beat's console half: findings.similar_past is persisted by
    agent/_act precisely so the investigations drawer can show it. If the
    console stops reading that key, the demo shows citations only in raw
    JSON — technically present, visibly absent."""
    html = (REPO / "ui" / "investigations.html").read_text()
    assert "similar_past" in html


# ── the demo ends at the cockpit ─────────────────────────────────────────────

def test_up_sh_hands_off_with_the_shipped_attach_verb():
    """Same contract as test_cockpit_attach_contract: the verb printed must be
    the one cfassist-go registers, read from the Go source."""
    go = (REPO / "cfassist-go" / "internal" / "cfoperator" / "briefing.go").read_text()
    verb = re.search(r'AttachVerb\s*=\s*"([a-z]+)"', go).group(1)
    assert f"cfassist {verb} " in (DEMO / "up.sh").read_text(), \
        "up.sh must end at the cockpit hand-off line"
