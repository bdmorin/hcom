# Lazy import everything to avoid loading shared.py at package import time.
# This enables hooks_entry to run with minimal overhead (~50ms vs ~100ms).
_api_state = "unloaded"  # 'unloaded' -> 'loading' -> 'loaded'
_version = None  # Cached version


def _load_api():
    """Load api module and populate globals."""
    global _api_state
    if _api_state != "unloaded":
        return
    _api_state = "loading"  # Prevent recursion during import
    try:
        from . import api

        for name in api.__all__:
            globals()[name] = getattr(api, name)
        _api_state = "loaded"
    except Exception:
        _api_state = "unloaded"  # Reset on failure to allow retry
        raise


def __getattr__(name: str):
    """Lazy load version and api module on first attribute access."""
    global _version

    # Lazy load __version__
    if name == "__version__":
        if _version is None:
            from .shared import __version__ as v

            _version = v
        return _version

    # Try loading api if not yet loaded
    if _api_state == "unloaded":
        _load_api()
        # Check if the attribute is now available
        if name in globals():
            return globals()[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    """List available attributes including lazy-loaded ones."""
    if _api_state == "unloaded":
        _load_api()
    return list(globals().keys()) + ["__version__"]


# Public API: version + all api.py exports (lazily loaded)
__all__ = ["__version__", "HcomError", "Session", "session", "instances", "launch"]
