"""Independent 401 recovery through the real provider, generated APIs and HTTP."""

import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import kubernetes
import pytest
import urllib3

from seekr_chain import k8s_api
from seekr_chain.k8s_api import kube


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    state = SimpleNamespace(accepted="A", status=None, callback=None, requests=[], watch_error=False)
    token = tmp_path / "token"
    token.write_text("A")

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            record = (self.command, self.path, dict(self.headers), body)
            state.requests.append(record)
            if state.callback:
                state.callback(record)
            accepted = getattr(state, "accepted_header", None) or "Bearer " + state.accepted
            status = state.status or (200 if self.headers.get("Authorization") == accepted else 401)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if status != 200:
                result = {"kind": "Status", "code": status, "reason": "Rejected"}
            elif parse_qs(urlsplit(self.path).query).get("watch") == ["True"]:
                result = (
                    {
                        "type": "ERROR",
                        "object": {
                            "kind": "Status",
                            "code": 401,
                            "reason": "Unauthorized",
                            "message": "synthetic rejection",
                        },
                    }
                    if state.watch_error
                    else {
                        "type": "ADDED",
                        "object": {
                            "kind": "Pod",
                            "apiVersion": "v1",
                            "metadata": {"name": "test", "resourceVersion": "1"},
                        },
                    }
                )
                if state.watch_error == "once":
                    state.watch_error = False
                    state.accepted = state.token.read_text()
            elif self.path.split("?")[0].endswith("/log"):
                self.wfile.write(b"log line\n")
                return
            else:
                result = {"kind": "PodList", "apiVersion": "v1", "metadata": {}, "items": []}
            self.wfile.write(json.dumps(result).encode() + b"\n")

        do_GET = handle_request
        do_POST = handle_request
        do_PATCH = handle_request
        do_DELETE = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / "config"
    data = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "active",
        "contexts": [{"name": "active", "context": {"cluster": "cluster", "user": "user", "namespace": "ns"}}],
        "clusters": [{"name": "cluster", "cluster": {"server": f"http://127.0.0.1:{server.server_port}"}}],
        "users": [{"name": "user", "user": {"tokenFile": "token"}}],
    }
    path.write_text(json.dumps(data))
    monkeypatch.setenv("KUBECONFIG", str(path))
    state.path, state.token, state.data = path, token, data
    yield state
    # The requests are finished (including explicitly closed streams).
    for name in ("core_v1", "batch_v1", "custom_objects"):
        api = kube.__dict__.get(name)
        if api is not None:
            api.api_client.close()
            api.api_client.rest_client.pool_manager.clear()
    server.shutdown()
    server.server_close()
    thread.join()


def operation(api, **kwargs):
    kwargs.setdefault("_preload_content", False)
    if isinstance(api, kubernetes.client.CoreV1Api):
        response = api.list_namespaced_pod("ns", **kwargs)
    elif isinstance(api, kubernetes.client.BatchV1Api):
        response = api.list_namespaced_job("ns", **kwargs)
    else:
        response = api.list_namespaced_custom_object("example.org", "v1", "widgets", "ns", **kwargs)
    if kwargs.get("async_req"):
        response = response.get(timeout=10)
    try:
        assert response.status == 200
    finally:
        response.close()


def rotate(endpoint, value="B"):
    new = endpoint.token.with_name("replacement")
    new.write_text(value)
    new.replace(endpoint.token)
    endpoint.accepted = value


@pytest.mark.parametrize("name", ["core_v1", "batch_v1", "custom_objects"])
@pytest.mark.parametrize("disable_native_hook", [False, True])
def test_rotation_race_and_native_reload(name, disable_native_hook, endpoint, monkeypatch):
    api = getattr(kube, name)
    configuration = api.api_client.configuration
    transport = api.api_client.rest_client
    reloads = []
    original = k8s_api.KubeConfigLoader.load_and_set

    def public_load(*args, **kwargs):
        reloads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(k8s_api.KubeConfigLoader, "load_and_set", public_load)
    if disable_native_hook:
        configuration.refresh_api_key_hook = None
        rotate(endpoint)
    else:
        # Rotation occurs after authentication headers were built.
        endpoint.callback = lambda _: rotate(endpoint) if len(endpoint.requests) == 1 else None
    operation(api)
    assert [r[2]["authorization"] for r in endpoint.requests] == ["Bearer A", "Bearer B"]
    assert reloads == [1]
    assert getattr(kube, name) is api
    assert api.api_client.rest_client is transport
    assert api.api_client.last_response.status == 200
    operation(api)
    assert len(reloads) == 1
    assert endpoint.requests[-1][2]["authorization"] == "Bearer B"


