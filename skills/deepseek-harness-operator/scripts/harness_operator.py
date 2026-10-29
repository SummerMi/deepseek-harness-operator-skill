#!/usr/bin/env python3
"""Locate, start, inspect and connect to a background DeepSeek Harness web host.

Standard library only. Windows is the primary platform: ``start`` is Windows
only. ``discover``, ``status``, log parsing and ``connect-script`` also run on
other platforms, but they are not claimed as full launch support there.

Safety rules baked into this helper:

* The authenticated launch URL is read from a private log and kept in memory.
  It is never printed, and error messages are redacted.
* Runtime state (state file, startup logs, generated connect module) lives in a
  private directory *outside* the Harness profile directory, so initialising a
  profile never renames files the host currently owns.
* An existing healthy helper record is reused. An unclear record refuses to
  start a second host. Another process is never killed.
* A start is serialised by an exclusive lock file in the state directory.
* TCP reachability is reported as reachability only, never as proof that a
  model session works.

Exit codes: 0 success, 1 operational failure, 2 refusal (unsafe or ambiguous).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

HELPER_NAME = "deepseek-harness-operator"
STATE_VERSION = 2
STATE_FILENAME = "operator-state.json"
LOCK_FILENAME = "start.lock"

DEFAULT_PROFILE = "codex-operator"
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

LISTEN_HOST = "127.0.0.1"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

DEFAULT_SPACE_NAME = "DeepSeek Harness Operator"

EXE_NAME = "DeepSeek Harness.exe"
CLI_RELPATH = Path(
    "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
)
# Path of the same file relative to the asar archive root.
ASAR_MEMBER = Path(*CLI_RELPATH.parts[2:])
ASAR_HEADER_LIMIT = 64 * 1024 * 1024

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI sequences
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences
    r"|\x1b[@-Z\\-_]"  # single character escapes
)
URL_RE = re.compile(r"https?://[^\s\"'<>`]+", re.IGNORECASE)
SECRET_PARAM_RE = re.compile(
    r"([?&#][A-Za-z0-9_-]*(?:token|key|secret|auth|ticket|code|session)[A-Za-z0-9_-]*=)[^&\s\"'<>]+",
    re.IGNORECASE,
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

POLL_INTERVAL = 0.25
MAX_POLLS = 20000


class OperatorError(Exception):
    """A recoverable operational failure."""


class Refusal(OperatorError):
    """The helper declined to act; doing so could damage or duplicate state."""


# --------------------------------------------------------------------------
# Redaction and URL parsing
# --------------------------------------------------------------------------


def strip_ansi(text: str) -> str:
    """Remove ANSI colour and cursor sequences from console output."""
    if not text:
        return text
    return ANSI_RE.sub("", text)


def _mask_url(raw: str) -> str:
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "[redacted-url]"
    if parts.username is not None or parts.password is not None:
        return "[redacted-url]"
    if parts.query or parts.fragment:
        netloc = parts.netloc
        return "http://%s/[redacted]" % netloc if parts.scheme == "http" else "[redacted-url]"
    return raw


def redact(text: str) -> str:
    """Strip credentials from text that is about to be printed."""
    if not text:
        return text
    text = strip_ansi(text)
    text = URL_RE.sub(lambda match: _mask_url(match.group(0)), text)
    text = SECRET_PARAM_RE.sub(r"\1[redacted]", text)
    return text


def _url_rejection(parts) -> Optional[str]:
    if parts.scheme.lower() != "http":
        return "unsupported scheme (http only)"
    if parts.username is not None or parts.password is not None:
        return "embedded credentials are not accepted"
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "unparsable host or port"
    if not host:
        return "missing host"
    if host.lower() not in LOOPBACK_HOSTS:
        return "host is not loopback"
    if port is None:
        return "missing port"
    if not 0 < port <= 65535:
        return "port out of range"
    return None


def _netloc(host: str, port: int) -> str:
    return "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)


def parse_origin(text: str) -> Dict[str, Any]:
    """Find the last acceptable authenticated Harness URL in console output.

    Returns the full URL for internal use only under ``url``; ``origin`` is the
    credential-free part that may be printed.
    """
    result: Dict[str, Any] = {
        "found": False,
        "origin": None,
        "url": None,
        "host": None,
        "port": None,
        "rejected": [],
    }
    if not text:
        return result
    cleaned = strip_ansi(text)
    for raw in URL_RE.findall(cleaned):
        raw = raw.rstrip(").,;:'\"")
        try:
            parts = urlsplit(raw)
        except ValueError:
            result["rejected"].append({"sample": "[unparsable]", "reason": "unparsable URL"})
            continue
        reason = _url_rejection(parts)
        if reason:
            result["rejected"].append({"sample": redact(raw), "reason": reason})
            continue
        host = parts.hostname.lower()
        result["found"] = True
        result["origin"] = "http://%s" % _netloc(host, parts.port)
        result["url"] = raw  # internal only, never printed
        result["host"] = host
        result["port"] = parts.port
    return result


def read_log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# ASAR archives and installation discovery
# --------------------------------------------------------------------------


def read_asar_index(archive: Path) -> Dict[str, Any]:
    """Read the JSON index that Electron's asar archives start with.

    Two layouts are in use. The plain one is ``uint32 4 | uint32 length |
    JSON``, so the JSON starts at byte 8. Installers built with the nested
    pickle layout repeat the length fields, and the JSON starts at byte 16.
    Both are tried, and the payload is never unpacked.
    """
    try:
        with archive.open("rb") as handle:
            head = handle.read(16)
            if len(head) < 8:
                raise OperatorError("asar archive is truncated: %s" % archive)
            words = [int.from_bytes(head[i : i + 4], "little") for i in (0, 4, 8, 12) if len(head) >= i + 4]
            plans = [(8, words[1])]
            if len(words) >= 4:
                plans.append((16, words[3]))
                plans.append((12, words[2]))
            for offset, length in plans:
                if not 0 < length <= ASAR_HEADER_LIMIT:
                    continue
                handle.seek(offset)
                raw = handle.read(length)
                try:
                    index = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(index, dict) and isinstance(index.get("files"), dict):
                    return index
    except OSError as exc:
        raise OperatorError("cannot read asar archive %s: %s" % (archive, exc))
    raise OperatorError("asar header could not be located or parsed: %s" % archive)


def asar_lookup(index: Dict[str, Any], parts: Sequence[str]) -> Optional[Dict[str, Any]]:
    node: Any = index
    for part in parts:
        files = node.get("files") if isinstance(node, dict) else None
        if not isinstance(files, dict) or part not in files:
            return None
        node = files[part]
    return node if isinstance(node, dict) else None


def resolve_host_cli(root: Path) -> Optional[Dict[str, str]]:
    """Return the host CLI entry and how it is stored.

    Handles an unpacked install (real file), a packaged install (member inside
    ``app.asar``, validated against the archive index), and an unpacked sidecar
    directory when the member is marked ``unpacked``.
    """
    direct = root / CLI_RELPATH
    if direct.is_file():
        return {"path": str(direct), "kind": "on-disk"}

    archive = root / "resources" / "app.asar"
    if not archive.is_file():
        return None
    try:
        member = asar_lookup(read_asar_index(archive), ASAR_MEMBER.parts)
    except OperatorError:
        return None
    if member is None:
        return None
    if member.get("unpacked"):
        sidecar = archive.with_name(archive.name + ".unpacked") / ASAR_MEMBER
        if sidecar.is_file():
            return {"path": str(sidecar), "kind": "unpacked-sidecar"}
        return None
    return {"path": str(direct), "kind": "asar"}


@dataclass(frozen=True)
class Install:
    name: str
    root: Path
    executable: Path
    cli_script: Optional[str]
    cli_kind: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "install_root": str(self.root),
            "executable": str(self.executable),
            "cli_script": self.cli_script,
            "cli_kind": self.cli_kind,
        }


def natural_key(value: str) -> List[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def programs_dir(env: Optional[Dict[str, str]] = None) -> Path:
    env = env if env is not None else os.environ
    local = env.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Programs"
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "Programs"
    return Path.home() / "Applications"


def install_from_root(root: Path, name: Optional[str] = None) -> Install:
    cli = resolve_host_cli(root)
    return Install(
        name=name or root.name,
        root=root,
        executable=root / EXE_NAME,
        cli_script=cli["path"] if cli else None,
        cli_kind=cli["kind"] if cli else None,
    )


def iter_installs(search_dir: Path) -> List[Install]:
    found: List[Install] = []
    try:
        children = sorted(
            (child for child in search_dir.iterdir() if child.is_dir()),
            key=lambda child: natural_key(child.name),
        )
    except OSError:
        return found
    for child in children:
        if not child.name.lower().startswith("deepseek harness"):
            continue
        install = install_from_root(child)
        if install.executable.is_file():
            found.append(install)
    return found


def _root_from_cli(cli: Path) -> Optional[Path]:
    """Walk up from cli.js until the directory that contains the executable."""
    for parent in list(cli.parents)[:12]:
        if (parent / EXE_NAME).is_file():
            return parent
    return None


def discover(
    *,
    executable: Optional[str] = None,
    cli_script: Optional[str] = None,
    search_dir: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Locate the installed DeepSeek Harness executable and host CLI script."""
    search = search_dir or programs_dir(env)
    candidates = [install.as_dict() for install in iter_installs(search)]

    chosen_exec: Optional[Path] = None
    chosen_cli: Optional[str] = None
    chosen_kind: Optional[str] = None
    source = None

    if executable:
        chosen_exec = Path(executable).expanduser()
        if not chosen_exec.is_file():
            raise OperatorError("--executable does not point to a file: %s" % chosen_exec)
        derived = install_from_root(chosen_exec.parent)
        chosen_cli = derived.cli_script
        chosen_kind = derived.cli_kind
        source = "--executable"
    if cli_script:
        explicit = Path(cli_script).expanduser()
        if chosen_exec is None:
            root = _root_from_cli(explicit)
            if root is None:
                raise OperatorError(
                    "--cli-script is not inside a DeepSeek Harness install that contains %s" % EXE_NAME
                )
            chosen_exec = root / EXE_NAME
        resolved = resolve_host_cli(chosen_exec.parent)
        if resolved is None:
            raise OperatorError(
                "--cli-script %s is not a member of the installed archive at %s"
                % (explicit, chosen_exec.parent)
            )
        chosen_cli = resolved["path"]
        chosen_kind = resolved["kind"]
        source = "--cli-script"

    if chosen_exec is None:
        if not candidates:
            raise OperatorError(
                "no DeepSeek Harness installation found under %s; pass --executable or --cli-script"
                % search
            )
        chosen = iter_installs(search)[-1]
        chosen_exec = chosen.executable
        chosen_cli = chosen.cli_script
        chosen_kind = chosen.cli_kind
        source = "localappdata-scan"

    if chosen_cli is None:
        raise OperatorError(
            "host CLI script missing: %s is not present inside %s/app.asar (or on disk)"
            % (ASAR_MEMBER.as_posix(), chosen_exec.parent / "resources")
        )

    return {
        "ok": True,
        "source": source,
        "executable": str(chosen_exec),
        "cli_script": chosen_cli,
        "cli_kind": chosen_kind,
        "install_root": str(chosen_exec.parent),
        "search_dir": str(search),
        "candidates": candidates,
        "notes": [
            "reads only; no user configuration was written",
            "an asar member is a virtual path: Electron loads it, Python cannot stat it",
            "port, profile and PID are runtime values and must be re-checked each run",
        ],
    }


