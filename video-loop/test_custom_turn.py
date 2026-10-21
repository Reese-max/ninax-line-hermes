"""One regression check for the generic platform extension and ordinary fallback."""
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_custom_turn_boundary(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    try:
        from gateway.config import Platform
        from gateway.run_turn_runner import TurnRunner
    except ModuleNotFoundError as exc:
        if exc.name != 'gateway':raise
        pytest.skip('pinned Hermes gateway package is not installed')
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


@pytest.mark.parametrize('broken_module', ['config', 'run_turn_runner'])
def test_installed_gateway_import_failure_is_not_skipped(tmp_path, monkeypatch, broken_module):
    gateway = ModuleType('gateway')
    gateway.__path__ = [str(tmp_path)]
    monkeypatch.setitem(sys.modules, 'gateway', gateway)
    for name in ('gateway.config', 'gateway.run_turn_runner'):
        monkeypatch.setitem(sys.modules, name, None)
        del sys.modules[name]
    (tmp_path / 'config.py').write_text("class Platform:\n    LOCAL = 'local'\n", encoding='utf-8')
    (tmp_path / f'{broken_module}.py').write_text('import ninax_missing_gateway_dependency\n', encoding='utf-8')
    with pytest.raises(ModuleNotFoundError, match='ninax_missing_gateway_dependency'):
        try:
            test_custom_turn_boundary(tmp_path, monkeypatch)
        except pytest.skip.Exception:
            pytest.fail('an installed gateway import failure must fail instead of skipping')
