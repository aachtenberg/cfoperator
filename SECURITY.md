# Security Policy

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting:** go to the
[Security tab](https://github.com/aachtenberg/cfoperator/security/advisories/new)
and open a draft advisory. That keeps the report private until a fix exists.

Please do **not** open a public issue for a security problem.

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
  design intent is that no amount of injected text can cause a cluster mutation
  — the worst outcome should be a bad pull request that a human then declines.
  A path that beats that is a real finding.
- **SSH / node-action lane** (`node_action.enabled`) — the one place the agent
  touches hosts directly, deliberately gated and off by default.

## Design limits, not vulnerabilities

These are intentional and documented; reporting them is welcome as feedback but
they are not treated as vulnerabilities:

- **The agent opens pull requests; it never mutates a running cluster.** The
  merge button is the deploy path and it belongs to a human. This is the
  central safety property, so a report that the agent "cannot fix things
  automatically" is describing the design working.
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
Prometheus/Loki/Postgres. The one outbound path is your configured LLM — if you
run Ollama locally (the default and the tested path), nothing leaves your
network at all. If you configure a cloud fallback (Anthropic, Groq, Gemini, DeepSeek),
then investigation prompts containing your alert text, log excerpts and metric
values go to that provider under their terms. That is your choice to make, and
it is why the local path is the default.
