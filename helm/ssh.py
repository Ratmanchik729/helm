"""SSH-сессия: подключение, выполнение команд, sudo, SFTP, PTY-каналы."""
from __future__ import annotations

import base64
import hashlib
import os
import shlex
import threading
import time

import paramiko

from . import config
from .config import HostProfile


class UnknownHostKey(Exception):
    def __init__(self, hostname: str, key: paramiko.PKey):
        super().__init__(f"Неизвестный ключ хоста {hostname}")
        self.hostname = hostname
        self.key = key

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.key.asbytes()).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class NeedSudoPassword(Exception):
    pass


class _AskPolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKey(hostname, key)


def trust_host_key(hostname: str, key: paramiko.PKey) -> None:
    config.ensure_dir()
    hk = paramiko.HostKeys()
    if config.KNOWN_HOSTS.exists():
        hk.load(str(config.KNOWN_HOSTS))
    hk.add(hostname, key.get_name(), key)
    hk.save(str(config.KNOWN_HOSTS))


def human_error(e: BaseException) -> str:
    if isinstance(e, paramiko.AuthenticationException):
        return "Ошибка аутентификации: неверный пароль или ключ"
    if isinstance(e, paramiko.BadHostKeyException):
        return ("ВНИМАНИЕ: ключ хоста изменился! Возможна MITM-атака.\n"
                "Если сервер переустановлен — удалите старый ключ из known_hosts.")
    if isinstance(e, TimeoutError) or "timed out" in str(e):
        return "Таймаут соединения"
    if isinstance(e, (ConnectionRefusedError,)):
        return "Соединение отклонено (порт закрыт?)"
    if isinstance(e, OSError) and e.errno == -2:
        return "Хост не найден (DNS)"
    if isinstance(e, PermissionError) or (isinstance(e, OSError) and e.errno == 13):
        return "Доступ запрещён"
    if isinstance(e, FileNotFoundError) or (isinstance(e, OSError) and e.errno == 2):
        return "Файл или каталог не найден"
    return str(e) or e.__class__.__name__


