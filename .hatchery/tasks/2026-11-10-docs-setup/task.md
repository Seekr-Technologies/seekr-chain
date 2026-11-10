# Task: docs-setup

**Status**: complete
**Branch**: docs-setup
**Created**: 2026-11-10 13:54

## Objective

Set up GitHub Pages for the existing MkDocs/Mike documentation after organization-level Pages enablement.

## Summary

### Decisions

- Keep versioned documentation on Mike's `gh-pages` branch. Publish the entire branch as a GitHub Pages artifact through GitHub Actions, rather than using branch-triggered Pages builds.
- The default site remains the latest release (`latest` alias with a root redirect), not unreleased `main`. Documentation-only changes become public with the next release.
- Use the built-in `GITHUB_TOKEN`; no personal access token or new secrets. Token-created releases and branch pushes do not trigger downstream workflows/Pages builds, so the release workflow explicitly calls the reusable Docs workflow.
- Retain stable manual-release events and strict PR builds. Manual Docs runs resolve the latest published stable GitHub release and check out its tag, enabling initial bootstrap and retries without publishing unreleased changes.
- Serialize publishing from Mike's updates through Pages deployment. Gate publication on a strict build, use locked docs-only dependencies, quote release tags through environment variables, and pin new actions to commit SHAs.

### Files changed

- `.github/workflows/docs.yml`: reusable/manual triggers, release-tag checkout, strict build gate, Mike branch update, complete-site artifact, and explicit Pages deployment with scoped permissions and `github-pages` environment.
- `.github/workflows/release.yml`: directly invoke Docs after successful release creation.
- `docs/developer/contributing.md`: local documentation commands, publishing design, GitHub setup, bootstrap, and retry instructions.

### Validation

- `uv sync --locked --group docs --no-dev` and `uv run --no-sync mkdocs build --strict` passed.
- Actionlint v1.7.7 passed for both modified workflows; `git diff --check` passed.
- Isolated local Git/Mike smoke tests passed: first publication, root redirect, version retention, alias movement, repeated same-version publication, complete-site export, and a fresh checkout loading/pushing existing remote `gh-pages` history.
- No live GitHub deployment was performed; GitHub permissions and environment protection rules must be verified by the maintainer on the first run.

### Maintainer handoff / gotchas

- User reports Pages enabled. Repository Pages source must be **GitHub Actions**.
- After merge, run **Actions → Docs → Run workflow** on **main** once. A published stable release must already exist. Default URL: <https://seekr-technologies.github.io/seekr-chain/>.
- If applicable, allow the pinned actions and workflow write permissions in organization policies; permit bot writes to `gh-pages` in rulesets; permit `main` and release tags (`v*`) in `github-pages` environment rules, and approve deployments when required.
- No DNS/custom domain setup is needed. Future releases publish automatically.
- Mike does not accept `deploy --strict`; strictness is enforced in the prerequisite MkDocs build job.
