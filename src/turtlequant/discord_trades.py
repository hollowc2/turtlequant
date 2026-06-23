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


class DiscordTrades:
    def __init__(self, state_dir: Path, mode: str) -> None:
        setting = os.getenv("DISCORD_TRADES", "all").lower()
        self.enabled = setting == "all" or (setting == "live" and mode == "live")
        webhook_file = Path(os.getenv("DISCORD_WEBHOOK_FILE", "/run/secrets/discord-webhook"))
        self.webhook = webhook_file.read_text().strip() if self.enabled and webhook_file.exists() else ""
        self.links_file = state_dir / "turtlequant-discord.json"

    def chart(self, frame, title: str, entry_ms: int, exit_ms: int | None = None) -> bytes | None:
        if frame is None or len(frame) < 2:
            return None
        values = [float(value) for value in frame["close"]]
        width, height, pad = 900, 420, 55
        image = Image.new("RGB", (width, height), "#111827")
        draw = ImageDraw.Draw(image)
        low, high = min(values), max(values)
        span = high - low or 1.0
        points = [
            (
                pad + index * (width - 2 * pad) / (len(values) - 1),
                height - pad - (value - low) * (height - 2 * pad) / span,
            )
            for index, value in enumerate(values)
        ]
        draw.line(points, fill="#38bdf8", width=3)
        draw.text((pad, 18), title, fill="white")
        draw.text((pad, height - 35), f"{low:,.2f}", fill="#94a3b8")
        draw.text((width - 140, 18), f"{high:,.2f}", fill="#94a3b8")
        times = [int(ts.timestamp() * 1000) for ts in frame["open_time"]]
        for timestamp, color, label in ((entry_ms, "#facc15", "ENTRY"), (exit_ms, "#c084fc", "EXIT")):
            if timestamp is None:
                continue
            index = min(range(len(times)), key=lambda i: abs(times[i] - timestamp))
            x, y = points[index]
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=color)
            draw.text((x + 8, y - 8), label, fill=color)
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()

    def send(self, key: str, content: str, chart: bytes | None = None, *, remember: bool = False) -> None:
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
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                "Content-Type: application/json\r\n\r\n"
                f"{json.dumps(payload)}\r\n--{boundary}\r\n"
                "Content-Disposition: form-data; name=\"files[0]\"; filename=\"trade.png\"\r\n"
                "Content-Type: image/png\r\n\r\n"
            ).encode() + chart + f"\r\n--{boundary}--\r\n".encode()
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
