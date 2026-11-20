# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Tumblify Blender add-on package entry point.

Blender discovers legacy add-ons by reading ``bl_info`` directly from this file,
so metadata must remain a literal dictionary here. Runtime implementation lives
in ``addon.py``.
"""

bl_info = {
    "name": "Tumblify",
    "author": "CG Corner addons",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > Tumblify",
    "description": "Physics-Based Object Placement for Blender, powered by Box3D",
    "category": "Physics",
}

from .addon import register, unregister

__all__ = ("bl_info", "register", "unregister")
