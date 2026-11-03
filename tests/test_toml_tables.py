"""toml_tables: the owned [mcp_servers.<name>] writer (strategy row 7, option C).

Synthetic: every document is a string built here.
"""

import tomllib

import pytest

from tongs_agents import toml_tables
from tongs_agents.toml_tables import TomlTableError, render_tables

USER = '''# A user's own comment at the top.
model = "example-model"  # inline user comment

# USER-COMMENT-ABOVE-MCP-TABLE
[mcp_servers.user-server]
# USER-COMMENT-INSIDE-MCP-TABLE
command = "user-mcp"  # USER-INLINE-COMMENT-IN-MCP-TABLE
args = ["--flag"]

[mcp_servers.user-server.env]
USER_TOKEN = "${USER_TOKEN}"

[profiles.review]
model = "other"
'''

OURS = {"command": "/opt/py", "args": ["-m", "tongs_agents.mcp_server"],
        "env_vars": ["XDG_STATE_HOME"]}


def test_adding_a_table_keeps_every_user_byte():
    result = render_tables(USER, {"tongs-agents": OURS})
    assert result.startswith(USER)
    assert result[len(USER):] == (
        '\n[mcp_servers.tongs-agents]\ncommand = "/opt/py"\n'
        'args = ["-m", "tongs_agents.mcp_server"]\nenv_vars = ["XDG_STATE_HOME"]\n'
    )
    assert tomllib.loads(result)["mcp_servers"]["tongs-agents"] == OURS


def test_replace_and_remove_round_trip_to_the_original():
    added = render_tables(USER, {"tongs-agents": OURS})
    changed = render_tables(added, {"tongs-agents": {**OURS, "args": ["-m", "x"]}})
    assert changed.startswith(USER)
    assert 'args = ["-m", "x"]' in changed
    assert render_tables(changed, {"tongs-agents": None}) == USER


def test_rendering_the_same_value_is_byte_stable():
    added = render_tables(USER, {"tongs-agents": OURS})
    assert render_tables(added, {"tongs-agents": OURS}) == added


def test_replacing_a_middle_table_keeps_comments_of_the_next_header():
    text = (
        '[mcp_servers.ours]\ncommand = "a"\n\n'
        '# above theirs\n[mcp_servers.theirs]\ncommand = "b"\n'
    )
    result = render_tables(text, {"ours": {"command": "c"}})
    assert result == (
        '[mcp_servers.ours]\ncommand = "c"\n\n'
        '# above theirs\n[mcp_servers.theirs]\ncommand = "b"\n'
    )
    assert render_tables(text, {"ours": None}) == (
        '# above theirs\n[mcp_servers.theirs]\ncommand = "b"\n'
    )


def test_our_sub_tables_are_removed_wherever_they_sit():
    text = (
        '[mcp_servers.ours]\ncommand = "a"\n\n[other]\nx = 1\n\n'
        '[mcp_servers.ours.env]\nTOKEN = "literal"\n'
    )
    result = render_tables(text, {"ours": {"command": "a"}})
    assert "[mcp_servers.ours.env]" not in result
    assert tomllib.loads(result) == {"mcp_servers": {"ours": {"command": "a"}},
                                     "other": {"x": 1}}


def test_headers_inside_multiline_strings_are_not_tables():
    text = (
        '[mcp_servers.theirs]\ncommand = "b"\nnote = """\n[mcp_servers.ours]\n"""\n'
        "arr = [\n  [1, 2],\n]\n"
    )
    result = render_tables(text, {"ours": {"command": "a"}})
    assert result.startswith(text)
    assert tomllib.loads(result)["mcp_servers"]["theirs"]["note"] == "[mcp_servers.ours]\n"


@pytest.mark.parametrize("text", [
    'mcp_servers = { ours = { command = "x" } }\n',
    '[mcp_servers]\nours.command = "x"\n',
    '[mcp_servers]\nours = { command = "x" }\n',
])
def test_inline_or_dotted_definitions_are_refused(text):
    with pytest.raises(TomlTableError, match="inline or with dotted keys"):
        render_tables(text, {"ours": {"command": "y"}})


def test_an_edit_the_parser_rejects_is_refused():
    with pytest.raises(TomlTableError, match="nothing written"):
        render_tables('mcp_servers = { theirs = { command = "x" } }\n',
                      {"ours": {"command": "y"}})


@pytest.mark.parametrize("value", [
    {"command": "a\nb"},
    {"command": "a\x1b"},
    {"args": ["ok", "bad\x00"]},
    {"timeout": 1.5},
    {"env": {"A": "b"}},
])
def test_unsupported_values_are_refused(value):
    with pytest.raises(TomlTableError):
        render_tables("", {"ours": value})


def test_quoted_names_and_values_are_escaped():
    result = render_tables("", {"odd.name": {"command": 'say "hi" \\ there'}})
    assert result.startswith('[mcp_servers."odd.name"]\n')
    assert toml_tables.read_tables(result) == {"odd.name": {"command": 'say "hi" \\ there'}}


def test_quoted_header_segments_are_located():
    text = '[mcp_servers."ours"]\ncommand = "a"\n'
    assert render_tables(text, {"ours": None}) == ""


def test_invalid_toml_is_refused():
    with pytest.raises(TomlTableError, match="invalid TOML"):
        render_tables("[unterminated\n", {"ours": {"command": "a"}})


def test_array_of_tables_is_refused():
    with pytest.raises(TomlTableError, match="array of tables"):
        render_tables('[[mcp_servers.ours]]\ncommand = "a"\n', {"ours": None})


def test_digest_ignores_formatting_and_key_order():
    first = toml_tables.read_tables('[mcp_servers.a]\ncommand = "x"\nargs = ["1"]\n')["a"]
    second = toml_tables.read_tables(
        '[mcp_servers.a]\n# comment\nargs = [ "1" ]\ncommand="x"\n')["a"]
    assert toml_tables.table_digest(first) == toml_tables.table_digest(second)
