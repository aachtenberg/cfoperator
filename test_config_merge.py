"""Guards for the shared config loader (`cfshared.config`).

CFOP-26 replaced "a present config file bypasses the defaults wholesale" with
"the file is merged over a complete default schema". That is a change to how
*every* deployment resolves its settings, including the production one, so the
guards here are about the class of regression rather than today's values:

  1. A fully-specified config must survive the merge unchanged. If merging
     defaults underneath `config.yaml.example` alters even one leaf, some
     existing deploy is about to behave differently for reasons nobody wrote
     down. This is checked against a local re-implementation of the *old*
     loader, so the test keeps meaning as the example file evolves.

  2. Defaults must be inert. A default that guesses an endpoint turns "the
     operator omitted this section" into "the operator pointed us at a host
     that does not resolve". Omitted sections must stay disabled.

  3. `profile:` is a ceiling, and an absent `profile:` key must not retroactively
     clamp a config written before profiles existed.
"""

from __future__ import annotations

import os
import textwrap

import pytest
import yaml

from cfshared import config as cfg


REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
EXAMPLE_CONFIG = os.path.join(REPO_ROOT, "config.yaml.example")

#: The 319-line `config.yaml.example` as it stood immediately before CFOP-26,
#: kept as a fixture rather than pointed at the live example — the example is
#: now the minimal getting-started file, and the thing that has to keep working
#: is the *old* format, which no file in the tree would otherwise exercise.
LEGACY_FULL_CONFIG = os.path.join(REPO_ROOT, "tests", "fixtures", "legacy_full_config.yaml")
PRODUCTION_SHAPED_CONFIG = os.path.join(
    REPO_ROOT, "tests", "fixtures", "production_shaped_config.yaml"
)


def _legacy_load(path: str) -> dict:
    """Re-implementation of the pre-CFOP-26 loader.

    This is deliberately a copy rather than an import: the point is to compare
    the new loader against the behaviour that shipped, and that behaviour no
    longer exists in the tree to import.
    """
    with open(path, "r") as handle:
        loaded = yaml.safe_load(handle)
    return cfg.expand_env_vars(loaded)


def _write(tmp_path, text: str) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# 1. The full-size config must be unchanged by the merge.
# ---------------------------------------------------------------------------


def test_fully_specified_config_is_unchanged_by_the_merge():
    """Every key the old loader produced must survive with the same value.

    The merged result may gain keys (that is the feature). It must never
    *change* one, because a changed leaf is a silent behaviour change for a
    deployment that already works — and the deployment that already works
    auto-deploys on merge to main, so there is no window to notice in.
    """
    legacy = _legacy_load(LEGACY_FULL_CONFIG)
    merged = cfg.load_config(LEGACY_FULL_CONFIG)

    diffs = list(_leaf_diffs(legacy, merged))
    assert not diffs, "merge altered values the config file had already set:\n" + "\n".join(diffs)


def test_production_shaped_config_is_unchanged_by_the_merge():
    """The guard above compares against the pre-change *example* file, which is
    not what runs. This one uses a redacted clone of the live ConfigMap.

    The distinction matters because production's shape is defined by what it
    OMITS — no `profile:`, no `ooda.noise`, no `ooda.sweep.max_iterations`, no
    `chat.max_tool_*` — and those omissions are precisely where a default can
    disagree with the call-site fallback that has been serving them.
    """
    legacy = _legacy_load(PRODUCTION_SHAPED_CONFIG)
    merged = cfg.load_config(PRODUCTION_SHAPED_CONFIG)

    diffs = list(_leaf_diffs(legacy, merged))
    assert not diffs, "merge altered live-deployment values:\n" + "\n".join(diffs)


def test_production_shaped_config_keeps_remediation_armed():
    """Absent `profile:` must stay unprofiled.

    Defaulting it to `investigate` would clamp these three to False on the next
    merge to main — remediation disarmed by a deploy nobody flagged.
    """
    merged = cfg.load_config(PRODUCTION_SHAPED_CONFIG)
    remediation = merged["remediation"]
    assert merged.get("profile") is None
    assert remediation["enabled"] is True
    assert remediation["open_prs"] is True
    assert remediation["deep_open_prs"] is True


