# Roadmap

Future development plans and features under consideration for seekr-chain.

## Planned Features

### Live Code Sync for Interactive Jobs

Enable real-time code synchronization during interactive debugging sessions, allowing you to edit code locally and have changes immediately reflected in the running job.

**Status**: Planned

### Result Passing Between Steps

Add support for passing data between workflow steps using JSON format for maximum compatibility.

**Status**: Planned

**Details:**

- Structured data passing via JSON
- Type validation
- Automatic serialization/deserialization
- Preserve compatibility with various data types

### Additional Backend Support

Expand beyond Kubernetes/Argo to support additional execution backends.

#### Local Execution

Run workflows locally for development and testing without requiring a Kubernetes cluster.

**Status**: Implemented — use `chain submit --backend local` or `seekr_chain.launch_workflow(config, backend="local")`

**Use cases:**

- Local development and debugging
- CI/CD testing
- Quick prototyping

#### Slurm Support

Execute workflows on HPC clusters using Slurm as the backend.

**Status**: Under consideration

**Use cases:**

- Traditional HPC environments
- Academic research clusters
- Legacy infrastructure

## Feature Requests

Have an idea for a feature? We'd love to hear it!

- Open an issue on GitHub with the `enhancement` label
- Describe the use case and expected behavior
- Explain why it would be valuable
- Provide examples if possible

## Contributing to the Roadmap

If you're interested in working on any of these features, please:

1. Check existing issues and discussions
2. Open an issue to discuss the implementation approach
3. Get feedback from maintainers before starting major work
4. See [Contributing](developer/contributing.md) for development guidelines

## Priority and Timeline

Feature prioritization is based on:

- Community demand and feedback
- Implementation complexity
- Alignment with core philosophy
- Available development resources

Specific timelines are not provided as development is community-driven. Features marked as "Planned" are more likely to be implemented sooner than those "Under consideration."

## Long-term Vision

Seekr-chain aims to remain a lightweight, transparent, and flexible workflow orchestration tool that:

- Stays close to underlying execution platforms
- Provides just enough abstraction to be helpful
- Gives users full control over their runtime
- Supports multiple execution backends
- Maintains simplicity and debuggability

## Stability Promise

While adding new features, we commit to:

- Maintaining backward compatibility where possible
- Clearly documenting breaking changes
- Following semantic versioning
- Providing migration guides for major changes

## Feedback

Your feedback helps shape the roadmap. Let us know what features matter most to you:

- Open issues for feature requests
- Comment on existing roadmap items
- Share your use cases and pain points
- Contribute implementations

The roadmap evolves based on community needs and contributions.
