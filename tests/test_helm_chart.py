"""Hermetic guards for the Helm chart (CFOP-30) — no helm binary needed.

The chart mirrors the docker-compose trial: same images, same config file
shape, same env contract. These tests guard the *class* of regression where
the compose file and the chart drift apart — above all the CFOP-31 defect,
where ALERTMANAGER_URL was configured but the poll source (which registers
only on CFOP_EVENT_RUNTIME_ALERTMANAGER_URL) silently never started.
Template correctness itself (lint, a real install) is chart-ci.yml's job.
"""

from repo_paths import REPO_ROOT
import re
from pathlib import Path

import yaml

CHART = REPO_ROOT / "charts" / "cfoperator"
TEMPLATES = sorted(CHART.glob("templates/*.yaml")) + sorted(CHART.glob("templates/*.tpl"))
TEMPLATE_TEXT = "\n".join(p.read_text() for p in TEMPLATES)


def _compose_env_names(service: str) -> set:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    return set(compose["services"][service].get("environment", {}))


def test_chart_exists_with_core_templates():
    names = {p.name for p in TEMPLATES}
    for required in ("agent.yaml", "event-runtime.yaml", "configmap.yaml",
                     "secrets.yaml", "rbac.yaml", "bootstrap-job.yaml"):
        assert required in names, f"chart is missing templates/{required}"


def test_chart_carries_every_compose_env_name():
    """Every env name the compose trial sets on agent/event-runtime must
    appear somewhere in the chart templates. A name that vanishes here is a
    feature that silently stops working on the k8s path only."""
    for service in ("agent", "event-runtime"):
        for name in _compose_env_names(service):
            assert name in TEMPLATE_TEXT, (
                f"compose {service} sets {name} but no chart template mentions it "
                "— the k8s install would silently lose that wiring")


def test_alertmanager_poll_env_wired_on_event_runtime():
    """The CFOP-31 defect class, pinned specifically: the poll source
    registers only on CFOP_EVENT_RUNTIME_ALERTMANAGER_URL."""
    er = (CHART / "templates" / "event-runtime.yaml").read_text()
    assert "CFOP_EVENT_RUNTIME_ALERTMANAGER_URL" in er


def test_configmap_mirrors_compose_config_placeholders():
    """Every ${VAR} the compose starter config env-fills must be filled by the
    chart ConfigMap too — a key present in one and not the other is config
    drift between the two install paths."""
    compose_cfg = (REPO_ROOT / "deploy" / "compose" / "config.yaml").read_text()
    chart_cm = (CHART / "templates" / "configmap.yaml").read_text()
    for var in sorted(set(re.findall(r"\$\{([A-Z_]+)\}", compose_cfg))):
        assert f"${{{var}}}" in chart_cm, (
            f"deploy/compose/config.yaml fills ${{{var}}} but the chart ConfigMap does not")


def test_bootstrap_job_is_db_only():
    """The chart provides session secret + API token via Secrets; a bootstrap
    Job without CFOP_BOOTSTRAP_DB_ONLY would revoke + remint a DB token nobody
    reads on every helm upgrade."""
    job = (CHART / "templates" / "bootstrap-job.yaml").read_text()
    assert "CFOP_BOOTSTRAP_DB_ONLY" in job


def test_executor_secret_keys_match_manifest_builder():
    """_build_executor_manifest reads GITHUB_TOKEN / ANTHROPIC_API_KEY /
    CFOP_COMPLETION_SHARED_SECRET from remediation.executor.secrets_name.
    The chart's generated Secret must carry those exact key names, and the
    ConfigMap must point secrets_name at that Secret."""
    secrets_t = (CHART / "templates" / "secrets.yaml").read_text()
    cm = (CHART / "templates" / "configmap.yaml").read_text()
    for key in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "CFOP_COMPLETION_SHARED_SECRET"):
        assert key in secrets_t, f"generated Secret is missing executor key {key}"
    assert 'secrets_name: {{ include "cfoperator.fullname" . }}-generated' in cm


