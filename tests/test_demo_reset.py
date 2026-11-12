from unittest.mock import Mock
import pytest


def test_reset_requires_explicit_confirmation_without_touching_docker():
    from helio_security.demo_reset import reset
    runner=Mock()
    with pytest.raises(ValueError): reset('',runner=runner)
    runner.assert_not_called()


def test_reset_stops_before_removing_data_then_starts_only_unpaid_services():
    from helio_security.demo_reset import reset
    runner=Mock()
    reset('RESET DEMO',runner=runner)
    commands=[call.args[0] for call in runner.call_args_list]
    assert commands[0]==['docker','compose','down','-v']
    assert 'awasi-agent' not in commands[1]
    assert commands[1][:6]==['docker','compose','up','--build','-d','--wait']
    assert {'simulator','crm','workbench','outside','neo4j','security-api','dashboard'} <= set(commands[1])
    assert all(call.kwargs['check'] for call in runner.call_args_list)


def test_failed_stop_does_not_restart():
    from helio_security.demo_reset import reset
    runner=Mock(side_effect=RuntimeError('stop failed'))
    with pytest.raises(RuntimeError): reset('RESET DEMO',runner=runner)
    assert runner.call_count==1
