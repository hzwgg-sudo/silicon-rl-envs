"""Independent regressions discovered while reviewing the integrated M0 milestone."""

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from silicon_env.environments.toy import TOY_TARGET_TEXT, ToyEnvironment, make_toy_task
from silicon_env.runner import ToolRunner
from silicon_env.runners.container import ContainerMount, ContainerRunner, ContainerRunnerError
from silicon_env.task import Action
from silicon_env.trace import TraceError, read_events, redact_mapping, verify_run
from silicon_env.types import StepStatus
from silicon_env.workspace import MARKER_FILENAME, WorkspaceError, WorkspaceManager


def action(kind, **params):
    return Action(schema_version=1, action_type=kind, params=params)


@pytest.mark.parametrize('timeout', ['bad', -1, True, float('nan'), None])
def test_bad_tool_timeout_is_charged_invalid_action(tmp_path, timeout):
    env = ToyEnvironment(tmp_path)
    try:
        env.reset(make_toy_task())
        result = env.step(action('run_tool', tool='python', timeout_s=timeout))
        assert result.status == StepStatus.INVALID_SUBMISSION
        assert env.budget_tracker.steps_used == 1
        assert env.budget_tracker.tool_calls_used == 0
        assert len(read_events(env.trace_path)) == 2
    finally:
        env.close()


def test_tool_consuming_remaining_wall_time_returns_structured_timeout(tmp_path):
    env = ToyEnvironment(tmp_path)
    env._runner = ToolRunner(tools={'sleep': [sys.executable, '-c', 'import time; time.sleep(10)']},
                             kill_grace_s=0.05)
    task = replace(make_toy_task(max_wallclock_s=0.2), allowed_actions=('run_tool', 'submit'))
    try:
        env.reset(task)
        result = env.step(action('run_tool', tool='sleep'))
        assert result.status == StepStatus.TIMEOUT
        assert result.done
        assert env.budget_tracker.steps_used == env.budget_tracker.tool_calls_used == 1
    finally:
        env.close()


def test_submit_cannot_bypass_action_budget(tmp_path):
    env = ToyEnvironment(tmp_path)
    try:
        env.reset(make_toy_task(max_steps=1))
        env.step(action('write_file', path='note.txt', content=TOY_TARGET_TEXT))
        grade = env.submit()
        assert grade.status.value == 'timeout'
        assert not grade.passed
        assert verify_run(env.run_dir)['passed'] is False
    finally:
        env.close()


def test_reset_does_not_attach_previous_episode_logs(tmp_path):
    env = ToyEnvironment(tmp_path)
    env._runner = ToolRunner(tools={'echo': [sys.executable, '-c', 'print("old episode")']})
    task = replace(make_toy_task(), allowed_actions=('run_tool', 'read_file', 'submit'))
    try:
        env.reset(task)
        env.step(action('run_tool', tool='echo'))
        env.reset(task)
        env.step(action('read_file', path='note.txt'))
        assert read_events(env.trace_path)[-1]['artifact_refs'] == []
    finally:
        env.close()


def test_template_marker_symlink_cannot_overwrite_outside_file(tmp_path):
    template = tmp_path / 'template'
    template.mkdir()
    sentinel = tmp_path / 'sentinel'
    sentinel.write_text('keep me')
    (template / MARKER_FILENAME).symlink_to(sentinel)
    with pytest.raises(WorkspaceError):
        WorkspaceManager(root_dir=tmp_path / 'episodes', template_dir=template).reset()
    assert sentinel.read_text() == 'keep me'


@pytest.mark.parametrize('prefix', ['../escape-', '/tmp/escape-', 'a/b'])
def test_workspace_prefix_cannot_escape_root(tmp_path, prefix):
    template = tmp_path / 'template'
    template.mkdir()
    with pytest.raises(WorkspaceError):
        WorkspaceManager(root_dir=tmp_path / 'episodes', template_dir=template, prefix=prefix)


