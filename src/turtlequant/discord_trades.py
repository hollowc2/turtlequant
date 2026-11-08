"""Small Discord webhook sender for confirmed trade fills."""

from __future__ import annotations

import functools
import io
import json
import math
import os
import sys
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Exit chart
# ---------------------------------------------------------------------------

# A share card: Discord shows it about 450px wide, so it carries a few large
# things (P&L, question, five stats, one price line) rather than a dashboard.
# Drawn supersampled and saved at 2x so it stays sharp when opened full size.
_W, _H = 1000, 620
_SS = 3  # drawing pixels per logical pixel
_OUT = 2  # saved pixels per logical pixel
_PLOT = (40, 236, 872, 566)  # left, top, right, bottom (logical px)
_FONTS = Path(__file__).with_name("fonts")

_BG = "#0e1116"
_GRID = "#1c212a"
_INK = "#eef1f6"
_INK_2 = "#a9b1be"
_MUTED = "#6b7482"
_PRE = "#4a5262"
_SPOT = "#5b9dff"
_STRIKE = "#e5a93b"
_GOOD = "#2fd07a"
_BAD = "#ff5d5d"

_HOUR_MS = 3_600_000
_DAY_MS = 24 * _HOUR_MS

# YES pays when spot finishes (european) or trades (barrier) on this side.
_YES_BELOW = {"barrier_down", "european_put"}
_BARRIERS = {"barrier", "barrier_down"}


def chart_window(entry_ms: int, exit_ms: int) -> tuple[str, int, int]:
    """Kline interval and span that frame the hold with some lead-in."""
    hold = max(exit_ms - entry_ms, _HOUR_MS)
    lead = min(max(hold * 0.4, 12 * _HOUR_MS), 10 * _DAY_MS)
    start, end = entry_ms - int(lead), exit_ms + int(hold * 0.04)
    span = end - start
    interval = "15m" if span <= 3 * _DAY_MS else "1h" if span <= 20 * _DAY_MS else "4h"
    return os.getenv("DISCORD_CHART_INTERVAL") or interval, start, end


