"""Turn an mtr.Board into a Telegram HTML message and its buttons."""

import html

import mtr

ICON = {mtr.HEAVY: "🚇", mtr.LIGHT: "🚈"}
MAX_TRAINS = 4  # per direction / platform; the APIs return at most four or five


def esc(text):
    return html.escape(str(text), quote=False)


def fmt_distance(metres):
    if metres < 1000:
        return "%d m" % (round(metres / 10.0) * 10)
    return "%.1f km" % (metres / 1000.0)


def icon(st):
    return ICON[mtr.HEAVY] + ICON[mtr.LIGHT] if st.partner else ICON[st.kind]


def station_label(st):
    return "%s %s %s" % (icon(st), st.en, st.zh)


def board_text(board, distance=None):
    st = board.station
    head = "%s <b>%s %s</b>" % (icon(st), esc(st.en), esc(st.zh))
    if distance is not None:
        head += "  ·  %s away" % fmt_distance(distance)
    if st.kind == mtr.LIGHT:
        head += "\n<i>Light Rail 輕鐵 · routes %s</i>" % esc(", ".join(st.lines))
    lines = [head]

    for i, sec in enumerate(board.sections):
        lines.append("")
        if st.partner and sec.kind == mtr.LIGHT and board.sections[i - 1].kind != mtr.LIGHT:
            # Interchange: the MTR lines are above, the Light Rail stop below.
            lines.append("🚈 <b>Light Rail 輕鐵</b>  ·  <i>routes %s</i>" % esc(", ".join(st.partner.lines)))
            lines.append("")
        title = sec.title + (" " + sec.title_zh if sec.title_zh else "")
        lines.append("<b><u>%s</u></b>" % esc(title))
        if sec.error:
            lines.append("⚠️ " + esc(sec.error))
            continue
        if sec.warning:
            lines.append("⚠️ " + esc(sec.warning))
        for group in sec.groups:
            if group.heading:
                lines.append("<b>%s</b>" % esc(group.heading))
            if not group.trains:
                lines.append("   <i>No more trains this way</i>")
                continue
            for t in group.trains[:MAX_TRAINS]:
                row = "<code>%9s</code>  %s" % (esc(t.label), esc(t.clock))
                if t.note:
                    row += "  " + esc(t.note)
                lines.append(row)

    lines.append("")
    if board.updated:
        lines.append("<i>MTR data as of %s</i>" % esc(board.updated))
    else:
        lines.append("<i>No timestamp from MTR</i>")
    return "\n".join(lines)


def _cb(station, loc):
    data = "b:%s:%s" % (station.kind, station.id)
    if loc:
        data += ":%.5f:%.5f" % loc
    return data  # at most ~30 bytes; Telegram's limit is 64


def board_keyboard(station, loc=None, others=()):
    """Refresh, plus the other nearby stations when we know where the user is.

    The user's position rides in the callback data, so Refresh and the nearby
    buttons keep working without the bot storing anyone's location."""
    rows = [[{"text": "🔄 Refresh", "callback_data": _cb(station, loc)}]]
    for metres, other in others:
        rows.append([{
            "text": "%s · %s" % (station_label(other), fmt_distance(metres)),
            "callback_data": _cb(other, loc),
        }])
    return {"inline_keyboard": rows}


def choice_keyboard(stations):
    rows = []
    for st in stations:
        extra = "" if st.kind == mtr.LIGHT else " (%s)" % ", ".join(
            st.lines + (["Light Rail"] if st.partner else []))
        rows.append([{"text": station_label(st) + extra, "callback_data": _cb(st, None)}])
    return {"inline_keyboard": rows}


LOCATION_KEYBOARD = {
    "keyboard": [[{"text": "📍 Nearest station", "request_location": True}]],
    "resize_keyboard": True,
    "is_persistent": True,
}

WELCOME = (
    "🚇 <b>MTR / Light Rail next train</b>\n\n"
    "• Tap <b>📍 Nearest station</b> below (or attach a location) and I will find "
    "the closest MTR station or Light Rail stop and list the next trains.\n"
    "• Or type a station name, e.g. <code>Admiralty</code>, <code>金鐘</code>, "
    "<code>TKO</code> or <code>Siu Hong</code>.\n\n"
    "Each train shows minutes left and the clock time (HH:MM). "
    "Tap 🔄 to refresh. Data: MTR via data.gov.hk."
)
