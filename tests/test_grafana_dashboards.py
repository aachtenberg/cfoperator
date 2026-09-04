"""Guards for the grafana/ dashboards this repo ships (CFOP-164).

The files in grafana/ are byte-identical mirrors of the copies provisioned
from homelab-infra. Copilot's review of #244 caught two ways that mirror
can lie to the next importer:

* grafana/README.md documenting one PostgreSQL wiring (and an env var whose
  only consumer was the deleted upload script) while the two boards actually
  wire Postgres differently;
* a panel description that still promised pool logs after the query dropped
  ``Pool``.

These check the class, not today's wording. A new board that hard-codes a
different Postgres uid, or a log panel whose description mentions pool
activity the query cannot return, should fail here rather than in a review
comment.
"""

from repo_paths import REPO_ROOT
import json
import re

GRAFANA = REPO_ROOT / "grafana"
README = GRAFANA / "README.md"
FLEET = GRAFANA / "cfoperator-dashboard.json"
EVENT_RUNTIME = GRAFANA / "event-runtime-dashboard.json"


def _walk_panels(panels):
    """Yield every panel dict, including those nested in collapsed rows."""
    for panel in panels:
        yield panel
        if panel.get("type") == "row":
            yield from _walk_panels(panel.get("panels") or [])


def _postgres_uids(dashboard):
    uids = set()
    for panel in _walk_panels(dashboard.get("panels") or []):
        for ds in (panel.get("datasource"),) + tuple(
            t.get("datasource") for t in panel.get("targets") or []
        ):
            if not isinstance(ds, dict):
                continue
            if ds.get("type") == "grafana-postgresql-datasource":
                uids.add(ds.get("uid"))
    return uids


def test_readme_does_not_advertise_the_deleted_upload_env_var():
    """SRE_PG_DATASOURCE_UID existed only for upload-dashboard.sh."""
    text = README.read_text(encoding="utf-8")
    assert "SRE_PG_DATASOURCE_UID" not in text
    for match in re.finditer(r"upload-dashboard\.sh", text):
        window = text[max(0, match.start() - 80): match.end() + 40].lower()
        assert any(w in window for w in ("gone", "retired", "deleted", "removed")), (
            f"upload-dashboard.sh is mentioned as if it still exists: {window!r}"
        )


def test_readme_documents_postgres_wiring_per_board():
    """The two boards are not interchangeable at import time."""
    text = README.read_text(encoding="utf-8")
    section = text.split("### 3. PostgreSQL", 1)
    assert len(section) == 2, "the PostgreSQL heading is gone"
    body = section[1].split("## ", 1)[0]
    assert "cfoperator-dashboard.json" in body
    assert "event-runtime-dashboard.json" in body
    assert "sre-postgres" in body
    assert "${postgres}" in body


def test_fleet_postgres_panels_use_the_provisioned_uid():
    dash = json.loads(FLEET.read_text(encoding="utf-8"))
    assert _postgres_uids(dash) == {"sre-postgres"}


def test_event_runtime_postgres_panels_use_the_variable():
    dash = json.loads(EVENT_RUNTIME.read_text(encoding="utf-8"))
    assert _postgres_uids(dash) == {"${postgres}"}
    names = {v.get("name") for v in dash.get("templating", {}).get("list", [])}
    assert "postgres" in names


def test_log_panel_descriptions_do_not_promise_pool_when_query_dropped_it():
    """The #244 miss: Sweep Logs dropped Pool from the regex, left the blurb.

    Other log panels describe conceptually (OODA, embeddings) without those
    tokens appearing in LogQL, so this is not a general description-vs-query
    check — only the pool terms the query actually used to filter on.
    """
    dash = json.loads(FLEET.read_text(encoding="utf-8"))
    pool_terms = re.compile(r"\b(pool|checkout|checkin)\b", re.I)
    for panel in _walk_panels(dash.get("panels") or []):
        if panel.get("type") != "logs":
            continue
        desc = panel.get("description") or ""
        if not pool_terms.search(desc):
            continue
        exprs = " ".join(
            t.get("expr") or "" for t in panel.get("targets") or []
        )
        assert pool_terms.search(exprs), (
            f"panel {panel.get('title')!r} description still names pool "
            f"activity the query does not select: {desc!r} vs {exprs!r}"
        )
