"""Square cover thumbnail: lead image, center-cropped, with the publication's icon badged top-left.

Square because neither Kobo nor Instapaper documents the list-thumbnail crop; a square
survives either a crop or a letterbox. With no lead image, the icon and name fill the tile.
"""
import io, os, re
from urllib.parse import urljoin
from PIL import Image, ImageDraw, ImageFont

from . import config
from .safety import safe_fetch

SIZE = 600
MAX_CACHED_ICONS = 200  # the icon cache holds at most this many files; the oldest are evicted first
BADGE = 0.5  # badge fills the upper-left quarter; smaller was unreadable in the e-reader's list view
# Tried in order after cover.font: common bold fonts on macOS and Linux, then Pillow's built-in font.
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _get(url, limit=3_000_000):
    return safe_fetch(url, max_bytes=limit, deadline_s=15)  # public http(s) only, capped


def _prune_cache(cache, keep=MAX_CACHED_ICONS):
    """Bound the icon cache: keep at most `keep` files, evicting the oldest by modification time."""
    try:
        files = sorted((os.path.join(cache, n) for n in os.listdir(cache)), key=os.path.getmtime)
        for old in files[:max(len(files) - keep, 0)]:
            os.remove(old)
    except OSError:
        pass


def site_icon(site):
    """Largest icon a site advertises (apple-touch-icon first), cached per host. None if nothing usable."""
    host = re.sub(r"^https?://", "", site).split("/")[0]
    cache = config.get().paths.icon_cache  # one small PNG per publication host (bounded by MAX_CACHED_ICONS)
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, host + ".png")
    if os.path.exists(path):
        return Image.open(path).convert("RGBA")
    candidates = []
    try:
        page, final = _get(f"https://{host}/", 500_000)
        for tag in re.findall(rb"<link[^>]+>", page):
            t = tag.decode("utf-8", "ignore")
            if re.search(r'rel="[^"]*(apple-touch-icon|icon)[^"]*"', t, re.I):
                href = re.search(r'href="([^"]+)"', t)
                size = re.search(r'sizes="(\d+)', t)
                rank = (2 if "apple-touch" in t else 1, int(size.group(1)) if size else 0)
                if href:
                    candidates.append((rank, urljoin(final, href.group(1).replace("&amp;", "&"))))
    except Exception:
        pass
    candidates.sort(reverse=True)
    for _, url in candidates + [((0, 0), f"https://{host}/favicon.ico")]:
        try:
            data, _ = _get(url)
            im = Image.open(io.BytesIO(data))
            if hasattr(im, "n_frames") and getattr(im, "format", "") == "ICO":
                im.size = max(im.info.get("sizes", [im.size]))
            im = im.convert("RGBA")
            if min(im.size) >= 32:
                im.save(path)
                _prune_cache(cache)
                return im
        except Exception:
            continue
    return None


def _square(im):
    im = im.convert("RGB")
    s = min(im.size)
    left, top = (im.width - s) // 2, (im.height - s) // 2
    return im.crop((left, top, left + s, top + s)).resize((SIZE, SIZE), Image.LANCZOS)


def _trim(icon):
    """Crop built-in padding (transparent or near-white) so the mark fills the badge, then upscale."""
    flat = Image.new("RGB", icon.size, "white")
    flat.paste(icon, mask=icon.getchannel("A"))
    ink = flat.convert("L").point(lambda v: 255 if v < 235 else 0)
    box = ink.getbbox()
    ic = icon.crop(box) if box else icon.copy()
    target = int(SIZE * BADGE)
    if max(ic.size) < target:  # small favicons: upscale so thumbnail() fills the badge
        k = target / max(ic.size)
        ic = ic.resize((round(ic.width * k), round(ic.height * k)), Image.LANCZOS)
    return ic


def _badge(icon):
    side = int(SIZE * BADGE)
    pad = side // 14
    card = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle((0, 0, side - 1, side - 1), radius=side // 6,
                                           fill=(255, 255, 255, 235), outline=(0, 0, 0, 90), width=2)
    ic = _trim(icon)
    ic.thumbnail((side - 2 * pad, side - 2 * pad), Image.LANCZOS)
    card.alpha_composite(ic, ((side - ic.width) // 2, (side - ic.height) // 2))
    return card


def _font(size):
    for path in (config.get().cover.font, *FONT_CANDIDATES):
        if path:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _initials(name):
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name) if w.lower() not in {"the", "of", "and", "a"}]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def make_cover(lead_jpeg, site, publication):
    """lead_jpeg: bytes of the first article image or None. Returns JPEG bytes."""
    icon = site_icon(site) if site else None
    if lead_jpeg:
        tile = _square(Image.open(io.BytesIO(lead_jpeg))).convert("RGBA")
        badge = _badge(icon) if icon else None
        if badge is None:  # text badge from initials
            side = int(SIZE * BADGE)
            badge = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            d = ImageDraw.Draw(badge)
            d.rounded_rectangle((0, 0, side - 1, side - 1), radius=side // 6, fill=(255, 255, 255, 235))
            f = _font(side // 2)
            d.text((side / 2, side / 2), _initials(publication), font=f, fill="black", anchor="mm")
        m = SIZE // 50
        tile.alpha_composite(badge, (m, m))  # top-left: a bottom-right badge gets cropped away in the e-reader's list view
    else:
        tile = Image.new("RGBA", (SIZE, SIZE), (238, 238, 238, 255))
        if icon:
            ic = icon.copy(); ic.thumbnail((SIZE // 2, SIZE // 2), Image.LANCZOS)
            tile.alpha_composite(ic, ((SIZE - ic.width) // 2, SIZE // 8))
        d = ImageDraw.Draw(tile)
        f = _font(44)
        d.multiline_text((SIZE / 2, SIZE * 0.82), publication[:40], font=f, fill="black", anchor="mm", align="center")
    out = io.BytesIO()
    tile.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()
