# mtr-nexttrain-bot

Telegram bot: send your location → nearest MTR station or Light Rail stop →
the next trains, each as **minutes left** and **clock time (HH:MM)**.

- 📍 location (button, attachment or venue) → nearest of 97 MTR stations + 68 LRT stops,
  with buttons for the next 4 nearest and 🔄 Refresh
- type a name (`Admiralty`, `金鐘`, `ADM`, `siu hong`) or `/station <name>`
- interchanges show every line (Admiralty = EAL, SIL, TWL, ISL)
- Siu Hong, Tuen Mun, Tin Shui Wai and Yuen Long are one board each: MTR lines
  first, then the Light Rail platforms, whichever half you picked (`INTERCHANGES`)
- MTR service alerts (`status: 0`) and "data delayed" (`isdelay: Y`) are shown in place

Data: MTR Next Train API (data.gov.hk `mtr-data2-nexttrain-data`, spec v1.7) and
Light Rail Next Train API (data dictionary v1.0). Station coordinates: MTR's
opendata CSVs + OpenStreetMap, built by `tools/build_stations.py`.

## Layout

| | |
|---|---|
| `app/bot.py` | polling loop, handlers, per-user burst limit (8 / 30 s) |
| `app/mtr.py` | stations, nearest/search, both APIs, 10 s response cache |
| `app/render.py` | message HTML + inline keyboards |
| `app/tgapi.py` | Bot API client (trimmed from dllmch-tgbot) |
| `app/selftest.py` | fake Telegram + fake MTR on loopback, 29 checks |
| `deploy/` | systemd unit, config template, `install.sh` |

Only dependency: `python3-requests` (apt).

## On mainhomeserver

| | |
|---|---|
| Unit | `mtr-nexttrain-bot.service` (DynamicUser, no state) |
| Code | `/opt/mtr-nexttrain-bot/app/` |
| Token | `/etc/mtr-nexttrain-bot/bot.conf`, root:root 0600, via `LoadCredential=` |

```sh
sudo nano /etc/mtr-nexttrain-bot/bot.conf      # bot_token = 123456:ABC...
sudo systemctl restart mtr-nexttrain-bot
journalctl -u mtr-nexttrain-bot -n 5            # "connected as @..."
```

Source lives on the server in `/mnt/raid1/shared/Documents/Dumbass Programs/mtr-nexttrain-bot`.
Update: `cd` there, `sudo bash deploy/install.sh`.
Test: `cd /opt/mtr-nexttrain-bot/app && sudo -u nobody python3 selftest.py`.
