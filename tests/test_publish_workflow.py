"""Text-level contract for the release pipeline, so drift fails without a YAML parser."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE_REQUIREMENTS = ROOT / ".github" / "release-requirements.txt"
GATES = {
    "tests": "test.yml", "shells": "shells.yml", "pi": "pi.yml", "zellij": "zellij.yml",
    "tmux": "tmux-mux.yml",
}
FULL_SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*$")
DRY_RUN = "release-dry-run.yml"
DRY_RUN_VERSION = "${{ inputs.version || '0.10.0' }}"
TAG_ON_MAIN_STEP = "Verify the tag commit is on main"


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _sections(text: str, indent: int) -> dict[str, str]:
    """Split a block into its keys at one indentation level, keeping nested lines."""
    pattern = re.compile(rf"^ {{{indent}}}([A-Za-z0-9_-]+):(.*)$")
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            current = match.group(1)
            sections[current] = [match.group(2).strip()]
        elif current is not None and (
            not line.strip() or line.startswith(" " * (indent + 1))
        ):
            sections[current].append(line)
        else:
            current = None
    return {key: "\n".join(lines).lstrip("\n").rstrip() for key, lines in sections.items()}


def _jobs(name: str) -> dict[str, str]:
    return _sections(_sections(_read(name), 0)["jobs"], 2)


def _steps(job: str) -> list[str]:
    """The steps of one job body, each as its text without trailing blank lines."""
    body = job.split("\n    steps:\n", 1)[1]
    steps = re.split(r"(?m)^(?=      - )", body)
    return [step.rstrip() for step in steps if step.strip()]


def _run_blocks(text: str) -> list[str]:
    """Every ``run:`` value, with its indented continuation lines."""
    blocks = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?run:(.*)$", line)
        if not match:
            continue
        indent = len(match.group(1))
        block = [match.group(2)]
        for following in lines[index + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            block.append(following)
        blocks.append("\n".join(block))
    return blocks


def _uses(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^\s*-?\s*uses:\s*(.+)$", text, re.M)]


@pytest.mark.parametrize("name", sorted(GATES.values()))
def test_gate_workflow_is_reusable_without_legacy_branch(name):
    top = _sections(_read(name), 0)
    triggers = _sections(top["on"], 2)
    assert "workflow_call" in triggers
    assert set(triggers) == {"push", "pull_request", "workflow_call"}
    assert "feat/harnesses-desktop" not in _read(name)
    assert re.fullmatch(r"\s*contents: read\s*", top["permissions"])


@pytest.mark.parametrize("name", sorted(GATES.values()))
def test_gate_concurrency_does_not_use_caller_workflow_name(name):
    # In a called workflow github.workflow is the caller's name, so a shared
    # group would make the gates called by publish.yml cancel each other.
    group = re.search(r"^  group: (.+)$", _sections(_read(name), 0)["concurrency"], re.M)
    assert group is not None
    assert "github.workflow" not in group.group(1)


def test_gate_concurrency_groups_are_distinct():
    groups = {
        re.search(r"^  group: (.+)$", _read(name), re.M).group(1)
        for name in GATES.values()
    }
    assert len(groups) == len(GATES)


def test_tests_workflow_runs_plugin_tests_with_tongs_extra():
    jobs = _jobs("test.yml")
    tongs_jobs = [
        body for body in jobs.values() if 'pip install -e ".[dev,tongs]"' in body
    ]
    assert len(tongs_jobs) == 1
    body = tongs_jobs[0]
    assert 'python-version: "3.12"' in body
    assert re.search(r"^\s+run: pytest", body, re.M)


def test_tests_workflow_runs_lowest_textual_on_oldest_python():
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    textual = [d for d in dependencies["project"]["dependencies"] if d.startswith("textual")]
    floor = re.fullmatch(r"textual>=(\d+),<\d+", textual[0])
    assert floor, textual
    lowest = [
        body for body in _jobs("test.yml").values()
        if f'"textual=={floor.group(1)}.0.0"' in body
    ]
    assert len(lowest) == 1
    assert 'python-version: "3.11"' in lowest[0]
    assert 'pip install -e ".[dev]"' in lowest[0]
    assert re.search(r"^\s+run: pytest", lowest[0], re.M)


def test_publish_triggers_only_on_version_tags():
    triggers = _sections(_sections(_read("publish.yml"), 0)["on"], 2)
    assert set(triggers) == {"push"}
    assert re.fullmatch(
        r"\s*tags:\s*\n\s*- \"v\*\"\s*", triggers["push"]
    ), triggers["push"]


def test_publish_top_level_permissions_are_read_only():
    top = _sections(_read("publish.yml"), 0)
    assert re.fullmatch(r"\s*contents: read\s*", top["permissions"])


def test_publish_gates_call_reusable_workflows():
    jobs = _jobs("publish.yml")
    for job, workflow in GATES.items():
        assert _sections(jobs[job], 4)["uses"] == f"./.github/workflows/{workflow}"


def test_id_token_write_only_in_publish_job():
    text = _read("publish.yml")
    assert text.count("id-token: write") == 1
    publish = _sections(_jobs("publish.yml")["publish"], 4)
    assert re.fullmatch(r"\s*id-token: write\s*", publish["permissions"])


def test_publish_job_uses_pypi_environment_after_every_gate():
    publish = _sections(_jobs("publish.yml")["publish"], 4)
    environment = publish["environment"]
    assert environment == "pypi" or re.search(r"^\s+name: pypi$", environment, re.M)
    needs = {item.strip() for item in publish["needs"].strip("[]").split(",")}
    assert needs == {*GATES, "build", "verify"}


def test_verify_job_checks_main_ancestry_and_built_version():
    jobs = _jobs("publish.yml")
    verify = jobs["verify"]
    assert "build" in _sections(verify, 4)["needs"]
    assert "refs/heads/main:refs/remotes/origin/main" in verify
    assert 'git merge-base --is-ancestor "$tag_commit" refs/remotes/origin/main' in verify
    assert "TAG_NAME: ${{ github.ref_name }}" in verify
    assert "${{ github.ref_name }}" not in verify.split("run:", 1)[1].replace(
        "TAG_NAME: ${{ github.ref_name }}", ""
    )
    assert "dev or local suffix" in verify
    assert 'HeaderParser().parsestr' in verify


def test_build_job_installs_hash_pinned_tools_and_checks_strictly():
    build = _jobs("publish.yml")["build"]
    assert "--require-hashes" in build
    assert "-r .github/release-requirements.txt" in build
    assert "python -m build --no-isolation" in build
    assert "python -m twine check --strict dist/*" in build
    assert "pip install build" not in build


def test_publish_actions_are_pinned_by_full_sha():
    uses = _uses(_read("publish.yml"))
    remote = [u for u in uses if not u.startswith("./")]
    assert remote
    for ref in remote:
        assert FULL_SHA_PIN.match(ref), ref
    names = {ref.split("@", 1)[0] for ref in remote}
    assert {
        "pypa/gh-action-pypi-publish",
        "actions/upload-artifact",
        "actions/download-artifact",
    } <= names


@pytest.mark.parametrize("name", sorted(GATES.values()))
def test_gate_actions_are_pinned_by_full_sha(name):
    remote = [u for u in _uses(_read(name)) if not u.startswith("./")]
    assert remote
    for ref in remote:
        assert FULL_SHA_PIN.match(ref), ref


@pytest.mark.parametrize("name", [*sorted(GATES.values()), "publish.yml", DRY_RUN])
def test_checkouts_do_not_persist_credentials(name):
    lines = _read(name).splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^\s+- uses: actions/checkout@", line)]
    assert starts
    for start in starts:
        indent = len(lines[start]) - len(lines[start].lstrip())
        step = [lines[start]]
        for line in lines[start + 1:]:
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            step.append(line)
        assert "persist-credentials: false" in {line.strip() for line in step}, lines[start]


def test_publish_checkouts_keep_full_history():
    jobs = _jobs("publish.yml")
    for job in ("build", "verify"):
        assert "fetch-depth: 0" in jobs[job]
        assert "persist-credentials: false" in jobs[job]


def test_release_requirements_are_exact_and_hashed():
    text = RELEASE_REQUIREMENTS.read_text(encoding="utf-8")
    logical = re.sub(r"\\\n", " ", text)
    pins = {}
    for line in logical.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"([a-z0-9-]+)==([0-9][0-9A-Za-z.]*)((?:\s+--hash=sha256:[0-9a-f]{64})+)",
            line,
        )
        assert match, line
        assert match.group(1) not in pins, match.group(1)
        pins[match.group(1)] = match.group(2)
    assert {"build", "twine", "hatchling", "hatch-vcs"} <= set(pins)


def test_publish_verify_installs_and_runs_the_built_wheel():
    smoke = [step for step in _steps(_jobs("publish.yml")["verify"])
             if "Install the built wheel in a clean venv" in step]
    assert len(smoke) == 1
    step = smoke[0]
    assert 'python3 -m venv "$smoke/venv"' in step
    assert "--only-binary :all:" in step and "dist/*.whl" in step
    assert '"$smoke/venv/bin/tongs-agents" --help' in step
    assert "workspace.load_template()" in step
    assert "zellij_download.load_pins()" in step
    assert '"pi_pins.json"' in step
    assert "pi_extension_path()" in step
    assert '("tongs.plugins", "agents", "tongs_agents.tongs_plugin:FleetMonitorPlugin")' in step


@pytest.mark.parametrize("name", ["publish.yml", DRY_RUN])
def test_release_run_blocks_do_not_interpolate_expressions(name):
    blocks = _run_blocks(_read(name))
    assert blocks
    for block in blocks:
        assert "${{" not in block, block


def test_dry_run_triggers_on_dispatch_and_release_file_prs_only():
    triggers = _sections(_sections(_read(DRY_RUN), 0)["on"], 2)
    assert set(triggers) == {"workflow_dispatch", "pull_request"}
    pull_request = triggers["pull_request"]
    assert re.search(r"^\s+branches:\s*\n\s+- main$", pull_request, re.M)
    paths = set(re.findall(r'^\s+- "([^"]+)"$', pull_request, re.M))
    assert {
        ".github/release-requirements.txt",
        ".github/workflows/publish.yml",
        f".github/workflows/{DRY_RUN}",
        "pyproject.toml",
        "README.md",
        "src/tongs_agents/data/**",
        "src/tongs_agents/layouts/**",
        "src/tongs_agents/pi_extension/**",
    } <= paths
    dispatch = triggers["workflow_dispatch"]
    assert re.search(r'^\s+default: "\d+\.\d+\.\d+"$', dispatch, re.M)


def test_dry_run_cannot_publish():
    text = _read(DRY_RUN)
    top = _sections(text, 0)
    assert re.fullmatch(r"\s*contents: read\s*", top["permissions"])
    assert "id-token" not in text
    assert "environment:" not in text
    assert "secrets." not in text
    assert "gh-action-pypi-publish" not in text
    assert set(_jobs(DRY_RUN)) == {"build", "verify"}
    for job in _jobs(DRY_RUN).values():
        assert re.fullmatch(r"\s*contents: read\s*", _sections(job, 4)["permissions"])


def test_dry_run_build_runs_the_publish_build_steps_with_a_pretend_version():
    dry = _jobs(DRY_RUN)["build"]
    assert _steps(dry) == _steps(_jobs("publish.yml")["build"])
    env = _sections(dry, 4)["env"]
    assert re.fullmatch(
        rf"\s*SETUPTOOLS_SCM_PRETEND_VERSION_FOR_TONGS_AGENTS: {re.escape(DRY_RUN_VERSION)}\s*",
        env,
    )


def test_dry_run_verify_runs_the_publish_verify_steps_except_the_tag_check():
    publish = [step for step in _steps(_jobs("publish.yml")["verify"])
               if TAG_ON_MAIN_STEP not in step]
    dry = _jobs(DRY_RUN)["verify"]
    assert "build" in _sections(dry, 4)["needs"]
    tag_env = "TAG_NAME: ${{ github.ref_name }}"
    dry_tag_env = f"TAG_NAME: v{DRY_RUN_VERSION}"
    assert _steps(dry) == [step.replace(tag_env, dry_tag_env) for step in publish]
    assert dry_tag_env in dry
    assert TAG_ON_MAIN_STEP not in dry


def test_dry_run_actions_use_the_publish_pins():
    remote = [u for u in _uses(_read(DRY_RUN)) if not u.startswith("./")]
    assert remote
    publish = set(_uses(_read("publish.yml")))
    for ref in remote:
        assert FULL_SHA_PIN.match(ref), ref
        assert ref in publish, ref
