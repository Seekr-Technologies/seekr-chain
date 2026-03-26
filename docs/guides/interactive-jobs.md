# Interactive Jobs

Interactive mode allows you to debug and develop directly inside your job environment. This is invaluable for troubleshooting environment issues, testing commands, and exploring the runtime.

## Launching Interactive Jobs

### Using the CLI

Add the `--interactive` flag when submitting a workflow:

```bash
chain submit config.yaml --interactive
```

### Using the Python API

Set `interactive=True` when launching:

```python
import seekr_chain

config = {...}
workflow = seekr_chain.launch_argo_workflow(config, interactive=True)
```

## What Happens

When you launch an interactive job, chain will:

1. Submit the workflow to Argo
2. Wait for the pod to start
3. Automatically connect you to a shell inside the container
4. Display helpful information about the environment

You'll see output like:

```
   ________  _____    _____   __
  / ____/ / / /   |  /  _/ | / /
 / /   / /_/ / /| |  / //  |/ /
/ /___/ __  / ___ |_/ // /|  /
\____/_/ /_/_/  |_/___/_/ |_/


Argo Workflow Name: my-workflow-abc123

Type `c-d` to exit this shell

To run this job, use `/seekr-chain/entrypoint.sh`

root@pod-name:/seekr-chain/workspace#
```

## Working in the Interactive Shell

### Exploring the Environment

```bash
# Check your code is present
ls -la

# Verify Python packages
pip list

# Check GPU availability
nvidia-smi

# Inspect environment variables
printenv | grep SEEKR_CHAIN
```

### Testing Commands

Before committing to a long-running job, test your commands interactively:

```bash
# Test your training command
python train.py --dry-run

# Test distributed setup
torchrun --nproc_per_node=1 train.py --epochs 1
```

### Running the Job

If your job has a command defined in the config, you can run it manually:

```bash
/seekr-chain/entrypoint.sh
```

This executes the command specified in your configuration.

## Exiting the Shell

To exit the interactive session:

- Type `exit` or press `Ctrl+D`
- The pod will continue running briefly, then terminate
- The workflow will complete

## Common Use Cases

### Debugging Import Errors

```bash
# Test imports interactively
python -c "import torch; print(torch.__version__)"
python -c "import transformers; print(transformers.__version__)"

# Check for missing dependencies
pip install missing-package
```

### Validating Data Paths

```bash
# Check mounted volumes
ls /data
ls /checkpoints

# Verify data is accessible
python -c "import os; print(os.listdir('/data'))"
```

### Testing Multi-node Setup

```bash
# Check distributed environment variables
echo $MASTER_ADDR
echo $MASTER_PORT
echo $NODE_RANK
echo $NNODES

# Verify hostfile
cat $HOSTFILE
```

### Experimenting with Configurations

```bash
# Try different hyperparameters
python train.py --learning-rate 0.001 --batch-size 32

# Test different configurations
deepspeed --num_gpus 1 train.py --deepspeed ds_config.json
```

## Tips and Best Practices

### Save Your Work

Interactive sessions are ephemeral. Save important outputs to persistent storage:

```bash
# Save to mounted PVC
cp results.json /checkpoints/

# Or upload to S3
aws s3 cp results.json s3://my-bucket/
```

### Multi-node Interactive Jobs

Not yet supported

## Limitations

- Only one interactive shell per workflow
- Not suitable for production workflows

## Debugging Workflow

A typical debugging workflow with interactive jobs:

1. Launch job in interactive mode
1. Explore the environment and verify setup
1. Test commands and fix issues
1. Exit the interactive session
1. Update your configuration based on findings
1. Submit the final production job

Interactive mode bridges the gap between local development and production execution, making it much easier to get your jobs running correctly.