def test_ownership_marker_protected_even_with_wildcard(tmp_path):
    template = tmp_path / 'template'
    template.mkdir()
    manager = WorkspaceManager(root_dir=tmp_path / 'episodes', template_dir=template,
                               allowed_edit_paths=('**',))
    workspace = manager.reset()
    with pytest.raises(WorkspaceError):
        workspace.write_text(MARKER_FILENAME, 'changed')
    manager.cleanup(workspace)
    assert not workspace.root.exists()


def _assert_stopped(pid):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            return
        time.sleep(0.02)
    pytest.fail(f'child {pid} survived process cleanup')


def test_deadline_applies_when_parent_exits_but_child_holds_pipes(tmp_path):
    pid_file = tmp_path / 'child.pid'
    code = (
        'import subprocess, sys; '
        'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]); '
        f'open({str(pid_file)!r}, "w").write(str(p.pid))'
    )
    runner = ToolRunner(tools={'tool': [sys.executable, '-c', code]}, kill_grace_s=0.05)
    try:
        result = runner.run('tool', cwd=tmp_path, log_dir=tmp_path / 'logs', timeout_s=0.3)
        assert result.status == StepStatus.TIMEOUT
        assert result.duration_s < 2
        _assert_stopped(int(pid_file.read_text()))
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_timeout_kills_child_even_if_it_closed_output_pipes(tmp_path):
    pid_file = tmp_path / 'child.pid'
    child = ('import signal,time,pathlib,os; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
             f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)')
    code = ('import subprocess,sys,time; '
            f'subprocess.Popen([sys.executable,"-c",{child!r}], '
            'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); time.sleep(30)')
    runner = ToolRunner(tools={'tool': [sys.executable, '-c', code]}, kill_grace_s=0.05)
    try:
        result = runner.run('tool', cwd=tmp_path, log_dir=tmp_path / 'logs', timeout_s=0.4)
        assert result.status == StepStatus.TIMEOUT
        _assert_stopped(int(pid_file.read_text()))
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('options', [{'network': 'host'}, {'user': '0:0'}, {'user': 'root'},
                                    {'memory': '0'}, {'cpus': '-1'}, {'scratch_size': '0'}])
def test_container_rejects_disabled_isolation(options):
    with pytest.raises(ContainerRunnerError):
        ContainerRunner(tools={'tool': ['echo']}, **options).build_argv('tool')


def test_container_rejects_mount_containing_secrets_directory(tmp_path, monkeypatch):
    secrets = tmp_path / 'secret'
    secrets.mkdir()
    monkeypatch.setenv('SILICON_EVAL_SECRETS_DIR', str(secrets))
    with pytest.raises(ContainerRunnerError, match='forbidden'):
        ContainerRunner(tools={'tool': ['echo']}).build_argv(
            'tool', mounts=[ContainerMount(tmp_path, '/inputs')])


def fake_docker(tmp_path, code):
    path = tmp_path / 'docker'
    path.write_text(f'#!{sys.executable}\n{code}\n')
    path.chmod(0o755)
    return ContainerRunner(tools={'tool': ['echo']}, docker_bin=str(path), kill_grace_s=0.05)


@pytest.mark.parametrize('exit_code', [0, 1])
def test_tool_diagnostic_text_is_not_misclassified_as_infra(tmp_path, exit_code):
    runner = fake_docker(tmp_path, 'import sys; '
                         f'print("permission denied: not found", file=sys.stderr); '
                         f'sys.exit({exit_code})')
    result = runner.run('tool', log_dir=tmp_path / 'logs')
    assert result.launched
    assert result.status == (StepStatus.SUCCESS if exit_code == 0 else StepStatus.TOOL_FAILURE)


def test_container_output_streams_before_process_finishes(tmp_path, monkeypatch):
    runner = fake_docker(tmp_path, 'import sys; sys.stdout.write("x" * 2_000_000)')
    # communicate() buffers all output in RAM. The bounded runner must not use it.
    def forbid_communicate(*args, **kwargs):
        pytest.fail('unbounded communicate used')
    monkeypatch.setattr(subprocess.Popen, 'communicate', forbid_communicate)
    result = runner.run('tool', log_dir=tmp_path / 'logs', max_output_bytes=1024)
    assert result.status == StepStatus.SUCCESS
    assert result.stdout_truncated
    assert result.stdout_path.stat().st_size == 1024


