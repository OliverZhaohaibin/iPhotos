"""Role definitions shared by the album models."""

from __future__ import annotations

from enum import IntEnum
from typing import Dict

from PySide6.QtCore import Qt


class Roles(IntEnum):
    """Custom roles exposed to QML or widgets."""

    REL = Qt.UserRole + 1
    ABS = Qt.UserRole + 2
    ASSET_ID = Qt.UserRole + 3
    IS_IMAGE = Qt.UserRole + 4
    IS_VIDEO = Qt.UserRole + 5
    IS_LIVE = Qt.UserRole + 6
    LIVE_GROUP_ID = Qt.UserRole + 7
    SIZE = Qt.UserRole + 8
    DT = Qt.UserRole + 9
    FEATURED = Qt.UserRole + 10
    LIVE_MOTION_REL = Qt.UserRole + 11
    LIVE_MOTION_ABS = Qt.UserRole + 12


def role_names(base: Dict[int, bytes] | None = None) -> Dict[int, bytes]:
    """Return a mapping of Qt role numbers to byte names."""

    mapping: Dict[int, bytes] = {} if base is None else dict(base)
    mapping.update(
        {
            Roles.REL: b"rel",
            Roles.ABS: b"abs",
            Roles.ASSET_ID: b"assetId",
            Roles.IS_IMAGE: b"isImage",
            Roles.IS_VIDEO: b"isVideo",
            Roles.IS_LIVE: b"isLive",
            Roles.LIVE_GROUP_ID: b"liveGroupId",
            Roles.LIVE_MOTION_REL: b"liveMotion",
            Roles.LIVE_MOTION_ABS: b"liveMotionAbs",
            Roles.SIZE: b"size",
            Roles.DT: b"dt",
            Roles.FEATURED: b"featured",
        }
    )
    return mapping
