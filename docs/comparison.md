

# Comparison with Other Tools

Understanding how seekr-chain compares to other workflow orchestration tools.

## Design Philosophy

Seekr-chain's philosophy is simple and transparent:

- A workflow consists of a DAG of steps
- A step consists of: image, command, and resources
- Stay close to the underlying platform (Argo Workflows)
- Give users full control without unnecessary abstractions

## Seekr-chain vs Metaflow

### Code Directory Management

**Seekr-chain:**

- Upload code from any directory
- Full control over inclusion/exclusion patterns
- Can upload specific files, entire directories, or follow symlinks
- Explicit and transparent

**Metaflow:**

- Only uploads code from current directory
- Limited control over file inclusion/exclusion
- Cumbersome to include/exclude specific files

### Runtime Control

**Seekr-chain:**

- Choose any Docker image
- Run any command (bash, Python scripts, binaries)
- Use any Python executable or version
- Easily run setup steps before main script
- Direct support for torchrun, deepspeed, accelerate

```yaml
command: |
  pip install -r requirements.txt
  torchrun --nproc_per_node=8 train.py
```

**Metaflow:**

- Jobs must run in Python
- Limited control over runtime environment
- Using frameworks like DeepSpeed or torchrun requires opaque decorators
- Difficult to debug when things go wrong

### Interactive Debugging

**Seekr-chain:**

- Native support for interactive jobs
- `--interactive` flag drops you into a shell
- Debug in the actual job environment
- Easy to test commands and validate setup

```bash
chain submit config.yaml --interactive
```

**Metaflow:**

- Limited interactive debugging support
- More difficult to access actual job environment

### Job Monitoring

**Seekr-chain:**

- Easy to follow all pods in a job
- `--follow` flag streams logs from all pods
- Simple monitoring even for complex multi-node jobs
- Transparent pod management

**Metaflow:**

- Easy to follow main steps
- Difficult to track pods created by decorators (e.g., @deepspeed)
- Must manually search for relevant pods
- Hidden abstractions make debugging harder

### DAG Support

Both seekr-chain and Metaflow support defining workflows as DAGs with dependencies between steps.

### Multi-node Training

**Seekr-chain:**

- Straightforward multi-node configuration
- Automatic network setup and environment variables
- Works seamlessly with standard tools (torchrun, DeepSpeed)
- Transparent and debuggable

**Metaflow:**

- Requires special decorators
- More abstraction between you and the running code
- Less transparent when debugging

### Argument Passing

**Seekr-chain:**

- Currently does not automatically pass arguments between steps
- Users manage state via shared storage (PVCs, S3)
- Future support planned for JSON-based argument passing

**Metaflow:**

- Passes arguments between steps using native Python types
- Works well when it works, but can be opaque when issues arise

## Seekr-chain vs Pure Argo Workflows

**Seekr-chain:**

- Higher-level abstraction while staying close to Argo
- Automatic code packaging and upload
- Built-in support for multi-node jobs
- Environment variable configuration
- Simpler configuration format
- Python and CLI interfaces

**Pure Argo Workflows:**

- Complete control and flexibility
- More verbose configuration
- Manual setup for code upload, multi-node jobs
- Steeper learning curve
- Direct YAML manifests

Seekr-chain reduces boilerplate while maintaining transparency and keeping close to Argo's model.

## Key Advantages of Seekr-chain

1. **Transparency**: Less magic, fewer assumptions. When something breaks, it's easy to understand and debug.

2. **Control**: Choose your image, command, and resources explicitly. No hidden abstractions.

3. **Framework Agnostic**: Works with any distributed training framework without special decorators or plugins.

4. **Interactive Development**: Built-in support for debugging in the actual job environment.

5. **Simplicity**: Clean configuration format that directly maps to what actually runs.

6. **Close to Metal**: Minimal abstraction layer over Argo Workflows makes it easy to understand what's happening.

## When to Choose Seekr-chain

Seekr-chain is ideal when you:

- Need full control over your runtime environment
- Want to use standard distributed training tools
- Value transparency and debuggability
- Need to run complex multi-node jobs
- Want interactive debugging capabilities
- Prefer explicit configuration over magic

## When to Consider Alternatives

Consider other tools if you:

- Need automatic argument passing between steps in Python types (though chain will support JSON passing in the future)
- Prefer more abstraction and are comfortable with the trade-offs
- Don't need multi-node distributed training
- Are already heavily invested in another framework's ecosystem
