# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Narrow compatibility helpers for scenes created by older Tumblify / Box3D Physics builds.

Keep migrations here instead of letting removed feature identifiers leak through the
production runtime. Compatibility code should be small, explicit and removable once
support for the corresponding scene generation is intentionally dropped.
"""

LEGACY_COLLISION_ALIASES = {
    # Auto Proxy was removed from the user-facing add-on in v0.13.40. Loose Parts is
    # the supported automatic multi-part replacement and matches the old intent best.
    'AUTO': 'LOOSE',
}


def archive_legacy_combined_results(objects, source_names):
    """Keep ambiguous old hidden Combined results as reversible visual backups.

    The user chose Fix/Unfix over guessing which old hidden results were imported
    by Interactive. Restrict this migration to sources of recovered legacy batches;
    newer snapshots carry explicit ownership/schema metadata instead.
    """
    archived=0
    for obj in objects:
        if (str(obj.get('Tumblify_SourceName','')) not in source_names
                or not obj.get('Tumblify_InstanceResult',False)
                or not obj.get('Tumblify_FrozenStatic',False)
                or obj.get('Tumblify_FixedVisualOnly',False)
                or obj.get('Tumblify_ActiveInstanceSnapshot',False)
                or not obj.hide_viewport or not obj.hide_render):
            continue
        obj['Tumblify_FixedVisualOnly']=True
        obj['Tumblify_FrozenStatic']=False
        obj['Tumblify_RecoveryArchive']=True
        obj.tumblify.role='NONE'
        archived+=1
    return archived


def migrate_collision_shape(shape):
    """Map a legacy collision identifier to a currently supported identifier."""
    key = str(shape or 'HULL').upper()
    return LEGACY_COLLISION_ALIASES.get(key, key)






def resolve_surface_emitter(settings, scene, emitter):
    """Resolve the v0.10.x ``scatter_emitter`` slot when opening an old scene.

    Returns ``(emitter, migrated)``. New scenes never need this path.
    """
    try:
        if emitter and emitter.name in scene.objects:
            return emitter, False
        legacy = getattr(settings, 'scatter_emitter', None)
        if legacy and legacy.name in scene.objects:
            return legacy, True
    except Exception:
        pass
    return emitter, False


# Product namespace used before the Tumblify rename. These strings exist only to migrate
# old .blend files; production runtime uses the Tumblify namespace exclusively.
LEGACY_PRODUCT_PREFIX = "Box3D_"
CURRENT_PRODUCT_PREFIX = "Tumblify_"
LEGACY_SCENE_SETTINGS_KEY = "box3d_paint"
LEGACY_OBJECT_SETTINGS_KEY = "box3d_paint"

LEGACY_ADV_DEMO_SOURCE_NAME = "Box3D_Demo_Source"
LEGACY_DATABLOCK_PREFIXES = {
    "Box3D_": "Tumblify_",
    "B3BATCH_": "Tumblify_Batch_",
    "B3DISPLAY_": "Tumblify_Display_",
    "B3FAST_": "Tumblify_Fast_",
    "B3RESULT_": "Tumblify_Result_",
    "B3INST_": "Tumblify_Instances_",
    "B3COL_": "Tumblify_Collision_",
}


def migrate_legacy_datablock_name(name):
    """Return the Tumblify datablock name for a known legacy product prefix."""
    value = str(name or "")
    for old_prefix, new_prefix in LEGACY_DATABLOCK_PREFIXES.items():
        if value.startswith(old_prefix):
            return new_prefix + value[len(old_prefix):]
    return value


def migrate_legacy_idprops(id_block):
    """Copy legacy Box3D_* custom properties to Tumblify_* keys, then remove old keys.

    This is intentionally data-only: it does not create helpers or modify physics state.
    """
    try:
        keys = list(id_block.keys())
    except Exception:
        return 0
    changed = 0
    for key in keys:
        if not isinstance(key, str) or not key.startswith(LEGACY_PRODUCT_PREFIX):
            continue
        new_key = CURRENT_PRODUCT_PREFIX + key[len(LEGACY_PRODUCT_PREFIX):]
        try:
            if new_key not in id_block:
                id_block[new_key] = id_block[key]
            del id_block[key]
            changed += 1
        except Exception:
            pass
    return changed


def migrate_legacy_property_group(id_block, current_settings, legacy_key):
    """Best-effort one-time copy from a legacy PropertyGroup IDProperty container."""
    marker = "Tumblify_LegacySettingsMigrated_v01459"
    try:
        if bool(id_block.get(marker, False)):
            return 0
        legacy = id_block.get(legacy_key)
    except Exception:
        legacy = None
    if legacy is None:
        return 0
    changed = 0
    try:
        items = list(legacy.items())
    except Exception:
        return 0
    for key, value in items:
        if not hasattr(current_settings, key):
            continue
        try:
            setattr(current_settings, key, value)
            changed += 1
        except Exception:
            pass
    # Do not delete the legacy group here. Blender may still own RNA metadata for an
    # older session; leaving the orphaned IDProperty is safer than invalidating undo.
    try:
        id_block[marker] = True
    except Exception:
        pass
    return changed


def migrate_product_namespace(bpy):
    """Import saved scenes from the former product namespace once per datablock."""
    old='Pile'+'Up'
    old_key=old.lower()
    def rename_value(value):
        if isinstance(value,str): return value.replace(old,'Tumblify').replace(old_key,'tumblify')
        if isinstance(value,dict): return {k.replace(old,'Tumblify'):rename_value(v) for k,v in value.items()}
        if isinstance(value,list): return [rename_value(v) for v in value]
        return value
    def copy_settings(dst,values):
        for key,value in values.items():
            prop=dst.bl_rna.properties.get(key)
            if prop is None: continue
            if prop.type=='COLLECTION':
                collection=getattr(dst,key);collection.clear()
                for row in value:copy_settings(collection.add(),row)
            elif prop.type=='ENUM' and isinstance(value,int): dst[key]=value
            else: setattr(dst,key,value)
    blocks=[]
    for name in ('scenes','objects','collections','meshes','node_groups','actions','materials'):
        blocks.extend(list(getattr(bpy.data,name)))
    for block in blocks:
        if block.get('Tumblify_NamespaceMigrated_118'): continue
        if old_key in block:
            raw=block[old_key]
            values=rename_value(raw.to_dict())
            copy_settings(block.tumblify,values)
        for key in list(block.keys()):
            if key.startswith(old):
                value=block[key]
                if hasattr(value,'to_dict'):value=value.to_dict()
                block[key.replace(old,'Tumblify',1)]=rename_value(value)
            elif key.startswith('Tumblify') and isinstance(block[key],str):
                block[key]=rename_value(block[key])
        if old in block.name: block.name=block.name.replace(old,'Tumblify')
        for mod in getattr(block,'modifiers',()):
            mod.name=mod.name.replace(old,'Tumblify')
        block['Tumblify_NamespaceMigrated_118']=True



def cleanup_retired_helpers(bpy):
    """Retire saved viewport helpers and obsolete spatial settings from older files."""
    names={'Tumblify_Advanced_Zone','Tumblify_Fast_Zone'}
    for obj in list(bpy.data.objects):
        if obj.library or obj.name not in names or not obj.get('Tumblify_Helper'): continue
        mesh=obj.data
        bpy.data.objects.remove(obj,do_unlink=True)
        if mesh is not None and mesh.users==0: bpy.data.meshes.remove(mesh)
    for scene in bpy.data.scenes:
        if scene.library or not hasattr(scene,'tumblify'): continue
        settings=scene.tumblify
        for key in list(settings.keys()):
            if 'zone' in key.lower() or key=='advanced_static_mode': del settings[key]
        if settings.get('fast_static_scope')=='ZONE': settings.fast_static_scope='SCENE'
    for obj in bpy.data.objects:
        if obj.library: continue
        for key in list(obj.keys()):
            if key.startswith('Tumblify_') and 'zone' in key.lower(): del obj[key]
