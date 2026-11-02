"""Overlay builder for FFmpeg filter_complex.

Supports:
- ticker (drawtext running line)
- PiP (picture-in-picture) with static image (PNG/JPEG)

Windows notes:
- Path to font/image inside filter_complex must escape the colon: C\\:/Windows/Fonts/arial.ttf
- Text inside drawtext must escape: \\ : ' , %
- drawtext/overlay require re-encoding. If source was '-c copy', we force libx264.
"""
from __future__ import annotations

from pathlib import Path

from event_writer import events
from frozen_utils import APP_ROOT


_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "C:/Windows/Fonts/verdana.ttf",
]


def _find_font() -> str | None:
    for p in _FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def _escape_filter_path(p: str) -> str:
    return p.replace("\\", "/").replace(":", r"\:")


def _escape_drawtext(text: str) -> str:
    out = text
    out = out.replace("\\", r"\\")
    out = out.replace(":", r"\:")
    out = out.replace("'", r"\'")
    out = out.replace("%", r"\%")
    out = out.replace(",", r"\,")
    out = out.replace("[", r"\[")
    out = out.replace("]", r"\]")
    out = out.replace(";", r"\;")
    return out


def _pip_input_path(channel: dict) -> Path:
    return APP_ROOT / "data" / "pip" / f"{channel['id']}.png"


def _pip_position_expr(channel: dict) -> tuple[str, str]:
    pos = (channel.get("pip_position") or "top-right").strip()
    m = int(channel.get("pip_margin") or 16)
    if pos == "top-left":
        return (str(m), str(m))
    if pos == "bottom-left":
        return (str(m), f"main_h-overlay_h-{m}")
    if pos == "bottom-right":
        return (f"main_w-overlay_w-{m}", f"main_h-overlay_h-{m}")
    return (f"main_w-overlay_w-{m}", str(m))


def _has_ticker(channel: dict) -> bool:
    t = channel.get("_active_ticker")
    return bool(t and (t.get("text") or "").strip())


def _has_pip(channel: dict) -> bool:
    if not channel.get("pip_enabled"):
        return False
    if (channel.get("pip_mode") or "none").lower() != "image":
        return False
    return _pip_input_path(channel).is_file()


def build_overlay_filter(channel: dict) -> str | None:
    """Return filter_complex string for [0:v] -> [vout].

    If PiP is active, the caller must have added the PNG as input #1
    via overlay_extra_inputs() BEFORE this filter is evaluated.
    """
    ticker_on = _has_ticker(channel)
    pip_on = _has_pip(channel)

    if not ticker_on and not pip_on:
        return None

    chain_parts: list[str] = []
    # Normalize main stream SAR to 1:1 (fixes PiP stretching on anamorphic PAL)
    chain_parts.append("[0:v]scale=iw*sar:ih,setsar=1[0v]")
    last_label = "0v"

    if pip_on:
        w = int(channel.get("pip_width") or 320)
        x_expr, y_expr = _pip_position_expr(channel)
        chain_parts.append(f"[1:v]scale={w}:-1[pipv]")
        chain_parts.append(
            f"[{last_label}][pipv]overlay={x_expr}:{y_expr}[v1]"
        )
        last_label = "v1"

    if ticker_on:
        t = channel["_active_ticker"]
        textfile = t.get("_file")
        if not textfile or not Path(textfile).is_file():
            events.warn("overlays", f"ticker file missing for channel {channel.get('id')}")
            if not pip_on:
                return None
        else:
            font = _find_font()
            if not font:
                events.warn("overlays", "no usable font found for drawtext — ticker disabled")
                if not pip_on:
                    return None
            else:
                font_esc = _escape_filter_path(font)
                file_esc = _escape_filter_path(textfile)
                speed = int(t.get("speed") or 100)
                fontsize = int(t.get("fontsize") or 24)
                fontcolor = (t.get("color") or "white").strip()
                bgcolor = (t.get("bgcolor") or "black@0.5").strip()
                drawtext = (
                    f"drawtext=fontfile='{font_esc}'"
                    f":textfile='{file_esc}'"
                    f":reload=1"
                    f":fontsize={fontsize}"
                    f":fontcolor={fontcolor}"
                    f":box=1:boxcolor={bgcolor}:boxborderw=6"
                    f":x=w-mod(t*{speed}\\,w+tw)"
                    f":y=h-th-24"
                )
                chain_parts.append(f"[{last_label}]{drawtext}[vout]")
                last_label = "vout"

    if last_label != "vout":
        chain_parts.append(f"[{last_label}]null[vout]")

    return ";".join(chain_parts)


def overlay_output_args() -> list[str]:
    return ["-map", "[vout]", "-map", "0:a?"]


def overlay_extra_inputs(channel: dict) -> list[str]:
    """Extra '-i' args for PiP image. MUST be inserted right after the main '-i <url>'."""
    if not _has_pip(channel):
        return []
    return ["-i", str(_pip_input_path(channel))]


def overlay_forces_encode(channel: dict) -> bool:
    return _has_ticker(channel) or _has_pip(channel)