@functools.cache
def _font(size: float, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    """Inter at a logical size; weight is Regular, Medium, SemiBold or Display."""
    name = "InterDisplay-Bold.ttf" if weight == "Display" else f"Inter-{weight}.ttf"
    try:
        return ImageFont.truetype(str(_FONTS / name), round(size * _SS))
    except OSError:
        return ImageFont.load_default(size=size * _SS)


def _nice_step(span: float, target: int) -> float:
    raw = span / max(target, 1)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _usd(value: float) -> str:
    if abs(value) >= 1000:
        return f"${value:,.0f}"
    if abs(value) >= 10:
        return f"${value:,.2f}"
    return f"${value:,.4f}"


def _price(value: float) -> str:
    """Spot level without the dollar sign, for axes and markers."""
    return _usd(value).removeprefix("$")


def _signed_usd(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _duration(ms: int) -> str:
    hours = ms / _HOUR_MS
    if hours < 1:
        return f"{ms / 60_000:.0f}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{int(hours // 24)}d {int(hours % 24)}h"


def _rgba(color: str, alpha: int) -> tuple[int, int, int, int]:
    return ImageColor.getrgb(color)[:3] + (alpha,)


Box = tuple[float, float, float, float]


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class _Canvas:
    """Logical-pixel drawing on a supersampled RGBA image."""

    def __init__(self) -> None:
        self.image = Image.new("RGBA", (_W * _SS, _H * _SS), _BG)
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def _xy(*points: float) -> list[float]:
        return [p * _SS for p in points]

    def _layer(self, paint) -> None:
        """Alpha-composite translucent shapes drawn by ``paint(draw)``."""
        overlay = Image.new("RGBA", self.image.size, (0, 0, 0, 0))
        paint(ImageDraw.Draw(overlay))
        self.image.alpha_composite(overlay)

    def rect(self, box, fill=None, outline=None, width=1.0, radius=0) -> None:
        self._layer(lambda d: d.rounded_rectangle(
            self._xy(*box), radius * _SS, fill=fill, outline=outline, width=round(width * _SS)))

    def fade(self, points: list[tuple[float, float]], floor: float, color: str, alpha: int) -> None:
        """Fill under a line down to ``floor``, fading out towards it."""
        top = min(y for _, y in points)
        mask = Image.new("L", self.image.size, 0)
        ImageDraw.Draw(mask).polygon(
            [p * _SS for xy in [(points[0][0], floor), *points, (points[-1][0], floor)] for p in xy], fill=255)
        ramp = Image.linear_gradient("L").resize((1, 256))
        y0, y1 = round(top * _SS), round(floor * _SS)
        ramp = ramp.point(lambda v: alpha * (255 - v) // 255).resize((self.image.width, max(y1 - y0, 1)))
        shade = Image.new("L", self.image.size, 0)
        shade.paste(ramp, (0, y0))
        layer = Image.new("RGBA", self.image.size, ImageColor.getrgb(color))
        layer.putalpha(ImageChops.multiply(mask, shade))
        self.image.alpha_composite(layer)

    def line(self, points, fill, width=1.0) -> None:
        if len(points) > 1:
            self.draw.line([p * _SS for xy in points for p in xy], fill=fill, width=round(width * _SS),
                           joint="curve")

    def dashed(self, x0, x1, y, fill, width=1.5, dash=6, gap=5) -> None:
        x = x0
        while x < x1:
            self.line([(x, y), (min(x + dash, x1), y)], fill, width)
            x += dash + gap

    def circle(self, x, y, radius, fill=None, outline=None, width=0.0) -> None:
        self._layer(lambda d: d.ellipse(self._xy(x - radius, y - radius, x + radius, y + radius),
                                        fill=fill, outline=outline, width=round(width * _SS)))

    def text(self, x, y, text, font, fill, anchor="la") -> None:
        self.draw.text((x * _SS, y * _SS), text, font=font, fill=fill, anchor=anchor)

    def width(self, text, font) -> float:
        return self.draw.textlength(text, font=font) / _SS

    def png(self) -> bytes:
        output = io.BytesIO()
        image = self.image.convert("RGB").resize((_W * _OUT, _H * _OUT), Image.Resampling.LANCZOS)
        image.save(output, "PNG", optimize=True)
        return output.getvalue()


def render_trade_chart(
    frame,
    *,
    asset: str,
    question: str,
    side: str,
    option_type: str,
    strike: float,
    entry_ms: int,
    exit_ms: int,
    upper_strike: float | None = None,
    entry_price: float | None = None,
    exit_price: float | None = None,
    model_prob: float | None = None,
    edge: float | None = None,
    sigma: float | None = None,
    pnl: float | None = None,
    size_usd: float | None = None,
    reason: str | None = None,
    expiry: str | None = None,
    interval: str = "1h",
    mode: str = "",
) -> bytes | None:
    """Share card for a closed trade: P&L, the question, and spot against the strike."""
    if frame is None or len(frame) < 2:
        return None
    rows = [
        (int(ts.timestamp() * 1000), float(c), float(h), float(lo))
        for ts, c, h, lo in zip(
            frame["open_time"],
            frame["close"],
            frame["high"] if "high" in frame else frame["close"],
            frame["low"] if "low" in frame else frame["close"],
        )
    ]
    # The line stops at the exit; candles after it are not part of the trade.
    held = [row for row in rows if row[0] <= exit_ms]
    rows = held if len(held) >= 2 else rows
    times, closes, highs, lows = (list(column) for column in zip(*rows))
    side = side.upper()
    won = (pnl or 0) >= 0
    outcome = _GOOD if won else _BAD

    canvas = _Canvas()
    left, top, right, bottom = _PLOT
    t0, t1 = times[0], times[-1]
    span_t = max(t1 - t0, 1)

    # Price range: the spot path, plus the strike when it doesn't flatten the path.
    low, high = min(lows), max(highs)
    path_span = high - low or max(high * 0.01, 1.0)
    levels = [s for s in (strike, upper_strike) if s]
    for level in levels:
        if low - path_span * 1.5 <= level <= high + path_span * 1.5:
            low, high = min(low, level), max(high, level)
    pad = (high - low) * 0.1 or max(high * 0.01, 1.0)
    low, high = low - pad, high + pad

    def x_at(ms: int) -> float:
        return left + (ms - t0) * (right - 24 - left) / span_t  # room for the exit marker

    def y_at(price: float) -> float:
        return bottom - (price - low) * (bottom - top) / (high - low)

    def clamp_y(y: float) -> float:
        return min(max(y, top), bottom)

    # ---- Header ---------------------------------------------------------
    tag_font = _font(13, "SemiBold")
    tags = [asset.upper(), f"BOUGHT {side}", option_type.replace("_", " ").upper()]
    x = 40
    for i, tag in enumerate(tags):
        if i:
            canvas.text(x + 8, 40, "·", tag_font, _MUTED, "lm")
            x += 22
        canvas.text(x, 40, tag, tag_font, _INK_2 if i == 0 else _MUTED, "lm")
        x += canvas.width(tag, tag_font)
    if mode:
        label = mode.upper()
        width = canvas.width(label, _font(11, "SemiBold")) + 14
        canvas.rect((x + 14, 30, x + 14 + width, 50), outline=_rgba(_INK_2, 90), width=1, radius=10)
        canvas.text(x + 14 + width / 2, 40, label, _font(11, "SemiBold"), _INK_2, "mm")

    title_font = _font(27, "SemiBold")
    lines, words = [""], question.split()
    for word in words:
        trial = f"{lines[-1]} {word}".strip()
        if canvas.width(trial, title_font) <= 600 or not lines[-1]:
            lines[-1] = trial
        elif len(lines) < 2:
            lines.append(word)
        else:
            lines[-1] = trial
    if canvas.width(lines[-1], title_font) > 600:
        while canvas.width(lines[-1] + "…", title_font) > 600:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    for i, line in enumerate(lines):
        canvas.text(40, 66 + i * 36, line, title_font, _INK)

    if pnl is not None:
        canvas.text(_W - 40, 26, _signed_usd(pnl), _font(58, "Display"), outcome, "ra")
        sub = [f"{pnl / size_usd:+.1%}"] if size_usd else []
        if reason:
            sub.append(reason.replace("_", " ").capitalize())
        canvas.text(_W - 40, 100, "  ·  ".join(sub), _font(17, "Medium"), _INK_2, "ra")

    # ---- Stats strip ----------------------------------------------------
    stats = [
        ("TOKEN", f"{entry_price:.3f} → {exit_price:.3f}" if entry_price is not None and exit_price is not None else None),
        ("HELD", _duration(exit_ms - entry_ms)),
        (f"MODEL P({side})", f"{model_prob:.1%}" if model_prob is not None else None),
        ("EDGE", f"{edge:+.1%}" if edge is not None else None),
        ("SIZE", f"${size_usd:,.2f}" if size_usd else None),
    ]
    canvas.line([(40, 152), (_W - 40, 152)], _GRID, 1)
    x = 40
    for label, value in ((k, v) for k, v in stats if v is not None):
        canvas.text(x, 172, label, _font(11, "SemiBold"), _MUTED, "lm")
        canvas.text(x, 196, value, _font(20, "SemiBold"), _INK, "lm")
        x += max(canvas.width(value, _font(20, "SemiBold")), 60) + 44

    # ---- Plot -----------------------------------------------------------
    # Where the held token pays: YES regions, or their complement for NO.
    if option_type == "range" and upper_strike:
        yes = [(strike, upper_strike)]
    elif option_type in _YES_BELOW:
        yes = [(-math.inf, strike)]
    else:
        yes = [(strike, math.inf)]
    wins = _complement(yes) if side == "NO" else yes
    taken: list[Box] = []
    for lo, hi in wins:
        y_hi = clamp_y(y_at(hi)) if math.isfinite(hi) else top
        y_lo = clamp_y(y_at(lo)) if math.isfinite(lo) else bottom
        if y_lo - y_hi > 1:
            canvas.rect((left, y_hi, right, y_lo), fill=_rgba(_GOOD, 15))
            if y_lo - y_hi > 26:
                y = y_hi + 14 if y_hi <= top or not math.isfinite(hi) else y_lo - 14
                canvas.text(left + 10, y, f"{side} PAYS", _font(11, "SemiBold"), _rgba(_GOOD, 200), "lm")
                taken.append((left, y - 10, left + 80, y + 10))

    # Strike lines; an off-scale strike gets a note on the edge it lies beyond.
    strike_ys = []
    for level in levels:
        y = y_at(level)
        if top <= y <= bottom:
            canvas.dashed(left, right, y, _rgba(_STRIKE, 230), 1.5)
            text = _price(level)
            font = _font(13, "SemiBold")
            width = canvas.width(text, font) + 14
            canvas.rect((right + 6, y - 11, right + 6 + width, y + 11), fill=_STRIKE, radius=5)
            canvas.text(right + 13, y, text, font, _BG, "lm")
            strike_ys.append(y)
        else:
            above = y < top
            distance = abs(level / closes[-1] - 1)
            text = f"STRIKE {_price(level)}  {'↑' if above else '↓'} {distance:.1%} {'above' if above else 'below'}"
            font = _font(12, "SemiBold")
            width = canvas.width(text, font) + 16
            y = top + 16 if above else bottom - 16
            canvas.rect((right - 8 - width, y - 11, right - 8, y + 11), fill=_rgba(_STRIKE, 40),
                        outline=_rgba(_STRIKE, 160), width=1, radius=5)
            canvas.text(right - 8 - width / 2, y, text, font, _STRIKE, "mm")
            taken.append((right - 8 - width, y - 11, right - 8, y + 11))

    # Price grid on the right, skipping ticks the strike tag would cover.
    axis_font = _font(12, "Medium")
    step = _nice_step(high - low, 4)
    tick = math.ceil(low / step) * step
    while tick <= high:
        y = y_at(tick)
        canvas.line([(left, y), (right, y)], _GRID, 1)
        if all(abs(y - s) > 18 for s in strike_ys):
            canvas.text(right + 10, y, _price(tick) if step < 1000 else f"{tick:,.0f}", axis_font, _MUTED, "lm")
        tick += step

    # Time axis on UTC-aligned steps.
    for step_ms in (_HOUR_MS * h for h in (1, 2, 3, 6, 12, 24, 48, 72, 168, 336, 720)):
        if span_t / step_ms <= 5:
            break
    tick_ms = (t0 // step_ms + 1) * step_ms
    while tick_ms < t1:
        when = datetime.fromtimestamp(tick_ms / 1000, UTC)
        label = f"{when:%b} {when.day}" if step_ms >= _DAY_MS or when.hour == 0 else f"{when:%H:%M}"
        canvas.text(x_at(tick_ms), bottom + 20, label, axis_font, _MUTED, "mm")
        tick_ms += step_ms

    # Spot: grey before entry, coloured over the hold.
    def nearest(ms: int) -> int:
        return min(range(len(times)), key=lambda i: abs(times[i] - ms))

    points = [(x_at(t), y_at(c)) for t, c in zip(times, closes)]
    entry_i, exit_i = nearest(entry_ms), len(points) - 1
    entry_xy, exit_xy = points[entry_i], points[exit_i]
    canvas.line([(entry_xy[0], top), (entry_xy[0], bottom)], _rgba(_INK_2, 35), 1)
    hold = points[entry_i:]
    if len(hold) > 1:
        canvas.fade(hold, bottom, _SPOT, 70)
    canvas.line(points[: entry_i + 1], _PRE, 2)
    for y in strike_ys:
        # Name the line at its left end, on the side clear of the path, then of other labels.
        near = [py for px, py in points if px < left + 90]
        sides = [12, -12] if near and min(near) < y else [-12, 12]
        boxes = [(left, y + dy - 10, left + 70, y + dy + 10) for dy in sides]
        free = [b for b in boxes if not any(_overlaps(b, t) for t in taken) and top <= b[1] and b[3] <= bottom]
        box = free[0] if free else (left + 90, *boxes[0][1:3], left + 160)
        canvas.text(box[0] + 10, (box[1] + box[3]) / 2, "STRIKE", _font(11, "SemiBold"), _STRIKE, "lm")
        taken.append(box)
    canvas.line(hold, _SPOT, 2.5)

    # First barrier touch after entry.
    touched_i = None
    if option_type in _BARRIERS:
        for i, (t, hi, lo) in enumerate(zip(times, highs, lows)):
            if t >= entry_ms and (hi >= strike if option_type == "barrier" else lo <= strike):
                touched_i = i
                break

    # Markers, each labelled where it covers the least of the line and other labels.
    def label(anchor: tuple[float, float], name: str, value: str, color: str) -> None:
        name_font, value_font = _font(10, "SemiBold"), _font(14, "SemiBold")
        width = canvas.width(name, name_font) + canvas.width(value, value_font) + 26
        x, y = anchor
        candidates = [
            (x - width / 2, y - 40), (x - width / 2, y + 16), (x + 14, y - 13), (x - 14 - width, y - 13),
            (x + 10, y - 38), (x - 10 - width, y - 38), (x + 10, y + 14), (x - 10 - width, y + 14),
        ]

        def cost(box: Box) -> float:
            inside = left <= box[0] and box[2] <= right and top <= box[1] and box[3] <= bottom
            hits = sum(1 for px, py in points if box[0] - 4 <= px <= box[2] + 4 and box[1] - 4 <= py <= box[3] + 4)
            return (0 if inside else 1000) + 100 * sum(_overlaps(box, t) for t in taken) + hits

        box = min(((bx, by, bx + width, by + 26) for bx, by in candidates), key=cost)
        taken.append(box)
        canvas.rect(box, fill=_rgba("#090b0f", 225), outline=_rgba(color, 120), width=1, radius=6)
        canvas.text(box[0] + 9, box[1] + 13, name, name_font, color, "lm")
        canvas.text(box[2] - 9, box[1] + 13, value, value_font, _INK, "rm")

    if touched_i is not None and min(abs(points[touched_i][0] - x) for x in (entry_xy[0], exit_xy[0])) > 30:
        canvas.circle(*points[touched_i], 4.5, fill=_STRIKE, outline=_BG, width=2)
        label(points[touched_i], "TOUCHED", _price(closes[touched_i]), _STRIKE)
    canvas.circle(*entry_xy, 6, fill=_BG, outline=_INK, width=2.5)
    canvas.circle(*exit_xy, 14, fill=_rgba(outcome, 45))
    canvas.circle(*exit_xy, 6.5, fill=outcome, outline=_BG, width=2)
    taken += [(x - 9, y - 9, x + 9, y + 9) for x, y in (entry_xy, exit_xy)]
    label(exit_xy, "EXIT", _price(closes[exit_i]), outcome)
    label(entry_xy, "ENTRY", _price(closes[entry_i]), _INK_2)

    return canvas.png()


def _complement(regions: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parts of the real line not covered by ``regions``."""
    out, cursor = [], -math.inf
    for lo, hi in sorted(regions):
        if lo > cursor:
            out.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < math.inf:
        out.append((cursor, math.inf))
    return out


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


class DiscordTrades:
    def __init__(self, state_dir: Path, mode: str) -> None:
        setting = os.getenv("DISCORD_TRADES", "all").lower()
        self.mode = mode
        self.enabled = setting == "all" or (setting == "live" and mode == "live")
        webhook_file = Path(
            os.getenv("DISCORD_WEBHOOK_FILE", "/run/secrets/discord-webhook")
        )
        self.webhook = (
            webhook_file.read_text().strip()
            if self.enabled and webhook_file.exists()
            else ""
        )
        self.links_file = state_dir / "turtlequant-discord.json"

    def send(
        self,
        key: str,
        content: str,
        chart: bytes | None = None,
        *,
        remember: bool = False,
    ) -> None:
        if not self.webhook:
            return
        link = self._load().get(key)
        if link and not remember:
            content += f"\n> Entry: {link}"
        payload = {"content": content, "allowed_mentions": {"parse": []}}
        url = f"{self.webhook}?wait=true"
        if chart:
            boundary = uuid.uuid4().hex
            body = (
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                    "Content-Type: application/json\r\n\r\n"
                    f"{json.dumps(payload)}\r\n--{boundary}\r\n"
                    'Content-Disposition: form-data; name="files[0]"; filename="trade.png"\r\n'
                    "Content-Type: image/png\r\n\r\n"
                ).encode()
                + chart
                + f"\r\n--{boundary}--\r\n".encode()
            )
            request = urllib.request.Request(
                url,
                body,
                {
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "TurtleQuant/1.0",
                },
            )
        else:
            request = urllib.request.Request(
                url,
                json.dumps(payload).encode(),
                {"Content-Type": "application/json", "User-Agent": "TurtleQuant/1.0"},
            )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                message = json.load(response)
            if remember:
                links = self._load()
                guild_id = message.get("guild_id")
                if not guild_id:
                    request = urllib.request.Request(
                        self.webhook, headers={"User-Agent": "TurtleQuant/1.0"}
                    )
                    with urllib.request.urlopen(request, timeout=10) as response:
                        guild_id = json.load(response)["guild_id"]
                links[key] = (
                    f"https://discord.com/channels/{guild_id}/"
                    f"{message['channel_id']}/{message['id']}"
                )
                self.links_file.write_text(json.dumps(links))
        except Exception as exc:
            print(f"Discord notification failed: {exc}", file=sys.stderr, flush=True)

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.links_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