# --------------------------------------------------------------------------
# Process identity, sockets, paths
# --------------------------------------------------------------------------


def read_process_identity(pid: int, platform: Optional[str] = None) -> Dict[str, Any]:
    """Return PID plus a creation stamp used to detect PID reuse."""
    platform = platform if platform is not None else os.name
    record: Dict[str, Any] = {
        "pid": int(pid),
        "platform": platform,
        "creation_time": None,
        "alive": None,
        "verifiable": False,
    }
    if platform != "nt":
        return record
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - only on unusual Windows builds
        return record

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        return record

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        # A missing PID is different from an inaccessible process. Only the
        # former permits a stale record to be replaced on the next start.
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: the PID does not exist
            record["alive"] = False
            record["verifiable"] = True
        return record
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        got_times = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        code = wintypes.DWORD()
        got_code = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        if not got_times or not got_code:
            return record
        record["creation_time"] = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        record["alive"] = code.value == STILL_ACTIVE
        record["verifiable"] = True
        return record
    finally:
        kernel32.CloseHandle(handle)


def compare_identity(recorded: Optional[Dict[str, Any]], current: Optional[Dict[str, Any]]) -> str:
    """Return one of: match, mismatch, unverifiable, unknown.

    A record only matches when both the PID and the process creation stamp
    agree, so a recycled PID is not mistaken for the host we started.
    """
    if not recorded or recorded.get("pid") is None:
        return "unknown"
    if not current or not current.get("verifiable"):
        return "unverifiable"
    if current.get("alive") is False:
        return "mismatch"
    if int(recorded["pid"]) != int(current.get("pid", -1)):
        return "mismatch"
    if recorded.get("creation_time") is None:
        return "unverifiable"
    if int(recorded["creation_time"]) != int(current["creation_time"]):
        return "mismatch"
    return "match"


