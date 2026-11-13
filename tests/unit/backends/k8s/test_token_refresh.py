"""Real cached clients and HTTP transport; no cluster or real credentials."""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import kubernetes
import pytest

from seekr_chain.k8s_api import kube

CLIENTS = ["core_v1", "batch_v1", "custom_objects"]


@pytest.fixture
def receiver():
    headers = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            headers.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", headers
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def config(tmp_path, monkeypatch, receiver):
    host, _ = receiver
    (tmp_path / "token").write_text("token-A")
    (tmp_path / "ca.crt").write_text("Unused CA fixture for HTTP transport")

    def write(user=None):
        path = tmp_path / "config"
        path.write_text(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "current-context": "active",
                    "clusters": [{"name": "cluster", "cluster": {"server": host, "certificate-authority": "ca.crt"}}],
                    "contexts": [
                        {"name": "active", "context": {"cluster": "cluster", "user": "user", "namespace": "test-ns"}}
                    ],
                    "users": [{"name": "user", "user": user if user is not None else {"tokenFile": "token"}}],
                }
            )
        )
        monkeypatch.setenv("KUBECONFIG", str(path))
        return path

    write()
    return write


def request(client):
    # Use the real generated operation for each client type.
    kwargs = {"_preload_content": False}
    if isinstance(client, kubernetes.client.CoreV1Api):
        response = client.list_namespace(**kwargs)
    elif isinstance(client, kubernetes.client.BatchV1Api):
        response = client.list_job_for_all_namespaces(**kwargs)
    else:
        response = client.list_cluster_custom_object("example.org", "v1", "widgets", **kwargs)
    try:
        assert response.status == 200
    finally:
        response.close()


@pytest.mark.parametrize("name", CLIENTS)
@pytest.mark.parametrize("replacement", ["overwrite", "atomic", "projected"])
def test_cached_client_observes_rotation(name, replacement, config, tmp_path, receiver):
    client = getattr(kube, name)
    settings = client.api_client.configuration
    original = (settings.host, settings.ssl_ca_cert, settings.verify_ssl)
    # Upstream ignores CA settings for plain HTTP; HTTPS is covered separately.
    assert settings.ssl_ca_cert is None
    assert kube.namespace == "test-ns"
    request(client)
    assert receiver[1] == ["Bearer token-A"]

    token = tmp_path / "token"
    if replacement == "overwrite":
        token.write_text("token-B")
    elif replacement == "atomic":
        new = tmp_path / "new-token"
        new.write_text("token-B")
        new.replace(token)
    else:
        for generation, value in [("generation1", "token-A"), ("generation2", "token-B")]:
            (tmp_path / generation).mkdir()
            (tmp_path / generation / "token").write_text(value)
        (tmp_path / "..data").symlink_to("generation1")
        token.unlink()
        token.symlink_to("..data/token")
        request(client)
        assert receiver[1][-1] == "Bearer token-A"
        (tmp_path / "..data_tmp").symlink_to("generation2")
        (tmp_path / "..data_tmp").replace(tmp_path / "..data")

    assert getattr(kube, name) is client
    request(client)
    assert receiver[1][-1] == "Bearer token-B"
    assert (settings.host, settings.ssl_ca_cert, settings.verify_ssl) == original


@pytest.mark.parametrize("name", CLIENTS)
@pytest.mark.parametrize("failure", ["empty", "missing", "unreadable", "invalid-encoding"])
@pytest.mark.parametrize("initial", [False, True])
def test_invalid_file_fails_without_transport(name, failure, initial, config, tmp_path, receiver):
    client = None if initial else getattr(kube, name)
    if client:
        request(client)
    before = len(receiver[1])
    token = tmp_path / "token"
    if failure == "empty":
        token.write_text(" \n\t")
    elif failure == "missing":
        token.unlink()
    elif failure == "unreadable":
        # A directory is unreadable as a token even when tests run as root.
        token.unlink()
        token.mkdir()
    else:
        token.write_bytes(b"\xff\xfe")
    with pytest.raises(RuntimeError, match="Kubernetes token file") as error:
        request(getattr(kube, name) if initial else client)
    assert "token-A" not in str(error.value)
    assert len(receiver[1]) == before
    if failure == "unreadable":
        token.rmdir()
    token.write_text("token-recovered")
    request(getattr(kube, name) if initial else client)
    assert receiver[1][-1] == "Bearer token-recovered"


@pytest.mark.parametrize(
    "user,expected",
    [
        ({"token": "inline", "tokenFile": "missing"}, "Bearer inline"),
        ({"username": "user", "password": "password"}, "Basic dXNlcjpwYXNzd29yZA=="),
        ({}, None),
        (
            {"auth-provider": {"name": "oidc", "config": {"id-token": "e30.e30.signature"}}, "tokenFile": "missing"},
            "Bearer e30.e30.signature",
        ),
    ],
)
def test_non_file_auth_preserves_precedence(user, expected, config, receiver):
    config(user)
    client = kube.core_v1
    request(client)
    request(client)
    assert receiver[1] == [expected, expected]


