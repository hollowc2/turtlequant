"""Small Discord webhook sender for confirmed trade fills."""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageDraw


def _price_y(value: float, low: float, span: float, height: int, pad: int) -> float:
    return height - pad - (value - low) * (height - 2 * pad) / span


class DiscordTrades:
    def __init__(self, state_dir: Path, mode: str) -> None:
        setting = os.getenv("DISCORD_TRADES", "all").lower()
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

    def chart(
        self,
        frame,
        title: str,
        entry_ms: int,
        exit_ms: int | None = None,
        *,
        strike: float | None = None,
        model_prob: float | None = None,
        entry_price: float | None = None,
        exit_price: float | None = None,
        edge: float | None = None,
        sigma: float | None = None,
        pnl: float | None = None,
        expiry: str | None = None,
        yes_above_strike: bool = True,
    ) -> bytes | None:
        if frame is None or len(frame) < 2:
            return None
        values = [float(value) for value in frame["close"]]
        width, height, pad = 1040, 500, 58
        chart_right = 760
        image = Image.new("RGB", (width, height), "#111827")
        draw = ImageDraw.Draw(image)
        anchors = values + ([strike] if strike else [])
        low, high = min(anchors), max(anchors)
        margin = (high - low) * 0.08 or max(high * 0.01, 1.0)
        low, high = low - margin, high + margin
        span = high - low or 1.0
        points = [
            (
                pad + index * (chart_right - pad) / (len(values) - 1),
                _price_y(value, low, span, height, pad),
            )
            for index, value in enumerate(values)
        ]
        if strike is not None:
            y = _price_y(strike, low, span, height, pad)
            if yes_above_strike:
                yes_box = (pad, pad, chart_right, y)
                no_box = (pad, y, chart_right, height - pad)
                yes_label_y, no_label_y = pad + 8, height - pad - 24
            else:
                no_box = (pad, pad, chart_right, y)
                yes_box = (pad, y, chart_right, height - pad)
                no_label_y, yes_label_y = pad + 8, height - pad - 24
            draw.rectangle(yes_box, fill="#123026")
            draw.rectangle(no_box, fill="#2a1720")
            draw.line((pad, y, chart_right, y), fill="#f97316", width=2)
            draw.text((pad + 8, y - 18), f"STRIKE ${strike:,.0f}", fill="#fdba74")
            draw.text((chart_right - 72, yes_label_y), "YES", fill="#86efac")
            draw.text((chart_right - 64, no_label_y), "NO", fill="#fca5a5")
        draw.line(points, fill="#38bdf8", width=3)
        draw.text((pad, 18), title, fill="white")
        draw.text((pad, height - 35), f"{low:,.2f}", fill="#94a3b8")
        draw.text((chart_right - 100, 18), f"{high:,.2f}", fill="#94a3b8")
        times = [int(ts.timestamp() * 1000) for ts in frame["open_time"]]
        for timestamp, color, label in (
            (entry_ms, "#facc15", "ENTRY"),
            (exit_ms, "#c084fc", "EXIT"),
        ):
            if timestamp is None:
                continue
            index = min(range(len(times)), key=lambda i: abs(times[i] - timestamp))
            x, y = points[index]
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=color)
            draw.text((x + 8, y - 8), label, fill=color)
        rows = [
            ("Entry", f"{entry_price:.3f}" if entry_price is not None else None),
            ("Exit", f"{exit_price:.3f}" if exit_price is not None else None),
            ("Model", f"{model_prob:.1%}" if model_prob is not None else None),
            ("Edge", f"{edge:+.1%}" if edge is not None else None),
            ("IV", f"{sigma:.1%}" if sigma is not None else None),
            ("P&L", f"${pnl:+.2f}" if pnl is not None else None),
            ("Expiry", expiry[:16] if expiry else None),
        ]
        draw.rectangle((790, 70, width - 35, height - 70), outline="#334155", width=1)
        draw.text((815, 96), "TRADE READ", fill="#e5e7eb")
        y = 135
        for label, value in rows:
            if value is None:
                continue
            color = (
                "#86efac"
                if label == "P&L" and pnl is not None and pnl >= 0
                else "#e5e7eb"
            )
            if label == "P&L" and pnl is not None and pnl < 0:
                color = "#fca5a5"
            draw.text((815, y), label, fill="#94a3b8")
            draw.text((900, y), value, fill=color)
            y += 36
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

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