# The keys production does not set, paired with the literal each call site
# falls back to today. If a default here drifts from its call site, the
# behaviour of the live deployment changes silently on the next deploy —
# which is exactly what this whole merge is meant to make impossible.
PRODUCTION_OMITTED_DEFAULTS = [
    # agent.py _deep_system_sweep / _restart_finding_is_noise / _recovered_and_healthy
    (("ooda", "noise", "enabled"), True),
    (("ooda", "noise", "recovered_restart_stable_seconds"), 600),
    (("ooda", "noise", "recovered_restart_max_per_day"), 6),
    # agent.py _get_sweep_max_iterations -> `return 12`
    (("ooda", "sweep", "max_iterations"), 12),
    # agent.py _get_max_tool_iterations -> `.get('max_tool_iterations', 10)`
    (("chat", "max_tool_iterations"), 10),
]


@pytest.mark.parametrize("path,expected", PRODUCTION_OMITTED_DEFAULTS)
def test_defaults_for_production_omitted_keys_match_their_call_sites(path, expected):
    merged = cfg.load_config(PRODUCTION_SHAPED_CONFIG)

    raw = _legacy_load(PRODUCTION_SHAPED_CONFIG)
    node = raw
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    assert node in ({}, None), (
        f"{'.'.join(path)} is now SET in the production-shaped fixture, so this "
        "test no longer proves anything about defaults — re-derive the list"
    )

    node = merged
    for key in path:
        node = node[key]
    assert node == expected, (
        f"default for {'.'.join(path)} is {node!r} but the call site falls back "
        f"to {expected!r}; production omits this key, so the two must agree or "
        "the next deploy changes behaviour silently"
    )


def test_the_legacy_config_stays_unprofiled():
    """It has no `profile:` key, so its remediation flags must be untouched."""
    merged = cfg.load_config(LEGACY_FULL_CONFIG)
    assert merged["profile"] is None


# Substrings that mark a config key as carrying a credential. Deliberately
# broad: a false positive costs one unreadable diff line, a false negative
# prints a live secret.
#
# `dsn` and `jobstore_url` are here because a connection string embeds its
# password in a single scalar — `event_runtime/scheduler_backends.py` ships a
# `_redact_dsn()` for exactly that reason, so this tree already knows they are
# credential carriers. `private_key` for the same reason in the other
# direction: nothing about the name says "secret", and the value is one.
_SECRET_KEY_HINTS = (
    "api_key",
    "token",
    "password",
    "secret",
    "webhook",
    "dsn",
    "jobstore_url",
    "private_key",
)


def _is_secret_key(key: str) -> bool:
    return any(hint in key.lower() for hint in _SECRET_KEY_HINTS)


def _redact(value, key: str = ""):
    """Structure-preserving copy with credential-shaped leaves masked.

    This assertion output is the whole reason CFOP-44 exists: resolved config
    carries real API keys, database passwords and GitHub tokens, and a failing
    diff used to render them verbatim into a message that people paste into
    issues, PRs and chat.

    Walks lists as well as dicts. `llm.fallback` is a *list* of provider dicts
    and `_leaf_diffs` does not recurse into lists, so the entire list is
    repr'd as one leaf — masking only scalars would have missed the four API
    keys that were the worst of the disclosure.

    Empty values are left visible: `'' -> '<redacted>'` still tells the reader
    which side changed, which is the diagnostic the diff exists to give.
    """
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        # Carry the parent key down. A list of dicts (llm.fallback) redacts on
        # its own keys either way, but a list of *scalars* under a secret name
        # — `tokens: ["ghp_…"]` — has no key of its own, and dropping the
        # parent here would print it.
        return [_redact(item, key) for item in value]
    if key and _is_secret_key(key) and value not in ("", None):
        return "<redacted>"
    return value


def _leaf_diffs(old, new, path: str = ""):
    """Yield human-readable descriptions of leaves that changed value.

    Values are redacted before rendering — see `_redact`.
    """
    if isinstance(old, dict):
        if not isinstance(new, dict):
            yield f"{path or '<root>'}: dict -> {type(new).__name__}"
            return
        for key, old_value in old.items():
            if key not in new:
                yield f"{path}.{key}: dropped by the merge"
                continue
            yield from _leaf_diffs(old_value, new[key], f"{path}.{key}")
        return
    if old != new:
        leaf = path.rsplit(".", 1)[-1]
        yield f"{path}: {_redact(old, leaf)!r} -> {_redact(new, leaf)!r}"