def test_trace_tampering_detected(tmp_path):
    env = ToyEnvironment(tmp_path)
    try:
        env.reset(make_toy_task())
        env.submit()
        events = read_events(env.trace_path)
        events[0]['seed'] = 999
        env.trace_path.write_text(''.join(json.dumps(e) + '\n' for e in events))
        with pytest.raises(TraceError, match='hash mismatch'):
            verify_run(env.run_dir)
    finally:
        env.close()


def test_nested_secret_values_and_log_artifacts_are_redacted(tmp_path, monkeypatch):
    from silicon_env.trace import TraceRecorder

    monkeypatch.setenv('REVIEW_API_KEY', 'sensitive-test-value')
    payload = {'nested': [{'token': 'nested-secret'}, 'sensitive-test-value']}
    sanitized = json.dumps(redact_mapping(payload))
    assert 'nested-secret' not in sanitized
    assert 'sensitive-test-value' not in sanitized
    recorder = TraceRecorder(tmp_path / 'run', task=make_toy_task())
    log = tmp_path / 'stdout.log'
    log.write_text('plain sensitive-test-value in tool output')
    ref = recorder.ingest_artifact(log, dest_rel='artifacts/stdout.log')
    assert 'sensitive-test-value' not in (recorder.run_dir / ref).read_text()


def test_container_common_cwd_protocol_and_trace_provenance(tmp_path):
    runner = fake_docker(tmp_path, 'import sys; print(" ".join(sys.argv[1:]))')
    env = ToyEnvironment(tmp_path / 'work')
    env._runner = runner
    task = replace(make_toy_task(), allowed_actions=('run_tool', 'submit'))
    try:
        env.reset(task)
        result = env.step(action('run_tool', tool='tool'))
        assert result.status == StepStatus.SUCCESS
        assert f'{env.workspace.root}:/work:rw' in result.observation.stdout_tail
        env.submit()
        manifest = verify_run(env.run_dir)
        assert manifest['runner_provenance']['image'] == runner.image
        assert manifest['runner_provenance']['limits']['pids_limit'] == 128
    finally:
        env.close()


def test_invalid_edit_patterns_fail_before_reset_discards_workspace(tmp_path):
    from silicon_env.types import ContractError

    env = ToyEnvironment(tmp_path)
    try:
        env.reset(make_toy_task())
        original = env.workspace.root
        invalid = replace(make_toy_task(), allowed_edit_paths=('../outside',))
        with pytest.raises(ContractError, match='allowed_edit_paths'):
            env.reset(invalid)
        assert original.is_dir()
    finally:
        env.close()


def test_cancellation_kills_child_that_ignores_term(tmp_path, monkeypatch):
    pid_file = tmp_path / 'cancel-child.pid'
    child = ('import signal,time,pathlib,os; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
             f'pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)')
    code = ('import subprocess,sys,time; '
            f'subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)')
    runner = ToolRunner(tools={'tool': [sys.executable, '-c', code]}, kill_grace_s=0.05)

    def cancel_after_child_ready(proc, **kwargs):
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, '_pump_and_wait', cancel_after_child_ready)
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run('tool', cwd=tmp_path, log_dir=tmp_path / 'logs')
        _assert_stopped(int(pid_file.read_text()))
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cli_usage_error_matches_documented_exit_code():
    from silicon_env.cli import main_run

    with pytest.raises(SystemExit) as exc:
        main_run([])
    assert exc.value.code == 3


def test_cli_preserves_infrastructure_exit_status(tmp_path, monkeypatch):
    from argparse import Namespace

    from silicon_env import cli

    task_path = tmp_path / 'task.json'
    actions_path = tmp_path / 'actions.json'
    task_path.write_text(make_toy_task().to_json())
    actions_path.write_text('[]')
    monkeypatch.setattr(cli, 'run_episode', lambda **kw: {
        'passed': False, 'status': 'infra_error', 'score': 0, 'steps': 0})
    assert cli.cmd_run_task(Namespace(task=str(task_path), actions=str(actions_path),
                                     output_dir=str(tmp_path / 'output'), seed=None)) == 3
