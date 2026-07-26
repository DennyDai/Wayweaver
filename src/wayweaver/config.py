import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

_ENV = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
_RESERVED = {"prefer", "cache_dir", "probe_ttl"}


def _expand(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.groups()
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is required")

        return _ENV.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class TargetConfig:
    name: str
    prefer: tuple[str, ...]
    adapters: dict[str, dict[str, Any]]
    probe_ttl: float = 30.0


@dataclass(frozen=True, slots=True)
class Config:
    targets: dict[str, TargetConfig]
    cache_dir: Path

    def target(self, name: str) -> TargetConfig:
        try:
            return self.targets[name]
        except KeyError as error:
            raise ConfigError(f"unknown target: {name}") from error


def default_config_path() -> Path:
    configured = os.environ.get("WAYWEAVER_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "wayweaver" / "targets.toml"


def load_config(path: str | Path | None = None) -> Config:
    source = Path(path).expanduser() if path else default_config_path()
    if not source.is_file():
        raise ConfigError(f"config not found: {source}")
    raw = _expand(tomllib.loads(source.read_text()))
    target_tables = raw.get("targets")
    if not isinstance(target_tables, dict) or not target_tables:
        raise ConfigError("config requires at least one [targets.NAME] table")
    targets = {}
    for name, table in target_tables.items():
        if not isinstance(table, dict):
            raise ConfigError(f"target {name} must be a table")
        preferred = table.get("prefer", [])
        if not isinstance(preferred, list) or not all(
            isinstance(item, str) for item in preferred
        ):
            raise ConfigError(f"target {name} prefer must be a string array")
        probe_ttl = table.get("probe_ttl", 30)
        if (
            isinstance(probe_ttl, bool)
            or not isinstance(probe_ttl, (int, float))
            or probe_ttl < 0
        ):
            raise ConfigError(f"target {name} probe_ttl must be a non-negative number")
        invalid = [
            key
            for key, value in table.items()
            if key not in _RESERVED and not isinstance(value, dict)
        ]
        if invalid:
            raise ConfigError(f"target {name} adapter {invalid[0]} must be a table")
        prefer = tuple(preferred)
        adapters = {key: value for key, value in table.items() if key not in _RESERVED}
        if not adapters:
            raise ConfigError(f"target {name} has no adapters")
        targets[name] = TargetConfig(name, prefer, adapters, float(probe_ttl))
    cache = Path(raw.get("cache_dir", "~/.cache/wayweaver")).expanduser()
    return Config(targets, cache)
