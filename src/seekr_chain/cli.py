#!/usr/bin/env python3

import os

import click

from seekr_chain.utils import format_timestamp
from seekr_chain.workflow import Backend


def _load_config(path):
    if path.suffix == ".yaml":
        import yaml

        with open(path, "r") as f:
            config_dict = yaml.safe_load(f)
    else:
        raise NotImplementedError(f"Unable to read config type: {path.suffix}")

    return config_dict


def _resolve_code_path(config, path):
    if config.code and config.code.path:
        if not config.code.path.startswith("/"):
            config.code.path = str(path.absolute().parent / config.code.path)

    return config


@click.group
@click.version_option()
def main():
    pass


@main.command()
@click.argument("config")
@click.option("-f", "--follow", is_flag=True, help="Follow job")
@click.option("-i", "--interactive", is_flag=True, help="Run interactively")
@click.option("-n", "--namespace", default=None, help="Override the namespace from the config")
@click.option(
    "-b",
    "--backend",
    default="argo",
    type=click.Choice([b.value.lower() for b in Backend], case_sensitive=False),
    help="Execution backend (default: argo)",
)
def submit(config, follow, interactive, namespace, backend):
    """
    Submit a job
    """
    from pathlib import Path

    import seekr_chain

    config_path = Path(config)

    config_dict = _load_config(config_path)

    config = seekr_chain.WorkflowConfig.model_validate(config_dict)
    config = _resolve_code_path(config, config_path)

    if namespace:
        config.namespace = namespace

    job = seekr_chain.launch_workflow(config, interactive=interactive, backend=backend)

    if follow and not interactive:
        job.follow()


@main.command()
@click.argument("JOB_ID")
@click.option("-s", "--step", help="Print logs from given step")
@click.option("-r", "--role", help="Print logs from give role")
@click.option(
    "-p",
    "--pod-index",
    help="Pod index",
    default="0",
)
@click.option("-a", "--attempt", type=click.INT, help="Attempt number", default="-1")
@click.option("-t", "--timestamps", is_flag=True, help="Print timestamps")
@click.option("-f", "--follow", is_flag=True, help="Follow logs from a running workflow")
@click.option("--all-replicas", is_flag=True, help="Follow logs from all replicas (use with --follow)")
def logs(job_id, step, role, pod_index, attempt, timestamps, follow, all_replicas):
    if follow:
        import seekr_chain

        workflow = seekr_chain.ArgoWorkflow(id=job_id)
        if workflow.get_status().is_finished():
            from seekr_chain.print_logs import print_logs

            print_logs(job_id, step, role, pod_index, attempt, timestamps)
        else:
            workflow.follow(all_replicas=all_replicas)
    else:
        from seekr_chain.print_logs import print_logs

        print_logs(job_id, step, role, pod_index, attempt, timestamps)


@main.command()
@click.argument("JOB_ID")
def status(job_id):
    """Show the status of a workflow."""
    import seekr_chain

    workflow = seekr_chain.ArgoWorkflow(id=job_id)
    click.echo(f"{workflow.get_status().value} : {job_id}")
    click.echo(workflow.format_state(workflow.get_detailed_state()))


@main.command()
@click.argument("JOB_ID")
@click.option("--poll-interval", type=click.INT, default=10, help="Polling interval in seconds")
def wait(job_id, poll_interval):
    """Wait for a workflow to complete."""
    import sys

    import seekr_chain

    workflow = seekr_chain.ArgoWorkflow(id=job_id)
    status = seekr_chain.wait(workflow, poll_interval=poll_interval)
    click.echo(f"{status.value} : {job_id}")
    if status.is_failed():
        sys.exit(1)


@main.command()
@click.argument("JOB_ID")
def attach(job_id):
    """Attach to an interactive workflow."""
    import seekr_chain

    workflow = seekr_chain.ArgoWorkflow(id=job_id)
    workflow.attach()


_STATUS_STYLES = {
    "Running": "cyan",
    "Pending": "yellow",
    "Succeeded": "green",
    "Failed": "red",
    "Error": "red",
    "Terminated": "red",
    "Omitted": "dim",
    "Skipped": "dim",
}


@main.command(name="list")
@click.option("-n", "--namespace", default=None, help="Kubernetes namespace (default: from kubeconfig context)")
@click.option(
    "-l", "--limit", type=click.INT, default=20, show_default=True, help="Most recent workflows to show (0 = no limit)"
)
@click.option("-u", "--user", default=None, help="Filter workflows by user (default: current $USER)")
@click.option("-A", "--all-users", is_flag=True, help="Show workflows from all users")
def list_cmd(namespace, limit, user, all_users):
    """List workflows."""
    from rich.box import SIMPLE_HEAD
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    import seekr_chain

    if all_users:
        effective_user = None
    elif user is not None:
        effective_user = user
    else:
        effective_user = os.environ.get("USER")

    workflows = seekr_chain.list_workflows(namespace=namespace, user=effective_user)
    workflows.sort(key=lambda wf: wf["created"])

    if limit:
        running = [wf for wf in workflows if wf["status"] in ("Running", "Pending")]
        finished = [wf for wf in workflows if wf["status"] not in ("Running", "Pending")]
        workflows = finished[-limit:] + running

    table = Table(box=SIMPLE_HEAD, pad_edge=False)
    table.add_column("ID")
    table.add_column("Job Name")
    table.add_column("User")
    table.add_column("Status")
    table.add_column("Created")
    table.add_column("Duration", justify="right")

    for wf in workflows:
        status_text = Text(wf["status"], style=_STATUS_STYLES.get(wf["status"], ""))
        table.add_row(
            wf["name"], wf["job_name"], wf["user"], status_text, format_timestamp(wf["created"]), wf["duration"]
        )

    Console().print(table)


@main.command()
@click.argument("JOB_IDS", nargs=-1, required=True)
def delete(job_ids):
    """Delete one or more workflows."""
    import seekr_chain

    for job_id in job_ids:
        workflow = seekr_chain.ArgoWorkflow(id=job_id)
        workflow.delete()
        click.echo(f"Deleted: {job_id}")


@main.command()
@click.argument("JOB_IDS", nargs=-1, required=True)
def cancel(job_ids):
    """Cancel one or more workflows without deleting them."""
    import seekr_chain

    for job_id in job_ids:
        workflow = seekr_chain.ArgoWorkflow(id=job_id)
        workflow.cancel()
        click.echo(f"Cancelled: {job_id}")


if __name__ == "__main__":
    main()
