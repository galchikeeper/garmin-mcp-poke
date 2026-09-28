"""The explicit local seeding command must perform at most one offline-faked login."""
import copy
import importlib.util
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import dotenv
import pytest


class FakeStore:
    def __init__(self):
        self.state = {"version": 1, "tokens": "old-test-tokens", "failure_count": 0,
                      "login_cooldown_until": 0, "last_login_at": 1000}
        self.load_calls = 0
        self.save_attempts = 0
        self.saved = []
        self.fail_load = False
        self.fail_save_at = None
        self.error_type = None

    def load(self):
        self.load_calls += 1
        if self.fail_load:
            raise self.error_type("test_read_failed")
        return copy.deepcopy(self.state)

    def save(self, state):
        self.save_attempts += 1
        if self.save_attempts == self.fail_save_at:
            raise self.error_type("test_write_failed")
        self.state = copy.deepcopy(state)
        self.saved.append(copy.deepcopy(state))


class FakeGarth:
    def __init__(self):
        self.configurations = []
        self.login_calls = []
        self.error = None
        self.before_login = None
        self.tokens = "private-new-token-must-not-be-printed"
        self.mfa_code = None

    def configure(self, **kwargs):
        self.configurations.append(kwargs)

    def login(self, email, password, prompt_mfa):
        self.login_calls.append((email, password))
        if self.before_login:
            self.before_login()
        self.mfa_code = prompt_mfa()
        if self.error:
            raise self.error

    def dumps(self):
        return self.tokens


class FakeGarmin:
    def __init__(self):
        self.garth = FakeGarth()
        self.profile_login_calls = 0

    def login(self, *args, **kwargs):
        self.profile_login_calls += 1
        pytest.fail("The seed script must not fetch profiles through Garmin.login")


