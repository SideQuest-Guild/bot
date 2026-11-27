"""Draws a gear sim's results as a PNG bar chart that reads at a glance: better to the right, worse to the left.

Each :class:`Panel` is one gear option; inside it each :class:`MetricGroup` (threat, damage taken, DPS...) has one
:class:`Bar` per fight. Bars are already oriented by the caller (positive = better for the player, so less damage
taken points right) and coloured by :data:`Effect`, with grey for changes inside the sim's margin of error. Every
option shares one scale, so bar lengths compare across options. Pillow's bundled font has no arrows or dashes, so
all chart text must be plain ASCII.
"""

from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Literal

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from collections.abc import Sequence

Effect = Literal["better", "worse", "same"]


@dataclass(frozen=True)
class Bar:
    """One fight's change: ``value`` is the % change oriented so positive is better; ``text`` is drawn beside it."""

    label: str
    value: float
    text: str
    effect: Effect


@dataclass(frozen=True)
class MetricGroup:
    """One metric's bars, under a title like ``Threat``."""

    title: str
    bars: list[Bar]


@dataclass(frozen=True)
class Panel:
    """One gear option: a title like ``Quel'Serrar vs Eater of the Dead`` and its metric groups."""

    title: str
    groups: list[MetricGroup]


SCALE = 2
"""Drawn at twice the layout size so the chart stays sharp when Discord scales it down."""
WIDTH = 640
PAD = 20
LABEL_WIDTH = 150
TEXT_ROOM = 90
"""Room kept beside the longest bar for its value text."""
BAR_HEIGHT = 18
ROW_HEIGHT = 28
GROUP_TITLE_HEIGHT = 26
PANEL_TITLE_HEIGHT = 30
AXIS_HEADER_HEIGHT = 22
PANEL_GAP = 22
MIN_SCALE_PCT = 2.0
"""The smallest full-bar scale, so a 0.3% change doesn't fill the chart and look huge."""
MIN_BAR_WIDTH = 3

BACKGROUND = (43, 45, 49)
TEXT = (242, 243, 245)
MUTED = (181, 186, 193)
AXIS = (90, 93, 102)
COLORS: dict[Effect, tuple[int, int, int]] = {
    "better": (35, 165, 90),
    "worse": (242, 63, 67),
    "same": (128, 132, 142),
}


def _font(size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.load_default(size=size * SCALE)
    if not isinstance(font, ImageFont.FreeTypeFont):  # pragma: no cover - Pillow built without FreeType
        msg = "Pillow needs FreeType to draw sim charts"
        raise TypeError(msg)
    return font


def _height(panels: Sequence[Panel]) -> int:
    height = PAD * 2 - PANEL_GAP
    for panel in panels:
        height += PANEL_TITLE_HEIGHT + AXIS_HEADER_HEIGHT + PANEL_GAP
        height += sum(GROUP_TITLE_HEIGHT + ROW_HEIGHT * len(group.bars) for group in panel.groups)
    return height


def render_chart(panels: Sequence[Panel]) -> bytes:
    """``panels`` drawn top to bottom as one PNG."""
    title_font, group_font, label_font = _font(18), _font(14), _font(14)
    image = Image.new("RGB", (WIDTH * SCALE, _height(panels) * SCALE), BACKGROUND)
    draw = ImageDraw.Draw(image)

    bar_left = PAD + LABEL_WIDTH
    center = (bar_left + WIDTH - PAD) / 2
    half = (WIDTH - PAD - bar_left) / 2 - TEXT_ROOM
    scale = max([MIN_SCALE_PCT, *(abs(bar.value) for p in panels for g in p.groups for bar in g.bars)])

    y = PAD
    for panel in panels:
        draw.text((PAD * SCALE, y * SCALE), panel.title, font=title_font, fill=TEXT)
        y += PANEL_TITLE_HEIGHT
        header_mid = (y + AXIS_HEADER_HEIGHT / 2) * SCALE
        draw.text(((center - 8) * SCALE, header_mid), "< worse", font=label_font, fill=MUTED, anchor="rm")
        draw.text(((center + 8) * SCALE, header_mid), "better >", font=label_font, fill=MUTED, anchor="lm")
        y += AXIS_HEADER_HEIGHT
        axis_top = y
        for group in panel.groups:
            draw.text((PAD * SCALE, y * SCALE), group.title, font=group_font, fill=MUTED)
            y += GROUP_TITLE_HEIGHT
            for bar in group.bars:
                mid = y + ROW_HEIGHT / 2 - 4
                draw.text((PAD * SCALE, mid * SCALE), bar.label, font=label_font, fill=TEXT, anchor="lm")
                length = bar.value / scale * half
                if abs(length) < MIN_BAR_WIDTH:
                    length = MIN_BAR_WIDTH if bar.value >= 0 else -MIN_BAR_WIDTH
                left, right = sorted((center, center + length))
                top, bottom = mid - BAR_HEIGHT / 2, mid + BAR_HEIGHT / 2
                draw.rounded_rectangle(
                    (left * SCALE, top * SCALE, right * SCALE, bottom * SCALE),
                    radius=3 * SCALE,
                    fill=COLORS[bar.effect],
                )
                if length >= 0:
                    draw.text(((right + 6) * SCALE, mid * SCALE), bar.text, font=label_font, fill=TEXT, anchor="lm")
                else:
                    draw.text(((left - 6) * SCALE, mid * SCALE), bar.text, font=label_font, fill=TEXT, anchor="rm")
                y += ROW_HEIGHT
        draw.line((center * SCALE, axis_top * SCALE, center * SCALE, (y - 4) * SCALE), fill=AXIS, width=SCALE)
        y += PANEL_GAP

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
