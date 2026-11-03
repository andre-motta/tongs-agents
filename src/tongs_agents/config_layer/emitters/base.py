"""The shape every emitter shares; subclasses implement ``plan_full`` and ``capture``."""

from __future__ import annotations

from typing import Any

from tongs_agents import owned
from tongs_agents.config_layer.emitters import _common
from tongs_agents.config_layer.protocol import CanonicalFragment, Change, ProfileTarget


class BaseEmitter:
    """Implements the protocol's ``plan`` on top of ``plan_full``.

    ``plan`` returns only file changes, as the protocol defines. The engine
    should call ``plan_full`` instead, which also returns the harness CLI
    steps, manifest entries, notes and conflicts, and apply it the way
    ``_common.apply_plan`` does.
    """

    harness_id = ""

    def plan(self, config: Any, target: ProfileTarget) -> list[Change]:
        return self.plan_full(config, target).changes

    def plan_commands(self, config: Any, target: ProfileTarget) -> list[_common.CommandStep]:
        return self.plan_full(config, target).commands

    def plan_full(
        self, config: Any, target: ProfileTarget, *, force: bool = False
    ) -> _common.EmitPlan:
        raise NotImplementedError

    def capture(
        self, target: ProfileTarget, *, include_unmanaged: bool = False
    ) -> CanonicalFragment:
        raise NotImplementedError

    def _start(self, config: Any, target: ProfileTarget) -> tuple[
        _common.EmitConfig, _common.EmitPlan, dict[str, dict[str, str]]
    ]:
        if target.harness != self.harness_id:
            raise ValueError(f"{self.harness_id} emitter got a {target.harness} target")
        canonical = _common.coerce_config(config)
        previous = owned.manifest_entries_for(target)
        plan = _common.EmitPlan(target=target, previous_entries=dict(previous))
        return canonical, plan, previous

    @staticmethod
    def _managed_names(previous: dict[str, dict[str, str]], kind: str) -> set[str]:
        return {
            fields["key"] for fields in previous.values()
            if fields.get("kind") == kind and fields.get("key")
        }
