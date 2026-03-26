# seekr-chain

A no-nonsense job launcher for running distributed workloads on Kubernetes.

## What is seekr-chain?

Seekr-chain makes it easy to launch and manage complex computational workflows on Kubernetes without getting in your way. Define your jobs with simple configuration files and let chain handle the orchestration.

## Core Concepts

**Workflows** consist of a directed acyclic graph (DAG) of **Steps**. Each step is defined by:

- **Image**: Your container runtime environment
- **Command**: The command to execute
- **Resources**: CPU, GPU, and memory requirements

## Key Features

- **Simple Configuration**: Define entire workflows in YAML or Python
- **Multi-node Jobs**: Run distributed training across multiple nodes with automatic communication setup
- **Interactive Mode**: Debug jobs interactively with shell access
- **Flexible Code Upload**: Full control over which files are included in your job runtime
- **DAG Support**: Chain together dependent steps in complex workflows
- **Resource Management**: Attach persistent volume claims and manage secrets securely

## Quick Example

```yaml
steps:
  - name: train
    image: pytorch/pytorch:latest
    command: python train.py
    resources:
      gpus: 8
      num_nodes: 4
```

```bash
chain submit config.yaml --follow
```

## Why seekr-chain?

Chain gives you direct control over your job execution environment. Unlike frameworks that abstract away the runtime, chain lets you:

- Choose exactly which code to upload
- Define custom Docker images
- Run any command (bash, Python, torchrun, deepspeed)
- Debug with interactive sessions
- Monitor all pods easily

Chain stays close to the underlying Argo Workflows while providing just enough abstraction to make your life easier.

## Getting Started

Ready to launch your first job? Head over to the [Installation](installation.md) guide, then check out [Getting Started](getting-started.md). For the full list of configuration options, see the [Configuration Reference](reference/configuration.md).
