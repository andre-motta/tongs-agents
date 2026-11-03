"""``tongs-agents config``: emit the dotfiles config into harness homes.

- ``config status`` reports every managed entry and exits 3 when any entry
  is not in sync (0 when clean, 1 on an error).
- ``config apply [--dry-run] [--force]`` writes pending entries as one
  rollback-able change set, then runs the harness CLI steps (MCP servers
  Claude Code and Pi keep themselves, the Pi extension) one target at a time;
  ``--dry-run`` prints a unified diff and the steps instead. A path or entry
  without the tongs-agents marker refuses the whole apply (exit 1). A
  drifted entry is left alone (exit 3) unless ``--force`` overwrites it after
  a backup. A failed CLI step exits 1 and is retried by the next apply. A
  second apply with nothing changed writes nothing.
- ``config capture HARNESS --into DIR [--force]`` reads that harness's side
  of every target and writes a dotfiles directory into DIR only. It never
  changes harness files and refuses a non-empty DIR without ``--force``.

The dotfiles directory comes from ``--source`` or ``[dotfiles] path`` in the
tongs-agents config file (docs/config.md).
"""

from __future__ import annotations

import json
import shlex
import sys

from tongs_agents.commands.doctor import Check
from tongs_agents.config_layer import engine, schema
from tongs_agents.profiles import ProfileError, load_profiles, profiles_path

NAME = "config"
HELP = "Emit the dotfiles config into harness homes (status, apply, capture)"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DRIFT = 3
JSON_SCHEMA = 1
_ERRORS = (schema.SchemaError, engine.EngineError, ProfileError, OSError, ValueError)


def _add_source(parser) -> None:
    parser.add_argument(
        "--source", metavar="DIR",
        help="The dotfiles directory (default: [dotfiles] path in the config file)",
    )


def add_arguments(parser):
    actions = parser.add_subparsers(dest="config_action", metavar="ACTION")
    actions.required = True
    status = actions.add_parser("status", help="Report each managed entry; exit 3 on drift")
    _add_source(status)
    status.add_argument("--json", action="store_true", help="Print the entries as JSON")
    apply = actions.add_parser("apply", help="Write pending entries into every target")
    _add_source(apply)
    apply.add_argument(
        "--dry-run", action="store_true", help="Print a unified diff and write nothing",
    )
    apply.add_argument(
        "--force", action="store_true",
        help="Overwrite drifted entries after a backup next to each file",
    )
    capture = actions.add_parser(
        "capture", help="Read one harness's managed entries into a dotfiles directory",
    )
    capture.add_argument("harness", choices=schema.EMITTED_HARNESSES)
    capture.add_argument("--into", metavar="DIR", required=True, help="Directory to write")
    capture.add_argument("--force", action="store_true", help="Write into a non-empty DIR")
    _add_source(capture)


def _print_entries(plan: engine.Plan) -> None:
    current = None
    for entry in plan.entries:
        heading = entry.target.describe()
        if heading != current:
            print(heading)
            current = heading
        detail = f"  ({entry.detail})" if entry.detail else ""
        print(f"  {entry.state:<9}  {entry.label}{detail}")


def _print_notes(plan: engine.Plan) -> None:
    for note in plan.notes:
        print(f"note: {note}")


def _step_text(step) -> str:
    argv = getattr(step, "argv", ())
    return f"{step.description}: {shlex.join(argv)}" if argv else step.description


def _print_problems(plan: engine.Plan) -> None:
    for problem in plan.problems:
        print(f"tongs-agents config: {problem}", file=sys.stderr)


def _no_emitters(plan: engine.Plan) -> bool:
    if plan.registry.emitters or plan.problems:
        return False
    print("No config emitters are installed; nothing is emitted.")
    return True


def _entry_json(entry: engine.Entry) -> dict:
    target = entry.target.target
    return {
        "harness": target.harness,
        "config_dir": None if target.config_dir is None else str(target.config_dir),
        "profiles": list(entry.target.profiles),
        "path": str(entry.path),
        "key": entry.key,
        "state": entry.state,
        "emitter": entry.emitter,
        "detail": entry.detail,
    }


def _status(args) -> int:
    config = schema.load_canonical(args.source)
    plan = engine.plan(config)
    code = EXIT_ERROR if plan.problems else (EXIT_OK if plan.clean else EXIT_DRIFT)
    if args.json:
        print(json.dumps({
            "schema_version": JSON_SCHEMA,
            "source": str(config.source),
            "clean": plan.clean,
            "problems": list(plan.problems),
            "notes": list(plan.notes),
            "steps": [f"{target.describe()}: {_step_text(step)}" for target, step in plan.steps],
            "entries": [_entry_json(entry) for entry in plan.entries],
        }, indent=2))
        return code
    _print_problems(plan)
    if _no_emitters(plan):
        return code
    _print_entries(plan)
    _print_notes(plan)
    if plan.clean:
        print("Every managed entry is in sync.")
    elif not plan.problems:
        print("Some entries are not in sync; run 'tongs-agents config apply --dry-run' to see why.")
    return code


