"""M0-04 runner tests: named argv tools, capped streaming, deadlines,
process-group cleanup, spaces in paths, and exit-vs-launch distinction."""

import os
import sys
from pathlib import Path

import pytest

from silicon_env.runner import RunnerError, ToolRunner
from silicon_env.types import StepStatus


def make_runner(tmp_path: Path, **kwargs) -> ToolRunner:
    kwargs.setdefault("tools", {"python": [sys.executable]})
    kwargs.setdefault("env_allowlist", ("PATH", "HOME", "SILICON_TEST_VAR"))
    return ToolRunner(**kwargs)


def run_python(runner: ToolRunner, code: str, cwd: Path, log_dir: Path, **kw):
    return runner.run("python", ["-c", code], cwd=cwd, log_dir=log_dir, **kw)


# --- basic outcomes -------------------------------------------------------


def test_success_captures_stdout(tmp_path):
    runner = make_runner(tmp_path)
    result = run_python(runner, "print('hello-stdout')", cwd=tmp_path, log_dir=tmp_path / "logs")
    assert result.launched and not result.timed_out
    assert result.exit_code == 0
    assert result.status == StepStatus.SUCCESS
    assert result.duration_s >= 0
    assert result.stdout_path.read_text() == "hello-stdout\n"
    assert result.stderr_bytes == 0
    assert not result.stdout_truncated and not result.stderr_truncated
    assert result.to_dict()["status"] == "success"


def test_stderr_streamed_to_separate_capped_file(tmp_path):
    runner = make_runner(tmp_path)
    result = run_python(
        runner,
        "import sys; sys.stderr.write('warn-here\\n'); print('out-here')",
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
    )
    assert result.exit_code == 0
    assert result.stdout_path.read_text() == "out-here\n"
    assert result.stderr_path.read_text() == "warn-here\n"


def test_nonzero_exit_is_tool_failure_not_infra_error(tmp_path):
    runner = make_runner(tmp_path)
    result = run_python(runner, "import sys; sys.exit(3)", cwd=tmp_path, log_dir=tmp_path / "logs")
    assert result.launched and not result.timed_out
    assert result.exit_code == 3
    assert result.status == StepStatus.TOOL_FAILURE


def test_missing_executable_is_infra_error_with_no_exit_code(tmp_path):
    runner = ToolRunner(tools={"missing": ["/nonexistent/no-such-tool-xyz"]})
    result = runner.run("missing", [], cwd=tmp_path, log_dir=tmp_path / "logs")
    assert not result.launched
    assert result.exit_code is None
    assert result.status == StepStatus.INFRA_ERROR
    assert result.error
    # Structured log files still exist.
    assert result.stdout_path.is_file() and result.stderr_path.is_file()


def test_unknown_tool_and_bad_cwd_raise_runner_error(tmp_path):
    runner = make_runner(tmp_path)
    with pytest.raises(RunnerError, match="unknown tool"):
        runner.run("nope", [], cwd=tmp_path, log_dir=tmp_path / "logs")
    with pytest.raises(RunnerError, match="cwd"):
        runner.run("python", [], cwd=tmp_path / "does-not-exist", log_dir=tmp_path / "logs")


def test_no_shell_string_execution(tmp_path):
    # A shell metacharacter in args must not be interpreted by any shell.
    runner = make_runner(tmp_path)
    result = run_python(
        runner,
        "import sys; print(sys.argv[1])",
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
        timeout_s=30,
    )
    # NOTE: no extra argv was passed, so sys.argv[1] is missing -> exit 1.
    # That failure mode itself proves args flow as an argv array (no shell).
    assert result.exit_code == 1
    assert result.status == StepStatus.TOOL_FAILURE
    runner2 = make_runner(
        tmp_path, tools={"echoer": [sys.executable, "-c", "import sys; print(sys.argv[1])"]}
    )
    evil = "$(touch pwned)"
    result2 = runner2.run("echoer", [evil], cwd=tmp_path, log_dir=tmp_path / "logs2")
    assert result2.exit_code == 0
    assert result2.stdout_path.read_text().strip() == evil
    assert not (tmp_path / "pwned").exists()


