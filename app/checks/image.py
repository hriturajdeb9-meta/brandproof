"""Image checks with Pillow: colours against the palette and text contrast."""
from __future__ import annotations

import io
from collections import Counter

from PIL import Image

from ..colour import contrast, delta_e, rgb_to_hex
from ..kit import Kit
from .common import make_finding

MAX_SIDE = 1280
OFF_PALETTE_DELTA_E = 10.0  # CIEDE2000 distance above which a colour counts as off-palette
MIN_SHARE = 0.03  # ignore colours that cover less than 3% of the image (anti-aliasing, tiny details)


def normalise_image(data: bytes) -> tuple[bytes, str, tuple[int, int]]:
    """Validate an upload, flatten transparency, shrink to MAX_SIDE and re-encode. Returns (bytes, mime, size)."""
    img = Image.open(io.BytesIO(data))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    if buf.tell() > 1_500_000:  # large photos: JPEG keeps the database small
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        return buf.getvalue(), "image/jpeg", img.size
    return buf.getvalue(), "image/png", img.size


def open_rgb(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    return img.convert("RGB")


def dominant_colours(img: Image.Image, n: int = 8, side: int = 320) -> list[tuple[tuple[int, int, int], float]]:
    """Main colours of an image with their share of pixels.

    Pixels are grouped with median-cut, then each group is represented by its most common real pixel
    colour (not the average), so anti-aliased edges don't shift a flat brand colour.
    """
    small = img.copy()
    small.thumbnail((side, side))
    q = small.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    groups: dict[int, Counter] = {}
    for idx, px in zip(_pixels(q), _pixels(small)):
        groups.setdefault(idx, Counter())[px[:3]] += 1
    total = small.size[0] * small.size[1] or 1
    merged: list[list] = []
    for idx, counter in groups.items():
        mode = counter.most_common(1)[0][0]
        share = sum(counter.values()) / total
        for m in merged:  # merge near-identical shades (e.g. several creams)
            if delta_e(m[0], mode) < 3:
                m[1] += share
                break
        else:
            merged.append([mode, share])
    return sorted(((tuple(m[0]), m[1]) for m in merged), key=lambda t: t[1], reverse=True)


def looks_like_photo(img: Image.Image) -> bool:
    tiny = img.copy()
    tiny.thumbnail((64, 64))
    colours = tiny.getcolors(maxcolors=64 * 64)
    distinct = len(colours) if colours else 64 * 64
    return distinct > 0.45 * tiny.size[0] * tiny.size[1]


def nearest_palette(kit: Kit, rgb) -> tuple[str, str, tuple[int, int, int], float] | None:
    best = None
    for name, hx, prgb in kit.palette_rgb():
        d = delta_e(rgb, prgb)
        if best is None or d < best[3]:
            best = (name, hx, prgb, d)
    return best


def check_palette(kit: Kit, img: Image.Image, is_photo: bool) -> tuple[list[dict], dict]:
    info = {"colours": [], "photo": is_photo}
    if not kit.rule("R-VIS-01") or not kit.palette_rgb():
        return [], info
    cols = dominant_colours(img)
    info["colours"] = [{"hex": rgb_to_hex(c), "share": round(s * 100, 1)} for c, s in cols]
    if is_photo:
        info["skipped"] = "Looks like a photo, so the palette check was skipped."
        return [], info
    out = []
    flagged: list[tuple[int, int, int]] = []
    for rgb, share in cols:
        if share < MIN_SHARE:
            continue
        near = nearest_palette(kit, rgb)
        if not near or near[3] <= OFF_PALETTE_DELTA_E:
            continue
        if any(delta_e(rgb, f) < 8 for f in flagged):
            continue
        flagged.append(rgb)
        hx = rgb_to_hex(rgb)
        out.append(make_finding(
            kit, "R-VIS-01", title=f"Off-palette colour {hx} ({share * 100:.0f}% of the image)",
            message=f"{hx} is not in the brand palette. Closest brand colour: {near[0]} {near[1]} (colour distance ΔE {near[3]:.0f}; 10 or less counts as a match).",
            target="image", fix={"type": "advice", "text": f"Swap {hx} for {near[0]} {near[1]} in the design."},
            evidence=[{"label": "Detected colour", "value": hx}, {"label": "Closest brand colour", "value": f"{near[0]} {near[1]}"}],
        ))
    return out, info


def _text_colours(region: Image.Image) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    cols = dominant_colours(region, 4)
    if len(cols) < 2:
        return None
    bg = cols[0][0]
    candidates = [c for c, s in cols[1:] if s >= 0.08] or [cols[1][0]]
    fg = max(candidates, key=lambda c: contrast(c, bg))
    return fg, bg


def check_text_contrast(kit: Kit, img: Image.Image, lines: list[dict]) -> tuple[list[dict], list[dict]]:
    """Measure contrast for each text line the AI located. The AI finds the text; code measures it."""
    measured = []
    if not kit.rule("R-ACC-01"):
        return [], measured
    w, h = img.size
    out = []
    for line in lines:
        box = line.get("box")
        if not box:
            continue
        y0, x0, y1, x1 = box
        px = (int(x0 / 1000 * w), int(y0 / 1000 * h), int(x1 / 1000 * w), int(y1 / 1000 * h))
        if px[2] - px[0] < 4 or px[3] - px[1] < 4:
            continue
        cols = _text_colours(img.crop(px))
        if not cols:
            continue
        fg, bg = cols
        ratio = contrast(fg, bg)
        large = (px[3] - px[1]) >= 0.07 * h
        need = 3.0 if large else 4.5
        measured.append({"text": line["text"], "fg": rgb_to_hex(fg), "bg": rgb_to_hex(bg), "ratio": round(ratio, 2),
                         "need": need, "pass": ratio >= need})
        if ratio >= need:
            continue
        suggestion = _better_colour(kit, fg, bg, need)
        fix_text = (f"Use {suggestion[0]} {suggestion[1]} for this text ({suggestion[2]:.1f} : 1 on {rgb_to_hex(bg)})."
                    if suggestion else "Use a darker text colour or a lighter background.")
        out.append(make_finding(
            kit, "R-ACC-01", title=f"Low contrast: {ratio:.1f} : 1 (needs {need:g} : 1)",
            message=f"“{line['text']}” is {rgb_to_hex(fg)} on {rgb_to_hex(bg)}. "
                    f"{'Large' if large else 'Normal'} text needs at least {need:g} : 1 under WCAG 2.2 AA.",
            quote=line["text"], target="image", box=box, method="ai+code",
            fix={"type": "advice", "text": fix_text},
            evidence=[{"label": "Text colour", "value": rgb_to_hex(fg)}, {"label": "Background", "value": rgb_to_hex(bg)},
                      {"label": "Contrast", "value": f"{ratio:.2f} : 1"}],
        ))
    return out, measured


def _better_colour(kit: Kit, fg, bg, need: float):
    options = [(name, hx, contrast(prgb, bg), delta_e(fg, prgb)) for name, hx, prgb in kit.palette_rgb()]
    options = [o for o in options if o[2] >= need]
    if not options:
        return None
    best = min(options, key=lambda o: o[3])
    return best[0], best[1], best[2]


def _pixels(img: Image.Image):
    # Pillow 12.1+ renamed getdata (removed in Pillow 14)
    return img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()
