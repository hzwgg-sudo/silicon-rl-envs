"""M0-07 trace tests: JSONL round trip, toy episode manifests, interrupted
finalization, hash mismatch, redaction, and stable semantic projection."""

import json
from pathlib import Path

import pytest

from silicon_env.environments.toy import TOY_FILE, TOY_TARGET_TEXT, ToyEnvironment, make_toy_task
from silicon_env.task import Action, Observation
from silicon_env.trace import (
    MANIFEST_FILENAME,
    TRACE_FILENAME,
    TraceError,
    TraceRecorder,
    read_events,
    read_manifest,
    redact_mapping,
    redact_text,
    semantic_hash_for,
    semantic_projection_trace,
    sha256_file,
    verify_run,
)
from silicon_env.types import GradeStatus


def act(action_type: str, params: dict | None = None) -> Action:
    return Action(schema_version=1, action_type=action_type, params=dict(params or {}))


def make_obs(step: int = 0, stdout: str = "ok") -> Observation:
    return Observation(
        schema_version=1,
        step_index=step,
        tool_name="reset",
        exit_code=0,
        stdout_tail=stdout,
        stderr_tail="",
        timed_out=False,
        duration_s=0.0,
    )


def make_recorder(tmp_path: Path, **kw) -> TraceRecorder:
    kw.setdefault("task", make_toy_task(seed=1))
    return TraceRecorder(tmp_path / "run", **kw)


# --- JSONL round trip --------------------------------------------------------


def test_jsonl_round_trip_and_ordered_seqs(tmp_path):
    rec = make_recorder(tmp_path)
    rec.record_reset(make_obs(), {"steps_used": 0})
    action = act("read_file", {"path": TOY_FILE})
    obs = make_obs(step=1, stdout="hello\n")
    from silicon_env.grader import StepResult
    from silicon_env.types import StepStatus

    result = StepResult(
        schema_version=1,
        step_index=1,
        status=StepStatus.SUCCESS,
        reward=0.0,
        done=False,
        observation=obs,
        metrics=(),
        message="read",
    )
    rec.record_step(action, result, {"steps_used": 1})
    events = read_events(rec.trace_path)
    assert [e["seq"] for e in events] == [0, 1]
    assert [e["type"] for e in events] == ["reset", "step"]
    assert events[1]["action"]["action_type"] == "read_file"
    assert events[1]["status"] == "success"
    # Raw file is line-delimited strict JSON.
    lines = rec.trace_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["schema_version"] == 1


def test_unordered_or_non_json_trace_rejected(tmp_path):
    bad = tmp_path / "trace.jsonl"
    bad.write_text('{"seq": 1}\n{"seq": 0}\n', encoding="utf-8")
    with pytest.raises(TraceError, match="ordered"):
        read_events(bad)
    bad.write_text('{"seq": 0, NaN}\n', encoding="utf-8")
    with pytest.raises(TraceError):
        read_events(bad)


# --- toy episode integration --------------------------------------------------


def test_completed_toy_episode_yields_parseable_trace_and_manifest(tmp_path):
    env = ToyEnvironment(work_root=tmp_path / "work")
    try:
        env.reset(make_toy_task(seed=7))
        env.step(act("read_file", {"path": TOY_FILE}))
        env.step(act("write_file", {"path": TOY_FILE, "content": TOY_TARGET_TEXT}))
        grade = env.submit()
        assert grade.status == GradeStatus.PASS
        assert env.run_dir is not None and env.run_dir.is_dir()
        trace_path = env.run_dir / TRACE_FILENAME
        manifest_path = env.run_dir / MANIFEST_FILENAME
        assert trace_path.is_file() and manifest_path.is_file()
        events = read_events(trace_path)
        assert [e["type"] for e in events] == ["reset", "step", "step", "submit"]
        assert [e["seq"] for e in events] == [0, 1, 2, 3]
        manifest = read_manifest(manifest_path)
        assert manifest["seed"] == 7
        assert manifest["status"] == "pass"
        assert manifest["passed"] is True
        assert manifest["complete"] is True
        assert manifest["task_id"] == "toy-text-edit"
        assert len(manifest["task_hash"]) == 64
        assert manifest["trace_file"] == TRACE_FILENAME
        assert manifest["events"] == len(events)
        assert manifest["budgets"]["max_steps"] == 10
        # No dangling refs: every artifact resolves under the run dir.
        verified = verify_run(env.run_dir)
        assert verified["run_id"] == manifest["run_id"]
    finally:
        env.close()


