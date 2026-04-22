# Features

All features below are configured via the [Configuration Reference](reference/configuration.md).

- **Multinode jobs**: Easily run high performance multi-node jobs, just by specifying `num_nodes` for a given step.
  
    `chain` will ensure all pods in a step can communicate, making multi-node training a breeze
    
- **Multi-role jobs**: Run multiple roles simultaneously, such as worker and server pods
- **DAGs**: String together arbitrary steps in a DAG
- **Code Upload**: Easily upload code from any directory for your job, with full control over inclusion/exclusion rules
- **Persistent Volume Claims**: Attach to or create PVCs for your jobs
- **Secrets**: Securely pass secrets into jobs
- **Job queue admission (Kueue)**: Assign workflows to a Kueue `LocalQueue` via the `scheduling.queue` field.
  Optionally set a priority class with `scheduling.priority`. The config is backend-agnostic — a future
  SLURM backend will map the same fields to `--partition` / `--qos`.

- **Interactive jobs**: Simply specify `--interactive` to the CLI, or `interactive=True` in python.

   Chain will launch your job, and automatically drop you in a shell in your job when it starts.

   ```
   chain submit examples/0_hello_world/config.yaml --interactive                             
	2025-09-18 11:00:21.985     INFO seekr_chain Packaging assets
	2025-09-18 11:00:21.986     INFO seekr_chain Uploading assets
	2025-09-18 11:00:22.122  WARNING seekr_chain Setting auto-timeout of 1 hour
	2025-09-18 11:00:23.459     INFO seekr_chain Uploaded workflow secrets:
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
	root@host:/seekr-chain/workspace# 
   ```
