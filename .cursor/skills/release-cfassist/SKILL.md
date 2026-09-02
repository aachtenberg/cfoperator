---
name: release-cfassist
description: Cut a cfassist GitHub release and roll it to the fleet. Use when merging a cfassist change that should ship, bumping Version, tagging cfassist-v*, waiting for the Release cfassist workflow, or deploying with homelab-infra ansible/deploy-cfassist.yml.
---

# Release cfassist

A feature PR on `main` does not ship the standalone binary. Shipping is these
steps, in order, and skipping the tag or running the playbook before the
GitHub release exists is an outage rather than a no-op.

```
Task Progress:
- [ ] 1. Feature PR on main
- [ ] 2. Bump the three pins, merge
- [ ] 3. Tag cfassist-vX.Y.Z on that squash commit immediately
- [ ] 4. Wait until the GitHub release has assets
- [ ] 5. Pin + deploy from homelab-infra
```

Do not combine 1 and 2. #200 is the shape: the feature lands, then a bump PR
explains *why* a new tag exists.

## 1. Feature on main

Merge with squash (`gh pr merge --squash --delete-branch`). Confirm CI green.

Only the Go tree ships in the binary. A Python-only change does not need a
cfassist tag.

## 2. Bump the three pins together

`test_the_pinned_cfassist_version_tracks_the_go_tree` fails if they drift.

| Pin | File |
|---|---|
| `var Version` | `cfassist-go/internal/config/config.go` |
| `DEFAULT_CFASSIST_VERSION` | `cockpit_ladder.py` |
| `cfassist_version:` example | `docs/config-reference.md` |

Semver: patch for a fix, minor for a feature (new provider, new command).
Local `go build` reports `Version`; the release workflow overrides it from
the tag via `-ldflags`. Keep them the same number anyway.

Commit: `chore(cfassist): bump Version to X.Y.Z for <reason>`.

The cockpit *image* builds cfassist from the tree (same commit as the agent).
Tiers 3/3b in `cockpit_ladder.py` download `cfassist-v<DEFAULT_CFASSIST_VERSION>`
from GitHub. That is why the tag cannot wait.

## 3. Tag immediately after the bump merges

```bash
git fetch origin main && git checkout main && git pull
git tag cfassist-vX.Y.Z
git push origin cfassist-vX.Y.Z
```

Tag form is `cfassist-v*` — not `v*`. That is what
`.github/workflows/release-cfassist.yml` matches. Host-tier spawns 404 until
this tag exists.

## 4. Wait for assets

Watch the **Release cfassist** workflow. Do not deploy until
`gh release view cfassist-vX.Y.Z` lists `cfassist-linux-amd64`,
`cfassist-linux-arm64`, `cfassist-linux-arm`, and `checksums.txt`.

The job also refreshes the `cfassist-latest` pointer the install one-liner
uses. A failed refresh must not be worse than a stale pointer — do not delete
that release by hand.

## 5. Fleet: homelab-infra

Repo: `/home/aachten/repos/homelab-infra`. Work from `origin/main` (checkouts
here drift onto stale branches).

Bump `cfassist_version` in `ansible/deploy-cfassist.yml` (the `vars:` pin *and*
the usage comment). Touch `ansible/templates/cfassist-config.yaml.j2` only when
the release needs a config change (new provider, model rename). The playbook
**replaces** `~/.cfassist/config.yaml` on every run; a hand edit on a host
does not survive.

Commit: `ansible(cfassist): roll the fleet to X.Y.Z`. Merge that PR, then:

```bash
cd /home/aachten/repos/homelab-infra/ansible
ansible-playbook deploy-cfassist.yml --check
ansible-playbook deploy-cfassist.yml --limit raspberrypi   # smoke one host
ansible-playbook deploy-cfassist.yml                       # fleet
```

Needs `secrets/.env.secrets` (provider keys). A host already on the pin is
skipped for the binary. Override without editing the pin:
`-e cfassist_version=X.Y.Z`.

Do not run the playbook before step 4 — `get_url` 404s on a tag with no
assets.

## Verify

```bash
# one upgraded host
ssh <host> cfassist --version    # "cfassist X.Y.Z"
```

Cockpit host-tier spawn downloads the same tag. If that 404s, the bump merged
and the tag did not.
