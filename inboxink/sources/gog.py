"""Gmail access through the `gog` command line tool (gogcli), for Google Workspace accounts.

Selected with source.kind = "gog". Needs gog.account (and gog.bin if `gog` is not on PATH).
If gog cannot open its keyring when started by a scheduler (no terminal to ask for the
keyring password), point gog.bin at a small wrapper script that supplies it.
"""
import base64, json, re, subprocess

from . import Pending, Source


def _unwrap(text):
    """gog may wrap fetched fields in untrusted-content markers; keep only the payload."""
    if text.startswith("<<<EXTERNAL_UNTRUSTED_CONTENT"):
        text = text.split("\n---\n", 1)[1].rsplit("<<<END", 1)[0]
    return text.strip()


def _q(label):
    """Gmail search spells nested/spaced labels with hyphens: 'Newsletter/Delivered' -> newsletter-delivered."""
    return re.sub(r"[\s/]+", "-", label.lower())


class GogSource(Source):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.ids = {}

    def _gog(self, *args):
        r = subprocess.run([self.cfg.gog.bin, "-a", self.cfg.gog.account, *args, "-j", "--results-only"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"gog {' '.join(args[:3])} failed: {r.stderr.strip()[:300]}")
        return json.loads(r.stdout) if r.stdout.strip() else None

    def _names(self):
        lab = self.cfg.gmail.labels
        names = (lab.done, lab.failed)
        return names if self.cfg.source.newsletters_only else (lab.source, *names)

    def open(self, create=True):
        """Fill self.ids {label name: id}, creating our labels if they are missing (unless create=False)."""
        labels = self._gog("gmail", "labels", "list")
        labels = labels.get("labels", labels) if isinstance(labels, dict) else labels
        ids = {_unwrap(l["name"]): l["id"] for l in labels}
        for name in self._names():
            if name not in ids:
                if not create:
                    raise RuntimeError(f"Gmail label '{name}' does not exist yet. 'inboxink run' creates it.")
                made = self._gog("gmail", "labels", "create", name)
                made = made.get("label", made) if isinstance(made, dict) else made
                ids[name] = made["id"]
        self.ids = ids

    def pending(self, query="", limit=50):
        lab, ids = self.cfg.gmail.labels, self.ids
        inbox_only = self.cfg.source.newsletters_only
        where = "in:inbox" if inbox_only else f"label:{_q(lab.source)}"
        q = f"{where} -label:{_q(lab.done)} -label:{_q(lab.failed)} {query}".strip()
        threads = self._gog("gmail", "search", q, "--max", str(limit)) or []
        threads = threads.get("threads", threads) if isinstance(threads, dict) else threads
        out = []
        for t in threads:
            th = self._gog("gmail", "thread", "get", t["id"])
            th = th.get("thread", th)
            for m in th.get("messages", []):
                have = set(m.get("labelIds", []))
                in_source = "INBOX" in have if inbox_only else ids[lab.source] in have
                if in_source and not have & {ids[lab.done], ids[lab.failed]}:
                    out.append(Pending(m["id"], int(m.get("internalDate", 0))))
        return sorted({m.id: m for m in out}.values(), key=lambda m: m.date)

    def fetch_raw(self, message_id):
        d = self._gog("gmail", "get", message_id, "--format", "raw")
        b = _unwrap(d["message"]["raw"])
        return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))

    def mark_done(self, message_id):
        """Delivered: label it and mark it read; take it out of the inbox when gmail.archive_on_done."""
        remove = "INBOX,UNREAD" if self.cfg.gmail.archive_on_done else "UNREAD"
        self._gog("gmail", "messages", "modify", message_id, "--add", self.ids[self.cfg.gmail.labels.done],
                  "--remove", remove)

    def mark_failed(self, message_id):
        self._gog("gmail", "messages", "modify", message_id, "--add", self.ids[self.cfg.gmail.labels.failed])
