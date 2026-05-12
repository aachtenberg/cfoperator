-- One-time cleanup: quarantine low-quality investigation_learnings.
--
-- Context: the sweep correlation pipeline and (older) extraction prompts
-- minted a `pattern`/`insight` learning per finding with no `applies_when` —
-- a learning with no trigger condition can never be retrieved on relevance,
-- so these are pure noise that dilute semantic/FTS search. The application
-- now auto-deprecates such learnings on write (see KnowledgeBase.store_learning);
-- this script handles the existing backlog.
--
-- Strategy: mark deprecated (NOT delete — keep the audit trail; find_learnings()
-- already filters `deprecated = false`). Preserve anything that has ever been
-- applied or is explicitly verified, regardless of applies_when.
--
-- Run:  kubectl exec -i -n data sre-postgres-0 -- psql -U sre_agent -d sre_knowledge < prune_low_quality_learnings.sql

BEGIN;

\echo == before ==
SELECT deprecated, count(*) FROM investigation_learnings GROUP BY 1 ORDER BY 1;

UPDATE investigation_learnings
SET deprecated = true,
    updated_at = now()
WHERE (applies_when IS NULL OR btrim(applies_when) = '')
  AND deprecated IS DISTINCT FROM true
  AND coalesce(times_applied, 0) = 0
  AND coalesce(verified, false) = false;

\echo == after ==
SELECT deprecated, count(*) FROM investigation_learnings GROUP BY 1 ORDER BY 1;

\echo == surviving (active) learnings by type ==
SELECT learning_type, count(*) FROM investigation_learnings WHERE deprecated = false GROUP BY 1 ORDER BY 2 DESC;

COMMIT;
