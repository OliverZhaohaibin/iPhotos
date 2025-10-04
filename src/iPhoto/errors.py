"""Custom exception hierarchy for iPhoto."""

from __future__ import annotations


class IPhotoError(Exception):
    """Base class for all custom errors raised by iPhoto."""


class AlbumNotFoundError(IPhotoError):
    """Raised when the requested album cannot be located."""


class ManifestInvalidError(IPhotoError):
    """Raised when a manifest fails validation against the schema."""


class ExternalToolError(IPhotoError):
    """Raised when an external tool such as exiftool or ffmpeg fails."""


class IndexCorruptedError(IPhotoError):
    """Raised when the cached index cannot be parsed."""


class PairingConflictError(IPhotoError):
    """Raised when mutually exclusive Live Photo pairings are detected."""


class LockTimeoutError(IPhotoError):
    """Raised when a file-level lock cannot be acquired in time."""


class SettingsError(IPhotoError):
    """Base class for settings related failures."""


class SettingsLoadError(SettingsError):
    """Raised when the settings file cannot be parsed or loaded."""


class SettingsValidationError(SettingsError):
    """Raised when settings data fails schema validation."""


class LibraryError(IPhotoError):
    """Base class for errors occurring while managing the basic library."""


class LibraryUnavailableError(LibraryError):
    """Raised when the configured basic library cannot be accessed."""


class AlbumNameConflictError(LibraryError):
    """Raised when trying to create or rename an album to an existing name."""


class AlbumDepthError(LibraryError):
    """Raised when attempting to create albums deeper than the supported nesting level."""


class AlbumOperationError(LibraryError):
    """Raised when album file-system operations fail."""
