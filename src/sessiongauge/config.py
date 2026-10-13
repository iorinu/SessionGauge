import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Config:
    poll_interval_seconds: int = 3
    hermes_db_paths: tuple[Path, ...] = ()
    data_dir: Path = Path.home() / ".local/share/sessiongauge"
    time_zone: str = "local"
    sidebar_enabled: bool = False
    sidebar_ttl_ms: int = 15000
    sidebar_time_zone: str = "local"
    sidebar_quota_enabled: bool = False
    sidebar_quota_agent_resume_bin: str = "agent-resume"
    sidebar_quota_hermes_home: Path = Path.home() / ".hermes"
    sidebar_quota_poll_interval_seconds: int = 60


def profile_name_for_path(path: Path, hermes_home: str | Path | None = None) -> str:
    """DBパスからCLIとCollectorで共通の表示profile名を返す。"""
    path = Path(path).expanduser().resolve(strict=False)
    home = Path(hermes_home or Path.home() / ".hermes").expanduser().resolve(strict=False)
    profiles_root = home / "profiles"
    try:
        relative = path.relative_to(profiles_root)
    except ValueError:
        relative = None
    if relative is not None and len(relative.parts) >= 2:
        return relative.parts[0]
    if path.parent.parent.name == "profiles":
        return path.parent.name
    if path == home / "state.db":
        default_home = (Path.home() / ".hermes").resolve(strict=False)
        return "default" if home == default_home else home.name
    if path.parent.name == "profiles":
        return "default"
    return "default"


def load_config(
    *,
    cli: dict | None = None,
    environ: dict[str, str] | None = None,
    config_path: Path | None = None,
) -> Config:
    """CLI > 環境変数 > TOML > 既定値の順で設定を解決する。"""
    env = os.environ if environ is None else environ
    path = config_path or Path(
        env.get("SESSIONGAUGE_CONFIG", Path.home() / ".config/sessiongauge/config.toml")
    )
    raw: dict = {}
    if path.is_file():
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    values = dict(raw)
    sidebar = raw.get("sidebar", {})
    if not isinstance(sidebar, dict):
        raise TypeError("sidebar must be a table")
    values["sidebar_enabled"] = sidebar.get("enabled", False)
    values["sidebar_ttl_ms"] = sidebar.get("ttl_ms", 15000)
    values["sidebar_time_zone"] = sidebar.get("time_zone", "local")
    quota = sidebar.get("quota", {})
    if not isinstance(quota, dict):
        raise TypeError("sidebar.quota must be a table")
    values["sidebar_quota_enabled"] = quota.get("enabled", False)
    values["sidebar_quota_agent_resume_bin"] = quota.get("agent_resume_bin", "agent-resume")
    values["sidebar_quota_hermes_home"] = quota.get(
        "hermes_home", env.get("HERMES_HOME", "~/.hermes")
    )
    values["sidebar_quota_poll_interval_seconds"] = quota.get("poll_interval_seconds", 60)
    if "SESSIONGAUGE_SIDEBAR_ENABLED" in env:
        text = env["SESSIONGAUGE_SIDEBAR_ENABLED"].strip().lower()
        if text not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
            raise ValueError("SESSIONGAUGE_SIDEBAR_ENABLED must be a boolean")
        values["sidebar_enabled"] = text in ("1", "true", "yes", "on")
    if "SESSIONGAUGE_SIDEBAR_TTL_MS" in env:
        try:
            values["sidebar_ttl_ms"] = int(env["SESSIONGAUGE_SIDEBAR_TTL_MS"])
        except ValueError as exc:
            raise ValueError("SESSIONGAUGE_SIDEBAR_TTL_MS must be an integer") from exc
    if "SESSIONGAUGE_SIDEBAR_TIME_ZONE" in env:
        values["sidebar_time_zone"] = env["SESSIONGAUGE_SIDEBAR_TIME_ZONE"]
    if "SESSIONGAUGE_POLL_INTERVAL_SECONDS" in env:
        values["poll_interval_seconds"] = int(env["SESSIONGAUGE_POLL_INTERVAL_SECONDS"])
    if "SESSIONGAUGE_HERMES_DB_PATHS" in env:
        values["hermes_db_paths"] = [
            item for item in env["SESSIONGAUGE_HERMES_DB_PATHS"].split(os.pathsep) if item
        ]
    if "SESSIONGAUGE_DATA_DIR" in env:
        values["data_dir"] = env["SESSIONGAUGE_DATA_DIR"]
    if "SESSIONGAUGE_TIME_ZONE" in env:
        values["time_zone"] = env["SESSIONGAUGE_TIME_ZONE"]
    values.update({key: value for key, value in (cli or {}).items() if value is not None})
    try:
        ttl = int(values["sidebar_ttl_ms"])
    except (TypeError, ValueError) as exc:
        raise ValueError("sidebar ttl_ms must be an integer") from exc
    if not 1 <= ttl <= 86400000:
        raise ValueError("sidebar ttl_ms must be between 1 and 86400000")
    if not isinstance(values["sidebar_enabled"], bool):
        raise TypeError("sidebar.enabled must be a boolean")
    if not isinstance(values["sidebar_quota_enabled"], bool):
        raise TypeError("sidebar.quota.enabled must be a boolean")
    bin_path = values["sidebar_quota_agent_resume_bin"]
    if not isinstance(bin_path, str) or not bin_path.strip():
        raise ValueError("sidebar.quota.agent_resume_bin must be nonempty")
    bin_path = bin_path.strip()
    if bin_path.startswith("~"):
        bin_path = str(Path(bin_path).expanduser())
    quota_interval = values["sidebar_quota_poll_interval_seconds"]
    if type(quota_interval) is not int or not 30 <= quota_interval <= 3600:
        raise ValueError("sidebar.quota.poll_interval_seconds must be between 30 and 3600")
    time_zone = str(values.get("time_zone", "local"))
    if time_zone != "local":
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("time_zone must be a valid IANA zone") from exc
    paths = values.get("hermes_db_paths", ())
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        home = env.get("HERMES_HOME")
        root = Path(home).expanduser() if home else Path.home() / ".hermes"
        profile_dbs = (
            sorted((root / "profiles").glob("*/state.db")) if (root / "profiles").is_dir() else []
        )
        paths = [str(root / "state.db"), *(str(path) for path in profile_dbs)]
        paths = [item for item in paths if Path(item).is_file()] or [str(root / "state.db")]
    interval = int(values.get("poll_interval_seconds", 3))
    if not 2 <= interval <= 60:
        raise ValueError("poll_interval_seconds must be between 2 and 60")
    if (
        "ttl_ms" not in sidebar
        and "SESSIONGAUGE_SIDEBAR_TTL_MS" not in env
        and "sidebar_ttl_ms" not in (cli or {})
    ):
        ttl = max(15000, (2 * interval + 1) * 1000)
    canonical_paths = {}
    physical_paths = []
    for item in paths:
        expanded = Path(item).expanduser()
        if expanded.exists() and any(
            existing.exists() and os.path.samefile(expanded, existing)
            for existing in physical_paths
        ):
            continue
        canonical_paths.setdefault(str(expanded.resolve(strict=False)), expanded)
        physical_paths.append(expanded)
    return Config(
        interval,
        tuple(canonical_paths.values()),
        Path(values.get("data_dir", Config.data_dir)).expanduser(),
        time_zone,
        values["sidebar_enabled"],
        ttl,
        str(values["sidebar_time_zone"]),
        values["sidebar_quota_enabled"],
        bin_path,
        Path(values["sidebar_quota_hermes_home"]).expanduser(),
        quota_interval,
    )
