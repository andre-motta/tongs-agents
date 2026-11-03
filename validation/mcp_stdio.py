"""Run a synthetic MCP stdio smoke with the current Python environment.

The server runs twice, with [control].mcp_enabled false and then true, in a
temporary state and config dir: the agent control tools must be absent, then
listed, and a control call from outside a launched pane must be refused and
audited. No agent, multiplexer or model is started.
"""

import argparse
import json
import os
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FLEET_TOOLS = {
    'fleet_status', 'fleet_session', 'fleet_sessions_needing_attention',
    'fleet_focus', 'fleet_cleanup',
}
CONTROL_TOOLS = {'agents_spawn', 'agents_send', 'agents_status', 'agents_wait'}


def run_server(source, control_enabled):
    with tempfile.TemporaryDirectory(prefix='fleet-mcp-validation-') as directory:
        sessions = Path(directory) / 'sessions'
        sessions.mkdir(mode=0o700)
        config = Path(directory) / 'config'
        config.mkdir(mode=0o700)
        (config / 'config.toml').write_text(
            f'[control]\nmcp_enabled = {"true" if control_enabled else "false"}\n'
        )
        (config / 'config.toml').chmod(0o600)
        env = dict(
            os.environ, TONGS_AGENTS_STATE_DIR=directory, TONGS_AGENTS_CONFIG_DIR=str(config),
            XDG_RUNTIME_DIR=str(Path(directory) / 'run'),
            PYTHONPATH=str(source.resolve()), PYTHONUNBUFFERED='1',
        )
        for key in ('TONGS_AGENTS_LAUNCH_ID', 'ZELLIJ', 'ZELLIJ_SESSION_NAME', 'ZELLIJ_PANE_ID',
                    'ZELLIJ_SOCKET_DIR', 'TMUX', 'TMUX_PANE'):
            env.pop(key, None)
        fixture = {
            'session_id': 'validation-session', 'agent': 'claude',
            'repo': 'validation-fixture', 'cwd': directory, 'pid': '',
            'status': 'waiting', 'ts': int(time.time()),
        }
        (sessions / 'fixture.json').write_text(json.dumps(fixture))
        process = subprocess.Popen(
            [sys.executable, '-u', '-m', 'tongs_agents.mcp_server'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, bufsize=0,
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        buffered = bytearray()
        errors = bytearray()

        def send(message):
            process.stdin.write((json.dumps(message) + '\n').encode())
            process.stdin.flush()

        def response(request_id):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                for key, _ in selector.select(timeout=0.5):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is process.stderr:
                        errors.extend(chunk)
                        continue
                    buffered.extend(chunk)
                    while b'\n' in buffered:
                        line, _, rest = buffered.partition(b'\n')
                        buffered[:] = rest
                        message = json.loads(line)
                        if message.get('id') == request_id:
                            if 'error' in message:
                                raise RuntimeError(message['error'])
                            return message['result']
            raise TimeoutError(f'MCP request {request_id} timed out; process exit: {process.poll()}; stderr: {errors.decode(errors="replace")[-1000:]}')

        try:
            send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2024-11-05', 'capabilities': {},
                'clientInfo': {'name': 'fleet-validation', 'version': '1'},
            }})
            initialized = response(1)
            assert initialized['serverInfo']['name'] == 'tongs-agents'
            send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
            listing = response(2)
            expected = FLEET_TOOLS | (CONTROL_TOOLS if control_enabled else set())
            names = {tool['name'] for tool in listing['tools']}
            assert names == expected, sorted(names)
            send({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                'name': 'fleet_status', 'arguments': {},
            }})
            result = response(3)
            assert not result.get('isError')
            status = json.loads(result['content'][0]['text'])
            assert any(session['session_id'] == 'validation-session' and session['status'] == 'waiting' for session in status['sessions'])
            if control_enabled:
                send({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {
                    'name': 'agents_send', 'arguments': {'launch_id': 'a' * 32, 'text': 'hi'},
                }})
                refused = json.loads(response(4)['content'][0]['text'])
                assert (refused['ok'], refused['outcome'], refused['exit_code']) == (False, 'refused', 5), refused
                audit = Path(directory) / 'audit' / 'control.jsonl'
                assert stat.S_IMODE(audit.stat().st_mode) == 0o600
                entry = json.loads(audit.read_text().splitlines()[-1])
                assert (entry['actor'], entry['action'], entry['outcome']) == ('mcp', 'send', 'refused')
                print('PASS: control enabled: nine tools; an agents_send outside a launched pane is refused and audited (0600)')
            else:
                print('PASS: control disabled: subprocess stdio initialization, five fleet tools only and synthetic fleet status')
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            selector.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1] / 'src')
    args = parser.parse_args()
    run_server(args.source, control_enabled=False)
    run_server(args.source, control_enabled=True)


if __name__ == '__main__':
    main()
