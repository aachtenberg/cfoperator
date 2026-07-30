"""Tiny HTTP client: close a change record after node-action execution.

The agent owns open() + approval gating before spawn. The executor only
POSTs /close when CFOP_EXEC_CHANGE_URL is set and the work order carries
change_record_ref. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("cfop-executor.change_record")


def close_record(base_url: str, ref: str, outcome: Dict[str, Any], *,
                 timeout: int = 30) -> Optional[str]:
    """POST /close. Returns an error string on failure, None on success.

    Best-effort: execution already ran; callers log and continue.
    """
    if not base_url or not ref:
        return None
    url = f"{base_url.rstrip('/')}/close"
    body = json.dumps({"ref": ref, "outcome": outcome}, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec
            if 200 <= resp.status < 300:
                return None
            return f"close HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        return f"close HTTP {e.code}: {err}"
    except urllib.error.URLError as e:
        return f"close transport: {e}"
