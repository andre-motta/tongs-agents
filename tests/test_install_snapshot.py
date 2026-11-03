"""Byte-for-byte install output, pinned from the installer before owned.py.

Synthetic: HOME, the state and config dirs are temporary; the harness CLIs
are absent, so only settings.json and hooks.json are written.
"""

import json
import sys
from types import SimpleNamespace

import pytest

from tongs_agents import cli, legacy, paths

HOOK = "/opt/tongs agents/bin/tongs-agents-hook"
LEGACY_HOOK = legacy.LEGACY_HOOK_COMMANDS[0]
LEGACY_MCP_MODULE = legacy.LEGACY_MCP_MODULES[0]

SETTINGS_INPUT = json.dumps({
    "env": {"EXAMPLE": "1"},
    "greeting": "olá",
    "hooks": {
        "SessionStart": [
            {"hooks": [{"type": "command", "command": f"{LEGACY_HOOK} session-start",
                        "timeout": 5}]},
            {"hooks": [
                {"type": "command", "command": "say hello"},
                {"type": "command",
                 "command": "/old/venv/bin/tongs-agents-hook session-start --agent claude",
                 "timeout": 5},
            ]},
        ],
        "Notification": [
            {"matcher": "", "hooks": [{"type": "command", "command": "notify-send hi"}]},
        ],
        "PreCompact": [
            {"hooks": [{"type": "command",
                        "command": "tongs-agents-hook pre-compact --agent claude"}]},
        ],
    },
    "mcpServers": {
        "fleet": {"command": "/usr/bin/python3",
                  "args": ["-m", LEGACY_MCP_MODULE]},
        "docs": {"command": "docs-mcp"},
    },
    "permissions": {"allow": ["Bash(ls:*)"]},
}, indent=4, ensure_ascii=False)

CODEX_INPUT = '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}'


def _environment(root, monkeypatch):
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(root / "state"))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(root / "config"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / ".pi" / "agent"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(cli, "CLAUDE_DIR", home / ".claude")
    monkeypatch.setattr(cli, "SETTINGS_FILE", home / ".claude" / "settings.json")
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", home / ".claude.json")
    monkeypatch.setattr(cli, "CODEX_DIR", home / ".codex")
    monkeypatch.setattr(cli, "CODEX_HOOKS_FILE", home / ".codex" / "hooks.json")
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: HOOK if name == "tongs-agents-hook" else None
    )
    return home


def _seed(directory, settings=SETTINGS_INPUT, codex=CODEX_INPUT):
    (directory / "claude").mkdir(parents=True)
    (directory / "codex").mkdir(parents=True)
    (directory / "claude" / "settings.json").write_text(settings, encoding="utf-8")
    (directory / "codex" / "hooks.json").write_text(codex, encoding="utf-8")


def _install(**options):
    values = {"agent": None, "claude_config_dirs": None, "codex_homes": None,
              "pi_agent_dirs": None, "keep_data": False}
    values.update(options)
    cli.cmd_install(SimpleNamespace(**values))


def _uninstall(**options):
    values = {"agent": None, "claude_config_dirs": None, "codex_homes": None,
              "pi_agent_dirs": None, "keep_data": True}
    values.update(options)
    cli.cmd_uninstall(SimpleNamespace(**values))


def run_scenario(name, root, monkeypatch):
    """Run one installer scenario under ``root``; return {label: bytes}."""
    home = _environment(root, monkeypatch)
    defaults = {"settings": cli.SETTINGS_FILE, "hooks": cli.CODEX_HOOKS_FILE}
    if name == "fresh":
        _install()
        files = defaults
    elif name in ("existing", "reinstall", "uninstall"):
        cli.SETTINGS_FILE.parent.mkdir(parents=True)
        cli.CODEX_HOOKS_FILE.parent.mkdir(parents=True)
        cli.SETTINGS_FILE.write_text(SETTINGS_INPUT, encoding="utf-8")
        cli.CODEX_HOOKS_FILE.write_text(CODEX_INPUT, encoding="utf-8")
        _install()
        if name == "reinstall":
            _install()
        if name == "uninstall":
            _uninstall()
        files = defaults
    elif name == "config_dirs":
        homes = home / "profiles" / "work"
        _seed(homes)
        _install(claude_config_dirs=[str(homes / "claude")],
                 codex_homes=[str(homes / "codex")])
        files = {"settings": homes / "claude" / "settings.json",
                 "hooks": homes / "codex" / "hooks.json"}
    else:
        raise ValueError(name)
    return {label: path.read_bytes() for label, path in files.items()}


