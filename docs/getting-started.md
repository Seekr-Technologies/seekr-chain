# Getting Started

## Usage

`seekr-chain` allows you to define an arbitrary workflow, specified completely by config.

### Workflow Config

The Workflow Config is defined and validated as a [pydantic](https://docs.pydantic.dev/latest/) DataModel. As such, just by viewing the config definition, you can see a full definition and documentation of all options.

For the config definition, see the [Configuration Reference](reference/configuration.md). The main config is the [`WorkflowConfig`](reference/configuration.md#workflowconfig)

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

To run locally without a cluster (useful for debugging):

```
chain submit <path_to_config> --backend local
```

Supported CLI config formats:
	
- `yaml`

