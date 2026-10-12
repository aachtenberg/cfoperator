"""Which stored learnings an investigation is shown, and how (CFOP-377).

``_orient`` searches the knowledge base with the alert's trigger text and the
investigation prompt lists what comes back as "Relevant past learnings". Two
things made that list do harm rather than good:

  * **Auto-correlations crowd everything out.** The sweep writes learnings
    tagged ``automated`` from alerts that fired close together, guessing a
    cause from co-occurrence. CFOP-149 tightened how they are written, but
    their text stays generic enough to sit at 0.75-0.85 similarity to almost
    any trigger. Run against the last 30 triggers (2026-10-12), they held the
    top slots on about half -- a plane-admin restart was offered "Vaultwarden
    pod instability" at 0.807 -- and the model treats whatever it is shown as
    established. Remediation #275 restated one (learning 2459) nearly word for
    word, while the operator-written correction ranked below it.
  * **The top three were taken however weak the match.** Relevant hits on that
    probe scored 0.70-0.88; unrelated ones 0.59-0.65 still filled the slots
    whenever nothing better existed.

So the prompt asks for more candidates than it shows, drops auto-correlations
and anything under the floor, and labels each survivor with where it came
from. The auto-correlations stay in the KB, and chat and ``/api/kb/search``
still return them -- only the investigation prompt stops presenting them as
known.
"""
from typing import Any, Dict, Iterable, List, Tuple

# Cosine similarity below which a learning is not shown. Measured against
# nomic-embed-text on the 2026-10-12 probe above; a different embedding model
# moves the whole distribution, so re-measure before reusing the number.
SIMILARITY_FLOOR = 0.65

# How many the prompt shows, and how many to ask the KB for so that enough
# survive the filters below.
PROMPT_LIMIT = 3
CANDIDATE_LIMIT = 12

AUTO_CORRELATION_TAG = 'automated'


def _tags(learning: Dict[str, Any]) -> List[str]:
    tags = learning.get('tags') or []
    return [str(t) for t in tags] if isinstance(tags, list) else []


def is_auto_correlation(learning: Dict[str, Any]) -> bool:
    return AUTO_CORRELATION_TAG in _tags(learning)


def provenance(learning: Dict[str, Any]) -> str:
    """Where a learning came from, in words the investigating model can weigh.

    Read off the tags each writer sets: ``cfop-reverify`` from a re-verification
    that rejected a filed row (``reverify.store_learning``), ``source:<who>``
    from ``POST /api/learnings``. Anything else was extracted from a finished
    investigation.
    """
    tags = _tags(learning)
    if 'cfop-reverify' in tags:
        return 'from a rejected remediation'
    for tag in tags:
        if tag.startswith('source:') and tag[len('source:'):].strip():
            return f"from {tag[len('source:'):].strip()}"
    return 'from a past investigation'


def _clears_floor(learning: Dict[str, Any]) -> bool:
    # The FTS-only search (embeddings down) carries no score at all, so there
    # is nothing to hold it to. In the hybrid search a nonzero fts_rank means
    # full-text search matched every word of the trigger -- a strong match in
    # its own right, and the only way a learning still waiting for its
    # embedding (vector_similarity 0) can be found.
    if 'vector_similarity' not in learning:
        return True
    return ((learning.get('vector_similarity') or 0) >= SIMILARITY_FLOOR
            or (learning.get('fts_rank') or 0) > 0)


def select_prompt_learnings(
    candidates: Iterable[Dict[str, Any]], limit: int = PROMPT_LIMIT,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """The learnings to show, in the KB's ranking order, and what was dropped.

    Returns ``(kept, dropped)`` where ``dropped`` counts ``auto_correlation``
    and ``under_floor`` so the caller can say why fewer came back.
    """
    kept: List[Dict[str, Any]] = []
    dropped = {'auto_correlation': 0, 'under_floor': 0}
    for learning in candidates or []:
        if not isinstance(learning, dict):
            continue
        if is_auto_correlation(learning):
            dropped['auto_correlation'] += 1
        elif not _clears_floor(learning):
            dropped['under_floor'] += 1
        elif len(kept) < limit:
            kept.append(learning)
    return kept, dropped
