#!/usr/bin/env python3

import logging
import os
import time
from collections import defaultdict
from functools import lru_cache
from typing import Optional

import kubernetes
import kubernetes as k8s
from kubernetes.client.models import V1Pod
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


@lru_cache()
def load_kubeconfig():
    """Load kubeconfig once with a friendly error on failure."""
    try:
        kubernetes.config.load_kube_config(config_file=os.environ.get("KUBECONFIG"))
    except kubernetes.config.ConfigException as e:
        raise RuntimeError(
            f"Failed to load Kubernetes config: {e}\n\n"
            "Ensure a valid kubeconfig is available:\n"
            "  - Set the KUBECONFIG environment variable, or\n"
            "  - Place a config file at ~/.kube/config"
        ) from e


@lru_cache()
def get_core_v1_api() -> kubernetes.client.CoreV1Api:
    load_kubeconfig()
    return kubernetes.client.CoreV1Api()


@lru_cache()
def get_custom_objects_api() -> kubernetes.client.CustomObjectsApi:
    load_kubeconfig()
    return kubernetes.client.CustomObjectsApi()


def _mem_str_to_bytes(mem_str: str) -> int:
    units = ["Ki", "Mi", "Gi", "Ti"]

    try:
        out = int(mem_str)
    except ValueError:
        unit_index = units.index(mem_str[-2:])
        out = int(mem_str[:-2]) * 1024 ** (1 + unit_index)
    return out


@lru_cache()
def get_node_resources_by_gpu() -> dict[str, dict]:
    logger.info("Collecting node info")
    v1 = get_core_v1_api()
    nodes = v1.list_node().items

    # Collect node allocations
    gpu_types = {"nvidia.com/gpu", "amd.com/gpu"}
    keys = {"cpu", "memory", "gpu_type", "gpu", "ephemeral-storage"}
    node_alloc = []
    for node in nodes:
        alloc = node.status.capacity

        node_gpu_type = list(gpu_types.intersection(set(alloc.keys())))
        if len(node_gpu_type) == 1:
            alloc["gpu_type"] = node_gpu_type[0]
            alloc["gpu"] = int(alloc[alloc["gpu_type"]])
            alloc["cpu"] = int(alloc["cpu"])
            alloc["memory"] = _mem_str_to_bytes(alloc["memory"])
            alloc["ephemeral-storage"] = _mem_str_to_bytes(alloc["ephemeral-storage"])

            alloc = {k: alloc[k] for k in keys}

            # Only select nodes that have 8 GPUs
            if alloc["gpu"] == 8:
                node_alloc.append(alloc)

    # Collect by GPU type
    alloc_by_gpu = defaultdict(list)
    for node in node_alloc:
        alloc_by_gpu[node["gpu_type"]].append(node)

    # Get minimum by GPU type
    min_per_gpu = {
        gpu: {k: min(d[k] for d in gpu_data if k in d) for k in set().union(*gpu_data)}
        for gpu, gpu_data in alloc_by_gpu.items()
    }

    return min_per_gpu


def _container_is_terminated(pod, container_name):
    """
    Checks both init and regular containers to see if the specified container is terminated.
    """
    statuses = (pod.status.init_container_statuses or []) + (pod.status.container_statuses or [])
    for status in statuses:
        if status.name == container_name:
            return status.state and status.state.terminated is not None
    return False


def _wait_for_container_termination(v1_api, pod_name, namespace, container_name, timeout, poll_interval):
    """
    Polls the container status until it enters the 'terminated' state.
    Raises TimeoutError if the container does not terminate in time.
    """
    for _ in range(timeout // poll_interval):
        try:
            pod = v1_api.read_namespaced_pod(name=pod_name, namespace=namespace)
            if _container_is_terminated(pod, container_name):
                return
        except ApiException as e:
            logger.error(f"Error while checking container status: {e}")
        time.sleep(poll_interval)

    raise TimeoutError(f"Container '{container_name}' in pod '{pod_name}' did not terminate within {timeout} seconds.")


def get_container_logs(
    v1_api: k8s.client.CoreV1Api,
    pod: V1Pod,
    namespace: str,
    container_name: Optional[str] = None,
    as_list: bool = False,
    timeout: float = 60,
    poll_interval: float = 2,
):
    """
    Waits for the specified container (including initContainers) to terminate before retrieving logs.

    Parameters
    ----------
    v1_api
    pod
    """
    if container_name is None:
        container_name = pod.spec.containers[0].name

    _wait_for_container_termination(
        v1_api=v1_api,
        pod_name=pod.metadata.name,
        namespace=namespace,
        container_name=container_name,
        timeout=timeout,
        poll_interval=poll_interval,
    )

    try:
        logs = v1_api.read_namespaced_pod_log(
            name=pod.metadata.name, namespace=namespace, follow=False, container=container_name
        )
    except ApiException as e:
        raise RuntimeError(f"Failed to fetch logs from container '{container_name}' in pod '{pod.metadata.name}': {e}")

    if as_list:
        logs = logs.split("\n")

    return logs
