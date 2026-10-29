"""Unit tests for scripts/harness_operator.py and the generated connect module.

No network, no DeepSeek account and no real Harness process: process identity,
clock, launcher and socket checks are injected. The generated ``.mjs`` module is
executed against a small Node stub that implements the documented ego-browser
API. Scratch files stay in the repo-local ``tmp/`` directory, which is ignored.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "deepseek-harness-operator"
SCRIPT = SKILL_DIR / "scripts" / "harness_operator.py"
DRIVER = REPO_ROOT / "tests" / "stub_driver.mjs"
TMP_ROOT = REPO_ROOT / "tmp"

NODE = shutil.which("node")

TOKEN = "tok_SYNTHETIC_9f3a"
AUTH_LINE = "harness ready at http://127.0.0.1:5555/?token=%s#frag\n" % TOKEN
ORIGIN = "http://127.0.0.1:5555"


def load_operator():
    spec = importlib.util.spec_from_file_location("harness_operator_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


operator = load_operator()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def capture_main(argv):
    buffer = StringIO()
    original = sys.stdout
    sys.stdout = buffer
    try:
        code = operator.main(argv)
    finally:
        sys.stdout = original
    return code, buffer.getvalue()


def identity(creation_time=111, alive=True, verifiable=True, pid=4242):
    return {
        "pid": pid,
        "platform": "nt",
        "creation_time": creation_time,
        "alive": alive,
        "verifiable": verifiable,
    }


def seed_state(state_dir: Path, *, profile="codex-operator", pid=4242, creation_time=111):
    """Write a private record plus logs, mimicking a finished start."""
    state_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = state_dir / "startup-1.stdout.log"
    stdout_log.write_text(AUTH_LINE, encoding="utf-8")
    stderr_log = state_dir / "startup-1.stderr.log"
    stderr_log.write_text("", encoding="utf-8")
    state = {
        "helper": operator.HELPER_NAME,
        "version": operator.STATE_VERSION,
        "profile": profile,
        "pid": pid,
        "identity": identity(creation_time=creation_time, pid=pid),
        "origin": ORIGIN,
        "host": "127.0.0.1",
        "port": 5555,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "state_dir": str(state_dir),
    }
    (state_dir / operator.STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")
    return state


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def build_install(root: Path, *, asar=True, member=True, unpacked=False) -> Path:
    """Create a fake installation tree; optionally with a real asar index."""
    install_root = root / "Programs" / "DeepSeek Harness rc3"
    (install_root / "resources").mkdir(parents=True, exist_ok=True)
    (install_root / operator.EXE_NAME).write_text("stub", encoding="utf-8")
    if not asar:
        cli = install_root / operator.CLI_RELPATH
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("stub", encoding="utf-8")
        return install_root
    archive = install_root / "resources" / "app.asar"
    write_asar(archive, operator.ASAR_MEMBER.parts if member else ("dsh", "other.js"), unpacked=unpacked)
    if unpacked:
        sidecar = install_root / "resources" / "app.asar.unpacked" / operator.ASAR_MEMBER
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("stub", encoding="utf-8")
    return install_root


def write_asar(archive: Path, member_parts, *, unpacked=False, size=32, layout="modern") -> None:
    root = {"files": {}}
    cursor = root
    for part in member_parts[:-1]:
        node = {"files": {}}
        cursor["files"][part] = node
        cursor = node
    entry = {"size": size, "offset": "0"}
    if unpacked:
        entry["unpacked"] = True
    cursor["files"][member_parts[-1]] = entry
    header = json.dumps(root).encode("utf-8")
    if layout == "modern":
        # Installed packages repeat the length fields and start JSON at byte 16.
        prefix = (
            (4).to_bytes(4, "little")
            + (len(header) + 9).to_bytes(4, "little")
            + (len(header) + 5).to_bytes(4, "little")
            + len(header).to_bytes(4, "little")
        )
    else:
        prefix = (4).to_bytes(4, "little") + len(header).to_bytes(4, "little")
    archive.write_bytes(prefix + header + b"0" * size)


class FakeClock:
    def __init__(self, step: float = 0.05, start: float = 1000.0):
        self.value = start
        self.step = step

    def now(self) -> float:
        self.value += self.step
        return self.value

    def sleep(self, _seconds: float) -> None:
        return None


class TempCase(unittest.TestCase):
    """Base case that confines every scratch file to the repo-local tmp dir."""

    def setUp(self):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=TMP_ROOT))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.dsh_home = self.root / "dsh"
        self.state_dir = self.root / "private" / "codex-operator"
        self.clock = FakeClock()
        self.launch_calls = []

    def launcher(self, log_text=AUTH_LINE, pid=4242):
        calls = self.launch_calls

        def _launch(cmd, env, cwd, stdout_path, stderr_path):
            calls.append({"cmd": list(cmd), "env": dict(env), "cwd": str(cwd)})
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(log_text, encoding="utf-8")
            return pid

        return _launch

    def deps(self, *, launcher=None, identity_fn=None, port_probe=None, clock=None, platform="nt"):
        clock = clock or self.clock
        return operator.Deps(
            platform=platform,
            launcher=launcher or self.launcher(),
            identity=identity_fn or (lambda pid: identity(pid=pid)),
            port_probe=port_probe or (lambda host, port, timeout: True),
            sleep=clock.sleep,
            now=clock.now,
            timestamp=lambda: "20261029T000000",
        )

    def start(self, deps=None, **kwargs):
        params = {
            "dsh_home": str(self.dsh_home),
            "state_dir": str(self.state_dir),
            "install": {
                "executable": "C:/harness/DeepSeek Harness.exe",
                "cli_script": "C:/harness/app.asar/dsh/cli.js",
                "install_root": "C:/harness",
                "source": "test",
            },
        }
        params.update(kwargs)
        return operator.start("codex-operator", deps=deps or self.deps(), **params)


# ---------------------------------------------------------------------------
# URL acceptance
# ---------------------------------------------------------------------------


class UrlRejectionTests(unittest.TestCase):
    def test_rejects_non_loopback(self):
        info = operator.parse_origin("serving http://192.0.2.5:8080/?token=%s" % TOKEN)
        self.assertFalse(info["found"])
        self.assertIn("host is not loopback", [item["reason"] for item in info["rejected"]])

    def test_rejects_embedded_credentials_without_echoing_them(self):
        info = operator.parse_origin("http://user:%s@127.0.0.1:1234/" % TOKEN)
        self.assertFalse(info["found"])
        self.assertIn(
            "embedded credentials are not accepted",
            [item["reason"] for item in info["rejected"]],
        )
        self.assertNotIn(TOKEN, json.dumps(info))

    def test_rejects_https(self):
        info = operator.parse_origin("https://127.0.0.1:1234/")
        self.assertFalse(info["found"])

    def test_rejects_missing_port(self):
        info = operator.parse_origin("http://127.0.0.1/")
        self.assertFalse(info["found"])

    def test_rejects_out_of_range_port(self):
        self.assertFalse(operator.parse_origin("http://127.0.0.1:99999/")["found"])

    def test_rejects_malformed_url(self):
        self.assertFalse(operator.parse_origin("http://127.0.0.1:not-a-port/")["found"])

    def test_accepts_localhost_and_ipv6_loopback(self):
        self.assertEqual(operator.parse_origin("http://localhost:5000/")["origin"], "http://localhost:5000")
        self.assertEqual(operator.parse_origin("http://[::1]:5000/")["origin"], "http://[::1]:5000")


# ---------------------------------------------------------------------------
# console/log parsing
# ---------------------------------------------------------------------------


class LogParsingTests(unittest.TestCase):
    def test_ansi_codes_are_stripped(self):
        raw = "\x1b[32mready\x1b[0m \x1b[1;34mhttp://127.0.0.1:56364/\x1b[0m\n"
        self.assertNotIn("\x1b", operator.strip_ansi(raw))
        self.assertTrue(operator.parse_origin(raw)["found"])

    def test_fragment_url_is_kept_for_internal_use_only(self):
        info = operator.parse_origin(AUTH_LINE)
        self.assertTrue(info["found"])
        self.assertEqual(info["origin"], ORIGIN)
        self.assertTrue(info["url"].endswith("?token=%s#frag" % TOKEN))

    def test_last_acceptable_url_wins(self):
        raw = "http://127.0.0.1:1111/ then http://127.0.0.1:2222/#x"
        self.assertEqual(operator.parse_origin(raw)["port"], 2222)


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


class RedactionTests(unittest.TestCase):
    def test_redact_hides_query_and_fragment(self):
        out = operator.redact("open http://127.0.0.1:56364/?token=%s#frag failed" % TOKEN)
        self.assertNotIn(TOKEN, out)
        self.assertIn("http://127.0.0.1:56364", out)

    def test_redact_hides_userinfo(self):
        self.assertNotIn(TOKEN, operator.redact("bad http://u:%s@127.0.0.1:1/" % TOKEN))

    def test_redact_keeps_plain_origin(self):
        self.assertEqual(operator.redact(ORIGIN + "/"), ORIGIN + "/")

    def test_redact_hides_token_parameters(self):
        self.assertNotIn("abc123", operator.redact("callback?access_token=abc123&x=1"))

    def test_cli_error_message_is_redacted(self):
        code, out = capture_main(
            ["discover", "--executable", "http://127.0.0.1:1/?token=%s/missing.exe" % TOKEN]
        )
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out)


# ---------------------------------------------------------------------------
# CLI entry point (regression for the stdlib "operator" shadowing bug)
# ---------------------------------------------------------------------------


class CliEntryPointTests(TempCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120
        )

    def test_help_runs_as_a_subprocess(self):
        proc = self.run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("discover", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_discover_runs_as_a_subprocess(self):
        empty = self.root / "empty-programs"
        empty.mkdir(parents=True)
        proc = self.run_cli("discover", "--search-dir", str(empty))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        payload = json.loads(proc.stdout.strip())
        self.assertFalse(payload["ok"])
        self.assertIn("no DeepSeek Harness installation", payload["error"])

    def test_discover_finds_a_packaged_install(self):
        install_root = build_install(self.root, asar=True)
        proc = self.run_cli("discover", "--search-dir", str(self.root / "Programs"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["cli_kind"], "asar")
        self.assertTrue(payload["executable"].startswith(str(install_root)))

    def test_old_shadowing_module_is_gone(self):
        self.assertFalse((SKILL_DIR / "scripts" / "operator.py").exists())


# ---------------------------------------------------------------------------
# profiles and paths
# ---------------------------------------------------------------------------


class PathAndProfileTests(TempCase):
    def test_invalid_profile_names_are_refused(self):
        for bad in ["", "codex operator", "-leading", "has.dot", "a" * 49, "codex/operator"]:
            with self.assertRaises(operator.Refusal, msg=bad):
                operator.validate_profile(bad)

    def test_valid_profile_names_are_accepted(self):
        for good in ["codex-operator", "a", "A1_b-2", "x" * 48]:
            self.assertEqual(operator.validate_profile(good), good)

    def test_main_exits_two_on_refusal(self):
        code, out = capture_main(["status", "--profile", "bad name"])
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(out)["refused"])

    def test_state_dir_equal_to_profile_dir_is_refused(self):
        with self.assertRaises(operator.Refusal):
            operator.resolve_paths(
                "codex-operator",
                dsh_home=str(self.dsh_home),
                state_dir=str(self.dsh_home / "profiles" / "codex-operator"),
            )

    def test_state_dir_inside_profile_dir_is_refused(self):
        with self.assertRaises(operator.Refusal):
            operator.resolve_paths(
                "codex-operator",
                dsh_home=str(self.dsh_home),
                state_dir=str(self.dsh_home / "profiles" / "codex-operator" / "private"),
            )

    def test_default_state_dir_is_outside_the_profile(self):
        paths = operator.resolve_paths("codex-operator", dsh_home=str(self.dsh_home))
        self.assertFalse(str(paths["state_dir"]).startswith(str(paths["profile_dir"])))


# ---------------------------------------------------------------------------
# state validation
# ---------------------------------------------------------------------------


class StateValidationTests(TempCase):
    def paths(self):
        return operator.resolve_paths(
            "codex-operator", dsh_home=str(self.dsh_home), state_dir=str(self.state_dir)
        )

    def test_rejects_foreign_profile(self):
        state = seed_state(self.state_dir, profile="other-profile")
        with self.assertRaises(operator.Refusal):
            operator.load_state(self.paths(), "codex-operator")
        self.assertEqual(state["profile"], "other-profile")

    def test_rejects_wrong_version(self):
        state = seed_state(self.state_dir)
        state["version"] = 1
        write_json(self.state_dir / operator.STATE_FILENAME, state)
        with self.assertRaises(operator.Refusal):
            operator.load_state(self.paths(), "codex-operator")

    def test_rejects_state_with_authenticated_origin(self):
        state = seed_state(self.state_dir)
        state["origin"] = "http://127.0.0.1:5555/?token=%s" % TOKEN
        write_json(self.state_dir / operator.STATE_FILENAME, state)
        with self.assertRaises(operator.Refusal) as caught:
            operator.load_state(self.paths(), "codex-operator")
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_rejects_remote_origin(self):
        state = seed_state(self.state_dir)
        state["origin"] = "http://192.0.2.5:5555"
        write_json(self.state_dir / operator.STATE_FILENAME, state)
        with self.assertRaises(operator.Refusal):
            operator.load_state(self.paths(), "codex-operator")

    def test_rejects_userinfo_origin(self):
        state = seed_state(self.state_dir)
        state["origin"] = "http://user:%s@127.0.0.1:5555" % TOKEN
        write_json(self.state_dir / operator.STATE_FILENAME, state)
        with self.assertRaises(operator.Refusal):
            operator.load_state(self.paths(), "codex-operator")

    def test_rejects_port_mismatch(self):
        state = seed_state(self.state_dir)
        state["port"] = 6666
        write_json(self.state_dir / operator.STATE_FILENAME, state)
        with self.assertRaises(operator.Refusal):
            operator.load_state(self.paths(), "codex-operator")

    def test_rejects_bad_pid_type(self):
        state = seed_state(self.state_dir)
        state["pid"] = "4242"
        write_json(self.state_dir / operator.STATE_FILENAME, state)
        with self.assertRaises(operator.Refusal):
            operator.load_state(self.paths(), "codex-operator")

    def test_accepts_and_normalises_a_clean_origin(self):
        state = seed_state(self.state_dir)
        state["origin"] = "http://LOCALHOST:5555"
        write_json(self.state_dir / operator.STATE_FILENAME, state)
        loaded = operator.load_state(self.paths(), "codex-operator")
        self.assertEqual(loaded["origin"], "http://localhost:5555")


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


class IdentityTests(unittest.TestCase):
    def test_match(self):
        self.assertEqual(operator.compare_identity(identity(111), identity(111)), "match")

    def test_mismatch_on_newer_creation_time(self):
        self.assertEqual(operator.compare_identity(identity(111), identity(222)), "mismatch")

    def test_mismatch_on_recycled_pid(self):
        self.assertEqual(
            operator.compare_identity(identity(111, pid=4242), identity(111, pid=5252)), "mismatch"
        )

    def test_mismatch_when_process_exited(self):
        self.assertEqual(
            operator.compare_identity(identity(111), identity(111, alive=False)), "mismatch"
        )

    def test_unverifiable(self):
        self.assertEqual(
            operator.compare_identity(identity(111), identity(111, verifiable=False)), "unverifiable"
        )

    def test_unknown_without_record(self):
        self.assertEqual(operator.compare_identity(None, identity(111)), "unknown")

    def test_non_windows_identity_is_not_claimed(self):
        record = operator.read_process_identity(1234, platform="posix")
        self.assertFalse(record["verifiable"])
        self.assertIsNone(record["creation_time"])


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


class StartTests(TempCase):
    def test_refuses_profile_dir_without_config(self):
        profile_dir = self.dsh_home / "profiles" / "codex-operator"
        profile_dir.mkdir(parents=True)
        (profile_dir / "notes.txt").write_text("hand written", encoding="utf-8")
        with self.assertRaises(operator.Refusal) as caught:
            self.start()
        self.assertIn("no cordis.yml", str(caught.exception))
        self.assertEqual(self.launch_calls, [])

    def test_arguments_and_child_only_env(self):
        before = os.environ.get("ELECTRON_RUN_AS_NODE")
        result = self.start()
        self.assertTrue(result["ok"])
        self.assertTrue(result["ready"])
        call = self.launch_calls[0]
        self.assertEqual(call["env"].get("ELECTRON_RUN_AS_NODE"), "1")
        self.assertEqual(call["env"].get("DSH_HOME"), str(self.dsh_home.resolve()))
        self.assertEqual(os.environ.get("ELECTRON_RUN_AS_NODE"), before)
        self.assertIn("--no-open", call["cmd"])
        self.assertEqual(call["cmd"][call["cmd"].index("--from-default-profile") + 1], "web")
        self.assertEqual(call["cmd"][call["cmd"].index("--host") + 1], "127.0.0.1")
        self.assertEqual(call["cmd"][call["cmd"].index("--port") + 1], "0")
        self.assertEqual(result["origin"], ORIGIN)

    def test_reuses_existing_config_without_initialising(self):
        profile_dir = self.dsh_home / "profiles" / "codex-operator"
        profile_dir.mkdir(parents=True)
        (profile_dir / "cordis.yml").write_text("profile: mine\n", encoding="utf-8")
        result = self.start()
        self.assertFalse(result["initialized_profile"])
        self.assertNotIn("--from-default-profile", self.launch_calls[0]["cmd"])
        self.assertEqual((profile_dir / "cordis.yml").read_text(encoding="utf-8"), "profile: mine\n")

    def test_output_never_contains_the_authenticated_url(self):
        dumped = json.dumps(self.start())
        self.assertNotIn(TOKEN, dumped)
        self.assertNotIn("?token", dumped)

    def test_not_ready_when_the_port_never_answers(self):
        deps = self.deps(port_probe=lambda host, port, timeout: False, clock=FakeClock(step=0.5))
        result = self.start(deps=deps, timeout=1.0)
        self.assertFalse(result["ok"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["origin"], ORIGIN)
        self.assertEqual(len(self.launch_calls), 1)

    def test_timeout_keeps_the_record_and_a_later_status_finds_the_url(self):
        deps = self.deps(launcher=self.launcher(log_text="no url yet\n"), clock=FakeClock(step=0.5))
        result = self.start(deps=deps, timeout=1.0)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["origin"])

        # The host prints its URL a little late; the same record is refreshed.
        stdout_log = Path(result["stdout_log"])
        stdout_log.write_text(AUTH_LINE, encoding="utf-8")

        report = operator.status(
            "codex-operator",
            dsh_home=str(self.dsh_home),
            state_dir=str(self.state_dir),
            deps=operator.Deps(
                identity=lambda pid: identity(pid=pid),
                port_probe=lambda host, port, timeout: True,
            ),
        )
        self.assertTrue(report["healthy"])
        self.assertEqual(report["origin"], ORIGIN)
        self.assertEqual(report["port"], 5555)

    def test_reuses_a_verified_healthy_record(self):
        seed_state(self.state_dir)
        result = self.start()
        self.assertEqual(result["action"], "reused")
        self.assertTrue(result["ok"])
        self.assertEqual(self.launch_calls, [])

    def test_does_not_duplicate_when_the_process_is_alive_but_the_port_is_closed(self):
        seed_state(self.state_dir)
        deps = self.deps(port_probe=lambda host, port, timeout: False, clock=FakeClock(step=0.5))
        result = self.start(deps=deps, timeout=1.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "reused")
        self.assertEqual(self.launch_calls, [])

    def test_refuses_when_the_existing_process_cannot_be_verified(self):
        seed_state(self.state_dir)
        deps = self.deps(identity_fn=lambda pid: identity(verifiable=False))
        with self.assertRaises(operator.Refusal):
            self.start(deps=deps)
        self.assertEqual(self.launch_calls, [])

    def test_recycled_pid_counts_as_stale_and_starts_fresh(self):
        seed_state(self.state_dir, creation_time=111)
        deps = self.deps(identity_fn=lambda pid: identity(creation_time=222, pid=pid))
        result = self.start(deps=deps)
        self.assertEqual(result["action"], "started")
        self.assertTrue(result["previous_record_stale"])
        self.assertEqual(len(self.launch_calls), 1)

    def test_refuses_on_non_windows_platform(self):
        with self.assertRaises(operator.OperatorError):
            self.start(deps=self.deps(platform="posix"))
        self.assertEqual(self.launch_calls, [])

    def test_refuses_when_another_start_holds_the_lock(self):
        self.state_dir.mkdir(parents=True)
        (self.state_dir / operator.LOCK_FILENAME).write_text('{"pid": 9999}', encoding="utf-8")
        with self.assertRaises(operator.Refusal) as caught:
            self.start()
        self.assertIn("another start", str(caught.exception))
        self.assertEqual(self.launch_calls, [])
        self.assertTrue((self.state_dir / operator.LOCK_FILENAME).exists())

    def test_lock_is_released_after_a_successful_start(self):
        self.start()
        self.assertFalse((self.state_dir / operator.LOCK_FILENAME).exists())

    def test_state_lives_outside_the_profile_directory(self):
        self.start()
        self.assertTrue((self.state_dir / operator.STATE_FILENAME).is_file())
        self.assertFalse(str(self.state_dir).startswith(str(self.dsh_home / "profiles")))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class StatusTests(TempCase):
    def status(self, deps):
        return operator.status(
            "codex-operator", dsh_home=str(self.dsh_home), state_dir=str(self.state_dir), deps=deps
        )

    def test_absent_record_is_reported_honestly(self):
        result = self.status(operator.Deps())
        self.assertFalse(result["state_present"])
        self.assertFalse(result["healthy"])

    def test_healthy_record(self):
        seed_state(self.state_dir)
        result = self.status(
            operator.Deps(
                identity=lambda pid: identity(pid=pid),
                port_probe=lambda host, port, timeout: True,
            )
        )
        self.assertTrue(result["healthy"])
        self.assertEqual(result["identity_verdict"], "match")
        self.assertEqual(result["origin"], ORIGIN)

    def test_pid_reuse_is_unhealthy(self):
        seed_state(self.state_dir, creation_time=111)
        result = self.status(
            operator.Deps(
                identity=lambda pid: identity(creation_time=999, pid=pid),
                port_probe=lambda host, port, timeout: True,
            )
        )
        self.assertFalse(result["healthy"])
        self.assertEqual(result["identity_verdict"], "mismatch")

    def test_open_port_alone_is_not_health(self):
        seed_state(self.state_dir)
        result = self.status(
            operator.Deps(
                identity=lambda pid: identity(verifiable=False),
                port_probe=lambda host, port, timeout: True,
            )
        )
        self.assertFalse(result["healthy"])
        self.assertIn("not proof", result["note"])

    def test_closed_port_is_reported(self):
        seed_state(self.state_dir)
        result = self.status(
            operator.Deps(
                identity=lambda pid: identity(pid=pid),
                port_probe=lambda host, port, timeout: False,
            )
        )
        self.assertEqual(result["socket"], "closed")
        self.assertFalse(result["healthy"])


# ---------------------------------------------------------------------------
# connect-script
# ---------------------------------------------------------------------------


class ConnectScriptTests(TempCase):
    def matching_deps(self, creation_time=111, verifiable=True):
        return operator.Deps(
            identity=lambda pid: identity(creation_time=creation_time, verifiable=verifiable, pid=pid)
        )

    def generate(self, **kwargs):
        return operator.connect_script(
            "codex-operator",
            dsh_home=str(self.dsh_home),
            state_dir=str(self.state_dir),
            deps=self.matching_deps(),
            **kwargs,
        )

    def run_stub(self, mode, script_path):
        if not NODE:
            self.skipTest("node is not on PATH")
        proc = subprocess.run(
            [NODE, str(DRIVER), Path(script_path).as_uri(), mode],
            capture_output=True,
            text=True,
            timeout=120,
        )
        calls = []
        for line in proc.stderr.splitlines():
            if line.startswith("__STUB_LOG__"):
                calls = json.loads(line[len("__STUB_LOG__") :])
        return proc, calls

    @staticmethod
    def names(calls):
        return [call[0] for call in calls]

    def test_generated_module_is_private_utf8(self):
        seed_state(self.state_dir)
        result = self.generate()
        path = Path(result["script"])
        self.assertTrue(path.is_file())
        self.assertTrue(str(path).startswith(str(self.state_dir)))
        text = path.read_text(encoding="utf-8")
        for expected in ("listTaskSpaces", "claimTaskSpace", "space.tabs()", "space.adopt(", "page.url()"):
            self.assertIn(expected, text)

    def test_module_has_no_credentials_and_an_absolute_state_path(self):
        seed_state(self.state_dir)
        text = Path(self.generate()["script"]).read_text(encoding="utf-8")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("?token", text)
        self.assertNotIn(ORIGIN, text)
        match = re.search(r"^const STATE_FILE = (.*);$", text, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match.group(1)), str(self.state_dir / operator.STATE_FILENAME))

    def test_renderer_escapes_quotes_and_newlines(self):
        nasty_path = 'C:/priv/we"ird\nname/operator-state.json'
        nasty_name = 'space "; throw new Error("injected");\n'
        text = operator.render_connect_module(nasty_path, nasty_name, None)
        path_line = re.search(r"^const STATE_FILE = (.*);$", text, re.MULTILINE)
        name_line = re.search(r"^const SPACE_NAME = (.*);$", text, re.MULTILINE)
        self.assertEqual(json.loads(path_line.group(1)), nasty_path)
        self.assertEqual(json.loads(name_line.group(1)), nasty_name)
        self.assertNotIn("\ninjected", text)

    def test_claim_space_defaults_to_off_and_is_an_integer_when_set(self):
        seed_state(self.state_dir)
        plain = Path(self.generate()["script"]).read_text(encoding="utf-8")
        self.assertIn("const CLAIM_SPACE_ID = null;", plain)
        claimed = Path(self.generate(force=True, claim_space=7)["script"]).read_text(encoding="utf-8")
        self.assertIn("const CLAIM_SPACE_ID = 7;", claimed)

    def test_claim_space_rejects_non_positive_and_non_integer_values(self):
        seed_state(self.state_dir)
        for bad in (0, -3, "7", "7 ", True):
            with self.assertRaises(operator.Refusal, msg=repr(bad)):
                self.generate(claim_space=bad)

    def test_refuses_without_state(self):
        with self.assertRaises(operator.Refusal):
            self.generate()

    def test_refuses_when_record_is_stale(self):
        seed_state(self.state_dir, creation_time=111)
        with self.assertRaises(operator.Refusal):
            operator.connect_script(
                "codex-operator",
                dsh_home=str(self.dsh_home),
                state_dir=str(self.state_dir),
                deps=self.matching_deps(creation_time=222),
            )

    def test_refuses_to_overwrite_without_force(self):
        seed_state(self.state_dir)
        self.generate()
        with self.assertRaises(operator.Refusal):
            self.generate()

    # -- the generated module executed against the ego stub ------------------

    def test_stub_first_connect_navigates_the_existing_blank_page(self):
        seed_state(self.state_dir)
        proc, calls = self.run_stub("create", self.generate()["script"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual(payload["spaceId"], 3)
        self.assertEqual(payload["page"], "p1")
        self.assertIn("SNAPSHOT-p1", proc.stdout)
        names = self.names(calls)
        self.assertNotIn("newPage", names)
        self.assertNotIn("adopt", names)
        self.assertIn("goto", names)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)

    def test_stub_reuses_the_current_space_and_tab(self):
        seed_state(self.state_dir)
        proc, calls = self.run_stub("reuse", self.generate()["script"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual(payload["page"], "p2")
        names = self.names(calls)
        self.assertNotIn("adopt", names)
        self.assertNotIn("goto", names)
        self.assertNotIn("newPage", names)

    def test_stub_preserves_a_managed_page_for_another_host(self):
        seed_state(self.state_dir)
        proc, calls = self.run_stub("managed-unrelated", self.generate()["script"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("newPage", self.names(calls))
        self.assertNotIn(["goto", "p1", ORIGIN], calls)
        payload = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual(payload["page"], "p2")

    def test_stub_claims_and_adopts_after_explicit_claim(self):
        seed_state(self.state_dir)
        proc, calls = self.run_stub("claim", self.generate(claim_space=7)["script"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = self.names(calls)
        self.assertIn("listTaskSpaces", names)
        self.assertIn("claimTaskSpace", names)
        self.assertIn("adopt", names)
        claim_args = [call[1] for call in calls if call[0] == "claimTaskSpace"]
        self.assertEqual(claim_args, [7])
        payload = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual(payload["page"], "p9")

    def test_stub_does_not_take_over_unrelated_tabs(self):
        seed_state(self.state_dir)
        proc, calls = self.run_stub("unrelated", self.generate()["script"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = self.names(calls)
        self.assertNotIn("adopt", names)
        goto = [call for call in calls if call[0] == "goto"]
        self.assertEqual(len(goto), 1)
        self.assertEqual(goto[0][2], ORIGIN)
        self.assertEqual(goto[0][1], "p1")

    def test_stub_refuses_ambiguous_tabs(self):
        seed_state(self.state_dir)
        proc, calls = self.run_stub("ambiguous", self.generate()["script"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("several tabs", proc.stderr)
        self.assertNotIn("adopt", self.names(calls))
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)

    def test_stub_refuses_a_name_owned_by_the_user(self):
        seed_state(self.state_dir)
        proc, calls = self.run_stub("name-in-use", self.generate()["script"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--claim-space", proc.stderr)
        self.assertNotIn("claimTaskSpace", self.names(calls))

    def test_module_runs_from_a_custom_out_directory(self):
        seed_state(self.state_dir)
        custom = self.root / "elsewhere" / "connect.mjs"
        self.generate(out=str(custom))
        proc, _ = self.run_stub("create", custom)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SNAPSHOT-p1", proc.stdout)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


class DiscoveryTests(TempCase):
    def test_finds_newest_install(self):
        self.build("DeepSeek Harness rc2")
        self.build("DeepSeek Harness rc10")
        result = operator.discover(search_dir=self.root / "Programs")
        self.assertIn("rc10", result["executable"])
        self.assertEqual(len(result["candidates"]), 2)

    def build(self, name: str) -> Path:
        root = self.root / "Programs" / name
        (root / "resources").mkdir(parents=True, exist_ok=True)
        (root / operator.EXE_NAME).write_text("stub", encoding="utf-8")
        write_asar(root / "resources" / "app.asar", operator.ASAR_MEMBER.parts)
        return root

    def test_errors_without_install(self):
        (self.root / "Programs").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(operator.OperatorError):
            operator.discover(search_dir=self.root / "Programs")

    def test_asar_member_is_accepted(self):
        install_root = build_install(self.root, asar=True)
        result = operator.discover(search_dir=self.root / "Programs")
        self.assertEqual(result["cli_kind"], "asar")
        self.assertTrue(result["cli_script"].endswith("cli.js"))
        self.assertTrue(result["cli_script"].startswith(str(install_root)))

    def test_asar_without_the_member_is_rejected(self):
        build_install(self.root, asar=True, member=False)
        with self.assertRaises(operator.OperatorError) as caught:
            operator.discover(search_dir=self.root / "Programs")
        self.assertIn("host CLI script missing", str(caught.exception))

    def test_asar_unpacked_member_uses_the_sidecar_file(self):
        build_install(self.root, asar=True, unpacked=True)
        result = operator.discover(search_dir=self.root / "Programs")
        self.assertEqual(result["cli_kind"], "unpacked-sidecar")
        self.assertIn("app.asar.unpacked", result["cli_script"])

    def test_truncated_asar_is_reported(self):
        archive = self.root / "broken.asar"
        archive.write_bytes(b"")
        with self.assertRaises(operator.OperatorError):
            operator.read_asar_index(archive)

    def test_reads_the_legacy_eight_byte_header(self):
        archive = self.root / "legacy.asar"
        write_asar(archive, operator.ASAR_MEMBER.parts, layout="legacy")
        index = operator.read_asar_index(archive)
        self.assertIsNotNone(operator.asar_lookup(index, operator.ASAR_MEMBER.parts))

    def test_unpacked_install_uses_the_on_disk_file(self):
        build_install(self.root, asar=False)
        result = operator.discover(search_dir=self.root / "Programs")
        self.assertEqual(result["cli_kind"], "on-disk")

    def test_explicit_cli_script_derives_executable(self):
        install_root = build_install(self.root, asar=True)
        result = operator.discover(
            cli_script=str(install_root / operator.CLI_RELPATH),
            search_dir=self.root / "Programs",
        )
        self.assertEqual(Path(result["executable"]), install_root / operator.EXE_NAME)

    def test_explicit_executable_must_exist(self):
        with self.assertRaises(operator.OperatorError):
            operator.discover(
                executable=str(self.root / "nope.exe"), search_dir=self.root / "Programs"
            )

    def test_discovery_writes_nothing(self):
        build_install(self.root, asar=True)
        before = sorted(str(p) for p in self.root.rglob("*"))
        operator.discover(search_dir=self.root / "Programs")
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