def _print_conflicts(plan: engine.Plan) -> None:
    for entry in plan.foreign:
        print(f"tongs-agents config: {entry.label}: {entry.detail}", file=sys.stderr)
    if plan.foreign:
        print("tongs-agents config: refusing to apply; nothing was written", file=sys.stderr)


def _print_skipped(skipped) -> None:
    for entry in skipped:
        print(f"tongs-agents config: skipped {entry.label}: {entry.detail}", file=sys.stderr)


def _apply(args) -> int:
    config = schema.load_canonical(args.source)
    plan = engine.plan(config, force=args.force)
    _print_problems(plan)
    if plan.problems:
        return EXIT_ERROR
    if _no_emitters(plan):
        return EXIT_OK
    if args.dry_run:
        for entry in plan.entries:
            if entry.writes:
                sys.stdout.write(engine.unified_diff(entry))
        _print_entries(plan)
        for target, step in plan.steps:
            print(f"would run  {target.describe()}: {_step_text(step)}")
        _print_notes(plan)
        _print_conflicts(plan)
        _print_skipped(plan.skipped)
        if plan.foreign:
            return EXIT_ERROR
        return EXIT_DRIFT if plan.skipped else EXIT_OK
    if plan.foreign:
        _print_notes(plan)
        _print_conflicts(plan)
        return EXIT_ERROR
    result = engine.apply(plan)
    for change in result.written:
        print(f"{change.kind:<10} {change.path}")
    for step in result.steps:
        if step.ok:
            print(f"{'ran':<10} {step.target.describe()}: {step.description}")
    if not result.written and not result.steps:
        print("Nothing to change.")
    _print_notes(plan)
    _print_skipped(result.skipped)
    for step in result.failed:
        detail = f": {step.detail}" if step.detail else ""
        print(f"tongs-agents config: failed {step.target.describe()}: {step.description}{detail}; "
              "the files above were kept and the next apply retries it", file=sys.stderr)
    if result.failed:
        return EXIT_ERROR
    return EXIT_DRIFT if result.skipped else EXIT_OK


def _capture_profiles(source):
    if source is not None:
        return schema.load_canonical(source).profiles
    try:
        return schema.load_canonical().profiles
    except schema.NoSourceError:
        pass
    except schema.SchemaError as error:
        print(f"tongs-agents config: {error}; using the profiles in {profiles_path()}",
              file=sys.stderr)
    return load_profiles()


def _capture(args) -> int:
    targets = engine.resolve_targets(_capture_profiles(args.source))
    captured = engine.capture(args.harness, targets)
    for warning in captured.warnings:
        print(f"tongs-agents config: {warning}", file=sys.stderr)
    for path in engine.write_capture(args.harness, captured, args.into, force=args.force):
        print(f"wrote {path}")
    if captured.empty:
        print(f"Nothing managed was found for {args.harness}; wrote an empty agentic.toml.")
    return EXIT_OK


def run(args) -> int:
    handlers = {"status": _status, "apply": _apply, "capture": _capture}
    try:
        return handlers[args.config_action](args)
    except _ERRORS as error:
        print(f"tongs-agents config: {error}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as error:
        # A bug, not a user error: one line and a failing status, never a traceback.
        print(f"tongs-agents config: internal error: {type(error).__name__}: {error}",
              file=sys.stderr)
        return EXIT_ERROR


def doctor_checks():
    """Config drift between the dotfiles directory and the harness homes."""
    fix = "Run 'tongs-agents config status' for details"
    try:
        source = schema.dotfiles_path()
        if source is None:
            return []
        plan = engine.plan(schema.load_canonical(source))
    except _ERRORS as error:
        return [Check("config", "error", f"Cannot check config drift: {error}", fix)]
    if plan.problems:
        return [Check("config", "error", problem, fix) for problem in plan.problems]
    if not plan.registry.emitters:
        return [Check("config", "info", "No config emitters are installed; nothing is emitted")]
    checks = []
    foreign = plan.foreign
    if foreign:
        checks.append(Check(
            "config", "warn",
            f"{len(foreign)} path(s) exist without the tongs-agents marker; apply refuses them",
            "Move them aside or remove them from the dotfiles, then run 'tongs-agents config apply'",
            tuple(entry.label for entry in foreign),
        ))
    drifted = [entry for entry in plan.entries if entry.state == engine.DRIFTED]
    if drifted:
        checks.append(Check(
            "config", "warn",
            f"{len(drifted)} managed path(s) were edited outside tongs-agents",
            "Keep the edits with 'tongs-agents config capture', or overwrite them with "
            "'tongs-agents config apply --force'",
            tuple(entry.label for entry in drifted),
        ))
    behind = [
        entry for entry in plan.entries
        if entry.state in (engine.PENDING, engine.MISSING, engine.ORPHANED)
    ]
    if behind:
        checks.append(Check(
            "config", "info",
            f"{len(behind)} managed path(s) are behind the dotfiles",
            "Run 'tongs-agents config apply'",
            tuple(f"{entry.state}: {entry.label}" for entry in behind),
        ))
    if not checks:
        checks.append(Check("config", "ok", f"Config from {schema.resolve_source(source)} is in sync"))
    return checks