def test_submit_action_via_step_also_finalizes_manifest(tmp_path):
    env = ToyEnvironment(work_root=tmp_path / "work")
    try:
        env.reset(make_toy_task(seed=3))
        env.step(act("write_file", {"path": TOY_FILE, "content": TOY_TARGET_TEXT}))
        result = env.step(act("submit", {}))
        assert result.done
        assert env.run_dir is not None
        manifest = read_manifest(env.run_dir / MANIFEST_FILENAME)
        assert manifest["status"] == "pass"
        assert manifest["complete"] is True
        verify_run(env.run_dir)
    finally:
        env.close()


def test_run_tool_artifacts_copied_with_relative_refs(tmp_path):
    import sys

    from silicon_env.environment import BaseEnvironment
    from silicon_env.runner import ToolRunner
    from silicon_env.task import TaskSpec
    from silicon_env.types import Budget, GraderConfig

    template = tmp_path / "template"
    template.mkdir()
    (template / "note.txt").write_text("hello\n", encoding="utf-8")

    class ScriptEnv(BaseEnvironment):
        def _grade(self):  # pragma: no cover - not graded in this test
            raise AssertionError("no grading here")

    task = TaskSpec(
        schema_version=1,
        task_id="toy-text-edit",
        task_version="0.1.0",
        source_ref="toy@v0.1.0",
        toolchain_refs={},
        seed=0,
        allowed_actions=("run_tool", "submit"),
        allowed_edit_paths=(),
        read_only=False,
        budgets=Budget(max_steps=5, max_wallclock_s=60.0, max_tool_calls=5),
        grader=GraderConfig("toy-grader", "0.1.0", 5.0),
    )
    env = ScriptEnv(
        template_dir=template,
        work_root=tmp_path / "work",
        runner=ToolRunner(tools={"python": [sys.executable]}),
    )
    try:
        env.reset(task)
        result = env.step(act("run_tool", {"tool": "python", "args": ["-c", "print('hi')"]}))
        assert result.observation.stdout_tail.strip() == "hi"
        assert env.run_dir is not None
        # Step event already references the copied logs via relative refs.
        events = read_events(env.run_dir / "trace.jsonl")
        refs = events[-1].get("artifact_refs", [])
        assert refs, "expected copied tool logs"
        assert all(not Path(r).is_absolute() and ".." not in r for r in refs)
        for ref in refs:
            assert (env.run_dir / ref).is_file()
    finally:
        env.close()
    # Close finalizes the (unsubmitted) episode as incomplete; refs still resolve.
    assert env.run_dir is not None
    manifest = verify_run(env.run_dir)
    assert manifest["status"] == "incomplete"
    assert [a["path"] for a in manifest["artifacts"]] != []


# --- interrupted finalization --------------------------------------------------


def test_interrupted_run_finalizes_incomplete_never_successful(tmp_path):
    env = ToyEnvironment(work_root=tmp_path / "work")
    try:
        env.reset(make_toy_task(seed=9))
        env.step(act("read_file", {"path": TOY_FILE}))
        run_dir = env.run_dir
        assert run_dir is not None
        env.close()  # no submit
        manifest = read_manifest(run_dir / MANIFEST_FILENAME)
        assert manifest["status"] == "incomplete"
        assert manifest["passed"] is False
        assert manifest["complete"] is False
        verify_run(run_dir)
    finally:
        env.close()


