"""M0-09 container runner tests.

Default (no-Docker) unit tests: command construction, volume-allowlist
rejection, missing-Docker infra error, provenance. These MUST pass on
machines without Docker (e.g. M1 Mac CI).

Opt-in integration tests (real filesystem / network / timeout isolation
checks) run only when ``SILICON_RUN_DOCKER_TESTS=1`` is set. Missing
Docker, daemon, or image fails an explicitly requested run.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from silicon_env.runner import RunResult
from silicon_env.runners.container import (
    DEFAULT_IMAGE,
    ContainerMount,
    ContainerRunner,
    ContainerRunnerError,
    build_docker_argv,
    docker_available,
)
from silicon_env.types import StepStatus

RUN_DOCKER_TESTS = os.environ.get("SILICON_RUN_DOCKER_TESTS", "") == "1"


def make_runner(**kwargs):
    kwargs.setdefault("tools", {"python": ["python3"]})
    return ContainerRunner(**kwargs)


def make_mounts(tmp_path: Path):
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "in.txt").write_text("input-data\n")
    outdir = tmp_path / "outputs"
    outdir.mkdir(exist_ok=True)
    return [
        ContainerMount(inputs, "/task/inputs", readonly=True),
        ContainerMount(outdir, "/task/outputs", readonly=False),
    ]


# --- pure command construction (no Docker needed) ---------------------------


def test_hardening_flags_present(tmp_path):
    mounts = make_mounts(tmp_path)
    cmd = make_runner().build_argv(
        "python", ["-c", "print(1)"], mounts=mounts, container_name="probe"
    )
    assert cmd[:2] == ["docker", "run"]
    for flag in (
        ["--rm"],
        ["--network", "none"],
        ["--pull", "never"],
        ["--user", "65532:65532"],
        ["--read-only"],
        ["--cap-drop", "ALL"],
        ["--pids-limit", "128"],
        ["--memory", "512m"],
        ["--memory-swap", "512m"],
        ["--cpus", "1.0"],
        ["--init", DEFAULT_IMAGE],
    ):
        assert flag_in(cmd, flag), f"missing {flag} in {cmd}"
    # Writable scratch is an explicit tmpfs, not the host filesystem.
    assert any(a.startswith("/scratch:") and "rw" in a for a in tmpfs_args(cmd)), (
        f"no rw /scratch tmpfs in {cmd}"
    )
    # No privileged / host-leaking flags may ever appear.
    for banned in ("--privileged", "--pid=host", "--network=host", "--gpus"):
        assert banned not in cmd
    assert not any("docker.sock" in a for a in cmd)


def test_mount_modes_and_workdir(tmp_path):
    mounts = make_mounts(tmp_path)
    cmd = make_runner().build_argv(
        "python", [], mounts=mounts, container_name="probe", workdir="/task/inputs"
    )
    vols = volume_args(cmd)
    assert len(vols) == 2
    assert vols[0].endswith(":/task/inputs:ro")
    assert vols[1].endswith(":/task/outputs:rw")
    assert ["--workdir", "/task/inputs"] in pairwise(cmd)


def test_tool_argv_appended_without_shell(tmp_path):
    mounts = make_mounts(tmp_path)
    evil = "$(touch pwned); `id`"
    cmd = make_runner().build_argv(
        "python", ["-c", "print(1)", evil], mounts=mounts, container_name="probe"
    )
    # The evil string is one argv element after the image, never a shell string.
    idx = cmd.index(DEFAULT_IMAGE)
    tail = cmd[idx + 1 :]
    assert tail == ["python3", "-c", "print(1)", evil]
    assert "sh" not in tail and "-c" in tail  # python's -c, not a shell


def test_env_allowlist_filters_and_rejects(tmp_path):
    mounts = make_mounts(tmp_path)
    runner = make_runner(env_allowlist=("PATH", "SILICON_OK"))
    os.environ["SILICON_OK"] = "yes"
    os.environ["SILICON_SECRET_X"] = "no"
    cmd = runner.build_argv("python", [], mounts=mounts, container_name="probe")
    env_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--env"]
    assert any(e.startswith("SILICON_OK=") for e in env_args)
    assert not any("SILICON_SECRET_X" in e for e in env_args)
    with pytest.raises(ContainerRunnerError, match="allowlist"):
        runner.build_argv(
            "python",
            [],
            mounts=mounts,
            container_name="probe",
            env={"SILICON_SECRET_X": "x"},
        )


def test_unknown_tool_raises(tmp_path):
    with pytest.raises(ContainerRunnerError, match="unknown tool"):
        make_runner().build_argv("nope", [], mounts=[], container_name="probe")


def test_unpinned_and_off_allowlist_images_rejected():
    with pytest.raises(ContainerRunnerError, match="allowlist"):
        ContainerRunner(tools={"t": ["x"]}, image="ubuntu:22.04")
    with pytest.raises(ContainerRunnerError, match="floating.*latest|pinned"):
        ContainerRunner(
            tools={"t": ["x"]},
            image="evil:latest",
            allowed_images=["evil:latest"],
        )
    with pytest.raises(ContainerRunnerError, match="pinned"):
        ContainerRunner(
            tools={"t": ["x"]},
            image="evil",
            allowed_images=["evil"],
        )
    # Digest-pinned refs on the allowlist are accepted.
    digest_ref = "custom:1.2.3@sha256:" + "ab" * 32
    runner = ContainerRunner(tools={"t": ["x"]}, image=digest_ref, allowed_images=[digest_ref])
    assert runner.image == digest_ref


# --- volume allowlist rejection (no Docker needed) --------------------------


def test_home_mount_rejected(tmp_path):
    home = str(Path.home())
    with pytest.raises(ContainerRunnerError, match="forbidden"):
        make_runner().build_argv(
            "python",
            [],
            mounts=[ContainerMount(home, "/mnt/home", readonly=True)],
            container_name="probe",
        )


def test_docker_socket_mount_rejected(tmp_path):
    with pytest.raises(ContainerRunnerError, match="forbidden"):
        build_docker_argv(
            container_name="probe",
            image=DEFAULT_IMAGE,
            tool_argv=["python3"],
            mounts=[ContainerMount("/var/run/docker.sock", "/sock", readonly=True)],
        )


def test_root_and_etc_mounts_rejected():
    for host in ("/", "/etc", "/etc/hostname", "/var/run"):
        with pytest.raises(ContainerRunnerError, match="forbidden"):
            make_runner().build_argv(
                "python",
                [],
                mounts=[ContainerMount(host, "/mnt/x", readonly=True)],
                container_name="probe",
            )


def test_secrets_dir_mount_rejected(tmp_path, monkeypatch):
    secrets = tmp_path / "eval-secrets"
    secrets.mkdir()
    monkeypatch.setenv("SILICON_EVAL_SECRETS_DIR", str(secrets))
    with pytest.raises(ContainerRunnerError, match="forbidden"):
        make_runner().build_argv(
            "python",
            [],
            mounts=[ContainerMount(str(secrets), "/mnt/s", readonly=True)],
            container_name="probe",
        )


def test_relative_and_missing_mounts_rejected(tmp_path):
    with pytest.raises(ContainerRunnerError, match="absolute"):
        make_runner().build_argv(
            "python",
            [],
            mounts=[ContainerMount("relative/path", "/mnt/x", readonly=True)],
            container_name="probe",
        )
    with pytest.raises(ContainerRunnerError, match="does not exist"):
        make_runner().build_argv(
            "python",
            [],
            mounts=[ContainerMount(str(tmp_path / "no-such-dir"), "/mnt/x", readonly=True)],
            container_name="probe",
        )


def test_duplicate_and_escaping_container_paths_rejected(tmp_path):
    mounts = make_mounts(tmp_path)
    dup = [
        ContainerMount(mounts[0].host_path, "/same", readonly=True),
        ContainerMount(mounts[1].host_path, "/same", readonly=False),
    ]
    with pytest.raises(ContainerRunnerError, match="duplicate"):
        make_runner().build_argv("python", [], mounts=dup, container_name="probe")
    with pytest.raises(ContainerRunnerError, match=r"\.\."):
        make_runner().build_argv(
            "python",
            [],
            mounts=[ContainerMount(mounts[0].host_path, "/a/../b", readonly=True)],
            container_name="probe",
        )


# --- missing-Docker behavior (no Docker needed) ------------------------------


def test_missing_docker_is_infra_error_never_success(tmp_path):
    runner = ContainerRunner(tools={"python": ["python3"]}, docker_bin="/nonexistent/docker-xyz")
    assert not docker_available("/nonexistent/docker-xyz")
    mounts = make_mounts(tmp_path)
    result = runner.run("python", ["-c", "print(1)"], mounts=mounts, log_dir=tmp_path / "logs")
    assert isinstance(result, RunResult)
    assert result.status == StepStatus.INFRA_ERROR
    assert result.status != StepStatus.SUCCESS
    assert not result.launched
    assert result.exit_code is None
    assert result.error
    assert result.stdout_path.is_file() and result.stderr_path.is_file()


def test_provenance_reports_image_and_limits():
    runner = make_runner(memory="256m", cpus="0.5")
    prov = runner.provenance()
    assert prov["runner"] == "container"
    assert prov["image"] == DEFAULT_IMAGE
    assert prov["limits"]["memory"] == "256m"
    assert prov["limits"]["cpus"] == "0.5"
    assert prov["security"]["network"] == "none"
    assert prov["security"]["user"] == "65532:65532"
    assert prov["security"]["read_only_root"] is True
    # JSON-serializable so it can ride along in manifests.
    import json

    json.dumps(prov)


# --- helpers -----------------------------------------------------------------


def flag_in(cmd: list[str], flag: list[str]) -> bool:
    for i in range(len(cmd) - len(flag) + 1):
        if cmd[i : i + len(flag)] == flag:
            return True
    return False


def pairwise(cmd: list[str]) -> list[list[str]]:
    return [cmd[i : i + 2] for i in range(len(cmd) - 1)]


def volume_args(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--volume"]


def tmpfs_args(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--tmpfs"]


# --- opt-in integration tests (need Linux + Docker) ---------------------------
# Default tests skip integration; explicitly opting in requires a working runtime.

needs_docker = pytest.mark.skipif(
    not RUN_DOCKER_TESTS,
    reason="opt-in Docker integration tests require SILICON_RUN_DOCKER_TESTS=1",
)


def _docker(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True, **kwargs)


@pytest.fixture
def docker_runtime():
    # An explicitly requested integration run must fail, not silently skip,
    # when the binary, daemon or pre-pulled image is unavailable.
    assert docker_available(), "Docker is required when SILICON_RUN_DOCKER_TESTS=1"
    info = json.loads(_docker(["docker", "info", "--format", "{{json .}}"]).stdout)
    assert info["OSType"] == "linux"
    image = json.loads(_docker(["docker", "image", "inspect", DEFAULT_IMAGE]).stdout)[0]
    assert image["RepoDigests"], "integration image must have a registry digest"
    return image



@needs_docker
def test_integration_no_read_outside_mounts_no_write_to_readonly(tmp_path, docker_runtime):
    """Container cannot read a host sentinel outside mounts nor write a ro input."""
    sentinel = tmp_path / "host-sentinel.txt"
    sentinel.write_text("top-secret-sentinel\n")
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "in.txt").write_text("readonly-input\n")
    (inputs / "in.txt").chmod(0o666)  # Only the read-only mount should prevent writes.
    outdir = tmp_path / "outputs"
    outdir.mkdir(exist_ok=True)
    outdir.chmod(0o777)  # The non-root container must be able to write candidate output.
    runner = make_runner()
    mounts = [
        ContainerMount(str(inputs), "/task/inputs", readonly=True),
        ContainerMount(str(outdir), "/task/outputs", readonly=False),
    ]
    code = (
        "import errno, pathlib, sys\n"
        f"print('sentinel-exists:', pathlib.Path({str(sentinel)!r}).exists())\n"
        "try:\n"
        "    print(open('/task/inputs/in.txt').read().strip())\n"
        "except Exception as e:\n"
        "    print('read-failed:', type(e).__name__)\n"
        "    sys.exit(3)\n"
        "try:\n"
        "    open('/task/inputs/in.txt', 'w').write('pwned')\n"
        "    print('WRITE-SUCCEEDED-BAD')\n"
        "except OSError as e:\n"
        "    assert e.errno == errno.EROFS, e\n"
        "    print('write-blocked-ok:', type(e).__name__)\n"
        "pathlib.Path('/task/outputs/candidate.txt').write_text('candidate-output')\n"
    )
    result = runner.run(
        "python",
        ["-c", code],
        mounts=mounts,
        log_dir=tmp_path / "logs",
        timeout_s=120,
    )
    assert result.status == StepStatus.SUCCESS, result.error
    out = result.stdout_path.read_text()
    assert "sentinel-exists: False" in out
    assert "readonly-input" in out
    assert "WRITE-SUCCEEDED-BAD" not in out
    assert "write-blocked-ok:" in out
    assert (outdir / "candidate.txt").read_text() == "candidate-output"
    assert (inputs / "in.txt").read_text() == "readonly-input\n"


@needs_docker
def test_integration_no_network(tmp_path, docker_runtime):
    """No network inside the container (socket creation / egress fails)."""
    outdir = tmp_path / "outputs"
    outdir.mkdir(exist_ok=True)
    outdir.chmod(0o777)  # The non-root container must be able to write candidate output.
    runner = make_runner()
    code = (
        "import socket\n"
        "assert {name for _, name in socket.if_nameindex()} == {'lo'}\n"
        "s = socket.socket()\n"
        "s.settimeout(3)\n"
        "try:\n"
        "    s.connect(('8.8.8.8', 53))\n"
        "    print('NETWORK-REACHABLE-BAD')\n"
        "except Exception as e:\n"
        "    print('network-blocked-ok:', type(e).__name__)\n"
    )
    result = runner.run(
        "python",
        ["-c", code],
        mounts=[ContainerMount(str(outdir), "/task/outputs", readonly=False)],
        log_dir=tmp_path / "logs",
        timeout_s=120,
    )
    assert result.status == StepStatus.SUCCESS, result.error
    assert "NETWORK-REACHABLE-BAD" not in result.stdout_path.read_text()


@needs_docker
def test_integration_timeout_removes_container(tmp_path, docker_runtime):
    """A runaway container maps to TIMEOUT and leaves no live container."""
    outdir = tmp_path / "outputs"
    outdir.mkdir(exist_ok=True)
    outdir.chmod(0o777)  # The non-root container must be able to write candidate output.
    runner = make_runner()
    before = _docker(["docker", "ps", "-aq"]).stdout
    result = runner.run(
        "python",
        ["-c", "import time; time.sleep(300)"],
        mounts=[ContainerMount(str(outdir), "/task/outputs", readonly=False)],
        log_dir=tmp_path / "logs",
        timeout_s=5,
    )
    assert result.status == StepStatus.TIMEOUT
    assert result.timed_out
    after = _docker(["docker", "ps", "-aq"]).stdout
    leaked = set(after.split()) - set(before.split())
    assert not leaked, f"live containers leaked after timeout: {leaked}"


@needs_docker
def test_integration_effective_identity_and_limits(tmp_path, docker_runtime):
    runner = make_runner()
    code = (
        "import json, os, pathlib\n"
        "status = dict(line.split(':', 1) for line in "
        "pathlib.Path('/proc/self/status').read_text().splitlines())\n"
        "assert os.getuid() == 65532\n"
        "assert int(status['CapEff'].strip(), 16) == 0\n"
        "assert status['NoNewPrivs'].strip() == '1'\n"
        "limits = {name: pathlib.Path('/sys/fs/cgroup', name).read_text().strip() "
        "for name in ['memory.max', 'memory.swap.max', 'pids.max', 'cpu.max']}\n"
        "print(json.dumps(limits))\n"
    )
    result = runner.run('python', ['-c', code], log_dir=tmp_path / 'limits', timeout_s=30)
    assert result.status == StepStatus.SUCCESS, result.stderr_path.read_text()
    limits = json.loads(result.stdout_path.read_text())
    assert limits['memory.max'] == str(512 * 1024 * 1024)
    assert limits['memory.swap.max'] == '0'
    assert limits['pids.max'] == '128'
    quota, period = map(int, limits['cpu.max'].split())
    assert quota / period == float(runner.provenance()['limits']['cpus'])
    assert docker_runtime['Os'] == 'linux'
