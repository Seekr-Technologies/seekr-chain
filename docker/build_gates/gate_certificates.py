"""Reject trust stores affected by GHSA-6q48-6c7w-9gw2 (eMudhra).

Standard library only, so the security bootstrap does not need another installer.
Run inside the interpreter/image being checked, not just on the build host.
"""

import hashlib
import importlib
import importlib.util
import re
import shutil
import ssl
import subprocess
from pathlib import Path

MINIMUM_VERSIONS = {"certifi": (2026, 10, 19), "pip": (26, 2, 2), "uv": (0, 13, 4)}
# SHA256 of DER emSign ECC Root CA - G3, from certifi 2025.4.26.
# Only G3 is affected. 2026.10.19 restored unaffected emSign roots; do not
# reject them just because they share an issuer organization:
# https://github.com/advisories/GHSA-6q48-6c7w-9gw2
COMPROMISED_ROOT = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"


def check_version(package: str, installed: str) -> None:
    """Fail closed on old, prerelease, or unrecognized tool versions."""
    valid = re.fullmatch(r"\d+\.\d+(?:\.\d+)?", installed)
    release = tuple(map(int, installed.split("."))) if valid else ()
    # PEP 440 release comparison pads missing components: 26.3 == 26.3.0.
    release += (0,) * (3 - len(release))
    if not valid or release < MINIMUM_VERSIONS[package]:
        minimum = ".".join(map(str, MINIMUM_VERSIONS[package]))
        raise SystemExit(f"Certificate gate failed: {package} {installed}; require >= {minimum} (stable)")


def check_bundle(path: Path) -> None:
    """Parse the whole bundle and reject the compromised root by certificate fingerprint."""
    bundle = path.read_text(encoding="ascii")
    certificates = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", bundle, re.DOTALL)
    if not certificates:
        raise SystemExit(f"Certificate gate failed: empty CA bundle: {path}")
    # OpenSSL also rejects malformed base64/DER and bundles with no usable CA.
    ssl.create_default_context(cadata=bundle)
    for certificate in certificates:
        der = ssl.PEM_cert_to_DER_cert(certificate)
        if hashlib.sha256(der).hexdigest() == COMPROMISED_ROOT:
            raise SystemExit(f"Certificate gate failed: compromised emSign root in {path}")
    print(f"Certificate gate passed: {path}")


def main() -> None:
    """Check system roots, application certifi, and tools with private trust stores."""
    check_bundle(Path("/etc/ssl/certs/ca-certificates.crt"))
    certifi = importlib.import_module("certifi")
    check_version("certifi", certifi.__version__)
    check_bundle(Path(certifi.where()))
    if importlib.util.find_spec("pip") is not None:
        pip = importlib.import_module("pip")
        check_version("pip", pip.__version__)
        vendored = importlib.import_module("pip._vendor.certifi")
        check_bundle(Path(vendored.where()))
    if uv := shutil.which("uv"):
        output = subprocess.check_output([uv, "--version"], text=True)
        check_version("uv", output.split()[1])


if __name__ == "__main__":
    main()
