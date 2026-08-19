"""Hermetic guards for the Helm chart (CFOP-30) — no helm binary needed.

The chart mirrors the docker-compose trial: same images, same config file
shape, same env contract. These tests guard the *class* of regression where
the compose file and the chart drift apart — above all the CFOP-31 defect,
where ALERTMANAGER_URL was configured but the poll source (which registers
only on CFOP_EVENT_RUNTIME_ALERTMANAGER_URL) silently never started.
Template correctness itself (lint, a real install) is chart-ci.yml's job.
"""

import re
from pathlib import Path

import yaml

CHART = Path(__file__).parent / "charts" / "cfoperator"
TEMPLATES = sorted(CHART.glob("templates/*.yaml")) + sorted(CHART.glob("templates/*.tpl"))
TEMPLATE_TEXT = "\n".join(p.read_text() for p in TEMPLATES)


def _compose_env_names(service: str) -> set:
    compose = yaml.safe_load((Path(__file__).parent / "docker-compose.yml").read_text())
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
    compose_cfg = (Path(__file__).parent / "deploy" / "compose" / "config.yaml").read_text()
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


def test_read_only_profile_grants_no_write_verbs():
    """The investigate profile's ClusterRole is the tameness claim in RBAC
    form: get/list/watch only. Write verbs live exclusively inside the
    remediate-profile block."""
    rbac = (CHART / "templates" / "rbac.yaml").read_text()
    investigate_part = rbac.split('{{- if eq .Values.profile "remediate" }}')[0]
    verb_lines = "\n".join(l for l in investigate_part.splitlines() if "verbs:" in l)
    assert verb_lines, "no verbs lines found in the investigate-profile RBAC"
    for verb in ("create", "update", "patch", "delete", "deletecollection", "escalate", "impersonate"):
        assert not re.search(rf"\b{verb}\b", verb_lines), (
            f"read-only ClusterRole grants write verb {verb!r}")
