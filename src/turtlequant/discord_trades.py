"""Small Discord webhook sender for confirmed trade fills."""

from __future__ import annotations

import io
import json
import math
import os
import sys
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Exit chart
# ---------------------------------------------------------------------------

# Drawn at 2x and downsampled so lines and text come out antialiased.
_SCALE = 2
_W, _H = 1200, 640
_PLOT = (24, 112, 820, 552)  # left, top, right, bottom (logical px)
_SIDE_X = 912

_BG = "#12151b"
_PANEL = "#181c24"
_GRID = "#252a34"
_INK = "#f2f4f8"
_INK_2 = "#b4bccb"
_MUTED = "#7d8696"
_SPOT = "#3987e5"
_STRIKE = "#c98500"
_GOOD = "#0ca30c"
_GOOD_TEXT = "#3ccf6e"
_BAD = "#d03b3b"
_BAD_TEXT = "#ff7a7a"

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


def _font(size: float, bold: bool = False) -> ImageFont.FreeTypeFont:
    font = ImageFont.load_default(size=size * _SCALE)
    font.bold = bold  # read by _Canvas.text
    return font


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


def _signed_usd(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _duration(ms: int) -> str:
    hours = ms / _HOUR_MS
    if hours < 1:
        return f"{ms / 60_000:.0f}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{int(hours // 24)}d {int(hours % 24)}h"


class _Canvas:
    """Logical-pixel drawing on a 2x supersampled RGBA image."""

    def __init__(self) -> None:
        self.image = Image.new("RGBA", (_W * _SCALE, _H * _SCALE), _BG)
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def _xy(*points: float) -> list[float]:
        return [p * _SCALE for p in points]

    def rect(self, box, fill=None, outline=None, width=1, radius=0) -> None:
        self.draw.rounded_rectangle(self._xy(*box), radius * _SCALE, fill=fill, outline=outline, width=width * _SCALE)

    def tint(self, box, color: str, alpha: int) -> None:
        overlay = Image.new("RGBA", self.image.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle(self._xy(*box), fill=ImageColor.getrgb(color) + (alpha,))
        self.image.alpha_composite(overlay)

    def line(self, points, fill, width=1.0) -> None:
        self.draw.line([p * _SCALE for xy in points for p in xy], fill=fill, width=round(width * _SCALE), joint="curve")

    def dashed(self, x0, x1, y, fill, width=1.5, dash=8, gap=6) -> None:
        x = x0
        while x < x1:
            self.line([(x, y), (min(x + dash, x1), y)], fill, width)
            x += dash + gap

    def dot(self, x, y, radius, fill, ring=_BG, ring_width=2) -> None:
        r = radius + ring_width
        self.draw.ellipse(self._xy(x - r, y - r, x + r, y + r), fill=ring)
        self.draw.ellipse(self._xy(x - radius, y - radius, x + radius, y + radius), fill=fill)

    def text(self, x, y, text, font, fill, anchor="la") -> None:
        stroke = 1 if getattr(font, "bold", False) else 0
        self.draw.text((x * _SCALE, y * _SCALE), text, font=font, fill=fill, anchor=anchor,
                       stroke_width=stroke, stroke_fill=fill)

    def text_width(self, text, font) -> float:
        return self.draw.textlength(text, font=font) / _SCALE

    def pill(self, x, y, text, font, fill, ink, anchor="l") -> None:
        """Label in a rounded box; ``y`` is the vertical centre."""
        width = self.text_width(text, font) + 14
        x0 = x - width if anchor == "r" else x
        self.rect((x0, y - 11, x0 + width, y + 11), fill=fill, radius=5)
        self.text(x0 + 7, y, text, font, ink, "lm")

    def png(self) -> bytes:
        output = io.BytesIO()
        self.image.convert("RGB").resize((_W, _H), Image.Resampling.LANCZOS).save(output, "PNG", optimize=True)
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
    """Spot path around a closed trade, with the strike, the side we held and a trade read."""
    if frame is None or len(frame) < 2:
        return None
    times = [int(ts.timestamp() * 1000) for ts in frame["open_time"]]
    closes = [float(v) for v in frame["close"]]
    highs = [float(v) for v in frame["high"]] if "high" in frame else closes
    lows = [float(v) for v in frame["low"]] if "low" in frame else closes
    side = side.upper()

    canvas = _Canvas()
    left, top, right, bottom = _PLOT
    t0, t1 = times[0], max(times[-1], exit_ms)
    span_t = max(t1 - t0, 1)

    # Price range: the spot path, plus the strike when it doesn't flatten the path.
    low, high = min(lows), max(highs)
    path_span = high - low or max(high * 0.01, 1.0)
    levels = [s for s in (strike, upper_strike) if s]
    for level in levels:
        if low - path_span * 1.5 <= level <= high + path_span * 1.5:
            low, high = min(low, level), max(high, level)
    pad = (high - low) * 0.08 or max(high * 0.01, 1.0)
    low, high = low - pad, high + pad

    def x_at(ms: int) -> float:
        return left + (ms - t0) * (right - left) / span_t

    def y_at(price: float) -> float:
        return bottom - (price - low) * (bottom - top) / (high - low)

    def clamp_y(y: float) -> float:
        return min(max(y, top), bottom)

    # Where the held token pays: YES regions, or their complement for NO.
    if option_type == "range" and upper_strike:
        yes = [(strike, upper_strike)]
    elif option_type in _YES_BELOW:
        yes = [(-math.inf, strike)]
    else:
        yes = [(strike, math.inf)]
    wins = _complement(yes) if side == "NO" else yes

    canvas.rect((left, top, right, bottom), fill=_PANEL)
    for lo, hi in wins:
        y_hi, y_lo = clamp_y(y_at(hi)) if math.isfinite(hi) else top, clamp_y(y_at(lo)) if math.isfinite(lo) else bottom
        if y_lo - y_hi > 1:
            canvas.tint((left, y_hi, right, y_lo), _GOOD, 26)
    label_font, small = _font(13, bold=True), _font(13)
    if wins:
        lo, hi = wins[0]
        y_hi = clamp_y(y_at(hi)) if math.isfinite(hi) else top
        y_lo = clamp_y(y_at(lo)) if math.isfinite(lo) else bottom
        if y_lo - y_hi > 24:
            label_y = y_hi + 16 if not math.isfinite(hi) or y_hi <= top else y_lo - 16
            canvas.text(left + 12, label_y, f"{side} PAYS HERE", label_font, _GOOD_TEXT, "lm")

    # Hold window.
    hold_x0, hold_x1 = x_at(entry_ms), x_at(exit_ms)
    canvas.tint((hold_x0, top, max(hold_x1, hold_x0 + 1), bottom), "#ffffff", 9)

    # Grid and price axis.
    axis_font = _font(12)
    step = _nice_step(high - low, 5)
    tick = math.ceil(low / step) * step
    while tick <= high:
        y = y_at(tick)
        canvas.line([(left, y), (right, y)], _GRID, 1)
        canvas.text(right + 8, y, _usd(tick) if step < 1 else f"{tick:,.0f}", axis_font, _MUTED, "lm")
        tick += step

    # Time axis on UTC-aligned steps.
    for step_ms in (_HOUR_MS * h for h in (1, 2, 3, 6, 12, 24, 48, 72, 168, 336, 720)):
        if span_t / step_ms <= 7:
            break
    tick_ms = (t0 // step_ms + 1) * step_ms
    fmt = "%b %d %H:%M" if step_ms < _DAY_MS else "%b %d"
    while tick_ms < t1:
        x = x_at(tick_ms)
        canvas.line([(x, top), (x, bottom)], _GRID, 1)
        label = datetime.fromtimestamp(tick_ms / 1000, UTC).strftime(fmt)
        canvas.text(x, bottom + 10, label, axis_font, _MUTED, "ma")
        tick_ms += step_ms

    # Strike(s): on the chart when in range, otherwise a pill on the edge they lie beyond.
    last_close = closes[-1]
    for level in levels:
        y = y_at(level)
        distance = level / last_close - 1
        if top <= y <= bottom:
            canvas.dashed(left, right, y, _STRIKE, 1.5)
            canvas.pill(right - 6, y, f"STRIKE {_usd(level)}", small, _STRIKE, _BG, anchor="r")
        else:
            edge_y = top + 14 if y < top else bottom - 14
            where = "above" if y < top else "below"
            canvas.pill(right - 6, edge_y, f"STRIKE {_usd(level)} · {abs(distance):.1%} {where}", small,
                        _STRIKE, _BG, anchor="r")

    # Spot path.
    points = [(x_at(t), y_at(c)) for t, c in zip(times, closes)]
    canvas.line(points, _SPOT, 2)

    # First barrier touch after entry.
    touched_at = None
    if option_type in _BARRIERS:
        for t, hi, lo in zip(times, highs, lows):
            if t >= entry_ms and (hi >= strike if option_type == "barrier" else lo <= strike):
                touched_at = t
                break
    if touched_at is not None:
        x = x_at(touched_at)
        canvas.line([(x, top), (x, bottom)], _STRIKE, 1)
        if x + 130 < right:
            canvas.text(x + 6, top + 8, "BARRIER TOUCHED", small, _STRIKE)
        else:
            canvas.text(x - 6, top + 8, "BARRIER TOUCHED", small, _STRIKE, "ra")

    # Entry and exit markers on the nearest bar.
    def nearest(ms: int) -> tuple[float, float, float]:
        index = min(range(len(times)), key=lambda i: abs(times[i] - ms))
        return x_at(times[index]), y_at(closes[index]), closes[index]

    entry_x, entry_y, entry_spot = nearest(entry_ms)
    exit_x, exit_y, exit_spot = nearest(exit_ms)
    exit_color = _GOOD if (pnl or 0) >= 0 else _BAD
    close_together = abs(exit_x - entry_x) < 150
    for x, y, spot, label, fill, above in (
        (entry_x, entry_y, entry_spot, "ENTRY", _INK, True),
        (exit_x, exit_y, exit_spot, "EXIT", exit_color, not close_together),
    ):
        canvas.dot(x, y, 6, fill)
        text = f"{label} {_usd(spot)}"
        width = canvas.text_width(text, label_font) + 14
        label_x = min(max(x - width / 2, left + 4), right - width - 4)
        label_y = y - 24 if above else y + 24
        if label_y < top + 12 or label_y > bottom - 12:
            label_y = y + 24 if above else y - 24
        canvas.pill(label_x, label_y, text, label_font, "#0b0d11", _INK)

    canvas.rect((left, top, right, bottom), outline=_GRID, width=1)

    # Header.
    tags = [asset.upper(), f"BOUGHT {side}", option_type.replace("_", " ").upper()]
    if mode:
        tags.append(mode.upper())
    canvas.text(left, 22, "  ·  ".join(tags), _font(13, bold=True), _MUTED)
    title_font = _font(22, bold=True)
    title = question
    while canvas.text_width(title, title_font) > 800 and len(title) > 10:
        title = title.removesuffix("…")[:-1].rstrip() + "…"
    canvas.text(left, 46, title, title_font, _INK)
    if pnl is not None:
        color = _GOOD_TEXT if pnl >= 0 else _BAD_TEXT
        canvas.text(_W - 24, 18, _signed_usd(pnl), _font(32, bold=True), color, "ra")
        sub = []
        if size_usd:
            sub.append(f"{pnl / size_usd:+.1%}")
        if reason:
            sub.append(reason.replace("_", " "))
        canvas.text(_W - 24, 62, "  ·  ".join(sub), _font(14), _INK_2, "ra")

    # Trade read.
    rows = [
        ("Token", f"{entry_price:.3f} to {exit_price:.3f}" if entry_price is not None and exit_price is not None else None),
        ("Model P at entry", f"{model_prob:.1%}" if model_prob is not None else None),
        ("Edge at entry", f"{edge:+.1%}" if edge is not None else None),
        ("Implied vol", f"{sigma:.1%}" if sigma else None),
        ("Size", f"${size_usd:,.2f}" if size_usd else None),
        ("Held", _duration(exit_ms - entry_ms)),
        ("Spot over hold", f"{exit_spot / entry_spot - 1:+.2%}" if entry_spot else None),
        ("Strike from spot", f"{min(levels, key=lambda s: abs(s - exit_spot)) / exit_spot - 1:+.2%}" if exit_spot else None),
        ("Barrier touched", ("yes" if touched_at is not None else "no") if option_type in _BARRIERS else None),
        ("Expires", datetime.fromisoformat(expiry).astimezone(UTC).strftime("%b %d %Y %H:%M UTC") if expiry else None),
    ]
    rows = [(label, value) for label, value in rows if value is not None]
    canvas.rect((_SIDE_X, top, _W - 24, bottom), fill=_PANEL, radius=8)
    canvas.text(_SIDE_X + 18, top + 18, "TRADE READ", _font(12, bold=True), _MUTED)
    row_y = top + 50
    row_h = min(40.0, (bottom - row_y - 8) / max(len(rows), 1))
    label_f, value_f = _font(13), _font(15, bold=True)
    for label, value in rows:
        canvas.text(_SIDE_X + 18, row_y, label, label_f, _MUTED, "lm")
        canvas.text(_W - 42, row_y, value, value_f, _INK, "rm")
        row_y += row_h

    # Footer.
    canvas.text(left, _H - 26, f"Spot: Binance {asset.upper()}USDT {interval} closes · times UTC · shaded band = hold",
                _font(12), _MUTED, "lm")
    canvas.text(_W - 24, _H - 26, "TurtleQuant", _font(12, bold=True), _MUTED, "rm")
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
