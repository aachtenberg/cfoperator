---
name: recheck-filed
description: Re-check the remediation rows cfoperator has filed to the issue tracker (status `filed`, mirrored in Plane as `[cfop #N]` items) against live cluster and host state. Use whenever asked to recheck, audit, review or groom what was filed to the tracker, whether filed or parked rows are still real, or what is sitting in the `[cfop #N]` backlog, even if the request only says "check the filed items".
---

# Re-check filed rows

`filed` means the pipeline gave up on a row and handed it to the tracker
(`docs/REMEDIATION.md`, "Tracker hand-off"). The `queue_reverify` tick already
re-reads these, so a hand pass earns its cost by doing what the tick cannot.

The tick confirms a symptom and then says the recommendation stands. It cannot
read Plane comments, so it repeats advice a person has already overturned, and
its cluster and host access is deliberately narrower than yours. So the question
here is not only "is it still broken" but "would the recommended fix work".

## Getting at everything

Nothing below needs working out again. Keep each command plain: start with
`kubectl --kubeconfig`, no leading `export`, no pipe into python, because the
permission allowlist matches each segment by prefix.

**Rows**, from a pod holding a scoped token. `deploy/cfoperator` has no token and
401s. Save to your scratchpad, then summarise as a separate command:

```bash
kubectl --kubeconfig ~/.kube/config-k3s -n apps exec deploy/cfoperator-mcp -- sh -c 'curl -s -H "Authorization: Bearer $CFOP_API_TOKEN" "$CFOP_AGENT_URL/api/remediations?status=filed&limit=200"' > <scratchpad>/filed.json
```

```bash
python3 -I .claude/skills/recheck-filed/scripts/filed_summary.py <scratchpad>/filed.json
```

The same `exec` reaches `/api/remediation/flags` (is `queue_reverify` on?) and
`/api/investigations/<id>` (the reasoning behind a row; `findings.response`).

**Plane.** Use the `plane` MCP server, `mcp__plane__*`, not the plugin that asks
to authenticate. The tools are deferred, so load them first with ToolSearch:
`select:mcp__plane__workitem,mcp__plane__workitem_comment`. Then
`workitem_comment` with `action: "list"` and the `project_id` and `workitem_id`
the summary printed. Read the content, not the flag: an error comes back as text
with `isError: false`. If the tools are absent, say so in the report, because
hand-written comments are the thing you would otherwise miss.

**Cluster.** `kubectl --kubeconfig ~/.kube/config-k3s ...`.

**Hosts.** `ssh -o BatchMode=yes -o ConnectTimeout=6 <user>@<host>`. Reads are
fine. Use `sudo -n`, so a command without a rule fails instead of waiting on a
password prompt.

**History.** Pod logs roll over within hours when something floods them, so ask
Loki from the mcp pod. Times are nanosecond epochs:

```bash
kubectl --kubeconfig ~/.kube/config-k3s -n apps exec deploy/cfoperator-mcp -- sh -c "curl -s -G 'http://loki.monitoring:3100/loki/api/v1/query_range' --data-urlencode 'query={pod=\"<pod>\"} |= \"<text>\"' --data-urlencode 'start=<ns>' --data-urlencode 'end=<ns>' --data-urlencode 'limit=60' --data-urlencode 'direction=forward'"
```

## The pass

1. **Read every comment before touching the row.** A comment that does not start
   `Automated re-verification` was written after someone looked, and it outranks
   both the row and the tick. Comments still age, so re-test what they claim.
2. **Group rows by condition.** Several rows are often one cause, and that
   grouping is itself a finding. Filed rows that dedupe missed show up here.
3. **Ask three separate questions of each group.** Is the symptom still there? Is
   the diagnosis right? Would the fix work? They fail independently: a real
   symptom with a wrong fix is the commonest result, and it is the one the tick
   cannot catch.
4. **Check that the fix's target exists.** A row saying "commit and push" needs a
   manifest in the repo. A row naming a variable needs that variable to be the
   one in use.
5. **Allow for what you cannot see.** A host the operator switched off looks
   exactly like a dead one. If a verdict turns on that, say what it rests on.

## When a diagnosis smells wrong

Look at what the fix assumes, then measure that.

- **A limit was hit:** look at what is holding the resource, including its age and
  state, rather than the count. Long-lived holders that all started together are
  a hang, not load.
- **Round-number timings:** an exact 300 s or 60 s between two log lines usually
  means a timer fired because a wake-up never arrived.
- **A hung process:** `sudo -n cat /proc/<pid>/wchan` and `/proc/<pid>/syscall`
  say where and whether there is a timeout. `/proc/<pid>/status` has the signal
  masks. CPU and `read_bytes` sampled twice separate slow from stuck.
- **Unexplained refusals:** `sudo -n journalctl -k -b | grep 'apparmor="DENIED"'`.
- **"When did this start":** line up `journalctl --list-boots`, the kernel of each
  boot, `/var/log/apt/history.log` and the first occurrence in Loki. A clean
  before and after is worth more than a plausible story, and an upgrade is often
  the answer.

## Writing back

Comment on an item only when you learned something it does not already say. The
tick already posts "still holds", so a second one is noise. `comment_html` takes
raw tags, not escaped ones, while a literal `&` in code text is `&amp;`. Read the
returned `comment_html` back. Mark what was measured apart from what was inferred.

Leave row state alone unless asked. Once a condition clears, the tick resolves
the row by itself within about a day. When asked to close one:
`POST /api/remediations/<id>/resolve` or `/reject` with `{"note": "..."}`, through
the same mcp pod `exec`. Closing the Plane item closes the row too.

Writes over ssh to fleet hosts are refused by the classifier. Do not probe other
routes around it. Hand the operator the exact `! ssh ...` command.

## Report

One line per condition, not per row: the keys, what is true now, and what happens
next. Lead with anything that overturns a recommendation. Say which fixes are
waiting on the operator, and give the command.
