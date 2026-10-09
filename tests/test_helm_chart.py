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


def rendered_docs(template: str) -> list:
    """Every document of a template as YAML, Helm directives stripped.

    No helm binary here (see the module docstring), and the templates are plain
    YAML once the ``{{ }}`` is gone: comment blocks go; a line that is nothing
    but an expression goes (``if``/``end`` control lines -- so every opt-in
    block is KEPT and checked -- and ``include``/``toYaml`` lines, which render
    a labels or resources block, never RBAC); an expression inside a line
    becomes the scalar ``PLACEHOLDER``, which the RBAC guard refuses to see in
    a resources list.
    """
    text = re.sub(r"\{\{/\*.*?\*/\}\}", "", template, flags=re.DOTALL)
    kept = []
    for line in text.splitlines():
        if re.fullmatch(r"\{\{.*\}\}", line.strip()):
            continue
        kept.append(re.sub(r"\{\{.*?\}\}", "PLACEHOLDER", line))
    return [d for d in yaml.safe_load_all("\n".join(kept)) if d]


def rendered_chart_docs() -> list:
    """Every document of every chart template, so a ClusterRole added to any
    template, not only rbac.yaml, is seen (CodeRabbit on #313)."""
    return [doc for template in sorted(CHART.glob("templates/*.yaml"))
            for doc in rendered_docs(template.read_text())]


def test_the_rbac_renderer_is_not_vacuous():
    """A renderer that lost the ClusterRoles, or their rules, would make the
    guard below pass on any chart at all."""
    docs = rendered_chart_docs()
    cluster_roles = [d for d in docs if d.get("kind") == "ClusterRole"]
    assert len(cluster_roles) >= 2, [d.get("kind") for d in docs]  # -read, -cockpit-readonly
    for role in cluster_roles:
        # every rule names resources, or non-resource URLs (/metrics, /healthz)
        assert role["rules"] and all(
            r.get("resources") or r.get("nonResourceURLs") for r in role["rules"]), role
    # and it reads the opt-in blocks too: the cockpit-spawn Role is behind cockpit.enabled
    assert any(d.get("kind") == "Role" and
               any("secrets" in r.get("resources", []) for r in d["rules"]) for d in docs)


#: Expression-only lines an RBAC document (ClusterRole or Role) may carry:
#: ``if``/``end`` for the opt-in blocks, and the chart's labels block. Anything
#: else would render rules the guards never see: a ``toYaml`` or an ``include``
#: inside ``rules``, and ``else`` too -- the renderer keeps both branches, so an
#: ``if``/``else`` that sets the same key twice parses as one mapping in which
#: the loader keeps the last value and a forbidden first branch goes unseen.
#: Use two ``if`` blocks instead.
_RBAC_EXPRESSIONS_ALLOWED = (
    r"\{\{-?\s*(if|end)\b.*\}\}",
    r"\{\{\s*include \"cfoperator\.labels\" \. \| indent 4 \}\}",
)


def test_rbac_rules_are_spelled_out():
    """The renderer drops expression-only lines, so a ClusterRole or Role whose
    rules came from ``{{ toYaml .Values.x }}`` would parse as only its literal
    rules and the guards below would pass on whatever Helm rendered (CodeRabbit
    and claude-review on #313). Every RBAC document therefore has to carry
    nothing but control lines and the labels block as standalone expressions;
    an inline expression is caught by the PLACEHOLDER check instead."""
    seen = set()
    for template in sorted(CHART.glob("templates/*.yaml")):
        text = re.sub(r"\{\{/\*.*?\*/\}\}", "", template.read_text(), flags=re.DOTALL)
        for raw_doc in re.split(r"^---\s*$", text, flags=re.MULTILINE):
            # Identify the document by its parsed kind, not by the spelling of
            # the line: `kind: ClusterRole  # comment` is one too (CodeRabbit).
            kinds = {d.get("kind") for d in rendered_docs(raw_doc)} & {"ClusterRole", "Role"}
            if not kinds:
                continue
            seen |= kinds
            for line in raw_doc.splitlines():
                stripped = line.strip()
                if not re.fullmatch(r"\{\{.*\}\}", stripped):
                    continue
                assert any(re.fullmatch(pat, stripped) for pat in _RBAC_EXPRESSIONS_ALLOWED), (
                    f"{template.name}: a {'/'.join(sorted(kinds))} carries the standalone "
                    f"expression {stripped!r}; the RBAC guards cannot see what it renders, "
                    "so spell the rules out (CFOP-312)")
    assert seen == {"ClusterRole", "Role"}, f"saw only {seen} -- the check would guard too little"


