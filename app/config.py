"""Configuration: one `key = value` file holding the bot token.

Under systemd the file is /etc/mtr-nexttrain-bot/bot.conf (root:root 0600),
handed to the service with LoadCredential=, so the process reads it from
$CREDENTIALS_DIRECTORY and the unprivileged (DynamicUser) service never needs
any rights on /etc. Outside systemd, MTRBOT_CONF names the file.

Keys:
    bot_token      = <from @BotFather>       (required)
    allowed_users  = 123456789, 987654321    (optional; empty = anyone)
"""

import os
import re

_TOKEN_RE = re.compile(r"^([0-9]{5,16}):([A-Za-z0-9_-]{20,})$")


class ConfigError(Exception):
    pass


def conf_path():
    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    if creds:
        return os.path.join(creds, "bot.conf")
    return os.environ.get("MTRBOT_CONF", "/etc/mtr-nexttrain-bot/bot.conf")


def _parse(path):
    values = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip().lower()] = value.strip().strip('"').strip("'")
    return values


class Config(object):
    def __init__(self, path=None):
        path = path or conf_path()
        try:
            values = _parse(path)
        except OSError as exc:
            raise ConfigError("cannot read %s (%s)" % (path, exc.__class__.__name__))

        token = values.get("bot_token", "")
        m = _TOKEN_RE.match(token)
        if not m:
            # Never echo the value -- it may be a real token with a typo.
            raise ConfigError(
                "bot_token in %s is %s" % (path, "empty" if not token else "not in BotFather's 123456:ABC... format")
            )
        self.token = token
        # Public half of the token: safe to log, says which bot is running.
        self.bot_id = int(m.group(1))

        self.allowed_users = set()
        for part in re.split(r"[\s,]+", values.get("allowed_users", "")):
            if part:
                if not part.isdigit():
                    raise ConfigError("allowed_users must be numeric Telegram user ids")
                self.allowed_users.add(int(part))