WRITE_VERBS = ("create", "update", "patch", "delete", "deletecollection",
               "escalate", "impersonate")


def unconditional(template: str) -> str:
    """The part of a template that renders for a *default* install.

    Everything inside a ``{{- if }}`` block is dropped, at any nesting depth.
    Used to be a split on the remediate conditional; CFOP-35 added a second
    opt-in block (cockpit.enabled), and a split on one marker would have
    stopped noticing write verbs added after it.
    """
    kept, depth = [], 0
    for line in template.splitlines():
        stripped = line.strip()
        if re.match(r"\{\{-?\s*if\b", stripped):
            depth += 1
            continue
        if re.match(r"\{\{-?\s*end\b", stripped):
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            kept.append(line)
    return "\n".join(kept)


def test_the_conditional_stripper_is_not_vacuous():
    """A helper that quietly kept everything would make the guard below pass on
    any chart at all."""
    doc = ('always: here\n'
           '{{- if .Values.something }}\n'
           '    verbs: [create]\n'
           '{{- end }}\n'
           '    verbs: [get]\n')
    kept = unconditional(doc)
    assert "create" not in kept, "conditional content was not stripped"
    assert "get" in kept and "always: here" in kept, "unconditional content was lost"


def test_a_default_install_grants_no_write_verbs():
    """The investigate profile's RBAC is the tameness claim in RBAC form:
    get/list/watch only. Every write verb has to sit behind an explicit opt-in
    — the remediate profile, or cockpit.enabled."""
    rbac = (CHART / "templates" / "rbac.yaml").read_text()
    verb_lines = "\n".join(l for l in unconditional(rbac).splitlines() if "verbs:" in l)
    assert verb_lines, "no verbs lines found in the default-install RBAC"
    for verb in WRITE_VERBS:
        assert not re.search(rf"\b{verb}\b", verb_lines), (
            f"a default install's RBAC grants write verb {verb!r}")


def _cockpit_block(rbac: str) -> str:
    """The cockpit RBAC, i.e. what `cockpit.enabled` turns on."""
    marker = "{{- if .Values.cockpit.enabled }}"
    assert marker in rbac, "the cockpit RBAC is no longer gated on cockpit.enabled"
    return rbac.split(marker, 1)[1]


def test_the_cockpit_pod_identity_is_read_only():
    """The pod an operator sits inside runs as cfoperator-cockpit, which mirrors
    the deep-investigation worker: no exec, no write, no secrets. A cockpit is a
    place to look from — the write path stays the PR/console gate even from a
    pod on the affected node."""
    block = _cockpit_block((CHART / "templates" / "rbac.yaml").read_text())
    cluster_role = block.split("kind: ClusterRole", 1)[1].split("---", 1)[0]

    verb_lines = "\n".join(l for l in cluster_role.splitlines() if "verbs:" in l)
    assert verb_lines, "the cockpit ClusterRole has no rules"
    for verb in WRITE_VERBS:
        assert not re.search(rf"\b{verb}\b", verb_lines), (
            f"the cockpit service account may {verb!r} — it must be read-only")
    for forbidden in ("pods/exec", "pods/attach", "secrets", "configmaps"):
        assert forbidden not in cluster_role, (
            f"the cockpit service account can reach {forbidden}")


def test_the_agents_cockpit_grant_can_create_secrets_but_never_read_them():
    """The token Secret is created by the agent and deleted by ownership GC, so
    `create` is the whole grant. `get` would turn the launcher into a way to
    read every secret in the namespace; `delete` would let it remove them."""
    block = _cockpit_block((CHART / "templates" / "rbac.yaml").read_text())
    lines = [l.strip() for l in block.splitlines()]
    idx = [i for i, l in enumerate(lines) if l == "resources: [secrets]"]
    assert idx, "the cockpit spawn Role no longer names secrets at all"
    for i in idx:
        verbs = next(l for l in lines[i:] if l.startswith("verbs:"))
        assert verbs == "verbs: [create]", (
            f"the agent's secret grant is {verbs!r}; it may only create")
