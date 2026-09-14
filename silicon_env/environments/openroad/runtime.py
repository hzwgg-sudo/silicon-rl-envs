"""Per-invocation restricted Docker backend for the pinned GCD flow.

Only trusted source copies and fresh outputs are mounted. The submitted
workspace, host checkout, credentials and Docker socket are never mounted.
ContainerRunner owns deadlines, teardown and the no-network/non-root profile.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from silicon_env.environments.openroad import config, flow
from silicon_env.runners.container import ContainerMount, ContainerRunner
from silicon_env.types import StepStatus

TOOL_ROOT = "/OpenROAD-flow-scripts/tools/install"
TOOLS = {
    "openroad": [f"{TOOL_ROOT}/OpenROAD/bin/openroad", "-version"],
    "yosys": [f"{TOOL_ROOT}/yosys/bin/yosys", "-V"],
    "make": ["make", "--version"],
}


def _backend(tools):
    return ContainerRunner(
        tools=tools, image=config.IMAGE_PINNED_REF,
        allowed_images=[config.IMAGE_PINNED_REF],
        env_allowlist=[*flow.FLOW_ENV_KEYS, "HOME"],
        memory="4g", memory_swap="4g", cpus="1", pids_limit=256,
        user=str(os.getuid() or 1000), scratch_size="1g",
    )


def probe_pinned_tool(name: str) -> tuple[bool, str]:
    if name not in TOOLS:
        return False, ""
    with tempfile.TemporaryDirectory(prefix="gcd-probe-") as temp:
        result = _backend({name: TOOLS[name]}).run(
            name, log_dir=temp, workdir="/tmp", timeout_s=30, env={"HOME": "/tmp"},
        )
        if result.status != StepStatus.SUCCESS:
            return False, result.error
        text = Path(result.stdout_path).read_text().strip()
        return bool(text), text.splitlines()[0] if text else ""


class GcdContainerRunner:
    """Adapt host Make arguments and returned files to isolated container paths."""

    def __init__(self, backend=None):
        self.backend = backend or _backend({flow.FLOW_TOOL_NAME: [
            "/usr/bin/time", "-v", "-o", "/outputs/resources.txt", "make",
        ]})

    def provenance(self):
        return self.backend.provenance()

    def run(self, tool, args=(), *, cwd, log_dir, env=None, timeout_s=None):
        if tool != flow.FLOW_TOOL_NAME:
            raise ValueError(f"unsupported GCD tool: {tool}")
        assignments = dict(arg.split("=", 1) for arg in args if "=" in arg)
        output = Path(assignments["WORK_HOME"])
        hook = Path(assignments["POST_FINAL_REPORT_TCL"])
        # Temp mounts live outside host home directories, as required by M0.
        with tempfile.TemporaryDirectory(prefix="gcd-container-") as temp:
            base = Path(temp)
            source = base / "flow"
            shutil.copytree(cwd, source, symlinks=False)
            out = base / "outputs"
            out.mkdir()
            evidence = base / "final_evidence.tcl"
            shutil.copyfile(hook, evidence)
            evidence.chmod(0o644)
            constraints = base / "constraint.sdc"
            shutil.copyfile(config.FIXED_SDC_PATH, constraints)
            constraints.chmod(0o444)
            # Hosts running as root still launch a non-root container. These
            # disposable copies are the only writable mounts exposed to it.
            for directory, _, files in os.walk(source):
                Path(directory).chmod(0o777)
                for name in files:
                    path = Path(directory) / name
                    path.chmod(0o777 if path.stat().st_mode & 0o111 else 0o666)
            out.chmod(0o777)
            translated = [arg for arg in args if not arg.startswith(
                ("WORK_HOME=", "POST_FINAL_REPORT_TCL=", "SDC_FILE="))]
            translated += [
                "WORK_HOME=/outputs", "POST_FINAL_REPORT_TCL=/trusted/final_evidence.tcl",
                "SDC_FILE=/trusted/constraint.sdc",
                f"OPENROAD_EXE={TOOL_ROOT}/OpenROAD/bin/openroad",
                f"YOSYS_EXE={TOOL_ROOT}/yosys/bin/yosys",
            ]
            result = self.backend.run(
                tool, translated, workdir="/flow", log_dir=log_dir,
                mounts=[ContainerMount(source, "/flow", readonly=False),
                        ContainerMount(out, "/outputs", readonly=False),
                        ContainerMount(evidence, "/trusted/final_evidence.tcl"),
                        ContainerMount(constraints, "/trusted/constraint.sdc")],
                env={**(env or {}), "HOME": "/tmp"}, timeout_s=timeout_s,
            )
            # Preserve timestamps and retain partial evidence on failure.
            shutil.copytree(out, output, dirs_exist_ok=True, symlinks=True)
            return result
