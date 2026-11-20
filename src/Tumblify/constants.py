# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Stable constants and public enum definitions for Tumblify.

This module must stay free of Blender runtime state. It is safe to import from future
feature modules without creating registration or operator side effects.
"""

BRUSH_NAME = "Tumblify_Brush"
GUIDE_NAME = "Tumblify_Emitter_Guide"
GUIDE_MOD_NAME = "Tumblify Emitter Guide"
GUIDE_GROUP_PREFIX = "Tumblify_EmitterGuide_"
SPAWN_COLLECTION = "Tumblify_Spawned"
DEBUG_COLLECTION = "Tumblify_Collision_Debug"
FROZEN_PROP = "Tumblify_FrozenStatic"
RUNTIME_PROP = "Tumblify_RuntimeSpawn"
FIXED_PROP = "Tumblify_FixedVisualOnly"
FAST_COLLECTION = "Tumblify_FastPreview"
AUTO_DETECTED_SHAPE_PROP = "Tumblify_AutoDetectedShape"
AUTO_DETECTED_AXIS_PROP = "Tumblify_AutoDetectedAxis"
DISPLAY_MOD_NAME = "Tumblify Instance Display"
INSTANCE_RESULT_MOD_NAME = "Tumblify Result Instances"
DISPLAY_GROUP_PREFIX = "Tumblify_InstanceDisplay_"
EMITTER_BAKE_ACTION_PREFIX = "Tumblify_EmitterBake_"
EMITTER_BAKE_PROP = "Tumblify_EmitterBake"
BAKE_PREV_ACTION_PROP = "Tumblify_BakePrevAction"
BAKE_PREV_ROT_PROP = "Tumblify_BakePrevRotationMode"
MANUAL_COMPOUND_PROP = "Tumblify_ManualCompoundV1"
DISPLAY_PROXY_COLLECTION = "Tumblify_DisplayPreview"
DISPLAY_PROXY_PROP = "Tumblify_DisplayProxy"
DISPLAY_PREV_TYPE_PROP = "Tumblify_DisplayPrevType"
INSTANCE_RESULT_PROP = "Tumblify_InstanceResult"
INSTANCE_HELPER_PROP = "Tumblify_InstanceHelper"
INSTANCE_SOURCE_COLLECTION_PROP = "Tumblify_InstanceSources"

FAST_CURSOR_NAME = "Tumblify_Fast_Cursor"
FAST_CURSOR_RAY_NAME = "Tumblify_Fast_Cursor_Ray"
FAST_GHOST_NAME = "Tumblify_Fast_Ghost"
FAST_STATIC_SHAPE_PROP = "Tumblify_FastStaticShape"

MESH_COLLISION_TRI_BUDGET = 10_000
MESH_COLLISION_DECIMATE_TRIGGER = 14_000

# Explicit enum values preserve numeric IDs used by older scenes where practical.
SHAPES = [
    ('DETECT', 'Auto Detect', 'Fit Box / Sphere / Capsule / Cylinder from sampled surface points and normals; fall back to Convex Hull', 'MOD_PHYSICS', 11),
    ('BOX', 'Box', 'Fast box collision', 'MESH_CUBE', 0),
    ('SPHERE', 'Sphere', 'Fast sphere collision', 'MESH_UVSPHERE', 1),
    ('CAPSULE', 'Capsule', 'Capsule fitted inside the local bounds, along the longest axis', 'META_CAPSULE', 2),
    ('CYLINDER', 'Cylinder', 'Stable convex-cylinder fitted to the detected local axis', 'MESH_CYLINDER', 3),
    ('HULL', 'Convex Hull', 'Blender convex hull reduced to a safe Box3D physics hull', 'MESH_ICOSPHERE', 4),
    ('COMPOUND', 'Compound Hull', 'Concave approximation made from several convex hulls attached to one Box3D body', 'MOD_ARRAY', 5),
    ('LOOSE', 'Loose Parts Compound', 'Build one convex hull per disconnected mesh island; never substitute fitted primitives', 'MOD_BUILD', 7),
    ('CONTAINER', 'Container / Open Concave', 'Sample the real inner cavity and build an open compound container from convex wall segments', 'MESH_CYLINDER', 10),
    ('MANUAL', 'Manual Compound', 'Compound collision stored by Join Selected as Compound; each source object keeps its assigned collision proxy', 'AUTOMERGE_ON', 8),
    ('TERRAIN', 'Terrain / Height Field', 'Static upper surface sampled on a height grid; no caves or overhangs', 'MESH_GRID', 12),
    ('MESH', 'Mesh (Surface)', 'Static triangle surface collision; Auto preserves evaluated detail, Custom controls simplification', 'MESH_MONKEY', 9),
]

MATERIAL_PRESETS = {
    'CONCRETE': {'label': 'Concrete', 'density': 2400.0, 'friction': 0.80, 'restitution': 0.05},
    'RUBBER': {'label': 'Rubber', 'density': 1100.0, 'friction': 1.00, 'restitution': 0.65},
    'PLASTIC': {'label': 'Plastic', 'density': 950.0, 'friction': 0.40, 'restitution': 0.20},
    'WOOD': {'label': 'Wood', 'density': 700.0, 'friction': 0.55, 'restitution': 0.15},
    'METAL': {'label': 'Metal', 'density': 7850.0, 'friction': 0.45, 'restitution': 0.05},
    'STONE': {'label': 'Stone', 'density': 2700.0, 'friction': 0.75, 'restitution': 0.05},
}
MATERIAL_ITEMS = [(key, value['label'], f"Density {value['density']:.0f} kg/m³") for key, value in MATERIAL_PRESETS.items()]

EMITTER_WIRE_PROP = "Tumblify_EmitterPrevDisplayType"
EMITTER_SHOW_WIRE_PROP = "Tumblify_EmitterPrevShowWire"
EMITTER_ALL_EDGES_PROP = "Tumblify_EmitterPrevAllEdges"

# Every selector derives its labels, icons and ordering from the Advanced list.
SHAPE_LABELS = {item[0]: item[1] for item in SHAPES}
SHAPE_ICONS = {item[0]: item[3] for item in SHAPES}
FAST_COLLISION_CYCLE = tuple(item[0] for item in SHAPES if item[0] not in {'MESH','TERRAIN'})
FAST_STATIC_COLLISION_CYCLE = tuple(item[0] for item in SHAPES)
