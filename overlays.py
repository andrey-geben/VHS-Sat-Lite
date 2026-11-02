"""Overlay builder for FFmpeg filter_complex.

Supports:
- ticker (drawtext running line, textfile+reload, no restart needed for text)
- PiP (picture-in-picture) with static image (PNG/JPEG)
- preview (announcement of next file, folder channels only)

Windows notes:
- Path to font/image inside filter_complex must escape the colon: C\\:/Windows/Fonts/arial.ttf
- drawtext/overlay require re-encoding. If source was '-c copy', we force libx264.

Chain order: SAR normalize -> PiP -> ticker -> preview -> vout.
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
    """Kept for backward compatibility; ticker/preview now use textfile=..."""
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
    ap = channel.get("_active_pip")
    if ap and ap.get("file"):
        return APP_ROOT / "data" / "pip" / ap["file"]
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
    if channel.get("_active_pip"):
        return _pip_input_path(channel).is_file()
    if not channel.get("pip_enabled"):
        return False
    if (channel.get("pip_mode") or "none").lower() != "image":
        return False
    return _pip_input_path(channel).is_file()


def _has_preview(channel: dict) -> bool:
    """True if folder-channel preview (announcement) should be drawn."""
    if (channel.get("source_type") or "folder") != "folder":
        return False
    if not channel.get("preview_enabled"):
        return False
    cid = channel.get("id")
    if not cid:
        return False
    return True


def _preview_textfile(channel: dict) -> Path:
    return APP_ROOT / "data" / "preview" / f"{channel['id']}.txt"


def build_overlay_filter(channel: dict) -> str | None:
    """Return filter_complex string for [0:v] -> [vout].

    If PiP is active, the caller must have added the PNG as input #1
    via overlay_extra_inputs() BEFORE this filter is evaluated.

    Chain order: SAR normalize -> PiP -> ticker -> preview -> vout.
    """
    ticker_on = _has_ticker(channel)
    pip_on = _has_pip(channel)
    preview_on = _has_preview(channel)

    if not ticker_on and not pip_on and not preview_on:
        return None

    chain_parts: list[str] = []
    # Normalize main stream SAR to 1:1 (fixes PiP stretching on anamorphic PAL)
    chain_parts.append("[0:v]scale=iw*sar:ih,setsar=1[0v]")
    last_label = "0v"

    # --- PiP image ---
    if pip_on:
        w = int(channel.get("pip_width") or 320)
        x_expr, y_expr = _pip_position_expr(channel)
        chain_parts.append(f"[1:v]scale={w}:-1[pipv]")
        chain_parts.append(
            f"[{last_label}][pipv]overlay={x_expr}:{y_expr}[v1]"
        )
        last_label = "v1"

    # --- Ticker (running line) ---
    if ticker_on:
        t = channel["_active_ticker"]
        textfile = t.get("_file")
        if not textfile or not Path(textfile).is_file():
            events.warn("overlays",
                        f"ticker file missing for channel {channel.get('id')}")
        else:
            font = _find_font()
            if not font:
                events.warn("overlays",
                            "no usable font found for drawtext — ticker disabled")
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
                chain_parts.append(f"[{last_label}]{drawtext}[vtick]")
                last_label = "vtick"

    # --- Preview (announcement of next file, folder channels only) ---
    if preview_on:
        font = _find_font()
        if font:
            preview_file = _preview_textfile(channel)
            try:
                if not preview_file.is_file():
                    preview_file.parent.mkdir(parents=True, exist_ok=True)
                    preview_file.write_text("", encoding="utf-8")
            except OSError:
                pass
            font_esc = _escape_filter_path(font)
            file_esc = _escape_filter_path(str(preview_file))
            drawtext = (
                f"drawtext=fontfile='{font_esc}'"
                f":textfile='{file_esc}'"
                f":reload=1"
                f":fontsize=22"
                f":fontcolor=yellow"
                f":box=1:boxcolor=black@0.6:boxborderw=6"
                f":x=w-tw-24"
                f":y=h-th-24"
            )
            chain_parts.append(f"[{last_label}]{drawtext}[vprev]")
            last_label = "vprev"

    # Final label
    if last_label != "vout":
        chain_parts.append(f"[{last_label}]null[vout]")

    return ";".join(chain_parts)


def overlay_output_args() -> list[str]:
    return ["-map", "[vout]", "-map", "0:a?"]


def overlay_extra_inputs(channel: dict) -> list[str]:
    """Extra '-i' args for PiP image.
    MUST be inserted right after the main '-i <url>' in the command."""
    if not _has_pip(channel):
        return []
    return ["-i", str(_pip_input_path(channel))]


def overlay_forces_encode(channel: dict) -> bool:
    return _has_ticker(channel) or _has_pip(channel) or _has_preview(channel)