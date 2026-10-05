#!/bin/bash
# Install or update the bot on mainhomeserver. Run as root from the repo root:
#     sudo bash deploy/install.sh
# Never touches an existing /etc/mtr-nexttrain-bot/bot.conf.
set -euo pipefail

name=mtr-nexttrain-bot
src=$(cd "$(dirname "$0")/.." && pwd)

install -d -m 0755 -o root -g root "/opt/$name" "/opt/$name/app"
for f in bot.py config.py mtr.py render.py tgapi.py selftest.py stations.json; do
    install -m 0644 -o root -g root "$src/app/$f" "/opt/$name/app/$f"
done

install -d -m 0700 -o root -g root "/etc/$name"
if [ ! -e "/etc/$name/bot.conf" ]; then
    install -m 0600 -o root -g root "$src/deploy/bot.conf.template" "/etc/$name/bot.conf"
    echo "created /etc/$name/bot.conf (empty token)"
fi

install -m 0644 -o root -g root "$src/deploy/$name.service" "/etc/systemd/system/$name.service"
systemctl daemon-reload
systemd-analyze verify "/etc/systemd/system/$name.service"
systemctl enable "$name.service"

if grep -qE '^[[:space:]]*bot_token[[:space:]]*=[[:space:]]*[0-9]+:' "/etc/$name/bot.conf"; then
    systemctl restart "$name.service"
    echo "restarted $name"
else
    echo "no token yet: edit /etc/$name/bot.conf, then: systemctl restart $name"
fi