def port_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _norm(path: Any) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _inside(child: Any, parent: Any) -> bool:
    child_text, parent_text = _norm(child), _norm(parent)
    return child_text == parent_text or child_text.startswith(parent_text + os.sep)


def resolve_paths(
    profile: str,
    *,
    dsh_home: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Path]:
    home = Path(dsh_home or os.environ.get("DSH_HOME") or (Path.home() / ".dsh")).expanduser()
    profile_dir = home / "profiles" / profile
    private = Path(
        state_dir or os.environ.get("DSH_OPERATOR_STATE_DIR") or (home / "operator-private" / profile)
    ).expanduser()
    if _inside(private, profile_dir):
        raise Refusal(
            "state directory %s is inside the Harness profile directory %s. "
            "Private state must live outside the profile, because profile initialisation "
            "renames that directory; choose another --state-dir." % (private, profile_dir)
        )
    return {
        "dsh_home": home,
        "profile_dir": profile_dir,
        "state_dir": private,
        "state_file": private / STATE_FILENAME,
        "lock_file": private / LOCK_FILENAME,
    }


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def validate_profile(profile: str) -> str:
    if not profile or not PROFILE_RE.match(profile):
        raise Refusal(
            "invalid profile name %r: use letters, digits, underscore or hyphen, "
            "start with a letter or digit, at most 48 characters" % profile
        )
    return profile