def test_exec_plugin_still_refreshes(config, tmp_path, receiver):
    # Return an already-expired credential so upstream's expiry hook must rerun
    # the real executable on every authentication (without sleeps or hook mocks).
    credential = tmp_path / "exec-token"
    credential.write_text("exec-A")
    plugin = tmp_path / "plugin.py"
    plugin.write_text(
        "import json, pathlib\n"
        f"token = pathlib.Path({str(credential)!r}).read_text()\n"
        "print(json.dumps({'apiVersion': 'client.authentication.k8s.io/v1beta1', "
        "'kind': 'ExecCredential', 'status': {'token': token, "
        "'expirationTimestamp': '2000-01-01T00:00:00Z'}}))\n"
    )
    config(
        {
            "exec": {
                "apiVersion": "client.authentication.k8s.io/v1beta1",
                "command": sys.executable,
                "args": [str(plugin)],
            }
        }
    )
    client = kube.core_v1
    request(client)
    credential.write_text("exec-B")
    request(client)
    assert receiver[1] == ["Bearer exec-A", "Bearer exec-B"]


def test_token_file_wins_over_exec(config, tmp_path, receiver):
    config(
        {
            "tokenFile": "token",
            "exec": {"apiVersion": "client.authentication.k8s.io/v1beta1", "command": "/must-not-execute"},
        }
    )
    request(kube.core_v1)
    (tmp_path / "token").write_text("token-B")
    request(kube.core_v1)
    assert receiver[1] == ["Bearer token-A", "Bearer token-B"]


def test_configuration_isolation_and_concurrent_clients(config, tmp_path, receiver):
    # Creating the provider must not change defaults or unrelated clients.
    before = kubernetes.client.Configuration.get_default_copy()
    unrelated = kubernetes.client.CoreV1Api()
    unrelated_before = unrelated.api_client.configuration.api_key.copy()
    clients = [getattr(kube, name) for name in CLIENTS]
    (tmp_path / "token").write_text("token-B")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(request, clients * 4))
    assert receiver[1] == ["Bearer token-B"] * 12
    after = kubernetes.client.Configuration.get_default_copy()
    assert (before.host, before.api_key, before.refresh_api_key_hook) == (
        after.host,
        after.api_key,
        after.refresh_api_key_hook,
    )
    assert unrelated.api_client.configuration.api_key == unrelated_before
    assert unrelated.api_client.configuration.refresh_api_key_hook is before.refresh_api_key_hook


def test_merged_config_resolves_user_file_relative_to_its_source(config, tmp_path, monkeypatch, receiver):
    main = config()
    user_dir = tmp_path / "users"
    user_dir.mkdir()
    (user_dir / "token").write_text("merged-token")
    data = json.loads(main.read_text())
    users = data.pop("users")
    main.write_text(json.dumps(data))
    users_file = user_dir / "config"
    users_file.write_text(json.dumps({"users": users}))
    monkeypatch.setenv("KUBECONFIG", f"{main}:{users_file}")
    request(kube.core_v1)
    assert receiver[1] == ["Bearer merged-token"]


@pytest.mark.parametrize("certificate_auth", [False, True])
def test_https_settings_match_upstream(config, tmp_path, certificate_auth):
    path = config()
    data = json.loads(path.read_text())
    data["clusters"][0]["cluster"]["server"] = "https://cluster.example:6443"
    data["clusters"][0]["cluster"]["tls-server-name"] = "cluster.example"
    if certificate_auth:
        (tmp_path / "client.crt").write_text("certificate fixture")
        (tmp_path / "client.key").write_text("key fixture")
        data["users"][0]["user"] = {"client-certificate": "client.crt", "client-key": "client.key"}
    path.write_text(json.dumps(data))
    upstream = kubernetes.client.Configuration()
    kubernetes.config.load_kube_config(config_file=str(path), client_configuration=upstream)
    settings = kube.core_v1.api_client.configuration
    attributes = ["host", "ssl_ca_cert", "verify_ssl", "tls_server_name", "cert_file", "key_file"]
    expected = [getattr(upstream, key) for key in attributes]
    assert [getattr(settings, key) for key in attributes] == expected
    assert settings.ssl_ca_cert == str(tmp_path / "ca.crt")
    if certificate_auth:
        assert settings.refresh_api_key_hook is None
    else:
        (tmp_path / "token").write_text("token-B")
        assert settings.auth_settings()["BearerToken"]["value"] == "Bearer token-B"
    assert [getattr(settings, key) for key in attributes] == expected