def test_config_diffs_never_render_credential_values():
    """The diff must report *that* a secret changed, never *what* it is.

    Guards the disclosure directly: with a local .env present this helper was
    printing live API keys, a database password and a GitHub token, in a
    message people paste into issues and chat. Sentinels stand in for the real
    thing so this test never needs a credential of its own.
    """
    sentinel = "SENTINEL_MUST_NOT_APPEAR"
    old = {
        "llm": {"fallback": [{"provider": "groq", "api_key": ""}]},
        "database": {"password": ""},
        "git": {"github": {"token": ""}},
        "observability": {"notifications": [{"webhook_url": ""}]},
        # A list of bare scalars under a secret-shaped name: no inner key to
        # match on, so the parent has to carry down.
        "auth": {"tokens": []},
        # A connection string hides its password inside one scalar.
        "scheduler": {"jobstore_url": ""},
        "event_runtime": {"pg": {"dsn": ""}},
    }
    new = {
        "llm": {"fallback": [{"provider": "groq", "api_key": f"gsk_{sentinel}"}]},
        "database": {"password": f"pw_{sentinel}"},
        "git": {"github": {"token": f"ghp_{sentinel}"}},
        "observability": {"notifications": [{"webhook_url": f"https://{sentinel}"}]},
        "auth": {"tokens": [f"ghp_{sentinel}", f"cfop_{sentinel}"]},
        "scheduler": {"jobstore_url": f"postgresql://u:{sentinel}@h/db"},
        "event_runtime": {"pg": {"dsn": f"postgresql://u:{sentinel}@h/db"}},
    }

    rendered = "\n".join(_leaf_diffs(old, new))

    assert sentinel not in rendered, (
        "a credential value reached the assertion output:\n" + rendered
    )
    # Still diagnostic: every changed path is named, including the ones inside
    # lists, which are the cases that do not recurse.
    for expected_path in (
        ".database.password",
        ".git.github.token",
        ".llm.fallback",
        ".auth.tokens",
        ".scheduler.jobstore_url",
        ".event_runtime.pg.dsn",
    ):
        assert expected_path in rendered, f"{expected_path} vanished from the diff"