def validate_port(port: int) -> int:
    if not isinstance(port, int) or port < 0 or port > 65535:
        raise OperatorError("port must be 0 (choose a free port) or 1-65535")
    return port


def validate_state_origin(origin: Any, port: Any) -> Dict[str, Any]:
    """Re-derive the credential-free origin recorded in a state file.

    A state file is editable, so nothing in it is trusted: the scheme, userinfo,
    host, query and fragment are re-checked before the value is used or printed.
    """
    if not isinstance(origin, str) or not isinstance(port, int):
        raise Refusal("state origin/port have the wrong type; refusing to use this record")
    try:
        parts = urlsplit(origin)
        parsed_port = parts.port
    except ValueError:
        raise Refusal("state origin is not a parsable URL; refusing to use this record")
    if parts.scheme != "http" or parts.username is not None or parts.password is not None:
        raise Refusal("state origin is not a plain http loopback URL; refusing to use this record")
    if parts.query or parts.fragment:
        raise Refusal(
            "state origin carries query or fragment parameters; refusing to use or print this record"
        )
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise Refusal("state origin host is not loopback; refusing to contact it")
    if parsed_port is None or parsed_port != port:
        raise Refusal("state origin port does not match the recorded port; refusing to use this record")
    return {"origin": "http://%s" % _netloc(host, parsed_port), "host": host, "port": parsed_port}


def load_state(paths: Dict[str, Path], profile: str) -> Optional[Dict[str, Any]]:
    state_file = paths["state_file"]
    try:
        raw = state_file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        raise Refusal(
            "state file is not valid JSON; inspect %s before starting a new host" % state_file
        )
    if not isinstance(state, dict) or state.get("helper") != HELPER_NAME:
        raise Refusal("state file %s was not written by this helper" % state_file)
    if state.get("version") != STATE_VERSION:
        raise Refusal(
            "state file %s has version %r, this helper writes version %d; inspect it before reuse"
            % (state_file, state.get("version"), STATE_VERSION)
        )
    if state.get("profile") != profile:
        raise Refusal(
            "state file %s belongs to profile %r, not %r" % (state_file, state.get("profile"), profile)
        )
    pid = state.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise Refusal("state file %s has an invalid PID; inspect it" % state_file)
    identity = state.get("identity")
    if not isinstance(identity, dict) or not isinstance(identity.get("pid"), int):
        raise Refusal("state file %s has an invalid process identity; inspect it" % state_file)
    if state.get("origin") is not None:
        checked = validate_state_origin(state.get("origin"), state.get("port"))
        state["origin"] = checked["origin"]
        state["host"] = checked["host"]
        state["port"] = checked["port"]
    return state


def save_state(paths: Dict[str, Path], state: Dict[str, Any]) -> None:
    paths["state_dir"].mkdir(parents=True, exist_ok=True)
    tmp = paths["state_file"].with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, paths["state_file"])


class StartLock:
    """Exclusive, non-destructive lock around one start attempt."""

    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def __enter__(self) -> "StartLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            detail = ""
            try:
                detail = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                detail = "unreadable"
            raise Refusal(
                "another start already holds %s (%s). Refusing to start a second host; "
                "inspect that process, then remove the lock yourself if it is truly stale."
                % (self.path, redact(detail) or "no detail")
            )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {
                        "helper": HELPER_NAME,
                        "pid": os.getpid(),
                        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                )
                + "\n"
            )
        self.held = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.held:
            try:
                os.unlink(str(self.path))
            except OSError:
                pass
            self.held = False


# --------------------------------------------------------------------------
# Dependencies (injectable for tests)
# --------------------------------------------------------------------------