# --- caps / truncation -----------------------------------------------------


def test_excessive_output_truncated_with_metadata(tmp_path):
    runner = make_runner(tmp_path)
    result = run_python(
        runner,
        "import sys; sys.stdout.write('A' * 100000)",
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
        max_output_bytes=1024,
    )
    assert result.exit_code == 0
    assert result.stdout_truncated
    assert result.stdout_bytes == 1024
    assert result.stdout_path.stat().st_size == 1024
    assert not result.stderr_truncated


def test_stderr_truncation_tracked_independently(tmp_path):
    runner = make_runner(tmp_path)
    result = run_python(
        runner,
        "import sys; sys.stderr.write('E' * 5000)",
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
        max_output_bytes=100,
    )
    assert result.stderr_truncated
    assert result.stderr_bytes == 100
    assert not result.stdout_truncated


# --- timeout + child cleanup ----------------------------------------------


def test_timeout_kills_parent_and_spawned_child(tmp_path):
    runner = make_runner(tmp_path, kill_grace_s=0.5)
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(30)"
    )
    result = run_python(runner, code, cwd=tmp_path, log_dir=tmp_path / "logs", timeout_s=1)
    assert result.timed_out
    assert result.launched
    assert result.status == StepStatus.TIMEOUT
    assert result.error
    assert result.duration_s >= 1
    assert result.duration_s < 15
    # Structured result files exist even on timeout.
    assert result.stdout_path.is_file() and result.stderr_path.is_file()
    # The spawned grandchild shares the process group, so killpg reaps it.
    child_pid = int(pid_file.read_text().strip())
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass  # expected: child is gone
    else:
        # A zombie/orphan still addressable via kill(pid, 0) is a cleanup failure.
        # Give the reaper one more beat, then fail loudly.
        import time as _time

        _time.sleep(1.0)
        assert not _pid_alive(child_pid), "child process survived timeout"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    return True


# --- paths with spaces / env allowlist ------------------------------------


def test_paths_with_spaces_work(tmp_path):
    spaced = tmp_path / "dir with spaces"
    spaced.mkdir()
    logs = spaced / "log dir"
    runner = make_runner(tmp_path)
    result = run_python(runner, "print('spaced-ok')", cwd=spaced, log_dir=logs)
    assert result.exit_code == 0
    assert result.status == StepStatus.SUCCESS
    assert result.stdout_path.read_text() == "spaced-ok\n"


def test_env_allowlist_filters_and_permits(tmp_path):
    runner = ToolRunner(
        tools={"python": [sys.executable]},
        env_allowlist=("PATH", "SILICON_TEST_VAR"),
    )
    os.environ["SILICON_TEST_VAR"] = "allowed-value"
    os.environ["SILICON_SECRET_VAR"] = "must-not-leak"
    code = (
        "import os; print(os.environ.get('SILICON_TEST_VAR', 'MISSING')); "
        "print(os.environ.get('SILICON_SECRET_VAR', 'MISSING'))"
    )
    result = runner.run("python", ["-c", code], cwd=tmp_path, log_dir=tmp_path / "logs")
    lines = result.stdout_path.read_text().splitlines()
    assert lines[0] == "allowed-value"
    assert lines[1] == "MISSING"
    # Per-run env keys outside the allowlist are rejected, not silently passed.
    with pytest.raises(RunnerError, match="allowlist"):
        runner.run(
            "python",
            ["-c", "pass"],
            cwd=tmp_path,
            log_dir=tmp_path / "logs2",
            env={"SILICON_SECRET_VAR": "x"},
        )
    # Per-run allowlisted overrides do reach the child.
    result2 = runner.run(
        "python",
        ["-c", "import os; print(os.environ.get('SILICON_TEST_VAR'))"],
        cwd=tmp_path,
        log_dir=tmp_path / "logs3",
        env={"SILICON_TEST_VAR": "override"},
    )
    assert result2.stdout_path.read_text().strip() == "override"