class Session:
    def __init__(self, profile: HostProfile, password: str | None = None,
                 passphrase: str | None = None):
        self.profile = profile
        self.password = password
        self.passphrase = passphrase
        self.client: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None
        self.sftp_lock = threading.RLock()
        self.sudo_password: str | None = password
        self._sudo_nopasswd: bool | None = None
        self.is_root = profile.user == "root"
        self.home = "/"
        self.uname = ""
        self.docker_mode: str | None = None  # direct | sudo | missing

    # ---------------- подключение ----------------
    def connect(self) -> None:
        p = self.profile
        c = paramiko.SSHClient()
        try:
            c.load_system_host_keys()
        except Exception:
            pass
        if config.KNOWN_HOSTS.exists():
            try:
                c.get_host_keys().load(str(config.KNOWN_HOSTS))
            except Exception:
                pass
        c.set_missing_host_key_policy(_AskPolicy())

        kw = dict(hostname=p.host, port=p.port, username=p.user, timeout=12,
                  banner_timeout=20, auth_timeout=25)
        key = os.path.expanduser(p.key_path) if p.key_path else None
        if p.auth == "password":
            kw.update(password=self.password, allow_agent=False, look_for_keys=False)
        elif p.auth == "key":
            kw.update(key_filename=key, passphrase=self.passphrase,
                      allow_agent=True, look_for_keys=key is None)
        else:
            kw.update(password=self.password, key_filename=key, passphrase=self.passphrase,
                      allow_agent=True, look_for_keys=True)
        c.connect(**kw)
        t = c.get_transport()
        t.set_keepalive(20)
        t.use_compression(False)
        self.client = c
        try:
            self.sftp = c.open_sftp()
            self.home = self.sftp.normalize(".")
        except Exception:
            self.sftp = None
        try:
            rc, out, _ = self._exec("id -u; uname -srm", 10)
            lines = out.splitlines()
            if lines:
                self.is_root = lines[0].strip() == "0"
            if len(lines) > 1:
                self.uname = lines[1].strip()
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        t = self.client.get_transport() if self.client else None
        return bool(t and t.is_active())

    @property
    def transport(self) -> paramiko.Transport:
        if not self.alive:
            raise ConnectionError("Соединение потеряно")
        return self.client.get_transport()

    def close(self) -> None:
        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass

    # ---------------- команды ----------------
    def _exec(self, cmd: str, timeout: float = 60, stdin_data: str | bytes | None = None):
        chan = self.transport.open_session(timeout=15)
        try:
            chan.settimeout(timeout)
            chan.exec_command(cmd)
            if stdin_data:
                chan.sendall(stdin_data.encode() if isinstance(stdin_data, str) else stdin_data)
            chan.shutdown_write()
            out, err = bytearray(), bytearray()
            deadline = time.monotonic() + timeout
            while True:
                got = False
                if chan.recv_ready():
                    out += chan.recv(65536)
                    got = True
                if chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                    got = True
                if not got:
                    if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"Команда не завершилась за {timeout} с")
                    time.sleep(0.005)
            rc = chan.recv_exit_status()
            while chan.recv_ready():
                out += chan.recv(65536)
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        finally:
            chan.close()

    def run(self, cmd: str, timeout: float = 60, sudo: bool = False,
            stdin_data: str | None = None):
        """Выполнить команду. Возвращает (rc, stdout, stderr).
        При sudo=True и отсутствии пароля бросает NeedSudoPassword."""
        if not sudo or self.is_root:
            return self._exec(cmd, timeout, stdin_data)
        if self._sudo_nopasswd is None:
            rc, _, _ = self._exec("sudo -n true", 10)
            self._sudo_nopasswd = rc == 0
        wrapped = "sh -c " + shlex.quote(cmd)
        if self._sudo_nopasswd:
            return self._exec("sudo -n " + wrapped, timeout, stdin_data)
        if not self.sudo_password:
            raise NeedSudoPassword("Нужен пароль sudo")
        rc, out, err = self._exec("sudo -S -p '' " + wrapped, timeout,
                                  self.sudo_password + "\n" + (stdin_data or ""))
        low = err.lower()
        if rc != 0 and ("incorrect password" in low or "sorry, try again" in low
                        or "неверный пароль" in low or "no password was provided" in low):
            self.sudo_password = None
            raise NeedSudoPassword("Неверный пароль sudo")
        return rc, out, err

    def check(self, cmd: str, timeout: float = 60, sudo: bool = False) -> str:
        rc, out, err = self.run(cmd, timeout, sudo)
        if rc != 0:
            raise RuntimeError((err or out).strip() or f"Код возврата {rc}")
        return out

    # ---------------- docker ----------------
    def detect_docker(self) -> str:
        if self.docker_mode:
            return self.docker_mode
        rc, out, err = self._exec(
            "command -v docker >/dev/null 2>&1 || { echo missing; exit 0; }; "
            "docker ps -q >/dev/null 2>&1 && echo direct || echo sudo", 20)
        self.docker_mode = out.strip().splitlines()[-1] if out.strip() else "missing"
        return self.docker_mode

    def docker(self, args: str, timeout: float = 60):
        mode = self.detect_docker()
        if mode == "missing":
            raise RuntimeError("Docker не установлен на сервере")
        return self.run("docker " + args, timeout, sudo=(mode == "sudo"))

    @property
    def docker_prefix(self) -> str:
        return "sudo docker" if self.docker_mode == "sudo" and not self.is_root else "docker"

    # ---------------- SFTP ----------------
    def open_sftp(self) -> paramiko.SFTPClient:
        return self.client.open_sftp()

    def listdir(self, path: str):
        import stat as st
        with self.sftp_lock:
            items = self.sftp.listdir_attr(path)
            out = []
            for a in items:
                is_link = st.S_ISLNK(a.st_mode or 0)
                is_dir = st.S_ISDIR(a.st_mode or 0)
                target = ""
                if is_link:
                    full = path.rstrip("/") + "/" + a.filename
                    try:
                        target = self.sftp.readlink(full) or ""
                        is_dir = st.S_ISDIR(self.sftp.stat(full).st_mode or 0)
                    except Exception:
                        pass
                owner = ""
                parts = (a.longname or "").split()
                if len(parts) >= 4:
                    owner = f"{parts[2]}:{parts[3]}"
                out.append(dict(name=a.filename, size=a.st_size or 0, mtime=a.st_mtime or 0,
                                mode=a.st_mode or 0, is_dir=is_dir, is_link=is_link,
                                target=target, owner=owner))
            return out

    def read_file(self, path: str, limit: int = 20 * 1024 * 1024) -> bytes:
        with self.sftp_lock:
            size = self.sftp.stat(path).st_size or 0
            if size > limit:
                raise RuntimeError(f"Файл слишком большой ({size // 1024 // 1024} МБ)")
            with self.sftp.open(path, "rb") as f:
                f.prefetch()
                return f.read()

    def write_file(self, path: str, data: bytes, use_sudo: bool = False) -> None:
        if not use_sudo:
            with self.sftp_lock:
                with self.sftp.open(path, "wb") as f:
                    f.set_pipelined(True)
                    f.write(data)
            return
        tmp = f"/tmp/.helm-{os.getpid()}-{int(time.time() * 1000)}"
        with self.sftp_lock:
            with self.sftp.open(tmp, "wb") as f:
                f.write(data)
            self.sftp.chmod(tmp, 0o600)
        q = shlex.quote
        try:
            self.check(f"cat {q(tmp)} > {q(path)}", 30, sudo=True)
        finally:
            try:
                self._exec(f"rm -f {q(tmp)}", 10)
            except Exception:
                pass

    # ---------------- PTY ----------------
    def open_pty(self, cols: int, rows: int, command: str | None = None) -> paramiko.Channel:
        chan = self.transport.open_session(timeout=15)
        chan.get_pty(term="xterm-256color", width=cols, height=rows)
        try:
            chan.update_environment({"COLORTERM": "truecolor"})
        except Exception:
            pass
        if command:
            chan.exec_command(command)
        else:
            chan.invoke_shell()
        return chan
