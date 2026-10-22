"""Static checks of the deployment files: workflows, compose, Dockerfile, scripts, units.

No server and no Docker needed. These tests pin the properties that are easy to break in a
refactor and costly to discover on a real server: pinned actions, secrets never inlined into
shell text, a stop grace period longer than the scheduler's drain, LF line endings, and so on.
"""

import configparser
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.scheduler import STOP_TIMEOUT_SECONDS
from tests.ops.shell import BASH, ROOT, posix, requires_bash, run_script

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"
BASH_SCRIPTS = [
    ROOT / "scripts" / "ops" / "backup.sh",
    ROOT / "scripts" / "ops" / "restore.sh",
    ROOT / "deploy" / "bootstrap.sh",
    ROOT / "deploy" / "remote-deploy.sh",
]
SH_SCRIPTS = [ROOT / "deploy" / "entrypoint.sh"]
SHELL_SCRIPTS = BASH_SCRIPTS + SH_SCRIPTS
SHA_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """YAML 1.1 reads the key ``on`` as the boolean True."""
    return workflow.get("on") or workflow.get(True)


def steps_of(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


# --------------------------------------------------------------------------- workflows


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    for step in steps_of(load(path)):
        if "uses" in step:
            assert SHA_PINNED.match(step["uses"]), f"{path.name}: {step['uses']} is not SHA-pinned"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflows_declare_least_privilege_and_timeouts(path: Path) -> None:
    workflow = load(path)
    assert "permissions" in workflow
    for name, job in workflow["jobs"].items():
        assert "timeout-minutes" in job, f"{path.name}: job {name} has no timeout"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_secrets_and_untrusted_input_never_appear_in_shell_text(path: Path) -> None:
    for step in steps_of(load(path)):
        script = step.get("run", "")
        assert "secrets." not in script, f"{path.name}: secret inlined in a run step"
        assert "github.event." not in script
        assert "inputs." not in script
        assert "github.head_ref" not in script


def test_deploy_triggers_on_version_tags_and_manual_dispatch_only() -> None:
    on = triggers(load(DEPLOY))
    assert on["push"] == {"tags": ["v*"]}
    assert "workflow_dispatch" in on
    assert set(on) == {"push", "workflow_dispatch"}


def test_deploy_is_a_noop_without_secrets() -> None:
    workflow = load(DEPLOY)
    jobs = workflow["jobs"]
    gate = jobs["gate"]
    assert "if" not in gate, "the gate must always run so the workflow ends green"
    gate_script = "\n".join(step.get("run", "") for step in gate["steps"])
    for secret in ("SSH_HOST", "SSH_USER", "SSH_KEY"):
        assert f"secrets.{secret}" in yaml.safe_dump(gate)
        assert f'-n "${secret}"' in gate_script
    assert "exit 1" not in gate_script
    for name in ("image", "deploy"):
        assert "needs.gate.outputs.enabled == 'true'" in jobs[name]["if"]
        assert "gate" in jobs[name]["needs"]


def test_deploy_never_runs_twice_at_once_and_uses_a_protected_environment() -> None:
    workflow = load(DEPLOY)
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["jobs"]["deploy"]["environment"] == "production"


def test_deploy_pins_host_keys_and_does_not_leak_the_key() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert "StrictHostKeyChecking=yes" in text
    assert "StrictHostKeyChecking=no" not in text
    assert "set -x" not in text
    assert 'echo "$SSH_KEY"' not in text
    assert "rm -f ~/.ssh/deploy_key" in text


def test_deploy_registry_token_is_passed_on_stdin() -> None:
    text = DEPLOY.read_text(encoding="utf-8")
    assert "--password-stdin" in text
    assert "GHCR_TOKEN=" not in text.replace("GHCR_TOKEN: ${{", "")


def test_deploy_job_can_pull_but_not_push() -> None:
    jobs = load(DEPLOY)["jobs"]
    assert jobs["image"]["permissions"]["packages"] == "write"
    assert jobs["deploy"]["permissions"]["packages"] == "read"


# --------------------------------------------------------------------------- compose / image


def service() -> dict[str, Any]:
    return load(ROOT / "docker-compose.yml")["services"]["bot"]


def test_compose_restart_policy_survives_reboots() -> None:
    assert service()["restart"] == "${RESTART_POLICY:-unless-stopped}"


def test_compose_stop_grace_period_exceeds_the_scheduler_drain() -> None:
    grace = service()["stop_grace_period"]
    seconds = int(grace.removesuffix("s"))
    assert seconds >= STOP_TIMEOUT_SECONDS + 10
    assert seconds >= 70


def test_compose_healthcheck_runs_the_probe() -> None:
    check = service()["healthcheck"]
    assert check["test"] == ["CMD", "python", "-m", "app.ops.healthcheck"]
    assert int(check["start_period"].removesuffix("s")) >= 60


def test_compose_limits_logs_memory_and_privileges() -> None:
    bot = service()
    assert bot["logging"]["options"]["max-size"]
    assert bot["logging"]["options"]["max-file"]
    assert bot["mem_limit"]
    assert bot["read_only"] is True
    assert bot["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in bot["security_opt"]
    assert bot["init"] is True


def test_compose_persists_data_in_a_named_volume_and_exposes_the_services_switch() -> None:
    compose = load(ROOT / "docker-compose.yml")
    assert "bot-data" in compose["volumes"]
    assert "bot-data:/data" in service()["volumes"]
    assert service()["environment"]["APP_SERVICES"] == "${APP_SERVICES:-fake}"
    assert service()["image"] == "${AI_TAHLILCHI_IMAGE:-ai-tahlilchi:local}"
    assert "ports" not in service(), "the bot polls Telegram; nothing should be published"


def test_dockerfile_ships_migrations_entrypoint_and_healthcheck() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY alembic.ini /app/alembic.ini" in text
    assert "COPY alembic /app/alembic" in text
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in text
    assert "HEALTHCHECK" in text
    assert "USER app:app" in text
    context = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for needed in ("!alembic/", "!alembic.ini", "!deploy/entrypoint.sh"):
        assert needed in context


def test_ci_smoke_test_matches_the_new_entrypoint() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--tmpfs /data:uid=10001,gid=10001" in text
    assert "app.ops.healthcheck" in text


def test_env_example_documents_the_ops_settings() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("APP_SERVICES=", "HEALTHCHECKS_URL="):
        assert name in text


# --------------------------------------------------------------------------- scripts


@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_scripts_use_unix_line_endings_and_a_shebang(path: Path) -> None:
    data = path.read_bytes()
    assert b"\r" not in data, f"{path.name} has CRLF line endings; it would not run on Linux"
    assert data.startswith(b"#!")


@pytest.mark.parametrize("path", BASH_SCRIPTS, ids=lambda p: p.name)
def test_bash_scripts_fail_fast(path: Path) -> None:
    assert "set -euo pipefail" in path.read_text(encoding="utf-8")


def test_entrypoint_is_posix_sh_and_fails_fast() -> None:
    text = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "set -eu" in text


@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: p.name)
@requires_bash
def test_scripts_have_valid_syntax(path: Path) -> None:
    assert BASH is not None
    result = subprocess.run(  # noqa: S603
        [BASH, "-n", posix(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: p.name)
@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_scripts_are_shellcheck_clean(path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        ["shellcheck", "-x", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "path", [ROOT / "scripts/ops/backup.sh", ROOT / "scripts/ops/restore.sh"], ids=lambda p: p.name
)
@requires_bash
def test_ops_scripts_have_help(path: Path) -> None:
    result = run_script(path, "--help")
    assert result.returncode == 0
    assert path.name in result.stdout or "Usage" in result.stdout or "#" in result.stdout


@requires_bash
def test_bootstrap_dry_run_needs_no_root_and_changes_nothing(tmp_path: Path) -> None:
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA test\n")
    result = run_script(
        ROOT / "deploy" / "bootstrap.sh",
        "--dry-run",
        "--ssh-key-file",
        posix(key),
        "--app-dir",
        "/opt/ai-tahlilchi-dryrun",
        "--install-service",
    )

    assert result.returncode == 0, result.stderr
    output = result.stdout
    for fragment in (
        "ufw limit 22/tcp",
        "ufw --force enable",
        "fail2ban",
        "unattended-upgrades",
        "docker-ce",
        "PasswordAuthentication no",
        "write /etc/ssh/sshd_config.d/99-ai-tahlilchi.conf",
        "chmod 600",
        "ai-tahlilchi-backup.timer",
        "enable ai-tahlilchi.service",
    ):
        assert fragment in output, fragment
    assert "create empty /opt/ai-tahlilchi-dryrun/.env" in output
    assert not Path("/opt/ai-tahlilchi-dryrun").exists()


@requires_bash
@pytest.mark.parametrize(
    "args",
    [
        [],  # the key is mandatory: without it password login cannot be disabled safely
        ["--bogus"],
        ["--ssh-key-file", "k", "--deploy-user", "Bad User"],
        ["--ssh-key-file", "k", "--ssh-port", "x"],
        ["--ssh-key-file", "k", "--app-dir", "relative"],
    ],
)
def test_bootstrap_rejects_unsafe_arguments(args: list[str]) -> None:
    result = run_script(ROOT / "deploy" / "bootstrap.sh", "--dry-run", *args)
    assert result.returncode != 0


# --------------------------------------------------------------------------- systemd units


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keep CamelCase keys
    parser.read(ROOT / "deploy" / name, encoding="utf-8")
    return parser


def test_service_unit_does_not_restart_on_configuration_errors() -> None:
    service_section = unit("ai-tahlilchi.service")["Service"]
    assert service_section["RestartPreventExitStatus"] == "2"
    assert service_section["Restart"] == "on-failure"
    assert int(service_section["TimeoutStopSec"]) > STOP_TIMEOUT_SECONDS
    assert "--exit-code-from bot" in service_section["ExecStart"]


def test_backup_timer_is_persistent_and_runs_the_backup_script() -> None:
    timer = unit("ai-tahlilchi-backup.timer")
    assert timer["Timer"]["Persistent"] == "true"
    assert "OnCalendar" in timer["Timer"]
    service_section = unit("ai-tahlilchi-backup.service")["Service"]
    assert service_section["ExecStart"].endswith("backup.sh")
    assert service_section["Type"] == "oneshot"
