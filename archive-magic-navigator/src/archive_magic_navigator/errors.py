"""Expected user-facing Navigator failures."""


class NavigatorError(Exception):
    """Base class for a deterministic, user-correctable failure."""


class ValidationError(NavigatorError):
    """The selected archive data is missing, malformed, or unsafe."""


class StartupError(NavigatorError):
    """The pywb child could not be started safely."""
