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
  
  **Defenses (CFOP-313)**: Untrusted data (alert summaries, labels, pod names,
  logs, tool outputs) is framed with explicit delimiters (`<<< DATA START >>>`
  / `<<< DATA END >>>`) and system prompts instruct models to treat delimited
  content as data, not instructions. Delimiter tokens, markdown code fences,
  fake role markers (ASSISTANT:, SYSTEM:), and fake verdict/status markers
  (STATUS:, VERDICT:, APPROVED:, RECOMMENDATION:, FIX:) are neutralized with
  zero-width joiners before they reach prompts. Log excerpts and alert fields
  are capped (alert summaries: 800 chars, logs: 2000-4000 chars, tool results:
  4000 chars per the existing `chat.max_tool_result_chars` config). The
  mutation judge, investigation, triage, and node-action (deep-tier SSH)
  prompts all apply these defenses.
  
  **Limits**: These are prompt-level defenses; they make injection harder but
  do not eliminate the attack surface. An adversary who controls alert text or
  logs may still craft prompts that confuse the model into bad recommendations.
  The gate remains the pull request: a human reviews the diff before it merges.
  Models are probabilistic and can be steered; framing raises the bar but is
  not a semantic firewall. The real guarantee is that the agent never mutates
  the cluster directly — only via a reviewed PR.
- **SSH / node-action lane** (`node_action.enabled`) — the one place the agent
  touches hosts directly. Schema default is off; the remediate-profile chart
  flips it on (CFOP-131). Still gated on the change-record PR, the allowlist,
  and a console kill-switch.

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
network at all. If you configure a cloud fallback (Anthropic, Groq, Gemini, DeepSeek,
OpenRouter), then investigation prompts containing your alert text, log excerpts
and metric values go to that provider under their terms. OpenRouter is a router:
a prompt sent to it goes on to whichever third-party host it picks for the
request, under that host's terms as well. The agent asks OpenRouter to use only
hosts whose stated policy is not to store or train on prompts
(`data_collection: deny`); cfassist does not send that, so set it in your
OpenRouter account if you use cfassist with it. That is your choice to make, and
it is why the local path is the default.
