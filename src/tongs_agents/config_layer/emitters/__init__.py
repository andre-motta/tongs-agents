"""Per-harness emitters; ``EMITTERS`` maps each harness id to its emitter.

See docs/work/emitter-strategy.md for what each emitter writes. ``plan`` is
the protocol's file-only view; ``plan_full`` also returns the harness CLI
steps (Claude and Pi MCP servers, the Pi extension), manifest entries,
notes and conflicts, applied as ``apply_plan`` shows.
"""

from tongs_agents.config_layer.emitters._common import (
    CommandStep,
    EmitConfig,
    EmitError,
    EmitPlan,
    apply_plan,
)
from tongs_agents.config_layer.emitters.claude import ClaudeEmitter
from tongs_agents.config_layer.emitters.codex import CodexEmitter
from tongs_agents.config_layer.emitters.pi import PiEmitter

EMITTERS = {
    "claude": ClaudeEmitter(),
    "codex": CodexEmitter(),
    "pi": PiEmitter(),
}

__all__ = [
    "EMITTERS",
    "ClaudeEmitter",
    "CodexEmitter",
    "CommandStep",
    "EmitConfig",
    "EmitError",
    "EmitPlan",
    "PiEmitter",
    "apply_plan",
]
