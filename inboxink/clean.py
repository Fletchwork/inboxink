"""Turn a newsletter .eml into a clean, e-reader-friendly HTML article.

The output is published at a public URL, so cleaning is allow-list based: only a, img and plain
structural tags survive, links survive only as clean public http(s) URLs (trackers decoded offline
or resolved one hop, token-shaped params stripped, account/login pages and the reader's own
addresses dropped). Images are re-encoded as baseline JPEGs (flattened on white, screen-width)
and served from our own host: Instapaper/Kobo drop extensionless or transparent sender-CDN images.
"""
import base64, email, html, io, json, re
from email import policy
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup, Comment, Declaration, Doctype, ProcessingInstruction
from PIL import Image
import segno
from . import config
from .adstrip import strip_ads
from .cover import make_cover
from .safety import clean_query, is_account_link, is_tracker, safe_fetch

DROP_TEXT = re.compile(r"unsubscribe|manage (your )?(subscription|preferences)|update (your )?preferences|"
                       r"email preferences|view in browser|view online|forward(ed)? (this|to a friend)|"
                       r"refer a friend|share this|disable", re.I)
DROP_HREF = re.compile(r"unsubscribe|disable_email|/action/|preferences|submitLike|app-link/post|"
                       r"utm_source=podcast-email|/subscribe\b|/signup", re.I)
MAX_IMAGES = 40  # per issue; bounds fetch time and KV writes
MAX_RESOLVE = 40  # opaque tracker links resolved per issue (each one is a click attributed to the reader)
Image.MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard
# Pages are public: only these tags/attributes survive. Everything active or embeddable goes.
DANGEROUS = ["script", "style", "head", "meta", "link", "title", "noscript", "iframe", "frame", "frameset",
             "form", "input", "button", "select", "textarea", "svg", "math", "object", "embed", "applet",
             "video", "audio", "source", "track", "canvas", "base", "template", "picture"]
KEEP_ATTRS = {"a": ("href",), "img": ("src", "alt")}
MIN_SIDE = 48  # smaller is an icon or spacer
# Sizing attributes don't survive the allow-list above, and Instapaper shows every image at
# column width, so a 48px thumbnail or a social icon arrives as a full-screen banner on the
# e-reader. Decoration is therefore dropped instead of sized. Thresholds come from an audit of a
# batch of real issues. Chosen so every dropped image was chrome (icons, avatars, mastheads) and
# every kept one was a real figure.
THUMB_MAX = 160  # sender-declared px width or height
ICON_MAX = 150  # natural longest side: social icons are often 96px with no declared size
BANNER_RATIO = 3.5  # natural width/height: mastheads and section strips are ~4:1


def unwrap(url):
    """Real target of a tracked link, decoded offline; opaque trackers come back unchanged."""
    m = re.match(r"https://substack\.com/redirect/2/([A-Za-z0-9_-]+)", url)
    if m:
        seg = m.group(1).split(".")[0]
        try:
            return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))["e"]
        except Exception:
            return url
    # Kit/ConvertKit and similar: target URL base64-encoded as the last path segment.
    # Decoded offline on purpose: following the tracker would register a fake click.
    seg = url.rstrip("/").rsplit("/", 1)[-1]
    try:
        dec = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode()
        if dec.startswith("http"):
            return dec
    except Exception:
        pass
    return url



# E-reader fonts have no emoji or pictographs: they render as tofu boxes, so they go.
# One astral range per character class: CodeQL misreads two astral ranges in one class as overlapping.
EMOJI = re.compile("[\U0001F000-\U0001FAFF]|[\U000E0000-\U000E007F]"
                   "|[\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F\u200D\u20E3]")


def no_emoji(text):
    return re.sub(r"\s{2,}", " ", EMOJI.sub("", text)).strip(" -|·:")


INVISIBLE = re.compile(r"[\s\u00a0\u034f\u00ad\u200b-\u200d\u2060\ufeff]+")
CONTAINERS = ["p", "div", "td", "tr", "tbody", "table", "span", "center", "font", "li", "ul", "ol",
              "h1", "h2", "h3", "h4", "h5", "h6", "a", "strong", "em", "b", "i", "u", "blockquote"]


