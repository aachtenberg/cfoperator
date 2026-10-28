# Security Policy

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting:** go to the
[Security tab](https://github.com/aachtenberg/cfoperator/security/advisories/new)
and open a draft advisory. That keeps the report private until a fix exists.

Please do **not** open a public issue for a security problem. If the private
reporting form is not available to you, open a public issue asking for a
private contact, with no details of the vulnerability in it.

Include what you'd want to receive: what you did, what happened, and why it
matters. A proof of concept helps but is not required to file.

This is a small project maintained by one person, so a realistic expectation:
an acknowledgement within a week. If a report is credible and I cannot fix it
quickly, I would rather tell you that than go quiet.

## What is in scope

The agent and its surfaces — the `:8083` console and API, the MCP server, the
Slack bridge, the executor, and the auth/token model. Anything that would let
someone read infrastructure data they shouldn't, run an action they shouldn't,
or escalate from one scope tier to another.

Particularly interesting:

- **Scope escalation** across the `read` ⊂ `investigate` ⊂ `remediate` tiers,
  whether via the console, an API token, or an MCP tool call.
- **Prompt injection that reaches an action.** Alert text, log lines, and pod
  names are attacker-influenceable and they all end up in an LLM prompt. The
  design intent is that injected text cannot reach a change without one of the
  human gates in [Design limits](#design-limits-not-vulnerabilities): the merge
  on a PR or a change record, or an admin confirming a command in the console.
  So the worst outcome should be a bad pull request, or a bad command put in
  front of an admin, that the human then declines. A path that beats that is a
  real finding.

  **Defenses (CFOP-313)**: Untrusted data (alert summaries, labels, pod names,
  logs, tool outputs) is framed with explicit delimiters (`<<< DATA START >>>`
  / `<<< DATA END >>>`) and system prompts instruct models to treat delimited
  content as data, not instructions. Delimiter tokens (and look-alikes that
  differ in case or spacing), markdown code fences, and upper-case fake role
  or verdict markers at the start of a line (ASSISTANT:, SYSTEM:, STATUS:,
  VERDICT:, APPROVED:, RECOMMENDATION:, FIX:) are neutralized with
  zero-width spaces (U+200B) before they reach prompts. Mid-line and
  lower-case matches, such as `system:serviceaccount:` principals or
  kubectl's `status:` and `user:` keys, are left intact: the model may need
  them verbatim in its next tool call. Each
  framed field has its own cap (an alert summary 500 chars in the triage
  prompt, an alert's details 2000 in all in the investigation prompt, a
  proposed remediation 800 in the mutation judge's), and every tool result in a
  tool loop is cut to `chat.max_tool_result_chars`, 6000 chars by default,
  inside its frame.
  The mutation judge, investigation, triage, and node-action (deep-tier SSH)
  prompts all apply these defenses; the executor carries its own stdlib copy
  of the node-action framing, held to the agent's by a parity test.

  **Limits**: These are prompt-level defenses; they make injection harder but
  do not eliminate the attack surface. An adversary who controls alert text or
  logs may still craft prompts that confuse the model into bad recommendations.
  The gate remains a person: a human reviews the diff before a PR merges, and
  an admin reads the exact command before a host write runs.
  Models are probabilistic and can be steered; framing raises the bar but is
  not a semantic firewall. Marker neutralization is deliberately narrow: only
  an upper-case marker at the start of a line is defused, so a mixed-case
  `Verdict:` or `Approved:` in a log line is left as data, as is a marker that
  follows an opening quote inside a JSON-serialized tool result
  (`{"msg": "APPROVED: ..."}`), and the framing and system guidance are what
  cover them. The fine-tune dataset builder
  (`scripts/build_triage_dataset.py`) still emits the pre-framing shape of the
  triage prompt; training data for the next triage model should be
  regenerated from the live shape (CFOP-277). The real guarantee is the one in
  [Design limits](#design-limits-not-vulnerabilities): nothing changes
  unattended.
- **SSH / node-action lane** (`remediation.executor.node_action.enabled`) —
  where an approved remediation runs a command plan on a host. Schema default
  is off; the remediate-profile chart flips it on (CFOP-131). A node-action is
  refused outright when no change recorder is configured; otherwise it waits on
  the change-record PR's merge, and it is bound by the allowlist and a console
  kill-switch. The same lane carries an admin's console-confirmed host write
  (CFOP-442), which runs exactly the command the admin was shown. Everything
  else the agent does over SSH is a read, as an identity whose sudoers refuses
  writes.
- **Cluster-wide secrets access** (CFOP-312) — CFOperator's Helm chart grants
  NO cluster-wide access to Kubernetes secrets or configmaps. The only secrets
  access is the namespaced cockpit-spawn Role's `create`-only grant for
  ephemeral token Secrets. No tool reads secret values, and
  `tests/test_helm_chart.py` parses the rendered RBAC: every ClusterRole rule
  is refused `secrets`, `configmaps` and `*`, every binding must name a role
  the chart defines, and the only Role rule naming secrets must be
  cockpit-spawn's create-only one. The homelab deploy repo
  (aachtenberg/cfoperator-deploy) is a separate install whose `cfoperator-role`
  still grants cluster-wide secrets read today; CFOP-312 narrows it there. See
  docs/DEPLOYMENT.md "Agent secrets read".

## Design limits, not vulnerabilities

These are intentional and documented; reporting them is welcome as feedback but
they are not treated as vulnerabilities:

- **CFOperator never changes anything unattended.** Every change has a named
  human behind it: the merge on a GitOps PR, the merge of a change-record PR
  for a host action (node-action is refused outright when no change recorder
  is configured; see [docs/REMEDIATION.md](docs/REMEDIATION.md) "Imperative
  lane change records" and "Safety model"), or an admin confirming a command
  in the console. This is the central safety property, and in detail:
  - An unattended run (an investigation, the sweep, triage) does not change the
    system. Its write tools are withheld, except two whose effect waits on a
    person or stays in the agent's own memory: opening a pull request, and
    storing a learning (CFOP-240). Its SSH and pod-exec commands run only when
    a classifier reads them as reads; that classifier is a denylist of write
    shapes, not a proof, so a write it lets through is a real finding, not a
    design limit. What the run wants changed goes into its proposed fix and
    through the remediation queue.
  - An admin chatting in the console may ask the agent to change something. A
    host write over SSH pauses the turn and shows the admin the host and the
    exact command; it runs once, on **Run**, with a change record (CFOP-442).
    The agent's in-cluster write tools (a rollout restart, an exec into a
    pod), and any MCP connector tool its server does not declare read-only,
    run on the admin's request. In-cluster that is within the agent's own
    Kubernetes RBAC, and the Helm chart grants it no cluster-wide write verb.
    A `remediate`-scope API
    token counts as an admin for those, and is refused host writes because
    nobody is at a console to confirm them. A member is never offered a write
    tool.
  - The agent's own disposable Jobs (the executor that turns an approved row
    into a PR or runs an approved plan, the deep-investigation worker, a
    cockpit) are how it does that work, not changes to what it watches.

  So a report that the agent "cannot fix things automatically" is describing
  the design working.
- **The LLM can be wrong.** A bad diff in a PR is an expected failure mode
  handled by human review, not a security bug.
- **Secrets you put in your own config are yours to protect.** The agent reads
  the config and environment it is given.

## Data handling — no telemetry

**CFOperator never calls home.** There is no version-check ping, no usage
analytics, no crash reporting, and no license or activation check. Nothing in
this repository sends data to the maintainer, and there is no server to send it
to. If you find network traffic that contradicts this, treat it as a
vulnerability and report it.

This is deliberate and it costs us something real: install counts are invisible
and there is no way to know who is running this. That trade is accepted, because
the product is for people who will not send their logs to a third party, and a
tool with that pitch should not quietly make an exception for itself.

**Where your data does go:** wherever you point it. Telemetry stays in your
Prometheus/Loki/Postgres. What leaves it goes where you configured it to: your
LLM, and whatever you set up to receive results (notification sinks, the Git
host its pull requests go to, an issue tracker, an MCP connector). If you run
Ollama locally (the default and the tested path), no prompt leaves your
network. If you configure a cloud fallback (Anthropic, Groq, xAI, Gemini,
DeepSeek, OpenRouter), then investigation prompts containing your alert text, log excerpts
and metric values go to that provider under their terms. OpenRouter is a router:
a prompt sent to it goes on to whichever third-party host it picks for the
request, under that host's terms as well. The agent asks OpenRouter to use only
hosts whose stated policy is not to store or train on prompts
(`data_collection: deny`); cfassist does not send that, so set it in your
OpenRouter account if you use cfassist with it. That is your choice to make, and
it is why the local path is the default.