def _default_launcher(
    cmd: Sequence[str],
    env: Dict[str, str],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> int:
    if os.name != "nt":
        raise OperatorError(
            "start is only supported on Windows; discovery, status and connect-script still work"
        )
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(cmd),
            env=env,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
    return int(proc.pid)


def default_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


@dataclass
class Deps:
    platform: str = os.name
    launcher: Callable[..., int] = _default_launcher
    identity: Callable[[int], Dict[str, Any]] = read_process_identity
    port_probe: Callable[[str, int, float], bool] = port_reachable
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic
    timestamp: Optional[Callable[[], str]] = None


def default_deps() -> Deps:
    return Deps()


# --------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------


def _read_origin_from_logs(state: Dict[str, Any]) -> Dict[str, Any]:
    text = "".join(
        read_log_text(Path(str(state[key])))
        for key in ("stdout_log", "stderr_log")
        if isinstance(state.get(key), str)
    )
    return parse_origin(text)


def _wait_for_url(log_paths: Sequence[Path], deadline: float, deps: Deps) -> Dict[str, Any]:
    polls = 0
    info = parse_origin("")
    while polls < MAX_POLLS:
        polls += 1
        info = parse_origin("".join(read_log_text(path) for path in log_paths))
        if info["found"]:
            return info
        if deps.now() >= deadline:
            return info
        deps.sleep(POLL_INTERVAL)
    return info


def _wait_for_port(state: Dict[str, Any], timeout: float, deps: Deps) -> Dict[str, Any]:
    """Health gate: identity must still be alive and the port must answer."""
    deadline = deps.now() + timeout
    polls = 0
    while polls < MAX_POLLS:
        polls += 1
        current = deps.identity(int(state["pid"]))
        if current.get("alive") is False:
            return {"ready": False, "reason": "the recorded process has exited"}
        if deps.port_probe(state["host"], int(state["port"]), 0.5):
            return {"ready": True, "reason": None}
        if deps.now() >= deadline:
            return {"ready": False, "reason": "the port is not accepting connections yet"}
        deps.sleep(POLL_INTERVAL)
    return {"ready": False, "reason": "health check did not settle"}


def refresh_origin(paths: Dict[str, Path], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pick up a late printed URL for the same record, without a new process."""
    info = _read_origin_from_logs(state)
    if not info["found"]:
        return None
    state["origin"] = info["origin"]
    state["host"] = info["host"]
    state["port"] = info["port"]
    save_state(paths, state)
    return state


def start(
    profile: str = DEFAULT_PROFILE,
    *,
    dsh_home: Optional[str] = None,
    state_dir: Optional[str] = None,
    port: int = 0,
    timeout: float = 30.0,
    install: Optional[Dict[str, Any]] = None,
    deps: Optional[Deps] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Launch the background web host, or reuse a verified healthy one."""
    deps = deps or default_deps()
    env = env if env is not None else os.environ
    validate_profile(profile)
    validate_port(port)

    if deps.platform != "nt":
        raise OperatorError(
            "start is only supported on Windows; discovery, status and connect-script still work"
        )

    paths = resolve_paths(profile, dsh_home=dsh_home, state_dir=state_dir)
    paths["state_dir"].mkdir(parents=True, exist_ok=True)

    with StartLock(paths["lock_file"]):
        return _start_locked(
            profile,
            paths,
            port=port,
            timeout=timeout,
            install=install,
            deps=deps,
            env=env,
        )


def _start_locked(
    profile: str,
    paths: Dict[str, Path],
    *,
    port: int,
    timeout: float,
    install: Optional[Dict[str, Any]],
    deps: Deps,
    env: Dict[str, str],
) -> Dict[str, Any]:
    existing = load_state(paths, profile)
    stale = False

    if existing:
        recorded = existing.get("identity") or {"pid": existing.get("pid")}
        current = deps.identity(int(existing["pid"]))
        verdict = compare_identity(recorded, current)

        if verdict == "match":
            if not existing.get("port"):
                existing = refresh_origin(paths, existing) or existing
            if not existing.get("port"):
                return {
                    "ok": False,
                    "action": "reused",
                    "ready": False,
                    "profile": profile,
                    "pid": existing.get("pid"),
                    "origin": None,
                    "state_file": str(paths["state_file"]),
                    "reason": (
                        "the recorded host is alive but has not printed an acceptable loopback URL yet"
                    ),
                    "next": "run status again shortly; this helper will not start a second copy",
                }
            health = _wait_for_port(existing, timeout, deps)
            if not health["ready"]:
                return {
                    "ok": False,
                    "action": "reused",
                    "ready": False,
                    "profile": profile,
                    "pid": existing.get("pid"),
                    "origin": existing.get("origin"),
                    "state_file": str(paths["state_file"]),
                    "reason": health["reason"],
                    "next": "inspect the host and its startup log; this helper will not start a second copy",
                }
            return {
                "ok": True,
                "action": "reused",
                "ready": True,
                "profile": profile,
                "pid": existing.get("pid"),
                "origin": existing.get("origin"),
                "state_file": str(paths["state_file"]),
                "state_dir": str(paths["state_dir"]),
                "stdout_log": existing.get("stdout_log"),
                "stderr_log": existing.get("stderr_log"),
                "note": "port is reachable; this is not proof that a model session works",
            }
        if verdict == "unverifiable":
            raise Refusal(
                "an existing record for profile %r cannot be verified (PID %s). "
                "Refusing to start a second host; inspect %s and the process, then start manually."
                % (profile, existing.get("pid"), paths["state_file"])
            )
        stale = True

    profile_dir: Path = paths["profile_dir"]
    init_used = False
    if profile_dir.exists():
        if not (profile_dir / "cordis.yml").is_file():
            raise Refusal(
                "profile directory %s exists but has no cordis.yml. "
                "Refusing to guess or overwrite it; inspect or remove it yourself." % profile_dir
            )
    else:
        init_used = True

    if install is None:
        install = discover(env=env)

    run_id = (deps.timestamp or default_timestamp)()
    suffix = 1
    stdout_log = paths["state_dir"] / ("startup-%s.stdout.log" % run_id)
    while stdout_log.exists():
        suffix += 1
        stdout_log = paths["state_dir"] / ("startup-%s-%d.stdout.log" % (run_id, suffix))
    stderr_log = stdout_log.with_name(stdout_log.name.replace(".stdout.log", ".stderr.log"))

    cmd: List[str] = [
        str(install["executable"]),
        "--expose-internals",
        str(install["cli_script"]),
        "--profile",
        profile,
    ]
    if init_used:
        cmd.extend(["--from-default-profile", "web"])
    cmd.extend(["--no-open", "--host", LISTEN_HOST, "--port", str(port)])

    child_env = dict(env)
    child_env["ELECTRON_RUN_AS_NODE"] = "1"
    child_env["DSH_HOME"] = str(paths["dsh_home"].resolve())

    try:
        pid = int(deps.launcher(cmd, child_env, paths["state_dir"], stdout_log, stderr_log))
    except OSError as exc:
        raise OperatorError("could not launch the host: %s" % redact(str(exc)))

    record: Dict[str, Any] = {
        "helper": HELPER_NAME,
        "version": STATE_VERSION,
        "profile": profile,
        "pid": pid,
        "identity": deps.identity(pid),
        "origin": None,
        "host": None,
        "port": None,
        "init_used": init_used,
        "install_root": install.get("install_root"),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "state_dir": str(paths["state_dir"]),
        "profile_dir": str(profile_dir),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_state(paths, record)

    info = _wait_for_url([stdout_log, stderr_log], deps.now() + timeout, deps)
    if not info["found"]:
        return {
            "ok": False,
            "action": "started",
            "ready": False,
            "profile": profile,
            "pid": pid,
            "origin": None,
            "previous_record_stale": stale,
            "state_file": str(paths["state_file"]),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
            "reason": "no acceptable loopback URL appeared within %gs" % timeout,
            "next": (
                "run status again to pick up a late URL from the same log; this helper will not "
                "restart automatically and will not kill the process it started"
            ),
        }

    record.update({"origin": info["origin"], "host": info["host"], "port": info["port"]})
    save_state(paths, record)

    health = _wait_for_port(record, timeout, deps)
    if not health["ready"]:
        return {
            "ok": False,
            "action": "started",
            "ready": False,
            "profile": profile,
            "pid": pid,
            "origin": record["origin"],
            "state_file": str(paths["state_file"]),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
            "reason": health["reason"],
            "next": "run status again; the URL and port are already recorded for the same process",
        }

    return {
        "ok": True,
        "action": "started",
        "ready": True,
        "profile": profile,
        "pid": pid,
        "origin": record["origin"],
        "state_file": str(paths["state_file"]),
        "state_dir": str(paths["state_dir"]),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "initialized_profile": init_used,
        "previous_record_stale": stale,
        "notes": [
            "the authenticated URL is withheld; it stays in the private startup log",
            "port is reachable; this is not proof that a model session works",
        ],
    }


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def status(
    profile: str = DEFAULT_PROFILE,
    *,
    dsh_home: Optional[str] = None,
    state_dir: Optional[str] = None,
    deps: Optional[Deps] = None,
) -> Dict[str, Any]:
    deps = deps or default_deps()
    validate_profile(profile)
    paths = resolve_paths(profile, dsh_home=dsh_home, state_dir=state_dir)
    state = load_state(paths, profile)

    if not state:
        return {
            "ok": True,
            "profile": profile,
            "state_present": False,
            "healthy": False,
            "state_file": str(paths["state_file"]),
            "process": "no record",
            "socket": "unknown",
            "note": "no helper record; run discover and start",
        }

    if not state.get("port"):
        state = refresh_origin(paths, state) or state

    verdict = compare_identity(state.get("identity") or {"pid": state.get("pid")}, deps.identity(state["pid"]))

    socket_state = "unknown"
    if state.get("port"):
        socket_state = (
            "open" if deps.port_probe(state["host"], int(state["port"]), 0.5) else "closed"
        )

    healthy = verdict == "match" and socket_state == "open"
    if verdict == "mismatch":
        process_text = "PID reused or process exited"
    elif verdict == "match":
        process_text = "alive and identity matched"
    else:
        process_text = verdict

    return {
        "ok": True,
        "profile": profile,
        "state_present": True,
        "healthy": healthy,
        "pid": state.get("pid"),
        "origin": state.get("origin"),
        "port": state.get("port"),
        "process": process_text,
        "identity_verdict": verdict,
        "socket": socket_state,
        "state_file": str(paths["state_file"]),
        "stdout_log": state.get("stdout_log"),
        "note": (
            "socket open only means the port accepts TCP; it is not proof that a model "
            "session or a tool call works"
        ),
    }


# --------------------------------------------------------------------------
# connect-script
# --------------------------------------------------------------------------

CONNECT_TEMPLATE = r"""// DeepSeek Harness operator connect module (generated locally, private).
//
// Attaches ego-browser to the already authenticated DeepSeek Harness web UI in
// a task space this operator controls. It connects and shows the initial
// interface only; it does not dispatch any project task.
//
// Run it through scripts/run_ego.ps1, which masks authenticated URLs in the
// output. The module never prints the authenticated URL itself.

import { readFileSync } from 'node:fs';

const STATE_FILE = __STATE_FILE__;
const SPACE_NAME = __SPACE_NAME__;
const CLAIM_SPACE_ID = __CLAIM_SPACE__;

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);
const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;

function mask(text) {
  return String(text).replace(/https?:\/\/[^\s"'<>`]+/gi, (raw) => {
    try {
      return new URL(raw).origin + '/[redacted]';
    } catch {
      return '[redacted-url]';
    }
  });
}

function originOf(value) {
  try {
    return new URL(String(value)).origin;
  } catch {
    return null;
  }
}

function readAuthUrl(state) {
  const logs = [state.stdout_log, state.stderr_log].filter((item) => typeof item === 'string');
  let found = null;
  for (const file of logs) {
    let text = '';
    try {
      text = readFileSync(file, 'utf8');
    } catch {
      continue;
    }
    for (const raw of text.replace(ANSI, ' ').match(/https?:\/\/[^\s"'<>`]+/gi) || []) {
      let candidate;
      try {
        candidate = new URL(raw);
      } catch {
        continue;
      }
      if (candidate.protocol !== 'http:') continue;
      if (!LOOPBACK.has(candidate.hostname)) continue;
      if (candidate.username || candidate.password) continue;
      if (!candidate.port) continue;
      found = candidate; // latest acceptable entry wins; stays in memory only
    }
  }
  return found;
}

async function openSpace() {
  if (CLAIM_SPACE_ID !== null) {
    const spaces = (await listTaskSpaces()) || [];
    const entry = spaces.find((item) => Number(item.id) === CLAIM_SPACE_ID);
    if (!entry) {
      throw new Error(
        'no task space with id ' + CLAIM_SPACE_ID + '; list the spaces before claiming one'
      );
    }
    return await claimTaskSpace(CLAIM_SPACE_ID);
  }

  let space;
  try {
    space = await taskSpace(SPACE_NAME);
  } catch (error) {
    const text = String(error && error.message ? error.message : error);
    if (text.includes('EGO_TASK_SPACE_NAME_IN_USE')) {
      throw new Error(
        'a task space named "' +
          SPACE_NAME +
          '" exists but is not agent-owned; ask the user whether to work there, then re-run with --claim-space <id>'
      );
    }
    throw error;
  }
  if (space && space.ownership === 'user') {
    throw new Error(
      'the task space named "' +
        SPACE_NAME +
        '" is user-owned; ask the user, then re-run with --claim-space <id>'
    );
  }
  return space;
}

async function openPage(space, authUrl) {
  const origin = authUrl.origin;
  const tabs = (await space.tabs()) || [];
  const matches = tabs.filter((tab) => originOf(tab && tab.url) === origin);

  if (matches.length > 1) {
    const labels = matches.map((tab) => tab.label || tab.targetId || 'unlabeled').join(', ');
    throw new Error(
      'several tabs already show ' + origin + ' (' + labels + '); ask the user which one to use'
    );
  }

  if (matches.length === 1) {
    const tab = matches[0];
    return tab.label ? space.page(tab.label) : await space.adopt(tab.page);
  }

  // Reuse only an agent-created blank page. A managed page can belong to
  // another application or another Harness host; never navigate it away.
  const blank = tabs.find((tab) => tab.label && tab.url === 'about:blank' && tab.openedBy === 'agent');
  if (blank) return space.page(blank.label);
  return await space.newPage();
}

async function main() {
  const state = JSON.parse(readFileSync(STATE_FILE, 'utf8'));
  const authUrl = readAuthUrl(state);
  if (!authUrl) {
    throw new Error(
      'no acceptable loopback URL in the private startup log; run status, then start again if the host is gone'
    );
  }

  const space = await openSpace();
  const page = await openPage(space, authUrl);

  if (originOf(await page.url()) !== authUrl.origin) {
    await page.goto(authUrl.href);
  }

  const snapshot = await page.snapshot();
  console.log(
    JSON.stringify({
      ok: true,
      spaceId: space.spaceId,
      spaceName: space.name,
      ownership: space.ownership,
      page: page.label,
      origin: authUrl.origin,
      note: 'authenticated URL withheld; read the page, then dispatch one task at a time',
    })
  );
  console.log(snapshot);
}

try {
  await main();
} catch (error) {
  console.error(mask(error && error.message ? error.message : error));
  process.exitCode = 1;
}
"""


def js_literal(value: Any) -> str:
    """Serialise a value into a JavaScript literal that cannot break out."""
    return json.dumps(value, ensure_ascii=True)


def render_connect_module(state_file: str, space_name: str, claim_space: Optional[int]) -> str:
    body = CONNECT_TEMPLATE
    body = body.replace("__STATE_FILE__", js_literal(str(state_file)))
    body = body.replace("__SPACE_NAME__", js_literal(str(space_name)))
    body = body.replace("__CLAIM_SPACE__", "null" if claim_space is None else str(int(claim_space)))
    return body


def connect_script(
    profile: str = DEFAULT_PROFILE,
    *,
    dsh_home: Optional[str] = None,
    state_dir: Optional[str] = None,
    out: Optional[str] = None,
    claim_space: Optional[int] = None,
    space_name: Optional[str] = None,
    force: bool = False,
    deps: Optional[Deps] = None,
) -> Dict[str, Any]:
    """Write a .mjs connect module from a verified local record."""
    deps = deps or default_deps()
    validate_profile(profile)
    if claim_space is not None:
        if isinstance(claim_space, bool) or not isinstance(claim_space, int) or claim_space <= 0:
            raise Refusal("--claim-space takes the positive integer task space id, not a name or path")

    paths = resolve_paths(profile, dsh_home=dsh_home, state_dir=state_dir)
    state = load_state(paths, profile)
    if not state:
        raise Refusal("no helper record at %s; run start first" % paths["state_file"])

    verdict = compare_identity(state.get("identity") or {"pid": state.get("pid")}, deps.identity(state["pid"]))
    if verdict == "mismatch":
        raise Refusal(
            "the recorded host for profile %r is gone or the PID was reused; run status and start again"
            % profile
        )
    if verdict == "unverifiable":
        raise Refusal(
            "process identity for profile %r cannot be verified on this platform; "
            "connect only after checking the host yourself" % profile
        )
    if not state.get("origin"):
        raise Refusal("the record has no origin yet; the host has not finished starting")

    logs = [Path(str(state[key])) for key in ("stdout_log", "stderr_log") if state.get(key)]
    if not any(path.is_file() for path in logs):
        raise Refusal("the private startup log is missing; the authenticated URL cannot be recovered")

    target = Path(out).expanduser() if out else (paths["state_dir"] / ("connect-%s.mjs" % profile))
    if target.exists() and not force:
        raise Refusal("%s already exists; pass --force to replace it" % target)

    body = render_connect_module(
        str(paths["state_file"]), space_name or DEFAULT_SPACE_NAME, claim_space
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass

    return {
        "ok": True,
        "profile": profile,
        "script": str(target),
        "state_file": str(paths["state_file"]),
        "origin": state["origin"],
        "claim_space": claim_space,
        "note": (
            "the module reads the authenticated URL from the private startup log at run time; "
            "run it through scripts/run_ego.ps1 and never paste it into chat or a commit"
        ),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness_operator.py",
        description="Locate, start, inspect and connect to a background DeepSeek Harness host.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="find the installed executable and host CLI script")
    p_discover.add_argument("--executable")
    p_discover.add_argument("--cli-script")
    p_discover.add_argument("--search-dir")

    def add_paths(target: argparse.ArgumentParser) -> None:
        target.add_argument("--profile", default=DEFAULT_PROFILE)
        target.add_argument("--dsh-home", default=None)
        target.add_argument("--state-dir", default=None)

    p_start = sub.add_parser("start", help="start or reuse the background web host")
    add_paths(p_start)
    p_start.add_argument("--port", type=int, default=0)
    p_start.add_argument("--timeout", type=float, default=30.0)
    p_start.add_argument("--executable")
    p_start.add_argument("--cli-script")

    p_status = sub.add_parser("status", help="report process identity and port reachability")
    add_paths(p_status)

    p_connect = sub.add_parser("connect-script", help="generate a private ego-browser connect module")
    add_paths(p_connect)
    p_connect.add_argument("--out")
    p_connect.add_argument("--claim-space", type=int, default=None)
    p_connect.add_argument("--space-name", default=None)
    p_connect.add_argument("--force", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "discover":
            search = Path(args.search_dir).expanduser() if args.search_dir else None
            result = discover(executable=args.executable, cli_script=args.cli_script, search_dir=search)
        elif args.command == "start":
            install = None
            if args.executable or args.cli_script:
                install = discover(executable=args.executable, cli_script=args.cli_script)
            result = start(
                args.profile,
                dsh_home=args.dsh_home,
                state_dir=args.state_dir,
                port=args.port,
                timeout=args.timeout,
                install=install,
            )
        elif args.command == "status":
            result = status(args.profile, dsh_home=args.dsh_home, state_dir=args.state_dir)
        else:
            result = connect_script(
                args.profile,
                dsh_home=args.dsh_home,
                state_dir=args.state_dir,
                out=args.out,
                claim_space=args.claim_space,
                space_name=args.space_name,
                force=args.force,
            )
    except Refusal as exc:
        print(json.dumps({"ok": False, "refused": True, "error": redact(str(exc))}))
        return 2
    except OperatorError as exc:
        print(json.dumps({"ok": False, "error": redact(str(exc))}))
        return 1

    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
