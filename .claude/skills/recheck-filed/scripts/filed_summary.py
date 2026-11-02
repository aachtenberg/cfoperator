#!/usr/bin/env python3
"""One block per row from a saved GET /api/remediations?status=filed response.

Usage: filed_summary.py <filed.json>

Prints what a re-check needs and nothing else: the tracker key, the ids the
Plane MCP wants (decoded from result.tracker), what the automated re-verify
pass last said, and the recommendation.
"""
import base64
import json
import re
import sys
from datetime import datetime, timezone

# The limit= in SKILL.md's fetch. The API returns the newest rows first and
# stops there, so a full page means the oldest filed rows were left out.
FETCH_LIMIT = 200
REC_CHARS = 300
STEP_CHARS = 160
MAX_STEPS = 4


def plane_ids(tracker):
    """(project_id, workitem_id) for the Plane MCP; (None, None) on another backend."""
    ref = tracker.get('ref') or ''
    try:
        decoded = json.loads(base64.urlsafe_b64decode(ref + '=' * (-len(ref) % 4)))
    except ValueError:
        return None, None
    if not isinstance(decoded, dict) or decoded.get('backend') != 'plane':
        return None, None
    project = re.search(r'/projects/([0-9a-f-]{36})/', tracker.get('url') or '')
    return (project.group(1) if project else None), decoded.get('id')


def age_days(created_at):
    """Days since a naive UTC timestamp, or None when it does not parse."""
    try:
        created = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - created).total_seconds() / 86400


def reverify_line(state):
    if not state:
        return 'never checked'
    return (f"{state.get('verdict')} after {state.get('checks')} check(s), "
            f"last {state.get('checked_at')} by {state.get('verified_by')}")


def clip(text, limit):
    return text if len(text) <= limit else text[:limit] + ' [...]'


def load_rows(path):
    """The rows, or exit with what came back instead.

    A 401 or 500 is not an empty backlog. The 500 body even carries
    `remediations: []`, so reading that key alone would report nothing filed.
    """
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    try:
        data = json.loads(text)
    except ValueError:
        sys.exit(f'not JSON, so the fetch failed: {text[:300]!r}')
    if not isinstance(data, dict) or data.get('error') or 'remediations' not in data:
        sys.exit(f'not a remediation list: {json.dumps(data)[:300]}')
    return data['remediations'] or []


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    rows = load_rows(sys.argv[1])
    print(f'{len(rows)} filed row(s)')
    if len(rows) >= FETCH_LIMIT:
        print(f'warning: that is a full page of {FETCH_LIMIT}, so older filed rows '
              f'may be missing; re-fetch with a higher limit=')
    for row in rows:
        payload = row.get('payload') or {}
        result = row.get('result') or {}
        tracker = result.get('tracker') or {}
        project_id, workitem_id = plane_ids(tracker)
        age = age_days(row.get('created_at'))
        print()
        print(f"#{row.get('id')} {row.get('tracker_key') or 'no-key'} "
              f"tracker={row.get('tracker_state')} "
              f"{row.get('remediation_class')}/{row.get('risk')} "
              f"age={'?' if age is None else f'{age:.1f}d'} "
              f"investigation={row.get('investigation_id')}")
        print(f'  plane: project_id={project_id} workitem_id={workitem_id}')
        print(f"  reverify: {reverify_line(result.get('reverify'))}")
        if tracker.get('error'):
            print(f"  tracker error: {tracker['error']}")
        print(f"  rec: {clip((payload.get('recommendation') or '').strip(), REC_CHARS)}")
        steps = payload.get('steps') or []
        for step in steps[:MAX_STEPS]:
            print(f'  step: {clip(str(step), STEP_CHARS)}')
        if len(steps) > MAX_STEPS:
            print(f'  ({len(steps) - MAX_STEPS} more step(s) not shown)')


if __name__ == '__main__':
    main()