def test_double_reset_marks_first_run_incomplete(tmp_path):
    env = ToyEnvironment(work_root=tmp_path / "work")
    try:
        env.reset(make_toy_task(seed=1))
        first_run = env.run_dir
        assert first_run is not None
        env.reset(make_toy_task(seed=2))  # supersedes the first episode
        assert env.run_dir != first_run
        manifest = read_manifest(first_run / MANIFEST_FILENAME)
        assert manifest["status"] == "incomplete"
        assert manifest["passed"] is False
    finally:
        env.close()


def test_standalone_finalize_incomplete(tmp_path):
    rec = make_recorder(tmp_path)
    rec.record_reset(make_obs(), {})
    manifest = rec.finalize_incomplete("test-interrupt", {})
    assert manifest["status"] == "incomplete"
    assert manifest["passed"] is False
    assert manifest["complete"] is False
    with pytest.raises(TraceError, match="never be marked passed"):
        rec2 = TraceRecorder(tmp_path / "run2", task=make_toy_task(seed=1))
        rec2.record_reset(make_obs(), {})
        rec2.finalize(status="incomplete", passed=True)


# --- hash mismatch --------------------------------------------------------------


def test_hash_mismatch_detected(tmp_path):
    env = ToyEnvironment(work_root=tmp_path / "work")
    try:
        env.reset(make_toy_task(seed=1))
        env.step(act("write_file", {"path": TOY_FILE, "content": TOY_TARGET_TEXT}))
        env.submit()
        assert env.run_dir is not None
        manifest = read_manifest(env.run_dir / MANIFEST_FILENAME)
        assert manifest["artifacts"] == []  # toy file edits copy no tool logs
        # Plant a fake artifact with a wrong hash to prove verification bites.
        planted = env.run_dir / "artifacts" / "step_1" / "stdout.log"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text("tampered\n", encoding="utf-8")
        manifest["artifacts"] = [
            {"path": "artifacts/step_1/stdout.log", "sha256": "0" * 64, "size_bytes": 9}
        ]
        import os

        tmp_manifest = env.run_dir / "manifest.json.tmp"
        tmp_manifest.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_manifest, env.run_dir / MANIFEST_FILENAME)
        with pytest.raises(TraceError, match="hash mismatch"):
            verify_run(env.run_dir)
    finally:
        env.close()


def test_tampered_artifact_bytes_detected(tmp_path):
    rec = make_recorder(tmp_path)
    rec.record_reset(make_obs(), {})
    src = tmp_path / "out.log"
    src.write_text("original\n", encoding="utf-8")
    rec.ingest_artifact(src, dest_rel="artifacts/step_1/stdout.log")
    rec.finalize(status="pass", passed=True, budget_snapshot={})
    copied = rec.run_dir / "artifacts" / "step_1" / "stdout.log"
    assert sha256_file(copied) == sha256_file(src)
    copied.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(TraceError, match="hash mismatch"):
        verify_run(rec.run_dir)


def test_dangling_artifact_ref_rejected(tmp_path):
    rec = make_recorder(tmp_path)
    rec.record_reset(make_obs(), {})
    manifest = rec.finalize(status="pass", passed=True, budget_snapshot={})
    manifest["artifacts"] = [
        {"path": "artifacts/missing.log", "sha256": "ab" * 32, "size_bytes": 1}
    ]
    import os

    tmp_manifest = rec.run_dir / "manifest.json.tmp"
    tmp_manifest.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_manifest, rec.run_dir / MANIFEST_FILENAME)
    with pytest.raises(TraceError, match="dangling artifact"):
        verify_run(rec.run_dir)


# --- redaction -------------------------------------------------------------------


def test_secret_mapping_redacted():
    redacted = redact_mapping({"API_KEY": "sk-live-123", "path": "note.txt"})
    assert redacted["API_KEY"] == "***REDACTED***"
    assert redacted["path"] == "note.txt"
    assert "sk-live-123" not in json.dumps(redacted)


