from hermetic import rustfs


def test_start_configures_rustfs_with_s3_credentials(monkeypatch):
    """The hermetic service uses RustFS's documented S3 listener and credentials."""
    service = rustfs.HermeticRustFS()
    commands = []
    health_checks = []
    bucket_endpoints = []

    monkeypatch.setattr(service, "_container_is_running", lambda: False)
    monkeypatch.setattr(service, "_container_exists", lambda: False)
    monkeypatch.setattr(service, "_query_host_port", lambda: 31234)
    monkeypatch.setattr(service, "_get_container_ip", lambda: "172.20.0.2")
    monkeypatch.setattr(service, "_wait_for_health", lambda endpoint: health_checks.append(endpoint))
    monkeypatch.setattr(service, "_create_bucket", lambda endpoint: bucket_endpoints.append(endpoint))
    monkeypatch.setattr(rustfs.subprocess, "run", lambda command, **kwargs: commands.append(command))

    info = service._start_locked()

    port_flag = f"{rustfs.RUSTFS_HOST_PORT}:9000" if rustfs.RUSTFS_HOST_PORT else ":9000"
    assert commands == [
        [
            rustfs._RUNTIME,
            "run",
            "-d",
            "--name",
            rustfs.RUSTFS_CONTAINER_NAME,
            "--network",
            rustfs.RUSTFS_NETWORK,
            "-p",
            port_flag,
            "-e",
            f"RUSTFS_ACCESS_KEY={rustfs.RUSTFS_ACCESS_KEY}",
            "-e",
            f"RUSTFS_SECRET_KEY={rustfs.RUSTFS_SECRET_KEY}",
            rustfs.RUSTFS_IMAGE,
            "/data",
        ]
    ]
    endpoint_url_local = f"http://localhost:{rustfs.RUSTFS_HOST_PORT or 31234}"
    assert health_checks == [endpoint_url_local]
    assert bucket_endpoints == [endpoint_url_local]
    assert info.endpoint_url_pod == "http://172.20.0.2:9000"
