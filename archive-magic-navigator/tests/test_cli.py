import json
import pytest
from archive_magic_navigator import cli


def test_parse_args_defaults_and_modes(tmp_path):
    request = cli.parse_args(['--catalog', str(tmp_path / 'catalog.json')])
    assert request.port == 8080
    assert request.bind == '127.0.0.1'
    assert request.poll_interval_seconds == 300
    assert request.wayback_fallback
    assert cli.parse_args(['--catalog', 'x.json', '--wayback-fallback', 'off']).wayback_fallback is False


@pytest.mark.parametrize('args', [[], ['archive'], ['archive', '--catalog', 'x.json'], ['--catalog', 'x', '--port', '0'], ['--catalog', 'x', '--poll-interval', 'nan'], ['--catalog', 'x', '--poll-interval', '0']])
def test_invalid_arguments(args):
    with pytest.raises(SystemExit) as error:
        cli.parse_args(args)
    assert error.value.code == 2


def test_legacy_migration_message(tmp_path, capsys):
    assert cli.main(['--catalog', str(tmp_path)]) == 1
    assert 'migrate navigator.toml' in capsys.readouterr().err


def test_empty_catalog_starts_and_opens_only_after_ready(tmp_path, monkeypatch):
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps({'archives': []}))
    events = []
    def run(runtime, bind, port, **kwargs):
        assert json.loads((runtime / 'catalog-state.json').read_text())['archives'] == []
        events.append('ready')
        kwargs['on_ready']('http://localhost:8080/')
        return 0
    monkeypatch.setattr(cli, 'run_wayback', run)
    monkeypatch.setattr(cli.webbrowser, 'open', lambda _: events.append('open'))
    assert cli.main(['--catalog', str(path), '--open']) == 0
    assert events == ['ready', 'open']


def test_help(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(['--help'])
    output = capsys.readouterr().out
    assert '--catalog PATH' in output
    assert 'positional arguments' not in output