def drop_empty(soup):
    """Email spacer paragraphs (zero-width padding, &nbsp;, stacked <br>) read as big gaps on an e-reader."""
    for el in reversed(soup.find_all(CONTAINERS)):
        if not el.find(["img", "hr"]) and not INVISIBLE.sub("", el.get_text()):
            el.decompose()
    for br in soup.find_all("br"):
        sib = br.next_sibling
        while sib is not None and isinstance(sib, str) and not INVISIBLE.sub("", sib):
            sib = sib.next_sibling
        if sib is None or getattr(sib, "name", None) == "br":
            br.decompose()  # trailing or doubled break


def declared(img, dim):
    """Pixel width or height the sender asked for: a px value in style, else the attribute, else None."""
    m = re.search(rf"(?:^|;)\s*{dim}\s*:\s*(\d+)px", img.get("style") or "", re.I)
    v = m.group(1) if m else (img.get(dim) or "").strip().removesuffix("px")
    return int(v) if re.fullmatch(r"[0-9]{1,5}", v) else None  # isdigit() accepts "²", which int() rejects


def is_thumbnail(img):
    return any(v is not None and v <= THUMB_MAX for v in (declared(img, "width"), declared(img, "height")))


TABLE_TAGS = ["table", "thead", "tbody", "tfoot", "tr", "td", "th"]
CELL_BLOCKS = ["div", "p", "center", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody",
               "tfoot", "tr", "td", "th"]


def linearize_tables(soup):
    """Instapaper renders a table as one block per cell, so a ranking falls apart into a column of
    fragments. Any row with 2+ text cells becomes one line ("1 · Widget Deluxe · 1,293,425 · -4%");
    layout rows can't be told apart and get the same treatment, which reads fine. Every other
    table tag becomes a plain block."""
    for tr in soup.find_all("tr"):
        if tr.find("tr"):
            continue
        cells = [c for c in tr.find_all(["td", "th"], recursive=False)
                 if INVISIBLE.sub("", c.get_text()) or c.find("img")]
        if sum(1 for c in cells if INVISIBLE.sub("", c.get_text())) < 2:
            continue
        line = soup.new_tag("p")
        for i, c in enumerate(cells):
            for b in c.find_all(CELL_BLOCKS):
                b.insert_before(" ")  # a block boundary was a line break: "8<div>▲11</div>" must not read "8▲11"
                b.unwrap()
            if i:
                line.append(" · ")
            for child in list(c.contents):
                line.append(child.extract())
        tr.replace_with(line)
    for t in soup.find_all(TABLE_TAGS):
        t.name = "div"


def publication(from_header, names=None):
    """Sponsor tags are dropped: 'The Weekly Widget - powered by Acme' -> 'The Weekly Widget'.
    'Ada from The Weekly Widget' -> 'The Weekly Widget'; 'Ada (The Widget Report)' -> 'The Widget Report'.
    `names` maps a sender address to a name for senders with no usable display name
    (default: reader.publication_names from the config)."""
    names = config.get().reader.publication_names if names is None else names
    name, addr = email.utils.parseaddr(from_header)
    if addr.lower() in names:
        return names[addr.lower()]
    name = name or addr or from_header
    m = re.search(r"\(([^)]+)\)\s*$", name) or re.search(r"\bfrom (.+)$", name)
    name = (m.group(1) if m else name).strip()
    return re.sub(r"\s*[-–—|]\s*(powered|presented|sponsored) by .+$", "", name, flags=re.I)


def to_jpeg(url, max_w=None):
    """Fetch an image, return e-reader-safe JPEG bytes, or None for pixels, icons, banners and failures."""
    max_w = max_w or config.get().reader.max_image_width
    try:
        data, _ = safe_fetch(url, max_bytes=8_000_000, deadline_s=20)  # public http(s) only, capped
        im = Image.open(io.BytesIO(data))
        im.seek(0)  # first frame of an animated GIF
        if min(im.size) < MIN_SIDE or max(im.size) <= ICON_MAX or im.width / im.height >= BANNER_RATIO:
            return None
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, "white")
        flat.paste(im, mask=im.getchannel("A"))
        if flat.width > max_w:
            flat = flat.resize((max_w, round(flat.height * max_w / flat.width)), Image.LANCZOS)
        out = io.BytesIO()
        flat.save(out, "JPEG", quality=80, optimize=True)
        return out.getvalue()
    except Exception:
        return None


