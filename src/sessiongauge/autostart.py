"""macOS LaunchAgent と herdr 用 SessionGauge plugin bundle の管理。"""

import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path


class AutostartError(RuntimeError):
    """安全にautostartを管理できない。"""


@dataclass(frozen=True)
class Installation:
    data_dir: Path
    socket_path: Path
    config_path: Path
    python_executable: str
    herdr_bin: str
    home: Path | None = None
    uid: int | None = None
    runner: object = subprocess.run
    stop_timeout: float = 45.0
    hermes_db_paths: tuple[Path, ...] | None = None
    poll_interval_seconds: int | None = None
    time_zone: str | None = None

    def __post_init__(self):
        for name in ("data_dir", "socket_path", "config_path"):
            value = Path(getattr(self, name)).expanduser()
            if not value.is_absolute() or "\0" in str(value):
                raise ValueError(f"{name} must be an absolute path")
            object.__setattr__(
                self,
                name,
                value.resolve(strict=False)
                if name != "config_path"
                else Path(os.path.abspath(value)),
            )
        for name in ("python_executable", "herdr_bin"):
            value = str(getattr(self, name))
            if not value.startswith("/") or "\0" in value:
                raise ValueError(f"{name} must be an absolute path")
            object.__setattr__(self, name, os.path.normpath(value))
        home = Path(self.home or Path.home()).expanduser()
        if not home.is_absolute() or "\0" in str(home):
            raise ValueError("home must be an absolute path")
        object.__setattr__(self, "home", home.resolve(strict=False))
        object.__setattr__(self, "uid", os.getuid() if self.uid is None else int(self.uid))
        if self.uid < 0:
            raise ValueError("uid must be nonnegative")

    @property
    def scope_hash(self):
        payload = f"{self.data_dir}\0{self.socket_path}".encode()
        return hashlib.sha256(payload).hexdigest()

    @property
    def label(self):
        return f"io.github.iorinu.sessiongauge.{self.scope_hash}"

    @property
    def plugin_id(self):
        return f"iorinu.sessiongauge.{self.scope_hash}"

    @property
    def plist_path(self):
        return self.home / "Library/LaunchAgents" / f"{self.label}.plist"

    @property
    def plugin_path(self):
        return self.data_dir / "autostart" / self.scope_hash / "herdr-plugin.toml"

    @property
    def service_target(self):
        return f"gui/{self.uid}/{self.label}"

    @property
    def run_arguments(self):
        arguments = [
            self.python_executable,
            "-m",
            "sessiongauge",
            "--config",
            str(self.config_path),
            "--data-dir",
            str(self.data_dir),
            "autostart",
            "run",
            "--socket-path",
            str(self.socket_path),
            "--herdr-bin",
            self.herdr_bin,
        ]
        if self.hermes_db_paths is not None:
            for path in self.hermes_db_paths:
                arguments.extend(("--hermes-db", str(path)))
        if self.poll_interval_seconds is not None:
            arguments.extend(("--poll-interval-seconds", str(self.poll_interval_seconds)))
        if self.time_zone is not None:
            arguments.extend(("--time-zone", self.time_zone))
        return arguments

    def _plist_bytes(self):
        environment = {
            "HERDR_ENV": "1",
            "HERDR_SOCKET_PATH": str(self.socket_path),
            "HERDR_BIN_PATH": self.herdr_bin,
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    [
                        str(Path(self.herdr_bin).parent),
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                        "/usr/bin",
                        "/bin",
                        "/usr/sbin",
                        "/sbin",
                    ]
                )
            ),
            "SESSIONGAUGE_AUTOSTART_SCOPE": self.scope_hash,
            "HOME": str(self.home),
        }
        for name in (
            "HERMES_HOME",
            "SESSIONGAUGE_CONFIG",
            "SESSIONGAUGE_HERMES_DB_PATHS",
            "SESSIONGAUGE_DATA_DIR",
            "SESSIONGAUGE_POLL_INTERVAL_SECONDS",
            "SESSIONGAUGE_TIME_ZONE",
            "SESSIONGAUGE_SIDEBAR_ENABLED",
            "SESSIONGAUGE_SIDEBAR_TTL_MS",
            "SESSIONGAUGE_SIDEBAR_DISPLAY",
            "SESSIONGAUGE_SIDEBAR_TIME_ZONE",
        ):
            if name in os.environ:
                environment[name] = os.environ[name]
        document = {
            "Label": self.label,
            "ProgramArguments": self.run_arguments,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "Umask": 0o077,
            "EnvironmentVariables": environment,
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": str(self.data_dir / "logs" / f"autostart-{self.scope_hash}.log"),
            "ExitTimeOut": 60,
            "ThrottleInterval": 30,
        }
        return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)

    def _plugin_text(self):
        def array(arguments):
            return (
                "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in arguments) + "]"
            )

        base = [
            self.python_executable,
            "-m",
            "sessiongauge",
            "--config",
            str(self.config_path),
            "--data-dir",
            str(self.data_dir),
            "autostart",
        ]
        sections = [
            f"id = {json.dumps(self.plugin_id)}",
            'name = "SessionGauge autostart"',
            'version = "0.1.0"',
            'min_herdr_version = "0.9.2"',
            'platforms = ["macos"]',
            "",
            "[[startup]]",
            f"command = {array([*base, 'start', '--socket-path', str(self.socket_path), '--herdr-bin', self.herdr_bin])}",
        ]
        for action in ("start", "stop", "status"):
            sections.extend(
                [
                    "",
                    "[[actions]]",
                    f"id = {json.dumps(action)}",
                    f"title = {json.dumps({'start': 'Start SessionGauge', 'stop': 'Stop SessionGauge', 'status': 'SessionGauge status'}[action])}",
                    f"command = {array([*base, action, '--socket-path', str(self.socket_path), '--herdr-bin', self.herdr_bin])}",
                ]
            )
        text = "\n".join(sections) + "\n"
        tomllib.loads(text)
        return text.encode("utf-8")

    @staticmethod
    def _ensure_dir(path):
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700)
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise AutostartError("directory path is not a real directory")

    @staticmethod
    def _write_owned(path, content):
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AutostartError("refusing to replace a non-owned file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                if (opened.st_ino, opened.st_dev) != (info.st_ino, info.st_dev):
                    raise AutostartError("file changed during validation")
                existing = b""
                while chunk := os.read(fd, 65536):
                    existing += chunk
            finally:
                os.close(fd)
            if existing != content:
                raise AutostartError("refusing to overwrite an existing file")
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("failed to write owned file")
                view = view[written:]
        finally:
            os.close(fd)

    def _launchctl(self, *arguments):
        if os.sys.platform != "darwin" and self.runner is subprocess.run:
            raise AutostartError("macOS LaunchAgent operations are only available on macOS")
        try:
            result = self.runner(
                ["/bin/launchctl", *arguments],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AutostartError("launchctl operation failed") from exc
        return result

    def _print_missing(self, result):
        return result.returncode == 113 and not result.stdout.strip()

    def _is_missing(self, result):
        return (
            result.returncode == 113
            and not result.stdout.strip()
            and "Could not find service" in result.stderr
        )

    def _loaded(self):
        result = self._launchctl("print", self.service_target)
        if result.returncode == 0:
            fields = self._parse_status(result.stdout)
            marker = re.search(
                r"^\s*SESSIONGAUGE_AUTOSTART_SCOPE\s*=>\s*(.*?)\s*$", result.stdout, re.MULTILINE
            )
            fields["_owned"] = (
                self.label in result.stdout
                and marker is not None
                and marker.group(1) == self.scope_hash
            )
            return fields
        if self._is_missing(result):
            return None
        raise AutostartError("launchctl status failed")

    @staticmethod
    def _parse_status(output):
        fields = {}
        for line in output.splitlines():
            match = re.match(r"\s*(state|pid|last exit code)\s*=\s*(.*?)\s*$", line)
            if match:
                fields[match.group(1).replace(" ", "_")] = match.group(2)
        return fields

    def _owned_plist(self):
        try:
            info = self.plist_path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return False
        fd = os.open(self.plist_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                return False
            with os.fdopen(os.dup(fd), "rb") as stream:
                try:
                    value = plistlib.load(stream)
                except (plistlib.InvalidFileException, ValueError, TypeError) as exc:
                    raise AutostartError("owned LaunchAgent plist is invalid") from exc
        finally:
            os.close(fd)
        if not isinstance(value, dict) or not isinstance(
            value.get("EnvironmentVariables", {}), dict
        ):
            raise AutostartError("owned LaunchAgent plist has invalid fields")
        return (
            value.get("Label") == self.label
            and value.get("EnvironmentVariables", {}).get("SESSIONGAUGE_AUTOSTART_SCOPE")
            == self.scope_hash
        )

    def _ensure_files(self):
        plist_bytes = self._plist_bytes()
        plugin_bytes = self._plugin_text()
        log_dir = self.data_dir / "logs"
        plugin_dir = self.plugin_path.parent
        self._ensure_dir(self.home / "Library/LaunchAgents")
        self._ensure_dir(self.data_dir)
        self._ensure_dir(log_dir)
        self._ensure_dir(plugin_dir)
        log_path = log_dir / f"autostart-{self.scope_hash}.log"
        try:
            info = log_path.lstat()
        except FileNotFoundError:
            fd = os.open(
                log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            os.close(fd)
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AutostartError("refusing unsafe stderr log file")
            fd = os.open(
                log_path,
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                opened = os.fstat(fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise AutostartError("refusing unsafe stderr log file")
            finally:
                os.close(fd)
        self._write_owned(self.plist_path, plist_bytes)
        self._write_owned(self.plugin_path, plugin_bytes)

    def install(self):
        self._ensure_files()
        loaded = self._loaded()
        if loaded is not None and not loaded.get("_owned"):
            raise AutostartError("loaded launch service ownership is unknown")
        if loaded is None:
            result = self._launchctl("bootstrap", f"gui/{self.uid}", str(self.plist_path))
            if result.returncode:
                loaded = self._loaded()
                if loaded is None or not loaded.get("_owned"):
                    raise AutostartError("launchctl bootstrap failed")
        return self

    def status(self):
        loaded = self._loaded()
        if loaded is not None and not loaded.get("_owned"):
            return {"loaded": False}
        return {
            "loaded": loaded is not None,
            **{key: value for key, value in (loaded or {}).items() if not key.startswith("_")},
        }

    def start(self):
        if not self._owned_plist():
            raise AutostartError("owned LaunchAgent plist is required")
        loaded = self._loaded()
        if loaded is not None and not loaded.get("_owned"):
            raise AutostartError("loaded launch service ownership is unknown")
        if loaded is None:
            result = self._launchctl("bootstrap", f"gui/{self.uid}", str(self.plist_path))
            loaded = self._loaded()
            if loaded is None or not loaded.get("_owned"):
                raise AutostartError("launchctl bootstrap failed")
        if loaded.get("pid") and loaded.get("state", "").lower() not in (
            "not running",
            "waiting",
        ):
            return
        result = self._launchctl("kickstart", self.service_target)
        if result.returncode:
            raise AutostartError("launchctl kickstart failed")

    def stop(self):
        if not self._owned_plist():
            raise AutostartError("owned LaunchAgent plist is required")
        loaded = self._loaded()
        if loaded is None:
            return
        if not loaded.get("_owned"):
            raise AutostartError("loaded launch service ownership is unknown")
        if not loaded.get("pid") or loaded.get("state", "").lower() in ("not running", "waiting"):
            return
        result = self._launchctl("kill", "SIGINT", self.service_target)
        if result.returncode:
            raise AutostartError("launchctl stop failed")
        deadline = time.monotonic() + self.stop_timeout
        while True:
            current = self._loaded()
            if (
                current is None
                or not current.get("pid")
                or current.get("state", "").lower() in ("not running", "waiting")
            ):
                return
            if time.monotonic() >= deadline:
                raise AutostartError("launch service did not stop before timeout")
            time.sleep(0.1)

    def uninstall(self):
        if not self._owned_plist():
            return
        loaded = self._loaded()
        if loaded is not None and not loaded.get("_owned"):
            raise AutostartError("loaded launch service ownership is unknown")
        if loaded is not None:
            if loaded.get("pid") and loaded.get("state", "").lower() not in (
                "not running",
                "waiting",
            ):
                self.stop()
            result = self._launchctl("bootout", self.service_target)
            if result.returncode:
                raise AutostartError("launchctl bootout failed")
        self.plist_path.unlink()
