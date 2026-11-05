"""Хранение профилей хостов, секретов, настроек и сниппетов."""
from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "helm"
HOSTS_FILE = CONFIG_DIR / "hosts.json"
SECRETS_FILE = CONFIG_DIR / "secrets.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
KNOWN_HOSTS = CONFIG_DIR / "known_hosts"

DEFAULT_SNIPPETS = [
    {"name": "Место на дисках", "cmd": "df -h"},
    {"name": "Память", "cmd": "free -h"},
    {"name": "Топ процессов", "cmd": "ps aux --sort=-%cpu | head -20"},
    {"name": "Открытые порты", "cmd": "ss -tulpn"},
    {"name": "Docker: контейнеры", "cmd": "docker ps -a"},
    {"name": "Последние логи", "cmd": "journalctl -n 100 --no-pager"},
    {"name": "Кто залогинен", "cmd": "w"},
]


def ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data, private: bool = False) -> None:
    ensure_dir()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    if private:
        os.chmod(tmp, 0o600)
    tmp.replace(path)


@dataclass
class HostProfile:
    name: str
    host: str
    port: int = 22
    user: str = "root"
    auth: str = "auto"  # auto | password | key
    key_path: str = ""
    group: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source: str = "saved"  # saved | sshconfig

    @property
    def address(self) -> str:
        port = "" if self.port == 22 else f":{self.port}"
        return f"{self.user}@{self.host}{port}"

    @classmethod
    def from_dict(cls, d: dict) -> "HostProfile":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        known["port"] = int(known.get("port", 22) or 22)
        return cls(**known)


def load_hosts() -> list[HostProfile]:
    data = _read_json(HOSTS_FILE, [])
    out = []
    for d in data if isinstance(data, list) else []:
        try:
            out.append(HostProfile.from_dict(d))
        except TypeError:
            pass
    return out


def save_hosts(hosts: list[HostProfile]) -> None:
    _write_json(HOSTS_FILE, [asdict(h) for h in hosts if h.source == "saved"])


def ssh_config_hosts() -> list[HostProfile]:
    """Хосты из ~/.ssh/config (без масок)."""
    path = Path.home() / ".ssh" / "config"
    if not path.exists():
        return []
    try:
        import paramiko

        cfg = paramiko.SSHConfig.from_path(str(path))
    except Exception:
        return []
    out = []
    for alias in sorted(cfg.get_hostnames()):
        if any(c in alias for c in "*?!") or not alias:
            continue
        o = cfg.lookup(alias)
        keys = o.get("identityfile") or []
        out.append(
            HostProfile(
                name=alias,
                host=o.get("hostname", alias),
                port=int(o.get("port", 22)),
                user=o.get("user", os.environ.get("USER", "root")),
                auth="key" if keys else "auto",
                key_path=keys[0] if keys else "",
                group="~/.ssh/config",
                id="sshcfg:" + alias,
                source="sshconfig",
            )
        )
    return out


# --- секреты: файл 0600 (пароль слегка обфусцирован, это НЕ шифрование) ---

def get_secret(host_id: str) -> str | None:
    data = _read_json(SECRETS_FILE, {})
    v = data.get(host_id)
    if not v:
        return None
    try:
        return base64.b64decode(v.encode()).decode("utf-8")
    except Exception:
        return None


def set_secret(host_id: str, secret: str | None) -> None:
    data = _read_json(SECRETS_FILE, {})
    if secret:
        data[host_id] = base64.b64encode(secret.encode("utf-8")).decode()
    else:
        data.pop(host_id, None)
    _write_json(SECRETS_FILE, data, private=True)


# --- настройки ---

def load_settings() -> dict:
    s = _read_json(SETTINGS_FILE, {})
    s.setdefault("term_font_size", 11)
    s.setdefault("editor_font_size", 11)
    s.setdefault("show_hidden", False)
    s.setdefault("snippets", DEFAULT_SNIPPETS)
    return s


def save_settings(s: dict) -> None:
    _write_json(SETTINGS_FILE, s)