def test_the_suite_does_not_inherit_an_ambient_dotenv(tmp_path, monkeypatch):
    """conftest.py sets CFOP_NO_DOTENV; prove it actually stops the read.

    Mutation-check: clear CFOP_NO_DOTENV and this fails, because the planted
    file is then loaded — which is exactly what was happening with the real
    .env before CFOP-44.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CFOP_DOTENV_SENTINEL=leaked\n", encoding="utf-8")
    monkeypatch.delenv("CFOP_DOTENV_SENTINEL", raising=False)

    cfg.load_env_file()

    assert os.getenv("CFOP_DOTENV_SENTINEL") is None, (
        "an ambient .env was read into the test process"
    )


def test_example_config_still_names_only_shipped_backends():
    """M0 (PR #122) made the example honest; the merge must not smuggle one back.

    A default that introduced an unregistered backend name would fail at
    startup for every user who does not override that section.
    """
    merged = cfg.load_config(EXAMPLE_CONFIG)
    observability = merged["observability"]
    assert observability["metrics"]["backend"] == "prometheus"
    assert observability["logs"]["backend"] == "loki"
    assert observability["alerts"]["backend"] == "alertmanager"


def test_example_config_is_actually_short():
    """The point of CFOP-26. Guards the file against creeping back up.

    Counted as settings, not lines: comments are what makes the example
    teachable and should not be rationed.
    """
    with open(EXAMPLE_CONFIG, "r", encoding="utf-8") as handle:
        settings = [
            line for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        ]
    assert len(settings) <= 25, f"config.yaml.example is back up to {len(settings)} settings"


def test_example_config_resolves_without_a_host_inventory():
    """"Boots with the ~15-line file and zero host inventory" from the issue."""
    merged = cfg.load_config(EXAMPLE_CONFIG)
    assert merged["infrastructure"]["hosts"] == {}
    assert merged["profile"] == cfg.PROFILE_INVESTIGATE
    assert merged["observability"]["metrics"]["url"]
    assert merged["observability"]["alerts"]["url"]
    assert merged["llm"]["primary"]["provider"] == "ollama"
    assert merged["llm"]["primary"]["url"]


# ---------------------------------------------------------------------------
# 2. Merge semantics: dicts merge, lists and scalars replace.
# ---------------------------------------------------------------------------


def test_partial_file_merges_nested_dicts_and_keeps_defaults(tmp_path):
    path = _write(tmp_path, """
        observability:
          metrics:
            url: http://prom.example:9090
    """)
    merged = cfg.load_config(path)

    # The set value wins.
    assert merged["observability"]["metrics"]["url"] == "http://prom.example:9090"
    # Its siblings inside the same nested dict survive from the defaults.
    assert merged["observability"]["metrics"]["backend"] == "prometheus"
    # Untouched top-level sections are present rather than missing.
    assert "database" in merged
    assert "ooda" in merged


def test_lists_replace_rather_than_merge(tmp_path):
    """An operator's list is an ordered choice; merging would resurrect deletions."""
    path = _write(tmp_path, """
        observability:
          notifications:
            - backend: discord
              webhook_url: https://discord.example/hook
    """)
    merged = cfg.load_config(path)

    notifications = merged["observability"]["notifications"]
    assert len(notifications) == 1
    assert notifications[0]["backend"] == "discord"


def test_scalars_replace(tmp_path):
    path = _write(tmp_path, """
        ooda:
          sweep_interval: 60
    """)
    merged = cfg.load_config(path)
    assert merged["ooda"]["sweep_interval"] == 60
    # Sibling defaults inside the same section are still there.
    assert merged["ooda"]["alert_check_interval"] == cfg.DEFAULT_CONFIG["ooda"]["alert_check_interval"]


def test_deep_merge_does_not_mutate_the_defaults(tmp_path):
    """A loader that aliases DEFAULT_CONFIG would leak one deploy's config into the next."""
    before = cfg.DEFAULT_CONFIG["observability"]["metrics"]["url"]
    path = _write(tmp_path, """
        observability:
          metrics:
            url: http://mutated.example:9090
    """)
    merged = cfg.load_config(path)
    merged["observability"]["metrics"]["url"] = "http://even-more-mutated:9090"
    assert cfg.DEFAULT_CONFIG["observability"]["metrics"]["url"] == before


# ---------------------------------------------------------------------------
# 3. Degenerate files are states, not crashes.
# ---------------------------------------------------------------------------


def test_empty_file_yields_the_defaults(tmp_path):
    path = _write(tmp_path, "")
    merged = cfg.load_config(path)
    assert merged["observability"]["metrics"]["backend"] == "prometheus"
    assert merged["database"]["port"] == cfg.DEFAULT_CONFIG["database"]["port"]


def test_missing_file_yields_the_defaults(tmp_path):
    merged = cfg.load_config(str(tmp_path / "nope.yaml"))
    assert merged["observability"]["metrics"]["backend"] == "prometheus"


def test_non_mapping_file_is_fatal_not_ignored(tmp_path):
    """Valid YAML that is not a mapping is still a config we cannot honour.

    This assertion was originally the other way round — "must not raise at
    import/boot time". Inverted deliberately: not raising means booting with
    defaults while the operator's file says something else, and the process
    looks healthy the whole time. See _read_yaml.
    """
    path = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_unparseable_file_refuses_to_start(tmp_path):
    """A present-but-broken file must not resolve to defaults.

    Falling back would leave the process running with empty Prometheus/Loki
    URLs and every remediation flag off, while /api/health — a liveness probe,
    not a config check — kept reporting green. That is a silent outage of
    investigation and of open_prs on a live deployment. Refusing is louder and
    is what the pre-merge loader did by accident.
    """
    path = _write(tmp_path, "key: [unclosed\n")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_non_mapping_file_refuses_to_start(tmp_path):
    path = _write(tmp_path, "- just\n- a list\n")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(path)


def test_empty_file_is_not_an_error(tmp_path):
    """"No overrides" is exactly what the defaults express — unlike a file
    that says something we cannot read."""
    merged = cfg.load_config(_write(tmp_path, ""))
    assert merged["observability"]["metrics"]["backend"] == "prometheus"


def test_missing_file_still_defaults(tmp_path):
    merged = cfg.load_config(str(tmp_path / "nope.yaml"))
    assert merged["observability"]["metrics"]["backend"] == "prometheus"


# ---------------------------------------------------------------------------
# 4. Defaults are inert: omitting a section disables it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("section", ["metrics", "logs", "alerts"])
def test_omitted_backend_sections_default_to_no_endpoint(tmp_path, section):
    """The whole reason merge-under-existing-config is safe.

    A guessed URL would silently start a client against a host the operator
    never named — a behaviour change disguised as a default.
    """
    path = _write(tmp_path, "database:\n  host: db\n")
    merged = cfg.load_config(path)
    assert merged["observability"][section]["url"] == ""


def test_omitted_containers_and_notifications_default_to_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    path = _write(tmp_path, "database:\n  host: db\n")
    merged = cfg.load_config(path)
    assert merged["observability"]["containers"] == []
    assert merged["observability"]["notifications"] == []


def test_in_cluster_containers_are_autodetected(tmp_path, monkeypatch):
    """"Kubernetes can self-describe", scoped so it cannot surprise anyone.

    KUBERNETES_SERVICE_HOST is a fact about where the process is running, not a
    guess about intent — and this only fires when the config named no container
    backend at all, which no existing deployment does.
    """
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    merged = cfg.load_config(_write(tmp_path, "database:\n  host: db\n"))
    assert merged["observability"]["containers"] == [{"backend": "kubernetes"}]


def test_autodetection_never_overrides_an_explicit_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    path = _write(tmp_path, """
        observability:
          containers: []
    """)
    merged = cfg.load_config(path)
    assert merged["observability"]["containers"] == []


def test_defaults_carry_no_hardcoded_urls():
    """No default may carry a URL or socket path.

    A guessed endpoint is the one kind of default that turns an omitted section
    into an outbound connection. Conventional service *names* (database.host)
    are fine — the database is not optional and has no disabled state.
    """
    offenders = sorted(_endpoint_like_leaves(cfg.DEFAULT_CONFIG))
    assert not offenders, f"default config guesses endpoints: {offenders}"


def _endpoint_like_leaves(node, path: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _endpoint_like_leaves(value, f"{path}.{key}")
        return
    if isinstance(node, list):
        for index, value in enumerate(node):
            yield from _endpoint_like_leaves(value, f"{path}[{index}]")
        return
    if isinstance(node, str) and ("://" in node):
        yield f"{path}={node}"


# ---------------------------------------------------------------------------
# 5. Alias normalisation: the getting-started shape.
# ---------------------------------------------------------------------------


GETTING_STARTED = """
    llm:
      backend: ollama
      url: http://gpu.example:11434
      model: gemma4:26b
    prometheus:
      url: http://prometheus:9090
    loki:
      url: http://loki:3100
    alertmanager:
      url: http://alertmanager:9093
    notify:
      slack_webhook: https://hooks.slack.example/abc
    profile: investigate
"""

LONGHAND_EQUIVALENT = """
    llm:
      primary:
        provider: ollama
        url: http://gpu.example:11434
        model: gemma4:26b
    observability:
      metrics:
        backend: prometheus
        url: http://prometheus:9090
      logs:
        backend: loki
        url: http://loki:3100
      alerts:
        backend: alertmanager
        url: http://alertmanager:9093
      notifications:
        - backend: slack
          webhook_url: https://hooks.slack.example/abc
    profile: investigate
"""


def test_getting_started_config_equals_its_longhand(tmp_path):
    """The short keys are sugar; they must resolve to the canonical shape.

    If these ever diverge, half the code reads the sugar and half reads the
    canonical form, which is the drift this whole change exists to prevent.
    """
    short = cfg.load_config(_write(tmp_path / "a", GETTING_STARTED))
    long = cfg.load_config(_write(tmp_path / "b", LONGHAND_EQUIVALENT))
    assert short == long


def test_getting_started_config_needs_no_host_inventory(tmp_path):
    """Absence of infrastructure.hosts is a first-class state, never an error."""
    merged = cfg.load_config(_write(tmp_path, GETTING_STARTED))
    assert merged["infrastructure"]["hosts"] == {}
    assert merged["git"]["repos"] == []


def test_loki_is_optional(tmp_path):
    """Named in the issue as optional; omitting it must leave logs disabled."""
    without_loki = "\n".join(
        line for line in textwrap.dedent(GETTING_STARTED).splitlines()
        if "loki" not in line
    )
    merged = cfg.load_config(_write(tmp_path, without_loki))
    assert merged["observability"]["logs"]["url"] == ""
    assert merged["observability"]["metrics"]["url"] == "http://prometheus:9090"


def test_longhand_llm_sections_survive_alias_folding(tmp_path):
    """Flat llm keys fold into llm.primary without disturbing the fallback chain."""
    path = _write(tmp_path, """
        llm:
          backend: ollama
          url: http://gpu.example:11434
          fallback:
            - provider: anthropic
              model: claude-sonnet-4-5
    """)
    merged = cfg.load_config(path)
    assert merged["llm"]["primary"]["provider"] == "ollama"
    assert merged["llm"]["primary"]["url"] == "http://gpu.example:11434"
    assert merged["llm"]["fallback"][0]["provider"] == "anthropic"
    # The flat aliases are consumed, not left lying around to be read by mistake.
    assert "backend" not in merged["llm"]
    assert "url" not in merged["llm"]


def test_explicit_nested_llm_primary_beats_the_alias(tmp_path):
    path = _write(tmp_path, """
        llm:
          url: http://alias.example:11434
          primary:
            url: http://explicit.example:11434
    """)
    merged = cfg.load_config(path)
    assert merged["llm"]["primary"]["url"] == "http://explicit.example:11434"


def test_notify_aliases_build_the_notification_list(tmp_path):
    path = _write(tmp_path, """
        notify:
          slack_webhook: https://hooks.slack.example/abc
          ntfy_url: https://ntfy.example
          ntfy_topic: cfop
    """)
    merged = cfg.load_config(path)
    backends = [entry["backend"] for entry in merged["observability"]["notifications"]]
    assert backends == ["slack", "ntfy"]


def test_blank_notify_aliases_are_dropped(tmp_path):
    """An unset ${VAR} expands to '' — that must not create a dead sink."""
    path = _write(tmp_path, """
        notify:
          slack_webhook: ""
          discord_webhook: ""
    """)
    merged = cfg.load_config(path)
    assert merged["observability"]["notifications"] == []


def test_alias_and_canonical_section_both_present_prefers_canonical(tmp_path):
    path = _write(tmp_path, """
        prometheus:
          url: http://alias.example:9090
        observability:
          metrics:
            url: http://canonical.example:9090
    """)
    merged = cfg.load_config(path)
    assert merged["observability"]["metrics"]["url"] == "http://canonical.example:9090"


def test_alias_keys_are_removed_from_the_result(tmp_path):
    """Leaving them behind would give two places to read the same setting."""
    merged = cfg.load_config(_write(tmp_path, GETTING_STARTED))
    for alias in ("prometheus", "loki", "alertmanager", "notify"):
        assert alias not in merged


# ---------------------------------------------------------------------------
# 6. Profile is a ceiling.
# ---------------------------------------------------------------------------


REMEDIATION_ON = textwrap.dedent("""
    remediation:
      enabled: true
      open_prs: true
      deep_open_prs: true
      queue_feed: true
      queue_drain: true
      queue_reap: true
      queue_verify: true
      executor:
        node_action:
          enabled: true
""")


def _remediation_config(profile: str | None) -> str:
    """The same everything-on remediation block, with or without a profile key."""
    prefix = "" if profile is None else f"profile: {profile}\n"
    return prefix + REMEDIATION_ON


def test_investigate_profile_zeroes_remediation_flags(tmp_path):
    path = _write(tmp_path, _remediation_config("investigate"))
    merged = cfg.load_config(path)
    remediation = merged["remediation"]
    for flag in ("enabled", "open_prs", "deep_open_prs",
                 "queue_feed", "queue_drain", "queue_reap", "queue_verify"):
        assert remediation[flag] is False, f"{flag} survived the investigate profile"
    assert remediation["executor"]["node_action"]["enabled"] is False


def test_remediate_profile_leaves_flags_as_written(tmp_path):
    path = _write(tmp_path, _remediation_config("remediate"))
    merged = cfg.load_config(path)
    assert merged["remediation"]["open_prs"] is True
    assert merged["remediation"]["queue_drain"] is True
    assert merged["remediation"]["executor"]["node_action"]["enabled"] is True


def test_absent_profile_key_does_not_clamp(tmp_path):
    """The production-safety case.

    Prod runs open_prs/deep_open_prs with no `profile:` key and auto-deploys on
    merge. Defaulting an unprofiled config to `investigate` would switch the
    remediation pipeline off with no migration window.
    """
    path = _write(tmp_path, _remediation_config(None))
    merged = cfg.load_config(path)
    assert merged["remediation"]["open_prs"] is True
    assert merged["remediation"]["queue_drain"] is True
    assert merged["profile"] is None


def test_missing_file_resolves_to_the_investigate_profile(tmp_path):
    """Nothing configured at all should be read-only."""
    merged = cfg.load_config(str(tmp_path / "absent.yaml"))
    assert merged["profile"] == cfg.PROFILE_INVESTIGATE
    assert merged["remediation"]["open_prs"] is False


def test_unknown_profile_falls_back_to_the_safer_one(tmp_path):
    path = _write(tmp_path, _remediation_config("yolo"))
    merged = cfg.load_config(path)
    assert merged["profile"] == cfg.PROFILE_INVESTIGATE
    assert merged["remediation"]["open_prs"] is False


def test_deep_investigation_is_not_clamped_by_investigate(tmp_path):
    """The deep tier is read-only by design; its fixes go through the PR gates."""
    path = _write(tmp_path, """
        profile: investigate
        event_runtime:
          deep_investigation:
            enabled: true
            completion_base_url: http://runtime:8080
    """)
    merged = cfg.load_config(path)
    assert merged["event_runtime"]["deep_investigation"]["enabled"] is True


def test_profile_allows_follows_the_scope_ladder():
    assert cfg.profile_allows(cfg.PROFILE_INVESTIGATE, cfg.SCOPE_READ)
    assert cfg.profile_allows(cfg.PROFILE_INVESTIGATE, cfg.SCOPE_INVESTIGATE)
    assert not cfg.profile_allows(cfg.PROFILE_INVESTIGATE, cfg.SCOPE_REMEDIATE)
    assert cfg.profile_allows(cfg.PROFILE_REMEDIATE, cfg.SCOPE_REMEDIATE)
    # Unprofiled deploys keep pre-CFOP-26 behaviour: no ceiling.
    assert cfg.profile_allows(None, cfg.SCOPE_REMEDIATE)


def test_scope_ladder_matches_the_auth_module():
    """cfshared restates the ladder instead of importing it (auth pulls in
    SQLAlchemy, and event_runtime's loader must stay stdlib-only). Restating it
    is only acceptable while something checks the two copies agree."""
    from auth import models

    assert cfg.SCOPE_READ == models.SCOPE_READ
    assert cfg.SCOPE_INVESTIGATE == models.SCOPE_INVESTIGATE
    assert cfg.SCOPE_REMEDIATE == models.SCOPE_REMEDIATE
    assert cfg.expand_scopes([cfg.SCOPE_REMEDIATE]) == models.expand_scopes([models.SCOPE_REMEDIATE])
    assert cfg.expand_scopes([cfg.SCOPE_INVESTIGATE]) == models.expand_scopes([models.SCOPE_INVESTIGATE])
    assert cfg.expand_scopes([cfg.SCOPE_READ]) == models.expand_scopes([models.SCOPE_READ])
    # The roles ride the same restatement (CFOP-124): tools/ reads them from
    # cfshared, the console from auth.models and web_auth.
    import web_auth

    assert cfg.ROLE_ADMIN == models.ROLE_ADMIN == web_auth.ROLE_ADMIN
    assert cfg.ROLE_MEMBER == models.ROLE_MEMBER == web_auth.ROLE_MEMBER


# ---------------------------------------------------------------------------
# 7. Env expansion and the colocated .env loader still work.
# ---------------------------------------------------------------------------


def test_env_vars_expand(tmp_path, monkeypatch):
    monkeypatch.setenv("CFOP_TEST_PROM_URL", "http://from-env:9090")
    path = _write(tmp_path, """
        observability:
          metrics:
            url: ${CFOP_TEST_PROM_URL}
    """)
    merged = cfg.load_config(path)
    assert merged["observability"]["metrics"]["url"] == "http://from-env:9090"


def test_unset_env_var_expands_to_empty_not_the_literal(tmp_path, monkeypatch):
    monkeypatch.delenv("CFOP_TEST_UNSET", raising=False)
    path = _write(tmp_path, """
        observability:
          metrics:
            url: ${CFOP_TEST_UNSET}
    """)
    merged = cfg.load_config(path)
    assert merged["observability"]["metrics"]["url"] == ""


def test_colocated_env_file_is_loaded(tmp_path, monkeypatch):
    # conftest sets CFOP_NO_DOTENV for the whole suite so nobody's real .env
    # leaks in. This test is *about* .env loading, so it opts back in.
    #
    # chdir is not optional here: load_env_file always also walks
    # Path.cwd()/".env", so opting back in while sitting in the repo root
    # reads the developer's real file. It copies in via os.environ.setdefault,
    # which monkeypatch does not roll back — it only restores keys it set
    # itself — so those credentials would outlive this test and expand into
    # later fixtures. That is the disclosure this whole change exists to stop.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CFOP_NO_DOTENV", raising=False)
    monkeypatch.delenv("CFOP_TEST_DOTENV", raising=False)
    (tmp_path / ".env").write_text('CFOP_TEST_DOTENV="http://from-dotenv:9090"\n', encoding="utf-8")
    path = _write(tmp_path, """
        observability:
          metrics:
            url: ${CFOP_TEST_DOTENV}
    """)
    merged = cfg.load_config(path)
    assert merged["observability"]["metrics"]["url"] == "http://from-dotenv:9090"


def test_real_env_beats_the_dotenv_file(tmp_path, monkeypatch):
    # Must opt back in like the test above, or the planted .env is never read
    # and this only proves "a monkeypatched variable wins" — which is not the
    # precedence the name claims. chdir for the same reason as above.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CFOP_NO_DOTENV", raising=False)
    monkeypatch.setenv("CFOP_TEST_PRECEDENCE", "http://real-env:9090")
    (tmp_path / ".env").write_text("CFOP_TEST_PRECEDENCE=http://dotenv:9090\n", encoding="utf-8")
    path = _write(tmp_path, """
        observability:
          metrics:
            url: ${CFOP_TEST_PRECEDENCE}
    """)
    merged = cfg.load_config(path)
    assert merged["observability"]["metrics"]["url"] == "http://real-env:9090"


# ---------------------------------------------------------------------------
# 8. Both consumers resolve the same config.
# ---------------------------------------------------------------------------


def test_event_runtime_resolves_the_shared_merged_config(tmp_path):
    """Two loaders with two default philosophies was the drift this replaced.

    (The agent half of this pairing lives in `agent/test_config_loader.py` —
    `agent.agent` uses bare imports that need `agent/` on sys.path, which the
    root-level suite deliberately does not provide.)
    """
    from event_runtime import bootstrap

    path = _write(tmp_path, GETTING_STARTED)
    assert bootstrap._load_root_config(path) == cfg.load_config(path)


def test_event_runtime_still_tolerates_no_config_path(tmp_path, monkeypatch):
    """It used to return {} and lean on per-field literals; it must not raise."""
    from event_runtime import bootstrap

    monkeypatch.delenv("CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    resolved = bootstrap._load_root_config(None)
    assert resolved["observability"]["metrics"]["backend"] == "prometheus"
    # Persistence stays off, so no DSN gets built from the default DB block.
    assert resolved["event_runtime"]["persistence"]["postgres"]["enabled"] is False


def test_database_section_is_always_present(tmp_path):
    """agent.py builds the DB URL by direct indexing; a missing section was a
    KeyError at startup rather than a diagnosable message."""
    merged = cfg.load_config(_write(tmp_path, GETTING_STARTED))
    for key in ("host", "port", "database", "user", "password"):
        assert key in merged["database"]