@pytest.fixture
def seed_script(monkeypatch, tmp_path):
    # Loading this command must not load any real workstation .env values.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    path = Path(__file__).resolve().parents[1] / "scripts" / "generate_tokens.py"
    spec = importlib.util.spec_from_file_location("garmin_seed_command_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("RENDER", "GARMIN_EMAIL", "GARMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for name in ("GIST_ID", "GITHUB_TOKEN", "TOKEN_ENCRYPTION_KEY"):
        monkeypatch.setenv(name, "fake-" + name.lower())

    store = FakeStore()
    store.error_type = module.TokenStoreError
    store_constructions = []
    garmin = FakeGarmin()
    garmin_constructions = []
    prompts = []
    now = 1_800_000_000
    output = tmp_path / ".secrets" / "seed.env"

    def create_store(*args):
        store_constructions.append(args)
        return store

    def create_garmin(*args, **kwargs):
        garmin_constructions.append((args, kwargs))
        return garmin

    def ask_email(prompt):
        prompts.append(prompt)
        return "test-person@example.invalid"

    def ask_password(prompt):
        prompts.append(prompt)
        return " 123456 " if "MFA" in prompt else "private-test-password"

    monkeypatch.setattr(module, "TokenStore", create_store)
    monkeypatch.setattr(module, "Garmin", create_garmin)
    monkeypatch.setattr(module.time, "time", lambda: now)
    monkeypatch.setattr(module.getpass, "getpass", ask_password)
    monkeypatch.setattr("builtins.input", ask_email)

    def run(*, confirmed=True):
        args = ["generate_tokens.py", "--output", str(output)]
        if confirmed:
            args.append("--confirm-cooldown-elapsed")
        monkeypatch.setattr(sys, "argv", args)
        module.main()

    return SimpleNamespace(module=module, store=store, garmin=garmin, run=run, output=output,
                           now=now, prompts=prompts, store_constructions=store_constructions,
                           garmin_constructions=garmin_constructions)


def assert_no_login(command):
    assert command.garmin_constructions == []
    assert command.garmin.garth.login_calls == []
    assert command.garmin.profile_login_calls == 0
    assert not command.output.exists()


def assert_no_secrets(captured, command):
    output = captured.out + captured.err
    assert command.garmin.garth.tokens not in output
    assert "private-test-password" not in output
    assert "test-person@example.invalid" not in output
    assert "private-upstream-error" not in output


def test_missing_explicit_flag_exits_before_storage_or_credentials(seed_script, capsys):
    with pytest.raises(SystemExit) as result:
        seed_script.run(confirmed=False)
    assert result.value.code == 2
    assert seed_script.store_constructions == []
    assert seed_script.prompts == []
    assert_no_login(seed_script)
    assert_no_secrets(capsys.readouterr(), seed_script)


def test_render_environment_refuses_local_seed_command(seed_script, monkeypatch, capsys):
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(SystemExit) as result:
        seed_script.run()
    assert result.value.code == 2
    assert seed_script.store_constructions == []
    assert seed_script.prompts == []
    assert_no_login(seed_script)
    assert_no_secrets(capsys.readouterr(), seed_script)


def test_active_persisted_cooldown_cannot_be_bypassed_with_flag(seed_script, capsys):
    seed_script.store.state["login_cooldown_until"] = seed_script.now + 60
    with pytest.raises(SystemExit) as result:
        seed_script.run()
    assert result.value.code == 1
    assert seed_script.store.load_calls == 1
    assert seed_script.store.save_attempts == 0
    assert seed_script.prompts == []
    assert_no_login(seed_script)
    assert_no_secrets(capsys.readouterr(), seed_script)


def test_store_read_failure_stops_before_credentials_and_login(seed_script, capsys):
    seed_script.store.fail_load = True
    with pytest.raises(SystemExit) as result:
        seed_script.run()
    assert result.value.code == 1
    assert seed_script.store.save_attempts == 0
    assert seed_script.prompts == []
    assert_no_login(seed_script)
    assert_no_secrets(capsys.readouterr(), seed_script)


def test_attempt_guard_must_persist_before_any_login(seed_script, capsys):
    seed_script.store.fail_save_at = 1
    with pytest.raises(SystemExit) as result:
        seed_script.run()
    assert result.value.code == 1
    assert seed_script.store.save_attempts == 1
    assert seed_script.store.saved == []
    assert_no_login(seed_script)
    assert_no_secrets(capsys.readouterr(), seed_script)


def test_success_performs_one_garth_login_without_profile_reads(seed_script, capsys):
    def inspect_guard():
        assert seed_script.store.state["login_cooldown_until"] == seed_script.now + 21_600
        assert seed_script.store.state["tokens"] == "old-test-tokens"

    seed_script.garmin.garth.before_login = inspect_guard
    seed_script.run()
    assert seed_script.garmin_constructions == [((), {})]
    assert seed_script.garmin.garth.configurations == [{"retries": 0, "timeout": 20}]
    assert seed_script.garmin.garth.login_calls == [("test-person@example.invalid", "private-test-password")]
    assert seed_script.garmin.garth.mfa_code == "123456"
    assert seed_script.garmin.profile_login_calls == 0
    assert seed_script.store.save_attempts == 2
    assert seed_script.store.state["tokens"] == seed_script.garmin.garth.tokens
    assert seed_script.store.state["last_login_at"] == seed_script.now
    assert seed_script.store.state["failure_count"] == 0
    assert seed_script.store.state["login_cooldown_until"] == 0
    assert seed_script.output.read_text() == "GARMIN_TOKENS_BASE64=" + seed_script.garmin.garth.tokens + "\n"
    assert stat.S_IMODE(seed_script.output.stat().st_mode) == 0o600
    assert stat.S_IMODE(seed_script.output.parent.stat().st_mode) == 0o700
    assert_no_secrets(capsys.readouterr(), seed_script)


@pytest.mark.parametrize("previous_failures,retry_after,wait", [(0, "30", 21_600),
                                                               (0, "36000", 36_000),
                                                               (3, "30", 86_400)])
def test_429_persists_failure_and_cooldown_without_retry(seed_script, capsys,
                                                      previous_failures, retry_after, wait):
    error = RuntimeError("private-upstream-error")
    error.response = SimpleNamespace(status_code=429, headers={"Retry-After": retry_after})
    seed_script.garmin.garth.error = error
    seed_script.store.state["failure_count"] = previous_failures
    with pytest.raises(SystemExit) as result:
        seed_script.run()
    assert result.value.code == 1
    assert len(seed_script.garmin.garth.login_calls) == 1
    assert seed_script.garmin.profile_login_calls == 0
    assert seed_script.store.state["failure_count"] == previous_failures + 1
    assert seed_script.store.state["login_cooldown_until"] == seed_script.now + wait
    assert seed_script.store.state["last_rate_limit_at"] == seed_script.now
    assert seed_script.store.state["last_error"] == "GARMIN_RATE_LIMITED"
    assert seed_script.store.state["tokens"] == "old-test-tokens"
    assert not seed_script.output.exists()
    assert_no_secrets(capsys.readouterr(), seed_script)


def test_remote_success_save_failure_retains_private_local_seed(seed_script, capsys):
    seed_script.store.fail_save_at = 2
    with pytest.raises(SystemExit) as result:
        seed_script.run()
    assert result.value.code == 1
    assert len(seed_script.garmin.garth.login_calls) == 1
    assert seed_script.garmin.profile_login_calls == 0
    assert seed_script.store.state["tokens"] == "old-test-tokens"
    assert seed_script.store.state["login_cooldown_until"] == seed_script.now + 21_600
    assert seed_script.output.read_text() == "GARMIN_TOKENS_BASE64=" + seed_script.garmin.garth.tokens + "\n"
    assert stat.S_IMODE(seed_script.output.stat().st_mode) == 0o600
    captured = capsys.readouterr()
    assert "Do not login again" in captured.err
    assert_no_secrets(captured, seed_script)


def test_local_backup_failure_still_saves_remote_tokens(seed_script, monkeypatch, capsys):
    def fail_open(*args, **kwargs):
        raise OSError("private-upstream-error")

    monkeypatch.setattr(seed_script.module.os, "open", fail_open)
    with pytest.raises(SystemExit) as result:
        seed_script.run()
    assert result.value.code == 1
    assert len(seed_script.garmin.garth.login_calls) == 1
    assert seed_script.store.state["tokens"] == seed_script.garmin.garth.tokens
    assert seed_script.store.state["login_cooldown_until"] == 0
    assert not seed_script.output.exists()
    captured = capsys.readouterr()
    assert "encrypted Gist is saved" in captured.err
    assert "Do not login again" in captured.err
    assert_no_secrets(captured, seed_script)


def test_both_recovery_saves_failing_never_retries_login(seed_script, monkeypatch, capsys):
    seed_script.store.fail_save_at = 2

    def fail_open(*args, **kwargs):
        raise OSError("private-upstream-error")

    monkeypatch.setattr(seed_script.module.os, "open", fail_open)
    with pytest.raises(SystemExit) as result:
        seed_script.run()
    assert result.value.code == 1
    assert len(seed_script.garmin.garth.login_calls) == 1
    assert seed_script.store.state["login_cooldown_until"] == seed_script.now + 21_600
    assert not seed_script.output.exists()
    captured = capsys.readouterr()
    assert "both recovery saves failed" in captured.err
    assert_no_secrets(captured, seed_script)


def test_existing_output_is_never_overwritten(seed_script, capsys):
    seed_script.output.parent.mkdir()
    seed_script.output.write_text("existing recovery material")
    with pytest.raises(SystemExit) as result:
        seed_script.run()
    assert result.value.code == 2
    assert seed_script.output.read_text() == "existing recovery material"
    assert seed_script.store_constructions == []
    assert seed_script.garmin_constructions == []
    assert seed_script.prompts == []
    assert_no_secrets(capsys.readouterr(), seed_script)
