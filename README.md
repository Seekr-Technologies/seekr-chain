# Seekr Chain

[![Documentation](https://img.shields.io/badge/docs-latest-blue)](https://seekr-technologies.github.io/seekr-chain)

No-nonsense job launcher

Currently supported backends:

- Argo Workflows

## Why Seekr-Chain?

Seekr-chain aims to make it as easy as possible to get your jobs running, without getting in your way. Chain's philosophy is simple.

- A job consists of a DAG of steps
- A step consists of:
  - image
  - command
  - resources

### Vs Metaflow:
- Code directory:
  - `chain` gives you full control over exactly which directory (and which files) are uploaded into your job runtime
  - `metaflow` only allows uploading code in the current directory, and is cumbersome to include/exclude specific files
- Runtime:
  - `chain` provides full control over the job runtime. You define your image, and you can run any command you want - bash, python (select your own executable!), anything. 
     - Easily run setup steps, such as installs, etc, before your main script
     - Easily run `torchrun`, `deepspeed`, `accelerate`, or anything else!
  - `metaflow` does not allow you to choose your runtime. You job runs in python.
     - In order to use `deepspeed`, `torchrun`, etc, complicated and opaque decorators are required
     - When things go wrong, it is hard to debug!
- Interactive jobs:
  - `chain` supports interactive jobs, allowing you to easily debug
- Job monitoring
  - `chain` makes it easy to follow all pods within a job
  - `metaflow` can easily follow steps, EXCEPT for jobsets created by decorators (e.g. `@deepspeed`). Then you have to manually search for relevant pods
- DAGs:
  - both `chain` and `metaflow` support DAGs
- Much closer to pure argo-workflows
  - Less 'stuff' between you and the running code. Less assumptions, less magic
- Arguments between steps:
  - `chain` does not (yet) pass arguments between steps for you. In the future, `chain` will support passing args between steps as json, for maximum compatability
  - `metaflow` passes args between steps in native python dtypes. When it works, it's magic. When it breaks, it's not.

## Installation

It is recommended to install as a dependency in your project environment. You can install directly from git, or as a submodule.

### Pre-reqs:

- Kubectl

  Make sure you have `kubectl` installed and configured.

### Install from PyPI

- `uv`

   ```shell
   uv add seekr-chain
   ```

- `pip`

   ```shell
   pip install seekr-chain
   ```

### Install from Git

You can use your favorite package manager to install `seekr-chain` into your development environment, directly from git:

- `uv`

   ```
   uv add git+https://github.com/seekr-technologies/seekr-chain.git@v0.5.0
   ```

- `pip`

   ```
   pip install git+https://github.com/seekr-technologies/seekr-chain.git@v0.5.0
   ```

> Tip: It is recommended to use a `@<version_tag>` to get a stable installation. See the releases page on GitHub for the latest stable version. 
> 
> You can also use `@main` to get the latest version, or `@dev` for the latest features. 

### Install as submodule

If you think you will need to modify `seekr-chain` in conjunction with your project, it may be convenient to install as an `editable submodule`.

- In your repo, create a `submods` directory: `mkdir submods && cd submods`
- Clone repository: `git clone https://github.com/seekr-technologies/seekr-chain.git`
- Add as submodule: `git submodule add ./seekr-chain`
- Add as editable dependency:
  - `uv`:
  
     `uv pip install -e ./seekr-chain`

  - `pip`

     `pip install -e ./seekr-chain`

### Install as `uv tool`

This makes `chain` available everywhere

``` shell
uv tool install seekr-chain
```

## Usage

`seekr-chain` allows you to define an arbitrary workflow, specified completely by config.

### Workflow Config

The Workflow Config is defined and validated as a [pydantic](https://docs.pydantic.dev/latest/) DataModel. As such, just by viewing the config definition, you can see a full definition and documentation of all options.

For the config definition, see the [Configuration Reference](https://seekr-technologies.github.io/seekr-chain/reference/configuration/). The main config is the `WorkflowConfig`

### Python API

You can easily construct and launch jobs in python.

```python
import seekr_chain

# Define the job config
config = {...}

# Launch the workflow
workflow = seekr_chain.launch_argo_workflow(config)    # Returns an `ArgoWorkflow` object

# follow the workflow, printing logs and workflow status
workflow.follow()

# Alternatively, wait for workflow to complete
seekr_chain.wait(workflow)
```

### CLI

Define a config in any of the supported languages, and run the job with:

```
chain submit <path_to_config>
```

You can also use the `-f/--follow` flag to follow the workflow.

Supported CLI config formats:
	
- `yaml`

## Examples

Multiple examples can be found in the [examples](./examples) directory. Each example can be run with

`chain submit examples/.../config.yaml --follow`

## Features

- **Multinode jobs**: Easily run high performance multi-node jobs, just by specifying `num_nodes` for a given step.
  
  `chain` will ensure all pods in a step can communicate, making multi-node training a breeze
  
- **DAGs**: String together arbitrary steps in a DAG
- **Code Upload**: Easily upload code from any directory for your job, with full control over inclusion/exclusion rules
- **Persistent Volume Claims**: Attach to or create PVCs for your jobs
- **Secrets**: Securely pass secrets into jobs
- **Interactive jobs**: Simply specify `--interactive` to the CLI, or `interactive=True` in python.

   Chain will launch your job, and automatically drop you in a shell in your job when it starts.

   ```
   chain submit examples/0_hello_world/config.yaml --interactive                             
	2025-09-18 11:00:21.985     INFO seekr_chain Packaging assets: None
	2025-09-18 11:00:21.986     INFO seekr_chain Uploading assets to s3://seekr-ml-taw/seekr-chain/54/c72ca3-9172-461f-8315-2c9c15ebd696.tar.gz
	2025-09-18 11:00:22.122  WARNING seekr_chain Setting auto-timeout of 1 hour
	2025-09-18 11:00:23.459     INFO seekr_chain Uploaded workflow secrets:
	  AWS_ACCESS_KEY_ID
	  AWS_SECRET_ACCESS_KEY
	Launched argo workflow: hello-world-nt7y6d
	2025-09-18 11:00:28.220     INFO seekr_chain Waiting for job to start hello-world-nt7y6d
	2025-09-18 11:00:34.884     INFO seekr_chain Connecting
	
	       ________  _____    _____   __
	      / ____/ / / /   |  /  _/ | / /
	     / /   / /_/ / /| |  / //  |/ /
	    / /___/ __  / ___ |_/ // /|  /
	    \____/_/ /_/_/  |_/___/_/ |_/
	    
	
	
	    Argo Workflow Name: hello-world-nt7y6d
	
	    Type `c-d` to exit this shell
	
	    To run this job, use `/seekr-chain/entrypoint.sh`
	    
	Defaulted container "trainer" out of: trainer, download-assets (init), unpack-assets (init), create-hostfile (init)
	root@oke-trn-01-ngwgf6vcrhq-1:/seekr-chain/workspace# 
   ```

## Environment Variables

Chain provides the following evars:

| Evar | Description |
|---|---|
| `GPUS_PER_NODE`  | Number of GPUs per node
| `HOSTNAME`  | Hostname of current pod, usually the same as the node. For a unique DNS name, use `SEEKR_CHAIN_POD_INSTANCE_ID`
| `HOSTFILE` | Deepspeed-style hostfile, wiht the hostname and num slots per node
| `MASTER_ADDR`  | Master addr for distributed comm
| `MASTER_PORT`  | Master port for distributed comm
| `NNODES`  | Number of nodes in set
| `NODE_RANK`  | Rank of this node in set
| `SEEKR_CHAIN_WORKFLOW_ID`  | ID of the overall workflow (shared across all steps)
| `SEEKR_CHAIN_JOBSET_ID`  | ID of the current step/jobset
| `SEEKR_CHAIN_POD_ID`  | Stable ID of the current pod in step/jobset 
| `SEEKR_CHAIN_POD_INSTANCE_ID`  | Unique ID of the current pod, unique across restarts/completions

## Roadmap
- Live code sync for `interactive` jobs
- Expanded backend support:
  - Local
  - Slurm
- Basic result passing

## Developer/Contributing

### Developer install

- Install `uv`
- Clone repository
- Run `uv sync`
- Run tests with `uv run pytest tests`

Contributions welcome.

CI will be set up to run unittests on PR

### Commit conventions

Releases are triggered automatically on merge to `main`. The version bump is determined
by the highest-priority conventional commit prefix found in the MR's commits:

| Prefix | Bump |
|--------|------|
| `feat!:`, `fix!:`, any `!:` | major |
| `feat:` | minor |
| `fix:`, `perf:`, `refactor:`, `revert:`, `test:` | patch |
| `ci:`, `chore:`, `docs:`, `style:`, `build:` | none — no release |

If no commits use conventional format, no release is triggered.

**Choosing the right prefix:** `feat:` means a user-facing feature that warrants a minor
version bump. Changes that only affect CI, build infrastructure, dev tooling, or test
scaffolding should use `ci:` or `chore:` even if they touch production code — the test is
whether an end-user would notice the change.

## Changelog

See [changelog](./docs/developer/CHANGELOG.md)
