"""Offline tests for the email cleaner: synthetic newsletters only, no network, no model call."""
import base64, io, json
from email.message import EmailMessage

from PIL import Image

from inboxink.clean import clean, no_emoji, publication, unwrap

BASE = "https://letters.invalid"
AID = "0" * 32


def eml(html, sender='"Jane Doe from Example Weekly" <example@substack.com>',
        subject="🎮 Big news - powered by Acme", list_post=None):
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = sender, subject, "Mon, 05 Oct 2026 14:41:00 +0000"
    if list_post:
        m["List-Post"] = f"<{list_post}>"
    m.set_content("plain")
    m.add_alternative(html, subtype="html")
    return bytes(m)


def jpeg(w=400, h=300):
    out = io.BytesIO()
    Image.new("RGB", (w, h), "gray").save(out, "JPEG")
    return out.getvalue()


def run(html, **kw):
    return clean(eml(html, **kw), AID, BASE, ad_filter=lambda *a: [], fetch_image=lambda url: jpeg(),
                 resolve_link=lambda url: None)


def test_title_drops_emoji_sponsor_and_adds_date_in_configured_zone():
    title, *_ = run("<p>Hi</p>")
    assert title == "Big news (Oct 5, 2026)"  # 14:41 UTC is still Oct 5 in the test config's zone


def test_title_date_follows_reader_timezone(monkeypatch):
    from dataclasses import replace
    from inboxink import config
    far_east = replace(config.get(), reader=replace(config.get().reader, timezone="Pacific/Auckland"))
    monkeypatch.setattr(config, "_current", far_east)
    title, *_ = run("<p>Hi</p>")
    assert title == "Big news (Oct 6, 2026)"  # the same instant is already Oct 6 in Auckland


def test_publication_from_sender_name():
    assert publication('"Ada Example from The Weekly Widget" <x@substack.com>') == "The Weekly Widget"
    assert publication('"Ada Example (The Widget Report)" <x@substack.com>') == "The Widget Report"
    assert publication("Widget Digest - powered by Acme <mail@widgets.example>") == "Widget Digest"
    assert publication("digest@example.com") == "Example Digest"  # reader.publication_names in the test config


def test_unwrap_decodes_kit_and_substack_trackers_offline():
    target = "https://widgets.example/"
    kit = "https://1.click.kit-mail3.com/abc/def/" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert unwrap(kit) == target
    seg = base64.urlsafe_b64encode(json.dumps({"e": target}).encode()).decode().rstrip("=")
    assert unwrap(f"https://substack.com/redirect/2/{seg}.sig") == target
    assert unwrap("https://example.com/plain") == "https://example.com/plain"


def test_tokens_and_unsubscribe_links_are_removed():
    _, doc, *_ = run('<p>Story <a href="https://x.com/a?utm_source=email&id=7">link</a></p>'
                     '<p><a href="https://x.com/unsubscribe?token=SECRET">Unsubscribe</a></p>'
                     '<p><a href="https://substack.com/app-link/post?submitLike=true&token=SECRET">Like</a></p>')
    assert "SECRET" not in doc and "Unsubscribe" not in doc
    assert 'href="https://x.com/a?id=7"' in doc


def test_auditor_token_cases_never_reach_the_page():
    _, doc, *_ = run(
        '<p>Hi reader@example.com, read <a href="https://click.convertkit-mail2.com/SUBTOKEN123/x">this</a></p>'
        '<p><a href="https://site.com/p?_bhlid=A&uuid=B&key=C&email=reader%40example.com&mkt_tok=D&_hsenc=E&page=2">post</a></p>'
        '<p><a href="https://site.com/account/login?next=/">Log in</a></p>')
    for leak in ("SUBTOKEN123", "_bhlid", "uuid=", "key=", "mkt_tok", "_hsenc", "reader@example.com", "reader%40", "/account/login"):
        assert leak not in doc, leak
    assert 'href="https://site.com/p?page=2"' in doc and "read this" in doc


def test_list_post_must_be_http():
    _, doc, images, _ = run("<p>Body</p>", list_post="javascript:alert(1)")
    assert "javascript:" not in doc and f"{AID}-qr" not in images


def test_outlook_conditional_comments_are_dropped():
    _, doc, *_ = run('<p>x</p><!--[if mso]><a href="https://1.click.kit-mail3.com/SUB/TOK">Go</a><![endif]-->')
    assert "kit-mail" not in doc and "SUB/TOK" not in doc