def test_unmodified_native_loader_used_initially_and_on_recovery(endpoint, monkeypatch):
    types = []
    original = k8s_api.KubeConfigLoader.load_and_set

    def record(loader, configuration):
        types.append(type(loader))
        return original(loader, configuration)

    monkeypatch.setattr(k8s_api.KubeConfigLoader, "load_and_set", record)
    api = kube.core_v1
    rotate(endpoint)
    operation(api)
    assert types == [k8s_api.KubeConfigLoader, k8s_api.KubeConfigLoader]


@pytest.mark.parametrize("status,attempts", [(401, 2), (403, 1), (404, 1), (500, 1)])
def test_retry_is_bounded_and_only_for_401(status, attempts, endpoint):
    endpoint.status = status
    with pytest.raises(kubernetes.client.ApiException) as error:
        operation(kube.core_v1)
    assert error.value.status == status
    assert len(endpoint.requests) == attempts


@pytest.mark.parametrize("method", ["create", "patch", "delete"])
def test_repeatable_mutation_preserves_request(method, endpoint):
    api = kube.custom_objects
    endpoint.callback = lambda _: rotate(endpoint) if len(endpoint.requests) == 1 else None
    args = ("example.org", "v1", "ns", "widgets")
    if method == "create":
        response = api.create_namespaced_custom_object(*args, {"metadata": {"name": "fixed"}}, _preload_content=False)
    elif method == "patch":
        response = api.patch_namespaced_custom_object(
            *args, "fixed", {"spec": {"suspend": True}}, _preload_content=False
        )
    else:
        response = api.delete_namespaced_custom_object(
            *args, "fixed", body={"gracePeriodSeconds": 0}, _preload_content=False
        )
    response.close()
    first, second = endpoint.requests
    assert first[0:2] == second[0:2]
    assert first[3] == second[3]
    assert first[2]["Content-Type"] == second[2]["Content-Type"]


def test_async_and_caller_headers_survive_recovery(endpoint):
    api = kube.core_v1
    api.api_client.configuration.refresh_api_key_hook = None
    api.api_client.default_headers["X-Caller"] = "preserved"
    api.api_client.cookie = "session=preserved"
    rotate(endpoint)
    operation(api, async_req=True, _request_timeout=(2, 5))
    assert len(endpoint.requests) == 2
    for _, _, headers, _ in endpoint.requests:
        assert headers["X-Caller"] == "preserved"
        assert headers["Cookie"] == "session=preserved"


def test_concurrent_clients_share_one_reload(endpoint, monkeypatch):
    apis = [kube.core_v1, kube.batch_v1, kube.custom_objects]
    apis[0].api_client.configuration.refresh_api_key_hook = None
    rotate(endpoint)
    barrier = threading.Barrier(6)
    endpoint.callback = lambda r: barrier.wait(timeout=10) if r[2].get("authorization") == "Bearer A" else None
    original = k8s_api.KubeConfigLoader.load_and_set
    loads = []

    def public_load(*args, **kwargs):
        loads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(k8s_api.KubeConfigLoader, "load_and_set", public_load)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(operation, apis * 2))
    assert loads == [1]
    assert len(endpoint.requests) == 12


@pytest.mark.parametrize("mode", ["watch", "logs"])
def test_stream_handshake_and_reconnect(mode, endpoint):
    api = kube.core_v1
    api.api_client.configuration.refresh_api_key_hook = None
    transport = api.api_client.rest_client
    for i in range(2):
        rotate(endpoint, f"rotation-{i}")
        if mode == "watch":
            events = list(kubernetes.watch.Watch().stream(api.list_namespaced_pod, "ns", timeout_seconds=1))
            assert len(events) == 1
        else:
            response = api.read_namespaced_pod_log("pod", "ns", follow=True, _preload_content=False)
            assert b"log line" in response.read()
            response.close()
        # Independently exercise recovery with and without the native hook.
        api.api_client.recovery.current.refresh_api_key_hook = None
    assert len(endpoint.requests) == 4
    assert api.api_client.rest_client is transport


