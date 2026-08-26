---
name: add-llm-provider
description: Wire a new hosted LLM provider (OpenAI-compatible) into cfoperator, cfassist, and the homelab deploy repos. Use when asked to "add", "wire in", or "support" a provider like DeepSeek, Mistral, Together, etc.
---

# Add a hosted LLM provider

Everything below is the footprint one provider touches. It was reconstructed
from the Gemini/xAI round (CFOP-104/105/106, PR #198) and the DeepSeek round
(CFOP-110). Do the steps in order; each has a test that fails if skipped.

## 0. Verify before you write anything

- `curl -s https://<base>/models -H "Authorization: Bearer $KEY"` — confirm
  the **exact model id** from the live list. Twice the first-run stubs
  shipped a model Google had retired (#199, #201). Never write a model name
  from memory.
- Send one tiny `chat/completions` with a `tools` array. This catches three
  things at once: billing (DeepSeek returned `Insufficient Balance` on a
  valid key), the URL shape, and tool-calling support. Report the result in
  the Plane issue; do not skip the wiring because the account is unfunded.
- Decide the **wire shape**: does the host serve `/v1/chat/completions`
  (Groq, xAI, DeepSeek → `provider: openai` in cfassist) or something else
  (Gemini's `/v1beta/openai` with no `/v1` → its own provider name and an
  `openaiPath()` branch in `cfassist-go/internal/client/client.go`)? Only a
  new shape needs Go code.
- Env var: `<NAME>_API_KEY`. Helm, compose, seal-secrets and Ansible all
  derive from this spelling.

## 1. Plane

Create the issue (project CFOP, module "Hosted provider wiring", current
cycle — new issues without both are invisible), write the plan into
`description_html`, move to In Progress. Record the premise checks above.

## 2. cfoperator (this repo) — one commit, `Refs CFOP-N`

Registry-driven (add the row, everything downstream follows):

- `agent/agent.py` `OPENAI_COMPAT_PROVIDERS`: `label`, `base_url`,
  `key_env`, and `default_model` if the request names one. The console
  (`/api/providers`, `/api/models/<b>`, the settings allowlist),
  `_resolve_provider`, the judge key lookup and `test_console_provider_registry.py`
  all iterate this dict.

Hand-copied sites that still need the row (each is asserted by a test or
would silently omit the provider):

| File | What |
|---|---|
| `web_server.py` `_PROVIDER_DESCRIPTIONS` | console blurb (falls back to `"<label> models"`) |
| `agent/test_xai_provider.py` | registry set assertion |
| `.env.example` | commented `<NAME>_API_KEY=` + the wizard provider list comment |
| `docker-compose.yml` | `<NAME>_API_KEY: ${<NAME>_API_KEY:-}` |
| `charts/cfoperator/values.yaml` / `templates/secrets.yaml` / `templates/_helpers.tpl` | `<name>ApiKey`, secret key, `$cred` dict (the dict is the `llm.backend` allowlist) |
| `setup_wizard.py` `_PROVIDERS` | wizard's accepted names |
| `config.yaml.example` | "Shipped providers" comment |
| `docs/config-reference.md` | `llm.fallback` example entry + the escalation-chain note |
| `docs/mcp-server.md`, `docs/slack-bridge.md`, `mcp_server/client.py`, `mcp_server/tools/chat.py` | `ask_sre` backend union — `test_ask_sre_backend_docs_name_every_registered_provider` enforces all four |
| `SECURITY.md`, `.github/ISSUE_TEMPLATE/bug_report.yml`, `README.md` (cfassist stubs paragraph) | prose lists |

Decisions to make explicitly, not by default:

- **Investigation `fallback_order`** in `_get_provider_chain`: adding a
  provider there changes which paid model an escalation lands on. Gemini and
  DeepSeek are deliberately out, and
  `test_gemini_is_not_in_the_investigation_fallback_chain` asserts each
  excluded name is absent from the `fallback_order` line — **add the new
  name to that test**, or the exclusion is a comment, not a guard.
- **`_JUDGE_MODEL_FLOOR`**: the mutation judge's veto holders, frontier-tier
  only, pinned in code. Adding a vendor is its own issue.

Tests: `agent/test_xai_provider.py`, `test_console_provider_registry.py`,
`test_setup_wizard.py`, `test_helm_chart.py`, `agent/test_remediation_queue.py`
(judge tests), plus `helm template` with `llm.backend=<name>`.

## 3. cfassist-go — same PR, own commit

- `internal/config/config.go` `writeDefaultConfig`: a **commented** stub
  between the existing ones (`provider`, `url`, `model`, `temperature`,
  `api_key: ${<NAME>_API_KEY}`, `context_window`). It must stay commented:
  a live stub probes a paid API with an empty key on every fresh laptop.
- `internal/config/config_test.go`: add the row to the `stubs` table in
  `TestDefaultConfigStubsRemoteProvidersCommentedOut` and to the name list
  in `TestDefaultConfigStubsDoNotNameRetiredModels`.
- Bump together: `Version` in `config.go`, `DEFAULT_CFASSIST_VERSION` in
  `cockpit_ladder.py`, `cfassist_version` example in `docs/config-reference.md`
  (`test_cockpit_ladder.py` pins all three). After the squash merge, tag
  `cfassist-vX.Y.Z` on it immediately — the Ansible pin downloads that tag.
- `cd cfassist-go && go test ./...`

## 4. Deploy side — private repos, separate PRs, **merge order matters**

1. **homelab-infra first** (restarts nothing):
   - `secrets/.env.secrets` (gitignored, local): add `<NAME>_API_KEY=`.
   - `scripts/seal-secrets.sh`: add `<NAME>_API_KEY=<NAME>_API_KEY` to the
     `cfoperator-secrets` list, run it (needs kubeseal + cluster access),
     commit the regenerated `k3s/base/apps/sealed-secrets/cfoperator-secrets.yml`.
   - `ansible/templates/cfassist-config.yaml.j2`: provider block;
     `ansible/deploy-cfassist.yml`: `<name>_api_key` var, header comment,
     `cfassist_version` pin (only once the tag exists).
2. **cfoperator-deploy second**: `cfoperator.yml` env `<NAME>_API_KEY` from
   `cfoperator-secrets`, and an `llm.fallback` entry naming the model. Under
   `strategy: Recreate`, landing this before the secret key exists is
   `CreateContainerConfigError` with nothing serving.
3. Roll out cfassist: `ansible-playbook deploy-cfassist.yml` from
   `homelab-infra/ansible`.

## 5. Close

Move the Plane issue to Done on merge; record anything that contradicted the
plan back into the issue.
