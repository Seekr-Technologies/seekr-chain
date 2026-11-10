# Contributing

Thank you for your interest in contributing to seekr-chain! We welcome contributions from the community.

## Getting Started

### Development Setup

1. **Install uv**

    Seekr-chain uses [uv](https://github.com/astral-sh/uv) for dependency management:

    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

2. **Clone the repository**

    ```bash
    git clone {{ git_install_url }}
    cd seekr-chain
    ```

3. **Install dependencies**

    ```bash
    uv sync
    ```

4. **Run tests**

    ```bash
    uv run pytest tests
    ```

## Development Workflow

### Making Changes

1. Create a new branch for your feature or bugfix:

    ```bash
    git checkout -b feature/your-feature-name
    ```

2. Make your changes, following the code style guidelines below

3. Add or update tests as needed

4. Run the test suite to ensure everything passes:

    ```bash
    uv run pytest tests
    ```

5. Commit your changes with clear, descriptive commit messages

### Submitting Changes

1. Push your branch to the repository
2. Create a pull request on GitHub
3. Ensure all CI checks pass
4. Wait for review from maintainers

## Code Style Guidelines

- Follow PEP 8 for Python code style
- Use type hints where appropriate
- Write clear docstrings for public functions and classes
- Keep functions focused and modular
- Add comments for complex logic

## Testing

- Write unit tests for new features
- Ensure existing tests pass
- Aim for good test coverage of critical paths
- Test edge cases and error conditions

Run tests with:

```bash
# Run all tests
uv run pytest tests

# Run specific test file
uv run pytest tests/test_config.py

# Run with coverage
uv run pytest --cov=seekr_chain tests
```

## Documentation

When adding new features:

- Update relevant documentation files
- Add docstrings to new functions and classes
- Include examples where appropriate
- Update the CHANGELOG.md

### Building documentation locally

Install the locked documentation dependencies, then preview or validate the site:

```bash
uv sync --locked --group docs --no-dev
uv run --no-sync mkdocs serve
uv run --no-sync mkdocs build --strict
```

### Publishing documentation (maintainers)

The public site is <https://seekr-technologies.github.io/seekr-chain/>.
The default **latest** version follows `main`, including documentation-only and
`no-bump:` changes. It may describe unreleased features. Release versions (for example,
**v1.2.3**) remain exact, immutable snapshots available in the version selector.
PRs receive a strict build check without publishing.

Mike stores the built versions on `gh-pages`. `latest` is an independent rolling
version, not an alias for a release. Its selector label is simply **latest**. The Docs workflow
uploads the **entire branch**, including older versions, as a GitHub Pages artifact
and deploys it to the `github-pages` environment. The root URL redirects to `latest`.
Do not configure Pages to deploy from the branch: bot pushes using `GITHUB_TOKEN`
do not trigger branch-based Pages builds.

#### One-time GitHub setup

1. In **Settings → Pages → Build and deployment**, choose **GitHub Actions**.
2. Ensure organization/repository Actions policies allow the actions used in
   `.github/workflows/docs.yml` and allow workflows to request write permissions.
   No personal access token or additional repository secret is required.
3. If the `github-pages` environment has deployment protection rules, permit
   deployments from `main` and release tags (`v*`). Approve deployments if your
   environment requires reviewers. Branch rules/rulesets must also allow the
   workflow's bot to update `gh-pages`.
4. Merging this workflow into `main` publishes **latest** automatically, initializing
   `gh-pages` if needed. No release is required. To bootstrap or retry manually,
   open **Actions → Docs → Run workflow**, select **main**, and leave `release_tag` blank.
   To add a missing historical snapshot, enter its published stable release tag
   (for example, `v1.2.3`). Existing snapshots are never overwritten.
5. Check the deployment URL in the workflow's **Deploy to GitHub Pages** job.
   No custom domain or DNS configuration is needed for the default URL.

Every push to `main` rebuilds **latest**, independently of package version bumps.
Automated releases also call the Docs workflow directly from `release.yml` to add a
snapshot built from the release tag. This is necessary because releases created with
`GITHUB_TOKEN` do not trigger other release-event workflows. Stable releases published
manually also add snapshots; prereleases are not published. Release publication never
moves **latest** back to a release snapshot.

To retry publishing, run **Docs** manually on `main`: leave `release_tag` blank for
**latest**, or supply a stable release tag to add/redeploy that snapshot. Existing
snapshots are retained unchanged, even on retries. Mike updates and Pages deployments
are serialized to avoid concurrent writes. Rolling runs resolve the current `main`
head when they start and publish the exact commit validated by the build job.

Repositories with the former `latest` release alias are migrated automatically on
the first rolling publication: only the alias is removed, preserving its release
snapshot, and a standalone version labeled **latest** replaces it.

## Continuous Integration

CI will automatically run on pull requests to:

- Execute the test suite
- Check code formatting
- Verify builds

Ensure your code passes all CI checks before requesting review.

## Getting Help

If you need help or have questions:

- Open an issue on GitHub describing your question
- Reach out to the maintainers
- Check existing issues and documentation

## Code of Conduct

- Be respectful and professional
- Provide constructive feedback
- Welcome newcomers and help them learn
- Focus on what is best for the community

## Types of Contributions

We welcome various types of contributions:

### Bug Reports

When reporting bugs, include:

- Clear description of the issue
- Steps to reproduce
- Expected vs actual behavior
- Environment details (OS, Python version, etc.)
- Relevant logs or error messages

### Feature Requests

When requesting features:

- Describe the use case clearly
- Explain why the feature would be valuable
- Provide examples if possible
- Consider implementation complexity

### Bug Fixes

- Reference the issue being fixed
- Include tests that verify the fix
- Explain the root cause if it's not obvious

### New Features

- Discuss major features in an issue first
- Include comprehensive tests
- Update documentation
- Consider backward compatibility

### Documentation

- Fix typos or unclear explanations
- Add examples
- Improve structure and organization
- Keep documentation up to date with code changes

## Release Process

Releases are triggered automatically when a PR is merged into `main`. The version bump is
determined by scanning all commit messages in the PR for conventional commit prefixes:

| Prefix | Bump | Examples |
|--------|------|---------|
| any type + `!:` | **major** | `feat!:`, `fix!:` |
| `feat:` | **minor** | `feat: add new command` |
| `fix:`, `perf:`, `refactor:`, `revert:`, `test:` | **patch** | `fix: handle empty input` |
| `ci:`, `chore:`, `docs:`, `style:`, `build:` | **skip** (no release) | `docs: update readme` |
| `no-bump:` (PR title only) | **skip** | `no-bump: internal cleanup` |

If no commits use conventional format, the PR title is used as fallback.
The highest bump level across all commits wins.

Before merging, the `preview-release` CI job posts a comment on the PR showing
the computed next version and a changelog preview. If the bump is major, the
`warn-major-bump` job fails visibly as an extra safeguard.

Release notes are maintained in `docs/developer/CHANGELOG.md`.

## License

By contributing to seekr-chain, you agree that your contributions will be licensed under the same license as the project.

## Questions?

If you have questions about contributing, feel free to open an issue or reach out to the maintainers. We're here to help!