def resolve(url):
    """Final target of an opaque click tracker, one redirect hop at a time, never fetching the target."""
    for _ in range(3):
        try:
            body, loc = safe_fetch(url, max_bytes=1, deadline_s=10, follow=False)
        except Exception:
            return None  # not a redirect (or blocked): the link cannot be cleaned, so it is dropped
        if loc == url:
            return None
        url = unwrap(loc)
        if not is_tracker(url):
            return url
    return None


def scrub_addresses(text, own):
    for a in own:
        text = re.sub(re.escape(a), "", text, flags=re.I)
    return text


def qr_jpeg(url):
    """QR code for the original post, so the phone can open it in the Substack app."""
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=8, border=2)
    out = io.BytesIO()
    Image.open(buf).convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def is_substack(m):
    hay = " ".join(str(m.get(h) or "") for h in ("From", "Message-ID", "List-Unsubscribe", "X-Mailgun-Tag"))
    return "substack" in hay.lower()


def site_of(m):
    """Publication's own site: List-URL (Substack sets it), else the sender's domain if it isn't an ESP."""
    lu = re.search(r"https?://[^>\s]+", str(m.get("List-URL") or ""))
    if lu:
        return lu.group(0)
    dom = email.utils.parseaddr(str(m["From"]))[1].split("@")[-1].lower()
    dom = re.sub(r"^(mail|email|e|news|newsletter|hello|info)\.", "", dom)
    esp = ("substack.com", "beehiiv.com", "kit-mail", "convertkit", "mailchimp", "sendgrid", "ghost.io")
    return None if not dom or any(x in dom for x in esp) else dom


