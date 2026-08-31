"""Generates a self-signed TLS certificate + private key for local HTTPS testing.

Python's own `ssl` module can USE certificates but has no built-in way to
GENERATE one -- that's normal; cert generation isn't an HTTP-server concern,
it's a separate cryptographic tooling step, so this shells out to the
`openssl` command-line tool (present on essentially every dev machine)
rather than pulling in a third-party Python crypto library.

Run with: python generate_cert.py
Produces: cert.pem, key.pem (both git-ignored -- never commit private keys)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

CERT_PATH = Path(__file__).parent / "cert.pem"
KEY_PATH = Path(__file__).parent / "key.pem"


def main() -> None:
    if shutil.which("openssl") is None:
        print(
            "openssl was not found on your PATH. It's normally preinstalled on macOS/Linux; "
            "on Windows, install it via 'winget install OpenSSL' or use WSL/Git Bash, then re-run this."
        )
        sys.exit(1)

    if CERT_PATH.exists() and KEY_PATH.exists():
        print(f"{CERT_PATH.name} and {KEY_PATH.name} already exist -- delete them first to regenerate.")
        return

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(KEY_PATH),
            "-out", str(CERT_PATH),
            "-days", "365",
            "-subj", "/CN=localhost",
        ],
        check=True,
    )
    print(f"Generated {CERT_PATH.name} and {KEY_PATH.name} -- valid for 365 days, for localhost only.")
    print("These are self-signed, so browsers will show a warning -- expected for local testing.")


if __name__ == "__main__":
    main()
