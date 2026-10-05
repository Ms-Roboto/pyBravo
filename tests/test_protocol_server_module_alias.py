"""The module launcher and Protocol Assistant must share one active Bravo."""

import os
import subprocess
import sys


def test_module_launcher_shares_active_bravo_with_protocol_assistant(tmp_path):
    script = """
import runpy
import uvicorn

def capture(app, **kwargs):
    from pybravo.web import server
    from pybravo.workflow.protocols import api
    assert app is server.app
    assert server._bravo is not None
    assert api._bravo() is server._bravo
    print('shared-active-bravo')

uvicorn.run = capture
runpy.run_module('pybravo.web.server', run_name='__main__', alter_sys=True)
"""
    env = dict(os.environ, PYBRAVO_PROFILE_DIR=str(tmp_path))
    completed = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True,
        text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "shared-active-bravo" in completed.stdout
