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

Releases are triggered automatically when an MR is merged into `main`. The version bump is
determined by scanning all commit messages in the MR for conventional commit prefixes:

| Prefix | Bump | Examples |
|--------|------|---------|
| any type + `!:` | **major** | `feat!:`, `fix!:` |
| `feat:` | **minor** | `feat: add new command` |
| `fix:`, `perf:`, `refactor:`, `revert:`, `test:` | **patch** | `fix: handle empty input` |
| `ci:`, `chore:`, `docs:`, `style:`, `build:` | **skip** (no release) | `docs: update readme` |
| `no-bump:` (MR title only) | **skip** | `no-bump: internal cleanup` |

If no commits use conventional format, the MR title is used as fallback.
The highest bump level across all commits wins.

Before merging, the `preview-release` CI job posts a comment on the MR showing
the computed next version and a changelog preview. If the bump is major, the
`warn-major-bump` job fails visibly as an extra safeguard.

Release notes are maintained in `docs/developer/CHANGELOG.md`.

## License

By contributing to seekr-chain, you agree that your contributions will be licensed under the same license as the project.

## Questions?

If you have questions about contributing, feel free to open an issue or reach out to the maintainers. We're here to help!
