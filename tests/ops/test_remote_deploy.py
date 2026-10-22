"""deploy/remote-deploy.sh against a fake ``docker``: switch, health wait and rollback."""

from pathlib import Path

import pytest

from tests.ops.shell import ROOT, posix, requires_bash, run_script

SCRIPT = ROOT / "deploy" / "remote-deploy.sh"
OLD = "ghcr.io/o/ai-tahlilchi:v1"
NEW = "ghcr.io/o/ai-tahlilchi:v2"
BAD = "ghcr.io/o/ai-tahlilchi:v3-bad"

pytestmark = requires_bash

# A health verdict by image name: references containing "bad" are unhealthy, "slow" never
# become healthy (state stays "starting"). Every call is logged for the assertions.
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_DIR/calls.log"
image() { sed -n 's/^AI_TAHLILCHI_IMAGE=//p' "$APP_DIR/deploy.env"; }
case "$1" in
  login) cat > "$STUB_DIR/login_stdin"; exit 0 ;;
  logout|image) exit 0 ;;
  pull) [ -z "${STUB_PULL_FAIL:-}" ] || exit 1; exit 0 ;;
  inspect)
    case "$*" in
      *Health.Status*)
        case "$(image)" in
          *bad*) echo unhealthy ;;
          *slow*) echo starting ;;
          *) echo healthy ;;
        esac ;;
      *) echo running ;;
    esac
    exit 0 ;;
  compose)
    case "$*" in
      *" ps -q"*) echo cid ;;
      *" up "*) echo "$(image)" >> "$STUB_DIR/ups.log" ;;
    esac
    exit 0 ;;
esac
exit 0
"""

BACKUP_STUB = (
    '#!/usr/bin/env bash\necho "backup $*" >> "$STUB_DIR/calls.log"\n'
    '[ -z "${STUB_BACKUP_FAIL:-}" ]\n'
)


class Server:
    """A fake deployment directory with a fake docker and backup script."""

    def __init__(self, root: Path) -> None:
        self.app = root / "app"
        self.stub = root / "stub"
        (self.app / "bin").mkdir(parents=True)
        self.stub.mkdir()
        (self.app / "docker-compose.yml").write_text("services: {}\n")
        docker = self.stub / "docker"
        docker.write_text(FAKE_DOCKER, newline="\n")
        docker.chmod(0o755)
        backup = self.app / "bin" / "backup.sh"
        backup.write_text(BACKUP_STUB, newline="\n")
        backup.chmod(0o755)
        self.env = {
            "APP_DIR": posix(self.app),
            "STUB_DIR": posix(self.stub),
            "DOCKER": posix(docker),
            "BACKUP_CMD": posix(backup),
            "POLL_SECONDS": "0",
            "HEALTH_TIMEOUT_MINUTES": "1",
        }

    def deploy(self, *args: str, stdin: str | None = None, **env: str):
        return run_script(SCRIPT, *args, env={**self.env, **env}, stdin=stdin)

    def deployed(self) -> str:
        text = (self.app / "deploy.env").read_text()
        return text.strip().removeprefix("AI_TAHLILCHI_IMAGE=")

    def calls(self) -> list[str]:
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def ups(self) -> list[str]:
        log = self.stub / "ups.log"
        return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path)


def test_first_deploy_skips_backup_and_records_the_image(server: Server) -> None:
    result = server.deploy(NEW)

    assert result.returncode == 0, result.stdout + result.stderr
    assert server.deployed() == NEW
    assert not any(call.startswith("backup") for call in server.calls())
    assert server.ups() == [NEW]


def test_update_backs_up_first_then_switches(server: Server) -> None:
    assert server.deploy(OLD).returncode == 0

    result = server.deploy(NEW)

    assert result.returncode == 0, result.stdout
    calls = server.calls()
    backup_index = calls.index("backup --label pre-deploy")
    pull_index = calls.index(f"pull {NEW}")
    assert backup_index < pull_index
    assert server.deployed() == NEW
    assert (server.app / "deploy.env.prev").read_text().strip().endswith(OLD)


def test_unhealthy_release_is_rolled_back_automatically(server: Server) -> None:
    assert server.deploy(OLD).returncode == 0

    result = server.deploy(BAD)

    assert result.returncode == 1
    assert "rolling back" in result.stdout
    assert server.deployed() == OLD
    assert server.ups()[-2:] == [BAD, OLD]


def test_release_that_never_becomes_healthy_is_rolled_back(server: Server) -> None:
    assert server.deploy(OLD).returncode == 0

    result = server.deploy("ghcr.io/o/ai-tahlilchi:slow", HEALTH_TIMEOUT_MINUTES="1")

    assert result.returncode == 1
    assert "not healthy within" in result.stdout
    assert server.deployed() == OLD


def test_unhealthy_first_deploy_reports_and_has_nothing_to_roll_back_to(server: Server) -> None:
    result = server.deploy(BAD)

    assert result.returncode == 1
    assert "no previous version" in result.stdout


def test_failed_pull_changes_nothing(server: Server) -> None:
    assert server.deploy(OLD).returncode == 0

    result = server.deploy(NEW, STUB_PULL_FAIL="1")

    assert result.returncode == 1
    assert server.deployed() == OLD
    assert server.ups() == [OLD]


def test_failed_backup_aborts_before_any_change(server: Server) -> None:
    assert server.deploy(OLD).returncode == 0

    result = server.deploy(NEW, STUB_BACKUP_FAIL="1")

    assert result.returncode == 1
    assert server.deployed() == OLD
    assert f"pull {NEW}" not in server.calls()


def test_skip_backup_flag(server: Server) -> None:
    assert server.deploy(OLD).returncode == 0
    assert server.deploy("--skip-backup", NEW).returncode == 0
    assert "backup --label pre-deploy" not in server.calls()


def test_registry_token_comes_from_stdin_and_the_session_is_removed(server: Server) -> None:
    result = server.deploy(NEW, stdin="s3cret-token\n", GHCR_USER="octocat")

    assert result.returncode == 0, result.stdout
    assert (server.stub / "login_stdin").read_text().strip() == "s3cret-token"
    calls = server.calls()
    assert any(c.startswith("login ghcr.io -u octocat --password-stdin") for c in calls)
    assert all("s3cret-token" not in c for c in calls)
    assert "s3cret-token" not in result.stdout + result.stderr
    assert calls[-1] == "image prune -f" or "logout ghcr.io" in calls


def test_missing_token_with_a_user_is_an_error(server: Server) -> None:
    result = server.deploy(NEW, stdin="", GHCR_USER="octocat")
    assert result.returncode == 1
    assert "no token" in result.stdout


@pytest.mark.parametrize("image", ["", "-rf", "img;rm -rf /", "a b", "img$(id)"])
def test_invalid_image_references_are_rejected(server: Server, image: str) -> None:
    args = [image] if image else []
    result = server.deploy(*args)
    assert result.returncode == 64
    assert server.calls() == []


def test_dry_run_changes_nothing(server: Server) -> None:
    assert server.deploy(OLD).returncode == 0
    before = server.calls()

    result = server.deploy("--dry-run", NEW)

    assert result.returncode == 0
    assert "[dry-run]" in result.stdout
    assert server.deployed() == OLD
    assert server.calls() == before
