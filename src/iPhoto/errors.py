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
