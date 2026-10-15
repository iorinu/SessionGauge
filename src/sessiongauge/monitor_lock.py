"""プロセス間で共有するmonitor lease。"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

if os.name == "posix":
    import fcntl
else:
    fcntl = None


class MonitorAlreadyRunning(RuntimeError):
    """同じscopeのmonitor leaseがすでに取得されている。"""


class MonitorEndpointError(ValueError):
    """排他scopeに使うherdr socketを特定できない。"""


class MonitorLease:
    """data directoryとendpointごとの排他lease。"""

    def __init__(self, data_dir, endpoint):
        self.data_dir = Path(data_dir)
        self.endpoint = endpoint
        self._fd = None

    def __enter__(self):
        if fcntl is None:
            raise NotImplementedError("MonitorLease is unsupported on this platform")

        canonical_dir = self.data_dir.resolve(strict=True)
        run_dir = canonical_dir / ".sessiongauge-run"
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise RuntimeError("monitor lock directory must be a real directory")

        endpoint = Path(self.endpoint)
        if not endpoint.is_absolute():
            raise MonitorEndpointError("monitor endpoint must be an absolute path")
        canonical_endpoint = endpoint.resolve(strict=False)
        name = hashlib.sha256(os.fsencode(canonical_endpoint)).hexdigest() + ".lock"
        lock_path = run_dir / name
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
        try:
            os.set_inheritable(fd, False)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("monitor lock must be a single-link regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MonitorAlreadyRunning("monitor is already running for this scope") from exc
        except BaseException:
            os.close(fd)
            raise

        self._fd = fd
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)
        return False


def resolve_endpoint(*, environ=None, runner=subprocess.run, which=shutil.which):
    """herdr statusの公開契約から、排他scopeに使うsocketを取得する。"""
    environ = os.environ if environ is None else environ
    configured = environ.get("HERDR_SOCKET_PATH")
    if configured is not None:
        path = configured
    else:
        executable = environ.get("HERDR_BIN_PATH") or which("herdr")
        if not executable:
            raise MonitorEndpointError(
                "herdr socket endpoint is unknown: herdr executable not found"
            )
        try:
            result = runner(
                [executable, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MonitorEndpointError(
                "herdr socket endpoint is unknown: status request failed"
            ) from exc
        if result.returncode != 0:
            raise MonitorEndpointError("herdr socket endpoint is unknown: status request failed")
        try:
            status = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise MonitorEndpointError(
                "herdr socket endpoint is unknown: invalid status JSON"
            ) from exc
        server = status.get("server") if isinstance(status, dict) else None
        if not isinstance(server, dict):
            raise MonitorEndpointError("herdr socket endpoint is unknown: server status missing")
        path = server.get("socket", server.get("socket_path"))
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise MonitorEndpointError(
            "herdr socket endpoint is unknown: expected an absolute socket path"
        )
    return os.path.normpath(path)
