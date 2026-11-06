"""公开源码不固定个人路径；可信本地目录仍有边界且构建不丢配置。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.guide import manage, windows


@pytest.fixture
def runtime_config(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    (workspace / '.local').mkdir(parents=True)
    directory = tmp_path / 'external-runtime'
    config_path = workspace / '.local' / 'guide-runtime.json'
    config = {'location': 'ntfs', 'ntfs_runtime': str(directory),
        'private_note': 'synthetic-preserved-setting'}
    config_path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(windows, 'ROOT', workspace)
    monkeypatch.setattr(manage, 'ROOT', workspace)
    return workspace, directory, config_path, config


def test_local_config_selects_same_external_directory_for_both_shells(runtime_config):
    _, directory, _, _ = runtime_config
    directory.mkdir()
    for name in ('shell.exe', 'explore-shell.exe'):
        (directory / name).write_bytes(b'synthetic-not-executable')
    assert windows.ntfs_runtime() == directory
    assert windows.executable() == directory / 'shell.exe'
    assert windows.exploration_executable() == directory / 'explore-shell.exe'


@pytest.mark.parametrize('bad', ['relative', 'workspace', 'ancestor', 'root', 'parent_segment', 'empty', 'type', 'null'])
def test_local_config_rejects_unsafe_runtime_directory(runtime_config, bad):
    workspace, directory, config_path, config = runtime_config
    choices = {'relative': 'relative-runtime', 'workspace': str(workspace / '.local' / 'runtime'),
        'ancestor': str(workspace.parent), 'root': workspace.anchor,
        'parent_segment': str(directory / '..' / 'runtime'), 'empty': '', 'type': [], 'null': None}
    config['ntfs_runtime'] = choices[bad]
    config_path.write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(windows.IsolationUnavailable):
        windows.ntfs_runtime()


@pytest.mark.parametrize('source', ['local_app_data', 'home'])
def test_missing_local_override_uses_user_derived_default(runtime_config, monkeypatch, source):
    workspace, _, config_path, _ = runtime_config
    config_path.unlink()
    user_base = workspace.parent / 'synthetic-user'
    if source == 'local_app_data':
        monkeypatch.setenv('LOCALAPPDATA', str(user_base))
        expected = user_base / 'LLM-runtime'
    else:
        monkeypatch.delenv('LOCALAPPDATA', raising=False)
        monkeypatch.setattr(Path, 'home', classmethod(lambda cls: user_base))
        expected = user_base / 'AppData' / 'Local' / 'LLM-runtime'
    assert windows.ntfs_runtime() == expected


@pytest.mark.parametrize('location', ['project', 'ntfs'])
def test_build_preserves_private_runtime_configuration(runtime_config, monkeypatch, location):
    _, directory, config_path, before = runtime_config
    calls = []
    monkeypatch.setattr(manage.shutil, 'which', lambda _: str(directory.parent / 'compiler' / 'gcc.exe'))
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(manage.subprocess, 'run', run)
    manage.build(location)
    after = json.loads(config_path.read_text(encoding='utf-8'))
    assert after == {**before, 'location': location}
    assert len(calls) == 1


@pytest.mark.parametrize('system_root', [None, 'relative-os-directory'])
def test_child_without_absolute_system_root_fails_closed(monkeypatch, system_root):
    if system_root is None:
        monkeypatch.delenv('SystemRoot', raising=False)
    else:
        monkeypatch.setenv('SystemRoot', system_root)
    with pytest.raises(windows.IsolationUnavailable):
        windows._child_environment()


def test_child_environment_uses_system_root_without_inheriting_business_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'synthetic-os'))
    monkeypatch.setenv('BUSINESS_DATABASE_URL', 'synthetic-private-value')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-private-value')
    result = windows._child_environment()
    assert result['windir'] == result['SystemRoot']
    assert 'BUSINESS_DATABASE_URL' not in result and 'DEEPSEEK_API_KEY' not in result