def test_inline_secret_and_host_path_redacted():
    text = redact_text("api_key=sk-live-123 in /tmp/work/abc", extra_roots=("/tmp/work/abc",))
    assert "sk-live-123" not in text
    assert "<REDACTED>" in text
    assert "/tmp/work/abc" not in text


def test_trace_events_redact_secrets_and_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_API_KEY", "sk-live-should-not-appear")
    rec = make_recorder(tmp_path)
    rec.record_reset(make_obs(stdout=f"wrote {rec.run_dir}"), {})
    from silicon_env.grader import StepResult
    from silicon_env.types import StepStatus

    action = act("write_file", {"path": TOY_FILE, "API_KEY": "sk-live-should-not-appear"})
    result = StepResult(
        schema_version=1,
        step_index=1,
        status=StepStatus.SUCCESS,
        reward=0.0,
        done=False,
        observation=make_obs(step=1, stdout=f"token abc under {rec.run_dir}"),
        metrics=(),
        message="api_key=sk-live-should-not-appear",
    )
    rec.record_step(action, result, {})
    raw = rec.trace_path.read_text(encoding="utf-8")
    assert "sk-live-should-not-appear" not in raw
    assert str(rec.run_dir) not in raw


# --- semantic projection ------------------------------------------------------------


def test_semantic_projection_strips_timing_but_keeps_semantics():
    events = [
        {
            "seq": 0,
            "type": "reset",
            "run_id": "run_aaa",
            "timestamp_s": 111.0,
            "observation": {"stdout_tail": "hi", "duration_s": 0.5},
            "budget": {"steps_used": 0, "elapsed_s": 1.0, "remaining_wallclock_s": 59.0},
        },
        {
            "seq": 1,
            "type": "step",
            "run_id": "run_aaa",
            "timestamp_s": 222.0,
            "status": "success",
            "observation": {"stdout_tail": "hi", "duration_s": 2.0},
            "budget": {"steps_used": 1, "elapsed_s": 3.0, "remaining_wallclock_s": 57.0},
        },
    ]
    projected = semantic_projection_trace(events)
    dumped = json.dumps(projected)
    assert "timestamp_s" not in dumped
    assert "duration_s" not in dumped
    assert "elapsed_s" not in dumped
    assert "remaining_wallclock_s" not in dumped
    assert "run_aaa" not in dumped  # run identity is not replay semantics
    assert projected[0]["observation"]["stdout_tail"] == "hi"
    assert projected[1]["status"] == "success"
    assert projected[0]["seq"] == 0  # ordering preserved


def test_semantic_hash_stable_across_clocks(tmp_path):
    def run_once(run_name: str, tick: float) -> str:
        rec = TraceRecorder(
            tmp_path / run_name, task=make_toy_task(seed=5), clock=lambda: tick
        )
        rec.record_reset(make_obs(stdout="ready"), {"steps_used": 0, "elapsed_s": tick})
        from silicon_env.grader import StepResult
        from silicon_env.types import StepStatus

        action = act("read_file", {"path": TOY_FILE})
        result = StepResult(
            schema_version=1,
            step_index=1,
            status=StepStatus.SUCCESS,
            reward=0.0,
            done=False,
            observation=Observation(
                schema_version=1,
                step_index=1,
                tool_name="read_file",
                exit_code=0,
                stdout_tail="hello\n",
                stderr_tail="",
                timed_out=False,
                duration_s=tick,
            ),
            metrics=(),
            message="read",
        )
        rec.record_step(action, result, {"steps_used": 1, "elapsed_s": tick + 1})
        events = read_events(rec.trace_path)
        assert events[0]["timestamp_s"] == tick  # timing really differed
        return semantic_hash_for(events)

    assert run_once("run_a", 1000.0) == run_once("run_b", 2000.0)
