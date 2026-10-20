"""Docker in the console policy (plan de conectores, C7).

`docker` and `docker compose` are classified for every user, with or without
a connector: reads run in every mode, running containers is a local command,
deleting asks in project mode and is refused in read-only. Pure policy: no
docker binary is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rinari.policy.engine import (
    CHOICES_HARD,
    CHOICES_SESSION,
    DOCKER_DESTRUCTIVE,
    DOCKER_EXEC,
    DOCKER_PUSH,
    DOCKER_READ,
    PermissionProfile,
    PolicyAction,
    PolicyEngine,
    SessionScope,
    docker_intent,
)

READ = PermissionProfile.READ_ONLY
WORK = PermissionProfile.WORKSPACE
FULL = PermissionProfile.FULL_ACCESS
ALLOW, ASK, DENY = PolicyAction.ALLOW, PolicyAction.ASK, PolicyAction.DENY


def _scope(tmp_path: Path, profile, **kwargs) -> SessionScope:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    return SessionScope(
        kind="PROJECT", root=root, cwd=root, profile=profile, user_home=tmp_path, **kwargs
    )


def _decide(tmp_path: Path, profile, command: str, **kwargs):
    return PolicyEngine().decide("shell.exec", _scope(tmp_path, profile, **kwargs), command=command)


READS = [
    "docker ps",
    "docker ps -a",
    "docker logs -f web",
    "docker logs web | grep error",
    "docker inspect web",
    "docker images -a",
    "docker version",
    "docker info",
    "docker compose ps",
    "docker compose logs --tail 50 web",
    "docker-compose ps",
    "docker container ls -a",
    "docker image ls",
    "docker volume ls",
    "docker system df",
    "docker -H tcp://10.0.0.5:2375 ps",
    "docker --context staging logs api",
    "docker compose -f deploy/compose.yml ps",
]
EXECS = [
    "docker run --rm -it alpine sh",
    "docker build -t app .",
    "docker exec -it web sh",
    "docker compose up -d",
    "docker compose -p demo up --build",
    "docker-compose up -d",
    "docker start web",
    "docker stop web",
    "docker restart web",
    "docker pull postgres:16",
    "docker compose down",
    "docker compose stop",
    "docker container restart web",
]
DESTRUCTIVE = [
    "docker rm web",
    "docker rm -f web",
    "docker rm --force web db",
    "docker rmi app:old",
    "docker rmi -f app:old",
    "docker container rm -f web",
    "docker image rm --force app",
    "docker volume rm pgdata",
    "docker volume rm -f pgdata",
    "docker network rm backend",
    "docker system prune",
    "docker system prune -a -f",
    "docker system prune --all --force --volumes",
    "docker image prune -a",
    "docker image prune --all --force",
    "docker container prune -f",
    "docker volume prune",
    "docker volume prune -a -f",
    "docker network prune -f",
    "docker compose down -v",
    "docker compose down --volumes",
    "docker compose down --rmi all",
    "docker compose -f compose.yml down -v --remove-orphans",
    "docker-compose down -v",
    "docker compose rm -f",
    "sudo docker rm -f web",
    "docker -H ssh://user@build-box volume rm cache",
    "docker ps && docker rm -f web",
]


@pytest.mark.parametrize("command", READS)
def test_docker_reads_run_in_every_mode(tmp_path, command) -> None:
    assert docker_intent(command).kind == DOCKER_READ
    for profile in (READ, WORK, FULL):
        assert _decide(tmp_path, profile, command).action is ALLOW, profile


@pytest.mark.parametrize("command", EXECS)
def test_docker_commands_that_run_are_local_commands(tmp_path, command) -> None:
    assert docker_intent(command).kind == DOCKER_EXEC
    asked = _decide(tmp_path, READ, command)
    assert asked.action is ASK and asked.choices == CHOICES_HARD
    assert _decide(tmp_path, WORK, command).action is ALLOW
    assert _decide(tmp_path, FULL, command).action is ALLOW


@pytest.mark.parametrize("command", DESTRUCTIVE)
def test_docker_deletions_ask_in_project_mode_and_are_refused_read_only(tmp_path, command) -> None:
    assert docker_intent(command).kind == DOCKER_DESTRUCTIVE
    refused = _decide(tmp_path, READ, command)
    assert refused.action is DENY and refused.rule_id == "docker_destructive"
    asked = _decide(tmp_path, WORK, command)
    assert asked.action is ASK and asked.rule_id == "docker_destructive"
    assert asked.risk == "high" and asked.risk_class == "local-destructive"
    # Once or for this session, never «always»; bound to this exact command.
    assert asked.choices == CHOICES_SESSION and asked.binding_mode == "exact"
    assert _decide(tmp_path, FULL, command).action is ALLOW


def test_docker_push_publishes_like_any_send(tmp_path) -> None:
    command = "docker push registry.example.com/team/app:1.2"
    assert docker_intent(command).kind == DOCKER_PUSH
    assert _decide(tmp_path, READ, command).action is DENY
    asked = _decide(tmp_path, WORK, command)
    assert asked.action is ASK and asked.rule_id == "docker_push"
    assert _decide(tmp_path, FULL, command).action is ALLOW
    # After external content, full access asks only with the owner's switch.
    guarded = {"external_content": True, "ask_new_destinations": True}
    assert _decide(tmp_path, FULL, command, **guarded).rule_id == "external_content_send"


def test_a_read_with_more_than_filters_is_not_a_read(tmp_path) -> None:
    assert docker_intent("docker ps > containers.txt").kind == DOCKER_EXEC
    assert docker_intent("docker ps; npm test").kind == DOCKER_EXEC
    assert _decide(tmp_path, READ, "docker ps > containers.txt").action is ASK


def test_commands_that_only_mention_docker_are_not_docker(tmp_path) -> None:
    for command in ("echo docker rm -f web", "cat docker-compose.yml", "grep docker README.md"):
        assert docker_intent(command) is None


def test_docker_reads_run_from_a_folder_outside_the_project(tmp_path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    scope = {"command_cwd": outside}
    assert _decide(tmp_path, WORK, "docker compose ps", **scope).action is ALLOW
    asked = _decide(tmp_path, WORK, "docker compose up -d", **scope)
    assert asked.action is ASK and asked.rule_id == "shell_cwd_outside"


def test_docker_deletions_on_another_machine_ask_every_time(tmp_path) -> None:
    command = 'ssh build-box "docker system prune -a -f"'
    asked = _decide(tmp_path, WORK, command)
    assert asked.action is ASK and asked.rule_id == "remote_destructive"
    assert asked.choices == CHOICES_HARD
    assert _decide(tmp_path, FULL, command).action is ALLOW


def test_the_catastrophic_list_still_wins_next_to_docker(tmp_path) -> None:
    command = "docker ps && rm -rf /"
    for profile in (WORK, FULL):
        decision = _decide(tmp_path, profile, command)
        assert decision.action is DENY and decision.rule_id == "catastrophic"