def test_active_and_embedded_markup_is_removed():
    _, doc, *_ = run('<p onclick="x()">t</p><svg onload="alert(1)"></svg><iframe src="https://e.vil"></iframe>'
                     '<form action="https://e.vil"><input name="pw"></form><a href="javascript:alert(1)">js</a>'
                     '<img src="https://cdn.example/x" onerror="alert(1)">')
    for bad in ("onclick", "<svg", "onload", "<iframe", "<form", "<input", "javascript:", "onerror"):
        assert bad not in doc, bad


def test_spacer_paragraphs_are_dropped():
    _, doc, *_ = run("<p>One</p><p>​</p><p>&nbsp;</p><br><br><p>Two</p>")
    assert "<p>​</p>" not in doc and "<p>\xa0</p>" not in doc and "<br/><br/>" not in doc


def test_images_are_rehosted_and_cover_exists():
    _, doc, images, _ = run('<p><img src="https://cdn.example/x"></p>')
    assert f'src="{BASE}/i/{AID}-0.jpg"' in doc
    assert f"{AID}-cover" in images
    assert Image.open(io.BytesIO(images[f"{AID}-cover"])).size == (600, 600)


def test_substack_posts_get_comment_link_and_qr():
    post = "https://example.substack.com/p/big-news"
    _, doc, images, _ = run("<p>Body</p>", list_post=post)
    assert "Comment or like on Substack" in doc and f'href="{post}"' in doc
    assert f"{AID}-qr" in images


def test_metadata_names_the_publication():
    _, doc, *_ = run("<p>Body text here</p>")
    assert '<meta property="og:site_name" content="Example Weekly">' in doc
    assert 'content="Example Weekly · Body text here' in doc


def test_no_emoji_keeps_typography():
    assert no_emoji("Q2’26 – “quotes” café 🎉") == "Q2’26 – “quotes” café"


def test_sender_sized_thumbnails_are_dropped():
    # Instapaper stretches every image to the column; a 48px thumbnail becomes a full-screen banner.
    _, doc, images, _ = run('<p><img src="https://a.invalid/hero.jpg" width="560" style="width:100%">'
                            '<img src="https://a.invalid/t1.jpg" style="display:block;width:48px">'
                            '<img src="https://a.invalid/t2.jpg" width="92">'
                            '<img src="https://a.invalid/logo.png" style="height:16px;display:inline">'
                            '<img src="https://a.invalid/plain.jpg"></p>')
    assert len([k for k in images if not k.endswith("-cover")]) == 2  # hero + unsized


def test_table_rows_become_single_lines():
    _, doc, _, _ = run('<table><tr><td>1</td><td><img src="https://a.invalid/t.jpg" width="48"></td>'
                       '<td><a href="https://store.example.com/app/42">Widget Deluxe</a></td>'
                       '<td>1,293,425</td></tr><tr><td><div>Layout cell</div></td></tr></table>')
    assert "<table" not in doc and "<td" not in doc
    assert '<p>1 · <a href="https://store.example.com/app/42">Widget Deluxe</a> · 1,293,425</p>' in doc
    assert "Layout cell" in doc


def test_icons_and_wide_banners_are_dropped_at_fetch(monkeypatch):
    from inboxink import clean as c
    def fake(w, h):
        out = io.BytesIO(); Image.new("RGB", (w, h)).save(out, "PNG"); return out.getvalue(), ""
    for size, kept in [((96, 96), False), ((1350, 330), False), ((1000, 750), True), ((560, 262), True)]:
        monkeypatch.setattr(c, "safe_fetch", lambda url, **kw: fake(*size))
        assert (c.to_jpeg("https://a.invalid/x.png") is not None) is kept, size


def test_thumbnail_link_is_not_resolved_and_odd_sizes_dont_crash():
    hit = []
    clean(eml('<p><a href="https://click.example.com/abc123"><img src="https://a.invalid/t.jpg" width="48"></a>'
              '<img src="https://a.invalid/x.jpg" width="²">Text</p>'), AID, BASE, ad_filter=lambda *a: [],
          fetch_image=lambda url: jpeg(), resolve_link=lambda url: hit.append(url))
    assert hit == []
