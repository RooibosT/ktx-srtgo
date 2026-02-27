from __future__ import annotations

import importlib
import os
import sys

import keyring

_WARNED = False


def _warn_once(message: str) -> None:
    global _WARNED
    if _WARNED:
        return
    print(message, file=sys.stderr)
    _WARNED = True


def configure_keyring_backend() -> None:
    """Configure keyring backend safely across mixed environments.

    - If PYTHON_KEYRING_BACKEND points to a missing module, ignore it.
    - If no system backend is available, fallback to keyrings.alt plaintext backend.
      If that backend is also unavailable, fallback to null backend to avoid crashes.
    """

    backend_name = os.getenv("PYTHON_KEYRING_BACKEND", "").strip()
    if backend_name:
        module_name = backend_name.rsplit(".", 1)[0]
        if module_name:
            try:
                importlib.import_module(module_name)
            except ModuleNotFoundError:
                os.environ.pop("PYTHON_KEYRING_BACKEND", None)
                _warn_once(
                    (
                        "[warn] Invalid PYTHON_KEYRING_BACKEND was ignored. "
                        "Install matching backend package or unset the variable."
                    )
                )

    try:
        backend = keyring.get_keyring()
    except Exception:
        os.environ.pop("PYTHON_KEYRING_BACKEND", None)
        try:
            backend = keyring.get_keyring()
        except Exception:
            from keyring.backends.null import Keyring as NullKeyring

            keyring.set_keyring(NullKeyring())
            _warn_once(
                "[warn] Keyring initialization failed. Stored credentials/settings are disabled."
            )
            return

    if backend.__class__.__module__.startswith("keyring.backends.fail"):
        try:
            from keyrings.alt.file import PlaintextKeyring
        except Exception:
            from keyring.backends.null import Keyring as NullKeyring

            keyring.set_keyring(NullKeyring())
            _warn_once(
                "[warn] No usable keyring backend. Stored credentials/settings are disabled."
            )
        else:
            keyring.set_keyring(PlaintextKeyring())
            _warn_once(
                "[warn] System keyring not found. Using keyrings.alt plaintext backend."
            )
