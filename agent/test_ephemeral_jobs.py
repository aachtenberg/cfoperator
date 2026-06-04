"""Job/CronJob pods must not be read as failures/drift (false co-failures)."""
from knowledge_base import is_ephemeral_job_pod, KnowledgeBase


def test_real_cronjob_pods_are_ephemeral():
    # the exact pods from the freshet-alerter <-> ingest false correlation
    for n in ["freshet-alerter-29676615-6slrn", "reservoir-ingest-29675790-skc75",
              "hq-ingest-29676627-xpg2c", "swe-caldas-ingest-29676613-pt5fj"]:
        assert is_ephemeral_job_pod(n), n


def test_deployment_pods_are_not_ephemeral():
    # hash segments contain letters -> not a CronJob timestamp
    for n in ["searxng-c55b97cbb-ccnj6", "immich-server-868f4dd875-p7kcc",
              "cfoperator-7655b5cb59-d6f56", "freshet-dashboard-b5c857756-zr7bv"]:
        assert not is_ephemeral_job_pod(n), n


def test_plain_names_not_ephemeral():
    for n in ["loki-0", "redis", "", None]:
        assert not is_ephemeral_job_pod(n)


def test_normalize_still_collapses_cronjob_basename():
    # normalization itself is unchanged; the filter is what drops them
    assert KnowledgeBase._normalize_service_name("reservoir-ingest-29675790-skc75") == "reservoir-ingest"
