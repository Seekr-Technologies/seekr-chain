# Secrets Management

Secrets allow you to securely pass sensitive information like API keys, credentials, and tokens into your jobs.

## Basic Usage

Create and use secrets in your workflow configuration:

```yaml
secrets:
  - key: <value> 
steps:
  - name: train
```

Secrets are set at the `workflow` level, and are available to all steps in your workflow

On job launch, {{package}} will:

- Create secrets using `kubectl`
- Inject the secret values as environment variables in your job runtime 

!!! Warning
    
    Currently, secret values must be written directly into your job config! 
    
    When using secrets, it is recommended to dynamically generate your configuration usign the Python API.
    
    Support for securely loading secrets from your environment or other sources is comming soon.

## Secret Lifecycle Management

- On job launch, {{package}} automatically creates secrets, keyed by your job_id
- On launch, {{package}} also looks for any secrets created by {{package}} older than `7` days, and deletes them

## Using cluster secrets

Access to cluster secrets comming soon
