"""Offline, fail-closed validation for deployment-specific settings."""
from urllib.parse import urlsplit

from access import configured_owner


class ConfigurationError(ValueError):
    """A controlled diagnostic that never includes submitted credentials."""


def validate_config(config):
    if not isinstance(config, dict):
        raise ConfigurationError('configuration must be a JSON object')
    try:
        configured_owner(config)
    except ValueError:
        raise ConfigurationError('owner_id must be your positive numeric Telegram user ID') from None
    token = config.get('bot_token')
    if (not isinstance(token, str) or not token.strip()
            or token.startswith(('YOUR_', 'INSERT_', 'CHANGE_ME'))):
        raise ConfigurationError('bot_token must be configured')
    users = config.get('allowed_user_ids', [])
    if not isinstance(users, list) or any(type(uid) is not int or not 0 < uid < 2**63 for uid in users):
        raise ConfigurationError('allowed_user_ids must be a list of positive numeric Telegram IDs')
    for key in ('printer_url', 'snapshot_url'):
        value = config.get(key)
        if not isinstance(value, str) or any(ord(c) < 33 for c in value):
            raise ConfigurationError(key + ' must be an HTTP(S) URL')
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
                raise ValueError()
            parsed.port
        except ValueError:
            raise ConfigurationError(key + ' must be an HTTP(S) URL without embedded credentials') from None
    for key in ('web_enabled', 'media_enabled'):
        if type(config.get(key, False)) is not bool:
            raise ConfigurationError(key + ' must be true or false')
    if config.get('web_enabled'):
        origin = config.get('web_origin', '')
        try:
            parsed = urlsplit(origin)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                raise ValueError()
        except ValueError:
            raise ConfigurationError('web_origin must be the HTTPS origin of your dashboard') from None
    return config
