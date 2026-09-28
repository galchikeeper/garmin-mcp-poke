"""Suppress upstream credentials/payload logs while retaining application logs."""
import logging
from pathlib import Path
import threading

import garth


_NAMESPACES = ("garminconnect", "garth", "requests_oauthlib", "oauthlib", "urllib3")
_LOCK = threading.RLock()


class _GarthRootFilter(logging.Filter):
    """Pinned Garth's Body Battery parser logs payloads directly to root."""

    _garmin_mcp_upstream_root_filter = True

    def __init__(self):
        super().__init__()
        self._directory = Path(garth.__file__).resolve().parent

    def filter(self, record):
        try:
            return not Path(record.pathname).resolve().is_relative_to(self._directory)
        except (TypeError, ValueError, OSError, RuntimeError):
            # An unrelated application's unconventional record stays visible.
            return True


def suppress_upstream_logs():
    """Idempotently silence only upstream Garmin/OAuth/HTTP library loggers.

    Disabling a parent logger alone does not stop child records propagating to
    root. A NullHandler with propagation disabled closes that route, including
    children created later. Existing child handlers are also detached (never
    closed, since a handler may be shared with the application).

    The root filter covers Garth's direct logging.warning/error calls by source
    path. It leaves all other direct root and application records unchanged.
    """
    with _LOCK:
        names = set(_NAMESPACES)
        names.update(name for name in tuple(logging.Logger.manager.loggerDict)
                     if any(name.startswith(namespace + ".") for namespace in _NAMESPACES))
        for name in names:
            logger = logging.getLogger(name)
            if len(logger.handlers) != 1 or not isinstance(logger.handlers[0], logging.NullHandler):
                logger.handlers[:] = [logging.NullHandler()]
            logger.propagate = False
            logger.disabled = True

        root = logging.getLogger()
        if not any(getattr(item, "_garmin_mcp_upstream_root_filter", False)
                   for item in root.filters):
            root.addFilter(_GarthRootFilter())