def test_cluster_roles_never_grant_secrets_access():
    """CFOP-312: no ClusterRole in the chart may name secrets, configmaps or a
    wildcard in any rule's resources -- whatever the verbs, whichever opt-in
    block or template it sits in. Cluster-wide secrets read is exactly the
    latent exposure docs/DEPLOYMENT.md "Agent secrets read" describes on the
    homelab deploy, and the chart's claim is that it never had it. A resource
    the guard cannot read (a templated value) is refused too, rather than
    waved through. Roles are namespaced and out of scope here; the
    cockpit-spawn Role's create-only grant has its own test above."""
    cluster_roles = [d for d in rendered_chart_docs() if d.get("kind") == "ClusterRole"]
    assert cluster_roles, "no ClusterRole rendered -- the guard would check nothing"
    for role in cluster_roles:
        name = role["metadata"]["name"]
        assert "aggregationRule" not in role, (
            f"ClusterRole {name} aggregates other roles' rules -- the guard cannot "
            "see what it ends up granting (CFOP-312)")
        for rule in role["rules"]:
            resources = rule.get("resources", [])
            assert isinstance(resources, list), (
                f"ClusterRole {name} has an opaque resources value in {rule!r}")
            assert not any("PLACEHOLDER" in str(r) for r in resources), (
                f"ClusterRole {name} names a templated resource in {rule!r} -- "
                "the guard cannot see through it; spell the resource out")
            resources = [str(r).lower().split("/", 1)[0] for r in resources]
            for forbidden in ("secrets", "configmaps", "*"):
                assert forbidden not in resources, (
                    f"ClusterRole {name} grants {forbidden!r} cluster-wide via "
                    f"{rule!r} -- never, under any profile (CFOP-312)")


def test_bindings_point_only_at_the_charts_own_roles():
    """The guard above reads the chart's ClusterRoles; a binding to a built-in
    role (`edit`, `admin`, `cluster-admin`) or to anything else the chart does
    not define would grant secrets without a `secrets` rule anywhere in the
    templates (claude-review on #313). Every binding's roleRef must name a
    role rendered from the chart, of the kind it claims."""
    docs = rendered_chart_docs()
    own = {(d["kind"], d["metadata"]["name"]) for d in docs if d.get("kind") in ("ClusterRole", "Role")}
    bindings = [d for d in docs if d.get("kind") in ("ClusterRoleBinding", "RoleBinding")]
    assert bindings, "no binding rendered -- the check would guard nothing"
    for binding in bindings:
        ref = binding["roleRef"]
        assert (ref["kind"], ref["name"]) in own, (
            f"{binding['kind']} {binding['metadata']['name']} binds {ref['kind']} "
            f"{ref['name']!r}, which this chart does not define (CFOP-312)")


def test_the_only_secrets_grant_in_the_chart_is_cockpit_spawns_create():
    """docs/DEPLOYMENT.md says the cockpit-spawn Role's namespaced `create` is
    the only secrets grant anywhere in the chart. The ClusterRole guard above
    does not cover Roles, so this holds the claim for them too (claude-review
    on #313): any Role rule naming secrets is cockpit-spawn's, and create-only."""
    roles = [d for d in rendered_chart_docs() if d.get("kind") == "Role"]
    grants = [(d["metadata"]["name"], rule) for d in roles for rule in d["rules"]
              if "secrets" in [str(r).lower() for r in rule.get("resources", [])]]
    assert grants, "no Role grants secrets at all -- the cockpit token Secret could not be created"
    for name, rule in grants:
        assert name.endswith("-cockpit-spawn"), (
            f"Role {name} grants secrets via {rule!r}; only cockpit-spawn may (CFOP-312)")
        assert [str(v) for v in rule.get("verbs", [])] == ["create"], (
            f"Role {name}'s secrets grant is {rule.get('verbs')!r}; it may only create")