def clean(eml_bytes, article_id, base_url, ad_filter=None, fetch_image=None, resolve_link=None):
    """Returns (title, html, {image_key: jpeg_bytes}, removed_blocks). base_url is the public host root.
    ad_filter, fetch_image and resolve_link are injectable so tests run offline. ad_filter defaults to
    the model-based stripper when adstrip.enabled, else to nothing."""
    cfg = config.get()
    own_addresses = cfg.reader.own_addresses
    if ad_filter is None:
        model = cfg.adstrip.model
        ad_filter = (lambda soup, title, pub: strip_ads(soup, title, pub, model=model)) if cfg.adstrip.enabled \
            else (lambda soup, title, pub: [])
    m = email.message_from_bytes(eml_bytes, policy=policy.default)
    title = no_emoji(str(m["Subject"] or ""))
    title = re.sub(r"^((fwd?|fw|re):\s*)+", "", title, flags=re.I)
    title = re.sub(r"\s*[-–—|]\s*(powered|presented|sponsored|brought to you) by .+$", "", title, flags=re.I) or "Newsletter"
    try:
        sent = parsedate_to_datetime(str(m["Date"])).astimezone(cfg.tzinfo)
        title += f" ({sent:%b} {sent.day}, {sent.year})"
    except Exception:
        pass
    pub = no_emoji(publication(str(m["From"])))
    original = (str(m.get("List-Post") or "").strip("<>") or None)
    if original and (not original.startswith(("http://", "https://")) or is_account_link(original)
                     or any(o in original.lower() for o in own_addresses)):
        original = None  # List-Post is sender-controlled: same allow-list as body links (mailto: -> no link)
    part = m.get_body(("html", "plain"))
    raw = part.get_content()
    if part.get_content_type() == "text/plain":
        raw = "<pre>" + html.escape(raw) + "</pre>"
    soup = BeautifulSoup(raw, "html.parser")

    own = set(own_addresses) | {
        email.utils.parseaddr(str(m.get(h) or ""))[1].lower() for h in ("To", "Delivered-To")} - {""}
    # Outlook conditional comments (<!--[if mso]>) carry whole duplicate button blocks with raw tracker links.
    for c in soup.find_all(string=lambda s: isinstance(s, (Comment, Declaration, Doctype, ProcessingInstruction))):
        c.extract()
    for t in soup(DANGEROUS):
        t.decompose()
    for img in soup.find_all("img"):  # before link resolving, so a thumbnail's tracker is never hit
        if is_thumbnail(img):
            a = img.find_parent("a")
            img.decompose()
            if a is not None and not a.find("img") and not INVISIBLE.sub("", a.get_text()):
                a.decompose()
    # Substack email chrome: paywall CTA, button rows, subscription widgets
    for sel in [".paywall", ".subscription-benefits", ".email-button-outline", ".email-icon-button",
                ".footer", ".post-meta", ".preview", ".youtube-wrap"]:
        for t in soup.select(sel):
            t.decompose()
    resolved = 0
    for a in soup.find_all("a"):
        text = a.get_text(" ", strip=True)
        href = (a.get("href") or "").strip()
        if DROP_TEXT.search(text) or DROP_HREF.search(href):
            a.unwrap() if a.find("img") else a.decompose()  # never resolve an unsubscribe tracker
            continue
        real = unwrap(href)
        if is_tracker(real) and resolved < MAX_RESOLVE:
            resolved += 1
            real = (resolve_link or resolve)(real) or ""
        # Allow-list: a link survives only as a clean public http(s) URL that is not a tracker,
        # an account/login/subscription page, or carrying one of the reader's addresses. Otherwise keep the text.
        if (not real.startswith(("http://", "https://")) or is_tracker(real) or is_account_link(real)
                or DROP_HREF.search(real) or any(o in real.lower() for o in own)):
            a.unwrap(); continue
        a.attrs = {"href": clean_query(real, own)}
    for t in soup.find_all(True):  # every other attribute (on*, style, srcset, data-*) goes
        keep = KEEP_ATTRS.get(t.name, ())
        t.attrs = {k: v for k, v in t.attrs.items() if k in keep}
    for node in soup.find_all(string=lambda s: any(o in s.lower() for o in own)):
        node.replace_with(scrub_addresses(node, own))

    for node in soup.find_all(string=EMOJI):
        node.replace_with(EMOJI.sub("", node))
    drop_empty(soup)
    removed = ad_filter(soup, title, pub)  # before image fetches, so ad images are never pulled
    linearize_tables(soup)  # after the ad filter, which judges whole tables as blocks
    drop_empty(soup)

    images, lead = {}, None
    for img in soup.find_all("img"):
        if len(images) >= MAX_IMAGES:
            img.decompose(); continue
        jpg = (fetch_image or (lambda u: to_jpeg(u, cfg.reader.max_image_width)))(img.get("src") or "")
        if not jpg:
            img.decompose(); continue
        key = f"{article_id}-{len(images)}"
        images[key] = jpg
        img.attrs = {"src": f"{base_url}/i/{key}.jpg", "alt": scrub_addresses(img.get("alt", ""), own)}
        w, h = Image.open(io.BytesIO(jpg)).size
        if lead is None and w / h <= 2.5:  # wide banners are mastheads/logos, not the lead image
            lead = jpg
    images[f"{article_id}-cover"] = make_cover(lead, site_of(m), pub)

    # Newsletter preheaders pad with invisible joiners (U+034F, U+200B-200D, U+2060, U+FEFF, U+00AD)
    text = re.sub(r"[͏­​-‍⁠﻿]", "", soup.get_text(" ", strip=True))
    text = re.sub(r"\s+", " ", text).strip()
    desc = f"{pub} · {text[:200]}"
    cover = f'<meta property="og:image" content="{base_url}/i/{article_id}-cover.jpg">'
    top = bottom = ""
    if original:
        link = html.escape(clean_query(original, own))
        if is_substack(m):
            label = "Comment or like on Substack"
            qr_key = f"{article_id}-qr"
            images[qr_key] = qr_jpeg(clean_query(original, own))
            bottom = (f'<hr><p><strong><a href="{link}">{label} →</a></strong></p>'
                      f'<p>Or scan to open it in the Substack app:</p>'
                      f'<p><img src="{base_url}/i/{qr_key}.jpg" alt="QR code for the post on Substack" width="240"></p>')
        else:
            label = "Open original"
            bottom = f'<hr><p><strong><a href="{link}">{label} →</a></strong></p>'
        top = f' · <a href="{link}">{label}</a>'
    byline = f'<p><em>{html.escape(pub)}</em>{top}</p>'
    body = soup.body.decode_contents() if soup.body else str(soup)
    e = html.escape
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex,nofollow"><title>{e(title)}</title>
<meta property="og:type" content="article"><meta property="og:title" content="{e(title)}">
<meta property="og:site_name" content="{e(pub)}"><meta name="author" content="{e(pub)}">
<meta name="application-name" content="{e(pub)}">
<meta name="description" content="{e(desc)}"><meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{base_url}/a/{article_id}">{cover}
<style>img{{max-width:100%;height:auto}}body{{max-width:40em;margin:auto;font-family:Georgia,serif}}</style>
</head><body><article><h1>{e(title)}</h1>{byline}{body}{bottom}</article></body></html>"""
    return title, doc, images, removed

