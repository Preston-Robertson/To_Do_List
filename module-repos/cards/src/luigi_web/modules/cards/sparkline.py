"""Dependency-free SVG price sparklines."""
from __future__ import annotations

from typing import Sequence


def sparkline(
    series: Sequence[tuple[int | float | None, int | float | None]],
    *,
    width: int = 120,
    height: int = 32,
    stroke: str = "#67c5a1",
    stroke_foil: str = "#f0b35a",
    baseline: str = "#39434f",
) -> str:
    safe_width = min(max(int(width), 32), 1200)
    safe_height = min(max(int(height), 16), 400)
    if not series:
        return _empty(safe_width, safe_height, baseline)
    values = [float(value) for pair in series for value in pair if value is not None]
    if not values:
        return _empty(safe_width, safe_height, baseline)
    low, high = min(values), max(values)
    if high == low:
        high = low + 1
    padding = 2
    inner_width = safe_width - padding * 2
    inner_height = safe_height - padding * 2

    def points(channel: int) -> str:
        rendered: list[str] = []
        for index, pair in enumerate(series):
            value = pair[channel]
            if value is None:
                continue
            x = padding + (index / max(1, len(series) - 1)) * inner_width
            y = padding + inner_height - ((float(value) - low) / (high - low)) * inner_height
            rendered.append(f"{x:.1f},{y:.1f}")
        return " ".join(rendered)

    regular = points(0)
    foil = points(1)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {safe_width} {safe_height}" '
        f'width="{safe_width}" height="{safe_height}" preserveAspectRatio="none">',
        f'<line x1="{padding}" y1="{safe_height - padding}" '
        f'x2="{safe_width - padding}" y2="{safe_height - padding}" '
        f'stroke="{baseline}" stroke-width="0.5"/>',
    ]
    if len(foil.split()) > 1:
        parts.append(
            f'<polyline fill="none" stroke="{stroke_foil}" stroke-width="1.2" '
            f'stroke-dasharray="2 2" points="{foil}"/>'
        )
    if len(regular.split()) > 1:
        parts.append(
            f'<polyline fill="none" stroke="{stroke}" stroke-width="1.4" points="{regular}"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _empty(width: int, height: int, baseline: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}"><line x1="2" y1="{height // 2}" '
        f'x2="{width - 2}" y2="{height // 2}" stroke="{baseline}" '
        f'stroke-width="1" stroke-dasharray="2 2"/></svg>'
    )