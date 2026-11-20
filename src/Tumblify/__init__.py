# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Tumblify Blender extension package entry point.

Extension metadata (id, version, supported Blender and platform, permissions)
lives in ``blender_manifest.toml``. Runtime implementation lives in ``addon.py``.
"""

from .addon import register, unregister

__all__ = ("register", "unregister")
