# Task: docs-setup

**Status**: complete
**Branch**: docs-setup
**Created**: 2026-11-10 13:54

## Objective

Set up GitHub Pages for the existing MkDocs/Mike documentation, with continuously updated documentation and preserved release snapshots.

## Summary

### Decisions

- Mike maintains the complete versioned site on `gh-pages`. GitHub Actions uploads that branch as a Pages artifact and deploys explicitly; do not use branch-triggered Pages publishing.
- **latest** is an independent rolling Mike version built from `main`, not a release alias. Its title is exactly `latest`, without a version suffix. The root URL redirects to `/latest/`.
- Every push to `main` publishes rolling docs, including documentation-only and `no-bump:` changes. Rolling docs may describe unreleased functionality.
- Published stable releases get immutable snapshots under their exact tags (e.g. `v1.2.3`). Once a snapshot exists, reruns retain it unchanged; releases never move or overwrite rolling latest. Prereleases are excluded.
- This supersedes the initial latest-release-only policy: documentation fixes should not require a package release, but exact release documentation must remain discoverable.
- Existing repositories with a `latest` release alias migrate automatically on the first rolling publication: remove only the alias, preserve its release snapshot, then deploy a standalone `latest`.
- The built-in `GITHUB_TOKEN` is sufficient. Its releases do not trigger release-event workflows, so `release.yml` directly calls the reusable Docs workflow. Manual stable-release events are also supported.
- Serialize branch updates through Pages deployment. Strictly build before publishing, publish the exact validated source SHA, use locked docs-only dependencies and scoped permissions, and pin added actions to commit SHAs. Rolling runs resolve current `main` when they start.

### Files changed

- `.github/workflows/docs.yml`: strict PR builds; rolling push/manual publication; reusable/manual stable-release snapshot publication; release validation; immutable snapshot guards; alias migration; complete-site export/artifact and Pages deployment.
- `.github/workflows/release.yml`: invoke Docs directly after successful release creation.
- `docs/developer/contributing.md`: local build commands, publishing policy, setup, bootstrap/backfill, and retry instructions.

### Validation

- Locked docs dependency sync and strict MkDocs builds passed.
- Actionlint v1.7.7 passed for both workflows; `git diff --check` passed.
- Isolated smoke tests ran the actual workflow publication script against temporary Git remotes. Verified rolling changes, lowercase latest title, immutable snapshot trees despite changed source, retries, release isolation, fresh-checkout preservation of remote versions, legacy alias migration, release-first bootstrap, and complete-site export.
- Mocked resolver checks verified main/latest and stable-tag target outputs, plus rejection of missing/nonstable release results and reserved `latest` release tags.
- No live GitHub deployment was performed. GitHub policies and environment rules must be checked on the first run.

### Maintainer handoff / gotchas

- User reports Pages enabled. Repository Pages source must remain **GitHub Actions**.
- Merging this branch publishes latest automatically, even though the commits are `no-bump:`. No package release is required.
- To retry/bootstrap rolling docs, run **Actions → Docs → Run workflow** on **main**, leaving `release_tag` blank. To backfill a historical snapshot, supply its published stable tag. Existing snapshots are retained, not rebuilt in place.
- Allow required actions and workflow write permissions in organization policies; allow bot writes to `gh-pages` in applicable rulesets; permit `main` and release tags (`v*`) in `github-pages` environment rules, and approve deployments if required.
- Default URL: <https://seekr-technologies.github.io/seekr-chain/>. No new secrets, personal access token, DNS, or custom domain setup is needed.
- Mike does not accept `deploy --strict`; the prerequisite MkDocs job enforces strict builds.
- A release may finish before the first rolling publication; `set-default --allow-undefined latest` permits this ordering. The initial latest publication supplies the root redirect's target.
