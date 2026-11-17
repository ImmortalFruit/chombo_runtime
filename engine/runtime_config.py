"""Strict, startup-only runtime configuration; no secret configuration keys."""
from pathlib import Path
import json
import os
import sys


class ConfigError(ValueError):
    pass


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError('duplicate key')
        result[key] = value
    return result


def read_json(path):
    def reject_constant(value):
        raise ConfigError('non-JSON constant')
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=strict_object, parse_constant=reject_constant)


def defaults(environ, platform, home):
    def base(key, fallback=None):
        value = environ.get(key, fallback)
        if not value or not Path(value).is_absolute():
            raise ConfigError('invalid OS base')
        return Path(value)
    if platform == 'win32':
        return base('APPDATA') / 'Chombo/config', base('LOCALAPPDATA') / 'Chombo/data'
    if not home or not Path(home).is_absolute():
        raise ConfigError('home unavailable')
    home = Path(home)
    if platform == 'darwin':
        root = home / 'Library/Application Support/Chombo'
        return root / 'config', root / 'data'
    return base('XDG_CONFIG_HOME', str(home / '.config')) / 'chombo', base('XDG_DATA_HOME', str(home / '.local/share')) / 'chombo'


KEYS = {'port', 'data_dir', 'log_dir', 'assets_dir', 'log_level',
        'shutdown_timeout_seconds', 'allow_evaluation_records'}
PATHS = {'data_dir', 'log_dir', 'assets_dir'}


def validate(key, value, origin):
    if key in PATHS:
        if not isinstance(value, str) or not value or '\x00' in value:
            raise ConfigError('invalid path')
        return (origin / value).resolve()
    if key in ('port', 'shutdown_timeout_seconds'):
        limit = (0, 65535) if key == 'port' else (1, 300)
        if type(value) is not int or not limit[0] <= value <= limit[1]:
            raise ConfigError('invalid integer')
    elif key == 'allow_evaluation_records':
        if type(value) is not bool:
            raise ConfigError('invalid boolean')
    elif value not in ('DEBUG', 'INFO', 'WARNING', 'ERROR'):
        raise ConfigError('invalid log level')
    return value


def resolve(cli, *, environ=None, cwd=None, platform=None, home=None):
    env = dict(os.environ if environ is None else environ)
    cwd = Path.cwd() if cwd is None else Path(cwd)
    config_dir = data_default = None
    # OS bases are unnecessary when both locations are explicitly supplied.
    selected = cli.get('config') if cli.get('config') is not None else env.get('CHOMBO_CONFIG')
    if selected is None:
        config_dir, data_default = defaults(env, platform or sys.platform, str(Path.home()) if home is None else home)
    source = 'CLI' if cli.get('config') is not None else 'ENVIRONMENT' if selected is not None else 'DEFAULT'
    path = validate('data_dir', selected, cwd) if selected is not None else config_dir / 'runtime.json'
    values = {}
    try:
        document = read_json(path)
    except FileNotFoundError:
        if selected is not None:
            raise ConfigError('missing config')
    else:
        if type(document) is not dict or set(document) - KEYS - {'config_version'} or type(document.get('config_version')) is not int or document['config_version'] != 1:
            raise ConfigError('invalid config object')
        values = {key: validate(key, value, path.parent) for key, value in document.items() if key != 'config_version'}
    result = {'port': 8765, 'shutdown_timeout_seconds': 30, 'log_level': 'INFO',
              'allow_evaluation_records': False, 'assets_dir': Path(__file__).resolve().parents[1]}
    if data_default is not None:
        result['data_dir'] = data_default
    result.update(values)
    for key in KEYS:
        raw = env.get('CHOMBO_' + key.upper())
        if raw is not None:
            if key in ('port', 'shutdown_timeout_seconds'):
                if not raw or not raw.isascii() or not raw.isdecimal():
                    raise ConfigError('invalid environment integer')
                raw = int(raw)
            elif key == 'allow_evaluation_records':
                if raw not in ('true', 'false'):
                    raise ConfigError('invalid environment boolean')
                raw = raw == 'true'
            result[key] = validate(key, raw, cwd)
        if cli.get(key) is not None:
            raw = cli[key]
            result[key] = validate(key, str(raw) if key in PATHS else raw, cwd)
    if 'data_dir' not in result:
        _, result['data_dir'] = defaults(env, platform or sys.platform, str(Path.home()) if home is None else home)
    result.setdefault('log_dir', result['data_dir'] / 'logs')
    for key in ('data_dir', 'log_dir'):
        if result[key] == result['assets_dir'] or result['assets_dir'] in result[key].parents:
            raise ConfigError('writable path inside assets')
    result['config_source'] = source
    return result
