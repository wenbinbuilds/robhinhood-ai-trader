"""Generate short, silent README demos from synthetic terminal scenes.

The animations contain no provider calls, account state, or private runtime
files. They are documentation assets, not captured performance evidence.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
WIDTH, HEIGHT = 1200, 675
BACKGROUND = "#0B1020"
TITLE_BAR = "#151C31"
FOREGROUND = "#E6EDF3"
MUTED = "#8B949E"
MAIZE = "#FFCB05"
GREEN = "#3FB950"
RED = "#F85149"
BLUE = "#58A6FF"


def _font(size: int, *, bold: bool = False):
    candidates = [
        Path("/System/Library/Fonts/SFNSMono.ttf"),
        Path("/System/Library/Fonts/Menlo.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size, index=1 if bold and path.suffix == ".ttc" else 0)
    return ImageFont.load_default()


FONT = _font(24)
SMALL = _font(18)
TITLE = _font(22, bold=True)


def _line_color(line: str) -> str:
    if line.startswith("MODE") or line.startswith("SAFETY") or "SYNTHETIC" in line:
        return MAIZE
    if "BLOCKED" in line or "REJECTED" in line or "TOO_WIDE" in line:
        return RED
    if "CONNECTED" in line or "WATCHLIST" in line or "TARGET_HIT" in line or "APPROVED" in line:
        return GREEN
    if line.startswith("$"):
        return BLUE
    if line.startswith("  "):
        return MUTED
    return FOREGROUND


def _frame(title: str, badge: str, lines: list[str], progress: float, cursor: bool) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((22, 20, WIDTH - 22, HEIGHT - 20), radius=18, fill="#0D1117", outline="#30363D", width=2)
    draw.rounded_rectangle((22, 20, WIDTH - 22, 72), radius=18, fill=TITLE_BAR)
    draw.rectangle((22, 52, WIDTH - 22, 72), fill=TITLE_BAR)
    for x, color in ((52, "#FF5F56"), (82, "#FFBD2E"), (112, "#27C93F")):
        draw.ellipse((x - 8, 38 - 8, x + 8, 38 + 8), fill=color)
    draw.text((145, 31), title, font=TITLE, fill=FOREGROUND)
    badge_box = draw.textbbox((0, 0), badge, font=SMALL)
    badge_width = badge_box[2] - badge_box[0] + 28
    draw.rounded_rectangle((WIDTH - badge_width - 48, 29, WIDTH - 48, 62), radius=8, fill="#3A2F00")
    draw.text((WIDTH - badge_width - 34, 34), badge, font=SMALL, fill=MAIZE)

    y = 92
    for line in lines:
        draw.text((55, y), line, font=FONT, fill=_line_color(line))
        y += 32
    if cursor and y < HEIGHT - 58:
        draw.rectangle((55, y + 3, 70, y + 28), fill=FOREGROUND)

    draw.rectangle((55, HEIGHT - 35, WIDTH - 55, HEIGHT - 27), fill="#21262D")
    draw.rectangle((55, HEIGHT - 35, 55 + int((WIDTH - 110) * progress), HEIGHT - 27), fill=MAIZE)
    return image


def _animate(path: Path, title: str, badge: str, lines: list[str], reveal: list[int]) -> None:
    frames = []
    durations = []
    for index, count in enumerate(reveal):
        frames.append(_frame(title, badge, lines[:count], (index + 1) / len(reveal), index % 2 == 0))
        durations.append(650)
    frames.append(_frame(title, badge, lines, 1.0, False))
    durations.append(2200)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=True,
    )


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    research = [
        "$ python runner.py --once",
        "MODE: SHADOW_TRADING",
        "SAFETY: SHADOW ONLY | LIVE EXECUTION BLOCKED",
        "",
        "SNAPSHOT REFRESH: STARTED",
        "DATA SOURCE: ROBINHOOD_MCP",
        "MCP STATUS: CONNECTED",
        "SCANNER: AI_INTRADAY_MOMENTUM_V1",
        "SCANNER RESULTS: 3 EQUITY CANDIDATES",
        "",
        "ACME   technical=.780  combined=.714  -> WATCHLIST",
        "BETA   technical=.640  combined=.628  -> WATCHLIST",
        "GAMMA  hard gate: SPREAD_TOO_WIDE     -> REJECTED",
        "",
        "ANALYSIS STATUS: NO_TRADE",
        "NO BROKER ORDER ATTEMPTED",
    ]
    lifecycle = [
        '$ python -c "...run_deterministic_shadow_simulation..."',
        "MODE: SHADOW_TRADING",
        "SAFETY: SHADOW ONLY | LIVE EXECUTION BLOCKED",
        "DATA: SYNTHETIC / NETWORK-FREE",
        "",
        "ACME slow=.660 live=.880 combined=.756",
        "confirmation: 2/2 -> TRADE_READY",
        "geometry: VALID | deterministic risk: APPROVED",
        "[POSITION ENTRY] ACME  SHADOW_POSITION",
        "  simulated entry=101.0605",
        "FastPositionWatcher -> TARGET_HIT",
        "[POSITION EXIT] ACME  reason=TARGET_HIT",
        "",
        "RISKY high alpha -> REJECTED: DAILY_LOSS_LIMIT",
        "real_order_operations=0",
    ]
    _animate(
        ASSETS / "research-scan.gif",
        "Robinhood AI Trader — Research Scan",
        "ILLUSTRATIVE • SYNTHETIC DATA",
        research,
        [1, 3, 5, 7, 9, 11, 13, 15, 16],
    )
    _animate(
        ASSETS / "shadow-trade.gif",
        "Robinhood AI Trader — Shadow Lifecycle",
        "DETERMINISTIC LOCAL SIMULATION",
        lifecycle,
        [1, 4, 6, 8, 9, 11, 12, 14, 15],
    )
    print(ASSETS / "research-scan.gif")
    print(ASSETS / "shadow-trade.gif")


if __name__ == "__main__":
    main()
