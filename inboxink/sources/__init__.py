"""Where newsletters come from. run.py talks only to the Source interface defined here.

A source finds messages that still need delivering, hands back each one as raw RFC 822 bytes,
and files it away afterwards (done, or failed after too many tries). Which source is used comes
from `source.kind` in the config.
"""
import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class Pending:
    id: str  # opaque to run.py; passed back to fetch_raw / mark_done / mark_failed and used in the state file
    date: int  # sort key, milliseconds since the epoch; run.py handles oldest first


class Source(abc.ABC):
    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def failed_label(self):
        """Name of the label/folder given up issues land in (shown in the log)."""
        return self.cfg.gmail.labels.failed

    @abc.abstractmethod
    def open(self, create=True):
        """Connect and make sure the done/failed (and source) labels or folders exist.
        create=False (dry run) must not change anything: raise RuntimeError if something is missing.
        Raises on any failure; run.py reports it as one log line."""

    @abc.abstractmethod
    def pending(self, query="", limit=50):
        """Messages waiting to be delivered (in the source label, or the inbox when
        source.newsletters_only is true; never the done or failed ones), oldest first, at most `limit`
        Pending items. `query` is optional extra search text from the command line; a source that
        has no search may ignore it."""

    @abc.abstractmethod
    def fetch_raw(self, message_id):
        """The full message as RFC 822 bytes."""

    @abc.abstractmethod
    def mark_done(self, message_id):
        """Delivered: apply the done label and, when gmail.archive_on_done is true, archive and mark read."""

    @abc.abstractmethod
    def mark_failed(self, message_id):
        """Given up: apply the failed label so the message is not picked up again."""

    def close(self):
        """Release a connection, if the source holds one."""


def get_source(cfg):
    """The Source selected by cfg.source.kind (imports lazily so unused backends cost nothing)."""
    if cfg.source.kind == "gog":
        from .gog import GogSource
        return GogSource(cfg)
    if cfg.source.kind == "imap":
        from .imap import ImapSource
        return ImapSource(cfg)
    raise ValueError(f"unknown source kind {cfg.source.kind!r}")