def test_established_stream_survives_another_requests_recovery(endpoint):
    api = kube.core_v1
    stream = api.read_namespaced_pod_log("pod", "ns", follow=True, _preload_content=False)
    api.api_client.configuration.refresh_api_key_hook = None
    rotate(endpoint)
    operation(api)
    assert b"log line" in stream.read()
    stream.close()


def test_watch_error_event_is_not_replayed(endpoint):
    endpoint.watch_error = True
    with pytest.raises(kubernetes.client.ApiException) as error:
        list(kubernetes.watch.Watch().stream(kube.core_v1.list_namespaced_pod, "ns", timeout_seconds=1))
    assert error.value.status == 401
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize("change", ["server", "namespace", "user", "token-path", "inline", "tls", "provider"])
def test_recovery_rejects_selected_policy_changes(change, endpoint):
    api = kube.core_v1
    if change == "server":
        endpoint.data["clusters"][0]["cluster"]["server"] = "http://127.0.0.1:1"
    elif change == "namespace":
        endpoint.data["contexts"][0]["context"]["namespace"] = "other"
    elif change == "user":
        endpoint.data["contexts"][0]["context"]["user"] = "other"
    elif change == "token-path":
        endpoint.data["users"][0]["user"]["tokenFile"] = "other-token"
    elif change == "inline":
        endpoint.data["users"][0]["user"]["token"] = "DO-NOT-EXPOSE"
    elif change == "tls":
        endpoint.data["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    else:
        endpoint.data["users"][0]["user"]["auth-provider"] = {"name": "oidc"}
    endpoint.path.write_text(json.dumps(endpoint.data))
    endpoint.status = 401
    with pytest.raises(RuntimeError, match="cannot safely reload") as error:
        operation(api)
    assert "DO-NOT-EXPOSE" not in str(error.value)
    assert len(endpoint.requests) == 1


def test_environment_context_and_working_directory_do_not_redirect_recovery(endpoint, monkeypatch, tmp_path):
    api = kube.core_v1
    api.api_client.configuration.refresh_api_key_hook = None
    monkeypatch.setenv("KUBECONFIG", "/unrelated/config")
    monkeypatch.chdir(tmp_path.parent)
    endpoint.data["current-context"] = "different"
    endpoint.path.write_text(json.dumps(endpoint.data))
    rotate(endpoint)
    operation(api)
    assert len(endpoint.requests) == 2
    assert kube.namespace == "ns"


@pytest.mark.parametrize("invalid", ["missing", "empty", "encoding", "yaml"])
def test_failed_reload_does_not_retry_stale_credentials_and_can_recover(invalid, endpoint):
    api = kube.core_v1

    def invalidate(_):
        if len(endpoint.requests) != 1:
            return
        if invalid == "missing":
            endpoint.token.unlink()
        elif invalid == "empty":
            endpoint.token.write_text(" \n")
        elif invalid == "encoding":
            endpoint.token.write_bytes(b"\xff")
        else:
            endpoint.path.write_text("users: [secret-DO-NOT-EXPOSE")

    endpoint.callback = invalidate
    endpoint.status = 401
    with pytest.raises(RuntimeError, match="cannot safely reload") as error:
        operation(api)
    assert "DO-NOT-EXPOSE" not in str(error.value)
    assert len(endpoint.requests) == 1
    endpoint.callback = None
    endpoint.status = None
    endpoint.path.write_text(json.dumps(endpoint.data))
    rotate(endpoint)
    operation(api)


@pytest.mark.parametrize(
    "user",
    [
        {"token": "inline"},
        {"exec": {"apiVersion": "client.authentication.k8s.io/v1beta1", "command": "/not-executed"}},
        {"auth-provider": {"name": "oidc", "config": {"id-token": "e30.e30.signature"}}},
    ],
)
def test_other_configured_auth_is_reacquired_once(user, endpoint, monkeypatch):
    # Exec acquisition itself isn't the subject here; existing expiry tests use
    # a real executable. Select an inline value alongside it to prevent execution.
    if "exec" in user:
        user = {**user, "token": "inline"}
    endpoint.data["users"][0]["user"] = user
    endpoint.path.write_text(json.dumps(endpoint.data))
    endpoint.status = 401
    with pytest.raises(kubernetes.client.ApiException):
        operation(kube.core_v1)
    assert len(endpoint.requests) == 2


def test_explicit_auth_override_not_retried(endpoint):
    if "_request_auth" not in inspect.signature(kubernetes.client.ApiClient.call_api).parameters:
        pytest.skip("Kubernetes version has no per-request auth override")
    with pytest.raises(kubernetes.client.ApiException):
        operation(
            kube.core_v1,
            _request_auth={"type": "api_key", "in": "header", "key": "Authorization", "value": "Bearer explicit"},
        )
    assert len(endpoint.requests) == 1


def test_ambiguous_transport_failure_is_not_retried(endpoint, monkeypatch):
    api = kube.core_v1
    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise urllib3.exceptions.ReadTimeoutError(None, "/probe", "synthetic timeout")

    monkeypatch.setattr(api.api_client.rest_client, "GET", timeout)
    with pytest.raises(urllib3.exceptions.ReadTimeoutError):
        operation(api)
    assert calls == [1]


@pytest.mark.parametrize("field", ["certificate-authority", "client-certificate", "client-key"])
def test_certificate_content_change_rejected_even_with_same_path(field, endpoint, tmp_path):
    endpoint.data["clusters"][0]["cluster"]["server"] = "https://cluster.example:6443"
    ca = tmp_path / "ca.crt"
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    for path in (ca, cert, key):
        path.write_text("original fixture")
    endpoint.data["clusters"][0]["cluster"]["certificate-authority"] = "ca.crt"
    endpoint.data["users"][0]["user"].update({"client-certificate": "client.crt", "client-key": "client.key"})
    endpoint.path.write_text(json.dumps(endpoint.data))
    api = kube.core_v1  # Builds a pool, but no TLS connection is attempted.
    recovery = api.api_client.recovery
    fresh = recovery.refresh(api.api_client.configuration)
    {"certificate-authority": ca, "client-certificate": cert, "client-key": key}[field].write_text("changed fixture")
    with pytest.raises(RuntimeError, match="cannot safely reload"):
        recovery.refresh(fresh)


def test_recovery_does_not_touch_global_defaults_or_unrelated_configuration(endpoint):
    before = kubernetes.client.Configuration.get_default_copy()
    api = kube.core_v1
    original = api.api_client.configuration
    api.api_client.configuration.refresh_api_key_hook = None
    rotate(endpoint)
    operation(api)
    after = kubernetes.client.Configuration.get_default_copy()
    assert (before.host, before.api_key, before.refresh_api_key_hook) == (
        after.host,
        after.api_key,
        after.refresh_api_key_hook,
    )
    assert api.api_client.configuration is not original
    # Old request snapshots retain their credentials; no in-place replacement.
    assert list(original.api_key.values()) == ["Bearer A"]


def test_forms_files_and_custom_host_are_not_retried(endpoint, tmp_path):
    api = kube.core_v1.api_client
    endpoint.status = 401
    upload = tmp_path / "upload"
    upload.write_text("repeatability is not assumed")
    for extras in (
        {"post_params": [("field", "value")], "header_params": {"Content-Type": "application/x-www-form-urlencoded"}},
        {"files": {"file": [str(upload)]}, "header_params": {"Content-Type": "multipart/form-data"}},
        {"_host": endpoint.data["clusters"][0]["cluster"]["server"]},
        {"body": "text is not an approved JSON mutation", "header_params": {"Content-Type": "text/plain"}},
    ):
        before = len(endpoint.requests)
        with pytest.raises(kubernetes.client.ApiException):
            api.call_api("/probe", "POST", auth_settings=["BearerToken"], _preload_content=False, **extras)
        assert len(endpoint.requests) == before + 1


def test_merged_user_relative_path_is_retained_during_public_reload(endpoint, tmp_path, monkeypatch):
    users = endpoint.data.pop("users")
    endpoint.path.write_text(json.dumps(endpoint.data))
    folder = tmp_path / "users"
    folder.mkdir()
    user_path = folder / "config"
    user_path.write_text(json.dumps({"users": users}))
    token = folder / "token"
    token.write_text("A")
    monkeypatch.setenv("KUBECONFIG", f"{endpoint.path}:{user_path}")
    api = kube.core_v1
    api.api_client.configuration.refresh_api_key_hook = None
    token.write_text("B")
    endpoint.accepted = "B"
    operation(api)
    assert [r[2]["authorization"] for r in endpoint.requests] == ["Bearer A", "Bearer B"]


def test_native_token_file_to_exec_fallback_is_preserved(endpoint, tmp_path):
    import sys

    sentinel = tmp_path / "exec-called"
    plugin = tmp_path / "plugin.py"
    plugin.write_text(
        "import json, pathlib\n"
        f"pathlib.Path({str(sentinel)!r}).touch()\n"
        "print(json.dumps({'apiVersion': 'client.authentication.k8s.io/v1beta1', "
        "'kind': 'ExecCredential', 'status': {'token': 'B'}}))\n"
    )
    endpoint.data["users"][0]["user"]["exec"] = {
        "apiVersion": "client.authentication.k8s.io/v1beta1",
        "command": sys.executable,
        "args": [str(plugin)],
    }
    endpoint.path.write_text(json.dumps(endpoint.data))
    api = kube.core_v1
    assert not sentinel.exists()
    endpoint.accepted = "B"
    endpoint.callback = lambda _: endpoint.token.write_text("")
    operation(api)
    assert sentinel.exists()
    assert len(endpoint.requests) == 2


def test_timeout_policy_is_preserved_on_both_attempts(endpoint, monkeypatch):
    api = kube.core_v1
    original = api.api_client.rest_client.GET
    timeouts = []

    def record(*args, **kwargs):
        timeouts.append(kwargs["_request_timeout"])
        return original(*args, **kwargs)

    monkeypatch.setattr(api.api_client.rest_client, "GET", record)
    endpoint.callback = lambda _: rotate(endpoint) if len(endpoint.requests) == 1 else None
    operation(api, _request_timeout=(2, 5))
    assert timeouts == [(2, 5), (2, 5)]


def test_success_does_not_reread_token_or_kubeconfig(endpoint):
    api = kube.core_v1
    endpoint.token.unlink()
    endpoint.path.unlink()
    operation(api)
    assert len(endpoint.requests) == 1


def test_inline_token_rotation_is_recovered(endpoint):
    endpoint.data["users"][0]["user"] = {"token": "A"}
    endpoint.path.write_text(json.dumps(endpoint.data))
    api = kube.core_v1
    endpoint.data["users"][0]["user"]["token"] = "B"
    endpoint.path.write_text(json.dumps(endpoint.data))
    endpoint.accepted = "B"
    operation(api)
    assert [r[2]["authorization"] for r in endpoint.requests] == ["Bearer A", "Bearer B"]


def test_exec_reacquisition_without_expiry(endpoint, tmp_path):
    import sys

    credential = tmp_path / "exec-token"
    credential.write_text("A")
    calls = tmp_path / "exec-calls"
    plugin = tmp_path / "plugin.py"
    plugin.write_text(
        "import json, pathlib\n"
        f"pathlib.Path({str(calls)!r}).open('a').write('call\\n')\n"
        f"token = pathlib.Path({str(credential)!r}).read_text()\n"
        "print(json.dumps({'apiVersion': 'client.authentication.k8s.io/v1beta1', "
        "'kind': 'ExecCredential', 'status': {'token': token}}))\n"
    )
    endpoint.data["users"][0]["user"] = {
        "exec": {"apiVersion": "client.authentication.k8s.io/v1beta1", "command": sys.executable, "args": [str(plugin)]}
    }
    endpoint.path.write_text(json.dumps(endpoint.data))
    api = kube.core_v1
    credential.write_text("B")
    endpoint.accepted = "B"
    operation(api)
    assert len(calls.read_text().splitlines()) == 2
    assert [r[2]["authorization"] for r in endpoint.requests] == ["Bearer A", "Bearer B"]


def test_oidc_inline_credentials_can_rotate_without_changing_provider_policy(endpoint):
    first = "e30.e30.signature"
    second = "e30.e30.rotated"
    endpoint.data["users"][0]["user"] = {"auth-provider": {"name": "oidc", "config": {"id-token": first}}}
    endpoint.path.write_text(json.dumps(endpoint.data))
    endpoint.accepted = first
    api = kube.core_v1
    endpoint.data["users"][0]["user"]["auth-provider"]["config"]["id-token"] = second
    endpoint.path.write_text(json.dumps(endpoint.data))
    endpoint.accepted = second
    operation(api)
    assert [r[2]["authorization"] for r in endpoint.requests] == ["Bearer " + first, "Bearer " + second]


def test_native_exec_expiry_refresh_still_works_without_401(endpoint, tmp_path):
    import sys

    credential = tmp_path / "native-exec-token"
    credential.write_text("A")
    plugin = tmp_path / "native-plugin.py"
    plugin.write_text(
        "import json, pathlib\n"
        f"token = pathlib.Path({str(credential)!r}).read_text()\n"
        "print(json.dumps({'apiVersion': 'client.authentication.k8s.io/v1beta1', "
        "'kind': 'ExecCredential', 'status': {'token': token, "
        "'expirationTimestamp': '2000-01-01T00:00:00Z'}}))\n"
    )
    endpoint.data["users"][0]["user"] = {
        "exec": {"apiVersion": "client.authentication.k8s.io/v1beta1", "command": sys.executable, "args": [str(plugin)]}
    }
    endpoint.path.write_text(json.dumps(endpoint.data))
    api = kube.core_v1
    operation(api)
    credential.write_text("B")
    endpoint.accepted = "B"
    operation(api)
    assert len(endpoint.requests) == 2
    assert [r[2]["authorization"] for r in endpoint.requests] == ["Bearer A", "Bearer B"]


def test_mixed_inline_and_file_preserves_native_inline_precedence(endpoint):
    endpoint.data["users"][0]["user"] = {"token": "A", "tokenFile": "missing"}
    endpoint.path.write_text(json.dumps(endpoint.data))
    api = kube.core_v1
    endpoint.data["users"][0]["user"]["token"] = "B"
    endpoint.path.write_text(json.dumps(endpoint.data))
    endpoint.accepted = "B"
    operation(api)
    assert [r[2]["authorization"] for r in endpoint.requests] == ["Bearer A", "Bearer B"]


@pytest.mark.parametrize("persistent", [False, True])
def test_streamed_401_refreshes_before_bounded_watch_reconnect(endpoint, monkeypatch, persistent):
    from seekr_chain.backends.k8s import watched_state

    api = kube.core_v1
    endpoint.watch_error = True if persistent else "once"
    if not persistent:
        endpoint.callback = lambda _: endpoint.token.write_text("B") if len(endpoint.requests) == 1 else None
    monkeypatch.setattr(watched_state, "_WATCH_BACKOFF_BASE_SECONDS", 0)
    watcher = None

    def key(obj):
        watcher._stop.set()
        return obj.metadata.name

    spec = watched_state.WatchSpec(
        kind="pods",
        seed=lambda: ([], ""),
        list_fn=api.list_namespaced_pod,
        list_kwargs={"namespace": "ns"},
        key=key,
        rv=lambda obj: obj.metadata.resource_version,
    )
    watcher = watched_state.ReconnectingWatcher(
        label="test", specs=[spec], project=lambda caches: caches, max_attempts=3
    )
    watcher._run_watch(spec, "")
    if persistent:
        assert len(endpoint.requests) == 3
        with pytest.raises(watched_state.WatchStalledError, match="3 attempts"):
            watcher._raise_if_fatal()
    else:
        assert [r[2]["authorization"] for r in endpoint.requests] == ["Bearer A", "Bearer B"]
        assert watcher._caches["pods"]["test"].metadata.resource_version == "1"


def test_basic_password_rotation_is_recovered(endpoint):
    import base64

    endpoint.data["users"][0]["user"] = {"username": "user", "password": "A"}
    endpoint.path.write_text(json.dumps(endpoint.data))
    api = kube.core_v1
    endpoint.data["users"][0]["user"]["password"] = "B"
    endpoint.path.write_text(json.dumps(endpoint.data))
    endpoint.accepted_header = "Basic " + base64.b64encode(b"user:B").decode()
    operation(api)
    assert [r[2]["authorization"] for r in endpoint.requests] == [
        "Basic " + base64.b64encode(b"user:A").decode(),
        endpoint.accepted_header,
    ]


def test_new_exec_command_rejected_before_execution(endpoint, tmp_path):
    marker = tmp_path / "must-not-execute"
    endpoint.data["users"][0]["user"]["exec"] = {
        "apiVersion": "client.authentication.k8s.io/v1beta1",
        "command": "/original/command",
    }
    endpoint.path.write_text(json.dumps(endpoint.data))
    api = kube.core_v1  # tokenFile wins; exec is not run.
    endpoint.data["users"][0]["user"]["exec"]["command"] = "/bin/sh"
    endpoint.data["users"][0]["user"]["exec"]["args"] = ["-c", f"touch {marker}"]
    endpoint.path.write_text(json.dumps(endpoint.data))
    endpoint.status = 401
    with pytest.raises(RuntimeError, match="cannot safely reload"):
        operation(api)
    assert not marker.exists()
    assert len(endpoint.requests) == 1
