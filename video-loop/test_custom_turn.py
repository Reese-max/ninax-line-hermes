"""One regression check for the generic platform extension and ordinary fallback."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_custom_turn_boundary(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    result={'final_response':'reviewed'}
    class Adapter:
        def run_custom_turn(self,ctx):return result
    runner=MagicMock()
    runner._adapter_for_source.return_value=Adapter()
    turn=TurnRunner(runner,SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL)))
    assert turn.run_sync() is result
    runner._resolve_session_agent_runtime.assert_not_called()
    result='invalid result'
    with pytest.raises(TypeError):turn.run_sync()
    class OrdinaryReached(Exception):pass
    def ordinary():raise OrdinaryReached()
    turn._combined_ephemeral_prompt=ordinary
    result=None
    with pytest.raises(OrdinaryReached):turn.run_sync()
    runner._adapter_for_source.return_value=MagicMock()
    with pytest.raises(OrdinaryReached):turn.run_sync()
