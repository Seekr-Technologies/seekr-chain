# Contributing Guide

## Commits

This project uses **[Conventional Commits](https://www.conventionalcommits.org/)**. The CI reads every commit in a PR to decide the release version and what goes in the changelog.

### Version bump rules

| Prefix | Version bump | Appears in changelog |
|--------|-------------|----------------------|
| `<any type>!:` | **major** | yes |
| `feat:` | minor | yes |
| `fix:` | patch | yes |
| `perf:` | patch | yes |
| `refactor:` | patch | yes |
| `revert:` | patch | yes |
| `test:` | patch | yes |
| `docs:` | none | no |
| `chore:` | none | no |
| `no-bump:` | **skips release entirely** | no |

The `!` breaking-change marker (e.g. `feat!:`, `fix!:`) bumps the **major** version regardless of type.

If **any** commit in a PR is prefixed `no-bump:`, the entire PR is excluded from releasing — use this for dependency updates, CI tweaks, and similar housekeeping.

### Guidance for agents and contributors

- **Non-conventional commits are fine when appropriate.** WIP commits, review-response commits, and other in-progress work don't need a prefix. Squash or amend before the PR is merged if needed.
- **Do not use `fix:` for mistakes introduced on the current branch.** A `fix:` commit implies a bug that existed in a previous release. If you made an error earlier in the same branch, amend or squash — don't create a `fix:` that will show up in the changelog for something users never saw.

**Examples:**
```
feat: add scheduling config for Kueue queue assignment
fix: correct memory calculation for multi-GPU nodes
feat!: remove deprecated WorkflowConfig.kueue field
no-bump: bump the all-dependencies group
```