SCENARIOS = ("fresh", "existing", "reinstall", "uninstall", "config_dirs")

FRESH_SETTINGS = r'''{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' session-start --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' prompt-submit --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' tool-use --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' stop --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "StopFailure": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' stop-failure --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' session-end --agent claude",
            "timeout": 3
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' permission-request --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "Elicitation": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' elicitation --agent claude",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
'''.encode()

FRESH_HOOKS = r'''{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' session-start --agent codex",
            "timeout": 5
          }
        ],
        "matcher": "startup|resume|clear"
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' prompt-submit --agent codex",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' tool-use --agent codex",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' stop --agent codex",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' session-end --agent codex",
            "timeout": 3
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' permission-request --agent codex",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
'''.encode()

EXISTING_SETTINGS = r'''{
  "env": {
    "EXAMPLE": "1"
  },
  "greeting": "ol\u00e1",
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "say hello"
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' session-start --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "notify-send hi"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' prompt-submit --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' tool-use --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' stop --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "StopFailure": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' stop-failure --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' session-end --agent claude",
            "timeout": 3
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' permission-request --agent claude",
            "timeout": 5
          }
        ]
      }
    ],
    "Elicitation": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' elicitation --agent claude",
            "timeout": 5
          }
        ]
      }
    ]
  },
  "mcpServers": {
    "docs": {
      "command": "docs-mcp"
    }
  },
  "permissions": {
    "allow": [
      "Bash(ls:*)"
    ]
  }
}
'''.encode()

EXISTING_HOOKS = r'''{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "say done"
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' stop --agent codex",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' session-start --agent codex",
            "timeout": 5
          }
        ],
        "matcher": "startup|resume|clear"
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' prompt-submit --agent codex",
            "timeout": 5
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' tool-use --agent codex",
            "timeout": 5
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' session-end --agent codex",
            "timeout": 3
          }
        ]
      }
    ],
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "'/opt/tongs agents/bin/tongs-agents-hook' permission-request --agent codex",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
'''.encode()

UNINSTALL_SETTINGS = r'''{
  "env": {
    "EXAMPLE": "1"
  },
  "greeting": "ol\u00e1",
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "say hello"
          }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "notify-send hi"
          }
        ]
      }
    ]
  },
  "mcpServers": {
    "docs": {
      "command": "docs-mcp"
    }
  },
  "permissions": {
    "allow": [
      "Bash(ls:*)"
    ]
  }
}
'''.encode()

UNINSTALL_HOOKS = r'''{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "say done"
          }
        ]
      }
    ]
  }
}
'''.encode()

EXPECTED = {
    "fresh": {"settings": FRESH_SETTINGS, "hooks": FRESH_HOOKS},
    "existing": {"settings": EXISTING_SETTINGS, "hooks": EXISTING_HOOKS},
    "reinstall": {"settings": EXISTING_SETTINGS, "hooks": EXISTING_HOOKS},
    "uninstall": {"settings": UNINSTALL_SETTINGS, "hooks": UNINSTALL_HOOKS},
    "config_dirs": {"settings": EXISTING_SETTINGS, "hooks": EXISTING_HOOKS},
}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hook command quoting")
@pytest.mark.parametrize("name", SCENARIOS)
def test_install_output_is_byte_identical(name, tmp_path, monkeypatch):
    result = run_scenario(name, tmp_path, monkeypatch)
    assert result == EXPECTED[name]
