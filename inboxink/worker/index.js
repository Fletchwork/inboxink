// Read-only: serves cleaned newsletter issues from KV at /a/<128-bit id> and their
// images at /i/<id>-<n|qr|cover>.jpg (body images, Substack QR code, list thumbnail). The InboxInk job writes keys with a TTL,
// so everything expires on its own.
// Replaced with a hash of this file when `inboxink` uploads the Worker, so `inboxink update` can
// tell whether the deployed copy is current. Served only as a header on the 404 page.
const VERSION = "__INBOXINK_WORKER_VERSION__";
const ROBOTS = { "X-Robots-Tag": "noindex, nofollow", "X-Content-Type-Options": "nosniff" };
// Pages are cleaned upstream; CSP is the backstop if hostile markup ever slips through.
const CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'none'; " +
            "frame-ancestors 'none'; base-uri 'none'";

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") return notFound();
    const path = new URL(request.url).pathname;

    const page = path.match(/^\/a\/([0-9a-f]{32})$/);
    if (page) {
      const body = await env.LETTERS.get(page[1]);
      if (!body) return notFound();
      return new Response(body, {
        headers: { ...ROBOTS, "Content-Security-Policy": CSP, "Content-Type": "text/html; charset=utf-8",
                   "Cache-Control": "private, max-age=3600", "Referrer-Policy": "no-referrer" },
      });
    }

    const img = path.match(/^\/i\/([0-9a-f]{32}-(?:\d{1,3}|qr|cover))\.jpg$/);
    if (img) {
      const body = await env.LETTERS.get(img[1], "arrayBuffer");
      if (!body) return notFound();
      return new Response(body, {
        headers: { ...ROBOTS, "Content-Type": "image/jpeg", "Cache-Control": "public, max-age=2592000, immutable" },
      });
    }

    return notFound();
  },
};

function notFound() {
  return new Response("Not found", { status: 404, headers: { ...ROBOTS, "X-InboxInk-Version": VERSION } });
}
