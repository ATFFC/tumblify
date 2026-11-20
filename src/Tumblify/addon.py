# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.


import bpy, bmesh, random, time, math, blf, json, re, traceback, gpu
try:
    import numpy as _np
except Exception:
    _np=None
from array import array
from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty, BoolProperty, PointerProperty, CollectionProperty, IntProperty, StringProperty
from mathutils import Quaternion, Vector, Euler, Matrix
from mathutils.bvhtree import BVHTree
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from bpy.app.handlers import persistent
from .constants import ADDON_VERSION
from .bridge import World, get_lib, CollisionGeometryError
from .bake_cache import BakeCacheFiles, select_backend
from .mesh_cache import clear_mesh_cache, mesh_cache_info
from .compat import migrate_collision_shape, resolve_surface_emitter, migrate_legacy_idprops, migrate_legacy_property_group, migrate_legacy_datablock_name, cleanup_retired_helpers, migrate_product_namespace, LEGACY_SCENE_SETTINGS_KEY, LEGACY_OBJECT_SETTINGS_KEY, LEGACY_ADV_DEMO_SOURCE_NAME

from . import snap as _fast_snap
from .scene_collisions import (TU_OT_prepare_scene, get_scene_cache as _get_scene_cache,
    invalidate_scene as _invalidate_scene_collisions, clear_scene_caches as _clear_scene_collisions,
    validate_scene_cache as _validate_scene_collisions, cached_static_data as _cached_scene_collision,
    preview_objects as _prepared_scene_preview_objects, build_scene_wire as _build_scene_collision_wire,
    clear_wire as _clear_scene_collision_wire, invalidate_updated_inputs as _invalidate_scene_inputs)

_RUNTIME=None
STOP_REQUESTED=False
_DEBUG_REFRESH_PENDING=False
_COLLISION_DEBUG_TARGETED_EDIT=False
_FAST_COLLISION_OVERLAY_RUNNING=False
_COLLISION_OVERLAY_OWNER=None
_COLLISION_POPUP_LOCKED=False
_COLLISION_POPUP_OWNER=None
_OWNED_APP_TIMERS=set()
_NATIVE_TRANSFORM_CLEANUPS=set()
_DISPLAY_UPDATE_SUSPENDED=False
_HISTORY_HAS_RUNTIME_SNAPSHOTS=False
_COLLISION_PREVIEW_MEMBERSHIP=None


_PREPARATION_DISPLAYS=set()


class _RealtimePreparation:
    """Shared preparation/calculation display; status updates are time-throttled."""
    def __init__(self,context,label,action='Preparing simulation',synchronous=True):
        self.context=context;self.label=label;self.action=action;self.synchronous=synchronous;self.force_paint=False;self.percent=0.0;self.stage='Reading scene'
        self.handle=None;self.opened=False;self.last_paint=0.0;self.closed=False
        self.window=context.window;self.area=context.area or next((area for area in context.window.screen.areas if area.type=='VIEW_3D'),None) if context.window else None
        self.region=next((r for r in self.area.regions if r.type=='WINDOW'),context.region) if self.area else context.region
    def begin(self):
        _PREPARATION_DISPLAYS.add(self)
        try:
            self.context.window_manager.progress_begin(0,100);self.opened=True
        except Exception:pass
        if not bpy.app.background:
            try:self.handle=bpy.types.SpaceView3D.draw_handler_add(self._draw,(),'WINDOW','POST_PIXEL')
            except Exception:pass
        self.update(0,'Reading scene',force=True)
    def update(self,percent,stage=None,force=False):
        self.percent=max(self.percent,min(100.0,max(0.0,float(percent))))
        if stage is not None:self.stage=str(stage)
        now=time.monotonic()
        if not force and now-self.last_paint<0.08:return
        self.last_paint=now
        self.force_paint=force
        try:self.context.window_manager.progress_update(self.percent)
        except Exception:pass
        try:self.context.workspace.status_text_set(f'Tumblify {self.label}: {self.stage} - {self.percent:.0f}%')
        except Exception:pass
        self._paint()
    def items(self,items,start,end,stage):
        total=len(items)
        self.update(start,stage)
        for index,item in enumerate(items):
            name=getattr(item,'name','')
            self.update(start+(end-start)*index/max(1,total),f'{stage} {index+1}/{total}' + (f' | {name}' if name else ''))
            yield item
            self.update(start+(end-start)*(index+1)/max(1,total))
        self.update(end,stage)
    def _paint(self):
        if bpy.app.background:return
        try:
            self.area.tag_redraw()
            if not self.synchronous and not self.force_paint and not self.closed:return
            # Redraw only: do not pump input or run an event loop during world construction.
            with bpy.context.temp_override(window=self.window,area=self.area,region=self.region):
                bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP',iterations=1)
        except (RuntimeError,ReferenceError,AttributeError):pass
    def _draw(self):
        context=bpy.context
        if context.window!=self.window or context.region!=self.region:return
        width=min(420,max(160,self.region.width-48));x=(self.region.width-width)*.5;y=54
        shader=gpu.shader.from_builtin('UNIFORM_COLOR')
        def rect(x0,y0,w,h,color):
            shader.bind();shader.uniform_float('color',color)
            batch_for_shader(shader,'TRIS',{'pos':[(x0,y0),(x0+w,y0),(x0+w,y0+h),(x0,y0),(x0+w,y0+h),(x0,y0+h)]}).draw(shader)
        gpu.state.blend_set('ALPHA')
        try:
            rect(x-14,y-13,width+28,86,(.035,.045,.055,.94))
            rect(x,y,width,7,(.20,.23,.26,1))
            rect(x,y,width*self.percent/100.0,7,(.30,.59,.90,1))
            blf.size(0,14);blf.color(0,.95,.96,.98,1);blf.position(0,x,y+43,0)
            blf.draw(0,f'{self.label} | {self.action}  {self.percent:.0f}%')
            text=self.stage
            blf.size(0,12)
            while len(text)>4 and blf.dimensions(0,text)[0]>width:text=text[:-4]+'...'
            blf.color(0,.72,.77,.83,1);blf.position(0,x,y+23,0);blf.draw(0,text)
        finally:gpu.state.blend_set('NONE')
    def close(self):
        if self.closed:return
        self.closed=True
        if self.handle is not None:
            try:bpy.types.SpaceView3D.draw_handler_remove(self.handle,'WINDOW')
            except (ValueError,ReferenceError,RuntimeError):pass
            self.handle=None
        if self.opened:
            try:self.context.window_manager.progress_end()
            except Exception:pass
            self.opened=False
        try:self.context.workspace.status_text_set(None)
        except Exception:pass
        _PREPARATION_DISPLAYS.discard(self)
        self._paint()


def _register_owned_app_timer(callback,*,first_interval):
    """Track transient callbacks and forget them when their timer finishes."""
    def _dispatch():
        try:
            interval=callback()
        except BaseException:
            _OWNED_APP_TIMERS.discard(_dispatch)
            raise
        if interval is None: _OWNED_APP_TIMERS.discard(_dispatch)
        return interval
    bpy.app.timers.register(_dispatch,first_interval=first_interval)
    _OWNED_APP_TIMERS.add(_dispatch)
    return _dispatch


def _cancel_owned_app_timers():
    global _DEBUG_REFRESH_PENDING, _COLLISION_POPUP_LOCKED, _COLLISION_POPUP_OWNER
    for callback in tuple(_OWNED_APP_TIMERS):
        if bpy.app.timers.is_registered(callback): bpy.app.timers.unregister(callback)
    _OWNED_APP_TIMERS.clear()
    _DEBUG_REFRESH_PENDING=False; _COLLISION_POPUP_LOCKED=False; _COLLISION_POPUP_OWNER=None


def _retire_native_rotate_target(owner,context):
    """Keep Blender's transform target alive until its native modal releases it.

    This narrowly scoped timer intentionally survives add-on disable. It captures
    only Blender resources and its own registry, never a disposed Tumblify operator.
    Non-persistent timers are discarded by Blender when loading another file.
    """
    if not bool(getattr(owner,'fast_native_rotate',False)): return
    owner._fast_native_rotate_sync(context)
    target=bpy.data.objects.get(FAST_CURSOR_NAME)
    if target is None: return
    window=context.window
    target.name=FAST_CURSOR_NAME+'_Retiring'
    registry=_NATIVE_TRANSFORM_CLEANUPS
    def cleanup():
        try:
            running=any(str(getattr(op,'bl_idname','')).lower() in
                        {'transform_ot_rotate','transform_ot_trackball'}
                        for op in window.modal_operators)
        except ReferenceError:
            running=False
        if running: return .03
        try: bpy.data.objects.remove(target,do_unlink=True)
        except (ReferenceError,RuntimeError): pass
        registry.discard(cleanup)
        return None
    bpy.app.timers.register(cleanup,first_interval=.03)
    registry.add(cleanup)


def _normalize_instance_display(scene):
    """Normalize legacy display state without a second callback-driven rebuild.

    Startup/load callers apply display once after their other synchronization.
    Ordinary user property edits still use the immediate update callback.
    """
    global _DISPLAY_UPDATE_SUSPENDED
    previous=_DISPLAY_UPDATE_SUSPENDED
    _DISPLAY_UPDATE_SUSPENDED=True
    try:
        if scene.tumblify.instance_display not in {'MESH','POINT','BOX'}:scene.tumblify.instance_display='MESH'
    finally:
        _DISPLAY_UPDATE_SUSPENDED=previous


def _set_runtime_owner(owner,context):
    """Retain the launch context for teardown from Preferences or another editor."""
    global _RUNTIME
    owner._tumblify_shutdown=False
    owner._tumblify_runtime_context={name:getattr(context,name) for name in ('window','area','region','scene','view_layer')}
    _RUNTIME=owner
    if not isinstance(owner,TU_OT_drop_offline):_solid_state_begin()


def _runtime_owner_shutdown(owner):
    try: return bool(getattr(owner,'_tumblify_shutdown',False))
    except ReferenceError: return True


def _release_static_as_dynamic(owner,index):
    """Promote one moved convex collider using each runtime's existing data arrays."""
    if index<0 or index>=len(owner.static_objs) or index not in owner.static_kinematic_indices: return False
    obj=owner.static_objs[index]
    if not _is_live_object(obj):
        owner.static_kinematic_indices.discard(index); return False
    collisions=owner.static_collision
    data=collisions.get(obj.name) if isinstance(collisions,dict) else collisions[index]
    if data is None or str(data.get('shape','MESH')) in {'MESH','TERRAIN'}: return False
    p,q,_=world_pose_scale(obj); mf,mr,md=_physics_material(obj)
    try:
        handle=add_body(owner.world,World.DYNAMIC,obj,owner.depsgraph,p_override=p,q_override=q,
                        simplify=(getattr(owner,'_tumblify_runtime_context',{}).get('scene') or bpy.context.scene).tumblify.convex_simplify,prepared=data,
                        friction=mf,restitution=mr,density=md)
    except Exception:
        owner._convert_static_to_static(index)
        return False
    old=owner.static_handles[index]
    if old is not None: owner.world.destroy(old)
    owner.static_handles[index]=None; owner.static_objs[index]=None
    owner.static_kinematic_indices.discard(index)
    preserved=getattr(owner,'_promoted_static_objects',None)
    if preserved is None: owner._promoted_static_objects=preserved=set()
    preserved.add(obj)
    owner.dynamic_handles.append(handle); owner.dynamic_objs.append(obj); owner.dynamic_scales.append(1.0)
    if hasattr(owner,'dynamic_source_names'):
        owner.dynamic_source_names.append(obj.name)
        owner.dynamic_cache.append([float(p.x),float(p.y),float(p.z),float(q[0]),float(q[1]),float(q[2]),float(q[3])])
        owner.source_collision[obj.name]=data
    else:
        owner.dynamic_collision.append(data); owner.dynamic_material.append(obj)
    if hasattr(owner,'dynamic_targets'): owner.dynamic_targets.append(tuple(p))
    return True


def _finish_runtime_move(owner):
    """Capture the final Blender pose before simulation or result serialization."""
    finish_fill=getattr(owner,'_finish_fill_emitter_move',None)
    if finish_fill is not None: finish_fill()
    owner._release_all_kinematics()
    owner.pending_move_finish=False; owner.moving_selected=False


def _poll_runtime_transform_completion(owner,context):
    """Observe native transform exit even when Blender consumes its confirm event."""
    window=getattr(owner,'_tumblify_runtime_context',{}).get('window') or context.window
    try:
        running=any(str(getattr(op,'bl_idname','')).lower().startswith('transform_ot_')
                    for op in window.modal_operators)
    except (AttributeError,ReferenceError): return
    if running:
        owner._tumblify_transform_seen=True
        owner.pending_move_finish=False
    elif (getattr(owner,'_tumblify_transform_seen',False) or
          time.monotonic()-getattr(owner,'_tumblify_transform_started',time.monotonic())>.7):
        owner.pending_move_finish=True


def _dispatch_runtime_modal(owner,context,event,label):
    """One late-event, active-Undo and exception boundary for all physics modals."""
    if _runtime_owner_shutdown(owner): return {'CANCELLED'}
    try:
        if event.type=='Z' and event.value=='PRESS' and event.ctrl:
            owner.report({'WARNING'},'Undo is disabled during active physics. Stop the simulation first.')
            return {'RUNNING_MODAL'}
        if event.type=='TIMER' and bool(getattr(owner,'moving_selected',False)):
            _poll_runtime_transform_completion(owner,context)
        result=owner._modal_impl(context,event)
        if event.type in {'G','R'} and event.value=='PRESS' and bool(getattr(owner,'moving_selected',False)):
            owner._tumblify_transform_seen=False
            owner._tumblify_transform_started=time.monotonic()
        return result
    except Exception as exc:
        return _runtime_emergency_abort(owner,context,exc,label)


def _shutdown_active_runtime(context):
    """Finish owned resources synchronously, while registered RNA is still usable."""
    global _RUNTIME, STOP_REQUESTED, _COLLISION_OVERLAY_OWNER
    owner=_RUNTIME
    if owner is not None:
        owner._tumblify_shutdown=True
        def finish():
            if isinstance(owner,TU_OT_drop_offline): owner._cleanup(bpy.context,True)
            elif isinstance(owner,TU_OT_run): owner._finish_keep(bpy.context)
            elif isinstance(owner,(TU_OT_surface_run,TU_OT_post_run)): owner._finish(bpy.context)
        try:
            with bpy.context.temp_override(**getattr(owner,'_tumblify_runtime_context',{})):
                finish()
        except Exception as exc:
            if isinstance(owner,TU_OT_drop_offline):
                owner._cleanup(context,True)
            else:
                _runtime_emergency_abort(owner,context,exc,'Tumblify disable')
        finally:
            _solid_state_restore()
            _RUNTIME=None
    overlay=_COLLISION_OVERLAY_OWNER
    if overlay is not None:
        overlay._tumblify_shutdown=True
        try: overlay._finish(context)
        except (ReferenceError,RuntimeError): pass
    STOP_REQUESTED=True

def _draw_interactive_selection_box(start, end):
    """Draw a Blender-like box-select marquee in VIEW_3D POST_PIXEL coordinates."""
    if start is None or end is None:
        return
    x1, y1 = float(start[0]), float(start[1])
    x2, y2 = float(end[0]), float(end[1])
    left, right = min(x1, x2), max(x1, x2)
    bottom, top = min(y1, y2), max(y1, y2)
    if right-left < 1.0 or top-bottom < 1.0:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    fill = batch_for_shader(shader, 'TRIS', {'pos': [
        (left, bottom), (right, bottom), (right, top),
        (left, bottom), (right, top), (left, top),
    ]})
    outline = batch_for_shader(shader, 'LINE_STRIP', {'pos': [
        (left, bottom), (right, bottom), (right, top), (left, top), (left, bottom),
    ]})
    try:
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float('color', (0.53, 0.72, 1.0, 0.12))
        fill.draw(shader)
        gpu.state.line_width_set(1.5)
        shader.uniform_float('color', (0.65, 0.82, 1.0, 0.95))
        outline.draw(shader)
    finally:
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')

def _hide_transform_hud(owner, context):
    """Hide Blender's bottom-left Adjust Last Operation/Move HUD while interactive physics is active."""
    try:
        space=getattr(context, 'space_data', None)
        if space is None or getattr(space, 'type', None) != 'VIEW_3D' or not hasattr(space, 'show_region_hud'):
            return
        # Preserve the original state once for the whole interactive session.
        if not hasattr(owner, '_b3_prev_show_region_hud'):
            owner._b3_prev_show_region_hud=bool(space.show_region_hud)
            owner._b3_hud_space=space
        space.show_region_hud=False
    except Exception:
        pass

def _restore_transform_hud(owner, context=None):
    """Restore the viewport HUD state saved by _hide_transform_hud()."""
    try:
        if not hasattr(owner, '_b3_prev_show_region_hud'):
            return
        space=getattr(owner, '_b3_hud_space', None)
        if space is None and context is not None:
            space=getattr(context, 'space_data', None)
        if space is not None and hasattr(space, 'show_region_hud'):
            space.show_region_hud=bool(owner._b3_prev_show_region_hud)
    except Exception:
        pass
    finally:
        for name in ('_b3_prev_show_region_hud', '_b3_hud_space'):
            try:
                delattr(owner, name)
            except Exception:
                pass
from .constants import BRUSH_NAME, GUIDE_NAME, GUIDE_MOD_NAME, GUIDE_GROUP_PREFIX, SPAWN_COLLECTION, DEBUG_COLLECTION, FROZEN_PROP, RUNTIME_PROP, FIXED_PROP, FAST_COLLECTION, AUTO_DETECTED_SHAPE_PROP, AUTO_DETECTED_AXIS_PROP, DISPLAY_MOD_NAME, INSTANCE_RESULT_MOD_NAME, DISPLAY_GROUP_PREFIX, EMITTER_BAKE_ACTION_PREFIX, EMITTER_BAKE_PROP, BAKE_PREV_ACTION_PROP, BAKE_PREV_ROT_PROP, MANUAL_COMPOUND_PROP, DISPLAY_PROXY_COLLECTION, DISPLAY_PROXY_PROP, DISPLAY_PREV_TYPE_PROP, INSTANCE_RESULT_PROP, INSTANCE_HELPER_PROP, INSTANCE_SOURCE_COLLECTION_PROP, FAST_CURSOR_NAME, FAST_CURSOR_RAY_NAME, FAST_GHOST_NAME, FAST_STATIC_SHAPE_PROP, MESH_COLLISION_TRI_BUDGET, MESH_COLLISION_DECIMATE_TRIGGER, SHAPES, MATERIAL_PRESETS, MATERIAL_ITEMS, EMITTER_WIRE_PROP, EMITTER_SHOW_WIRE_PROP, EMITTER_ALL_EDGES_PROP, FAST_COLLISION_CYCLE, FAST_STATIC_COLLISION_CYCLE, SHAPE_ICONS, SHAPE_LABELS
from .collision import (
    world_pose_scale,
    _evaluated_mesh_data,
    _emitter_bound_box_without_guide,
    _emitter_world_bound_points,
    _evaluated_collision_mesh_data,
    _local_bounds,
    _long_axis,
    _cylinder_axis,
    _axis_radius_half,
    _axis_map_point,
    _dedupe_points,
    _prefilter_hull_input,
    _convex_hull_from_points,
    _farthest_point_reduce,
    _evaluated_convex_geometry,
    _poly_volume,
    _triangle_set_volume,
    _compound_split_cluster,
    _compound_cluster_score,
    _compound_hull_for_triangles,
    _evaluated_compound_geometry,
    _auto_pca_frame,
    _auto_from_local,
    _auto_primitive_part,
    _loose_triangle_components,
    _normal_patch_components,
    _evaluated_loose_parts_geometry,
    _container_mesh_bvh,
    _container_solid_inside,
    _container_sample_ring,
    _container_cavity_profile,
    _container_collision_profile,
    _container_profile_radius,
    _container_profile_contains,
    _container_surface_parts,
    _data_as_compound_parts,
    _axis_y_quaternion,
    _quat_tuple_xyzw,
    _basis_quaternion,
    _native_parts_from_data,
    _manual_mixed_parts,
    _manual_compound_parts,
    _box_geometry,
    _sphere_geometry,
    _capsule_geometry,
    _cylinder_geometry,
    _hull_debug_geometry,
    _auto_hull_surface_samples,
    _weighted_median,
    _primitive_fit_quality,
    _auto_collision_result
)
from .display import _solid_state_begin, _solid_state_sync, _solid_state_restore, _pile_objects
from .display import (
    _is_live_object,
    _spawn_master_collection,
    _spawn_session_collections,
    _spawned_objects,
    spawned_collection,
    _fast_collection,
    _clear_fast_collection,
    _remove_display_modifier,
    _restore_instance_display_type,
    _remember_instance_display_type,
    _configure_instance_display,
    _build_batch_proxy_nodes,
    _display_proxy_collection,
    _clear_display_proxy_collection,
    _regular_display_proxy_needed,
    _sync_regular_display_proxy,
    _apply_global_instance_display,
    _new_fast_proxy,
    _rebatch_runtime_after_interactive,
    _fast_proxy_write,
    _recover_saved_runtime,
    _instance_source_collection,
    _cleanup_instance_source_collection,
    _force_real_mesh_display,
    _force_fast_result_mesh_display,
    _instance_result_helper,
    _remove_instance_result_helper,
    _sync_visual_state_from_source,
    _evaluated_visual_mesh,
    _build_instance_result_nodes,
    _create_instance_result,
    _update_instance_result_in_place,
    _instance_result_entries,
    _frozen_instance_results,
    _runtime_to_instance_results,
    _realize_instance_object,
    _realize_instance_results
)
from .emitter import (
    _collection_emitter_objects,
    _configured_emitter_objects,
    _restore_emitter_wire_object,
    _sync_emitter_wire,
    _emitter_objects,
    _remove_emitter_guide_nodes,
    _ensure_emitter_guide_nodes,
    _sync_emitter_guides,
    _advanced_emitter_objects,
    _advanced_emitter_bounds,
    _scatter_surface_sampler,
    _fill_point_inside,
    _fill_destroy_container,
    _fill_update_pose_from_object,
    _fill_make_bounds_kinematic,
    _fill_detect_standard_shape,
    _fill_prism_assets_from_triangles,
    _fill_build_standard_kinematic_assets,
    _fill_build_mesh_kinematic_assets,
    _fill_choose_point,
)

_EMITTER_SYNCING=False


def _emitter_wire_display_update(self,context):
    scene=getattr(self,'id_data',None) or (context.scene if context else None)
    try: _sync_emitter_wire(scene)
    except Exception: pass

def _advanced_remove_addon_emitter(scene):
    """Remove only the temporary emitter created by Tumblify itself.

    User-provided emitters are never touched. The helper is safe to call after
    the emitter pointer has already been cleared from RNA.
    """
    o=bpy.data.objects.get(BRUSH_NAME)
    if o is None:
        return False
    try:
        me=o.data if o.type=='MESH' else None
    except Exception:
        me=None
    try:
        bpy.data.objects.remove(o,do_unlink=True)
    except Exception:
        return False
    if me is not None:
        try:
            if me.users==0: bpy.data.meshes.remove(me)
        except Exception:
            pass
    return True

def _emitter_object_update(self, context):
    global _EMITTER_SYNCING
    if _EMITTER_SYNCING:
        return
    scene = getattr(self, 'id_data', None) or (context.scene if context else None)
    try:
        _EMITTER_SYNCING = True
        o = getattr(self, 'emitter_object', None)
        if o and o.name not in {BRUSH_NAME, GUIDE_NAME}:
            self.emitter_collection = None
            self.emitter_mode = 'CUSTOM'
        elif not getattr(self, 'emitter_collection', None):
            self.emitter_mode = 'FIXED'
    finally:
        _EMITTER_SYNCING = False
    _sync_emitter_wire(scene)
    if scene:
        if not getattr(self, 'emitter_object', None) and (not getattr(self, 'emitter_collection', None)):
            try:
                self.advanced_emitter_ready = False
                _advanced_remove_legacy_demo_source(scene)
                _advanced_remove_addon_emitter(scene)
            except Exception:
                pass
        update_emitter_guide(scene)
    _tag_view3d_redraw_all()

def _emitter_collection_update(self, context):
    global _EMITTER_SYNCING
    if _EMITTER_SYNCING:
        return
    scene = getattr(self, 'id_data', None) or (context.scene if context else None)
    try:
        _EMITTER_SYNCING = True
        if getattr(self, 'emitter_collection', None):
            self.emitter_object = None
            self.emitter_mode = 'COLLECTION'
        elif not getattr(self, 'emitter_object', None):
            self.emitter_mode = 'FIXED'
    finally:
        _EMITTER_SYNCING = False
    _sync_emitter_wire(scene)
    if scene:
        if not getattr(self, 'emitter_object', None) and (not getattr(self, 'emitter_collection', None)):
            try:
                self.advanced_emitter_ready = False
                _advanced_remove_legacy_demo_source(scene)
                _advanced_remove_addon_emitter(scene)
            except Exception:
                pass
        update_emitter_guide(scene)
    _tag_view3d_redraw_all()

def _spawn_location_update(self,context):
    """Surface emitters are real scene geometry, not Drop gizmos.

    Volume mode uses Wire + guide; Surface mode restores the object's normal
    display and removes the guide modifier so the emitter remains a clean mesh
    collider and mask surface.
    """
    scene=getattr(self,'id_data',None) or (context.scene if context else None)
    if scene is None: return
    try: _sync_emitter_wire(scene)
    except Exception: pass
    try: update_emitter_guide(scene)
    except Exception: pass
    try:
        brush=bpy.data.objects.get(BRUSH_NAME)
        if brush and brush.name in scene.objects:
            if getattr(self,'spawn_location','VOLUME')=='SURFACE':
                brush.display_type='SOLID'; _remove_emitter_guide_nodes(brush)
            elif bool(getattr(self,'fill_emitter',False)):
                brush.display_type='WIRE'; _remove_emitter_guide_nodes(brush)
            elif not _configured_emitter_objects(scene):
                brush.display_type='WIRE'; _ensure_emitter_guide_nodes(brush)
    except Exception: pass

def _display_settings_update(self, context):
    if _DISPLAY_UPDATE_SUSPENDED: return
    scene=getattr(self,'id_data',None) or (context.scene if context else None)
    if scene is None: return
    try:
        _apply_global_instance_display(scene)
    except Exception:
        pass

def _tag_view3d_redraw_all():
    """Redraw every 3D View, including its sidebar UI regions."""
    try:
        wm=bpy.context.window_manager
        if wm is None: return
        for win in wm.windows:
            scr=getattr(win,'screen',None)
            if scr is None: continue
            for area in scr.areas:
                if area.type=='VIEW_3D':
                    try: area.tag_redraw()
                    except Exception: pass
    except Exception:
        pass

def _request_collision_debug_refresh(scene):
    """Debounced rebuild of visible collision debug geometry after live setting edits."""
    global _DEBUG_REFRESH_PENDING
    if scene is None or not bool(getattr(getattr(scene,'tumblify',None),'show_collision_debug',False)):
        return
    if _DEBUG_REFRESH_PENDING:
        return
    _DEBUG_REFRESH_PENDING=True
    scene_name=str(scene.name)
    def _timer():
        global _DEBUG_REFRESH_PENDING
        _DEBUG_REFRESH_PENDING=False
        sc=bpy.data.scenes.get(scene_name)
        if sc is None or not bool(getattr(getattr(sc,'tumblify',None),'show_collision_debug',False)):
            return None
        try: _refresh_collision_debug(sc)
        except Exception: pass
        return None
    try: _register_owned_app_timer(_timer,first_interval=0.035)
    except Exception: _DEBUG_REFRESH_PENDING=False

# Shared preset definitions. User presets are persisted in add-on Preferences.
_PRESET_FIELDS=('solver_scale','contact_hertz','contact_damping','contact_push_speed','contact_recycle_distance','precision_auto_bullet','precision_disable_recycling','physics_frequency','physics_substeps')
_FACTORY_PROFILES={
 'FAST':dict(solver_scale=1.0,contact_hertz=30.0,contact_damping=10.0,contact_push_speed=3.0,contact_recycle_distance=0.05,precision_auto_bullet=False,precision_disable_recycling=False,physics_frequency=60,physics_substeps=1),
 'BALANCED':dict(solver_scale=1.0,contact_hertz=65.0,contact_damping=9.0,contact_push_speed=4.0,contact_recycle_distance=0.03,precision_auto_bullet=False,precision_disable_recycling=False,physics_frequency=120,physics_substeps=2),
 'STACKING':dict(solver_scale=1.0,contact_hertz=240.0,contact_damping=8.0,contact_push_speed=4.0,contact_recycle_distance=0.02,precision_auto_bullet=False,precision_disable_recycling=False,physics_frequency=240,physics_substeps=8),
 'SMALL':dict(solver_scale=10.0,contact_hertz=60.0,contact_damping=8.0,contact_push_speed=4.0,contact_recycle_distance=0.01,precision_auto_bullet=True,precision_disable_recycling=True,physics_frequency=120,physics_substeps=2),
}
_PRESET_NAMES={'FAST':'Fast','BALANCED':'Balanced','STACKING':'Stable Stacking','SMALL':'Small Objects'}
_CONTACT_PRESET_APPLYING=False

def _tumblify_preferences(context=None):
    try:
        prefs=(context or bpy.context).preferences
        a=prefs.addons.get(__package__)
        return a.preferences if a else None
    except Exception: return None

_PRESET_ENUM_CACHE=[]
def _preset_items(self,context):
    global _PRESET_ENUM_CACHE
    items=[(key,_PRESET_NAMES[key],f'Factory preset: {key}') for key in ('FAST','BALANCED','STACKING','SMALL')]
    prefs=_tumblify_preferences(context)
    if prefs:
        for item in prefs.custom_presets:
            if item.uid not in _FACTORY_PROFILES: items.append((f'USER_{item.uid}',item.name or 'User Preset','Saved custom tuning'))
    items.append(('CUSTOM','Custom','Manual contact tuning'))
    _PRESET_ENUM_CACHE=items  # keep dynamic enum string references alive for Blender RNA
    return _PRESET_ENUM_CACHE

def _preset_profile(key,context=None):
    if key=='DEFAULT': key='BALANCED'
    if key in _FACTORY_PROFILES:
        prefs=_tumblify_preferences(context)
        if prefs:
            for item in prefs.custom_presets:
                if item.uid==key: return {field:getattr(item,field) for field in _PRESET_FIELDS}
        return _FACTORY_PROFILES[key]
    if key.startswith('USER_'):
        prefs=_tumblify_preferences(context)
        if prefs:
            for item in prefs.custom_presets:
                if item.uid==key[5:]:
                    return {field:getattr(item,field) for field in _PRESET_FIELDS}
    return None

def _physics_setting_update(self,context):
    scene=context.scene if context and getattr(context,'scene',None) else None
    if scene is not None:
        try: _request_collision_debug_refresh(scene)
        except Exception: pass

def _contact_setting_update(self,context):
    if not _CONTACT_PRESET_APPLYING and getattr(self,'contact_preset','CUSTOM') != 'CUSTOM':
        try: self.contact_preset='CUSTOM'
        except (AttributeError,TypeError,ValueError): pass
    _physics_setting_update(self,context)

def _contact_preset_update(self,context):
    global _CONTACT_PRESET_APPLYING
    preset=getattr(self,'contact_preset','BALANCED')
    if preset=='CUSTOM' or _CONTACT_PRESET_APPLYING: return
    profile=_preset_profile(preset,context)
    if not profile: return
    _CONTACT_PRESET_APPLYING=True
    try:
        for name,value in profile.items(): setattr(self,name,value)
    finally: _CONTACT_PRESET_APPLYING=False
    _physics_setting_update(self,context)

class TU_PresetItem(bpy.types.PropertyGroup):
    uid: StringProperty(name='ID')
    name: StringProperty(name='Name',default='My Preset')
    solver_scale: FloatProperty(name='Physics Scale',default=1.0,min=1.0,max=50)
    contact_hertz: FloatProperty(name='Contact Hertz',default=60,min=1,max=2000)
    contact_damping: FloatProperty(name='Damping',default=8,min=0.1,max=30)
    contact_push_speed: FloatProperty(name='Push-out Speed',default=4,min=0.01,max=50)
    contact_recycle_distance: FloatProperty(name='Recycle Distance',default=0.02,min=0,max=0.25)
    precision_auto_bullet: BoolProperty(name='Thin CCD',default=False)
    precision_disable_recycling: BoolProperty(name='Fresh Contacts',default=False)
    physics_frequency: IntProperty(name='Frequency',default=120,min=10,max=240)
    physics_substeps: IntProperty(name='Multiplier',default=2,min=1,max=64)

# Preferences editor uses a detached draft. Editing does not change active simulation
# or the persisted preset until Save Defaults is explicitly pressed.
_EDITOR_ENUM_CACHE=[]
def _pref_editor_items(self, context):
    global _EDITOR_ENUM_CACHE
    items=[(key,_PRESET_NAMES[key],"Factory profile") for key in ('FAST','BALANCED','STACKING','SMALL')]
    prefs=_tumblify_preferences(context)
    if prefs:
        items += [("USER_"+i.uid,i.name,"Custom saved profile") for i in prefs.custom_presets if i.uid not in _FACTORY_PROFILES]
    _EDITOR_ENUM_CACHE=items or [('BALANCED','Balanced','Factory profile')]
    return _EDITOR_ENUM_CACHE

def _pref_get_item(prefs, key):
    uid=key[5:] if key.startswith('USER_') else key
    return next((i for i in prefs.custom_presets if i.uid==uid),None)

def _pref_load_editor(prefs, context):
    profile=_preset_profile(prefs.editor_preset,context)
    if not profile: return
    prefs.editor_loading=True
    try:
        for key in _PRESET_FIELDS: setattr(prefs.editor_values,key,profile[key])
    finally: prefs.editor_loading=False
    prefs.editor_dirty=False

def _pref_selection_update(self,context):
    _pref_load_editor(self,context)

def _pref_value_update(self,context):
    prefs=_tumblify_preferences(context)
    if prefs and not prefs.editor_loading: prefs.editor_dirty=True

class TU_PresetDraft(bpy.types.PropertyGroup):
    solver_scale: FloatProperty(name='Physics Scale',default=1,min=1,max=50,update=_pref_value_update)
    contact_hertz: FloatProperty(name='Contact Hertz',default=30,min=1,max=2000,update=_pref_value_update)
    contact_damping: FloatProperty(name='Contact Damping',default=10,min=0.1,max=30,update=_pref_value_update)
    contact_push_speed: FloatProperty(name='Push-out Speed',default=3,min=0.01,max=50,update=_pref_value_update)
    contact_recycle_distance: FloatProperty(name='Recycle Distance',default=.05,min=0,max=.25,precision=4,update=_pref_value_update)
    precision_auto_bullet: BoolProperty(name='Thin CCD',default=False,update=_pref_value_update)
    precision_disable_recycling: BoolProperty(name='Fresh Contacts',default=False,update=_pref_value_update)
    physics_frequency: IntProperty(name='Frequency',default=60,min=10,max=240,update=_pref_value_update)
    physics_substeps: IntProperty(name='Multiplier',default=1,min=1,max=64,update=_pref_value_update)

class TU_OT_preset_editor_save(bpy.types.Operator):
    bl_idname='tumblify.preset_editor_save'; bl_label='Save Preset Defaults'
    bl_description='Persist these values as the defaults of the selected preset'
    def execute(self,context):
        prefs=_tumblify_preferences(context)
        if not prefs: return {'CANCELLED'}
        key=prefs.editor_preset
        item=_pref_get_item(prefs,key)
        if item is None:
            if key not in _FACTORY_PROFILES: return {'CANCELLED'}
            item=prefs.custom_presets.add(); item.uid=key; item.name=_PRESET_NAMES[key]
        for prop in _PRESET_FIELDS: setattr(item,prop,getattr(prefs.editor_values,prop))
        prefs.editor_dirty=False
        # Reapply new saved values only to scenes already using this profile.
        for sc in bpy.data.scenes:
            try:
                if sc.tumblify.contact_preset==key: _contact_preset_update(sc.tumblify,context)
            except (AttributeError,ValueError): pass
        self.report({'INFO'},'Preset defaults saved: '+item.name)
        return {'FINISHED'}

class TU_OT_preset_editor_reload(bpy.types.Operator):
    bl_idname='tumblify.preset_editor_reload'; bl_label='Discard Unsaved Changes'
    def execute(self,context):
        prefs=_tumblify_preferences(context)
        if not prefs: return {'CANCELLED'}
        _pref_load_editor(prefs,context)
        return {'FINISHED'}

class TU_OT_preset_edit_factory(bpy.types.Operator):
    bl_idname='tumblify.preset_edit_factory'; bl_label='Edit Factory Presets'; bl_options={'INTERNAL'}
    def execute(self,context):
        prefs=_tumblify_preferences(context)
        if not prefs: return {'CANCELLED'}
        for key,profile in _FACTORY_PROFILES.items():
            if any(item.uid==key for item in prefs.custom_presets): continue
            item=prefs.custom_presets.add(); item.uid=key; item.name=_PRESET_NAMES[key]
            for field,value in profile.items(): setattr(item,field,value)
        self.report({'INFO'},'Factory profiles are editable below; restore defaults to reset')
        return {'FINISHED'}

class TU_OT_preset_save(bpy.types.Operator):
    bl_idname='tumblify.preset_save'; bl_label='Save Contact Preset'; bl_description='Save current simulation tuning as a new user preset'
    name: StringProperty(name='Preset Name',default='My Contact Preset')
    def invoke(self,context,event): return context.window_manager.invoke_props_dialog(self)
    def execute(self,context):
        import uuid
        prefs=_tumblify_preferences(context)
        if not prefs: self.report({'ERROR'},'Tumblify preferences unavailable'); return {'CANCELLED'}
        st=context.scene.tumblify
        item=prefs.custom_presets.add(); item.uid=uuid.uuid4().hex[:12]; item.name=self.name.strip() or 'My Preset'
        for key in _PRESET_FIELDS: setattr(item,key,getattr(st,key))
        st.contact_preset='USER_'+item.uid
        prefs.editor_preset='USER_'+item.uid
        self.report({'INFO'},'Saved preset: '+item.name)
        return {'FINISHED'}

class TU_OT_preset_remove(bpy.types.Operator):
    bl_idname='tumblify.preset_remove'; bl_label='Remove User Preset'
    def execute(self,context):
        prefs=_tumblify_preferences(context)
        if not prefs: return {'CANCELLED'}
        key=prefs.editor_preset
        if not key.startswith('USER_'):
            self.report({'WARNING'},'Factory presets cannot be deleted; use Restore Factory Presets')
            return {'CANCELLED'}
        uid=key[5:]
        idx=next((i for i,v in enumerate(prefs.custom_presets) if v.uid==uid),-1)
        if idx<0: return {'CANCELLED'}
        for sc in bpy.data.scenes:
            try:
                if sc.tumblify.contact_preset==key: sc.tumblify.contact_preset='BALANCED'
            except (AttributeError,ValueError): pass
        prefs.editor_preset='BALANCED'
        prefs.custom_presets.remove(idx)
        _pref_load_editor(prefs,context)
        return {'FINISHED'}

class TU_OT_preset_reset(bpy.types.Operator):
    bl_idname='tumblify.preset_reset'; bl_label='Restore Factory Presets'
    bl_description='Restore the original factory values; preserve user-created presets'
    def execute(self,context):
        prefs=_tumblify_preferences(context)
        if not prefs: return {'CANCELLED'}
        for index in reversed(range(len(prefs.custom_presets))):
            if prefs.custom_presets[index].uid in _FACTORY_PROFILES:
                prefs.custom_presets.remove(index)
        _pref_load_editor(prefs,context)
        for scene in bpy.data.scenes:
            try:
                if scene.tumblify.contact_preset in _FACTORY_PROFILES:
                    _contact_preset_update(scene.tumblify,context)
            except (AttributeError,ValueError): pass
        self.report({'INFO'},'Factory values restored; custom presets preserved')
        return {'FINISHED'}

class TU_OT_preset_preferences(bpy.types.Operator):
    bl_idname='tumblify.preset_preferences'; bl_label='Edit Presets'
    def execute(self,context):
        try:
            bpy.ops.preferences.addon_show(module=__package__)
        except Exception as ex: self.report({'WARNING'},str(ex))
        return {'FINISHED'}

def _object_physics_update(self,context):
    scene=context.scene if context and getattr(context,'scene',None) else None
    if scene is None: return
    obj=getattr(self,'id_data',None);rec=_get_scene_cache(scene)
    if rec is not None and obj is not None and obj.as_pointer() in rec['members']:
        _invalidate_scene_collisions(scene)
    # Fast collision popup performs a targeted one-object preview rebuild.
    # Do not also schedule the global collision-debug rebuild here, otherwise
    # a single menu choice can rebuild every captured Fast source twice.
    if globals().get('_COLLISION_DEBUG_TARGETED_EDIT',False):
        return
    try: _request_collision_debug_refresh(scene)
    except Exception: pass

def _mesh_detail_percent_get(self):
    if 'mesh_detail_percent' in self:return float(self['mesh_detail_percent'])
    if self.is_property_set('mesh_triangle_budget'):
        obj=self.id_data;eo=None
        try:
            eo=obj.evaluated_get(bpy.context.evaluated_depsgraph_get());me=eo.to_mesh();me.calc_loop_triangles()
            return max(0.1,min(100.0,100.0*self.mesh_triangle_budget/max(1,len(me.loop_triangles))))
        finally:
            if eo is not None:eo.to_mesh_clear()
    return 50.0


def _mesh_detail_percent_set(self,value):self['mesh_detail_percent']=float(value)


class TumblifyObjectSettings(bpy.types.PropertyGroup):
    role: EnumProperty(name="Role",items=[('NONE','None',''),('STATIC','Static','Static collision'),('SOURCE','Dynamic Source','Spawn source')],default='NONE')
    shape: EnumProperty(name="Shape",items=SHAPES,default='DETECT',update=_object_physics_update)
    mesh_quality: EnumProperty(name="Mesh Detail",items=[('AUTO','Auto','Preserve evaluated mesh detail; automatic collision-only thickness for open surfaces'),('CUSTOM','Custom','Set collision triangle budget'),('ORIGINAL','Original','Keep evaluated surface without decimation')],default='AUTO',update=_object_physics_update)
    mesh_detail_percent: FloatProperty(name="Mesh Detail",description="Percentage of evaluated triangles retained: 100% disables decimation",min=0.1,max=100.0,subtype='PERCENTAGE',get=_mesh_detail_percent_get,set=_mesh_detail_percent_set,update=_object_physics_update)
    mesh_triangle_budget: IntProperty(name="Triangle Budget",default=20000,min=100,max=2000000,update=_object_physics_update)
    mesh_thickness_mode: EnumProperty(name="Surface Thickness",items=[('AUTO','Auto (Centered)','Open surfaces only: automatic size-based thickness, half on each side; closed meshes unchanged'),('NONE','None','No added thickness; exact triangle surface'),('INWARD','Inward','Open surfaces only: extrude by Thickness against face normals; closed meshes unchanged')],default='AUTO',update=_object_physics_update)
    mesh_thickness: FloatProperty(name="Thickness",description="Open surfaces only. Local mesh units; object scale applies. Extrudes against face normals in Inward mode",default=0.01,min=0.0,subtype='DISTANCE',update=_object_physics_update)
    terrain_resolution: IntProperty(name="Grid Resolution",description="Samples on longest local axis; higher values preserve smaller pits. Upper surface only",default=129,min=3,max=1025,update=_object_physics_update)
    container_form: EnumProperty(
        name='Container Form', default='AUTO', update=_object_physics_update,
        items=[('AUTO', 'Auto', 'Detect a rectangular or round cavity'),
               ('BOX', 'Box', 'Build four walls around the detected rectangular cavity'),
               ('ROUND', 'Round', 'Follow the section of a barrel, pot or other round vessel')])
    container_wall_depth_override: FloatProperty(
        name='Wall Thickness',
        description='0 = Auto. Box walls grow inward within the outer bounds. Round walls grow outward from the cavity',
        default=0.0, min=0.0, soft_max=0.25, precision=4, subtype='DISTANCE',
        update=_object_physics_update)
    container_wall_thickness: FloatProperty(name='Wall Offset', description='Signed inner-wall offset: positive shrinks the cavity; negative expands it. Physics and Fill clearance use the same offset', default=0.002, min=-10.0, max=10.0, soft_min=-0.05, soft_max=0.05, precision=4, subtype='DISTANCE', update=_object_physics_update)
    container_safety_margin: FloatProperty(name='Safety Margin', description='Extra inward clearance that keeps spawned bodies away from the physical wall and reduces tunnelling', default=0.003, min=0.0, max=0.5, soft_max=0.05, precision=4, subtype='DISTANCE', update=_object_physics_update)
    container_proxy_depth: FloatProperty(name='Proxy Wall Depth', description='Collision thickness extending outward from the detected cavity surface into the render mesh/material', default=0.015, min=0.001, max=0.5, soft_max=0.08, precision=4, subtype='DISTANCE', update=_object_physics_update)
    container_max_parts: IntProperty(name='Legacy Max Container Parts', description='Legacy compatibility value; angular container walls are now controlled directly by Radial Segments', default=64, min=8, max=256, soft_max=128, update=_object_physics_update)
    container_radial_segments: IntProperty(name='Radial Segments', description='Number of angular convex-hull wall sectors. Higher values follow the container outline more closely', default=16, min=6, max=48, soft_max=32, update=_object_physics_update)
    container_height_segments: IntProperty(name='Height Samples', description='Number of vertical samples used inside each angular wall hull to follow taper and curved profiles', default=4, min=1, max=12, soft_max=8, update=_object_physics_update)
    container_open_top: BoolProperty(name='Open Top', description='Leave the top of Container / Open Concave collision physically open', default=True, update=_object_physics_update)
    container_opening_clearance: FloatProperty(name='Opening Clearance', description='Lower the upper collision rim by this distance when Open Top is enabled, keeping lip geometry from accidentally bridging the mouth', default=0.01, min=0.0, max=1.0, soft_max=0.1, precision=4, subtype='DISTANCE', update=_object_physics_update)
    container_height_override: FloatProperty(
        name="Container Height",
        description="Manual usable cavity height for Container collision, measured upward from the adjusted inner floor. 0 = Auto detect",
        default=0.0,min=0.0,max=100000.0,soft_max=10.0,precision=4,subtype='DISTANCE',update=_object_physics_update)
    container_top_offset: FloatProperty(
        name="Top Offset", description="Raise or lower the wall rim and optional lid",
        default=0.0,min=-100000.0,max=100000.0,soft_min=-1.0,soft_max=1.0,
        precision=4,subtype='DISTANCE',update=_object_physics_update)
    container_bottom_offset: FloatProperty(
        name="Bottom Offset",
        description="Move the detected Container inner floor up or down. Move only the usable floor between the exterior base and rim; walls and supports stay in place",
        default=0.0,min=-100000.0,max=100000.0,soft_min=-1.0,soft_max=1.0,precision=4,subtype='DISTANCE',update=_object_physics_update)
    material_preset: EnumProperty(name="Physics Material",items=MATERIAL_ITEMS,default='CONCRETE',update=_object_physics_update)
    mass_multiplier: FloatProperty(name="Mass Multiplier",description="Multiplier applied to preset density for this source",default=1.0,min=0.01,max=100.0,soft_max=10.0,update=_object_physics_update)
    spawn_weight: FloatProperty(name="Spawn %",description="Relative spawn probability for this Dynamic Source. Values are normalized across all sources, so 70/30 gives a 70%/30% distribution",default=100.0,min=0.0,max=100.0,subtype='PERCENTAGE')

class TumblifyObjectRef(bpy.types.PropertyGroup):
    obj: PointerProperty(name="Object", type=bpy.types.Object)
    selected: BoolProperty(name="Selected", default=False)


def _fast_colliders_update(self,context):
    _clear_scene_collision_wire()
    if context and context.scene: _request_collision_debug_refresh(context.scene)


def _fast_sleep_update(self,context):
    global _RUNTIME
    rt=_RUNTIME
    if rt is None or not bool(getattr(rt,'fast_runtime',False)):
        return
    try:
        # Fast uses Box3D's native island-based sleep. Never use the add-on's
        # forced sleep here: b3Body_SetAwake(false) sleeps an entire touching island.
        rt.world.set_forced_sleep(False,0.0,0.0,0.0)
        rt.world.set_native_sleep(bool(self.fast_sleep),0.05)
    except Exception:
        pass



def _advanced_run_tab_update(self, context):
    """Keep the compact Advanced run tabs mapped to the existing runtime backend."""
    tab=str(getattr(self, 'ui_advanced_run_tab', 'REALTIME'))
    try:
        if tab=='REALTIME': self.spawn_backend='FAST'
        elif tab in {'OFFLINE','BAKE'}: self.spawn_backend='OFFLINE'
    except Exception:
        pass

def _advanced_detail_page_update(self, context):
    # Older files may still name the removed Collision detail page.
    if self.ui_advanced_detail=='COLLISION':
        self.ui_advanced_detail='SOURCES'
    if bool(getattr(self,'ui_collision_preview_owned',False)):
        _clear_debug_collection(); self.show_collision_debug=False
        self.ui_collision_preview_owned=False
    _tag_view3d_redraw_all()

def _fast_rotation_axes_update(self, context):
    rt=_RUNTIME
    if rt is not None and getattr(rt,'fast_runtime',False) and not getattr(rt,'live_interactive',False) and not getattr(rt,'fast_native_rotate',False):
        rt._fast_preview_create(context,getattr(rt,'fast_preview_source',None),getattr(rt,'fast_preview_scale',1.0),False)
    _tag_view3d_redraw_all()

def _fast_layout_update(self, context):
    rt=_RUNTIME
    if rt is not None and getattr(rt,'fast_runtime',False) and not getattr(rt,'live_interactive',False) and not getattr(rt,'fast_native_rotate',False):
        rt._fast_preview_create(context,getattr(rt,'fast_preview_source',None),getattr(rt,'fast_preview_scale',1.0),True)
    _tag_view3d_redraw_all()


def _emitter_radius_update(self, context):
    brush=bpy.data.objects.get(BRUSH_NAME)
    if brush is not None and brush.type=='EMPTY':
        brush.empty_display_size=max(0.05,float(self.emitter_radius))
    _tag_view3d_redraw_all()

class TumblifySceneSettings(bpy.types.PropertyGroup):
    mode: EnumProperty(name='Mode', items=[('FAST', 'Fast', 'Minimal cursor-to-drop workflow with a visual held preview and physics on drop'), ('DROP', 'Advanced', 'Advanced emitter-driven Tumblify drop simulation')], default='FAST')
    ui_drop_spawn: BoolProperty(default=True, options={'HIDDEN'})
    ui_drop_dynamic: BoolProperty(default=True, options={'HIDDEN'})
    ui_drop_collision: BoolProperty(default=True, options={'HIDDEN'})
    ui_drop_static: BoolProperty(default=True, options={'HIDDEN'})
    ui_drop_emitter: BoolProperty(default=True, options={'HIDDEN'})
    ui_drop_physics: BoolProperty(default=True, options={'HIDDEN'})
    ui_drop_display: BoolProperty(default=True, options={'HIDDEN'})
    ui_drop_run: BoolProperty(default=True, options={'HIDDEN'})
    ui_drop_result: BoolProperty(default=True, options={'HIDDEN'})
    ui_fast_simulation: BoolProperty(name='Simulation', default=False, options={'HIDDEN'})
    ui_fast_results: BoolProperty(name='Show Fast Result Tools', default=False, options={'HIDDEN'})
    ui_advanced_run_tab: EnumProperty(name='', description='', items=[('REALTIME', 'Real Time', 'Simulate interactively in the viewport'), ('OFFLINE', 'Offline', 'Generate a settled placement result'), ('BAKE', 'Bake', 'Bake the simulation to animation')], default='REALTIME', update=_advanced_run_tab_update)
    ui_advanced_detail: EnumProperty(name='', description='', items=[('EMITTER', 'Emitter', ''), ('SOURCES', 'Dynamic', ''), ('COLLISION', 'Dynamic', 'Compatibility value; opens Dynamic settings'), ('STATIC', 'Static', ''), ('RANDOM', 'Randomization', ''), ('PHYSICS', 'Physics', '')], default='EMITTER', update=_advanced_detail_page_update)
    ui_collision_preview_owned: BoolProperty(default=False, options={'HIDDEN'})
    collision_preview_scope: EnumProperty(items=[('SELECTED', 'Selected', ''), ('DYNAMIC', 'Dynamic', ''), ('STATIC', 'Static', '')], default='SELECTED', options={'HIDDEN'})
    ui_interactive_tools: BoolProperty(default=True, options={'HIDDEN'})
    ui_interactive_collision: BoolProperty(default=False, options={'HIDDEN'})
    ui_interactive_physics: BoolProperty(default=False, options={'HIDDEN'})
    ui_interactive_run: BoolProperty(default=True, options={'HIDDEN'})
    spawn_location: EnumProperty(name='Spawn Location', description='Choose whether instances are generated inside the emitter volume or on its mesh surface', items=[('VOLUME', 'Inside', 'Generate instances inside the emitter volume'), ('SURFACE', 'Surface', 'Generate instances on the emitter surface; supports a vertex-group mask and optional magnetization')], default='VOLUME', update=_spawn_location_update)
    fast_sources: CollectionProperty(type=TumblifyObjectRef)
    advanced_emitter_ready: BoolProperty(default=False, options={'HIDDEN'})
    ui_bake_cache: BoolProperty(name='Bake Cache', default=False)
    fast_cursor_height: FloatProperty(name='Cursor Height', description='Height of the Fast placement cursor above world Z=0', default=2.0, min=0.05, max=100000.0, soft_max=100.0, unit='LENGTH')
    fast_spread: FloatProperty(name='Spread Radius', description='Random spawn radius in the manually oriented Fast cursor plane; S then move the mouse adjusts during simulation', default=0.0, min=0.0, soft_max=10.0, subtype='DISTANCE', update=_fast_layout_update)
    fast_random_rotation: BoolProperty(name='Random Rotation', description='Randomize rotation for each Fast preview/drop; toggle with Shift+R', default=False)
    fast_emitter_rotation: FloatVectorProperty(name='Fast Emitter Rotation', description='Base start rotation applied by the Fast cursor emitter', size=3, subtype='EULER', unit='ROTATION', default=(0.0, 0.0, 0.0), options={'HIDDEN'})
    fast_random_rot_x: BoolProperty(name='Fast Random X', description='Allow Fast random rotation around local X; choose axes from Q popup during Fast', default=True, options={'HIDDEN'}, update=_fast_rotation_axes_update)
    fast_random_rot_y: BoolProperty(name='Fast Random Y', description='Allow Fast random rotation around local Y; choose axes from Q popup during Fast', default=True, options={'HIDDEN'}, update=_fast_rotation_axes_update)
    fast_random_rot_z: BoolProperty(name='Fast Random Z', description='Allow Fast random rotation around local Z; choose axes from Q popup during Fast', default=True, options={'HIDDEN'}, update=_fast_rotation_axes_update)
    fast_objects_per_tick: IntProperty(name='Spread Count', description='Number of column positions per Fast drop; use + / - during Fast', default=1, min=1, max=10, update=_fast_layout_update)
    fast_stack_count: IntProperty(name='Stack Height', description='Objects per column, including its base; Shift + / - during Fast. Total per drop is Spread Count times Stack Height', default=1, min=1, max=10, update=_fast_layout_update)
    fast_scale_min: FloatProperty(name='Scale Min', description='Minimum randomized Fast object size', default=1.0, min=0.05, max=10.0, soft_max=3.0)
    fast_scale_max: FloatProperty(name='Scale Max', description='Maximum randomized Fast object size', default=1.0, min=0.05, max=10.0, soft_max=3.0)
    fast_static_scope: EnumProperty(name='Colliders', description='Choose which scene meshes become static Fast colliders', items=[('SCENE', 'Scene', 'All eligible visible scene meshes'), ('SELECTED', 'Selected', 'Only captured selected mesh colliders')], default='SCENE',update=_fast_colliders_update)
    fast_selected_statics: CollectionProperty(type=TumblifyObjectRef)
    fast_floor: BoolProperty(name='Floor', description='Enable Fast horizontal ground collider', default=True)
    fast_small_objects: BoolProperty(name='Small Objects', description='Use the Advanced Small Objects contact tuning for Fast: larger internal physics scale, stronger contact recovery and thin-body CCD; no additional controls', default=False)
    fast_sleep: BoolProperty(name='Sleep', description='Allow Fast Mode dynamic bodies to settle and sleep', default=True, update=_fast_sleep_update)
    fast_precision: EnumProperty(name='Physics Precision', items=[('LOW', 'Low', '30 Hz / 1 substep'), ('MEDIUM', 'Medium', '60 Hz / 2 substeps'), ('HIGH', 'High', '120 Hz / 4 substeps')], default='MEDIUM')
    fast_collision_menu_source: StringProperty(default='', options={'HIDDEN'})
    fast_cursor_x: FloatProperty(default=0.0, options={'HIDDEN'})
    fast_cursor_y: FloatProperty(default=0.0, options={'HIDDEN'})
    fast_cursor_ready: BoolProperty(default=False, options={'HIDDEN'})
    fast_shape_summary: StringProperty(default='', options={'HIDDEN'})
    enable_floor: BoolProperty(name='Enable Floor', description='Enable the built-in horizontal floor collider in Advanced simulation', default=True)
    floor_z: FloatProperty(name='Floor Z', default=0.0, precision=3)
    gravity_strength: FloatProperty(name='Gravity Strength', description='World gravity magnitude in m/s² (Earth gravity: 9.81). Lower values reduce impact velocity but do not improve contact precision by themselves', default=9.81, min=0.0, max=1000.0, soft_max=50.0, precision=2)
    assign_shape: EnumProperty(name='Collision Shape', items=SHAPES, default='DETECT')
    emitter_interaction: EnumProperty(name='Emitter Interaction (Legacy)', description='Legacy compatibility value; Advanced emitters no longer push or pull bodies', items=[('PUSH', 'Push', 'Legacy'), ('PULL', 'Pull', 'Legacy'), ('OFF', 'Off', 'Disabled')], default='OFF', options={'HIDDEN'})
    interactive_force_strength: FloatProperty(name='Interactive Force (Legacy)', description='Legacy compatibility value; Advanced Push/Pull was removed in v0.14.50', default=1.0, min=0.1, max=8.0, soft_min=0.25, soft_max=4.0, precision=2, options={'HIDDEN'})
    spawn_backend: EnumProperty(name='Spawn Mode', description='Choose Realtime viewport simulation or Offline final-result generation', items=[('FAST', 'Realtime', 'Interactive viewport simulation with batched display and live controls'), ('OFFLINE', 'Offline', 'Run the full Drop calculation without per-step viewport visualization, then reveal the finished result')], default='FAST')
    instance_display: EnumProperty(name='Display', description='Viewport display for all Tumblify piles: Mesh, Point or opaque Box. Rendering and physics are unchanged', items=[('BOX', 'Box', 'Display instances as fitted boxes only; physics collision is unchanged'), ('POINT', 'Point', 'Display instances as true point-cloud points sized to the object only; physics collision is unchanged'), ('MESH', 'Mesh', 'Display the source mesh; physics collision is unchanged')], default='MESH', update=_display_settings_update)
    result_output: EnumProperty(name='Result', items=[('INSTANCES', 'Combined', 'Keep one lightweight combined instancer per source and realize later'), ('OBJECTS', 'Separated', 'Create individual separated Blender objects at simulation finish')], default='INSTANCES')
    display_viewport: BoolProperty(name='Viewport', description='Use the selected Display representation in the viewport only; this does not change physics', default=True, update=_display_settings_update)
    display_render: BoolProperty(name='Render', description='Use the selected Display representation for rendering only; this does not change physics', default=False, update=_display_settings_update)
    display_hide_viewport: BoolProperty(name='Hide Viewport', description='Hide spawned physics instances in the viewport', default=False, update=_display_settings_update)
    display_hide_render: BoolProperty(name='Hide Render', description='Hide spawned physics instances from rendering', default=False, update=_display_settings_update)
    spawn_rate: FloatProperty(name='Spawn Rate', description='Objects emitted per second while Spawn toggle (E) is active', default=20.0, min=1.0, max=2000.0, soft_max=500.0)
    emitter_radius: FloatProperty(name='Emitter Radius', description='Radius of the emitter Empty and optional interaction field', default=1.25, min=0.05, max=50.0, soft_max=10.0, update=_emitter_radius_update)
    emitter_mode: EnumProperty(name='Emitter Mode', description='Internal emitter type; UI uses a direct object picker', items=[('FIXED', 'Fixed Empty', 'Use the Tumblify emitter Empty'), ('CUSTOM', 'Custom Object', 'Use a scene object as the emitter volume'), ('COLLECTION', 'Collection', 'Use all objects from a collection as emitter volumes')], default='FIXED')
    emitter_object: PointerProperty(name='Emitter Object', description='Scene object used as the custom emitter volume', type=bpy.types.Object, update=_emitter_object_update)
    emitter_wire_display: BoolProperty(name='Wire Display', description='Show the configured emitter as viewport wireframe. Display-only; does not affect physics', default=False, update=_emitter_wire_display_update)
    emitter_collection: PointerProperty(name='Emitter Collection', description='Use all objects in this collection as emitter volumes', type=bpy.types.Collection, update=_emitter_collection_update)
    fill_emitter: BoolProperty(name='Fill Emitter', description='Turn the Inside emitter into a temporary container: spawn starts near the bottom and rises as the emitter fills', default=False, update=_spawn_location_update)
    fill_interactive_proxy_mode: EnumProperty(name='Interactive Container Proxy', description='Choose how the Fill Emitter collision is represented while Live Interactive is active', items=[('AUTO', 'Auto Shell', 'Use the existing hollow shell logic: exact/low-poly surface when possible, stable bounds fallback otherwise'), ('ASSIGNED', 'Emitter Collision', 'Use the collision model assigned to the emitter when it is suitable. Loose Parts / Manual / Compound are used directly as one kinematic body; primitive assignments build a matching hollow shell')], default='AUTO')
    fill_interactive_margin: FloatProperty(name='Interactive Wall Margin', description='Extra wall thickness used by the kinematic Fill Emitter proxy in Live Interactive. Larger values resist tunneling better but slightly reduce usable interior space', default=0.01, min=0.001, max=0.25, soft_min=0.003, soft_max=0.05, precision=3, unit='LENGTH')
    random_scale_min: FloatProperty(name='Scale Min', description='Minimum uniform scale for spawned instances', default=1.0, min=0.05, max=10.0, soft_max=3.0)
    random_scale_max: FloatProperty(name='Scale Max', description='Maximum uniform scale for spawned instances', default=1.0, min=0.05, max=10.0, soft_max=3.0)
    advanced_random_rotation: BoolProperty(name='Random Rotation', description='Randomize new Advanced objects; toggle with Shift+R during simulation', default=True)
    random_rot_x: BoolProperty(name='X', description='Randomize rotation around X at spawn', default=True)
    random_rot_y: BoolProperty(name='Y', description='Randomize rotation around Y at spawn', default=True)
    random_rot_z: BoolProperty(name='Z', description='Randomize rotation around Z at spawn', default=True)
    static_friction: FloatProperty(name='Static Friction', description='Friction used by all assigned static colliders and the floor', default=0.7, min=0.0, max=2.0, update=_physics_setting_update)
    convex_simplify: FloatProperty(name='Convex Simplify', description='Reduce convex hull complexity before sending it to Box3D. 0 keeps more detail, 1 is very coarse', default=0.65, min=0.0, max=1.0, subtype='FACTOR', update=_physics_setting_update)
    compound_max_hulls: IntProperty(name='Max Compound Parts', description='Maximum convex pieces used by Compound Hull. More parts follow concave shapes more closely but increase contact cost', default=8, min=2, max=32, soft_max=16, update=_physics_setting_update)
    auto_fit_tolerance: FloatProperty(name='Fit Tolerance', description='Maximum normalized empty-space / concavity error before Auto Proxy splits a region or falls back to a local Convex Hull. Lower values follow the mesh more closely', default=0.035, min=0.005, max=0.15, soft_min=0.01, soft_max=0.08, precision=3, update=_physics_setting_update)
    loose_max_parts: IntProperty(name='Max Loose Parts', description='Maximum collision parts used by Loose Parts Compound, including automatic patches generated for a connected mesh', default=32, min=1, max=128, soft_max=64, update=_physics_setting_update)
    loose_auto_split_connected: BoolProperty(name='Auto Split Connected', description='If Loose Parts is assigned to a single connected mesh, automatically split its surface into connected normal-angle patches and build one convex collision part per patch. Useful for torus/ring and other connected concave shapes', default=True, update=_physics_setting_update)
    loose_split_angle: FloatProperty(name='Split Angle', description='Maximum accumulated surface-normal change inside one automatically generated Loose Parts patch. Smaller angles create more parts and preserve curved holes better', default=math.radians(28.0), min=math.radians(3.0), max=math.radians(90.0), soft_min=math.radians(8.0), soft_max=math.radians(60.0), subtype='ANGLE', update=_physics_setting_update)
    # Legacy scene values: retained only for migration of older blend files.
    container_wall_thickness: FloatProperty(name='Wall Offset', description='Signed inner-wall offset: positive shrinks the cavity; negative expands it. Physics and Fill clearance use the same offset', default=0.002, min=-10.0, max=10.0, soft_min=-0.05, soft_max=0.05, precision=4, subtype='DISTANCE', update=_physics_setting_update)
    container_safety_margin: FloatProperty(name='Safety Margin', description='Extra inward clearance that keeps spawned bodies away from the physical wall and reduces tunnelling', default=0.003, min=0.0, max=0.5, soft_max=0.05, precision=4, subtype='DISTANCE', update=_physics_setting_update)
    container_proxy_depth: FloatProperty(name='Proxy Wall Depth', description='Collision thickness extending outward from the detected cavity surface into the render mesh/material', default=0.015, min=0.001, max=0.5, soft_max=0.08, precision=4, subtype='DISTANCE', update=_physics_setting_update)
    container_max_parts: IntProperty(name='Legacy Max Container Parts', description='Legacy compatibility value; angular container walls are now controlled directly by Radial Segments', default=64, min=8, max=256, soft_max=128, update=_physics_setting_update)
    container_radial_segments: IntProperty(name='Radial Segments', description='Number of angular convex-hull wall sectors. Higher values follow the container outline more closely', default=16, min=6, max=48, soft_max=32, update=_physics_setting_update)
    container_height_segments: IntProperty(name='Height Samples', description='Number of vertical samples used inside each angular wall hull to follow taper and curved profiles', default=4, min=1, max=12, soft_max=8, update=_physics_setting_update)
    container_open_top: BoolProperty(name='Open Top', description='Leave the top of Container / Open Concave collision physically open', default=True, update=_physics_setting_update)
    container_opening_clearance: FloatProperty(name='Opening Clearance', description='Lower the upper collision rim by this distance when Open Top is enabled, keeping lip geometry from accidentally bridging the mouth', default=0.01, min=0.0, max=1.0, soft_max=0.1, precision=4, subtype='DISTANCE', update=_physics_setting_update)
    static_objects: CollectionProperty(type=TumblifyObjectRef)
    dynamic_sources: CollectionProperty(type=TumblifyObjectRef)
    static_index: IntProperty(default=0)
    dynamic_index: IntProperty(default=0)
    static_anchor: IntProperty(default=-1)
    dynamic_anchor: IntProperty(default=-1)
    physics_frequency: IntProperty(name='Physics Frequency', description='Outer Box3D fixed-step frequency in Hz. 30 Hz is the speed-oriented default; 60 Hz improves fast-contact timing. Any integer frequency is valid', default=30, min=10, max=240, soft_min=20, soft_max=120, update=_contact_setting_update)
    physics_substeps: IntProperty(name='Frequency Multiplier', description='Box3D solver sub-step count inside each outer physics step. Approximate solver sub-step rate is Physics Frequency × Multiplier, but this is not identical to increasing the base Frequency', default=1, min=1, max=64, soft_max=16, update=_contact_setting_update)
    physics_threads: IntProperty(name='Threads', description='Box3D worker threads. 0 = Auto. For hybrid CPUs, a value near the number of performance cores can be faster than using every logical core', default=0, min=0, max=32, soft_max=16, update=_physics_setting_update)
    contact_preset: EnumProperty(name='Simulation Preset', description='Saved contact and time-step profile; manual edits switch to Custom', items=_preset_items, default=0, update=_contact_preset_update)
    solver_scale: FloatProperty(name='Physics Scale', description='Internal length scale used only by Box3D. 10x makes a 2 mm object behave numerically like a 20 mm object while Blender remains at the original size', default=1.0, min=1.0, max=50.0, soft_max=20.0, precision=2, update=_contact_setting_update)
    contact_hertz: FloatProperty(name='Contact Hertz', description='Contact stiffness. Higher values recover overlap faster but can add jitter', default=30.0, min=1.0, max=2000.0, soft_min=10.0, soft_max=500.0, update=_contact_setting_update)
    contact_damping: FloatProperty(name='Contact Damping', description='Box3D contact damping ratio. Native default is 10', default=10.0, min=0.1, max=30.0, soft_max=15.0, precision=2, update=_contact_setting_update)
    contact_push_speed: FloatProperty(name='Push-out Speed', description='Maximum overlap recovery speed in Blender meters per second before internal Physics Scale is applied', default=3.0, min=0.01, max=50.0, soft_max=15.0, unit='VELOCITY', update=_contact_setting_update)
    contact_recycle_distance: FloatProperty(name='Recycle Distance', description='Distance for contact manifold recycling inside Box3D solver units. Smaller values reduce reuse of stale contacts at additional CPU cost', default=0.05, min=0.0, max=0.25, soft_max=0.1, precision=4, update=_contact_setting_update)
    precision_auto_bullet: BoolProperty(name='CCD for Thin Bodies', description='Mark thin dynamic bodies as Box3D bullets so they use continuous collision against non-bullet dynamic bodies', default=False, update=_contact_setting_update)
    precision_bullet_thickness: FloatProperty(name='CCD Thickness', description='Bodies whose smallest collision dimension is at or below this value are marked as bullets. Bullet CCD does not run against other bullets', default=0.03, min=0.001, max=1.0, soft_min=0.005, soft_max=0.1, unit='LENGTH', update=_physics_setting_update)
    precision_disable_recycling: BoolProperty(name='Fresh Contacts for Thin Bodies', description='Disable Box3D contact recycling on thin dynamic bodies to avoid reusing stale manifolds when contact features change quickly', default=False, update=_contact_setting_update)
    self_collision: BoolProperty(name='Self Collision', description='Allow spawned physics objects to collide with each other. When disabled they still collide with static colliders, the floor, and emitter interaction proxies', default=True, update=_physics_setting_update)
    forced_sleep: BoolProperty(name='Forced Sleep', description='Advanced only: force very slow bodies to sleep after a stable delay. Useful for residual jitter, but may sleep a whole touching island', default=False, update=_physics_setting_update)
    sleep_linear_threshold: FloatProperty(name='Linear Threshold', description='Linear speed below which Forced Sleep may engage', default=0.05, min=0.0, max=5.0, soft_max=0.5, unit='VELOCITY', update=_physics_setting_update)
    sleep_angular_threshold: FloatProperty(name='Angular Threshold', description='Angular speed below which Forced Sleep may engage', default=0.15, min=0.0, max=10.0, soft_max=1.0, unit='ROTATION', update=_physics_setting_update)
    sleep_delay: FloatProperty(name='Sleep Delay', description='How long both speed thresholds must remain satisfied before forcing sleep', default=0.5, min=0.0, max=10.0, soft_max=2.0, unit='TIME', update=_physics_setting_update)
    convex_settle_assist: BoolProperty(name='Convex Settle Assist', description='Physics Drop only: use a slightly stronger angular sleep threshold when Convex/Compound sources are present, reducing long edge-to-edge rocking without changing collision geometry', default=True, update=_physics_setting_update)
    post_proxy_shape: EnumProperty(name='Interactive Proxy', description='Collision proxy used only while settling selected frozen objects in Interactive mode', items=[('ORIGINAL','Original','Use each source object collision shape','OBJECT_DATA',0)]+[(item[0],item[1],item[2],item[3],{'BOX':1,'SPHERE':2,'CAPSULE':3,'CYLINDER':4,'HULL':5,'COMPOUND':6,'LOOSE':8}[item[0]]) for item in SHAPES if item[0] in {'BOX','SPHERE','CAPSULE','CYLINDER','HULL','COMPOUND','LOOSE'}], default='ORIGINAL')
    post_substeps: IntProperty(name='Interactive Multiplier', description='Solver substeps used by Interactive mode for a more accurate settle', default=4, min=1, max=12)
    show_collision_debug: BoolProperty(name='Show Collision', default=False)
    scatter_emitter: PointerProperty(name='Legacy Surface Emitter', description='Legacy v0.10.x surface emitter reference', type=bpy.types.Object)
    scatter_vertex_group: StringProperty(name='Mask', description='Vertex group used as the emission mask. Empty uses the whole surface', default='')
    scatter_magnetized: BoolProperty(name='Magnetized', description='Attract emitted bodies back to their sampled surface anchors while keeping them dynamic and collidable', default=True)
    scatter_magnet_strength: FloatProperty(name='Magnet Strength', description='Spring strength that attracts Magnetized bodies to the emitter surface', default=55.0, min=1.0, max=300.0, soft_max=120.0)
    scatter_magnet_damping: FloatProperty(name='Magnet Damping', description='Velocity damping for Magnetized surface attraction', default=10.0, min=0.0, max=50.0, soft_max=20.0)
    scatter_stop_delay: FloatProperty(name='Stop Delay', description='After Esc, stop spawning and keep simulating for this many seconds so surface-emitted objects can settle before freezing', default=3.0, min=0.0, max=30.0, soft_max=10.0, unit='TIME')
    drop_offline_count: IntProperty(name='Object Count', description='Exact number of bodies generated by Offline Drop. The simulation runs without viewport updates and materializes only the final result', default=500, min=1, max=1000000, soft_max=10000)
    drop_offline_settle: FloatProperty(name='Settle Time', description='Extra physics time after the last offline-spawned object appears before the final result is materialized', default=3.0, min=0.0, max=60.0, soft_max=10.0, unit='TIME')
    emitter_bake_frame_start: IntProperty(name='Start Frame', description='First Blender frame of the Emitter Bake animation', default=1, min=-1048574, max=1048574)
    emitter_bake_frames: IntProperty(name='Frames', description='Exact number of output frames for Emitter Bake. It must be long enough to emit the requested object count at the current Spawn Rate', default=250, min=2, max=1000000, soft_max=2000)
    emitter_bake_cache_mode: EnumProperty(name='Cache', items=[('AUTO', 'Auto', 'Choose disk when estimated transform data exceeds the memory limit'), ('MEMORY', 'Memory', 'Keep the frame cache in RAM'), ('DISK', 'Disk', 'Stream the frame cache to disk in bounded blocks')], default='AUTO')
    emitter_bake_cache_memory_mb: IntProperty(name='Memory Limit (MiB)', description='Auto switches to disk above this estimate; excludes physics, Blender objects and keyframes', default=256, min=16, max=65536)
    emitter_bake_cache_directory: StringProperty(name='Cache Folder', description='Unique run folders are created here; unsaved scenes use the system temporary folder for the default relative path', default='//Tumblify_Cache/', subtype='DIR_PATH', options={'PATH_SUPPORTS_BLEND_RELATIVE'})
    emitter_bake_cache_keep: BoolProperty(name='Keep Completed Cache', description='Retain completed disk data and its manifest after writing keys, or if keyframe conversion is cancelled. Incomplete simulations are removed', default=True)


def _resolve_collision_fit(obj,depsgraph,shape_override=None):
    """Resolve an assignment once for physics, Fast fits and collision previews.

    A fit dictionary is already resolved runtime data: copy it and keep its axis.
    Never replace an object's saved assignment or its diagnostic Auto cache here.
    """
    resolved=isinstance(shape_override,dict)
    fit=dict(shape_override) if resolved else {'shape':shape_override or obj.tumblify.shape}
    shape=migrate_collision_shape(fit.get('shape','HULL'))
    if shape=='DETECT':
        fit=dict(_auto_collision_result(obj,depsgraph))
        shape=migrate_collision_shape(fit.get('shape','HULL'))
    elif not resolved and shape in {'CYLINDER','CAPSULE'}:
        # Preserve Fast's fitted-axis behavior in every launch/preview path.
        try:
            auto=_auto_collision_result(obj,depsgraph)
            if auto.get('axis') is not None: fit['axis']=int(auto['axis'])
        except Exception:
            pass
    fit['shape']=shape
    if shape in {'CYLINDER','CAPSULE'} and fit.get('axis') is None:
        _center,half=_local_bounds(obj,depsgraph)
        fit['axis']=_cylinder_axis(half) if shape=='CYLINDER' else _long_axis(half)
    return fit


def _prepare_collision(obj,depsgraph,simplify=0.65,shape_override=None):
    """Precompute immutable collision geometry once per source object.

    shape_override is used by Post Simulation so a temporary settling proxy can be selected
    without changing the object's assigned collision model in the scene.
    """
    center,half=_local_bounds(obj,depsgraph)
    auto_fit=_resolve_collision_fit(obj,depsgraph,shape_override)
    sh=auto_fit['shape']
    data={'shape':sh,'center':tuple(center),'half':tuple(half),'axis':_long_axis(half)}
    if sh in {'CYLINDER','CAPSULE'}:
        data['axis']=int(auto_fit.get('axis')) if auto_fit and auto_fit.get('axis') is not None else (_cylinder_axis(half) if sh=='CYLINDER' else _long_axis(half))
    if sh=='HULL':
        pts,faces=_evaluated_convex_geometry(obj,depsgraph,simplify)
        data['points']=pts; data['faces']=faces; data['max_vertices']=min(48,max(8,len(pts)))
    elif sh=='COMPOUND':
        try:
            max_hulls=int(bpy.context.scene.tumblify.compound_max_hulls)
        except Exception:
            max_hulls=8
        parts=_evaluated_compound_geometry(obj,depsgraph,simplify,max_hulls)
        if not parts:
            pts,faces=_evaluated_convex_geometry(obj,depsgraph,simplify)
            parts=[{'points':pts,'faces':faces,'max_vertices':min(32,max(8,len(pts)))}] if faces else []
        data['parts']=parts
    elif sh=='LOOSE':
        try:
            st=bpy.context.scene.tumblify; max_parts=int(st.loose_max_parts); tol=float(st.auto_fit_tolerance)
        except Exception:
            max_parts,tol=32,0.025
        parts=_evaluated_loose_parts_geometry(obj,depsgraph,simplify,max_parts,tol)
        if not parts:
            pts,faces=_evaluated_convex_geometry(obj,depsgraph,simplify); parts=[{'points':pts,'faces':faces,'max_vertices':min(32,max(8,len(pts))),'fit':'Hull'}] if faces else []
        data['parts']=parts
        # Loose islands and generated patches retain their own convex hull geometry.
        mixed=[]
        for part in parts:
            n=part.get('native')
            if isinstance(n,dict) and str(n.get('kind','')).upper() in {'BOX','SPHERE','CAPSULE','CYLINDER'}:
                mixed.append(dict(n))
            elif len(part.get('points',[]))>=4:
                mixed.append({'kind':'HULL','pos':(0,0,0),'rot':(0,0,0,1),'params':(0,0,0),
                              'points':part.get('points',[]),'max_vertices':part.get('max_vertices',32)})
        data['mixed_parts']=mixed
    elif sh=='CONTAINER':
        profile=_container_collision_profile(obj,depsgraph)
        parts=_container_surface_parts(obj,depsgraph,profile=profile) if profile else []
        if not parts:
            raise RuntimeError(f"'{obj.name}': Container / Open Concave could not detect a usable inner cavity. Use a hollow vessel with the opening facing local +Z.")
        data['container_profile']={key:value for key,value in profile.items() if key!='geo'}
        data['parts']=parts
        data['mixed_parts']=[
            part.get('native') or {'kind':'HULL','pos':(0,0,0),'rot':(0,0,0,1),'params':(0,0,0),
                                   'points':part['points'],'max_vertices':part.get('max_vertices',32)}
            for part in parts
        ]
    elif sh=='MANUAL':
        parts=_manual_compound_parts(obj)
        mixed=_manual_mixed_parts(obj)
        if not parts:
            parts=_evaluated_loose_parts_geometry(obj,depsgraph,simplify,32,getattr(bpy.context.scene.tumblify,'auto_fit_tolerance',0.025))
        data['parts']=parts
        data['mixed_parts']=mixed if mixed else _native_parts_from_data({'shape':'COMPOUND','parts':parts})
    elif sh=='TERRAIN':
        from .collision import _evaluated_terrain_data
        data.update(_evaluated_terrain_data(obj,depsgraph))
    elif sh=='MESH':
        verts,indices,source_tris,collision_tris=_evaluated_collision_mesh_data(obj,depsgraph)
        data['verts']=verts; data['indices']=indices
        data['source_triangles']=int(source_tris); data['collision_triangles']=int(collision_tris)
        data['mesh_decimated']=bool(collision_tris < source_tris)
    return data



def _physics_material(obj):
    key=getattr(obj.tumblify,'material_preset','CONCRETE')
    m=MATERIAL_PRESETS.get(key,MATERIAL_PRESETS['CONCRETE'])
    mult=max(0.01,float(getattr(obj.tumblify,'mass_multiplier',1.0)))
    return float(m['friction']),float(m['restitution']),float(m['density'])*mult

def _cache_native_collision_assets(world,data):
    """Build reusable native hull assets once per prepared source collision."""
    if not data: return data
    sh=data.get('shape')
    if sh=='HULL' and 'hull_asset' not in data:
        data['hull_asset']=world.create_hull_asset(data.get('points',[]),data.get('max_vertices',48))
    elif sh in {'MANUAL','LOOSE','CONTAINER'} and 'native_mixed_parts' not in data:
        if not getattr(world.api,'has_mixed_compound',False):
            raise RuntimeError('Mixed compound collision requires the v0.13.6+ Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
        native=[]
        for part in data.get('mixed_parts',[]):
            q=dict(part); kind=str(q.get('kind','HULL')).upper()
            if kind=='HULL':
                pts=q.get('points',[])
                if len(pts)<4: continue
                q['asset']=world.create_hull_asset(pts,q.get('max_vertices',min(32,len(pts))))
            else:
                q['asset']=-1
            native.append(q)
        if not native: raise RuntimeError(f"{sh.title()} compound could not create any valid native parts")
        data['native_mixed_parts']=native
    elif sh=='COMPOUND' and 'compound_assets' not in data:
        if not getattr(world.api,'has_compound',False):
            raise RuntimeError('Compound Hull requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
        assets=[]
        for part in data.get('parts',[]):
            pts=part.get('points',[])
            if len(pts)>=4:
                assets.append(world.create_hull_asset(pts,part.get('max_vertices',min(32,len(pts)))))
        if not assets: raise RuntimeError('Compound Hull could not create any valid convex parts')
        data['compound_assets']=assets
    return data

def _builtin_floor_config(st, *, fast=False):
    return (bool(st.fast_floor),0.0) if fast else (bool(st.enable_floor),float(st.floor_z))



def _add_builtin_floor(world, st, *, fast=False):
    enabled, z = _builtin_floor_config(st, fast=fast)
    if enabled:
        world.add_floor(z, float(st.static_friction))
    return enabled


def _configure_world(world,st,fast=False):
    world.set_solver_tuning(float(st.solver_scale),float(st.contact_hertz),float(st.contact_damping),float(st.contact_push_speed),float(st.contact_recycle_distance))
    world.set_self_collision(bool(st.self_collision))
    try:
        if getattr(world.api,'has_gravity_control',False):
            world.set_gravity((0.0,0.0,-float(getattr(st,'gravity_strength',9.81))))
    except Exception:
        pass
    # Release behavior: use Box3D's native island sleep everywhere. The old custom
    # Forced Sleep path could call b3Body_SetAwake(false) on one apparently-stable
    # body and therefore put its entire touching island to sleep prematurely.
    world.set_forced_sleep(False,0.0,0.0,0.0)
    world.set_native_sleep(bool(getattr(st,'fast_sleep',True)) if fast else True,0.05)
    if not fast:
        _configure_advanced_forced_sleep(world,st)
    return world

def _configure_advanced_forced_sleep(world,st):
    """Optional Advanced-only anti-jitter sleep layered on top of native sleep.

    Keep this opt-in: bp forced sleep can deactivate an entire touching island. It is
    useful for pathological residual jitter (for example bodies that escaped a container),
    but native Box3D sleep remains the release default.
    """
    enabled=bool(getattr(st,'forced_sleep',False))
    try:
        world.set_forced_sleep(enabled,float(st.sleep_linear_threshold),float(st.sleep_angular_threshold),float(st.sleep_delay))
    except Exception:
        pass
    return world

def add_body(world,typ,obj,depsgraph,p_override=None,q_override=None,simplify=None,prepared=None,instance_scale=1.0,friction=None,restitution=None,density=None):
    p,q,_=world_pose_scale(obj)
    p=Vector(p_override) if p_override is not None else p
    if q_override is not None: q=q_override
    if simplify is None: simplify=bpy.context.scene.tumblify.convex_simplify
    data=prepared if prepared is not None else _prepare_collision(obj,depsgraph,simplify)
    sh=data['shape']; scale=max(0.001,float(instance_scale)); center=Vector(data['center'])*scale; half=Vector(data['half'])*scale
    if friction is None or restitution is None or density is None:
        mf,mr,md=_physics_material(obj)
        if friction is None: friction=mf
        if restitution is None: restitution=mr
        if density is None: density=md
    if sh=='SPHERE': h=world.add_sphere(typ,p,q,max(half),center,friction,restitution,density)
    elif sh=='CAPSULE':
        axis=int(data.get('axis',_long_axis(half))); r,long_half=_axis_radius_half(half,axis,True); hs=max(0.0,long_half-r)
        h=world.add_capsule(typ,p,q,hs,r,center,axis,friction,restitution,density)
    elif sh=='CYLINDER':
        # Production path: represent cylinders as an explicit convex hull instead of
        # Box3D's generated/transformed cylinder helper.  The latter produced unreliable
        # contact manifolds near the ends on some orientations even when the preview was
        # correct.  A 16-sided convex cylinder is still very cheap (32 vertices), is fully
        # supported by Box3D's normal hull path, and gives stable contacts over the complete
        # side wall and both caps.
        axis=int(data.get('axis',_cylinder_axis(half))); r,long_half=_axis_radius_half(half,axis,True)
        cv,_cf=_cylinder_geometry(long_half,r,(0,0,0),16)
        cv=[_axis_map_point(v,axis) for v in cv]
        # half and center already include instance_scale above.
        pts=[(v[0]+center.x,v[1]+center.y,v[2]+center.z) for v in cv]
        h=world.add_hull(typ,p,q,pts,32,friction,restitution,density)
    elif sh=='HULL':
        if 'hull_asset' in data: h=world.add_hull_asset(typ,p,q,data['hull_asset'],scale,friction,restitution,density)
        else: h=world.add_hull(typ,p,q,[(x*scale,y*scale,z*scale) for x,y,z in data['points']],data.get('max_vertices',min(48,len(data['points']))),friction,restitution,density)
    elif sh in {'MANUAL','LOOSE','CONTAINER'}:
        _cache_native_collision_assets(world,data)
        h=world.add_compound_mixed(typ,p,q,data['native_mixed_parts'],scale,friction,restitution,density)
    elif sh=='COMPOUND':
        _cache_native_collision_assets(world,data)
        h=world.add_compound_hull_assets(typ,p,q,data['compound_assets'],scale,friction,restitution,density)
    elif sh=='TERRAIN':
        if typ!=World.STATIC:raise RuntimeError('Terrain collision is static-only; use a convex proxy to release it')
        origin=Vector(data['origin'])*scale
        tp=Vector(p)+Quaternion((q[3],q[0],q[1],q[2])) @ origin
        tq=Quaternion((q[3],q[0],q[1],q[2])) @ Quaternion((1,0,0),math.pi/2)
        h=world.add_heightfield_static(tuple(tp),(tq.x,tq.y,tq.z,tq.w),[v*scale for v in data['heights']],data['materials'],data['nx'],data['nz'],data['dx']*scale,data['dz']*scale,friction,restitution)
    elif sh=='MESH':
        if typ!=World.STATIC:
            raise RuntimeError(f"'{obj.name}': Mesh collision is static-only in Box3D. Use Convex Hull, Compound Hull, Loose Parts Compound, or Manual Compound for spawned/dynamic objects.")
        h=world.add_mesh_static(p,q,data['verts'],data['indices'],friction,restitution)
    else:
        h=world.add_box(typ,p,q,(half.x,half.y,half.z),center,friction,restitution,density)
    if typ==World.DYNAMIC:
        st=bpy.context.scene.tumblify
        min_dim=max(0.0,2.0*min(abs(float(half.x)),abs(float(half.y)),abs(float(half.z))))
        if min_dim<=float(st.precision_bullet_thickness):
            fast_small=False
            if fast_small or bool(st.precision_auto_bullet): world.set_bullet(h,True)
            if fast_small or bool(st.precision_disable_recycling): world.set_contact_recycling(h,False)
    return h

def _set_brush_mode_visual(o,mode):
    # Object.color is used by Blender for object-color aware viewport drawing.
    # Keep the Empty itself so it stays lightweight and easy to manipulate.
    colors={
        'PUSH':(1.0,0.05,0.05,1.0),
        'PULL':(0.05,1.0,0.12,1.0),
        'OFF':(0.45,0.45,0.45,1.0),
    }
    o.color=colors.get(mode,colors['OFF'])
    o['Tumblify_Interaction']=mode


def ensure_brush(scene, *, fast_empty=False):
    o=bpy.data.objects.get(BRUSH_NAME)
    if o is not None and o.type!='EMPTY':
        old=o; old_mesh=getattr(old,'data',None); selected=old.select_get()
        active=bpy.context.view_layer.objects.active is old
        _remove_emitter_guide_nodes(old)
        old.name=BRUSH_NAME+'_LegacyMesh'
        o=bpy.data.objects.new(BRUSH_NAME,None)
        o.parent=old.parent; o.matrix_parent_inverse=old.matrix_parent_inverse.copy()
        o.matrix_world=old.matrix_world.copy()
        for key in old.keys():
            if key!='tumblify': o[key]=old[key]
        if old.animation_data and old.animation_data.action:
            o.animation_data_create(); o.animation_data.action=old.animation_data.action
        for collection in list(old.users_collection): collection.objects.link(o)
        old.user_remap(o)
        bpy.data.objects.remove(old,do_unlink=True)
        if old_mesh is not None and old_mesh.users==0: bpy.data.meshes.remove(old_mesh)
        if o.name in bpy.context.view_layer.objects:
            o.select_set(selected)
            if active: bpy.context.view_layer.objects.active=o
    if o is None: o=bpy.data.objects.new(BRUSH_NAME,None)
    if o.name not in scene.objects: scene.collection.objects.link(o)
    o.empty_display_type='SPHERE'
    o.empty_display_size=max(0.05,float(scene.tumblify.emitter_radius))
    o.hide_render=True; o.show_in_front=False
    _set_brush_mode_visual(o,'OFF')
    return o


def _remove_legacy_guide_object():
    o=bpy.data.objects.get(GUIDE_NAME)
    if o is not None:
        try: bpy.data.objects.remove(o,do_unlink=True)
        except Exception: pass



def _runtime_emergency_abort(owner, context, exc, label='Tumblify simulation'):
    """Last-resort teardown for modal runtime exceptions.

    A Blender modal operator must never leave timers/native worlds alive after an
    unexpected exception.  Keep this path deliberately defensive: every cleanup
    action is isolated so one broken RNA reference cannot prevent the rest.
    """
    global _RUNTIME, STOP_REQUESTED
    try:
        traceback.print_exc()
    except Exception:
        pass
    try:
        owner._tumblify_shutdown = True
    except (AttributeError, ReferenceError):
        pass
    completed = False
    try:
        if isinstance(owner, TU_OT_drop_offline):
            owner._cleanup(context, True)
            completed = True
        elif isinstance(owner, TU_OT_run):
            owner._finish_keep(context)
            completed = True
        elif isinstance(owner, (TU_OT_surface_run, TU_OT_post_run)):
            owner._finish(context)
            completed = True
    except Exception:
        pass
    if completed:
        try:
            owner.report({'ERROR'}, f'{label} aborted safely: {type(exc).__name__}: {exc}')
        except Exception:
            pass
        return {'CANCELLED'}
    try:
        job = getattr(owner, 'drop_job', None)
        if job is not None:
            try:
                job.cancel()
            finally:
                job.close()
                owner.drop_job = None
    except Exception:
        pass
    try:
        _restore_transform_hud(owner, context)
    except Exception:
        pass
    try:
        timer = getattr(owner, '_timer', None)
        if timer is not None:
            context.window_manager.event_timer_remove(timer)
            owner._timer = None
    except Exception:
        try:
            owner._timer = None
        except Exception:
            pass
    try:
        context.window_manager.progress_end()
    except Exception:
        pass
    try:
        handle = getattr(owner, '_draw_handle', None)
        if handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            owner._draw_handle = None
    except Exception:
        try:
            owner._draw_handle = None
        except Exception:
            pass
    try:
        _retire_native_rotate_target(owner, context)
    except Exception:
        pass
    try:
        if hasattr(owner, '_fast_preview_destroy'):
            owner._fast_preview_destroy()
    except Exception:
        pass
    try:
        _fast_clear_cursor_helpers()
    except Exception:
        pass
    try:
        if bool(getattr(owner, 'fast_runtime', False)):
            pass
    except Exception:
        pass
    try:
        for o in list(getattr(owner, 'dynamic_objs', []) or []):
            if _is_live_object(o):
                try:
                    o[FROZEN_PROP] = True
                    o[RUNTIME_PROP] = True
                    if hasattr(o, 'tumblify'):
                        o.tumblify.role = 'NONE'
                except Exception:
                    pass
    except Exception:
        pass
    try:
        world = getattr(owner, 'world', None)
        if world is not None:
            world.close()
    except Exception:
        pass
    try:
        _restore_mesh_collider_selection(getattr(owner, '_mesh_selection_lock', None))
    except Exception:
        pass
    try:
        for (name, old) in getattr(owner, '_post_passive_selection_lock', {}).items():
            o = bpy.data.objects.get(name)
            if o is not None:
                o.hide_select = old
    except Exception:
        pass
    try:
        owner.live_interactive = False
        owner.moving_selected = False
        owner.pending_move_finish = False
    except Exception:
        pass
    _solid_state_restore()
    _RUNTIME = None
    STOP_REQUESTED = False
    try:
        owner.report({'ERROR'}, f'{label} aborted safely: {type(exc).__name__}: {exc}')
    except Exception:
        pass
    try:
        if context.area:
            context.area.tag_redraw()
    except Exception:
        pass
    return {'CANCELLED'}


def update_emitter_guide(scene):
    _remove_legacy_guide_object()
    _sync_emitter_guides(scene)

def _lock_mesh_collider_selection(scene):
    locked=[]
    for o in (_advanced_runtime_static_objects(scene) if getattr(scene.tumblify,'mode','FAST')=='DROP' else _scene_role_objects(scene,'STATIC')):
        if o and o.tumblify.shape in {'MESH','TERRAIN'}:
            locked.append((o,bool(o.hide_select)))
            try:
                if o.select_get(): o.select_set(False)
                o.hide_select=True
            except Exception: pass
    return locked

def _restore_mesh_collider_selection(locked):
    for o,old in locked or []:
        try:
            if o and o.name in bpy.data.objects: o.hide_select=old
        except Exception: pass

class TU_OT_toggle_emitter_wire(bpy.types.Operator):
    bl_idname='tumblify.toggle_emitter_wire'; bl_label='Toggle Emitter Wire'
    bl_description='Toggle wireframe display for the configured emitter'
    def execute(self,context):
        s=context.scene.tumblify
        s.emitter_wire_display=not bool(s.emitter_wire_display)
        _sync_emitter_wire(context.scene)
        if context.area: context.area.tag_redraw()
        return {'FINISHED'}

class TU_OT_prepare_emitter(bpy.types.Operator):
    bl_idname='tumblify.prepare_emitter'; bl_label='Create / Select Fixed Emitter'
    def execute(self,context):
        s=context.scene.tumblify
        was_ready=bool(getattr(s,'advanced_emitter_ready',False))
        o=ensure_brush(context.scene)
        if (not was_ready) or o.get('Tumblify_EmitterInitialized') is None:
            o.location=context.scene.cursor.location.copy()
            o['Tumblify_EmitterInitialized']=True
        for x in context.selected_objects: x.select_set(False)
        o.hide_viewport=False; o.hide_set(False)
        if o.name not in context.view_layer.objects:
            if context.scene.collection.objects.get(o.name) is None: context.scene.collection.objects.link(o)
            context.view_layer.update()
        o.select_set(True); context.view_layer.objects.active=o
        global _EMITTER_SYNCING
        try:
            _EMITTER_SYNCING=True
            s.emitter_collection=None; s.emitter_mode='FIXED'; s.emitter_object=o; s.advanced_emitter_ready=True
        finally:
            _EMITTER_SYNCING=False
        _advanced_after_emitter_setup(context.scene,o)
        _sync_emitter_wire(context.scene); update_emitter_guide(context.scene)
        self.report({'INFO'},'Fixed emitter selected. Move/rotate it before Start Drop.')
        return {'FINISHED'}

class TU_OT_use_active_emitter(bpy.types.Operator):
    bl_idname = 'tumblify.use_active_emitter'
    bl_label = 'Use Selected as Emitter'
    bl_description = 'Use the active selected Mesh object as the Advanced emitter'

    @classmethod
    def poll(cls, context):
        try:
            active = context.view_layer.objects.active
            return bool(active and active.select_get() and (active.type == 'MESH') and (active.name not in {BRUSH_NAME, GUIDE_NAME, LEGACY_ADV_DEMO_SOURCE_NAME}))
        except Exception:
            return False

    def execute(self, context):
        s = context.scene.tumblify
        active = context.view_layer.objects.active
        global _EMITTER_SYNCING
        try:
            _EMITTER_SYNCING = True
            s.emitter_collection = None
            s.emitter_object = active
            s.emitter_mode = 'CUSTOM'
            s.advanced_emitter_ready = True
        finally:
            _EMITTER_SYNCING = False
        _advanced_after_emitter_setup(context.scene, active)
        _set_brush_mode_visual(active, s.emitter_interaction)
        _sync_emitter_wire(context.scene)
        update_emitter_guide(context.scene)
        self.report({'INFO'}, f'Emitter: {active.name}')
        return {'FINISHED'}


def _add_instance_results_as_static(world,scene,depsgraph,source_collision,simplify,friction,exclude=None,owner=None,progress=None,progress_range=(70,80)):
    count=0
    cache={}
    excluded=set(exclude or [])
    participants=[]
    instances=_frozen_instance_results(scene)
    total=sum(len(inst.data.vertices) for inst in instances if inst not in excluded)
    if progress:progress.update(progress_range[0],'Restoring Combined pile colliders')
    for inst in instances:
        if inst in excluded: continue
        src_name=str(inst.get('Tumblify_SourceName','')); src=bpy.data.objects.get(src_name); helper=_instance_result_helper(inst)
        material_obj=src or helper
        if material_obj is None: continue
        pdata=source_collision.get(src_name) if src_name else None
        if pdata is None:
            pdata=cache.get(src_name or inst.name)
            if pdata is None:
                pdata=_prepare_collision(material_obj,depsgraph,simplify)
                _cache_native_collision_assets(world,pdata); cache[src_name or inst.name]=pdata
        participants.append(inst)
        for pose,sc in _instance_result_entries(inst):
            p=Vector(pose[:3]); q=Quaternion((pose[6],pose[3],pose[4],pose[5]))
            add_body(world,World.STATIC,material_obj,depsgraph,p_override=p,q_override=q,simplify=simplify,prepared=pdata,instance_scale=sc,friction=friction,restitution=0.0,density=0.0)
            count+=1
            if progress:progress.update(progress_range[0]+(progress_range[1]-progress_range[0])*count/max(1,total),f'Restoring pile colliders {count}/{total}')
    if progress:progress.update(progress_range[1],'Previous piles restored')
    if owner is not None:
        owner.instance_static_results=participants
        owner.instance_static_body_count=count
    return count

def _debug_collection(scene):
    c=bpy.data.collections.get(DEBUG_COLLECTION)
    if not c:
        c=bpy.data.collections.new(DEBUG_COLLECTION)
        scene.collection.children.link(c)
    return c


def _delete_debug_object(obj):
    mesh=obj.data if obj.type=='MESH' else None
    groups=[mod.node_group for mod in obj.modifiers if mod.type=='NODES' and mod.node_group and mod.node_group.get('Tumblify_DebugOwned')]
    bpy.data.objects.remove(obj,do_unlink=True)
    if mesh is not None and mesh.users==0:bpy.data.meshes.remove(mesh)
    for group in groups:
        if group.users==0:bpy.data.node_groups.remove(group)


def _clear_debug_collection(keep_scene_wire=False):
    if not keep_scene_wire:_clear_scene_collision_wire()
    c=bpy.data.collections.get(DEBUG_COLLECTION)
    if c:
        for o in list(c.objects):
            _delete_debug_object(o)
        bpy.data.collections.remove(c)


def _remove_collision_debug_for_source(source_name):
    """Remove only debug objects belonging to one source, preserving all others."""
    c=bpy.data.collections.get(DEBUG_COLLECTION)
    if not c: return 0
    removed=0
    for o in list(c.objects):
        try:
            if str(o.get('Tumblify_DebugSource',''))!=str(source_name):
                continue
            _delete_debug_object(o); removed+=1
        except Exception: pass
        except Exception:
            pass
    return removed


def _new_debug_mesh_object(scene,name,verts,faces,location,quat):
    me=bpy.data.meshes.new(name+"_Mesh")
    me.from_pydata(verts,[],faces)
    me.update()
    o=bpy.data.objects.new(name,me)
    _debug_collection(scene).objects.link(o)
    o.location=location
    o.rotation_mode='QUATERNION'
    o.rotation_quaternion=(quat[3],quat[0],quat[1],quat[2])
    o.display_type='WIRE'
    o.show_in_front=False
    o.hide_render=True
    o.hide_select=True
    o['Tumblify_Helper']=True
    try: o.select_set(False)
    except Exception: pass
    o.color=(1.0,0.18,0.05,1.0)
    return o



def _advanced_simulation_collision_targets(scene):
    """Mesh objects whose collision shapes participate in the configured Advanced run."""
    out=[]; seen=set()
    groups=[]
    try: groups.append(_scene_role_objects(scene,'SOURCE'))
    except Exception: groups.append([])
    try: groups.append(_advanced_runtime_static_objects(scene))
    except Exception: groups.append([])
    if scene.tumblify.spawn_location=='SURFACE' or scene.tumblify.fill_emitter:
        groups.append(_configured_emitter_objects(scene))

    for group in groups:
        for obj in group:
            if not _is_live_object(obj) or getattr(obj,'type',None)!='MESH':
                continue
            target=_collision_target_from_object(scene,obj)
            if target is None or target.name in seen:
                continue
            seen.add(target.name); out.append(target)
    return out


def _collision_preview_fit(scene,obj,depsgraph):
    """Resolve the shape shown for an arbitrary selected mesh."""
    if obj.get(INSTANCE_RESULT_PROP,False): return None
    fast=(getattr(scene.tumblify,'mode','DROP')=='FAST')
    if not fast and scene.tumblify.spawn_location=='SURFACE' and obj in _configured_emitter_objects(scene):
        return {'shape':'MESH'}
    shape=_collision_target_shape(scene,obj)
    if shape=='DETECT':
        return _cache_auto_collision_result(obj,depsgraph,True)
    if fast:
        return _fast_collision_fit_for_shape(obj,depsgraph,shape)
    return None

def _fast_simulation_collision_targets(scene):
    """All actual Fast run participants: captured sources plus static meshes inside the collider selection."""
    sources = _fast_source_objects(scene)
    targets = []
    seen = set()
    statics=(_prepared_scene_preview_objects(scene,sources) if scene.tumblify.fast_static_scope=='SCENE' else _fast_static_objects(scene,sources))
    for obj in sources + statics:
        if not _is_live_object(obj) or obj.type != 'MESH' or obj.name in seen:
            continue
        seen.add(obj.name)
        targets.append(obj)
    return targets


def _collision_debug_targets(scene):
    rt=_RUNTIME
    if rt is not None and getattr(rt,'world',None) is not None:
        candidates=list(getattr(rt,'sources',[]))+list(getattr(rt,'static_objs',[]))+list(getattr(rt,'dynamic_objs',[]))+list(getattr(rt,'instance_static_results',getattr(rt,'instance_results',[])))
        if scene.tumblify.spawn_location=='SURFACE' or scene.tumblify.fill_emitter:
            candidates+=list(getattr(rt,'emitter_objs',[]))+[getattr(rt,'emitter',None),getattr(rt,'surface_emitter',None)]
    elif scene.tumblify.mode=='FAST':
        candidates=_fast_simulation_collision_targets(scene)+_frozen_spawned(scene)+_frozen_instance_results(scene)
    else:
        candidates=_advanced_simulation_collision_targets(scene)+_frozen_spawned(scene)+_frozen_instance_results(scene)
    targets=[];seen=set()
    for obj in candidates:
        if not _is_live_object(obj) or obj.get(FIXED_PROP): continue
        if obj.get(INSTANCE_RESULT_PROP,False):
            src=bpy.data.objects.get(str(obj.get('Tumblify_SourceName',''))) or _instance_result_helper(obj)
            target=obj if _is_live_object(src) else None
        else:
            target=_collision_target_from_object(scene,obj)
        if target is not None and target.as_pointer() not in seen:
            seen.add(target.as_pointer());targets.append(target)
    return targets



def _refresh_collision_debug(scene):
    """Rebuild collision geometry for configured or active simulation participants."""
    if not bool(getattr(scene.tumblify, 'show_collision_debug', False)):
        return
    targets = _collision_debug_targets(scene)
    _clear_debug_collection(keep_scene_wire=scene.tumblify.mode=='FAST' and scene.tumblify.fast_static_scope=='SCENE' and _get_scene_cache(scene) is not None)
    if not targets:
        _tag_view3d_redraw_all()
        return
    depsgraph = bpy.context.evaluated_depsgraph_get()
    st=scene.tumblify;batched=[]
    if st.mode=='FAST' and st.fast_static_scope=='SCENE' and _get_scene_cache(scene):
        sources=set(getattr(_RUNTIME,'sources',[]) if _RUNTIME is not None else _fast_source_objects(scene))
        sources.update(getattr(_RUNTIME,'dynamic_objs',[]) if _RUNTIME is not None else [])
        batched=[obj for obj in targets if obj not in sources and _cached_scene_collision(scene,obj) is not None]
    batched_set=set(batched)
    progress=None
    if batched and not bpy.app.background:
        progress=_RealtimePreparation(bpy.context,'Collision preview',action='Building preview')
        progress.begin()
    try:
        if batched:_build_scene_collision_wire(scene,batched,progress)
        for obj in targets:
            if obj in batched_set:continue
            try:
                fit=_collision_preview_fit(scene,obj,depsgraph)
                create_collision_debug(scene,obj,depsgraph,shape_override=fit)
            except Exception:continue
    finally:
        if progress:progress.close()
    _tag_view3d_redraw_all()



def _create_instance_collision_debug(scene,inst,depsgraph):
    """Instance a shared collision mesh at saved world poses without realizing the pile."""
    src=bpy.data.objects.get(str(inst.get('Tumblify_SourceName',''))) or _instance_result_helper(inst)
    if not _is_live_object(src): return None
    template=create_collision_debug(scene,src,depsgraph)
    if template is None: return None
    entries=_instance_result_entries(inst)
    if not entries:
        _delete_debug_object(template)
        return None
    template.location=(0,0,0);template.rotation_quaternion=(1,0,0,0)
    template['Tumblify_DebugSource']=inst.name;template['Tumblify_Helper']=True
    template.hide_set(True)
    debug=_new_debug_mesh_object(scene,'Tumblify_Collision_'+inst.name,[pose[:3] for pose,_ in entries],[],(0,0,0),(0,0,0,1))
    debug['Tumblify_DebugSource']=inst.name
    debug['Tumblify_DebugShape']=template.get('Tumblify_DebugShape','HULL')
    debug['Tumblify_DebugInstanceCount']=len(entries);debug['Tumblify_Helper']=True
    rotations=debug.data.attributes.new('debug_rotation','FLOAT_VECTOR','POINT')
    scales=debug.data.attributes.new('debug_scale','FLOAT_VECTOR','POINT')
    rotations.data.foreach_set('vector',[angle for pose,_ in entries for angle in Quaternion((pose[6],pose[3],pose[4],pose[5])).to_euler('XYZ')])
    scales.data.foreach_set('vector',[float(scale) for _,scale in entries for _axis in range(3)])
    group=bpy.data.node_groups.new('Tumblify_DebugInstances','GeometryNodeTree')
    group['Tumblify_DebugOwned']=True
    group.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
    group.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    nodes=group.nodes;links=group.links
    inp=nodes.new('NodeGroupInput');out=nodes.new('NodeGroupOutput')
    info=nodes.new('GeometryNodeObjectInfo');info.inputs['Object'].default_value=template
    info.transform_space='ORIGINAL';info.inputs['As Instance'].default_value=True
    rot=nodes.new('GeometryNodeInputNamedAttribute');rot.data_type='FLOAT_VECTOR';rot.inputs['Name'].default_value='debug_rotation'
    scale=nodes.new('GeometryNodeInputNamedAttribute');scale.data_type='FLOAT_VECTOR';scale.inputs['Name'].default_value='debug_scale'
    points=nodes.new('GeometryNodeInstanceOnPoints')
    links.new(inp.outputs['Geometry'],points.inputs['Points']);links.new(info.outputs['Geometry'],points.inputs['Instance'])
    links.new(rot.outputs['Attribute'],points.inputs['Rotation']);links.new(scale.outputs['Attribute'],points.inputs['Scale'])
    links.new(points.outputs['Instances'],out.inputs['Geometry'])
    mod=debug.modifiers.new('Collision Instances','NODES');mod.node_group=group
    return debug



def create_collision_debug(scene,obj,depsgraph,shape_override=None):
    if obj.get(INSTANCE_RESULT_PROP,False):
        return _create_instance_collision_debug(scene,obj,depsgraph)
    p,q,_=world_pose_scale(obj); center,half=_local_bounds(obj,depsgraph)
    auto_fit=_resolve_collision_fit(obj,depsgraph,shape_override)
    sh=auto_fit['shape']
    if sh=='BOX':
        verts,faces=_box_geometry(half.x,half.y,half.z); verts=[(x+center.x,y+center.y,z+center.z) for x,y,z in verts]
    elif sh=='SPHERE':
        verts,faces=_sphere_geometry(max(half)); verts=[(x+center.x,y+center.y,z+center.z) for x,y,z in verts]
    elif sh=='CAPSULE':
        axis=int(auto_fit.get('axis')) if auto_fit and auto_fit.get('axis') is not None else _long_axis(half); r,long_half=_axis_radius_half(half,axis,True); hs=max(0.0,long_half-r); verts,faces=_capsule_geometry(hs,r)
        verts=[_axis_map_point(v,axis) for v in verts]
        verts=[(x+center.x,y+center.y,z+center.z) for x,y,z in verts]
    elif sh=='CYLINDER':
        axis=int(auto_fit.get('axis')) if auto_fit and auto_fit.get('axis') is not None else _cylinder_axis(half); r,long_half=_axis_radius_half(half,axis,True); verts0,faces=_cylinder_geometry(long_half,r,(0,0,0),16)
        verts=[_axis_map_point(v,axis) for v in verts0]
        verts=[(x+center.x,y+center.y,z+center.z) for x,y,z in verts]
    elif sh=='HULL':
        verts,faces=_evaluated_convex_geometry(obj,depsgraph,scene.tumblify.convex_simplify)
        if not faces:
            verts,faces=_hull_debug_geometry(verts)
    elif sh=='COMPOUND':
        parts=_evaluated_compound_geometry(obj,depsgraph,scene.tumblify.convex_simplify,scene.tumblify.compound_max_hulls)
        verts=[]; faces=[]
        for part in parts:
            pv=part.get('points',[]); pf=part.get('faces',[])
            if not pf:
                pv,pf=_hull_debug_geometry(pv)
            off=len(verts); verts.extend(pv); faces.extend(tuple(off+i for i in f) for f in pf)
    elif sh=='LOOSE':
        parts=_evaluated_loose_parts_geometry(obj,depsgraph,scene.tumblify.convex_simplify,scene.tumblify.loose_max_parts,scene.tumblify.auto_fit_tolerance)
        verts=[]; faces=[]
        for part in parts:
            pv=part.get('points',[]); pf=part.get('faces',[])
            if not pf: pv,pf=_hull_debug_geometry(pv)
            off=len(verts); verts.extend(pv); faces.extend(tuple(off+i for i in f) for f in pf)
    elif sh=='CONTAINER':
        parts=_container_surface_parts(obj,depsgraph); verts=[]; faces=[]
        for part in parts:
            pv=part.get('points',[]); pf=part.get('faces',[])
            if not pf: pv,pf=_hull_debug_geometry(pv)
            off=len(verts); verts.extend(pv); faces.extend(tuple(off+i for i in f) for f in pf)
    elif sh=='MANUAL':
        parts=_manual_compound_parts(obj); verts=[]; faces=[]
        for part in parts:
            pv=part.get('points',[]); pf=part.get('faces',[])
            if not pf: pv,pf=_hull_debug_geometry(pv)
            off=len(verts); verts.extend(pv); faces.extend(tuple(off+i for i in f) for f in pf)
    elif sh in {'MESH','TERRAIN'}:
        if sh=='TERRAIN':
            from .collision import _evaluated_terrain_data
            terrain=_evaluated_terrain_data(obj,depsgraph);verts=terrain['verts'];indices=terrain['indices']
        else:verts,indices,source_tris,collision_tris=_evaluated_collision_mesh_data(obj,depsgraph)
        tri_count=len(indices)//3
        # Display-only budget. The native Box3D collision is NOT simplified here.
        if tri_count>3000:
            stride=max(1,(tri_count+2999)//3000)
            sampled=range(0,tri_count,stride)
            used=sorted({int(indices[3*i+j]) for i in sampled for j in range(3)})
            remap={old:i for i,old in enumerate(used)}
            verts=[verts[i] for i in used]
            faces=[tuple(remap[int(indices[3*i+j])] for j in range(3)) for i in range(0,tri_count,stride)]
        else:
            faces=[tuple(indices[i:i+3]) for i in range(0,len(indices),3)]
    else: return None
    dbg=_new_debug_mesh_object(scene,f"Tumblify_Collision_{obj.name}_{sh}",verts,faces,p,q)
    dbg['Tumblify_DebugSource']=obj.name
    dbg['Tumblify_DebugShape']=str(sh)
    return dbg

class TU_OT_assign_shape(bpy.types.Operator):
    bl_idname='tumblify.assign_shape'; bl_label='Assign Shape'
    def execute(self,context):
        sh=context.scene.tumblify.assign_shape
        depsgraph=context.evaluated_depsgraph_get() if sh=='DETECT' else None
        changed=[]
        for o in context.selected_objects:
            if o.type!='MESH': continue
            o.tumblify.shape=sh
            changed.append(o)
            if sh=='DETECT': _cache_auto_collision_result(o,depsgraph,True)
            else:
                for key in (AUTO_DETECTED_SHAPE_PROP,AUTO_DETECTED_AXIS_PROP):
                    try:
                        if key in o: del o[key]
                    except Exception: pass
        # Advanced used to keep the old debug proxy until Show Collision was toggled.
        # Rebuild only changed objects so Assign updates the viewport immediately.
        st=context.scene.tumblify
        if changed and bool(getattr(st,'show_collision_debug',False)):
            try:
                dg=context.evaluated_depsgraph_get()
                for o in changed:
                    _remove_collision_debug_for_source(o.name)
                    fit=_cache_auto_collision_result(o,dg,True) if sh=='DETECT' else None
                    create_collision_debug(context.scene,o,dg,shape_override=fit)
                _tag_view3d_redraw_all()
                _ensure_collision_overlay(context.scene)
            except Exception as exc:
                self.report({'WARNING'},f'Collision preview refresh failed: {exc}')
        return {'FINISHED'}

def _list_contains(coll,obj):
    return any(item.obj==obj for item in coll)

def _list_add_unique(coll,obj):
    if obj and not _list_contains(coll,obj):
        item=coll.add(); item.obj=obj

def _list_remove_object(coll,obj):
    for i in range(len(coll)-1,-1,-1):
        if coll[i].obj==obj or coll[i].obj is None:
            coll.remove(i)


def _static_collider_candidates(scene, candidates, exclude=(), *, include_hidden=False):
    """Filter and deduplicate statics without changing each mode's scope.

    collider selection scans and Fast Selected ignore hidden meshes. Advanced's explicit List
    keeps its historical hidden-object behavior. Frozen/runtime results are added
    separately by their owning runtime paths, never as ordinary static entries.
    """
    excluded = set(exclude)
    seen = set()
    out = []
    helper_names = {BRUSH_NAME, GUIDE_NAME, LEGACY_ADV_DEMO_SOURCE_NAME, FAST_CURSOR_NAME, FAST_CURSOR_RAY_NAME, FAST_GHOST_NAME}
    for obj in candidates:
        if not _is_live_object(obj):
            continue
        try:
            if obj in excluded or obj in seen or obj.name not in scene.objects or (obj.type != 'MESH'):
                continue
            if obj.name in helper_names or obj.get('Tumblify_Helper') or obj.get(DISPLAY_PROXY_PROP) or obj.get('Tumblify_BatchPreview'):
                continue
            if obj.get(RUNTIME_PROP) or obj.get(FROZEN_PROP) or obj.get(FIXED_PROP):
                continue
            if not include_hidden and obj.hide_get():
                continue
        except (ReferenceError, RuntimeError):
            continue
        seen.add(obj)
        out.append(obj)
    return out


def _advanced_runtime_static_objects(scene):
    return _scene_role_objects(scene,'STATIC')


def _advanced_remove_legacy_demo_source(scene):
    st=scene.tumblify
    o=bpy.data.objects.get(LEGACY_ADV_DEMO_SOURCE_NAME)
    if o is not None:
        _list_remove_object(st.dynamic_sources,o)
        me=o.data if o.type=='MESH' else None
        try: bpy.data.objects.remove(o,do_unlink=True)
        except Exception: pass
        try:
            if me is not None and me.users==0: bpy.data.meshes.remove(me)
        except Exception: pass


def _advanced_after_emitter_setup(scene, emitter):
    st = scene.tumblify
    try:
        st.advanced_emitter_ready = bool(emitter is not None or getattr(st, 'emitter_collection', None))
    except Exception:
        pass
    if emitter is not None:
        _list_remove_object(st.dynamic_sources, emitter)
        _list_remove_object(st.static_objects, emitter)
        try:
            emitter.tumblify.role = 'NONE'
        except Exception:
            pass
    _advanced_remove_legacy_demo_source(scene)
    _tag_view3d_redraw_all()


def _fast_source_objects(scene):
    st=scene.tumblify
    out=[]
    for item in st.fast_sources:
        o=item.obj
        if _is_live_object(o) and o.name in scene.objects and o.type=='MESH' and o not in out:
            out.append(o)
    return out

def _fast_world_bounds(objects):
    pts=[]
    for o in objects:
        if not _is_live_object(o): continue
        try: pts.extend([o.matrix_world @ Vector(c) for c in o.bound_box])
        except Exception: pass
    if not pts: return None
    mn=Vector((min(p.x for p in pts),min(p.y for p in pts),min(p.z for p in pts)))
    mx=Vector((max(p.x for p in pts),max(p.y for p in pts),max(p.z for p in pts)))
    return mn,mx



def _fast_static_objects(scene,sources):
    st=scene.tumblify
    candidates=[item.obj for item in st.fast_selected_statics] if st.fast_static_scope=='SELECTED' else scene.objects
    excluded=set(sources)
    if st.fast_static_scope=='SCENE':excluded.update(_fast_source_objects(scene))
    return _static_collider_candidates(scene,candidates,excluded)


def _fast_prepare_cursor(scene,objects=None):
    st=scene.tumblify
    st.fast_cursor_x=float(scene.cursor.location.x)
    st.fast_cursor_y=float(scene.cursor.location.y)
    bounds=_fast_world_bounds(list(objects or _fast_source_objects(scene)))
    st.fast_cursor_height=max(0.5,float(bounds[1].z-bounds[0].z)*2.0) if bounds else 2.0
    st.fast_cursor_ready=True
    return True


def _fast_clear_cursor_helpers():
    for name in (FAST_CURSOR_NAME,FAST_CURSOR_RAY_NAME,FAST_GHOST_NAME):
        o=bpy.data.objects.get(name)
        if o is not None:
            try:
                data=o.data if hasattr(o,'data') else None
                bpy.data.objects.remove(o,do_unlink=True)
                if data is not None and getattr(data,'users',1)==0 and hasattr(bpy.data,'meshes') and isinstance(data,bpy.types.Mesh): bpy.data.meshes.remove(data)
            except Exception: pass

def _fast_ensure_cursor_helpers(scene):
    empty=bpy.data.objects.get(FAST_CURSOR_NAME)
    if empty is None:
        empty=bpy.data.objects.new(FAST_CURSOR_NAME,None); scene.collection.objects.link(empty)
        empty.empty_display_type='CIRCLE'; empty.empty_display_size=0.12; empty.hide_render=True; empty.hide_select=True; empty['Tumblify_Helper']=True
    ray=bpy.data.objects.get(FAST_CURSOR_RAY_NAME)
    if ray is None:
        me=bpy.data.meshes.new(FAST_CURSOR_RAY_NAME+'Mesh'); ray=bpy.data.objects.new(FAST_CURSOR_RAY_NAME,me); scene.collection.objects.link(ray)
        ray.display_type='WIRE'; ray.show_in_front=False; ray.hide_render=True; ray.hide_select=True; ray['Tumblify_Helper']=True
    return empty,ray

def _fast_emitter_rotation_quat(scene):
    try:
        vals=tuple(float(v) for v in scene.tumblify.fast_emitter_rotation)
        return Euler(vals,'XYZ').to_quaternion().normalized()
    except Exception:
        return Quaternion((1.0,0.0,0.0,0.0))

def _fast_set_emitter_rotation_quat(scene,q):
    try:
        e=q.normalized().to_euler('XYZ')
        scene.tumblify.fast_emitter_rotation=(float(e.x),float(e.y),float(e.z))
    except Exception:
        pass

def _fast_reset_emitter_rotation(scene):
    """Reset the Fast placement pivot to its neutral orientation for a new run.

    Fast pivot rotation is intentionally session-local.  Users may rotate the pivot
    with R while a Fast Drop is active, but that orientation must not leak into the
    next simulation.
    """
    try:
        scene.tumblify.fast_emitter_rotation=(0.0,0.0,0.0)
    except Exception:
        pass
    empty=bpy.data.objects.get(FAST_CURSOR_NAME)
    if empty is not None:
        try:
            empty.rotation_mode='QUATERNION'
            empty.rotation_quaternion=Quaternion((1.0,0.0,0.0,0.0))
        except Exception:
            pass

def _fast_update_cursor_helpers(scene,p):
    empty,ray=_fast_ensure_cursor_helpers(scene)
    p=Vector(p); empty.location=p
    # The Fast cursor is also the orientation emitter. ARROWS makes its local axes
    # visible while R-rotate is active; the vertical ray remains a world-space
    # placement guide and is deliberately not rotated.
    try:
        empty.empty_display_type='ARROWS'; empty.empty_display_size=0.32
        empty.rotation_mode='QUATERNION'; empty.rotation_quaternion=_fast_emitter_rotation_quat(scene)
    except Exception:
        pass
    try: ray.data.clear_geometry()
    except Exception: pass
    ray.data.from_pydata([(p.x,p.y,0.0),(p.x,p.y,p.z)],[(0,1)],[]); ray.data.update()


def _cache_auto_collision_result(obj,depsgraph=None,refresh=True):
    """Cache the diagnostic result of Auto Detect without replacing DETECT mode."""
    if not _is_live_object(obj) or obj.type!='MESH': return None
    if not refresh:
        sh=str(obj.get(AUTO_DETECTED_SHAPE_PROP,''))
        if sh:
            result={'shape':sh}
            try:
                ax=int(obj.get(AUTO_DETECTED_AXIS_PROP,-1))
                if ax>=0: result['axis']=ax
            except Exception: pass
            return result
    try:
        depsgraph=depsgraph or bpy.context.evaluated_depsgraph_get()
        result=_auto_collision_result(obj,depsgraph)
        sh=str(result.get('shape','HULL'))
        obj[AUTO_DETECTED_SHAPE_PROP]=sh
        if result.get('axis') is not None: obj[AUTO_DETECTED_AXIS_PROP]=int(result['axis'])
        elif AUTO_DETECTED_AXIS_PROP in obj: del obj[AUTO_DETECTED_AXIS_PROP]
        return result
    except Exception:
        return None

def _cached_auto_shape(obj):
    try:
        return str(obj.get(AUTO_DETECTED_SHAPE_PROP,'')) if _is_live_object(obj) else ''
    except Exception:
        return ''

def _auto_collision_shape(obj,depsgraph):
    return _auto_collision_result(obj,depsgraph).get('shape','HULL')



def _fast_collision_fit_for_shape(obj,depsgraph,shape):
    shape=str(shape or 'DETECT')
    if shape in {'MESH','TERRAIN'}:
        return {'shape':shape}
    if shape not in FAST_COLLISION_CYCLE:
        shape='DETECT'
    return _resolve_collision_fit(obj,depsgraph,shape)

def _fast_collision_fit(obj,depsgraph):
    """Collision fit for a spawned Fast source."""
    shape=str(getattr(obj.tumblify,'shape','DETECT')) if _is_live_object(obj) else 'DETECT'
    return _fast_collision_fit_for_shape(obj,depsgraph,shape)

def _fast_static_collision_shape(obj):
    """Per-object Fast static override. Exact Mesh stays the non-destructive default."""
    if not _is_live_object(obj): return 'MESH'
    try: shape=str(obj.get(FAST_STATIC_SHAPE_PROP,'MESH'))
    except Exception: shape='MESH'
    return shape if shape in FAST_STATIC_COLLISION_CYCLE else 'MESH'

def _fast_static_collision_fit(obj,depsgraph):
    return _fast_collision_fit_for_shape(obj,depsgraph,_fast_static_collision_shape(obj))

def _fast_is_static(scene,obj):
    return obj in _fast_static_objects(scene,_fast_source_objects(scene))


def _fast_refresh_shape_summary(scene,depsgraph=None):
    st=scene.tumblify; dg=depsgraph or bpy.context.evaluated_depsgraph_get()
    shapes=[]
    for o in _fast_source_objects(scene):
        try:
            fit=_fast_collision_fit(o,dg); label=fit.get('shape','HULL').replace('_',' ').title()
            if o.tumblify.shape!='DETECT': label += ' *'
            shapes.append(f'{o.name} [{label}]')
        except Exception:
            pass
    st.fast_shape_summary=' • '.join(shapes[:4])

def _fast_set_orbit_pivot(op, context):
    """Keep the complete viewport state unchanged when starting Fast."""
    pass


def _fast_view_depth(context, depth_point=None):
    """Keep the placement plane in front of the current perspective view."""
    rv3d = context.space_data.region_3d
    focus = Vector(rv3d.view_location)
    point = focus.copy() if depth_point is None else Vector(depth_point)
    if rv3d.is_perspective:
        camera = rv3d.view_matrix.inverted()
        eye = camera.translation
        forward = -(camera.to_3x3() @ Vector((0, 0, 1)))
        near = max(0.001, float(context.space_data.clip_start) * 2.0)
        if (point - eye).dot(forward) <= near:
            if (focus - eye).dot(forward) > near:
                return focus
            return eye + forward * max(near * 2.0, float(rv3d.view_distance), 1.0)
    return point


def _fast_initial_preview_target(context, event, owner=None):
    """Start under the pointer at the view focus, independent of scene bounds."""
    region = next((r for r in context.area.regions if r.type == 'WINDOW'), context.region)
    x = float(event.mouse_x - region.x)
    y = float(event.mouse_y - region.y)
    # A sidebar/popover button may be outside the usable viewport rectangle.
    if not (24 <= x < region.width - 24 and 24 <= y < region.height - 24):
        x, y = region.width * 0.5, region.height * 0.5
    # Sidebar controls overlap the WINDOW region: do not aim through the panel.
    for panel in context.area.regions:
        if panel.type in {'UI', 'TOOLS'} and panel.width > 1:
            if panel.x <= event.mouse_x < panel.x + panel.width and panel.y <= event.mouse_y < panel.y + panel.height:
                right = min(region.x + region.width, panel.x) if panel.x > region.x else region.x + region.width
                x, y = max(24, (right - region.x) * 0.5), region.height * 0.5
                break
    rv3d = context.space_data.region_3d
    if owner is not None:
        picker = _fast_snap.SurfacePicker(owner, context)
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (x, y))
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (x, y)).normalized()
        hit = picker.cast(context, origin, direction)
        if hit is not None:
            # Free Drop remains view-aligned. Only its initial depth is taken
            # from actual supports, with a small front clearance for the source.
            clearance = max(0.5, float(context.scene.tumblify.fast_cursor_height))
            distance = (hit[0] - origin).dot(direction)
            return origin + direction * max(float(context.space_data.clip_start) * 2, distance - clearance)
    return view3d_utils.region_2d_to_location_3d(region, rv3d, (x, y), _fast_view_depth(context))


class _FastViewportEvent:
    """Preserve native event fields while translating panel coordinates to the view."""
    def __init__(self, event, region):
        self._event = event
        self.mouse_region_x = event.mouse_x - region.x
        self.mouse_region_y = event.mouse_y - region.y

    def __getattr__(self, name):
        return getattr(self._event, name)


def _fast_viewport_region(context):
    return next((r for r in context.area.regions if r.type == 'WINDOW'), context.region)


def _fast_mouse_3d_point(context, event, depth_point=None):
    """Follow the pointer on a view-aligned plane; recover from behind-camera depth."""
    region = _fast_viewport_region(context)
    if context.region != region:
        event = _FastViewportEvent(event, region)
    coord = (event.mouse_region_x, event.mouse_region_y)
    try:
        return view3d_utils.region_2d_to_location_3d(
            region, context.space_data.region_3d, coord,
            _fast_view_depth(context, depth_point))
    except Exception:
        return None

class TU_OT_fast_capture(bpy.types.Operator):
    bl_idname = 'tumblify.fast_capture'
    bl_label = 'Use Selected as Fast Sources'
    bl_options = {'REGISTER', 'UNDO'}

    append: BoolProperty(name='Add to List', description='Add selected objects without replacing the current list', default=False, options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return 'Add selected mesh objects to the current list; existing entries are kept and duplicates are skipped' if properties.append else 'Replace the list with selected mesh objects'

    def execute(self, context):
        st = context.scene.tumblify
        selected = [o for o in context.selected_objects if o.type == 'MESH' and (not o.get('Tumblify_Helper'))]
        if not selected:
            self.report({'ERROR'}, 'Select at least one Mesh source object')
            return {'CANCELLED'}
        if not self.append:
            st.fast_sources.clear()
        existing = {item.obj for item in st.fast_sources if item.obj is not None}
        for o in selected:
            if o not in existing:
                st.fast_sources.add().obj = o
                existing.add(o)
        if not self.append:
            _fast_prepare_cursor(context.scene, selected)
        dg = context.evaluated_depsgraph_get()
        _fast_refresh_shape_summary(context.scene, dg)
        self.report({'INFO'}, f'Fast: {len(existing)} source(s)')
        return {'FINISHED'}


class TU_OT_fast_start(bpy.types.Operator):
    bl_idname='tumblify.fast_start'; bl_label='Start Fast Drop'; bl_options={'REGISTER'}
    def execute(self,context):
        # Pivot orientation is a per-run Fast tool state. Start every new Fast Drop
        # from the neutral orientation instead of reusing the previous simulation.
        _fast_reset_emitter_rotation(context.scene)
        # Validate before invoking the nested modal operator.  If the child
        # operator reports ERROR while being invoked from another operator,
        # Blender can surface that as a Python traceback in the UI.  Fast Drop
        # should fail as a normal user-facing validation message instead.
        if not _fast_source_objects(context.scene):
            self.report({'ERROR'},'Add at least one Source object first')
            return {'CANCELLED'}
        st=context.scene.tumblify
        if st.fast_static_scope=='SCENE' and _validate_scene_collisions(context.scene) is None:
            self.report({'WARNING'},'Click Prepare Scene before starting with Scene colliders')
            return {'CANCELLED'}
        # Keep the normal Fast Drop path completely separate from the direct
        # Interactive entry point. Passing the hidden property explicitly also
        # avoids Blender reusing the last operator-property value from Redo.
        try:
            return bpy.ops.tumblify.run('INVOKE_DEFAULT',start_interactive=False,force_fast=True)
        except RuntimeError as exc:
            self.report({'ERROR'},str(exc).replace('Error: ','').strip())
            return {'CANCELLED'}

def _advanced_spawn_ready(scene):
    if not _scene_role_objects(scene,'SOURCE'): return False
    if scene.tumblify.spawn_location=='SURFACE':
        emitters=_configured_emitter_objects(scene)
        return bool(emitters) and all(o.type=='MESH' for o in emitters)
    return True

class TU_OT_advanced_start(bpy.types.Operator):
    bl_idname='tumblify.advanced_start'; bl_label='Start Advanced Drop'; bl_options={'REGISTER'}
    bl_description='Start Advanced Drop with an explicit clean runtime route'
    @classmethod
    def poll(cls,context):
        if _RUNTIME is not None:
            cls.poll_message_set('Stop the current simulation first'); return False
        if not _scene_role_objects(context.scene,'SOURCE'):
            cls.poll_message_set('Add an object to Dynamic first'); return False
        if not _advanced_spawn_ready(context.scene):
            cls.poll_message_set('Choose a Mesh emitter for Surface spawning'); return False
        return True
    def execute(self,context):
        # Never call TU_OT_run from Advanced UI without explicitly setting its
        # hidden routing properties. Blender may preserve the last values of
        # operator properties (for example after Fast Interactive), which can
        # otherwise leak start_interactive/force_fast into a later Advanced run.
        st=context.scene.tumblify
        st.mode='DROP'
        if not _scene_role_objects(context.scene,'SOURCE'):
            self.report({'WARNING'},'Add an object to Dynamic first'); return {'CANCELLED'}
        try:
            return bpy.ops.tumblify.run('INVOKE_DEFAULT',start_interactive=False,force_fast=False)
        except RuntimeError as exc:
            self.report({'WARNING'},str(exc).removeprefix('Error: ').strip()); return {'CANCELLED'}

class TU_OT_fast_interactive_start(bpy.types.Operator):
    bl_idname='tumblify.fast_interactive_start'; bl_label='Interactive Selected'; bl_options={'REGISTER'}
    def execute(self,context):
        selected=[o for o in context.selected_objects if o.type=='MESH' and not o.get('Tumblify_Helper')]
        if not selected:
            self.report({'ERROR'},'Select at least one Mesh object'); return {'CANCELLED'}
        # A surface/concave Mesh collider must NEVER be passed as a dynamic
        # Fast Interactive source: the native solver cannot move triangle meshes.
        # Previous builds silently replaced it with Auto Detect (usually BOX),
        # closing open containers. Require results only when such colliders are
        # selected; preserve their static collision when the simulation starts.
        colliders=[o for o in selected if not o.get(FROZEN_PROP) and not o.get(INSTANCE_RESULT_PROP,False) and (str(o.tumblify.shape) in {'MESH','TERRAIN'} or str(o.get(FAST_STATIC_SHAPE_PROP,'')) in {'MESH','TERRAIN'})]
        if colliders:
            self.report({'ERROR'},'Mesh container is a static collider; select the spawned objects instead. Use Shift+E during Fast Drop to interact with the pile.')
            return {'CANCELLED'}
        # IMPORTANT: never realize Combined / Offline instance results here.  The run
        # operator imports their stored point/rotation/scale data directly into native
        # Box3D bodies and keeps the viewport representation batched.  Realizing first
        # defeats Batched Interactive and can freeze Blender on large offline piles.
        return bpy.ops.tumblify.run('INVOKE_DEFAULT',start_interactive=True,force_fast=True)

class TU_OT_fast_set_collision(bpy.types.Operator):
    bl_idname='tumblify.fast_set_collision'; bl_label='Set Fast Collision'; bl_options={'INTERNAL'}
    source_name: StringProperty(options={'HIDDEN'})
    shape: EnumProperty(items=SHAPES,default='DETECT')
    def execute(self,context):
        global _RUNTIME
        src=bpy.data.objects.get(self.source_name)
        rt=_RUNTIME
        if src is None or not _is_live_object(src):
            self.report({'WARNING'},'Fast source is no longer available'); return {'CANCELLED'}
        if rt is None or not bool(getattr(rt,'fast_runtime',False)):
            global _COLLISION_DEBUG_TARGETED_EDIT
            st=context.scene.tumblify
            active=getattr(context.view_layer.objects,'active',None)
            active_was_debug=bool(active is not None and str(active.get('Tumblify_DebugSource',''))==src.name) if hasattr(active,'get') else False
            is_source=src in _fast_source_objects(context.scene)
            is_static=(not is_source) and _fast_is_static(context.scene,src)
            if not is_source and not is_static:
                self.report({'WARNING'},'Object is not a Fast source or an active static collider')
                return {'CANCELLED'}
            if is_source and self.shape in {'MESH','TERRAIN'}:
                self.report({'WARNING'},'Mesh collision is static-only')
                return {'CANCELLED'}
            try:
                _COLLISION_DEBUG_TARGETED_EDIT=True
                if is_source:
                    src.tumblify.shape=self.shape
                else:
                    src[FAST_STATIC_SHAPE_PROP]=self.shape
            finally:
                _COLLISION_DEBUG_TARGETED_EDIT=False

            # Rebuild ONLY this object's preview.  Do not touch the other Fast
            # sources: on production sets this turns a multi-second global
            # refresh into an immediate local edit.
            if bool(getattr(st,'show_collision_debug',False)) and getattr(st,'mode','DROP')=='FAST':
                try:
                    _remove_collision_debug_for_source(src.name)
                    dg=context.evaluated_depsgraph_get()
                    if is_source:
                        if self.shape=='DETECT':
                            fit=_cache_auto_collision_result(src,dg,True)
                        else:
                            fit=_fast_collision_fit(src,dg)
                    else:
                        fit=_fast_static_collision_fit(src,dg)
                    dbg=create_collision_debug(context.scene,src,dg,shape_override=fit)
                    if dbg is not None and active_was_debug:
                        try:
                            for o in context.selected_objects:
                                if o.name.startswith('Tumblify_Collision_'): o.select_set(False)
                            dbg.select_set(True); context.view_layer.objects.active=dbg
                        except Exception:
                            pass
                except Exception as exc:
                    self.report({'WARNING'},f'Collision preview refresh failed: {exc}')
            _tag_view3d_redraw_all()
            # The overlay closes before the popup is opened. Restart it only
            # after the targeted preview has been rebuilt.
            if bool(getattr(st,'show_collision_debug',False)) and getattr(st,'mode','DROP')=='FAST':
                try: bpy.ops.tumblify.fast_collision_overlay('INVOKE_DEFAULT')
                except Exception: pass
            return {'FINISHED'}
        try:
            rt._fast_set_source_collision(context,src,self.shape)
        except Exception as e:
            self.report({'ERROR'},f'Could not change Fast collision: {e}'); return {'CANCELLED'}
        return {'FINISHED'}

def _collision_target_from_object(scene, obj):
    """Resolve any selected mesh to a collision-editable source object.

    Collision editing is intentionally independent from Fast/Advanced role lists and collider selection capture.
    Debug proxies are unselectable, but legacy selected proxies are still resolved to their source.
    """
    if obj is None:
        return None
    debug_source = str(obj.get('Tumblify_DebugSource', '')) if hasattr(obj, 'get') else ''
    if debug_source:
        real = bpy.data.objects.get(debug_source)
        if real is not None and _is_live_object(real) and (real.type == 'MESH'):
            obj = real
    if not _is_live_object(obj) or getattr(obj, 'type', None) != 'MESH':
        return None
    try:
        if obj.get('Tumblify_Helper'):
            return None
    except Exception:
        pass
    if obj.get('Tumblify_BatchPreview') or obj.get(INSTANCE_RESULT_PROP, False):
        return None
    return obj


def _collision_menu_targets(context,anchor):
    """Return selected collision targets when anchor participates in selection; else anchor only."""
    anchor=_collision_target_from_object(context.scene,anchor)
    if anchor is None:
        return []
    selected=[]
    seen=set()
    for obj in context.selected_objects:
        target=_collision_target_from_object(context.scene,obj)
        if target is not None and target.name not in seen:
            seen.add(target.name); selected.append(target)
    if any(o.name==anchor.name for o in selected) and len(selected)>1:
        return selected
    return [anchor]


def _collision_target_is_source(scene,obj):
    st=scene.tumblify
    if getattr(st,'mode','DROP')=='FAST':
        return obj in _fast_source_objects(scene)
    return str(getattr(getattr(obj,'tumblify',None),'role','NONE'))=='SOURCE'


def _collision_target_shape(scene,obj):
    if getattr(scene.tumblify,'mode','DROP')=='FAST' and not _collision_target_is_source(scene,obj):
        return _fast_static_collision_shape(obj)
    return str(getattr(getattr(obj,'tumblify',None),'shape','DETECT'))


def _collision_targets_summary(scene,targets):
    shapes={_collision_target_shape(scene,o) for o in targets}
    return next(iter(shapes)) if len(shapes)==1 else 'MIXED'


class TU_OT_viewport_set_collision(bpy.types.Operator):
    bl_idname='tumblify.viewport_set_collision'; bl_label='Set Collision'; bl_options={'INTERNAL'}
    source_name: StringProperty(options={'HIDDEN'})
    shape: EnumProperty(items=SHAPES,default='DETECT')
    def execute(self,context):
        anchor=bpy.data.objects.get(self.source_name)
        if anchor is None or not _is_live_object(anchor):
            self.report({'WARNING'},'Collision source is no longer available'); return {'CANCELLED'}
        targets=_collision_menu_targets(context,anchor)
        if not targets:
            self.report({'WARNING'},'No valid selected collision objects'); return {'CANCELLED'}
        st=context.scene.tumblify
        fast=(getattr(st,'mode','DROP')=='FAST')
        # Mesh is static-only. The popup normally hides it for a mixed source/static
        # selection, but keep this guard for scripted/operator calls too.
        if self.shape in {'MESH','TERRAIN'} and any(_collision_target_is_source(context.scene,o) for o in targets):
            self.report({'WARNING'},'Mesh collision is static-only and cannot be assigned to the selected source objects')
            return {'CANCELLED'}
        dg=context.evaluated_depsgraph_get()
        changed=0
        for src in targets:
            try:
                if fast:
                    is_source=_collision_target_is_source(context.scene,src)
                    global _COLLISION_DEBUG_TARGETED_EDIT
                    _COLLISION_DEBUG_TARGETED_EDIT=True
                    try:
                        # Keep the generic object setting and Fast static override in sync.
                        # This makes collision editing meaningful even before an object is captured
                        src.tumblify.shape=self.shape
                        if not is_source:
                            src[FAST_STATIC_SHAPE_PROP]=self.shape
                    finally:
                        _COLLISION_DEBUG_TARGETED_EDIT=False
                    if self.shape=='DETECT':
                        fit=_cache_auto_collision_result(src,dg,True)
                    else:
                        fit=_fast_collision_fit_for_shape(src,dg,self.shape)
                else:
                    src.tumblify.shape=self.shape
                    if self.shape=='DETECT':
                        fit=_cache_auto_collision_result(src,dg,True)
                    else:
                        fit=None
                        for key in (AUTO_DETECTED_SHAPE_PROP,AUTO_DETECTED_AXIS_PROP):
                            try:
                                if key in src: del src[key]
                            except Exception: pass
                if bool(getattr(st,'show_collision_debug',False)):
                    _remove_collision_debug_for_source(src.name)
                    create_collision_debug(context.scene,src,dg,shape_override=fit)
                changed+=1
            except Exception as exc:
                self.report({'WARNING'},f'{src.name}: collision update failed: {exc}')
        _release_collision_popup()
        _tag_view3d_redraw_all()
        _ensure_collision_overlay(context.scene)
        if changed>1:
            self.report({'INFO'},f'Collision set to {self.shape.replace("_"," ").title()} for {changed} selected objects')
        return {'FINISHED'} if changed else {'CANCELLED'}

def _collision_popup_spec(context,src):
    targets=_collision_menu_targets(context,src)
    current=_collision_targets_summary(context.scene,targets) if targets else 'DETECT'
    dynamic=any(_collision_target_is_source(context.scene,o) for o in targets)
    labels=[(item[0],item[1]) for item in SHAPES if not (dynamic and item[0] in {'MESH','TERRAIN'})]
    return targets,current,labels


class TU_OT_fast_set_rotation_axes(bpy.types.Operator):
    bl_idname='tumblify.fast_set_rotation_axes'; bl_label='Set Fast Rotation Axes'; bl_options={'INTERNAL'}
    axes: StringProperty(name='Axes',default='XYZ',options={'HIDDEN'})

    def execute(self,context):
        global _RUNTIME
        st=context.scene.tumblify
        axes=set(str(self.axes or '').upper())
        st.fast_random_rot_x='X' in axes
        st.fast_random_rot_y='Y' in axes
        st.fast_random_rot_z='Z' in axes
        label=''.join(a if a in axes else '–' for a in 'XYZ')
        # Update the held Fast preview immediately without realizing or restarting
        # the simulation. The active modal runtime remains the owner of the preview.
        rt=_RUNTIME
        if rt is not None and bool(getattr(rt,'fast_runtime',False)) and not bool(getattr(rt,'live_interactive',False)):
            try:
                rt._fast_preview_create(context,getattr(rt,'fast_preview_source',None),getattr(rt,'fast_preview_scale',1.0),False)
            except Exception:
                pass
        _tag_view3d_redraw_all()
        self.report({'INFO'},f'Fast random rotation axes: {label}')
        return {'FINISHED'}


class TU_OT_fast_rotation_axes_popup(bpy.types.Operator):
    bl_idname='tumblify.fast_rotation_axes_popup'; bl_label='Random Rotation Axes'; bl_options={'INTERNAL'}
    def invoke(self,context,event):
        return context.window_manager.invoke_popup(self,width=260)
    def execute(self,context): return {'FINISHED'}
    def draw(self,context):
        st=context.scene.tumblify
        fast=bool(getattr(_RUNTIME,'fast_runtime',False)) if _RUNTIME is not None else st.mode=='FAST'
        self.layout.label(text='Random Rotation Axes',icon='ORIENTATION_GIMBAL')
        row=self.layout.row(align=True); row.scale_y=1.4
        prefix='fast_random_rot_' if fast else 'random_rot_'
        for axis in 'XYZ': row.prop(st,prefix+axis.lower(),text=axis,toggle=True)


class TU_MT_fast_rotation_axes_pie(bpy.types.Menu):
    bl_idname='TU_MT_fast_rotation_axes_pie'; bl_label='Random Rotation Axes'

    def draw(self,context):
        pie=self.layout.menu_pie()
        st=context.scene.tumblify
        current=''.join(a for a in 'XYZ' if bool(getattr(st,f'fast_random_rot_{a.lower()}',True)))

        # Pie order follows Blender's radial slots: W, E, S, N, NW, NE, SW, SE.
        entries=(
            ('X','X'), ('Y','Y'), ('Z','Z'), ('XYZ','XYZ'),
            ('XY','XY'), ('XZ','XZ'), ('YZ','YZ'), ('','None'),
        )
        for axes,label in entries:
            op=pie.operator(
                'tumblify.fast_set_rotation_axes',
                text=label,
                icon='CHECKMARK' if current==axes else 'NONE',
            )
            op.axes=axes


class TU_MT_fast_collision_popup(bpy.types.Menu):
    bl_idname='TU_MT_fast_collision_popup'; bl_label='Collision'
    def draw(self,context):
        layout=self.layout
        src_name=str(getattr(context.scene.tumblify,'fast_collision_menu_source','') or '')
        src=bpy.data.objects.get(src_name)
        targets,current,labels=_collision_popup_spec(context,src) if src is not None else ([], 'DETECT', [])
        if len(targets)>1:
            row=layout.row(); row.enabled=False
            row.label(text=f'{len(targets)} selected · ' + ('Mixed' if current=='MIXED' else SHAPE_LABELS.get(current,current)))
            layout.separator()
        for shape,label in labels:
            op=layout.operator('tumblify.viewport_set_collision',text=label,icon=SHAPE_ICONS[shape],depress=current==shape)
            op.source_name=src_name; op.shape=shape


def _release_collision_popup(owner=None):
    global _COLLISION_POPUP_LOCKED, _COLLISION_POPUP_OWNER
    if owner is not None and _COLLISION_POPUP_OWNER is not owner: return False
    _COLLISION_POPUP_LOCKED=False; _COLLISION_POPUP_OWNER=None
    return True


def _show_fast_collision_popup(context,src_name):
    """Open the viewport collision popup for Fast and Advanced, including multi-selection."""
    src=bpy.data.objects.get(src_name)
    if src is None or not _is_live_object(src):
        return False
    targets,current,labels=_collision_popup_spec(context,src)
    if not targets:
        return False
    count=len(targets)
    title='Collision'
    if count>1:
        title=f'Collision · {count} selected · ' + ('Mixed' if current=='MIXED' else SHAPE_LABELS.get(current,current))
    def _draw_popup(menu,ctx):
        layout=menu.layout
        if count>1:
            row=layout.row(); row.enabled=False
            row.label(text='Mixed collision types' if current=='MIXED' else f'Current: {SHAPE_LABELS.get(current,current)}')
            layout.separator()
        for shape,label in labels:
            op=layout.operator('tumblify.viewport_set_collision',text=label,icon=SHAPE_ICONS[shape],depress=current==shape)
            op.source_name=src_name; op.shape=shape
    global _COLLISION_POPUP_LOCKED, _COLLISION_POPUP_OWNER
    token=object(); _COLLISION_POPUP_OWNER=token; _COLLISION_POPUP_LOCKED=True
    scene_name=context.scene.name
    def _cancel_fallback():
        if _release_collision_popup(token):
            scene=bpy.data.scenes.get(scene_name)
            if scene is not None:
                try: _ensure_collision_overlay(scene)
                except Exception: pass
        return None
    try:
        context.window_manager.popup_menu(_draw_popup,title=title,icon='MOD_PHYSICS')
        _register_owned_app_timer(_cancel_fallback,first_interval=8.0)
    except Exception:
        _release_collision_popup(token)
        raise

    return True



class TU_OT_fast_capture_statics(bpy.types.Operator):
    """Capture the scene mesh objects selected as Fast static colliders."""
    bl_idname = 'tumblify.fast_capture_statics'
    bl_label = 'Use Selected Colliders'
    bl_options = {'REGISTER', 'UNDO'}

    append: BoolProperty(name='Add to List', description='Add selected objects without replacing the current list', default=False, options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        return 'Add selected mesh objects to the current list; existing entries are kept and duplicates are skipped' if properties.append else 'Replace the list with selected mesh objects'

    def execute(self, context):
        st = context.scene.tumblify
        sources = set(_fast_source_objects(context.scene))
        candidates = [o for o in context.selected_objects if o.type == 'MESH' and o not in sources and (not o.get('Tumblify_Helper')) and (not o.get(RUNTIME_PROP))]
        if not self.append:
            st.fast_selected_statics.clear()
        existing = {item.obj for item in st.fast_selected_statics if item.obj is not None}
        for o in candidates:
            if o not in existing:
                st.fast_selected_statics.add().obj = o
                existing.add(o)
        if candidates and st.fast_static_scope == 'SELECTED':
            pass
        _request_collision_debug_refresh(context.scene)
        self.report({'INFO'}, f'Fast: {len(existing)} static colliders')
        return {'FINISHED'}


class TU_OT_fast_collision_overlay(bpy.types.Operator):
    """Clickable collision labels for visible collision proxies in Fast and Advanced."""
    bl_idname='tumblify.fast_collision_overlay'; bl_label='Fast Collision Overlay'; bl_options={'INTERNAL'}

    def _finish(self,context):
        global _FAST_COLLISION_OVERLAY_RUNNING, _COLLISION_OVERLAY_OWNER
        self._tumblify_shutdown=True
        try:
            if getattr(self,'_draw_handle',None) is not None:
                bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle,'WINDOW')
        except Exception:
            pass
        self._draw_handle=None
        self._hitboxes=[]
        if _COLLISION_OVERLAY_OWNER is self:
            _COLLISION_OVERLAY_OWNER=None
            _FAST_COLLISION_OVERLAY_RUNNING=False
        try:
            if context.area: context.area.tag_redraw()
        except Exception:
            pass

    def cancel(self,context):
        self._finish(context)

    def _active(self,context):
        st=context.scene.tumblify
        return (
            bool(getattr(st,'show_collision_debug',False))
            and getattr(st,'mode','DROP') in {'FAST','DROP'}
            and _RUNTIME is None
        )

    def _draw(self):
        self._hitboxes=[]
        context=bpy.context
        try:
            if not self._active(context) or _COLLISION_POPUP_LOCKED:
                return
            region=getattr(self,'_region',None); rv3d=getattr(self,'_rv3d',None)
            if region is None or rv3d is None:
                return
            dg=context.evaluated_depsgraph_get()
            font=0
            blf.size(font,13.0)

            active=context.view_layer.objects.active
            active_source=_collision_target_from_object(context.scene,active)
            sources_to_draw=[active_source] if active_source is not None and active_source.select_get() else []
            fast=(getattr(context.scene.tumblify,'mode','DROP')=='FAST')
            fast_sources={o.name:o for o in _fast_source_objects(context.scene) if _is_live_object(o)} if fast else {}

            for src in sources_to_draw:
                if not _is_live_object(src) or src.type!='MESH':
                    continue
                # Project all eight world-space bounding-box corners and place
                # the editor at the TRUE top-right of the object's screen-space
                # bounding box.  This remains intuitive from any camera angle.
                projected=[]
                try:
                    eo=src.evaluated_get(dg)
                    mw=eo.matrix_world
                    for c in eo.bound_box:
                        p=view3d_utils.location_3d_to_region_2d(region,rv3d,mw @ Vector(c))
                        if p is not None:
                            projected.append(p)
                except Exception:
                    projected=[]
                if not projected:
                    p=view3d_utils.location_3d_to_region_2d(region,rv3d,src.matrix_world.translation)
                    if p is None:
                        continue
                    projected=[p]
                minx=min(p.x for p in projected); maxx=max(p.x for p in projected)
                miny=min(p.y for p in projected); maxy=max(p.y for p in projected)
                centre2=Vector(((minx+maxx)*0.5,(miny+maxy)*0.5))
                corner2=Vector((maxx,maxy))
                # Keep the familiar top-right placement, but move it 10% toward the object's centre.
                p2=corner2.lerp(centre2,0.10)
                raw=_collision_target_shape(context.scene,src)
                if raw=='DETECT':
                    resolved=_cached_auto_shape(src) or 'HULL'
                    label='Auto Detect -> '+SHAPE_LABELS.get(resolved,resolved)
                else:
                    label=SHAPE_LABELS.get(raw,raw)
                text=f'{label}  ▾'
                tw,th=blf.dimensions(font,text)
                x=float(p2.x)+8.0
                y=float(p2.y)+8.0
                # A compact dark plate keeps labels readable on arbitrary scenes.
                # Only the text itself is drawn; the expanded hitbox makes it easy
                # to click without introducing a permanent Blender gizmo/object.
                blf.position(font,x,y,0)
                blf.color(font,1.0,0.72,0.22,1.0)
                blf.draw(font,text)
                self._hitboxes.append((x-7.0,y-6.0,x+float(tw)+7.0,y+float(th)+6.0,src.name))
        except Exception:
            self._hitboxes=[]

    def invoke(self,context,event):
        global _FAST_COLLISION_OVERLAY_RUNNING, _COLLISION_OVERLAY_OWNER
        if _FAST_COLLISION_OVERLAY_RUNNING:
            return {'CANCELLED'}
        if context.area is None or context.area.type!='VIEW_3D':
            return {'CANCELLED'}
        if not self._active(context):
            return {'CANCELLED'}
        region=next((r for r in context.area.regions if r.type=='WINDOW'),None)
        rv3d=getattr(context.space_data,'region_3d',None)
        if region is None or rv3d is None:
            return {'CANCELLED'}
        self._region=region; self._rv3d=rv3d; self._hitboxes=[]; self._pending_source=''
        self._selection_signature=()
        self._draw_handle=bpy.types.SpaceView3D.draw_handler_add(self._draw,(), 'WINDOW','POST_PIXEL')
        _FAST_COLLISION_OVERLAY_RUNNING=True
        _COLLISION_OVERLAY_OWNER=self; self._tumblify_shutdown=False
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self,context,event):
        if _runtime_owner_shutdown(self): return {'CANCELLED'}
        if not self._active(context):
            self._finish(context)
            return {'FINISHED'}

        # Do NOT open Blender's popup on LEFTMOUSE PRESS.  A popup created from
        # the press still receives the release of the same physical click and
        # can become effectively non-interactive.  Capture the label on press,
        # consume that event, and only open the popup after the matching release.
        if event.type=='LEFTMOUSE' and event.value=='PRESS':
            mx=float(event.mouse_region_x); my=float(event.mouse_region_y)
            self._pending_source=''
            for x0,y0,x1,y1,name in list(getattr(self,'_hitboxes',[])):
                if x0 <= mx <= x1 and y0 <= my <= y1:
                    src=bpy.data.objects.get(name)
                    if src is not None and _is_live_object(src):
                        self._pending_source=src.name
                        return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        if event.type=='LEFTMOUSE' and event.value=='RELEASE' and getattr(self,'_pending_source',''):
            name=self._pending_source
            self._pending_source=''
            src=bpy.data.objects.get(name)
            if src is None or not _is_live_object(src):
                return {'RUNNING_MODAL'}
            context.scene.tumblify.fast_collision_menu_source=src.name
            # The opening click is now fully complete. Remove our modal handler
            # first, then invoke Blender's native menu so it exclusively owns
            # all subsequent mouse events.
            self._finish(context)
            try:
                _show_fast_collision_popup(context,src.name)
            except Exception as exc:
                self.report({'WARNING'},f'Could not open collision menu: {exc}')
            return {'FINISHED'}

        if event.type in {'MOUSEMOVE','WHEELUPMOUSE','WHEELDOWNMOUSE','MIDDLEMOUSE'}:
            try:
                if context.area: context.area.tag_redraw()
            except Exception:
                pass
        return {'PASS_THROUGH'}

def _ensure_collision_overlay(scene):
    """Keep the viewport collision editor alive even after a popup is cancelled."""
    global _FAST_COLLISION_OVERLAY_RUNNING
    if scene is None or _FAST_COLLISION_OVERLAY_RUNNING or _COLLISION_POPUP_LOCKED or _RUNTIME is not None:
        return
    st=getattr(scene,'tumblify',None)
    if st is None or not bool(getattr(st,'show_collision_debug',False)) or getattr(st,'mode','DROP') not in {'FAST','DROP'}:
        return
    try:
        wm=bpy.context.window_manager
        for win in wm.windows:
            scr=getattr(win,'screen',None)
            if scr is None: continue
            for area in scr.areas:
                if area.type!='VIEW_3D': continue
                region=next((r for r in area.regions if r.type=='WINDOW'),None)
                if region is None: continue
                with bpy.context.temp_override(window=win,area=area,region=region):
                    bpy.ops.tumblify.fast_collision_overlay('INVOKE_DEFAULT')
                return
    except Exception:
        pass

def _weighted_source_choice(sources):
    if not sources: return None
    weights=[max(0.0,float(getattr(o.tumblify,'spawn_weight',100.0))) for o in sources]
    total=sum(weights)
    if total<=1.0e-12: return random.choice(sources)
    r=random.random()*total; acc=0.0
    for o,w in zip(sources,weights):
        acc+=w
        if r<=acc: return o
    return sources[-1]

def _scene_role_objects(scene,role):
    """Return the explicit Advanced simulation list only.

    The UI lists are the authoritative source of truth.  Older builds also scanned
    every scene object for a stale ``role`` tag as a runtime compatibility fallback.
    That made removed Dynamic Sources silently reappear in later runs.  Legacy files
    should be migrated explicitly on load instead of changing the runtime meaning of
    an empty list.
    """
    coll=scene.tumblify.static_objects if role=='STATIC' else scene.tumblify.dynamic_sources
    result=[]
    for item in coll:
        o=item.obj
        if _is_live_object(o) and o.name in scene.objects and o.type=='MESH' and o.tumblify.role==role and o not in result:
            result.append(o)
    if role=='STATIC':
        return _static_collider_candidates(scene,result,include_hidden=True)
    return result

def _frozen_spawned(scene):
    return [o for o in _spawned_objects(scene) if o.type=='MESH' and not bool(o.get(INSTANCE_RESULT_PROP,False)) and bool(o.get(FROZEN_PROP,False)) and not bool(o.get(FIXED_PROP,False)) and not o.hide_get()]


def _frozen_physics_results(scene):
    """User-facing result operations include both Separated and Combined output."""
    return list(_frozen_spawned(scene))+list(_frozen_instance_results(scene))

def _ref_list_info(settings, kind):
    mapping={
        'STATIC':('static_objects','static_index','static_anchor'),
        'DYNAMIC':('dynamic_sources','dynamic_index','dynamic_anchor'),
    }
    cattr,iattr,aattr=mapping[kind]
    return getattr(settings,cattr),iattr,aattr


_LIST_SELECTION_SYNCING=False
_DROP_SELECTION_SIGNATURE=None

def _sync_ref_lists_from_viewport(scene=None,kinds=('STATIC','DYNAMIC')):
    """Mirror viewport selection into selected Tumblify lists.

    This is intentionally limited to Physics Drop to avoid unnecessary background list scans.
    """
    global _LIST_SELECTION_SYNCING
    if _LIST_SELECTION_SYNCING: return
    scene=scene or getattr(bpy.context,'scene',None)
    if scene is None or not hasattr(scene,'tumblify'): return
    try:
        selected={o.as_pointer() for o in bpy.context.selected_objects if _is_live_object(o)}
        active=getattr(bpy.context.view_layer.objects,'active',None) if getattr(bpy.context,'view_layer',None) else None
        ap=active.as_pointer() if _is_live_object(active) else None
    except Exception:
        return
    try:
        _LIST_SELECTION_SYNCING=True
        st=scene.tumblify
        for kind in kinds:
            coll,active_attr,anchor_attr=_ref_list_info(st,kind)
            active_i=None
            for i,item in enumerate(coll):
                o=item.obj
                val=bool(_is_live_object(o) and o.as_pointer() in selected)
                if bool(item.selected)!=val: item.selected=val
                if ap is not None and _is_live_object(o) and o.as_pointer()==ap: active_i=i
            if active_i is not None:
                setattr(st,active_attr,active_i); setattr(st,anchor_attr,active_i)
    finally:
        _LIST_SELECTION_SYNCING=False

def _sync_viewport_from_ref_list(context,kind,clicked_index=None,replace=False):
    """Mirror a Physics Drop UI-list selection into Blender's object selection.

    Only Physics Drop participates in bidirectional list synchronization.
    """
    global _LIST_SELECTION_SYNCING
    if _LIST_SELECTION_SYNCING or kind not in {'STATIC','DYNAMIC'}: return
    st=context.scene.tumblify; coll,active_attr,_=_ref_list_info(st,kind)
    try:
        _LIST_SELECTION_SYNCING=True
        if replace:
            for o in list(context.selected_objects):
                try: o.select_set(False)
                except Exception: pass
        for item in coll:
            o=item.obj
            if not _is_live_object(o): continue
            try: o.select_set(bool(item.selected))
            except Exception: pass
        idx=int(getattr(st,active_attr,clicked_index if clicked_index is not None else 0))
        if 0<=idx<len(coll) and _is_live_object(coll[idx].obj):
            try: context.view_layer.objects.active=coll[idx].obj
            except Exception: pass
    finally:
        _LIST_SELECTION_SYNCING=False

def _tumblify_selection_sync_timer():
    global _DROP_SELECTION_SIGNATURE, _COLLISION_PREVIEW_MEMBERSHIP
    try:
        scene = getattr(bpy.context, 'scene', None)
        if scene is not None and hasattr(scene,'tumblify') and scene.tumblify.show_collision_debug:
            if scene.tumblify.mode=='FAST' and scene.tumblify.fast_static_scope=='SCENE':
                rec=_get_scene_cache(scene)
                signature=(scene.as_pointer(),'PREPARED_SCENE',id(rec),id(_RUNTIME),len(getattr(_RUNTIME,'dynamic_objs',[])),tuple(o.as_pointer() for o in _fast_source_objects(scene)),
                           tuple(o.as_pointer() for o in _frozen_spawned(scene)),tuple(o.as_pointer() for o in _frozen_instance_results(scene)))
            else:
                signature=(scene.as_pointer(),tuple(o.as_pointer() for o in _collision_debug_targets(scene)))
            if signature!=_COLLISION_PREVIEW_MEMBERSHIP:
                _COLLISION_PREVIEW_MEMBERSHIP=signature
                _request_collision_debug_refresh(scene)
        else:
            _COLLISION_PREVIEW_MEMBERSHIP=None
        if scene is None or not hasattr(scene, 'tumblify') or scene.tumblify.mode != 'DROP':
            _DROP_SELECTION_SIGNATURE = None
            return 0.35
        if bool(getattr(scene.tumblify, 'advanced_emitter_ready', False)):
            emitters = _advanced_emitter_objects(scene)
            if not emitters:
                scene.tumblify.advanced_emitter_ready = False
                try:
                    _advanced_remove_legacy_demo_source(scene)
                except Exception:
                    pass
                if not getattr(scene.tumblify, 'emitter_object', None) and (not getattr(scene.tumblify, 'emitter_collection', None)):
                    try:
                        _advanced_remove_addon_emitter(scene)
                    except Exception:
                        pass
                _tag_view3d_redraw_all()
        selected = tuple(sorted((o.as_pointer() for o in bpy.context.selected_objects if _is_live_object(o))))
        active = getattr(bpy.context.view_layer.objects, 'active', None) if getattr(bpy.context, 'view_layer', None) else None
        ap = active.as_pointer() if _is_live_object(active) else 0
        sig = (selected, ap)
        if sig != _DROP_SELECTION_SIGNATURE:
            _DROP_SELECTION_SIGNATURE = sig
            _sync_ref_lists_from_viewport(scene, ('STATIC', 'DYNAMIC'))
        if bool(getattr(scene.tumblify, 'show_collision_debug', False)):
            _ensure_collision_overlay(scene)
    except Exception:
        pass
    return 0.2

class TU_UL_object_refs(bpy.types.UIList):
    def draw_item(self,context,layout,data,item,icon,active_data,active_propname,index):
        o=item.obj
        if not o:
            layout.label(text='<missing>',icon='ERROR')
            return
        kind={'static_index':'STATIC','dynamic_index':'DYNAMIC'}.get(active_propname,'DYNAMIC')
        split=layout.split(factor=(0.70 if kind=='DYNAMIC' else 0.88),align=True)
        # Standard-list look: plain text until explicitly selected. The template_list itself
        # still supplies the normal active-row highlight; Ctrl/Shift are handled by this operator.
        op=split.operator('tumblify.list_select',text=o.name,emboss=bool(item.selected),depress=bool(item.selected))
        op.list_kind=kind; op.index=index
        right=split.row(align=True)
        if kind=='DYNAMIC': right.prop(o.tumblify,'spawn_weight',text='',slider=False)
        display_shape=(_cached_auto_shape(o) or 'DETECT') if o.tumblify.shape=='DETECT' else o.tumblify.shape
        icon_name=SHAPE_ICONS.get(display_shape,'MESH_DATA')
        op=right.operator('tumblify.object_proxy_menu',text='',icon=icon_name,emboss=False)
        op.list_kind=kind; op.index=index

class TU_OT_list_select(bpy.types.Operator):
    bl_idname='tumblify.list_select'; bl_label='Select List Item'; bl_options={'INTERNAL'}
    list_kind: EnumProperty(items=[('STATIC','Static',''),('DYNAMIC','Dynamic','')])
    index: IntProperty(default=0)
    def invoke(self,context,event):
        s=context.scene.tumblify
        coll,active_attr,anchor_attr=_ref_list_info(s,self.list_kind)
        if not (0<=self.index<len(coll)): return {'CANCELLED'}
        anchor=getattr(s,anchor_attr,-1)
        if event.shift and 0<=anchor<len(coll):
            if not event.ctrl:
                for it in coll: it.selected=False
            a,b=sorted((anchor,self.index))
            for i in range(a,b+1): coll[i].selected=True
        elif event.ctrl:
            coll[self.index].selected=not coll[self.index].selected
            setattr(s,anchor_attr,self.index)
        else:
            for it in coll: it.selected=False
            coll[self.index].selected=True
            setattr(s,anchor_attr,self.index)
        setattr(s,active_attr,self.index)
        _sync_viewport_from_ref_list(context,self.list_kind,self.index,replace=not (event.ctrl or event.shift))
        return {'FINISHED'}

class TU_MT_object_proxy(bpy.types.Menu):
    bl_label='Collision Model'; bl_idname='TU_MT_object_proxy'
    def draw(self,context):
        layout=self.layout
        kind=getattr(context.scene,'tumblify_proxy_menu_kind','STATIC')
        index=int(getattr(context.scene,'tumblify_proxy_menu_index',0))
        allowed=SHAPES if kind=='STATIC' else [x for x in SHAPES if x[0] not in {'MESH','TERRAIN'}]
        for item in allowed:
            shape,label,desc=item[:3]
            op=layout.operator('tumblify.set_object_proxy',text=label,icon=SHAPE_ICONS.get(shape,'MESH_DATA'))
            op.list_kind=kind; op.index=index; op.shape=shape

class TU_MT_list_proxy(bpy.types.Menu):
    bl_label='Collision Model'; bl_idname='TU_MT_list_proxy'
    def draw(self,context):
        layout=self.layout
        kind=getattr(context.scene,'tumblify_list_menu_kind','STATIC')
        allowed=SHAPES if kind=='STATIC' else [x for x in SHAPES if x[0] not in {'MESH','TERRAIN'}]
        for item in allowed:
            shape,label,desc=item[:3]
            op=layout.operator('tumblify.set_list_proxy',text=label,icon=SHAPE_ICONS.get(shape,'MESH_DATA'))
            op.list_kind=kind; op.shape=shape

class TU_OT_object_proxy_menu(bpy.types.Operator):
    bl_idname='tumblify.object_proxy_menu'; bl_label='Collision Model'; bl_options={'INTERNAL'}
    list_kind: EnumProperty(items=[('STATIC','Static',''),('DYNAMIC','Dynamic','')])
    index: IntProperty(default=0)
    def execute(self,context):
        context.scene.tumblify_proxy_menu_kind=self.list_kind
        context.scene.tumblify_proxy_menu_index=self.index
        bpy.ops.wm.call_menu(name='TU_MT_object_proxy')
        return {'FINISHED'}

class TU_OT_set_object_proxy(bpy.types.Operator):
    bl_idname='tumblify.set_object_proxy'; bl_label='Set Collision Model'; bl_options={'INTERNAL'}
    list_kind: EnumProperty(items=[('STATIC','Static',''),('DYNAMIC','Dynamic','')])
    index: IntProperty(default=0)
    shape: EnumProperty(items=SHAPES)
    def execute(self,context):
        s=context.scene.tumblify
        coll,iattr,_=_ref_list_info(s,self.list_kind)
        if self.list_kind=='DYNAMIC' and self.shape in {'MESH','TERRAIN'}: return {'CANCELLED'}
        if 0<=self.index<len(coll) and coll[self.index].obj:
            obj=coll[self.index].obj
            obj.tumblify.shape=self.shape
            if self.shape=='DETECT': _cache_auto_collision_result(obj,context.evaluated_depsgraph_get(),True)
            else:
                for key in (AUTO_DETECTED_SHAPE_PROP,AUTO_DETECTED_AXIS_PROP):
                    try:
                        if key in obj: del obj[key]
                    except Exception: pass
            setattr(s,iattr,self.index)
            return {'FINISHED'}
        return {'CANCELLED'}

class TU_MT_list_menu(bpy.types.Menu):
    bl_label='List Actions'; bl_idname='TU_MT_list_menu'
    def draw(self,context):
        layout=self.layout
        kind=getattr(context.scene,'tumblify_list_menu_kind','STATIC') if hasattr(context.scene,'tumblify_list_menu_kind') else 'STATIC'
        layout.menu('TU_MT_list_proxy',text='Collision Model',icon='MOD_PHYSICS')
        layout.separator()
        for action,label,icon in [('SELECT_ALL','Select All','RESTRICT_SELECT_OFF'),('CLEAR','Clear All','TRASH'),('SORT_NAME','Sort by Name','SORTALPHA'),('SORT_SHAPE','Sort by Collision Model','MOD_PHYSICS')]:
            op=layout.operator('tumblify.list_action',text=label,icon=icon); op.list_kind=kind; op.action=action

class TU_OT_mark_static(bpy.types.Operator):
    bl_idname = 'tumblify.mark_static'
    bl_label = 'Add Selected Static'

    def execute(self, context):
        s = context.scene.tumblify
        n = 0
        for o in context.selected_objects:
            if o.type == 'MESH':
                _list_remove_object(s.dynamic_sources, o)
                _list_add_unique(s.static_objects, o)
                o.tumblify.role = 'STATIC'
                n += 1
        self.report({'INFO'}, f'{n} static colliders')
        return {'FINISHED'}

class TU_OT_mark_source(bpy.types.Operator):
    bl_idname = 'tumblify.mark_source'
    bl_label = 'Add Selected Dynamic'

    def execute(self, context):
        s = context.scene.tumblify
        n = 0
        candidates = [o for o in context.selected_objects if o.type == 'MESH' and o.name not in {LEGACY_ADV_DEMO_SOURCE_NAME}]
        if candidates:
            _advanced_remove_legacy_demo_source(context.scene)
        depsgraph = context.evaluated_depsgraph_get() if candidates else None
        for o in candidates:
            _list_remove_object(s.static_objects, o)
            _list_add_unique(s.dynamic_sources, o)
            o.tumblify.role = 'SOURCE'
            if o.tumblify.shape == 'DETECT':
                _cache_auto_collision_result(o, depsgraph, True)
            n += 1
        self.report({'INFO'}, f'{n} dynamic sources')
        return {'FINISHED'}


class TU_OT_remove_role_item(bpy.types.Operator):
    bl_idname='tumblify.remove_role_item'; bl_label='Remove from Simulation'
    list_kind: EnumProperty(items=[('STATIC','Static',''),('DYNAMIC','Dynamic','')])
    def execute(self,context):
        s=context.scene.tumblify; coll,iattr,aattr=_ref_list_info(s,self.list_kind)
        idx=getattr(s,iattr)
        targets=[i for i,item in enumerate(coll) if item.selected]
        if not targets and 0<=idx<len(coll): targets=[idx]
        for i in reversed(targets):
            o=coll[i].obj
            if o and self.list_kind in {'STATIC','DYNAMIC'}: o.tumblify.role='NONE'
            coll.remove(i)
        ni=max(0,min(idx,len(coll)-1))
        setattr(s,iattr,ni); setattr(s,aattr,-1)
        return {'FINISHED'}

class TU_OT_set_emitter_proxy(bpy.types.Operator):
    bl_idname='tumblify.set_emitter_proxy';bl_label='Assign Emitter Collision';bl_options={'REGISTER','UNDO'}
    shape: EnumProperty(items=SHAPES)
    def execute(self,context):
        targets=[obj for obj in _advanced_emitter_objects(context.scene) if obj.type=='MESH']
        if not targets:self.report({'WARNING'},'Choose a Mesh emitter to assign collision');return {'CANCELLED'}
        for obj in targets:obj.tumblify.shape=self.shape
        if context.scene.tumblify.show_collision_debug:_refresh_collision_debug(context.scene)
        self.report({'INFO'},f'Assigned {self.shape} to {len(targets)} emitter(s)');return {'FINISHED'}


class TU_OT_set_list_proxy(bpy.types.Operator):
    bl_idname = 'tumblify.set_list_proxy'
    bl_label = 'Set Collision Proxy'
    bl_options = {'INTERNAL'}
    list_kind: EnumProperty(items=[('STATIC', 'Static', ''), ('DYNAMIC', 'Dynamic', '')])
    shape: EnumProperty(items=SHAPES)

    def execute(self, context):
        s = context.scene.tumblify
        (coll, iattr, _) = _ref_list_info(s, self.list_kind)
        if self.list_kind == 'DYNAMIC' and self.shape in {'MESH', 'TERRAIN'}:
            return {'CANCELLED'}
        idx = getattr(s, iattr)
        targets = [it.obj for it in coll if it.selected]
        if not targets and 0 <= idx < len(coll):
            targets = [coll[idx].obj]
        for o in targets:
            if _collision_target_from_object(context.scene, o) is not None:
                o.tumblify.shape = self.shape
        if s.show_collision_debug:
            _refresh_collision_debug(context.scene)
        return {'FINISHED'}

class TU_OT_list_menu(bpy.types.Operator):
    bl_idname='tumblify.list_menu'; bl_label='List Menu'
    list_kind: EnumProperty(items=[('STATIC','Static',''),('DYNAMIC','Dynamic','')])
    def execute(self,context):
        context.scene.tumblify_list_menu_kind=self.list_kind
        bpy.ops.wm.call_menu(name='TU_MT_list_menu')
        return {'FINISHED'}

class TU_OT_list_action(bpy.types.Operator):
    bl_idname='tumblify.list_action'; bl_label='List Action'
    list_kind: EnumProperty(items=[('STATIC','Static',''),('DYNAMIC','Dynamic','')])
    action: EnumProperty(items=[('SELECT_ALL','Select All',''),('CLEAR','Clear',''),('SORT_NAME','Sort Name',''),('SORT_SHAPE','Sort Shape','')])
    def execute(self,context):
        s=context.scene.tumblify; coll,iattr,aattr=_ref_list_info(s,self.list_kind)
        if self.action=='SELECT_ALL':
            for it in coll: it.selected=True
        elif self.action=='CLEAR':
            for it in coll:
                if it.obj and self.list_kind in {'STATIC','DYNAMIC'}: it.obj.tumblify.role='NONE'
            coll.clear()
            setattr(s,aattr,-1)
        else:
            rows=[(it.obj,bool(it.selected)) for it in coll]
            if self.action=='SORT_NAME': rows.sort(key=lambda x: x[0].name.lower() if x[0] else '')
            else: rows.sort(key=lambda x: ((x[0].tumblify.shape if x[0] else ''),(x[0].name.lower() if x[0] else '')))
            coll.clear()
            for obj,selected in rows:
                it=coll.add(); it.obj=obj; it.selected=selected
            setattr(s,aattr,-1)
        return {'FINISHED'}


class TU_OT_join_compound(bpy.types.Operator):
    bl_idname='tumblify.join_compound'; bl_label='Join Selected as Compound'; bl_options={'REGISTER','UNDO'}
    bl_description='Join selected mesh objects and preserve each object assigned collision proxy as one part of a compound collision body'
    def execute(self,context):
        objs=[o for o in context.selected_objects if o.type=='MESH']
        active=context.view_layer.objects.active
        if active not in objs or len(objs)<2:
            self.report({'WARNING'},'Select at least two mesh objects and make one active'); return {'CANCELLED'}
        deps=context.evaluated_depsgraph_get(); inv=active.matrix_world.inverted_safe(); stored=[]
        # Destructive join invalidates Blender object references. Remove selected source rows first.
        st=context.scene.tumblify
        for coll_name in ('static_objects','dynamic_sources'):
            coll=getattr(st,coll_name,None)
            if coll is not None:
                for i in range(len(coll)-1,-1,-1):
                    try:
                        if coll[i].obj in objs: coll.remove(i)
                    except (ReferenceError,RuntimeError):
                        try: coll.remove(i)
                        except Exception: pass
        _,active_rot,active_sc=active.matrix_world.decompose(); active_rot=active_rot.normalized()
        asc=(abs(float(active_sc.x)),abs(float(active_sc.y)),abs(float(active_sc.z)))
        active_uniform=(max(asc)-min(asc) <= max(max(asc),1.0e-8)*1.0e-4)
        active_uniform_scale=max(1.0e-8,sum(asc)/3.0)
        for o in objs:
            sh=o.tumblify.shape
            if sh in {'MESH','TERRAIN'}:
                data=_prepare_collision(o,deps,context.scene.tumblify.convex_simplify,'HULL')
            else:
                data=_prepare_collision(o,deps,context.scene.tumblify.convex_simplify)
            fallback_parts=_data_as_compound_parts(data)
            native_parts=_native_parts_from_data(data)
            p0,q0,_=world_pose_scale(o); source_rot=Quaternion((q0[3],q0[0],q0[1],q0[2])).normalized()
            if len(native_parts)!=len(fallback_parts):
                native_parts=[{'kind':'HULL'} for _ in fallback_parts]
            for idx,part in enumerate(fallback_parts):
                pts=[]
                for x in part.get('points',[]):
                    wp=Vector(p0)+source_rot@Vector(x); lp=inv@wp; pts.append((lp.x,lp.y,lp.z))
                if len(pts)<4: continue
                rec={'kind':'HULL','points':pts,'faces':part.get('faces',[]),'max_vertices':part.get('max_vertices',32),'fit':part.get('fit',sh)}
                npart=native_parts[idx] if idx<len(native_parts) else {'kind':'HULL'}
                kind=str(npart.get('kind','HULL')).upper()
                if active_uniform and kind in {'BOX','SPHERE','CAPSULE','CYLINDER'}:
                    npos=Vector(npart.get('pos',(0,0,0)))
                    nr=npart.get('rot',(0,0,0,1)); local_rot=Quaternion((float(nr[3]),float(nr[0]),float(nr[1]),float(nr[2]))).normalized()
                    world_center=Vector(p0)+source_rot@npos; local_center=inv@world_center
                    rel_rot=(active_rot.inverted()@source_rot@local_rot).normalized()
                    par=[float(x) for x in npart.get('params',(0,0,0))]
                    if kind=='BOX': par=[par[0]/active_uniform_scale,par[1]/active_uniform_scale,par[2]/active_uniform_scale]
                    elif kind=='SPHERE': par=[par[0]/active_uniform_scale,0.0,0.0]
                    elif kind=='CAPSULE': par=[par[0]/active_uniform_scale,par[1]/active_uniform_scale,0.0]
                    else: par=[par[0]/active_uniform_scale,par[1]/active_uniform_scale,par[2] if len(par)>2 else 12.0]
                    rec.update({'kind':kind,'pos':(local_center.x,local_center.y,local_center.z),'rot':_quat_tuple_xyzw(rel_rot),'params':tuple(par)})
                stored.append(rec)
        if not stored:
            self.report({'ERROR'},'Could not build compound parts'); return {'CANCELLED'}
        for o in list(context.selected_objects): o.select_set(o in objs)
        context.view_layer.objects.active=active
        try: bpy.ops.object.join()
        except Exception as e:
            self.report({'ERROR'},f'Join failed: {e}'); return {'CANCELLED'}
        active[MANUAL_COMPOUND_PROP]=json.dumps({'version':2,'parts':stored},separators=(',',':'))
        active.tumblify.shape='MANUAL'; active.tumblify.role='NONE'
        msg=f'Created mixed Manual Compound with {len(stored)} collision part(s)'
        if not active_uniform: msg+=' (non-uniform active scale: primitive parts stored as hull fallbacks)'
        self.report({'INFO'},msg)
        return {'FINISHED'}



class TU_OT_toggle_collision(bpy.types.Operator):
    bl_idname='tumblify.toggle_collision'; bl_label='Show Collision'
    list_kind: EnumProperty(items=[('SELECTED','Selected',''),('DYNAMIC','Dynamic',''),('STATIC','Static','')],default='SELECTED')
    def execute(self,context):
        s=context.scene.tumblify
        if s.show_collision_debug:
            _clear_debug_collection(); s.show_collision_debug=False
            return {'FINISHED'}
        s.collision_preview_scope=self.list_kind
        _clear_debug_collection()
        s.show_collision_debug=True
        try:
            _refresh_collision_debug(context.scene)
            _ensure_collision_overlay(context.scene)
            self.report({'INFO'},'Collision preview enabled')
            return {'FINISHED'}
        except Exception as ex:
            _clear_debug_collection();s.show_collision_debug=False
            self.report({'ERROR'},f'Collision debug failed: {ex}');return {'CANCELLED'}


class TU_OT_toggle_result_output(bpy.types.Operator):
    bl_idname='tumblify.toggle_result_output'; bl_label='Result Output'; bl_description='Switch between Combined instances and Separated objects'; bl_options={'INTERNAL'}
    def execute(self,context):
        st=context.scene.tumblify
        if st.mode=='DROP' and st.ui_advanced_run_tab=='BAKE':return {'FINISHED'}
        st.result_output='INSTANCES' if st.result_output=='OBJECTS' else 'OBJECTS'
        return {'FINISHED'}

class TU_OT_check(bpy.types.Operator):
    bl_idname='tumblify.check'; bl_label='Check Tumblify'
    def execute(self,context):
        try:
            w=World(1); w.add_floor(0)
            w.add_box(World.DYNAMIC,(0,0,2),(0,0,0,1),(.5,.5,.5),(0,0,0))
            w.add_hull(World.DYNAMIC,(2,0,2),(0,0,0,1),[(-.5,-.5,-.5),(.5,-.5,-.5),(0,.5,-.5),(0,0,.5)],16)
            w.add_mesh_static((0,3,0),(0,0,0,1),[(-1,-1,0),(1,-1,0),(1,1,0),(-1,1,0)],[0,1,2,0,2,3])
            for _ in range(10): w.step(1/60)
            workers=w.workers; offline=getattr(get_lib(),'has_fast_drop',False); w.close()
            suffix='offline drop=OK' if offline else 'offline drop=MISSING (rebuild DLL)'
            self.report({'INFO'},f'Tumblify / Box3D OK: {get_lib().version()} | workers test={workers} | {suffix}'); return {'FINISHED'}
        except Exception as e: self.report({'ERROR'},str(e)); return {'CANCELLED'}

class TU_OT_auto_spawn_rate(bpy.types.Operator):
    bl_idname='tumblify.auto_spawn_rate'; bl_label='Fit Spawn Rate to Frames'; bl_options={'REGISTER','UNDO'}
    bl_description='Calculate Spawn Rate so the requested object count is emitted across the current Emitter Bake frame count'
    def execute(self,context):
        s=context.scene.tumblify
        fps=float(context.scene.render.fps)/max(1.0e-6,float(context.scene.render.fps_base))
        frames=max(2,int(s.emitter_bake_frames)); count=max(1,int(s.drop_offline_count))
        duration=max(1.0/fps,float(frames-1)/fps)
        raw=(max(0,count-1)/duration) if count>1 else 1.0
        fitted=max(1.0,min(2000.0,float(raw)))
        s.spawn_rate=fitted
        if raw>2000.0:
            self.report({'WARNING'},f'Required {raw:.1f}/s exceeds the 2000/s limit; clamped to 2000/s')
        else:
            self.report({'INFO'},f'Spawn Rate fitted to {fitted:.2f} objects/s for {count} objects over {frames} frames')
        return {'FINISHED'}

def _cleanup_scene_targets(scene):
    protected=set()
    st=scene.tumblify
    for key in ('fast_sources','dynamic_sources','static_objects','fast_selected_statics'):
        protected.update(item.obj for item in getattr(st,key,[]) if item.obj is not None)
    emitter=getattr(st,'emitter_object',None)
    if emitter is not None and emitter.name!=BRUSH_NAME:protected.add(emitter)
    owned=[]
    candidates=list(scene.objects)+[obj for obj in bpy.data.objects if not obj.users_scene]
    for obj in candidates:
        if obj in protected or any(other is not scene for other in obj.users_scene):continue
        flags=(RUNTIME_PROP,FROZEN_PROP,INSTANCE_RESULT_PROP,EMITTER_BAKE_PROP,'Tumblify_Helper','Tumblify_BatchHelper','Tumblify_BatchPreview',DISPLAY_PROXY_PROP)
        if any(obj.get(key,False) for key in flags) or obj.get('Tumblify_InstanceHelper') is True or obj.name in {BRUSH_NAME,GUIDE_NAME,FAST_CURSOR_NAME,FAST_CURSOR_RAY_NAME}:
            owned.append(obj)
    return owned


def _cleanup_scene_artifacts(scene,meshes=()):
    # Strip only Tumblify-owned display/guide modifiers from surviving originals.
    for obj in scene.objects:
        if any(other is not scene for other in obj.users_scene):continue
        for mod in list(obj.modifiers):
            if mod.type=='NODES' and mod.name in {DISPLAY_MOD_NAME,GUIDE_MOD_NAME}:obj.modifiers.remove(mod)
    others=set()
    def collect(parent,result):
        for child in parent.children:
            if child in result:continue
            result.add(child);collect(child,result)
    for other in bpy.data.scenes:
        if other is not scene:collect(other.collection,others)
    collections=set();collect(scene.collection,collections)
    collections.update(col for col in bpy.data.collections if col.users==0 and (col.get('Tumblify_SpawnSession') or col.get('Tumblify_InstanceSources') or col.name in {SPAWN_COLLECTION,FAST_COLLECTION,DEBUG_COLLECTION,DISPLAY_PROXY_COLLECTION}))
    deleted=0
    for col in list(collections):
        if col in others:continue
        owned=bool(col.get('Tumblify_SpawnSession') or col.get('Tumblify_InstanceSources')) or col.name in {SPAWN_COLLECTION,FAST_COLLECTION,DEBUG_COLLECTION,DISPLAY_PROXY_COLLECTION}
        if not owned:continue
        # A user may have moved unrelated content into a generated collection.
        for obj in list(col.objects):
            if obj.name not in scene.collection.objects:scene.collection.objects.link(obj)
        for child in list(col.children):
            if child not in collections or not (child.get('Tumblify_SpawnSession') or child.get('Tumblify_InstanceSources')):
                if child.name not in scene.collection.children:scene.collection.children.link(child)
        bpy.data.collections.remove(col);deleted+=1
    for mesh in meshes:
        try:
            if mesh.users==0:bpy.data.meshes.remove(mesh)
        except (ReferenceError,RuntimeError):pass
    for group in list(bpy.data.node_groups):
        if group.users==0 and (group.get('Tumblify_Owned') or group.name.startswith(('Tumblify_InstanceDisplay_','Tumblify_ResultInstances_','Tumblify_EmitterGuide_','Tumblify_Collision','Tumblify_DebugInstances'))):bpy.data.node_groups.remove(group)
    for action in list(bpy.data.actions):
        if action.users==0 and action.name.startswith(EMITTER_BAKE_ACTION_PREFIX):bpy.data.actions.remove(action)
    st=scene.tumblify
    st.fast_cursor_ready=False;st.show_collision_debug=False
    if st.emitter_object is None:st.advanced_emitter_ready=False
    if 'Tumblify_LastSpawnCollection' in scene:del scene['Tumblify_LastSpawnCollection']
    clear_mesh_cache()
    _clear_scene_collisions()
    return deleted


class TU_OT_clear_spawned(bpy.types.Operator):
    bl_idname='tumblify.clear_spawned'; bl_label='Clear Spawned'; bl_options={'REGISTER'}
    bl_description='Delete spawned physics objects in chunks with progress. If a simulation is active it is cancelled safely first.'
    @classmethod
    def description(cls,context,properties):
        return 'Delete generated piles, helper objects and addon collections; preserve sources and user colliders' if properties.cleanup_scene else cls.bl_description

    cleanup_scene: BoolProperty(default=False,options={'HIDDEN','SKIP_SAVE'})
    _timer=None
    _objects=None
    _total=0
    _done=0

    def _finish(self,context,cancelled=False):
        if self._timer:
            try: context.window_manager.event_timer_remove(self._timer)
            except Exception: pass
            self._timer=None
        try: context.window_manager.progress_end()
        except Exception: pass
        try: context.workspace.status_text_set(None)
        except Exception: pass
        _clear_display_proxy_collection()
        if context.area: context.area.tag_redraw()
        if not cancelled:
            if self.cleanup_scene:
                collections=_cleanup_scene_artifacts(context.scene,getattr(self,'_cleanup_meshes',[]))
                self.report({'INFO'},f'Cleanup complete: {self._done} objects, {collections} collections')
            else:self.report({'INFO'},f'Deleted {self._done} spawned object(s)')
        return {'CANCELLED'} if cancelled else {'FINISHED'}

    def invoke(self,context,event):
        if self.cleanup_scene:
            if _RUNTIME is not None:
                self.report({'WARNING'},'Finish the simulation before Cleanup Scene');return {'CANCELLED'}
            return context.window_manager.invoke_confirm(self,event,title='Cleanup Scene?',message='Delete all Tumblify results, helpers and addon collections? Sources and user colliders will be kept.',confirm_text='Delete All',icon='WARNING')
        return self.execute(context)

    def execute(self,context):
        global _RUNTIME
        if _RUNTIME is not None and self.cleanup_scene:
            self.report({'WARNING'},'Finish the simulation before Cleanup Scene');return {'CANCELLED'}
        if _RUNTIME is not None:
            # Never remove datablocks from under a running native world. The modal
            # runtime will close itself first and then invoke this operator again.
            setattr(_RUNTIME,'cancel_delete_all_requested',True)
            self.report({'INFO'},'Cancelling active simulation before deleting spawned objects…')
            return {'FINISHED'}
        if self.cleanup_scene and _COLLISION_OVERLAY_OWNER is not None:_COLLISION_OVERLAY_OWNER._finish(context)
        self._objects=_cleanup_scene_targets(context.scene) if self.cleanup_scene else list(_spawned_objects(context.scene))
        self._cleanup_meshes=[obj.data for obj in self._objects if obj.type=='MESH']
        if self.cleanup_scene:
            owned_set=set(self._objects)
            for obj in context.scene.objects:
                if obj not in owned_set and obj.parent in owned_set:
                    matrix=obj.matrix_world.copy();obj.parent=None;obj.matrix_world=matrix
        self._total=len(self._objects); self._done=0
        if not self._total:
            if self.cleanup_scene:return self._finish(context)
            _clear_display_proxy_collection()
            self.report({'INFO'},'No spawned objects to delete')
            return {'FINISHED'}
        wm=context.window_manager
        wm.progress_begin(0,self._total)
        try: context.workspace.status_text_set(f'Tumblify: deleting spawned objects… 0/{self._total}')
        except Exception: pass
        self._timer=wm.event_timer_add(0.01,window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self,context,event):
        if event.type=='ESC' and event.value=='PRESS':
            return self._finish(context,True)
        if event.type!='TIMER': return {'PASS_THROUGH'}
        # Keep each UI slice short; Blender object removal/depsgraph updates dominate.
        chunk=64
        for _ in range(chunk):
            if self._done>=self._total: break
            o=self._objects[self._done]; self._done+=1
            try:
                if _is_live_object(o):
                    if bool(o.get(INSTANCE_RESULT_PROP,False)): _remove_instance_result_helper(o)
                    bpy.data.objects.remove(o,do_unlink=True)
            except Exception: pass
        try: context.window_manager.progress_update(self._done)
        except Exception: pass
        try: context.workspace.status_text_set(f'Tumblify: deleting spawned objects… {self._done}/{self._total}')
        except Exception: pass
        if context.area: context.area.tag_redraw()
        if self._done>=self._total:
            return self._finish(context)
        return {'RUNNING_MODAL'}


def _remove_collection_tree(collection):
    """Remove a spawn-session collection recursively and return removed object count.

    Session collections may contain GN source/helper child collections.  Remove
    children first, clean node groups/meshes when they become orphaned, then
    remove the collection itself.  This helper is intentionally mode-agnostic
    so Fast and Advanced use the exact same cleanup path.
    """
    if collection is None:
        return 0
    try:
        if bpy.data.collections.get(collection.name) is not collection:
            return 0
    except Exception:
        return 0

    removed = 0
    for child in list(collection.children):
        removed += _remove_collection_tree(child)

    for obj in list(collection.objects):
        if obj is None:
            continue
        mesh = getattr(obj, 'data', None) if getattr(obj, 'type', None) == 'MESH' else None
        for mod in list(getattr(obj, 'modifiers', ())):
            if getattr(mod, 'type', None) == 'NODES':
                ng = getattr(mod, 'node_group', None)
                try:
                    obj.modifiers.remove(mod)
                except Exception:
                    pass
                if ng is not None and getattr(ng, 'users', 1) == 0:
                    try:
                        bpy.data.node_groups.remove(ng)
                    except Exception:
                        pass
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except Exception:
            pass
        if mesh is not None and getattr(mesh, 'users', 1) == 0:
            try:
                bpy.data.meshes.remove(mesh)
            except Exception:
                pass

    try:
        bpy.data.collections.remove(collection)
    except Exception:
        pass
    return removed


def _repair_last_spawn_collection(scene):
    """Point Tumblify_LastSpawnCollection at the newest surviving session."""
    cols = _spawn_session_collections(scene)
    if cols:
        def _key(c):
            try:
                return int(c.name.rsplit('_', 1)[-1])
            except Exception:
                return -1
        last = max(cols, key=_key)
        scene['Tumblify_LastSpawnCollection'] = last.name
    elif 'Tumblify_LastSpawnCollection' in scene:
        del scene['Tumblify_LastSpawnCollection']


def _delete_mode_spawn_collections(scene, mode):
    cols = [c for c in list(_spawn_session_collections(scene)) if str(c.get('Tumblify_Mode', '')) == str(mode)]
    removed_objects = 0
    removed_sessions = 0
    for c in cols:
        removed_objects += _remove_collection_tree(c)
        removed_sessions += 1
    _repair_last_spawn_collection(scene)
    return removed_sessions, removed_objects


def _last_mode_spawn_collection(scene,mode):
    cols=[c for c in _spawn_session_collections(scene) if str(c.get('Tumblify_Mode',''))==str(mode)]
    if not cols:
        return None
    def index_of(c):
        try: return int(c.name.rsplit('_',1)[-1])
        except Exception: return -1
    return max(cols,key=index_of)

def _last_fast_spawn_collection(scene):
    return _last_mode_spawn_collection(scene,'Fast')

def _last_drop_spawn_collection(scene):
    return _last_mode_spawn_collection(scene,'Drop')

def _current_mode_spawn_collection(scene):
    mode=str(getattr(scene.tumblify,'mode','FAST'))
    return _last_fast_spawn_collection(scene) if mode=='FAST' else _last_drop_spawn_collection(scene)


class TU_OT_select_results(bpy.types.Operator):
    bl_idname='tumblify.select_results';bl_label='Select Results';bl_options={'REGISTER','UNDO'}
    bl_description='Select visible Tumblify results; source assets and service objects are excluded'
    scope: EnumProperty(items=[('LAST','Last Drop','Select the latest result session for this mode'),('ALL','All Results','Select all generated piles in this scene')],default='LAST')

    def execute(self,context):
        if _RUNTIME is not None:
            self.report({'WARNING'},'Finish the simulation before selecting results');return {'CANCELLED'}
        targets=[obj for obj in _pile_objects(context.scene) if obj.type=='MESH' and not obj.get('Tumblify_BatchPreview') and not obj.get(DISPLAY_PROXY_PROP)]
        if self.scope=='LAST':
            collection=_current_mode_spawn_collection(context.scene)
            members=set(collection.all_objects) if collection is not None else set()
            targets=[obj for obj in targets if obj in members]
        visible=[obj for obj in targets if obj.name in context.view_layer.objects and obj.visible_get(view_layer=context.view_layer) and not obj.hide_select]
        if not visible:
            self.report({'INFO'},'No visible results to select');return {'CANCELLED'}
        for obj in list(context.selected_objects):obj.select_set(False)
        for obj in visible:obj.select_set(True)
        context.view_layer.objects.active=visible[0]
        self.report({'INFO'},f'Selected {len(visible)} result(s)'+(f'; {len(targets)-len(visible)} hidden/unselectable skipped' if len(visible)<len(targets) else ''))
        return {'FINISHED'}


class TU_OT_delete_last_fast(bpy.types.Operator):
    bl_idname='tumblify.delete_last_fast'; bl_label='Delete Last Objects'; bl_options={'REGISTER','UNDO'}
    bl_description='Delete only the most recent Fast Drop result and its session collection'
    def execute(self,context):
        global _RUNTIME
        if _RUNTIME is not None:
            self.report({'WARNING'},'Finish the active Fast simulation first'); return {'CANCELLED'}
        c=_last_fast_spawn_collection(context.scene)
        if c is None:
            self.report({'INFO'},'No Fast Drop result to delete'); return {'CANCELLED'}
        name=c.name
        n=_remove_collection_tree(c)
        _repair_last_spawn_collection(context.scene)
        _clear_display_proxy_collection()
        if context.area: context.area.tag_redraw()
        self.report({'INFO'},f'Deleted {name}: {n} object(s)')
        return {'FINISHED'}

class TU_OT_delete_all_fast(bpy.types.Operator):
    bl_idname = 'tumblify.delete_all_fast'
    bl_label = 'Delete All'
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = 'Delete every completed Fast Drop session; Advanced results are not touched'

    def execute(self, context):
        global _RUNTIME
        if _RUNTIME is not None:
            self.report({'WARNING'}, 'Finish the active Fast simulation first')
            return {'CANCELLED'}
        (sessions, objects) = _delete_mode_spawn_collections(context.scene, 'Fast')
        _clear_display_proxy_collection()
        if context.area:
            context.area.tag_redraw()
        if not sessions:
            self.report({'INFO'}, 'No Fast Drop results to delete')
            return {'CANCELLED'}
        self.report({'INFO'}, f'Deleted {sessions} Fast session(s), {objects} object(s)')
        return {'FINISHED'}


class TU_OT_delete_last_drop(bpy.types.Operator):
    bl_idname='tumblify.delete_last_drop'; bl_label='Delete Last Objects'; bl_options={'REGISTER','UNDO'}
    bl_description='Delete only the most recent Advanced Drop result and its session collection'
    def execute(self,context):
        global _RUNTIME
        if _RUNTIME is not None:
            self.report({'WARNING'},'Finish the active Advanced simulation first'); return {'CANCELLED'}
        c=_last_drop_spawn_collection(context.scene)
        if c is None:
            self.report({'INFO'},'No Advanced Drop result to delete'); return {'CANCELLED'}
        name=c.name; n=_remove_collection_tree(c)
        _repair_last_spawn_collection(context.scene)
        _clear_display_proxy_collection()
        if context.area: context.area.tag_redraw()
        self.report({'INFO'},f'Deleted {name}: {n} object(s)')
        return {'FINISHED'}

class TU_OT_realize_instances(bpy.types.Operator):
    bl_idname='tumblify.realize_instances'; bl_label='Realize Instances'; bl_options={'REGISTER','UNDO'}
    bl_description='Realize selected Combined results; if none are selected, realize the latest result session'
    def execute(self,context):
        global _RUNTIME
        if _RUNTIME is not None:
            self.report({'WARNING'},'Finish the active simulation first'); return {'CANCELLED'}
        selected=[o for o in list(getattr(context,'selected_objects',[]) or []) if _is_live_object(o) and bool(o.get(INSTANCE_RESULT_PROP,False))]
        made=[]
        touched=set()
        if selected:
            for inst in selected:
                cols=list(getattr(inst,'users_collection',[]) or [])
                c=cols[0] if cols else getattr(context,'collection',None)
                if c is None: continue
                touched.add(c.name)
                made.extend(_realize_instance_object(context,inst,c))
            # Remove only empty helper collections belonging to sessions touched by
            # this selection. Sibling Combined results keep their own helpers.
            for name in touched:
                c=bpy.data.collections.get(name)
                if c is None: continue
                # Do not wipe the shared Sources collection while sibling Combined
                # instancers in the same session still reference their helpers.
                siblings=[o for o in list(c.objects) if _is_live_object(o) and bool(o.get(INSTANCE_RESULT_PROP,False))]
                if not siblings: _cleanup_instance_source_collection(c)
        else:
            c=_current_mode_spawn_collection(context.scene)
            if c is None:
                self.report({'INFO'},'Select Combined result(s) or create a result session first'); return {'CANCELLED'}
            made=_realize_instance_results(context,c)
        if not made:
            self.report({'INFO'},'No Combined results to realize'); return {'CANCELLED'}
        # Select the newly realized objects for immediate follow-up work.
        try:
            bpy.ops.object.select_all(action='DESELECT')
            for o in made:
                if _is_live_object(o): o.select_set(True)
            if made and _is_live_object(made[0]): context.view_layer.objects.active=made[0]
        except Exception:
            pass
        self.report({'INFO'},f'Realized {len(made)} objects'); return {'FINISHED'}

def _join_result_visual_meshes(context,objects,collection,name):
    """Join evaluated copies; never merge into a mesh shared with a source asset."""
    objects=list({o.name:o for o in objects if _is_live_object(o)}.values())
    if len(objects)<2: raise RuntimeError('Need at least two result objects to join')
    depsgraph=context.evaluated_depsgraph_get()
    target=None; temporary_names=[]; mesh_names=[]
    try:
        mesh=bpy.data.meshes.new(name+'_Mesh'); mesh_names.append(mesh.name)
        target=bpy.data.objects.new(name,mesh); collection.objects.link(target)
        _sync_visual_state_from_source(objects[0],target)
        for original in objects:
            evaluated=original.evaluated_get(depsgraph)
            mesh=bpy.data.meshes.new_from_object(evaluated,preserve_all_data_layers=True,depsgraph=depsgraph)
            mesh_names.append(mesh.name)
            if not mesh.vertices: raise RuntimeError('Result has no evaluated mesh geometry')
            duplicate=bpy.data.objects.new('_TumblifyJoinInput',mesh)
            collection.objects.link(duplicate); temporary_names.append(duplicate.name)
            duplicate.matrix_world=original.matrix_world.copy()
            _sync_visual_state_from_source(original,duplicate)
            duplicate.hide_viewport=False; duplicate.hide_render=False; duplicate.hide_select=False; duplicate.hide_set(False)
        bpy.ops.object.select_all(action='DESELECT')
        target.select_set(True)
        for name in temporary_names: bpy.data.objects[name].select_set(True)
        context.view_layer.objects.active=target
        if bpy.ops.object.join()!={'FINISHED'}: raise RuntimeError('Blender could not join result meshes')
        if not _is_live_object(target): raise RuntimeError('Join destination was removed')
        for original in objects:
            bpy.data.objects.remove(original,do_unlink=True)
        return target
    except Exception:
        if _is_live_object(target): bpy.data.objects.remove(target,do_unlink=True)
        raise
    finally:
        for name in temporary_names:
            duplicate=bpy.data.objects.get(name)
            if duplicate is not None: bpy.data.objects.remove(duplicate,do_unlink=True)
        for name in mesh_names:
            mesh=bpy.data.meshes.get(name)
            if mesh is not None and mesh.users==0: bpy.data.meshes.remove(mesh)


class TU_OT_join_all_instances(bpy.types.Operator):
    bl_idname='tumblify.join_all_instances'; bl_label='Join All Instances'; bl_options={'REGISTER','UNDO'}
    bl_description='Join all mesh results from the most recent Tumblify Drop/Fast run into one Blender object'
    def execute(self,context):
        global _RUNTIME
        if _RUNTIME is not None:
            self.report({'WARNING'},'Finish the active simulation first'); return {'CANCELLED'}
        c=_current_mode_spawn_collection(context.scene)
        if c is None:
            self.report({'INFO'},'No Tumblify result session to join'); return {'CANCELLED'}
        # Freeze the join allow-list BEFORE realization.  Scanning the collection
        # afterwards can accidentally pick up hidden GN source helpers/legacy source
        # copies created as service data and duplicate the pile in the joined mesh.
        objs=[]
        instancers=[]
        for o in list(c.objects):
            if _is_live_object(o) and o.type=='MESH' and bool(o.get(INSTANCE_RESULT_PROP,False)):
                instancers.append(o)

        # Source assets and GN helper objects are service data for Combined results,
        # never result geometry.  Exclude them explicitly even if an older run left
        # runtime/frozen flags on the object or linked it into the session collection.
        excluded_ids=set()
        excluded_names=set()
        for inst in instancers:
            src_name=str(inst.get('Tumblify_SourceName',''))
            if src_name:
                excluded_names.add(src_name)
                src=bpy.data.objects.get(src_name)
                if _is_live_object(src): excluded_ids.add(id(src))
            helper=_instance_result_helper(inst)
            if _is_live_object(helper):
                excluded_ids.add(id(helper)); excluded_names.add(helper.name)

        for o in list(c.objects):
            if not _is_live_object(o) or o.type!='MESH': continue
            if bool(o.get(INSTANCE_RESULT_PROP,False)): continue
            if id(o) in excluded_ids or o.name in excluded_names: continue
            if bool(o.get('Tumblify_Helper',False)) or bool(o.get('Tumblify_InstanceHelper',False)): continue
            if INSTANCE_RESULT_MOD_NAME in getattr(o,'modifiers',{}): continue
            if not (bool(o.get(RUNTIME_PROP,False)) or bool(o.get(FROZEN_PROP,False))): continue
            try:
                if o.hide_get() or o.hide_viewport: continue
            except Exception:
                pass
            objs.append(o)
        if instancers:
            objs.extend(_realize_instance_results(context,c))
        _cleanup_instance_source_collection(c)
        # De-duplicate live objects while preserving order.  Only the pre-approved
        # real results and the explicit return value from realization are eligible.
        seen=set(); objs=[o for o in objs if _is_live_object(o) and not (id(o) in seen or seen.add(id(o)))]
        if len(objs)<2:
            self.report({'INFO'},'Need at least two result objects in the latest run'); return {'CANCELLED'}
        try:
            target=_join_result_visual_meshes(context,objs,c,f'{c.name}_Joined')
            target[RUNTIME_PROP]=True; target[FROZEN_PROP]=False; target[FIXED_PROP]=True
            target.tumblify.role='NONE'
            _clear_display_proxy_collection()
            self.report({'INFO'},f'Joined {len(objs)} objects')
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'},f'Join failed: {e}'); return {'CANCELLED'}


class TU_OT_fix_selected(bpy.types.Operator):
    bl_idname='tumblify.fix_selected'; bl_label='Fix Selected'
    bl_options={'REGISTER','UNDO'}
    bl_description='Make selected spawned results visual-only. They will not participate in Drop or Interactive again'
    def execute(self,context):
        global _RUNTIME
        if _RUNTIME is not None:
            self.report({'WARNING'},'Stop simulation before fixing objects'); return {'CANCELLED'}
        n=0
        members=set(_spawned_objects(context.scene))
        if members:
            for o in context.selected_objects:
                if o in members and o.type=='MESH':
                    o[FIXED_PROP]=True; o[FROZEN_PROP]=False; o.tumblify.role='NONE'; n+=1
        self.report({'INFO'},f'Fixed visual-only: {n} object(s)')
        return {'FINISHED'}


class TU_OT_unfix_selected(bpy.types.Operator):
    bl_idname='tumblify.unfix_selected'; bl_label='Unfix Selected'
    bl_options={'REGISTER','UNDO'}
    bl_description='Return selected fixed spawned results to the frozen-static physics set for the next iteration'
    def execute(self,context):
        global _RUNTIME
        if _RUNTIME is not None:
            self.report({'WARNING'},'Stop simulation before unfixing objects'); return {'CANCELLED'}
        n=0
        members=set(_spawned_objects(context.scene))
        if members:
            for o in context.selected_objects:
                if o in members and bool(o.get(FIXED_PROP,False)):
                    o[FIXED_PROP]=False; o[FROZEN_PROP]=True; o[RUNTIME_PROP]=True; n+=1
                    if 'Tumblify_RecoveryArchive' in o:
                        del o['Tumblify_RecoveryArchive']
        self.report({'INFO'},f'Returned to frozen physics: {n} object(s)')
        return {'FINISHED'}


def _delete_physics_result_objects(objects):
    """Delete explicit results and owned service data, preserving sibling groups."""
    removed=0; touched=set()
    for obj in objects:
        if not _is_live_object(obj): continue
        touched.update(c.name for c in obj.users_collection)
        mesh=obj.data if obj.type=='MESH' else None
        node_groups={m.node_group for m in obj.modifiers if m.type=='NODES' and m.node_group is not None}
        if bool(obj.get(INSTANCE_RESULT_PROP,False)):
            _remove_instance_result_helper(obj)
        bpy.data.objects.remove(obj,do_unlink=True); removed+=1
        if mesh is not None and mesh.users==0:
            bpy.data.meshes.remove(mesh)
        for group in node_groups:
            if group.users==0: bpy.data.node_groups.remove(group)
    for name in touched:
        collection=bpy.data.collections.get(name)
        if collection is not None and not any(bool(o.get(INSTANCE_RESULT_PROP,False)) for o in collection.objects):
            _cleanup_instance_source_collection(collection)
    return removed


class TU_OT_post_delete_all(bpy.types.Operator):
    bl_idname='tumblify.post_delete_all'; bl_label='Delete All Physics Results'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        global _RUNTIME
        if _RUNTIME is not None:
            self.report({'WARNING'},'Stop Interactive simulation before deleting all results'); return {'CANCELLED'}
        n=_delete_physics_result_objects(_frozen_physics_results(context.scene))
        self.report({'INFO'},f'Deleted {n} physics result object(s)')
        return {'FINISHED'}

class TU_OT_post_select_all(bpy.types.Operator):
    bl_idname='tumblify.post_select_all'; bl_label='Select All Frozen'
    def execute(self,context):
        for o in context.selected_objects: o.select_set(False)
        frozen=_frozen_physics_results(context.scene)
        for o in frozen: o.select_set(True)
        if frozen: context.view_layer.objects.active=frozen[0]
        return {'FINISHED'}

class TU_OT_post_join_selected(bpy.types.Operator):
    bl_idname='tumblify.post_join_selected'; bl_label='Join Selected to Mesh'; bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        global _RUNTIME
        if _RUNTIME is not None:
            self.report({'WARNING'},'Stop simulation before joining objects'); return {'CANCELLED'}
        members=set(_frozen_spawned(context.scene))
        objs=[o for o in context.selected_objects if o in members and o.type=='MESH']
        if len(objs)<2:
            self.report({'WARNING'},'Select at least two spawned mesh objects'); return {'CANCELLED'}
        material_preset=objs[0].tumblify.material_preset
        mass_multiplier=objs[0].tumblify.mass_multiplier
        collection=objs[0].users_collection[0] if objs[0].users_collection else context.collection
        try: active=_join_result_visual_meshes(context,objs,collection,objs[0].name+'_Joined')
        except Exception as e:
            self.report({'ERROR'},f'Join failed: {e}'); return {'CANCELLED'}
        active[FROZEN_PROP]=True; active[FIXED_PROP]=False; active[RUNTIME_PROP]=True
        active['Tumblify_SourceName']=''; active['Tumblify_InstanceScale']=1.0
        active.tumblify.role='NONE'; active.tumblify.shape='BOX'
        active.tumblify.material_preset=material_preset; active.tumblify.mass_multiplier=mass_multiplier
        self.report({'INFO'},'Joined selected objects; post collision fallback set to Box')
        return {'FINISHED'}


class TU_OT_post_delete_selected(bpy.types.Operator):
    bl_idname='tumblify.post_delete_selected'; bl_label='Delete Selected'
    bl_options={'REGISTER','UNDO'}
    bl_description='Delete selected frozen physics objects in Interactive mode when safe'
    def execute(self,context):
        global _RUNTIME
        rt=_RUNTIME
        if rt is not None and isinstance(rt,TU_OT_post_run):
            n=rt.delete_selected_runtime(context)
            if n<0:
                self.report({'WARNING'},'Selected object cannot be deleted safely during Interactive. Stop the simulation first.'); return {'CANCELLED'}
            self.report({'INFO'},f'Deleted {n} post-simulation object(s)'); return {'FINISHED'}
        if rt is not None:
            self.report({'WARNING'},'Stop the active simulation before deleting Interactive objects'); return {'CANCELLED'}
        members=set(_frozen_physics_results(context.scene))
        objs=[o for o in list(context.selected_objects) if o in members]
        n=_delete_physics_result_objects(objs)
        self.report({'INFO'},f'Deleted {n} object(s)')
        return {'FINISHED'}

class TU_OT_stop(bpy.types.Operator):
    bl_idname='tumblify.stop'; bl_label='Stop Simulation'
    def execute(self,context):
        global STOP_REQUESTED, _RUNTIME
        STOP_REQUESTED=True
        # If a modal callback has crashed, Blender may stop delivering events to it. In that case
        # provide a direct emergency cleanup path from the UI Stop button instead of leaving the
        # native world and viewport timer orphaned. Normal running modals still consume STOP_REQUESTED.
        rt=_RUNTIME
        if rt is not None and getattr(rt,'_timer',None) is None:
            try:
                if hasattr(rt,'_cancel_run'):
                    rt._cancel_run(context)
            except Exception:
                try:
                    if hasattr(rt,'world'): rt.world.close()
                except Exception: pass
                _solid_state_restore()
                _RUNTIME=None; STOP_REQUESTED=False
        return {'FINISHED'}


def _schedule_clear_spawned():
    """Start the chunked deletion operator after the current modal runtime has exited."""
    def _run():
        try: bpy.ops.tumblify.clear_spawned('INVOKE_DEFAULT',cleanup_scene=False)
        except Exception: pass
        return None
    try: _register_owned_app_timer(_run,first_interval=0.02)
    except Exception: pass


def _scatter_sample_world(obj, sampler, total):
    r=random.random()*total
    chosen=sampler[-1]
    lo=0; hi=len(sampler)-1
    while lo<hi:
        mid=(lo+hi)//2
        if r<=sampler[mid][0]: hi=mid
        else: lo=mid+1
    chosen=sampler[lo]
    _,a,b,c,n=chosen
    u=math.sqrt(random.random()); v=random.random()
    p=(1.0-u)*a + u*(1.0-v)*b + u*v*c
    mw=obj.matrix_world
    wp=mw @ p
    nm=mw.to_3x3().inverted().transposed() @ n
    if nm.length_squared<1.0e-12: nm=Vector((0,0,1))
    else: nm.normalize()
    return wp,nm


def _fill_build_emitter_data(obj,depsgraph,prepared_override=None):
    """Prepare the assigned Fill boundary and a matching collision-free spawn domain."""
    if obj is None:return None
    p,q,_=world_pose_scale(obj);rot=Quaternion((q[3],q[0],q[1],q[2]))
    prepared=None;profile=None;obstacles=[]
    if obj.type=='EMPTY':
        points,faces=_sphere_geometry(max(.05,float(obj.empty_display_size)))
        scale=world_pose_scale(obj)[2];points=[(v[0]*scale[0],v[1]*scale[1],v[2]*scale[2]) for v in points];shape='SPHERE'
    elif obj.type=='MESH':
        prepared=prepared_override or _prepare_collision(obj,depsgraph,float(bpy.context.scene.tumblify.convex_simplify))
        shape=prepared['shape'];center=Vector(prepared['center']);half=Vector(prepared['half'])
        if shape=='TERRAIN':raise RuntimeError(f"Emitter '{obj.name}': Terrain is an open height field, not a Fill volume. Choose Hull, Mesh or Container")
        if shape=='HULL':
            points,faces=prepared['points'],prepared.get('faces',[])
            if not faces:points,faces=_hull_debug_geometry(points)
        elif shape in {'BOX','SPHERE','CAPSULE','CYLINDER'}:
            if shape=='BOX':points,faces=_box_geometry(*half)
            elif shape=='SPHERE':points,faces=_sphere_geometry(max(half))
            else:
                axis=int(prepared['axis']);radius,long_half=_axis_radius_half(half,axis,True)
                if shape=='CAPSULE':points,faces=_capsule_geometry(max(0.0,long_half-radius),radius)
                else:points,faces=_cylinder_geometry(long_half,radius,(0,0,0),16)
                points=[_axis_map_point(v,axis) for v in points]
            points=[tuple(Vector(v)+center) for v in points]
        elif shape=='MESH':
            points=list(prepared['verts']);indices=prepared['indices'];faces=[tuple(indices[i:i+3]) for i in range(0,len(indices),3)]
        else:
            parts=prepared.get('parts',[])
            if prepared.get('mixed_parts'):parts=[{'points':item.get('points',[]),'native':item} for item in prepared['mixed_parts']]
            points=[];faces=[]
            for part in parts:
                pv,pf=part.get('points',[]),part.get('faces',[])
                native=part.get('native') if shape!='COMPOUND' else None
                if native and native.get('kind')=='HULL':pv,pf=_hull_debug_geometry(native.get('points',pv))
                if native and native.get('kind') in {'BOX','SPHERE','CAPSULE','CYLINDER'}:
                    kind=native['kind'];params=native['params']
                    if kind=='BOX':pv,pf=_box_geometry(*params)
                    elif kind=='SPHERE':pv,pf=_sphere_geometry(params[0])
                    elif kind=='CAPSULE':pv,pf=_capsule_geometry(params[0],params[1]);pv=[_axis_map_point(v,1) for v in pv]
                    else:pv,pf=_cylinder_geometry(params[0],params[1],(0,0,0),16);pv=[_axis_map_point(v,1) for v in pv]
                if native:
                    nq=native.get('rot',(0,0,0,1));nq=Quaternion((nq[3],nq[0],nq[1],nq[2]));pos=Vector(native.get('pos',(0,0,0)));pv=[tuple(pos+nq@Vector(v)) for v in pv]
                if not pf:pv,pf=_hull_debug_geometry(pv)
                if not pv or not pf:continue
                lo=Vector(tuple(min(v[i] for v in pv) for i in range(3)));hi=Vector(tuple(max(v[i] for v in pv) for i in range(3)))
                obstacles.append({'bvh':BVHTree.FromPolygons(pv,pf),'min':lo,'max':hi,'native':native})
                points.extend(pv)
            points,faces=_hull_debug_geometry(points)
        if shape in {'CONTAINER','MESH','COMPOUND','LOOSE','MANUAL'}:
            geo=None
            if shape=='MESH':
                pv=[Vector(v) for v in points];lo=Vector(tuple(min(v[i] for v in pv) for i in range(3)));hi=Vector(tuple(max(v[i] for v in pv) for i in range(3)))
                geo=(pv,faces,BVHTree.FromPolygons(pv,faces),lo,hi)
            profile=prepared['container_profile'] if shape=='CONTAINER' else _container_cavity_profile(obj,depsgraph,geometry=geo)
    else:raise RuntimeError(f"Emitter '{obj.name}': Fill requires a Mesh or Empty")
    if not points or not faces:raise RuntimeError(f"Emitter '{obj.name}': assigned {shape} collision has no usable Fill boundary")
    verts=[Vector(v) for v in points];triangles=[(face[0],face[j],face[j+1]) for face in faces for j in range(1,len(face)-1)]
    mn=Vector(tuple(min(v[i] for v in verts) for i in range(3)));mx=Vector(tuple(max(v[i] for v in verts) for i in range(3)))
    # Vessel Mesh uses its real inner-facing surfaces; closed single shells are inverted.
    reverse=not (shape=='MESH' and profile)
    indices=[]
    centre=(mn+mx)*.5
    for a,b,c in triangles:
        flip=reverse
        if shape in {'HULL','BOX','SPHERE','CYLINDER','CAPSULE'}:
            normal=(verts[b]-verts[a]).cross(verts[c]-verts[a])
            flip=normal.dot((verts[a]+verts[b]+verts[c])/3-centre)>0
        indices.extend((a,c,b) if flip else (a,b,c))
    data={'obj':obj,'origin':Vector(p),'rot':rot,'verts':[tuple(v) for v in verts],'indices':indices,
          'bvh':BVHTree.FromPolygons(verts,triangles,all_triangles=True),'min':mn,'max':mx,'used_volume':0.0,
          'bounds_volume':max(1e-9,(mx.x-mn.x)*(mx.y-mn.y)*(mx.z-mn.z)),
          'handle':None,'kind':'MESH','assigned_shape':shape,'assigned_collision':prepared,'obstacles':obstacles,'safety_margin':0.003 if shape=='CONTAINER' else float(obj.tumblify.container_safety_margin)}
    if profile:
        data['cavity_profile']=profile;data['bounds_volume']=float(profile.get('volume',data['bounds_volume']))
        data['min'].z=float(profile.get('bottom_z',profile['z_min']))+float(profile.get('wall_offset',0.0))
        data['max'].z=float(profile['z_max'])
    return data


def _surface_project_to_proxy(data,point,normal):
    if not data:return point,normal
    _fill_update_pose_from_object(data)
    local=data['rot'].inverted()@(point-data['origin'])
    hit=data['bvh'].find_nearest(local)
    if not hit or hit[0] is None:return point,normal
    position,direction=hit[0],hit[1]
    if direction.dot(position-(data['min']+data['max'])*.5)<0:direction=-direction
    return data['origin']+data['rot']@position,(data['rot']@direction).normalized()


def _fill_rebuild_container(world,data,friction=0.7):
    """Rebuild a Fill Emitter shell at its current Blender transform.

    Box3D triangle meshes are static-only in the current bridge, so a moved Mesh
    emitter cannot be targeted as a kinematic body. Rebuilding the static shell
    while the user drags the emitter is the safe Python-side fallback; overlap
    correction then pushes/settles the contained dynamic bodies.
    """
    if not data:
        return []
    _fill_destroy_container(world,data)
    _fill_update_pose_from_object(data)
    return _fill_add_container(world,data,friction)

def _fill_add_container(world,data,friction=0.7):
    """Create the invisible static containment proxy for one emitter.

    When the emitter itself is assigned Container / Open Concave, use that exact compound
    proxy already during normal Fill Drop. This keeps normal Fill, Show Collision and Shift+E
    Live Interactive on the same cavity geometry instead of spawning into one shape and later
    switching to another.
    """
    if not data:
        return []
    p=data['origin']; q=data['rot']
    obj=data.get('obj')
    if data.get('kind') in {'MESH','TERRAIN'} and _is_live_object(obj):
        try: assigned=str(data.get('assigned_shape') or obj.tumblify.shape).upper()
        except Exception: assigned=''
        if assigned in {'CONTAINER','COMPOUND','LOOSE','MANUAL'}:
            depsgraph=bpy.context.evaluated_depsgraph_get()
            prepared=data.get('assigned_collision') or _prepare_collision(obj,depsgraph,float(getattr(bpy.context.scene.tumblify,'convex_simplify',0.65)))
            if not (prepared.get('mixed_parts') or prepared.get('parts')):
                raise RuntimeError(f'{assigned} generated no static compound segments')
            h=add_body(world,World.STATIC,obj,depsgraph,p,(q.x,q.y,q.z,q.w),prepared=prepared,instance_scale=1.0,friction=friction,restitution=0.0,density=0.0)
            data['handle']=[h]
            data['container_proxy_mode']='OPEN_CONCAVE'
            return [h]
    if data.get('kind') in {'MESH','TERRAIN'} and data.get('indices'):
        # Primary exact inward-facing shell.  Add a second, slightly expanded inward
        # shell as a safety layer.  The second layer catches bodies that numerically
        # cross the exact surface during dense stacking or interactive emitter motion.
        # It is intentionally close to the original surface so the visible emitter
        # remains the effective container shape rather than falling back to its bounds.
        h0=world.add_mesh_static(p,(q.x,q.y,q.z,q.w),data['verts'],data['indices'],friction,0.0)
        handles=[h0]
        try:
            mn=Vector(data['min']); mx=Vector(data['max']); center=(mn+mx)*0.5
            diag=max(1.0e-6,(mx-mn).length)
            margin=max(0.002,min(0.03,diag*0.012))
            expanded=[]
            for vv in data['verts']:
                v=Vector(vv); d=v-center
                if d.length_squared>1.0e-12:
                    v=v+d.normalized()*margin
                expanded.append(tuple(v))
            h1=world.add_mesh_static(p,(q.x,q.y,q.z,q.w),expanded,data['indices'],friction,0.0)
            handles.append(h1)
        except Exception:
            pass
        data['handle']=handles
        return handles
    # Five OBB walls (open top) for non-mesh emitters.
    c=Vector(data['center']); h=Vector(data['half'])
    t=max(0.005,min(h.x,h.y,h.z)*0.035)
    walls=[
        ((h.x+t,h.y+t,t),(c.x,c.y,c.z-h.z-t)),
        ((t,h.y+t,h.z+t),(c.x-h.x-t,c.y,c.z)),
        ((t,h.y+t,h.z+t),(c.x+h.x+t,c.y,c.z)),
        ((h.x+t,t,h.z+t),(c.x,c.y-h.y-t,c.z)),
        ((h.x+t,t,h.z+t),(c.x,c.y+h.y+t,c.z)),
    ]
    handles=[]
    for half,center in walls:
        handles.append(world.add_box(World.STATIC,p,(q.x,q.y,q.z,q.w),half,center,friction,0.0,0.0))
    data['handle']=handles
    return handles


def _fill_add_assigned_interactive_container(world,data,friction=0.7,margin=0.01):
    """Build Live Interactive Fill collision from the emitter's assigned collision model.

    v0.13.44 deliberately makes Loose Parts / Compound use the same convex pieces that
    Show Collision displays.  This avoids the previous silent failure path where a mixed
    compound error dropped through to Auto Shell and a dense torus became a bounds box.
    """
    obj=data.get('obj') if data else None
    if not _is_live_object(obj):
        return (None,None)
    try: shape=str(data.get('assigned_shape') or obj.tumblify.shape).upper()
    except Exception: shape='MESH'
    if shape=='AUTO': shape='LOOSE'
    if shape=='DETECT': shape=_auto_collision_shape(obj,bpy.context.evaluated_depsgraph_get())
    p=data['origin']; q=data['rot']
    depsgraph=bpy.context.evaluated_depsgraph_get()
    simplify=float(getattr(bpy.context.scene.tumblify,'convex_simplify',0.65))

    if shape in {'LOOSE','COMPOUND','MANUAL','CONTAINER'}:
        try:
            # IMPORTANT: build the Live Interactive body through the exact same native path
            # used by an ordinary Box3D collision of this type.  Show Collision visualizes
            # _prepare_collision(), so using add_body(prepared=...) guarantees that the
            # runtime collider and the visible proxy are generated from the same data.
            #
            # In particular, LOOSE must use add_compound_mixed() (via add_body) rather
            # than being converted to a second ad-hoc list of hull assets.  The latter
            # was the source of the v0.13.44 mismatch where the preview looked correct
            # but Live Interactive could fall through to the bounds-box container.
            prepared=data.get('assigned_collision') or _prepare_collision(obj,depsgraph,simplify,shape_override=shape)
            part_count=len(prepared.get('parts',[]))
            if shape=='LOOSE' and not prepared.get('mixed_parts'):
                raise RuntimeError('Loose Parts generated no native compound parts')
            if shape=='COMPOUND' and not prepared.get('parts'):
                raise RuntimeError('Compound Hull generated no convex parts')
            if shape=='MANUAL' and not (prepared.get('mixed_parts') or prepared.get('parts')):
                raise RuntimeError('Manual Compound generated no parts')
            if shape=='CONTAINER' and not prepared.get('mixed_parts'):
                raise RuntimeError('Container / Open Concave generated no wall parts')
            h=add_body(
                world,World.KINEMATIC,obj,depsgraph,
                p,(q.x,q.y,q.z,q.w),simplify,prepared=prepared,instance_scale=1.0,
                friction=friction,restitution=0.0,density=0.0
            )
            data['interactive_assigned_collision']=prepared
            data['interactive_proxy_note']=f'{shape}: native assigned proxy ({part_count} part(s))'
            return (h,'ASSIGNED_'+shape)
        except Exception as exc:
            data['interactive_proxy_note']=f'{shape} proxy failed: {exc}'
            data['interactive_assigned_failed']=True
            return (None,None)

    # Primitive assignments mean a hollow container of that family.
    if shape in {'BOX','SPHERE','CYLINDER','CAPSULE'} and data.get('kind') in {'MESH','TERRAIN'}:
        axis=None
        if shape=='CYLINDER': axis=_cylinder_axis((Vector(data['max'])-Vector(data['min']))*0.5)
        elif shape=='CAPSULE': axis=_long_axis((Vector(data['max'])-Vector(data['min']))*0.5)
        assets,kind=_fill_build_standard_kinematic_assets(world,data,margin,forced_kind=shape,forced_axis=axis)
        if assets:
            h=world.add_compound_hull_assets(World.KINEMATIC,p,(q.x,q.y,q.z,q.w),assets,1.0,friction,0.0,0.0)
            data['interactive_proxy_note']=f'{kind} hollow shell'
            return (h,'ASSIGNED_'+str(kind))

    # Prepared convex boundaries are skinned with targetable triangle prisms below.
    data['interactive_proxy_note']=f'{shape}: kinematic skin of the prepared Fill boundary'
    return (None,None)

def _fill_add_interactive_container(world,data,friction=0.7,margin=0.01,proxy_mode='AUTO'):
    """Replace the normal static Fill shell with one targetable kinematic body."""
    if not data:
        return []
    _fill_destroy_container(world,data)
    _fill_update_pose_from_object(data)
    p=data['origin']; q=data['rot']
    h=None; mode='BOUNDS'
    data['interactive_proxy_note']=''
    # Container / Open Concave is semantic containment, not merely a display preference:
    # once assigned to the emitter it must remain the container in Shift+E as well.
    obj=data.get('obj')
    try: assigned_shape=str(obj.tumblify.shape).upper() if _is_live_object(obj) else ''
    except Exception: assigned_shape=''
    # The emitter's Collision Model is now the single source of truth for Fill/Interactive.
    use_assigned=bool(obj is not None and obj.type in {'MESH','TERRAIN'} and assigned_shape and assigned_shape not in {'MESH',''})
    if use_assigned:
        h,assigned_mode=_fill_add_assigned_interactive_container(world,data,friction,margin)
        if h is not None:
            mode=assigned_mode or 'ASSIGNED'
    if h is None and data.get('kind') in {'MESH','TERRAIN'}:
        # If an explicitly assigned compound unexpectedly fails, do NOT silently replace
        # it with a box. First try a direct triangle-prism shell with a generous budget.
        # A wrong box is much harder to diagnose than a visible warning/fallback shell.
        tri_budget=1536 if use_assigned else 384
        assets=_fill_build_mesh_kinematic_assets(world,data,margin,max_triangles=tri_budget)
        if assets:
            h=world.add_compound_hull_assets(World.KINEMATIC,p,(q.x,q.y,q.z,q.w),assets,1.0,friction,0.0,0.0)
            mode='MESH_PRISMS'
        elif h is None and not use_assigned:
            # Auto Shell may still use a recognized standard hollow primitive family.
            assets,standard_kind=_fill_build_standard_kinematic_assets(world,data,margin)
            if assets:
                h=world.add_compound_hull_assets(World.KINEMATIC,p,(q.x,q.y,q.z,q.w),assets,1.0,friction,0.0,0.0)
                mode='STANDARD_'+str(standard_kind)
    if h is None:
        if use_assigned:
            # Assigned emitter collision is strict: never lie by silently becoming BOUNDS.
            # Leave the Fill collision disabled for this emitter and expose the reason in
            # the runtime note/report instead of using an unrelated box proxy.
            data['interactive_proxy']=False
            data['interactive_proxy_mode']='ASSIGNED_FAILED'
            data['handle']=None
            return []
        # Auto Shell keeps the stable bounds fallback for irregular dense emitters.
        if data.get('kind') in {'MESH','TERRAIN'}:
            mn=Vector(data['min']); mx=Vector(data['max']); data.setdefault('center',(mn+mx)*0.5); data.setdefault('half',(mx-mn)*0.5)
        h=_fill_make_bounds_kinematic(world,data,friction,margin)
    data['handle']=h
    data['interactive_proxy']=True
    data['interactive_proxy_mode']=mode
    return [h]


def _fill_restore_static_container(world,data,friction=0.7):
    if not data:
        return []
    _fill_destroy_container(world,data)
    data['interactive_proxy']=False
    _fill_update_pose_from_object(data)
    return _fill_add_container(world,data,friction)


def _offline_volume_spawn_point(scene, emitter_objs):
    """Sample the current Drop emitter without creating any viewport simulation objects."""
    st=scene.tumblify
    custom=[o for o in emitter_objs if _is_live_object(o) and o.name!=BRUSH_NAME]
    if custom:
        o=random.choice(custom)
        try:
            corners=_emitter_bound_box_without_guide(o)
            mins=Vector((min(v.x for v in corners),min(v.y for v in corners),min(v.z for v in corners)))
            maxs=Vector((max(v.x for v in corners),max(v.y for v in corners),max(v.z for v in corners)))
            lp=Vector((random.uniform(mins.x,maxs.x),random.uniform(mins.y,maxs.y),random.uniform(mins.z,maxs.z)))
            return o.matrix_world @ lp
        except Exception:
            return o.matrix_world.translation.copy()
    brush=emitter_objs[0] if emitter_objs else ensure_brush(scene)
    center=brush.matrix_world.translation.copy()
    scale=max(abs(float(v)) for v in brush.scale) if brush else 1.0
    r=max(0.05,float(st.emitter_radius)*scale)*0.72
    v=Vector((0,0,0))
    for _ in range(16):
        v=Vector((random.uniform(-1,1),random.uniform(-1,1),random.uniform(-1,1)))
        if 0.0001 < v.length_squared <= 1.0: break
    return center+v*r


class TU_OT_drop_offline(bpy.types.Operator):
    bake_frames: BoolProperty(name='Bake Emitter Frames', description='Capture the offline Drop simulation at the scene frame rate and write keyframes after the native bake', default=False, options={'HIDDEN'})
    'Run Drop/Surface generation without per-step Blender visualization.\n\n    Bodies are prepared once, disabled, then enabled according to Spawn Rate while Box3D\n    advances in short CPU bursts. Blender objects are only created after the final settle.\n    This keeps the hot simulation loop free of Object/depsgraph transform traffic.\n    '
    bl_idname = 'tumblify.drop_offline'
    bl_label = 'Generate Final Result'
    bl_options = {'REGISTER', 'INTERNAL'}
    _timer = None

    def _status(self,context,text,progress=None):
        display=getattr(self,'_calculation_progress',None)
        if display is not None:
            stage=str(text).split(': ',1)[-1]
            display.update(display.percent if progress is None else progress,stage)
            return
        try:context.workspace.status_text_set(text)
        except Exception:pass
        if progress is not None:
            try:context.window_manager.progress_update(max(0.0,min(100.0,float(progress))))
            except Exception:pass

    def _cleanup(self, context, cancel=False):
        global _RUNTIME, STOP_REQUESTED
        try:
            if self._timer:
                context.window_manager.event_timer_remove(self._timer)
                self._timer = None
        except Exception:
            pass
        try:
            job = getattr(self, 'drop_job', None)
            if job is not None:
                if cancel:
                    job.cancel()
                job.close()
                self.drop_job = None
        except Exception:
            pass
        try:
            if getattr(self, 'world', None):
                self.world.close()
                self.world = None
        except Exception:
            pass
        if cancel:
            try:
                if getattr(self, 'bake_cache', None):
                    self.bake_cache.close()
                    self.bake_cache = None
            except Exception:
                pass
            for o in getattr(self, 'created_objects', []):
                try:
                    if _is_live_object(o):
                        bpy.data.objects.remove(o, do_unlink=True)
                except Exception:
                    pass
            action = getattr(self, 'key_action', None)
            try:
                if action is not None and action.users == 0:
                    bpy.data.actions.remove(action)
            except (ReferenceError, RuntimeError):
                pass
            collection = getattr(self, 'collection', None)
            try:
                if collection is not None and collection.get('Tumblify_SpawnSession', False) and (not collection.objects) and (not collection.children):
                    removed_name = collection.name
                    bpy.data.collections.remove(collection)
                    if str(self.scene.get('Tumblify_LastSpawnCollection', '')) == removed_name:
                        previous = str(getattr(self, '_previous_last_spawn_collection', ''))
                        if previous and bpy.data.collections.get(previous) is not None:
                            self.scene['Tumblify_LastSpawnCollection'] = previous
                        else:
                            _repair_last_spawn_collection(self.scene)
            except (ReferenceError, RuntimeError):
                pass
            master = getattr(self, '_created_spawn_master', None)
            try:
                if master is not None and (not master.objects) and (not master.children):
                    bpy.data.collections.remove(master)
            except (ReferenceError, RuntimeError):
                pass
        files = getattr(self, 'bake_cache_files', None)
        if files is not None:
            try:
                retained = files.finish(bool(getattr(self, 'bake_keep_cache', True)), cancel)
                if retained:
                    manifest = str(files.directory / 'manifest.json')
                    self.scene['Tumblify_LastBakeCache'] = manifest
                    self.report({'INFO'}, f'Emitter Bake cache retained: {manifest}')
            except Exception as exc:
                self.report({'WARNING'}, f'Bake cache files left at {files.directory}: {exc}')
            finally:
                self.bake_cache_files = None
        try:
            display=getattr(self,'_calculation_progress',None)
            if display is not None:
                if not cancel:display.update(100,'Complete',force=True)
                display.close();self._calculation_progress=None
            else:
                context.window_manager.progress_end()
        except Exception:
            pass
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass
        _solid_state_restore()
        _RUNTIME = None
        STOP_REQUESTED = False

    def _spawn_spec(self, st):
        src = _weighted_source_choice(self.sources)
        smin = min(float(st.random_scale_min), float(st.random_scale_max))
        smax = max(float(st.random_scale_min), float(st.random_scale_max))
        sc = random.uniform(smin, smax) if smax > smin else smin
        if self.surface_mode:
            (surface_p, normal) = _scatter_sample_world(self.surface_emitter, self.sampler, self.sampler_total)
            surface_p,normal=_surface_project_to_proxy(getattr(self,'surface_projection',None),surface_p,normal)
            pdata = self.source_collision[src.name]
            half = Vector(pdata.get('half', (0.1, 0.1, 0.1))) * sc
            offset = max(0.005, min(2.0, max(half) * 0.72))
            target = surface_p + normal * offset
            p = target + normal * 0.002
            q = normal.to_track_quat('Z', 'Y')
            angles = (random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_x else 0.0, random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_y else 0.0, random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_z else 0.0)
            q = (q @ Euler(angles, 'XYZ').to_quaternion()).normalized()
        else:
            p = _fill_choose_point(getattr(self, 'fill_emitters', []), self.source_collision, src, sc) if bool(st.fill_emitter) else None
            if p is None:
                if st.fill_emitter:raise RuntimeError(f"Fill Emitter: no safe space for '{src.name}'. Reduce its scale or increase the emitter volume")
                p = _offline_volume_spawn_point(self.scene, self.emitter_objs)
            (_, bq, _) = world_pose_scale(src)
            q = Quaternion((bq[3], bq[0], bq[1], bq[2]))
            angles = (random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_x else 0.0, random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_y else 0.0, random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_z else 0.0)
            q = (q @ Euler(angles, 'XYZ').to_quaternion()).normalized()
            target = None
        return (src, p, q, sc, target)

    def invoke(self, context, event):
        global _RUNTIME, STOP_REQUESTED
        if context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, 'Run from a 3D View')
            return {'CANCELLED'}
        if _RUNTIME is not None:
            self.report({'WARNING'}, 'Stop the current simulation first')
            return {'CANCELLED'}
        st = context.scene.tumblify
        self.scene = context.scene
        self.depsgraph = context.evaluated_depsgraph_get()
        self.frame_start = int(st.emitter_bake_frame_start)
        self.output_fps = float(context.scene.render.fps) / max(1e-06, float(context.scene.render.fps_base))
        self.requested_bake_frames = max(2, int(st.emitter_bake_frames))
        if self.bake_frames and (not getattr(get_lib(), 'has_fast_drop', False)):
            self.report({'ERROR'}, 'Emitter Bake requires the rebuilt native DLL. Reinstall the Tumblify release package and restart Blender.')
            return {'CANCELLED'}
        if self.bake_frames and (not getattr(get_lib(), 'has_exact_frame_bake', False)):
            self.report({'ERROR'}, 'Emitter Bake requires the updated native DLL for exact frame ranges. Install the complete release and restart Blender.')
            return {'CANCELLED'}
        self.sources = _scene_role_objects(context.scene, 'SOURCE')
        if not self.sources:
            self.report({'ERROR'}, 'Add at least one Dynamic Source')
            return {'CANCELLED'}
        bad = [o.name for o in self.sources if o.tumblify.shape in {'MESH', 'TERRAIN'}]
        if bad:
            self.report({'ERROR'}, f"Mesh collision is static-only. Change source proxy: {', '.join(bad[:3])}")
            return {'CANCELLED'}
        self.surface_mode = st.spawn_location == 'SURFACE'
        self.emitter_objs = []
        self.surface_emitter = None
        if self.surface_mode:
            if st.emitter_collection:
                self.report({'ERROR'}, 'Surface spawning requires one Mesh emitter object, not a Collection')
                return {'CANCELLED'}
            emitter = st.emitter_object
            if not emitter or emitter.name not in context.scene.objects:
                emitter = ensure_brush(context.scene)
                st.emitter_object = emitter
                st.emitter_mode = 'FIXED'
            if emitter.type != 'MESH':
                self.report({'ERROR'}, 'Surface spawning requires a Mesh emitter')
                return {'CANCELLED'}
            self.surface_emitter = emitter
            self.emitter_objs = [emitter]
        else:
            self.emitter_objs = _configured_emitter_objects(context.scene)
            if not self.emitter_objs:
                brush = ensure_brush(context.scene)
                self.emitter_objs = [brush]
        self.requested_total = max(1, int(st.drop_offline_count))
        self.total = self.requested_total
        if self.bake_frames:
            bake_duration = max(0.0, float(self.requested_bake_frames - 1) / max(1e-06, self.output_fps))
            possible = max(1, 1 + int(math.floor(bake_duration * max(1.0, float(st.spawn_rate)) + 1e-09)))
            self.total = min(self.requested_total, possible)
        self.bake_cache_files = None
        self.bake_keep_cache = bool(st.emitter_bake_cache_keep)
        (self.bake_backend, self.bake_estimated_bytes) = select_backend(st.emitter_bake_cache_mode, self.total, self.requested_bake_frames, st.emitter_bake_cache_memory_mb)
        if self.bake_frames and self.bake_backend == 'DISK' and (not getattr(get_lib(), 'has_disk_bake', False)):
            self.report({'ERROR'}, 'Disk Emitter Bake requires its updated native DLL. Install the complete release and restart Blender.')
            return {'CANCELLED'}
        self.bake_cache_root = None
        if self.bake_frames and self.bake_backend == 'DISK':
            import os, tempfile
            folder = st.emitter_bake_cache_directory or '//Tumblify_Cache/'
            self.bake_cache_root = os.path.join(tempfile.gettempdir(), 'Tumblify_Cache') if folder.startswith('//') and (not bpy.data.filepath) else bpy.path.abspath(folder)
        self.created = 0
        self.active_count = 0
        self.sim_step = 0
        self.handles = []
        self.source_names = []
        self.scales = []
        self.targets = []
        self.created_objects = []
        self.drop_job = None
        self.bake_cache = None
        self._previous_last_spawn_collection = str(self.scene.get('Tumblify_LastSpawnCollection', ''))
        master_before = _spawn_master_collection(self.scene, False)
        self.collection = spawned_collection(context.scene, True, 'Drop')
        self._created_spawn_master = _spawn_master_collection(self.scene, False) if master_before is None else None
        try:
            self._calculation_progress=_RealtimePreparation(context,'Bake' if self.bake_frames else 'Offline',action='Calculating simulation',synchronous=False)
            self._calculation_progress.begin()
            self.simplify = st.convex_simplify
            self.world = _configure_world(World(st.physics_threads), st)
            self.static_objs = [o for o in _advanced_runtime_static_objects(context.scene) if o != self.surface_emitter]
            self.frozen_objs = list(_frozen_spawned(context.scene))
            self.instance_results = list(_frozen_instance_results(context.scene))
            self.source_collision = {}
            self.fill_emitters = []
            self.fill_container_handles = []
            self.frozen_cache = {}
            self.instance_cache = {}
            self.prep_stage = 'WORLD'
            self.prep_index = 0
            self.prep_done = 0
            self.prep_total = 1 + (1 if self.surface_mode else 0) + len(self.static_objs) + len(self.sources) + (len(self.emitter_objs) if not self.surface_mode and bool(st.fill_emitter) else 0) + len(self.frozen_objs) + len(self.instance_results) + 1
            self.phase = 'PREPARE'
            STOP_REQUESTED = False
            _set_runtime_owner(self, context)
            self._timer = context.window_manager.event_timer_add(0.01, window=context.window)
            context.window_manager.modal_handler_add(self)
            label = 'Emitter Bake' if self.bake_frames else 'Offline Drop'
            self._status(context, f'Tumblify {label}: preparing physics world…', 1)
            return {'RUNNING_MODAL'}
        except Exception as e:
            self._cleanup(context, True)
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

    def _prep_progress(self, context, text):
        label = 'Emitter Bake' if self.bake_frames else 'Offline Drop'
        frac = self.prep_done / max(1, self.prep_total)
        self._status(context, f'Tumblify {label}: {text}', 1.0 + 19.0 * frac)

    def _prepare_chunk(self, context):
        st = self.scene.tumblify
        simplify = self.simplify
        started = time.perf_counter()
        while self.phase == 'PREPARE' and time.perf_counter() - started < 0.03:
            stage = self.prep_stage
            if stage == 'WORLD':
                self._prep_progress(context, 'setting up floor and simulation bounds…')
                _add_builtin_floor(self.world, st)
                self.prep_done += 1
                self.prep_stage = 'SURFACE' if self.surface_mode else 'STATIC'
                self.prep_index = 0
            elif stage == 'SURFACE':
                self._prep_progress(context, f'preparing surface emitter collision: {self.surface_emitter.name}')
                (self.sampler, self.sampler_total) = _scatter_surface_sampler(self.surface_emitter, st.scatter_vertex_group)
                pdata = _prepare_collision(self.surface_emitter, self.depsgraph, simplify, shape_override='MESH' if self.surface_emitter.tumblify.shape=='DETECT' else None)
                self.surface_projection=_fill_build_emitter_data(self.surface_emitter,self.depsgraph,pdata) if pdata['shape'] in {'HULL','BOX','SPHERE','CAPSULE','CYLINDER'} else None
                add_body(self.world, World.STATIC, self.surface_emitter, self.depsgraph, simplify=simplify, prepared=pdata, friction=st.static_friction, restitution=0.0, density=0.0)
                self.prep_done += 1
                self.prep_stage = 'STATIC'
                self.prep_index = 0
            elif stage == 'STATIC':
                if self.prep_index < len(self.static_objs):
                    o = self.static_objs[self.prep_index]
                    self._prep_progress(context, f'preparing static colliders {self.prep_index + 1}/{len(self.static_objs)} — {o.name}')
                    pdata = _prepare_collision(o, self.depsgraph, simplify)
                    add_body(self.world, World.STATIC, o, self.depsgraph, simplify=simplify, prepared=pdata, friction=st.static_friction, restitution=0.0, density=0.0)
                    self.prep_index += 1
                    self.prep_done += 1
                else:
                    self.prep_stage = 'SOURCES'
                    self.prep_index = 0
            elif stage == 'SOURCES':
                if self.prep_index < len(self.sources):
                    o = self.sources[self.prep_index]
                    self._prep_progress(context, f'preparing source collisions {self.prep_index + 1}/{len(self.sources)} — {o.name}')
                    pdata = _prepare_collision(o, self.depsgraph, simplify)
                    _cache_native_collision_assets(self.world, pdata)
                    self.source_collision[o.name] = pdata
                    self.prep_index += 1
                    self.prep_done += 1
                else:
                    self.prep_stage = 'FILL'
                    self.prep_index = 0
            elif stage == 'FILL':
                if not self.surface_mode and bool(st.fill_emitter) and (self.prep_index < len(self.emitter_objs)):
                    eo = self.emitter_objs[self.prep_index]
                    self._prep_progress(context, f'building emitter container {self.prep_index + 1}/{len(self.emitter_objs)} — {eo.name}')
                    data = _fill_build_emitter_data(eo, self.depsgraph)
                    if data is not None:
                        self.fill_emitters.append(data)
                        self.fill_container_handles.extend(_fill_add_container(self.world, data, st.static_friction))
                    self.prep_index += 1
                    self.prep_done += 1
                else:
                    if not self.surface_mode and bool(st.fill_emitter) and (not self.fill_emitters):
                        raise RuntimeError('Fill Emitter could not build a usable emitter container')
                    self.prep_stage = 'FROZEN'
                    self.prep_index = 0
            elif stage == 'FROZEN':
                if self.prep_index < len(self.frozen_objs):
                    o = self.frozen_objs[self.prep_index]
                    self._prep_progress(context, f'loading previous Drop objects {self.prep_index + 1}/{len(self.frozen_objs)} — {o.name}')
                    src_name = str(o.get('Tumblify_SourceName', ''))
                    src = bpy.data.objects.get(src_name)
                    pdata = self.source_collision.get(src_name)
                    if pdata is None:
                        key = src_name or o.name
                        pdata = self.frozen_cache.get(key)
                        if pdata is None:
                            pdata = _prepare_collision(src or o, self.depsgraph, simplify)
                            _cache_native_collision_assets(self.world, pdata)
                            self.frozen_cache[key] = pdata
                    (p0, q0, _) = world_pose_scale(o)
                    sc = float(o.get('Tumblify_InstanceScale', 1.0)) if src else 1.0
                    add_body(self.world, World.STATIC, src or o, self.depsgraph, p_override=p0, q_override=q0, simplify=simplify, prepared=pdata, instance_scale=sc, friction=st.static_friction, restitution=0.0, density=0.0)
                    self.prep_index += 1
                    self.prep_done += 1
                else:
                    self.prep_stage = 'INSTANCES'
                    self.prep_index = 0
            elif stage == 'INSTANCES':
                if self.prep_index < len(self.instance_results):
                    inst = self.instance_results[self.prep_index]
                    self._prep_progress(context, f'loading previous instance results {self.prep_index + 1}/{len(self.instance_results)} — {inst.name}')
                    src_name = str(inst.get('Tumblify_SourceName', ''))
                    src = bpy.data.objects.get(src_name)
                    helper = _instance_result_helper(inst)
                    material_obj = src or helper
                    if material_obj is not None:
                        pdata = self.source_collision.get(src_name) if src_name else None
                        if pdata is None:
                            key = src_name or inst.name
                            pdata = self.instance_cache.get(key)
                            if pdata is None:
                                pdata = _prepare_collision(material_obj, self.depsgraph, simplify)
                                _cache_native_collision_assets(self.world, pdata)
                                self.instance_cache[key] = pdata
                        for (pose, sc) in _instance_result_entries(inst):
                            p = Vector(pose[:3])
                            q = Quaternion((pose[6], pose[3], pose[4], pose[5]))
                            add_body(self.world, World.STATIC, material_obj, self.depsgraph, p_override=p, q_override=q, simplify=simplify, prepared=pdata, instance_scale=sc, friction=st.static_friction, restitution=0.0, density=0.0)
                    self.prep_index += 1
                    self.prep_done += 1
                else:
                    self.prep_stage = 'FINALIZE'
                    self.prep_index = 0
            elif stage == 'FINALIZE':
                self._prep_progress(context, 'finalizing simulation timing…')
                self.dt = 1.0 / max(1.0, float(st.physics_frequency))
                rate = max(1.0, float(st.spawn_rate))
                self.spawn_duration = max(0.0, float(self.total - 1) / rate)
                if self.bake_frames:
                    requested_duration = max(0.0, float(self.requested_bake_frames - 1) / max(1e-06, self.output_fps))
                    self.effective_settle = max(0.0, requested_duration - self.spawn_duration)
                    self.end_time = requested_duration
                else:
                    self.effective_settle = max(0.0, float(st.drop_offline_settle))
                    self.end_time = self.spawn_duration + self.effective_settle
                self.total_steps = max(1, int(math.ceil(self.end_time / self.dt)) + 1)
                self.prep_done += 1
                self.phase = 'CREATE'
                label = 'Emitter Bake' if self.bake_frames else 'Offline Drop'
                self._status(context, f'Tumblify {label}: creating physics bodies 0/{self.total}', 20)

    def _create_chunk(self, context):
        st = self.scene.tumblify
        started = time.perf_counter()
        while self.created < self.total and time.perf_counter() - started < 0.03:
            (src, p, q, sc, target) = self._spawn_spec(st)
            (mf, mr, md) = _physics_material(src)
            h = add_body(self.world, World.DYNAMIC, src, self.depsgraph, p, (q.x, q.y, q.z, q.w), st.convex_simplify, prepared=self.source_collision[src.name], instance_scale=sc, friction=mf, restitution=mr, density=md)
            self.world.enabled(h, False)
            self.handles.append(h)
            self.source_names.append(src.name)
            self.scales.append(float(sc))
            self.targets.append(tuple(target) if target is not None else None)
            self.created += 1
        p = 20.0 + 15.0 * (self.created / max(1, self.total))
        label = 'Emitter Bake' if self.bake_frames else 'Offline Drop'
        self._status(context, f'Tumblify {label}: creating physics bodies {self.created}/{self.total}', p)
        if self.created >= self.total:
            self.sim_step = 0
            self.active_count = 0
            api = get_lib()
            if getattr(api, 'has_fast_drop', False):
                try:
                    cache_options = {'output_frames': self.requested_bake_frames} if self.bake_frames else {}
                    if self.bake_frames and self.bake_backend == 'DISK':
                        self.bake_cache_files = BakeCacheFiles(self.bake_cache_root, self.bake_estimated_bytes, {'addon_version': '.'.join(map(str, ADDON_VERSION)), 'blend_file': bpy.data.filepath, 'scene': self.scene.name, 'frame_start': self.frame_start, 'requested_frames': self.requested_bake_frames, 'spawn_rate': float(st.spawn_rate), 'sources': list(self.source_names), 'instance_scales': [float(v) for v in self.scales], 'quaternion_layout': 'xyzw', 'position_units': 'Blender scene units', 'note': 'Transforms only; source geometry remains in the associated Blender scene'})
                        cache_options['cache_path'] = str(self.bake_cache_files.partial)
                    self.drop_job = self.world.drop_bake_begin(self.handles, self.targets if self.surface_mode else None, float(st.spawn_rate), float(self.effective_settle), float(st.physics_frequency), int(st.physics_substeps), float(st.scatter_magnet_strength), float(st.scatter_magnet_damping), bool(self.surface_mode and st.scatter_magnetized), bool(self.bake_frames), float(self.output_fps), **cache_options)
                except Exception as exc:
                    raise RuntimeError(f'Emitter Bake native start failed: {exc}') from exc
                self.phase = 'NATIVE_SIMULATE'
            else:
                self.phase = 'SIMULATE'

    def _native_simulate_poll(self, context):
        job = self.drop_job
        if job is None:
            raise RuntimeError('Offline Drop native job was lost')
        state = job.state
        frac = max(0.0, min(1.0, float(job.progress) / 1000.0))
        backend_label = f' [{self.bake_backend.title()} cache]' if self.bake_frames else ''
        self._status(context, f"Tumblify {('Emitter Bake' if self.bake_frames else 'Offline Drop')}: simulating {int(frac * 100)}%{backend_label}", 35.0 + 55.0 * frac)
        if state == 0:
            return False
        if state == -2:
            raise RuntimeError('Offline Drop native simulation was cancelled')
        if state < 0:
            raise RuntimeError(job.error or 'Offline Drop native simulation failed')
        self.final_transforms = job.transforms()
        if not self.bake_frames:
            self.total = len(self.final_transforms)
        self.bake_cache = job.take_cache() if self.bake_frames else None
        if self.bake_frames and self.bake_cache_files is not None:
            if self.bake_cache is None:
                raise RuntimeError('Native disk bake did not return a frame cache')
            self.bake_cache_files.mark_simulated(self.bake_cache.frame_count, self.bake_cache.body_count, self.bake_cache.fps)
        job.close()
        self.drop_job = None
        self.world.close()
        self.world = None
        self.phase = 'MATERIALIZE'
        self.materialized = 0
        return False

    def _simulate_chunk(self, context):
        st = self.scene.tumblify
        rate = max(1.0, float(st.spawn_rate))
        started = time.perf_counter()
        while self.sim_step < self.total_steps and time.perf_counter() - started < 0.035:
            t = float(self.sim_step) * self.dt
            wanted = min(self.total, 1 + int(t * rate))
            while self.active_count < wanted:
                self.world.enabled(self.handles[self.active_count], True)
                self.active_count += 1
            if self.surface_mode and st.scatter_magnetized and self.active_count:
                self.world.point_springs(self.handles[:self.active_count], self.targets[:self.active_count], st.scatter_magnet_strength, st.scatter_magnet_damping, 1.0)
            self.world.step(self.dt, int(st.physics_substeps))
            self.sim_step += 1
        frac = self.sim_step / max(1, self.total_steps)
        p = 35.0 + 55.0 * frac
        self._status(context, f"Tumblify {('Emitter Bake' if self.bake_frames else 'Offline Drop')}: simulating {min(100, int(frac * 100))}%  ({self.active_count}/{self.total} bodies)", p)
        if self.sim_step >= self.total_steps:
            (vals, _) = self.world.get_bulk(self.handles)
            self.final_transforms = [list(v) for v in vals]
            if not self.bake_frames:
                self.total = len(self.final_transforms)
            self.world.close()
            self.world = None
            self.phase = 'MATERIALIZE'
            self.materialized = 0

    def _materialize_chunk(self, context):
        st = self.scene.tumblify
        started = time.perf_counter()
        if st.result_output == 'INSTANCES' and (not self.bake_frames) and (self.materialized == 0):
            display=getattr(self,'_calculation_progress',None)
            if display is not None:display.update(90,'Creating Combined results',force=True)
            groups = {}
            for (src_name, v, sc) in zip(self.source_names, self.final_transforms, self.scales):
                src = bpy.data.objects.get(src_name)
                if src is None:
                    continue
                g = groups.setdefault(src_name, {'src': src, 'transforms': [], 'scales': []})
                g['transforms'].append(list(v))
                g['scales'].append(float(sc))
            n = 0
            created_results = []
            for g in groups.values():
                result = _create_instance_result(self.scene, self.collection, g['src'], g['transforms'], g['scales'], self.depsgraph)
                if result is not None:
                    created_results.append(result)
                    n += len(g['transforms'])
            try:
                bpy.ops.object.select_all(action='DESELECT')
                for result in created_results:
                    if _is_live_object(result):
                        result.hide_select = False
                        result.hide_viewport = False
                        result.hide_set(False)
                        result.select_set(True)
                if created_results:
                    context.view_layer.objects.active = created_results[0]
            except Exception:
                pass
            self.materialized = self.total
            self._cleanup(context, False)
            msg = f'Offline Drop finished: {n} instances'
            self.report({'INFO'}, msg)
            return True
        while self.materialized < self.total and time.perf_counter() - started < 0.03:
            i = self.materialized
            src = bpy.data.objects.get(self.source_names[i])
            v = self.final_transforms[i]
            if src is not None:
                q = Quaternion((v[6], v[3], v[4], v[5]))
                (_, _, base_scale) = world_pose_scale(src)
                dup = src.copy()
                if dup.animation_data:
                    dup.animation_data_clear()
                dup.parent = None
                dup.tumblify.role = 'NONE'
                dup.hide_select = False
                dup.hide_viewport = True
                dup.hide_render = True
                self.collection.objects.link(dup)
                dup.matrix_world = Matrix.LocRotScale(Vector((v[0], v[1], v[2])), q, Vector(base_scale) * self.scales[i])
                dup['Tumblify_SourceName'] = src.name
                dup['Tumblify_InstanceScale'] = self.scales[i]
                dup[FROZEN_PROP] = True
                dup[RUNTIME_PROP] = True
                dup[FIXED_PROP] = False
                self.created_objects.append(dup)
            self.materialized += 1
        p = 90.0 + 8.0 * (self.materialized / max(1, self.total))
        label = 'Emitter Bake' if self.bake_frames else 'Offline Drop'
        self._status(context, f'Tumblify {label}: creating Blender results {self.materialized}/{self.total}', p)
        if self.materialized >= self.total:
            n = len(self.created_objects)
            try:
                _apply_global_instance_display(self.scene)
            except Exception:
                pass
            if self.bake_frames:
                if self.bake_cache is None:
                    raise RuntimeError('Emitter Bake completed without a frame cache')
                if self.bake_cache_files is not None:
                    self.bake_cache_files.record_objects((o.name for o in self.created_objects))
                self.phase = 'KEYFRAMES'
                self.key_index = 0
                try:
                    (self.key_action, self.key_strip) = _bake_new_container(self.scene, EMITTER_BAKE_ACTION_PREFIX, 'Tumblify Emitter Bake')
                except Exception as exc:
                    raise RuntimeError(f'Emitter Bake action setup failed: {exc}') from exc
                self._status(context, f'Tumblify Emitter Bake: writing keyframes 0/{n}', 98.0)
                return False
            self._cleanup(context, False)
            msg = f'Offline Drop finished: {n} frozen objects'
            self.report({'INFO'}, msg)
            return True
        return False

    def _add_spawn_visibility(self, obj, index):
        try:
            ad = obj.animation_data
            slot = ad.action_slot if ad else None
            if slot is None:
                return
            bag = self.key_strip.channelbag(slot, ensure=True)
            spawn_frame = float(self.frame_start) + float(index) / max(1.0, float(self.scene.tumblify.spawn_rate)) * float(self.output_fps)
            for path in ('hide_viewport', 'hide_render'):
                fcu = bag.fcurves.new(data_path=path, index=0, group_name='Tumblify Emitter Bake')
                if spawn_frame <= float(self.frame_start) + 1e-06:
                    pts = fcu.keyframe_points
                    pts.add(1)
                    pts[0].co = (float(self.frame_start), 0.0)
                    pts[0].interpolation = 'CONSTANT'
                else:
                    pts = fcu.keyframe_points
                    pts.add(2)
                    pts[0].co = (max(float(self.frame_start), spawn_frame - 1.0), 1.0)
                    pts[0].interpolation = 'CONSTANT'
                    pts[1].co = (spawn_frame, 0.0)
                    pts[1].interpolation = 'CONSTANT'
        except Exception:
            pass

    def _keyframe_chunk(self, context):
        cache = self.bake_cache
        if cache is None:
            raise RuntimeError('Emitter Bake cache was lost')
        started = time.perf_counter()
        n = len(self.created_objects)
        while self.key_index < n and time.perf_counter() - started < 0.035:
            i = self.key_index
            o = self.created_objects[i]
            if _is_live_object(o):
                try:
                    track = cache.body_track(i)
                except Exception as exc:
                    raise RuntimeError(f'Emitter Bake cache read failed for body {i}: {exc}') from exc
                try:
                    _bake_create_action(o, track, self.frame_start, self.key_action, self.key_strip)
                except Exception as exc:
                    raise RuntimeError(f'Emitter Bake keyframe write failed for body {i} ({o.name}): {exc}') from exc
                o[EMITTER_BAKE_PROP] = True
                self._add_spawn_visibility(o, i)
                o[FROZEN_PROP] = True
            self.key_index += 1
        frac = self.key_index / max(1, n)
        self._status(context, f'Tumblify Emitter Bake: writing keyframes {self.key_index}/{n}', 98.0 + 2.0 * frac)
        if self.key_index < n:
            return False
        try:
            self.key_action.update_tag()
        except Exception:
            pass
        frame_count = int(cache.frame_count)
        end_frame = self.frame_start + frame_count - 1
        try:
            cache.close()
        except Exception:
            pass
        self.bake_cache = None
        self._cleanup(context, False)
        self.report({'INFO'}, f'Emitter Bake finished: {n} objects, frames {self.frame_start}–{end_frame}')
        return True

    def _modal_impl(self, context, event):
        global STOP_REQUESTED
        if event.type == 'ESC' or STOP_REQUESTED:
            self._cleanup(context, True)
            self.report({'INFO'}, 'Offline Drop cancelled')
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        try:
            if self.phase == 'PREPARE':
                self._prepare_chunk(context)
            elif self.phase == 'CREATE':
                self._create_chunk(context)
            elif self.phase == 'NATIVE_SIMULATE':
                self._native_simulate_poll(context)
            elif self.phase == 'SIMULATE':
                self._simulate_chunk(context)
            elif self.phase == 'MATERIALIZE' and self._materialize_chunk(context):
                return {'FINISHED'}
            elif self.phase == 'KEYFRAMES' and self._keyframe_chunk(context):
                return {'FINISHED'}
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        except Exception as e:
            self._cleanup(context, True)
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

    def modal(self, context, event):
        return _dispatch_runtime_modal(self, context, event, 'Tumblify Offline simulation')

class TU_OT_emitter_clear_bake(bpy.types.Operator):
    bl_idname='tumblify.emitter_clear_bake'; bl_label='Clear Emitter Keyframes'; bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        targets=[]
        for o in context.scene.objects:
            ad=getattr(o,'animation_data',None); act=ad.action if ad else None
            if bool(o.get(EMITTER_BAKE_PROP,False)) or (act and act.name.startswith(EMITTER_BAKE_ACTION_PREFIX)): targets.append(o)
        for o in targets: _clear_bake_object(o)
        for act in list(bpy.data.actions):
            try:
                if act.name.startswith(EMITTER_BAKE_ACTION_PREFIX) and act.users==0: bpy.data.actions.remove(act)
            except Exception: pass
        try: context.scene.frame_set(context.scene.frame_current)
        except Exception: pass
        self.report({'INFO'},f'Cleared Emitter Bake keyframes from {len(targets)} object(s)')
        return {'FINISHED'}

class TU_OT_emitter_bake(bpy.types.Operator):
    bl_idname='tumblify.emitter_bake'; bl_label='Bake Emitter to Keyframes'; bl_options={'REGISTER'}
    def invoke(self,context,event):
        return bpy.ops.tumblify.drop_offline('INVOKE_DEFAULT',bake_frames=True)

class TU_OT_surface_run(bpy.types.Operator):
    bl_idname = 'tumblify.surface_run'
    bl_label = 'Start Advanced (Surface)'
    bl_options = {'REGISTER', 'INTERNAL'}
    _timer = None

    def invoke(self, context, event):
        global _RUNTIME, STOP_REQUESTED
        self.fast_runtime = False
        if context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, 'Run from a 3D View')
            return {'CANCELLED'}
        if _RUNTIME is not None:
            self.report({'WARNING'}, 'Stop the current simulation first')
            return {'CANCELLED'}
        st = context.scene.tumblify
        if st.emitter_collection:
            self.report({'ERROR'}, 'Surface spawning requires a single Mesh emitter object, not an emitter Collection')
            return {'CANCELLED'}
        emitter = st.emitter_object
        (emitter, migrated) = resolve_surface_emitter(st, context.scene, emitter)
        if migrated:
            st.emitter_object = emitter
            st.emitter_mode = 'CUSTOM'
        if not emitter or emitter.name not in context.scene.objects:
            emitter = ensure_brush(context.scene)
            st.emitter_object = emitter
            st.emitter_mode = 'FIXED'
        if emitter.type != 'MESH':
            self.report({'ERROR'}, 'Surface spawning requires a Mesh emitter')
            return {'CANCELLED'}
        self.emitter = emitter
        sources = _scene_role_objects(context.scene, 'SOURCE')
        if not sources:
            self.report({'ERROR'}, 'Add at least one Dynamic Source')
            return {'CANCELLED'}
        bad = [o.name for o in sources if o.tumblify.shape in {'MESH', 'TERRAIN'}]
        if bad:
            self.report({'ERROR'}, f"Mesh collision is static-only. Change source proxy: {', '.join(bad[:3])}")
            return {'CANCELLED'}
        wm = context.window_manager
        prep=_RealtimePreparation(context,'Advanced / Surface')
        try:
            prep.begin()
            prep.update(5,'Building emission surface')
            try:
                context.workspace.status_text_set('Tumblify: Preparing Surface simulation… 5%')
            except Exception:
                pass
            self.depsgraph = context.evaluated_depsgraph_get()
            (self.sampler, self.sampler_total) = _scatter_surface_sampler(emitter, st.scatter_vertex_group)
            prep.update(20,'Creating physics world')
            try:
                context.workspace.status_text_set('Tumblify: Building surface mask… 20%')
            except Exception:
                pass
            simplify = st.convex_simplify
            self.world = _configure_world(World(st.physics_threads), st)
            _add_builtin_floor(self.world, st)
            self.emitter_collision = _prepare_collision(emitter, self.depsgraph, simplify, shape_override='MESH' if emitter.tumblify.shape=='DETECT' else None)
            self.surface_projection=_fill_build_emitter_data(emitter,self.depsgraph,self.emitter_collision) if self.emitter_collision['shape'] in {'HULL','BOX','SPHERE','CAPSULE','CYLINDER'} else None
            self.emitter_handle = add_body(self.world, World.STATIC, emitter, self.depsgraph, simplify=simplify, prepared=self.emitter_collision, friction=st.static_friction, restitution=0.0, density=0.0)
            prep.update(45,'Preparing static colliders')
            try:
                context.workspace.status_text_set('Tumblify: Building static colliders… 45%')
            except Exception:
                pass
            self.static_handles = []
            self.static_objs = []
            self.static_collision = {}
            static_objs = [o for o in _advanced_runtime_static_objects(context.scene) if o != emitter]
            total_static = max(1, len(static_objs))
            for (idx, o) in enumerate(static_objs):
                pdata = _prepare_collision(o, self.depsgraph, simplify)
                self.static_handles.append(add_body(self.world, World.STATIC, o, self.depsgraph, simplify=simplify, prepared=pdata, friction=st.static_friction, restitution=0.0, density=0.0))
                self.static_objs.append(o)
                self.static_collision[o.name] = pdata
                prep.update(45+20*(idx+1)/total_static,f'Preparing static colliders {idx+1}/{len(static_objs)}')
            self.sources = sources
            self.source_collision = {}
            total_sources = max(1, len(sources))
            for (idx, o) in enumerate(sources):
                self.source_collision[o.name] = _prepare_collision(o, self.depsgraph, simplify)
                prep.update(65+15*(idx+1)/total_sources,f'Preparing dynamic collisions {idx+1}/{len(sources)}')
            for o in sources:
                d = self.source_collision[o.name]
                _cache_native_collision_assets(self.world, d)
            _add_instance_results_as_static(self.world, context.scene, self.depsgraph, self.source_collision, simplify, st.static_friction,owner=self,progress=prep,progress_range=(78,84))
            prep.update(85,'Preparing viewport display')
            try:
                context.workspace.status_text_set('Tumblify: Preparing instance display… 82%')
            except Exception:
                pass
            self.collection = spawned_collection(context.scene, True, 'Drop')
            self.fast_mode = st.spawn_backend == 'FAST'
            self.fast_groups = {}
            self.dynamic_cache = []
            if self.fast_mode:
                _clear_fast_collection()
                try:
                    self.fast_groups = {o.name: _new_fast_proxy(context.scene, o, self.depsgraph, force_mesh=bool(getattr(self, 'fast_runtime', False))) for o in sources}
                except Exception as e:
                    _clear_fast_collection()
                    raise RuntimeError(f'Realtime display setup failed: {e}.')
            prep.update(95,'Starting simulation')
            self.dynamic_objs = []
            self.dynamic_handles = []
            self.dynamic_targets = []
            self.dynamic_source_names = []
            self.dynamic_scales = []
            self.session_objects = []
            self.session_indices = set()
            self.kinematic_indices = set()
            self.static_kinematic_indices = set()
            self.moving_selected = False
            self.pending_move_finish = False
            self.spawn_acc = 0.0
            self.dt = 1.0 / max(1.0, float(st.physics_frequency))
            self._timer_dt = self.dt
            self.spawning = False
            self.live_interactive = False
            self.stopping = False
            self.stop_remaining = 0.0
            STOP_REQUESTED = False
            _set_runtime_owner(self, context)
            self._region = context.region
            self._rv3d = context.space_data.region_3d
            self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(self._draw_hotkeys, (), 'WINDOW', 'POST_PIXEL')
            self._timer = context.window_manager.event_timer_add(self._timer_dt, window=context.window)
            context.window_manager.modal_handler_add(self)
            prep.update(100,'Ready',force=True)
            self.report({'INFO'}, f'Advanced / Surface | E toggles spawn | Shift+R random rotation | Q rotation axes | Enter keeps result | Esc cancels')
            return {'RUNNING_MODAL'}
        except Exception as e:
            try:
                if hasattr(self, 'world'):
                    self.world.close()
                _clear_fast_collection()
            except Exception:
                pass
            try:
                if getattr(self, '_draw_handle', None):
                    bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
                    self._draw_handle = None
            except Exception:
                pass
            _solid_state_restore()
            _RUNTIME = None
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        finally:
            prep.close()

    def _draw_hotkeys(self):
        try:
            font = 0
            st = bpy.context.scene.tumblify
            if getattr(self, 'live_interactive', False):
                lines = ['INTERACTIVE', 'G / R  Move / rotate physics objects / colliders / Fill emitter', 'Shift+Click  Add/toggle selection', 'LMB drag  Box Select   Shift add / Ctrl subtract', 'Moved convex colliders release as dynamic bodies', 'Shift+E  Return to Drop', 'Enter  Finish / keep result', 'Esc  Cancel / delete this run']
            elif getattr(self, 'stopping', False):
                lines = [f'Settling… {max(0.0, float(self.stop_remaining)):.1f}s', 'Spawning is stopped', 'Shift+E  Live Interactive', 'Enter  Finish / keep result', 'Esc  Cancel / delete this run']
            else:
                lines = [f"E  Spawn: {('ON' if self.spawning else 'OFF')}", f'Spawn rate: {float(st.spawn_rate):.1f}/s', 'Shift+E  Live Interactive', 'Enter  Finish / keep result', 'Esc  Cancel / delete this run']
            if getattr(self,'fast_snap_enabled',False) and not getattr(self,'live_interactive',False) and not getattr(self,'fast_snap_valid',False):lines.insert(1,'No surface under cursor')
            if getattr(self,'fast_runtime',False) and not getattr(self,'live_interactive',False) and time.monotonic()<getattr(self,'fast_scale_label_until',0.0):
                point=view3d_utils.location_3d_to_region_2d(self._region,self._rv3d,Vector(self.fast_preview_target))
                if point is not None:
                    label=f"Scale  {float(self.fast_preview_scale):.2f}x"
                    blf.size(font,22.0)
                    width,height=blf.dimensions(font,label)
                    x=max(8.0,min(float(point.x)+24.0,self._region.width-width-8.0))
                    y=max(8.0,min(float(point.y)+24.0,self._region.height-height-8.0))
                    blf.color(font,0.02,0.02,0.02,1.0);blf.position(font,x+1,y-1,0);blf.draw(font,label)
                    blf.color(font,0.35,0.9,1.0,1.0);blf.position(font,x,y,0);blf.draw(font,label)
            blf.size(font, 13.0)
            blf.color(font, 0.92, 0.92, 0.92, 1.0)
            y = 24.0
            for line in reversed(lines):
                blf.position(font, 18.0, y, 0)
                blf.draw(font, line)
                y += 18.0
            if bool(getattr(self, 'interactive_box_drag', False)):
                a = getattr(self, 'interactive_box_start', None)
                b = getattr(self, 'interactive_box_end', None)
                if a is not None and b is not None:
                    (x1, y1) = (float(a[0]), float(a[1]))
                    (x2, y2) = (float(b[0]), float(b[1]))
                    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
                    batch = batch_for_shader(shader, 'LINE_STRIP', {'pos': [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]})
                    gpu.state.blend_set('ALPHA')
                    gpu.state.line_width_set(1.5)
                    shader.bind()
                    shader.uniform_float('color', (0.88, 0.88, 0.88, 0.95))
                    batch.draw(shader)
                    gpu.state.line_width_set(1.0)
                    gpu.state.blend_set('NONE')
        except Exception:
            pass

    def _spawn(self, context):
        st = context.scene.tumblify
        src = _weighted_source_choice(self.sources)
        (surface_p, normal) = _scatter_sample_world(self.emitter, self.sampler, self.sampler_total)
        surface_p,normal=_surface_project_to_proxy(getattr(self,'surface_projection',None),surface_p,normal)
        smin = min(float(st.random_scale_min), float(st.random_scale_max))
        smax = max(float(st.random_scale_min), float(st.random_scale_max))
        sc = random.uniform(smin, smax) if smax > smin else smin
        pdata = self.source_collision[src.name]
        half = Vector(pdata.get('half', (0.1, 0.1, 0.1))) * sc
        offset = max(0.005, min(2.0, max(half) * 0.72))
        target = surface_p + normal * offset
        p = target + normal * 0.002
        q = normal.to_track_quat('Z', 'Y')
        angles = (random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_x else 0.0, random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_y else 0.0, random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_z else 0.0)
        q = (q @ Euler(angles, 'XYZ').to_quaternion()).normalized()
        (mf, mr, md) = _physics_material(src)
        h = add_body(self.world, World.DYNAMIC, src, self.depsgraph, p, (q.x, q.y, q.z, q.w), st.convex_simplify, prepared=pdata, instance_scale=sc, friction=mf, restitution=mr, density=md)
        if self.fast_mode:
            idx = len(self.dynamic_handles)
            self.dynamic_objs.append(None)
            self.dynamic_cache.append([float(p.x), float(p.y), float(p.z), float(q.x), float(q.y), float(q.z), float(q.w)])
            self.fast_groups[src.name]['indices'].append(idx)
        else:
            dup = src.copy()
            if dup.animation_data:
                dup.animation_data_clear()
            dup.parent = None
            dup.tumblify.role = 'NONE'
            dup.hide_select = False
            self.collection.objects.link(dup)
            _sync_visual_state_from_source(src, dup)
            (_, _, base_scale) = world_pose_scale(src)
            dup.matrix_world = Matrix.LocRotScale(p, q, Vector(base_scale) * sc)
            dup['Tumblify_SourceName'] = src.name
            dup['Tumblify_InstanceScale'] = float(sc)
            dup[FROZEN_PROP] = False
            dup[RUNTIME_PROP] = True
            _configure_instance_display(dup, src, bpy.context.scene)
            self.dynamic_objs.append(dup)
            self.session_objects.append(dup)
            self.dynamic_cache.append([float(p.x), float(p.y), float(p.z), float(q.x), float(q.y), float(q.z), float(q.w)])
        self.session_indices.add(len(self.dynamic_handles))
        self.dynamic_handles.append(h)
        self.dynamic_targets.append(tuple(target))
        self.dynamic_source_names.append(src.name)
        self.dynamic_scales.append(sc)

    def _sync(self):
        if not self.dynamic_handles:
            return
        (vals, awake) = self.world.get_bulk(self.dynamic_handles)
        _solid_state_sync(self,awake)
        while len(self.dynamic_cache) < len(vals):
            v = vals[len(self.dynamic_cache)]
            self.dynamic_cache.append(list(v))
        if self.fast_mode:
            for (i, v) in enumerate(vals):
                if i >= len(self.dynamic_cache):
                    break
                if i in self.kinematic_indices:
                    continue
                self.dynamic_cache[i] = v
                if i < len(self.dynamic_objs):
                    o = self.dynamic_objs[i]
                    if _is_live_object(o):
                        o.location = (v[0], v[1], v[2])
                        o.rotation_mode = 'QUATERNION'
                        o.rotation_quaternion = (v[6], v[3], v[4], v[5])
            for group in self.fast_groups.values():
                _fast_proxy_write(group, self.dynamic_cache, self.dynamic_scales, awake)
        else:
            for (i, (o, v)) in enumerate(zip(self.dynamic_objs, vals)):
                if i >= len(self.dynamic_cache):
                    break
                if i in self.kinematic_indices or not _is_live_object(o):
                    continue
                self.dynamic_cache[i] = v
                o.location = (v[0], v[1], v[2])
                o.rotation_mode = 'QUATERNION'
                o.rotation_quaternion = (v[6], v[3], v[4], v[5])
            try:
                if _regular_display_proxy_needed(bpy.context.scene.tumblify):
                    _sync_regular_display_proxy(bpy.context.scene)
            except Exception:
                pass

    def _materialize_fast(self):
        if not self.fast_mode:
            return
        for (i, (src_name, sc)) in enumerate(zip(self.dynamic_source_names, self.dynamic_scales)):
            if i < len(self.dynamic_objs) and self.dynamic_objs[i] is not None:
                continue
            src = bpy.data.objects.get(src_name)
            if not src or i >= len(self.dynamic_cache):
                continue
            v = self.dynamic_cache[i]
            q = Quaternion((v[6], v[3], v[4], v[5]))
            (_, _, base_scale) = world_pose_scale(src)
            dup = src.copy()
            if dup.animation_data:
                dup.animation_data_clear()
            dup.parent = None
            dup.tumblify.role = 'NONE'
            dup.hide_select = False
            self.collection.objects.link(dup)
            _sync_visual_state_from_source(src, dup)
            dup.matrix_world = Matrix.LocRotScale(Vector((v[0], v[1], v[2])), q, Vector(base_scale) * float(sc))
            dup['Tumblify_SourceName'] = src_name
            dup['Tumblify_InstanceScale'] = float(sc)
            dup[FROZEN_PROP] = True
            dup[RUNTIME_PROP] = True
            dup[FIXED_PROP] = False
            _remove_display_modifier(dup)
            _restore_instance_display_type(dup)
            try:
                dup.display_type = 'TEXTURED'
                dup.hide_viewport = False
                dup.hide_render = False
            except Exception:
                pass
            self.dynamic_objs[i] = dup
        _clear_fast_collection()
        self.fast_groups.clear()
        self.fast_mode = False

    def _convert_dynamic_to_kinematic(self, index):
        if index < 0 or index >= len(self.dynamic_objs) or index in self.kinematic_indices:
            return index in self.kinematic_indices
        o = self.dynamic_objs[index]
        if not _is_live_object(o):
            return False
        self.world.destroy(self.dynamic_handles[index])
        src = bpy.data.objects.get(self.dynamic_source_names[index]) if index < len(self.dynamic_source_names) else None
        if src is None:
            src = o
        (p, q, _) = world_pose_scale(o)
        pdata = self.source_collision.get(src.name)
        if pdata is None:
            pdata = _prepare_collision(src, self.depsgraph, bpy.context.scene.tumblify.convex_simplify)
        sc = self.dynamic_scales[index] if index < len(self.dynamic_scales) else 1.0
        (mf, mr, md) = _physics_material(src)
        self.dynamic_handles[index] = add_body(self.world, World.KINEMATIC, src, self.depsgraph, p, q, bpy.context.scene.tumblify.convex_simplify, prepared=pdata, instance_scale=sc, friction=mf, restitution=mr, density=md)
        self.kinematic_indices.add(index)
        return True

    def _convert_dynamic_to_dynamic(self, index):
        if index < 0 or index >= len(self.dynamic_objs) or index not in self.kinematic_indices:
            return False
        o = self.dynamic_objs[index]
        if not _is_live_object(o):
            return False
        self.world.destroy(self.dynamic_handles[index])
        src = bpy.data.objects.get(self.dynamic_source_names[index]) if index < len(self.dynamic_source_names) else None
        if src is None:
            src = o
        (p, q, _) = world_pose_scale(o)
        pdata = self.source_collision.get(src.name)
        if pdata is None:
            pdata = _prepare_collision(src, self.depsgraph, bpy.context.scene.tumblify.convex_simplify)
        sc = self.dynamic_scales[index] if index < len(self.dynamic_scales) else 1.0
        (mf, mr, md) = _physics_material(src)
        self.dynamic_handles[index] = add_body(self.world, World.DYNAMIC, src, self.depsgraph, p, q, bpy.context.scene.tumblify.convex_simplify, prepared=pdata, instance_scale=sc, friction=mf, restitution=mr, density=md)
        self.kinematic_indices.discard(index)
        if index < len(self.dynamic_cache):
            self.dynamic_cache[index] = [float(p.x), float(p.y), float(p.z), float(q[0]), float(q[1]), float(q[2]), float(q[3])]
        return True

    def _convert_static_to_kinematic(self, index):
        if index < 0 or index >= len(self.static_objs) or index in self.static_kinematic_indices:
            return index in self.static_kinematic_indices
        o = self.static_objs[index]
        if not _is_live_object(o):
            return False
        if o.tumblify.shape in {'MESH', 'TERRAIN'}:
            self.report({'WARNING'}, 'Exact Mesh collider is static-only. Move it while simulation is stopped.')
            return False
        self.world.destroy(self.static_handles[index])
        (p, q, _) = world_pose_scale(o)
        pdata = self.static_collision.get(o.name) or _prepare_collision(o, self.depsgraph, bpy.context.scene.tumblify.convex_simplify)
        self.static_handles[index] = add_body(self.world, World.KINEMATIC, o, self.depsgraph, p, q, bpy.context.scene.tumblify.convex_simplify, prepared=pdata, friction=bpy.context.scene.tumblify.static_friction, restitution=0.0, density=0.0)
        self.static_kinematic_indices.add(index)
        return True

    def _convert_static_to_static(self, index):
        if index < 0 or index >= len(self.static_objs) or index not in self.static_kinematic_indices:
            return False
        o = self.static_objs[index]
        if not _is_live_object(o):
            return False
        self.world.destroy(self.static_handles[index])
        (p, q, _) = world_pose_scale(o)
        pdata = self.static_collision.get(o.name) or _prepare_collision(o, self.depsgraph, bpy.context.scene.tumblify.convex_simplify)
        self.static_handles[index] = add_body(self.world, World.STATIC, o, self.depsgraph, p, q, bpy.context.scene.tumblify.convex_simplify, prepared=pdata, friction=bpy.context.scene.tumblify.static_friction, restitution=0.0, density=0.0)
        self.static_kinematic_indices.discard(index)
        return True

    def _activate_selected_for_move(self, context):
        n = 0
        rejected = 0
        for o in list(context.selected_objects):
            if o in self.dynamic_objs:
                i = self.dynamic_objs.index(o)
                if i in self.kinematic_indices or self._convert_dynamic_to_kinematic(i):
                    n += 1
            elif o in self.static_objs:
                i = self.static_objs.index(o)
                if o.tumblify.shape in {'MESH', 'TERRAIN'}:
                    try:
                        o.select_set(False)
                    except Exception:
                        pass
                    rejected += 1
                elif i in self.static_kinematic_indices or self._convert_static_to_kinematic(i):
                    n += 1
        if rejected:
            self.report({'WARNING'}, f'{rejected} exact Mesh collider(s) cannot move during Interactive')
        return n

    def _update_fill_emitter_containers(self, force=False):
        names = set(getattr(self, 'moving_fill_emitters', set()))
        if not names and (not force):
            return
        st = bpy.context.scene.tumblify
        rebuilt = []
        for data in getattr(self, 'fill_emitters', []):
            o = data.get('obj') if data else None
            if not _is_live_object(o):
                continue
            if names and o.name not in names and (not force):
                continue
            moved = _fill_update_pose_from_object(data)
            if moved or force:
                rebuilt.extend(_fill_rebuild_container(self.world, data, st.static_friction))
        if rebuilt:
            self.fill_container_handles = []
            for data in getattr(self, 'fill_emitters', []):
                h = data.get('handle') if data else None
                if isinstance(h, (list, tuple)):
                    self.fill_container_handles.extend(h)
                elif h is not None:
                    self.fill_container_handles.append(h)

    def _finish_fill_emitter_move(self):
        if not getattr(self, 'moving_fill_emitters', None):
            return
        try:
            self._update_fill_emitter_containers(force=True)
        finally:
            self.moving_fill_emitters = set()

    def _release_unselected_kinematics(self, context):
        selected = set(context.selected_objects)
        for i in tuple(self.kinematic_indices):
            if i < len(self.dynamic_objs) and self.dynamic_objs[i] not in selected:
                self._convert_dynamic_to_dynamic(i)
        for i in tuple(self.static_kinematic_indices):
            if i < len(self.static_objs) and self.static_objs[i] not in selected:
                _release_static_as_dynamic(self, i)

    def _release_all_kinematics(self):
        for i in tuple(self.kinematic_indices):
            self._convert_dynamic_to_dynamic(i)
        for i in tuple(self.static_kinematic_indices):
            _release_static_as_dynamic(self, i)

    def _update_object_kinematics(self):
        for i in tuple(self.kinematic_indices):
            if i < len(self.dynamic_objs) and _is_live_object(self.dynamic_objs[i]):
                (p, q, _) = world_pose_scale(self.dynamic_objs[i])
                self.world.target(self.dynamic_handles[i], p, q, self.dt)
        for i in tuple(self.static_kinematic_indices):
            if i < len(self.static_objs) and _is_live_object(self.static_objs[i]):
                (p, q, _) = world_pose_scale(self.static_objs[i])
                self.world.target(self.static_handles[i], p, q, self.dt)

    def _finish(self, context):
        global _RUNTIME, STOP_REQUESTED
        _restore_transform_hud(self, context)
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        try:
            if getattr(self, '_draw_handle', None):
                bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
                self._draw_handle = None
        except Exception:
            pass
        _finish_runtime_move(self)
        try:
            self._sync()
        except Exception:
            pass
        if context.scene.tumblify.result_output == 'INSTANCES':
            try:
                kept = _runtime_to_instance_results(self, context)
            except Exception:
                kept = 0
                self._materialize_fast()
        else:
            try:
                self._materialize_fast()
            except Exception:
                _clear_fast_collection()
        for (i, o) in enumerate(self.dynamic_objs):
            if not _is_live_object(o):
                continue
            if o in getattr(self, 'preexisting_dynamic', set()):
                continue
            o[FROZEN_PROP] = True
            o[RUNTIME_PROP] = True
            o.tumblify.role = 'NONE'
            if i < len(self.dynamic_source_names):
                o['Tumblify_SourceName'] = self.dynamic_source_names[i]
            if i < len(self.dynamic_scales):
                o['Tumblify_InstanceScale'] = float(self.dynamic_scales[i])
        try:
            self.world.close()
        except Exception:
            pass
        _solid_state_restore()
        _RUNTIME = None
        STOP_REQUESTED = False
        try:
            _apply_global_instance_display(context.scene)
        except Exception:
            pass
        if getattr(self, 'fast_runtime', False) and context.scene.tumblify.result_output == 'OBJECTS':
            _force_fast_result_mesh_display(self)
        msg = f"Advanced stopped. Result: {(kept if context.scene.tumblify.result_output == 'INSTANCES' else len([o for o in self.dynamic_objs if _is_live_object(o)]))} instance(s)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}

    def _cancel_run(self, context, delete_all=False):
        global _RUNTIME, STOP_REQUESTED
        _restore_transform_hud(self, context)
        if self._timer:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        try:
            if getattr(self, '_draw_handle', None):
                bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
                self._draw_handle = None
        except Exception:
            pass
        try:
            self.world.close()
        except Exception:
            pass
        _solid_state_restore()
        _RUNTIME = None
        STOP_REQUESTED = False
        try:
            _clear_fast_collection()
        except Exception:
            pass
        for o in list(getattr(self, 'session_objects', [])):
            try:
                if _is_live_object(o):
                    bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass
        if delete_all:
            _schedule_clear_spawned()
        self.report({'INFO'}, 'Advanced cancelled')
        return {'CANCELLED'}

    def _enter_live_interactive(self, context):
        if getattr(self, 'live_interactive', False):
            return True
        self._interactive_resume_spawning = bool(getattr(self, 'spawning', False))
        self.spawning = False
        try:
            self._sync()
            self._materialize_fast()
            self.session_objects = [o for o in self.dynamic_objs if _is_live_object(o)]
            for o in self.session_objects:
                try:
                    o.hide_select = False
                    o.hide_viewport = False
                    _force_real_mesh_display(o)
                except Exception:
                    pass
            if bool(getattr(context.scene.tumblify, 'fill_emitter', False)):
                for eo in self._active_emitter_objects():
                    try:
                        eo.hide_select = False
                        eo.hide_viewport = False
                    except Exception:
                        pass
            self.live_interactive = True
            _hide_transform_hud(self, context)
            self.report({'INFO'}, 'Live Interactive: bodies stay dynamic; colliders stay static; Fill emitter can be moved with G')
            if context.area:
                context.area.tag_redraw()
            return True
        except Exception as e:
            _restore_transform_hud(self, context)
            self.live_interactive = False
            self.report({'ERROR'}, f'Live Interactive failed: {e}')
            return False

    def _exit_live_interactive(self, context):
        if not getattr(self, 'live_interactive', False):
            return True
        try:
            self.pending_move_finish = False
            self.moving_selected = False
            self._finish_fill_emitter_move()
            self._release_all_kinematics()
            _restore_transform_hud(self, context)
            self.live_interactive = False
            st = context.scene.tumblify
            if getattr(self, 'fast_runtime', False):
                try:
                    _rebatch_runtime_after_interactive(self, context.scene, force_mesh=True)
                except Exception as e:
                    self.fast_groups = {}
                    self.fast_mode = False
                    self.report({'WARNING'}, f'Could not resume Fast batch display: {e}')
                self.fast_sim_enabled = True
                self.fast_click_enabled = True
                self.fast_preview_scale = float(getattr(self, 'fast_preview_scale', 1.0) or 1.0)
                self.fast_preview_target = _fast_view_depth(context, getattr(self, 'fast_preview_target', None))
                self._fast_preview_create(context)
                self.report({'INFO'}, 'Fast Drop resumed')
                if context.area:
                    context.area.tag_redraw()
                return True
            if st.spawn_backend == 'FAST':
                try:
                    _rebatch_runtime_after_interactive(self, context.scene, force_mesh=False)
                except Exception as e:
                    self.fast_groups = {}
                    self.fast_mode = False
                    self.report({'WARNING'}, f'Could not restore Realtime batched display; using materialized objects: {e}')
            else:
                self.fast_groups = {}
                self.fast_mode = False
            self.spawning = bool(getattr(self, '_interactive_resume_spawning', False))
            try:
                _apply_global_instance_display(context.scene)
            except Exception:
                pass
            self.report({'INFO'}, 'Advanced / Surface resumed: spawning ' + ('ON' if self.spawning else 'paused') + (' (Realtime ready)' if self.fast_mode else ' (materialized)'))
            if context.area:
                context.area.tag_redraw()
            return True
        except Exception as e:
            self.report({'ERROR'}, f'Return to Advanced failed: {e}')
            return False

    def _modal_impl(self, context, event):
        global STOP_REQUESTED
        st = context.scene.tumblify
        if not getattr(self, 'live_interactive', False) and event.type == 'Q' and (event.value == 'PRESS') and (not (event.ctrl or event.alt or event.shift)):
            try:
                bpy.ops.tumblify.fast_rotation_axes_popup('INVOKE_DEFAULT')
            except Exception as exc:
                self.report({'WARNING'}, f'Rotation axes menu failed: {exc}')
            return {'RUNNING_MODAL'}
        if not getattr(self, 'live_interactive', False) and event.type == 'R' and event.shift and (not (event.ctrl or event.alt)) and (event.value == 'PRESS'):
            st.advanced_random_rotation = not st.advanced_random_rotation
            self.report({'INFO'}, f"Advanced random rotation: {('ON' if st.advanced_random_rotation else 'OFF')}")
            _tag_view3d_redraw_all()
            return {'RUNNING_MODAL'}
        if getattr(self, 'live_interactive', False) and self.moving_selected and (event.value == 'PRESS') and (event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE', 'RIGHTMOUSE', 'ESC'}):
            self.pending_move_finish = True
            return {'PASS_THROUGH'}
        if getattr(self, 'cancel_delete_all_requested', False):
            return self._cancel_run(context, delete_all=True)
        if event.type == 'ESC' and event.value == 'PRESS':
            return self._cancel_run(context)
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._finish(context)
        if STOP_REQUESTED:
            STOP_REQUESTED = False
            return self._finish(context)
        if event.type == 'E' and (not event.shift) and (event.value == 'PRESS') and (not getattr(self, 'live_interactive', False)):
            self.spawning = not self.spawning
            self.report({'INFO'}, 'Spawning ON' if self.spawning else 'Spawning OFF')
            return {'RUNNING_MODAL'}
        if event.type == 'E' and event.shift and (event.value == 'PRESS') and (not self.moving_selected):
            try:
                if getattr(self, 'live_interactive', False):
                    self._exit_live_interactive(context)
                else:
                    if getattr(self, 'fast_runtime', False):
                        self.fast_sim_enabled = True
                        self.fast_click_enabled = False
                        self._fast_preview_destroy()
                        _fast_clear_cursor_helpers()
                    self._enter_live_interactive(context)
            except Exception as e:
                self.report({'ERROR'}, f'Interactive toggle failed: {e}')
            return {'RUNNING_MODAL'}
        if getattr(self, 'live_interactive', False) and event.type in {'G', 'R'} and (event.value == 'PRESS'):
            n = self._activate_selected_for_move(context)
            if n:
                self.moving_selected = True
                self.pending_move_finish = False
                return {'PASS_THROUGH'}
            self.report({'WARNING'}, 'Interactive: select a spawned object or movable collider before transform')
            return {'RUNNING_MODAL'}
        if event.type == 'TIMER':
            st = context.scene.tumblify
            if getattr(self, 'live_interactive', False):
                if self.pending_move_finish:
                    _finish_runtime_move(self)
                self._release_unselected_kinematics(context)
                self._update_object_kinematics()
            if not _is_live_object(getattr(self, 'emitter', None)) or self.emitter.name not in context.scene.objects:
                self.report({'WARNING'}, 'Emitter was removed; simulation stopped')
                return self._finish(context)
            if self.spawning:
                self.spawn_acc += self.dt
                interval = 1.0 / max(1.0, float(st.spawn_rate))
                emitted = 0
                while self.spawn_acc >= interval and emitted < 32:
                    self.spawn_acc -= interval
                    self._spawn(context)
                    emitted += 1
            if st.scatter_magnetized and self.dynamic_handles and (not getattr(self, 'live_interactive', False)):
                try:
                    self.world.point_springs(self.dynamic_handles, self.dynamic_targets, st.scatter_magnet_strength, st.scatter_magnet_damping, 1.0)
                except (AttributeError, RuntimeError) as e:
                    self.report({'ERROR'}, str(e))
                    return self._finish(context)
            self.world.step(self.dt, int(st.physics_substeps))
            self._sync()
            if context.area:
                context.area.tag_redraw()
        return {'PASS_THROUGH'}

    def modal(self, context, event):
        return _dispatch_runtime_modal(self, context, event, 'Tumblify Surface simulation')

class TU_OT_run(bpy.types.Operator):
    bl_idname = 'tumblify.run'
    bl_label = 'Start Advanced Drop'
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    start_interactive: BoolProperty(name='Start Interactive', default=False, options={'HIDDEN'})
    force_fast: BoolProperty(name='Force Fast Runtime', default=False, options={'HIDDEN'})
    _timer = None

    def invoke(self, context, event):
        global _RUNTIME, STOP_REQUESTED
        st = context.scene.tumblify
        force_fast = bool(getattr(self, 'force_fast', False))
        if not force_fast and st.spawn_backend == 'OFFLINE':
            return bpy.ops.tumblify.drop_offline('INVOKE_DEFAULT')
        if not force_fast and st.spawn_location == 'SURFACE':
            return bpy.ops.tumblify.surface_run('INVOKE_DEFAULT')
        if context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, 'Run from a 3D View')
            return {'CANCELLED'}
        if _RUNTIME is not None:
            self.report({'WARNING'}, 'Stop the current simulation first')
            return {'CANCELLED'}
        prep=_RealtimePreparation(context,'Fast' if st.mode=='FAST' else 'Advanced')
        try:
            prep.begin()
            self.fast_runtime = bool(force_fast or getattr(st, 'mode', 'DROP') == 'FAST')
            if self.fast_runtime and (not bool(getattr(self, 'start_interactive', False))) and st.show_collision_debug:
                _clear_debug_collection()
            if self.fast_runtime:
                self.interactive_existing = bool(getattr(self, 'start_interactive', False))
                if not self.interactive_existing:
                    _fast_reset_emitter_rotation(context.scene)
                self.preexisting_dynamic = set()
                self.preexisting_matrices = {}
                self.interactive_instance_results = []
                self.interactive_instance_visibility = {}
                self.interactive_instance_mode = False
                if self.interactive_existing:
                    selected = [o for o in context.selected_objects if o.type == 'MESH' and (not o.get('Tumblify_Helper'))]
                    if not selected:
                        self.report({'ERROR'}, 'Fast Interactive: select at least one Mesh object')
                        return {'CANCELLED'}
                    combined = [o for o in selected if bool(o.get(INSTANCE_RESULT_PROP, False))]
                    regular = [o for o in selected if o not in combined]
                    if combined:
                        self.interactive_instance_mode = True
                        self.interactive_instance_results = list(combined)
                        self.preexisting_dynamic = set(regular)
                        self.preexisting_matrices = {o: o.matrix_world.copy() for o in regular}
                        srcs = []
                        seen = set()
                        for inst in combined:
                            src = bpy.data.objects.get(str(inst.get('Tumblify_SourceName', '')))
                            if src is None or not _is_live_object(src):
                                self.report({'ERROR'}, f'Interactive source for {inst.name} is missing')
                                return {'CANCELLED'}
                            if src.name not in seen:
                                srcs.append(src)
                                seen.add(src.name)
                            self.interactive_instance_visibility[inst] = (bool(inst.hide_viewport), bool(inst.hide_render))
                        for o in regular:
                            if o.name not in seen:
                                srcs.append(o)
                                seen.add(o.name)
                        sources = srcs
                        if not self.interactive_existing:
                            _fast_prepare_cursor(context.scene, selected)
                    else:
                        sources = regular if regular else selected
                        self.preexisting_dynamic = set(sources)
                        self.preexisting_matrices = {o: o.matrix_world.copy() for o in sources}
                        if not self.interactive_existing:
                            _fast_prepare_cursor(context.scene, sources)
                else:
                    sources = _fast_source_objects(context.scene)
                    if not sources:
                        self.report({'ERROR'}, 'Fast: choose source objects first')
                        return {'CANCELLED'}
                    if not bool(getattr(st, 'fast_cursor_ready', False)):
                        _fast_prepare_cursor(context.scene, sources)
                if not self.interactive_existing:
                    _fast_prepare_cursor(context.scene,sources)
                if not self.interactive_existing:
                    _fast_set_orbit_pivot(self, context)
                if st.mode=='FAST' and st.fast_static_scope=='SCENE' and _validate_scene_collisions(context.scene) is None:
                    self.report({'WARNING'},'Click Prepare Scene before starting with Scene colliders')
                    return {'CANCELLED'}
                statics = _advanced_runtime_static_objects(context.scene) if self.interactive_existing and st.mode == 'DROP' else _fast_static_objects(context.scene, sources)
                if getattr(self, 'interactive_instance_mode', False):
                    selected_inst = set(self.interactive_instance_results)
                    statics = [o for o in statics if o not in selected_inst]
                frozen = _frozen_spawned(context.scene)
            else:
                sources = _scene_role_objects(context.scene, 'SOURCE')
                statics = _advanced_runtime_static_objects(context.scene)
                frozen = _frozen_spawned(context.scene)
                if not sources:
                    self.report({'ERROR'}, 'Mark at least one Source object')
                    return {'CANCELLED'}
                mesh_sources = [o.name for o in sources if o.tumblify.shape in {'MESH', 'TERRAIN'}]
                if mesh_sources:
                    self.report({'ERROR'}, f"Mesh collision is static-only. Set Sources to Convex Hull, Compound Hull, Loose Parts Compound, or Manual Compound: {', '.join(mesh_sources[:3])}")
                    return {'CANCELLED'}
            prep.update(8,'Evaluating scene')
            self.depsgraph = context.evaluated_depsgraph_get()
            simplify = context.scene.tumblify.convex_simplify
            if self.fast_runtime:
                self.fast_source_fits = {}
                for o in prep.items(sources,10,18,'Fitting source shapes'):
                    if bool(getattr(self, 'interactive_existing', False)) and o in getattr(self, 'preexisting_dynamic', set()) and (str(getattr(o.tumblify, 'shape', 'DETECT')) in {'MESH', 'TERRAIN'}):
                        fit = _fast_collision_fit_for_shape(o, self.depsgraph, 'DETECT')
                        if str(fit.get('shape', 'HULL')) in {'MESH', 'TERRAIN'}:
                            fit = {'shape': 'HULL'}
                    else:
                        fit = _fast_collision_fit(o, self.depsgraph)
                    self.fast_source_fits[o.name] = fit
                self.fast_source_shapes = {name: fit.get('shape', 'HULL') for (name, fit) in self.fast_source_fits.items()}
                self.source_collision = {o.name: _prepare_collision(o, self.depsgraph, simplify, shape_override=self.fast_source_fits[o.name]) for o in prep.items(sources,18,35,'Preparing dynamic collisions')}
            else:
                self.source_collision = {o.name: _prepare_collision(o, self.depsgraph, simplify) for o in prep.items(sources,18,35,'Preparing dynamic collisions')}
            prep.update(36,'Creating physics world')
            self.world = _configure_world(World(context.scene.tumblify.physics_threads), context.scene.tumblify, fast=self.fast_runtime)
            _add_builtin_floor(self.world, st, fast=self.fast_runtime and st.mode == 'FAST')
            for o in prep.items(sources,36,42,'Caching collision assets'):
                data = self.source_collision[o.name]
                _cache_native_collision_assets(self.world, data)
            self.static_objs = []
            self.static_handles = []
            self.static_collision = {}
            self.static_kinematic_indices = set()
            self.static_motion_proxy = {}
            self.skipped_static_colliders = []
            skip_unsupported=bool(self.fast_runtime and st.fast_static_scope=='SCENE')
            for o in prep.items(statics,42,60,'Preparing static colliders'):
                try:
                    if self.fast_runtime and st.mode=='FAST' and st.fast_static_scope=='SCENE':
                        pdata=_cached_scene_collision(context.scene,o)
                        if pdata is None:
                            rec=_get_scene_cache(context.scene)
                            reason=next((item['reason'] for item in (rec or {}).get('skipped',[]) if item['name']==o.name),'Collider missing from prepared Scene')
                            raise CollisionGeometryError(reason)
                    else:
                        pdata = _prepare_collision(o, self.depsgraph, simplify, shape_override=_fast_static_collision_fit(o, self.depsgraph) if self.fast_runtime else None)
                    h = add_body(self.world, World.STATIC, o, self.depsgraph, simplify=simplify, prepared=pdata, friction=context.scene.tumblify.static_friction, restitution=0.0, density=0.0)
                except Exception as exc:
                    if skip_unsupported and isinstance(exc,CollisionGeometryError):
                        self.skipped_static_colliders.append({'name':o.name,'reason':str(exc)})
                        print(f"[Tumblify] Skipped Scene collider '{o.name}': {exc}",flush=True)
                        continue
                    raise RuntimeError(f"Static collider '{o.name}': {exc}") from exc
                self.static_objs.append(o)
                self.static_collision[o.name] = pdata
                self.static_handles.append(h)
            self.fast_static_count = len(self.static_objs) if self.fast_runtime else 0
            for o in prep.items(frozen,60,70,'Restoring previous results'):
                src_name = str(o.get('Tumblify_SourceName', ''))
                src = bpy.data.objects.get(src_name)
                pdata = self.source_collision.get(src_name)
                if src is None:
                    src = o
                    pdata = _prepare_collision(o, self.depsgraph, simplify)
                elif pdata is None:
                    pdata = _prepare_collision(src, self.depsgraph, simplify)
                    _cache_native_collision_assets(self.world, pdata)
                    self.source_collision[src_name] = pdata
                (p0, q0, _) = world_pose_scale(o)
                sc = float(o.get('Tumblify_InstanceScale', 1.0))
                h = add_body(self.world, World.STATIC, src, self.depsgraph, p_override=p0, q_override=q0, simplify=simplify, prepared=pdata, instance_scale=sc, friction=context.scene.tumblify.static_friction, restitution=0.0, density=0.0)
                self.static_collision[o.name] = pdata
                self.static_objs.append(o)
                self.static_handles.append(h)
            _add_instance_results_as_static(self.world, context.scene, self.depsgraph, self.source_collision, simplify, context.scene.tumblify.static_friction, exclude=self.interactive_instance_results if getattr(self, 'interactive_instance_mode', False) else None,owner=self,progress=prep,progress_range=(70,80))
            prep.update(81,'Preparing interaction and selection')
            self.sources = sources
            self.dynamic_objs = []
            self.dynamic_handles = []
            self.dynamic_source_names = []
            self.dynamic_scales = []
            self.session_objects = []
            self.session_indices = set()
            self.kinematic_indices = set()
            self.left = False
            self.fast_click_enabled = bool(self.fast_runtime)
            self.fast_sim_enabled = bool(self.fast_runtime)
            self.fast_preview_handle = None
            self.fast_preview_obj = None
            self.fast_preview_source = None
            self.fast_preview_scale = 1.0
            self.fast_preview_target = _fast_initial_preview_target(context, event) if self.fast_runtime else Vector((0, 0, 0))
            self.live_interactive = False
            self.spawn_acc = 0.0
            self.fast_hold_spawning = False
            self.fast_hold_acc = 0.0
            self.fast_snap_enabled=False;self.fast_snap_valid=False;self.fast_snap_picker=None;self.fast_snap_bounds={}
            self.fast_snap_floor_config=_builtin_floor_config(st,fast=self.fast_runtime and st.mode=='FAST')
            self.fast_native_rotate = False
            self._fast_collision_hitbox = None
            _fhz = int(st.physics_frequency)
            _fsub = int(st.physics_substeps)
            self.runtime_substeps = max(1, int(_fsub))
            self.dt = 1.0 / max(1.0, float(_fhz))
            self.moving_selected = False
            self.pending_move_finish = False
            self.moving_fill_emitters = set()
            self.fill_collision_enabled = True
            self.interactive_box_armed = False
            self.interactive_box_drag = False
            self.interactive_box_start = None
            self.interactive_box_end = None
            self.interactive_box_mode = 'REPLACE'
            self.interactive_mouse_pending = False
            self.interactive_mouse_dragging = False
            self._mesh_selection_lock = _lock_mesh_collider_selection(context.scene)
            if self.fast_runtime:
                already = {o.as_pointer() for (o, _) in self._mesh_selection_lock if _is_live_object(o)}
                for collider in statics:
                    if not _is_live_object(collider) or collider.as_pointer() in already:
                        continue
                    if str((self.static_collision.get(collider.name) or {}).get('shape', 'MESH')) in {'MESH', 'TERRAIN'}:
                        self._mesh_selection_lock.append((collider, bool(collider.hide_select)))
                        try:
                            collider.select_set(False)
                            collider.hide_select = True
                        except Exception:
                            pass
            st = context.scene.tumblify
            st.emitter_interaction = 'OFF'
            prep.update(85,'Preparing emitter')
            configured = _configured_emitter_objects(context.scene) if not self.fast_runtime else []
            if self.fast_runtime:
                self.brush = None
                self.emitter_obj = None
                self.emitter_objs = []
                self.emitter_mode = 'FAST_CURSOR'
            else:
                self.brush = ensure_brush(context.scene)
                if st.emitter_collection is not None:
                    if not configured:
                        raise RuntimeError('Emitter Collection has no usable objects in the current scene')
                    self.emitter_mode = 'COLLECTION'
                    st.emitter_mode = 'COLLECTION'
                    self.emitter_objs = configured
                    self.emitter_obj = configured[0]
                    self.brush.hide_viewport = True
                elif configured:
                    self.emitter_mode = 'CUSTOM'
                    st.emitter_mode = 'CUSTOM'
                    self.emitter_objs = configured
                    self.emitter_obj = configured[0]
                    self.brush.hide_viewport = True
                else:
                    self.emitter_mode = 'FIXED'
                    st.emitter_mode = 'FIXED'
                    self.brush.hide_viewport = False
                    self.emitter_obj = self.brush
                    self.emitter_objs = [self.brush]
                    st.emitter_object = self.brush
            if not self.fast_runtime:
                _sync_emitter_wire(context.scene)
            self.fill_emitters = []
            self.fill_container_handles = []
            if not self.fast_runtime and bool(st.fill_emitter):
                emitter_set = set(self.emitter_objs)
                for i in range(len(self.static_objs) - 1, -1, -1):
                    so = self.static_objs[i]
                    if so in emitter_set:
                        try:
                            h = self.static_handles[i]
                            if h is not None:
                                self.world.destroy(h)
                        except Exception:
                            pass
                        try:
                            self.static_collision.pop(so.name, None)
                        except Exception:
                            pass
                        self.static_objs.pop(i)
                        self.static_handles.pop(i)
                for eo in self.emitter_objs:
                    try:
                        eo.hide_select = False
                    except Exception:
                        pass
                    data = _fill_build_emitter_data(eo, self.depsgraph)
                    if data is None:
                        continue
                    self.fill_emitters.append(data)
                    self.fill_container_handles.extend(_fill_add_container(self.world, data, st.static_friction))
                if not self.fill_emitters:
                    raise RuntimeError('Fill Emitter could not build a usable emitter container')
            st.emitter_interaction = 'OFF'
            if self.brush is not None:
                _set_brush_mode_visual(self.brush, 'OFF')
            for eo in self.emitter_objs:
                if eo is not self.brush:
                    _set_brush_mode_visual(eo, 'OFF')
            radius = self._emitter_radius()
            self.last_brush_radius = radius
            for x in context.selected_objects:
                x.select_set(False)
            if self.brush is not None and self.emitter_mode == 'FIXED' and (self.brush.name not in context.view_layer.objects):
                self.brush = ensure_brush(context.scene, fast_empty=self.fast_runtime)
                self.emitter_obj = self.brush
                self.emitter_objs = [self.brush]
                st.emitter_object = self.brush
                context.view_layer.update()
            active = None
            for eo in self.emitter_objs:
                if eo and eo.name in context.view_layer.objects and (not eo.hide_select):
                    eo.select_set(True)
                    if active is None:
                        active = eo
            if active is not None:
                context.view_layer.objects.active = active
            self.drag_depth = self._emitter_location()
            self.brush_handles = {}
            if not self.fast_runtime:
                update_emitter_guide(context.scene)
            prep.update(90,'Preparing viewport display')
            if bool(getattr(self, 'interactive_instance_mode', False)):
                first = self.interactive_instance_results[0] if self.interactive_instance_results else None
                cols = list(getattr(first, 'users_collection', []) or []) if first is not None else []
                self.collection = cols[0] if cols else context.collection
            else:
                self.collection = None if bool(getattr(self, 'interactive_existing', False)) else spawned_collection(context.scene, True, 'Fast' if self.fast_runtime else 'Drop')
            self.fast_mode = bool(getattr(self, 'interactive_instance_mode', False)) or (st.spawn_backend == 'FAST' and (not bool(getattr(self, 'interactive_existing', False))))
            self.fast_groups = {}
            self.dynamic_cache = []
            if bool(getattr(self, 'interactive_existing', False)):
                if getattr(self, 'interactive_instance_mode', False):
                    self.fast_groups = {o.name: _new_fast_proxy(context.scene, o, self.depsgraph, force_mesh=True) for o in prep.items(sources,90,97,'Preparing viewport display')}
                    for inst in self.interactive_instance_results:
                        src = bpy.data.objects.get(str(inst.get('Tumblify_SourceName', '')))
                        if src is None:
                            continue
                        pdata = self.source_collision[src.name]
                        (mf, mr, md) = _physics_material(src)
                        for (pose, sc) in _instance_result_entries(inst):
                            p = Vector(pose[:3])
                            q = Quaternion((pose[6], pose[3], pose[4], pose[5])).normalized()
                            h = add_body(self.world, World.DYNAMIC, src, self.depsgraph, p, (q.x, q.y, q.z, q.w), st.convex_simplify, prepared=pdata, instance_scale=float(sc), friction=mf, restitution=mr, density=md)
                            idx = len(self.dynamic_handles)
                            self.dynamic_objs.append(None)
                            self.dynamic_handles.append(h)
                            self.dynamic_source_names.append(src.name)
                            self.dynamic_scales.append(float(sc))
                            self.dynamic_cache.append(list(pose))
                            self.fast_groups[src.name]['indices'].append(idx)
                    initial_materialized = set()
                    for o in list(getattr(self, 'preexisting_dynamic', set()) or set()):
                        if not _is_live_object(o):
                            continue
                        pdata = self.source_collision[o.name]
                        (p, q, _) = world_pose_scale(o)
                        (mf, mr, md) = _physics_material(o)
                        h = add_body(self.world, World.DYNAMIC, o, self.depsgraph, p, q, st.convex_simplify, prepared=pdata, instance_scale=1.0, friction=mf, restitution=mr, density=md)
                        idx = len(self.dynamic_handles)
                        self.dynamic_objs.append(o)
                        self.dynamic_handles.append(h)
                        self.dynamic_source_names.append(o.name)
                        self.dynamic_scales.append(1.0)
                        self.dynamic_cache.append([float(p.x), float(p.y), float(p.z), float(q[0]), float(q[1]), float(q[2]), float(q[3])])
                        initial_materialized.add(idx)
                    for inst in self.interactive_instance_results:
                        try:
                            inst['Tumblify_ActiveInstanceSnapshot'] = True
                            inst.hide_viewport = True
                            inst.hide_render = True
                            inst.select_set(False)
                        except Exception:
                            pass
                    self.fast_interactive_materialized = initial_materialized
                    self._fast_interactive_rebuild_groups()
                    if not self.dynamic_handles or not self.fast_groups:
                        raise RuntimeError('Interactive could not import the selected Offline/Combined result')
                    self.interactive_box_armed = False
                    self.interactive_box_drag = False
                    try:
                        for o in context.selected_objects:
                            o.select_set(False)
                        active = None
                        for o in list(getattr(self, 'preexisting_dynamic', set()) or set()):
                            if _is_live_object(o):
                                o.hide_select = False
                                o.select_set(True)
                                if active is None:
                                    active = o
                        context.view_layer.objects.active = active
                    except Exception:
                        pass
                else:
                    for o in sources:
                        pdata = self.source_collision[o.name]
                        (p, q, _) = world_pose_scale(o)
                        (mf, mr, md) = _physics_material(o)
                        h = add_body(self.world, World.DYNAMIC, o, self.depsgraph, p, q, st.convex_simplify, prepared=pdata, instance_scale=1.0, friction=mf, restitution=mr, density=md)
                        self.dynamic_objs.append(o)
                        self.dynamic_handles.append(h)
                        self.dynamic_source_names.append(o.name)
                        self.dynamic_scales.append(1.0)
                        self.dynamic_cache.append([float(p.x), float(p.y), float(p.z), float(q[0]), float(q[1]), float(q[2]), float(q[3])])
                    for x in context.selected_objects:
                        try:
                            x.select_set(False)
                        except Exception:
                            pass
                    for o in sources:
                        try:
                            o.hide_select = False
                            o.select_set(True)
                        except Exception:
                            pass
                    if sources:
                        context.view_layer.objects.active = sources[0]
            elif self.fast_mode:
                _clear_fast_collection()
                try:
                    self.fast_groups = {o.name: _new_fast_proxy(context.scene, o, self.depsgraph, force_mesh=bool(getattr(self, 'fast_runtime', False))) for o in prep.items(sources,90,97,'Preparing viewport display')}
                except Exception as e:
                    _clear_fast_collection()
                    raise RuntimeError(f'Realtime display setup failed: {e}.')
            prep.update(98,'Starting simulation')
            STOP_REQUESTED = False
            _set_runtime_owner(self, context)
            self._region = next((r for r in context.area.regions if r.type == 'WINDOW'), context.region)
            self._rv3d = context.space_data.region_3d
            self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(self._draw_brush_label, (), 'WINDOW', 'POST_PIXEL')
            self._timer = context.window_manager.event_timer_add(self.dt, window=context.window)
            context.window_manager.modal_handler_add(self)
            if self.fast_runtime:
                if bool(getattr(self, 'interactive_existing', False)):
                    self.fast_click_enabled = False
                    self.fast_sim_enabled = True
                    self._enter_live_interactive(context)
                    self.report({'INFO'}, f'Fast Batched Interactive: {len(self.dynamic_handles)} physics body(s)')
                else:
                    self.fast_preview_target = _fast_initial_preview_target(context, event, self)
                    _fast_update_cursor_helpers(context.scene, self.fast_preview_target)
                    self._fast_preview_create(context)
            prep.update(100,'Ready',force=True)
            if self.skipped_static_colliders:
                skipped=self.skipped_static_colliders
                names='; '.join(f"{item['name']}: {item['reason']}" for item in skipped[:3])
                if len(skipped)>3:names+=f'; +{len(skipped)-3} more (see console)'
                self.report({'WARNING'},f'Skipped {len(skipped)} unsupported Scene collider(s). {names}')
            return {'RUNNING_MODAL'}
        except Exception as e:
            try:
                if hasattr(self, '_draw_handle') and self._draw_handle:
                    bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
                    self._draw_handle = None
                if hasattr(self, 'world'):
                    self.world.close()
                _restore_mesh_collider_selection(getattr(self, '_mesh_selection_lock', None))
                _clear_fast_collection()
                _fast_clear_cursor_helpers()
            except Exception:
                pass
            _solid_state_restore()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        finally:
            prep.close()

    def _active_emitter_objects(self):
        objs = []
        for o in getattr(self, 'emitter_objs', []):
            try:
                if _is_live_object(o) and o.name in bpy.context.scene.objects:
                    objs.append(o)
            except ReferenceError:
                pass
        if objs:
            return objs
        b = getattr(self, 'brush', None)
        if _is_live_object(b):
            return [b]
        return []

    def _emitter_location(self):
        objs = self._active_emitter_objects()
        if objs:
            return objs[0].matrix_world.translation.copy()
        if getattr(self, 'fast_runtime', False):
            return Vector(getattr(self, 'fast_preview_target', (0.0, 0.0, 0.0)))
        return self.brush.location.copy()

    def _object_emitter_radius(self, o):
        if o is self.brush or getattr(self, 'emitter_mode', 'FIXED') == 'FIXED':
            base = float(bpy.context.scene.tumblify.emitter_radius)
            scale = max((abs(float(v)) for v in o.scale)) if o else 1.0
            return max(0.05, base * scale)
        d = o.dimensions
        return max(0.05, 0.5 * max(abs(float(d.x)), abs(float(d.y)), abs(float(d.z))))

    def _emitter_radius(self):
        if getattr(self, 'fast_runtime', False):
            return max([0.5,float(bpy.context.scene.tumblify.fast_spread)]+[max(abs(float(v)) for v in obj.dimensions) for obj in self.sources if _is_live_object(obj)])
        objs = self._active_emitter_objects()
        return self._object_emitter_radius(objs[0]) if objs else max(0.05, float(bpy.context.scene.tumblify.emitter_radius))

    def _sample_emitter_point(self):
        objs = self._active_emitter_objects()
        if getattr(self, 'emitter_mode', 'FIXED') != 'FIXED' and objs:
            o = random.choice(objs)
            try:
                corners = _emitter_bound_box_without_guide(o, self.depsgraph)
                mins = Vector((min((v.x for v in corners)), min((v.y for v in corners)), min((v.z for v in corners))))
                maxs = Vector((max((v.x for v in corners)), max((v.y for v in corners)), max((v.z for v in corners))))
                lp = Vector((random.uniform(mins.x, maxs.x), random.uniform(mins.y, maxs.y), random.uniform(mins.z, maxs.z)))
                return o.matrix_world @ lp
            except Exception:
                return o.matrix_world.translation.copy()
        r = self._emitter_radius() * 0.72
        v = Vector((0, 0, 0))
        for _ in range(12):
            v = Vector((random.uniform(-1, 1), random.uniform(-1, 1), random.uniform(-1, 1)))
            if 0.0001 < v.length_squared <= 1:
                break
        return self._emitter_location() + v * r

    def _draw_brush_label(self):
        try:
            if not getattr(self, '_region', None) or not getattr(self, '_rv3d', None):
                return
            if getattr(self,'fast_runtime',False) and not getattr(self,'live_interactive',False):_fast_snap.draw(self)
            font = 0
            self._fast_collision_hitbox = None
            st = bpy.context.scene.tumblify
            if getattr(self, 'fast_runtime', False) and (getattr(self, 'fast_spread_adjust', None) is not None or time.monotonic() < getattr(self, 'fast_spread_circle_until', 0.0)):
                center = Vector(getattr(self, 'fast_preview_target', (0, 0, 0)))
                points = []
                for i in range(97):
                    angle = math.tau * i / 96
                    offset=_fast_emitter_rotation_quat(bpy.context.scene)@Vector((st.fast_spread*math.cos(angle),st.fast_spread*math.sin(angle),0))
                    if getattr(self,'fast_snap_enabled',False) and getattr(self,'fast_snap_valid',False):
                        normal=self.fast_snap_normal;offset=Vector((0,0,1)).rotation_difference(normal)@offset;offset-=normal*offset.dot(normal)
                    point=view3d_utils.location_3d_to_region_2d(self._region,self._rv3d,center+offset)
                    if point is None:
                        points = []
                        break
                    points.append(tuple(point))
                if points:
                    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
                    gpu.state.blend_set('ALPHA')
                    try:
                        gpu.state.line_width_set(2.0)
                        shader.bind()
                        shader.uniform_float('color', (0.25, 0.8, 1.0, 0.95))
                        batch_for_shader(shader, 'LINE_STRIP', {'pos': points}).draw(shader)
                    finally:
                        gpu.state.line_width_set(1.0)
                        gpu.state.blend_set('NONE')
            if getattr(self, 'fast_runtime', False) and (not getattr(self, 'live_interactive', False)):
                if bool(getattr(self, 'fast_native_rotate', False)):
                    try:
                        er = tuple((math.degrees(float(v)) for v in st.fast_emitter_rotation))
                        rot_label = f'X {er[0]:.0f}°  Y {er[1]:.0f}°  Z {er[2]:.0f}°'
                    except Exception:
                        rot_label = 'Rotation'
                    lines = ['FAST EMITTER — BLENDER ROTATE', rot_label, 'Native Blender Rotate is active', 'Use your normal Blender axis / numeric / snap controls', 'Confirm or cancel exactly as with ordinary object rotation']
                else:
                    lines = ['FAST SNAP' if getattr(self,'fast_snap_enabled',False) else 'FAST DROP', 'V  Snap: '+('ON' if getattr(self,'fast_snap_enabled',False) else 'OFF'), 'LMB  Place / hold to repeat' if getattr(self,'fast_snap_enabled',False) else 'LMB  Drop / hold to repeat', 'RMB  Random next source', 'Ctrl/Shift+Wheel  Scale', 'R / R R  Blender rotate / trackball (emitter + held object)', f'S  Spread Radius: {st.fast_spread:.2f}', f'+ / -  Spread Count: {int(st.fast_objects_per_tick)}', f'Shift + / -  Stack Height: {int(st.fast_stack_count)}', f'Objects / Drop: {int(st.fast_objects_per_tick)*int(st.fast_stack_count)}', f"Shift+R  Random rotation: {('ON' if bool(getattr(st, 'fast_random_rotation', False)) else 'OFF')}", f"Q  Random axes: {''.join((a if bool(getattr(st, f'fast_random_rot_{a.lower()}', True)) else '–' for a in 'XYZ'))}", f"E  Physics: {('ON' if getattr(self, 'fast_sim_enabled', False) else 'PAUSED')}", 'Shift+E  Live Interactive', 'Enter  Finish / keep result', 'Esc  Cancel / delete this run']
            elif getattr(self, 'live_interactive', False):
                fill_state = 'ON' if bool(getattr(self, 'fill_collision_enabled', True)) else 'OFF / RELEASED'
                if getattr(self, 'fast_runtime', False):
                    lines = ['FAST INTERACTIVE', 'G / R  Move / rotate physics objects', 'Shift+Click  Add/toggle selection', 'LMB drag  Box Select   Shift add / Ctrl subtract', 'Shift+E  Return to Fast', 'Enter  Finish / keep result', 'Esc  Cancel / delete this run']
                else:
                    lines = ['INTERACTIVE', 'G / R  Move / rotate physics objects / colliders / Fill emitter', *([f'F3  Fill collision: {fill_state} / release'] if getattr(self, 'fill_emitters', None) else []), 'Shift+Click  Add/toggle selection', 'Ctrl+Click  Remove from selection', 'LMB drag  Box Select   Shift add / Ctrl subtract', 'Shift+E  Return to Drop', 'Enter  Finish / keep result', 'Esc  Cancel / delete this run']
            else:
                fill_enabled = bool(getattr(st, 'fill_emitter', False))
                fill_state = 'ON' if bool(getattr(self, 'fill_collision_enabled', True)) else 'OFF / RELEASED'
                lines = [f"E  Spawn: {('ON' if self.left else 'OFF')}", f"Shift+R  Random rotation: {('ON' if st.advanced_random_rotation else 'OFF')}", 'Q  Random rotation axes', 'Fill Emitter: bottom-up container' if fill_enabled else 'Inside emitter']
                if fill_enabled:
                    lines.append(f'3  Fill collision: {fill_state} / release')
                lines.extend([f'- / =  Spawn rate: {float(st.spawn_rate):.1f}/s', 'Shift+E  Live Interactive', 'Enter  Finish / keep result', 'Esc  Cancel / delete this run'])
            if getattr(self,'fast_runtime',False) and not getattr(self,'live_interactive',False) and time.monotonic()<getattr(self,'fast_scale_label_until',0.0):
                point=view3d_utils.location_3d_to_region_2d(self._region,self._rv3d,Vector(self.fast_preview_target))
                if point is not None:
                    label=f"Scale  {float(self.fast_preview_scale):.2f}x"
                    blf.size(font,22.0)
                    width,height=blf.dimensions(font,label)
                    x=max(8.0,min(float(point.x)+24.0,self._region.width-width-8.0))
                    y=max(8.0,min(float(point.y)+24.0,self._region.height-height-8.0))
                    blf.color(font,0.02,0.02,0.02,1.0);blf.position(font,x+1,y-1,0);blf.draw(font,label)
                    blf.color(font,0.35,0.9,1.0,1.0);blf.position(font,x,y,0);blf.draw(font,label)
            blf.size(font, 13.0)
            blf.color(font, 0.92, 0.92, 0.92, 1.0)
            y = 24.0
            for line in reversed(lines):
                blf.position(font, 18.0, y, 0)
                blf.draw(font, line)
                y += 18.0
            if bool(getattr(self, 'interactive_box_drag', False)):
                _draw_interactive_selection_box(getattr(self, 'interactive_box_start', None), getattr(self, 'interactive_box_end', None))
        except Exception:
            pass

    def _mouse_point(self, context, event):
        coord = (event.mouse_region_x, event.mouse_region_y)
        try:
            return view3d_utils.region_2d_to_location_3d(context.region, context.space_data.region_3d, coord, self.drag_depth)
        except Exception:
            return None

    def _spawn(self, p_override=None):
        scene = bpy.context.scene
        st = scene.tumblify
        src = _weighted_source_choice(self.sources)
        smin = min(float(st.random_scale_min), float(st.random_scale_max))
        smax = max(float(st.random_scale_min), float(st.random_scale_max))
        scale_factor = random.uniform(smin, smax) if smax > smin else smin
        p = Vector(p_override) if p_override is not None else _fill_choose_point(getattr(self, 'fill_emitters', []), self.source_collision, src, scale_factor) if bool(st.fill_emitter) else None
        if p is None:
            if st.fill_emitter:
                if not getattr(self,'_fill_fit_warned',False):self.report({'WARNING'},f"Fill Emitter: no safe space for '{src.name}'; spawn skipped");self._fill_fit_warned=True
                return False
            p = self._sample_emitter_point()
        (base_p, base_q, base_scale) = world_pose_scale(src)
        base_q = Quaternion((base_q[3], base_q[0], base_q[1], base_q[2]))
        angles = (random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_x else 0.0, random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_y else 0.0, random.uniform(-math.pi, math.pi) if st.advanced_random_rotation and st.random_rot_z else 0.0)
        q = (base_q @ Euler(angles, 'XYZ').to_quaternion()).normalized()
        (mf, mr, md) = _physics_material(src)
        h = add_body(self.world, World.DYNAMIC, src, self.depsgraph, p, (q.x, q.y, q.z, q.w), st.convex_simplify, prepared=self.source_collision[src.name], instance_scale=scale_factor, friction=mf, restitution=mr, density=md)
        if self.fast_mode:
            idx = len(self.dynamic_handles)
            self.dynamic_objs.append(None)
            self.dynamic_cache.append([float(p.x), float(p.y), float(p.z), float(q.x), float(q.y), float(q.z), float(q.w)])
            self.fast_groups[src.name]['indices'].append(idx)
        else:
            dup = src.copy()
            if dup.animation_data:
                dup.animation_data_clear()
            dup.parent = None
            dup.tumblify.role = 'NONE'
            dup.hide_select = False
            self.collection.objects.link(dup)
            dup.matrix_world = Matrix.LocRotScale(p, q, Vector(base_scale) * scale_factor)
            dup['Tumblify_SourceName'] = src.name
            dup['Tumblify_InstanceScale'] = float(scale_factor)
            dup[FROZEN_PROP] = False
            dup[RUNTIME_PROP] = True
            _configure_instance_display(dup, src, scene)
            self.dynamic_objs.append(dup)
            self.session_objects.append(dup)
            self.dynamic_cache.append([float(p.x), float(p.y), float(p.z), float(q.x), float(q.y), float(q.z), float(q.w)])
        self.session_indices.add(len(self.dynamic_handles))
        self.dynamic_handles.append(h)
        self.dynamic_source_names.append(src.name)
        self.dynamic_scales.append(scale_factor)

    def _sync(self):
        if not self.dynamic_handles:
            return
        (vals, awake) = self.world.get_bulk(self.dynamic_handles)
        _solid_state_sync(self,awake)
        while len(self.dynamic_cache) < len(vals):
            v = vals[len(self.dynamic_cache)]
            self.dynamic_cache.append(list(v))
        if self.fast_mode:
            for (i, (v, a)) in enumerate(zip(vals, awake)):
                if i >= len(self.dynamic_cache):
                    break
                if i in self.kinematic_indices or not a:
                    continue
                self.dynamic_cache[i] = v
                if i < len(self.dynamic_objs) and self.dynamic_objs[i] is not None:
                    o = self.dynamic_objs[i]
                    if _is_live_object(o):
                        o.location = (v[0], v[1], v[2])
                        o.rotation_mode = 'QUATERNION'
                        o.rotation_quaternion = (v[6], v[3], v[4], v[5])
            for group in self.fast_groups.values():
                _fast_proxy_write(group, self.dynamic_cache, self.dynamic_scales, awake)
        else:
            for (i, (o, v, a)) in enumerate(zip(self.dynamic_objs, vals, awake)):
                if i >= len(self.dynamic_cache):
                    break
                if i in self.kinematic_indices or not a or (not _is_live_object(o)):
                    continue
                self.dynamic_cache[i] = v
                o.location = (v[0], v[1], v[2])
                o.rotation_mode = 'QUATERNION'
                o.rotation_quaternion = (v[6], v[3], v[4], v[5])
            try:
                if _regular_display_proxy_needed(bpy.context.scene.tumblify):
                    _sync_regular_display_proxy(bpy.context.scene)
            except Exception:
                pass

    def _materialize_fast(self):
        if not self.fast_mode:
            return
        for (i, (src_name, sc)) in enumerate(zip(self.dynamic_source_names, self.dynamic_scales)):
            if i < len(self.dynamic_objs) and self.dynamic_objs[i] is not None:
                continue
            src = bpy.data.objects.get(src_name)
            if not src or i >= len(self.dynamic_cache):
                continue
            v = self.dynamic_cache[i]
            q = Quaternion((v[6], v[3], v[4], v[5]))
            (_, _, base_scale) = world_pose_scale(src)
            dup = src.copy()
            if dup.animation_data:
                dup.animation_data_clear()
            dup.parent = None
            dup.tumblify.role = 'NONE'
            dup.hide_select = False
            self.collection.objects.link(dup)
            _sync_visual_state_from_source(src, dup)
            dup.matrix_world = Matrix.LocRotScale(Vector((v[0], v[1], v[2])), q, Vector(base_scale) * float(sc))
            dup['Tumblify_SourceName'] = src_name
            dup['Tumblify_InstanceScale'] = float(sc)
            dup[FROZEN_PROP] = True
            dup[RUNTIME_PROP] = True
            dup[FIXED_PROP] = False
            _remove_display_modifier(dup)
            _restore_instance_display_type(dup)
            try:
                dup.display_type = 'TEXTURED'
                dup.hide_viewport = False
                dup.hide_render = False
            except Exception:
                pass
            self.dynamic_objs[i] = dup
        _clear_fast_collection()
        self.fast_groups.clear()
        self.fast_mode = False

    def _fast_interactive_rebuild_groups(self):
        """Keep all non-materialized Fast bodies inside the shared GN batches."""
        if not getattr(self, 'fast_mode', False):
            return
        materialized = set(getattr(self, 'fast_interactive_materialized', set()) or set())
        for g in getattr(self, 'fast_groups', {}).values():
            g['indices'] = []
        preexisting = set(getattr(self, 'preexisting_dynamic', set()) or set()) | set(getattr(self, '_promoted_static_objects', set()))
        for (i, src_name) in enumerate(getattr(self, 'dynamic_source_names', [])):
            if i in materialized:
                continue
            live_obj = self.dynamic_objs[i] if i < len(getattr(self, 'dynamic_objs', [])) else None
            if _is_live_object(live_obj) and live_obj in preexisting:
                continue
            g = getattr(self, 'fast_groups', {}).get(str(src_name))
            if g is not None:
                g['indices'].append(i)
        for g in getattr(self, 'fast_groups', {}).values():
            _fast_proxy_write(g, self.dynamic_cache, self.dynamic_scales)

    def _fast_interactive_materialize_index(self, context, index, select=True, rebuild=True):
        """Expose exactly one Fast body as a real Blender object for G/R/Delete."""
        if index < 0 or index >= len(self.dynamic_handles):
            return None
        existing = self.dynamic_objs[index] if index < len(self.dynamic_objs) else None
        if _is_live_object(existing):
            if select:
                try:
                    existing.hide_select = False
                    existing.select_set(True)
                    context.view_layer.objects.active = existing
                except Exception:
                    pass
            self.fast_interactive_materialized.add(index)
            if rebuild:
                self._fast_interactive_rebuild_groups()
            return existing
        src_name = self.dynamic_source_names[index] if index < len(self.dynamic_source_names) else ''
        src = bpy.data.objects.get(str(src_name))
        if src is None or not _is_live_object(src) or index >= len(self.dynamic_cache):
            return None
        v = self.dynamic_cache[index]
        q = Quaternion((v[6], v[3], v[4], v[5])).normalized()
        (_, _, base_scale) = world_pose_scale(src)
        sc = float(self.dynamic_scales[index]) if index < len(self.dynamic_scales) else 1.0
        dup = src.copy()
        if dup.animation_data:
            dup.animation_data_clear()
        dup.parent = None
        dup.tumblify.role = 'NONE'
        dup.hide_select = False
        self.collection.objects.link(dup)
        _sync_visual_state_from_source(src, dup)
        dup.matrix_world = Matrix.LocRotScale(Vector((v[0], v[1], v[2])), q, Vector(base_scale) * sc)
        dup['Tumblify_SourceName'] = src_name
        dup['Tumblify_InstanceScale'] = sc
        dup[FROZEN_PROP] = False
        dup[RUNTIME_PROP] = True
        dup[FIXED_PROP] = False
        _remove_display_modifier(dup)
        _restore_instance_display_type(dup)
        try:
            dup.display_type = 'TEXTURED'
            dup.hide_viewport = False
            dup.hide_render = False
        except Exception:
            pass
        self.dynamic_objs[index] = dup
        if dup not in getattr(self, 'session_objects', []):
            self.session_objects.append(dup)
        self.fast_interactive_materialized.add(index)
        if select:
            try:
                dup.select_set(True)
                context.view_layer.objects.active = dup
            except Exception:
                pass
        if rebuild:
            self._fast_interactive_rebuild_groups()
        return dup

    def _fast_interactive_rebatch_index(self, index, rebuild=True):
        """Return one deselected interactive object to the Fast batch."""
        if index not in getattr(self, 'fast_interactive_materialized', set()):
            return False
        if index in getattr(self, 'kinematic_indices', set()):
            try:
                self._convert_dynamic_to_dynamic(index)
            except Exception:
                return False
        o = self.dynamic_objs[index] if index < len(self.dynamic_objs) else None
        if _is_live_object(o) and o not in set(getattr(self, 'preexisting_dynamic', set())) | set(getattr(self, '_promoted_static_objects', set())):
            try:
                bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                return False
            self.dynamic_objs[index] = None
            try:
                self.session_objects = [x for x in self.session_objects if _is_live_object(x) and x is not o]
            except Exception:
                pass
        self.fast_interactive_materialized.discard(index)
        if rebuild:
            self._fast_interactive_rebuild_groups()
        return True

    def _fast_interactive_clear_selection(self, context, except_indices=None, rebuild=True):
        """Return explicitly deselected materialized bodies to the shared batch.

        Batched Interactive owns body selection itself.  Do not poll
        context.selected_objects every TIMER: operators launched from the N-panel and
        GN/Combined results can expose a transient Blender selection state for one
        depsgraph update, which used to immediately rebatch a just-picked body.
        """
        keep = set(except_indices or ())
        changed = False
        for i in tuple(getattr(self, 'fast_interactive_materialized', set())):
            if i in keep:
                continue
            o = self.dynamic_objs[i] if i < len(self.dynamic_objs) else None
            if _is_live_object(o):
                try:
                    o.select_set(False)
                except Exception:
                    pass
            changed = self._fast_interactive_rebatch_index(i, False) or changed
        if changed and rebuild:
            self._fast_interactive_rebuild_groups()
        return changed

    def _batched_interactive_active(self):
        """True when Live Interactive is backed by shared GN batches.

        Do not key selection behavior off the Fast UI mode. Advanced Realtime can
        use the exact same batched representation, and its click/box selection must
        therefore follow the same path.
        """
        return bool(getattr(self, 'live_interactive', False) and (getattr(self, 'fast_groups', None) or (getattr(self, 'fast_mode', False) and getattr(self, 'dynamic_cache', None))))

    def _fast_interactive_rebatch_unselected(self, context):
        """Compatibility helper for explicit selection changes only.

        This is intentionally NOT called from TIMER anymore.  Interactive selection
        is authoritative in fast_interactive_materialized and changes only in the
        custom click / shift-click / box-select handlers.
        """
        if not self._batched_interactive_active():
            return
        selected = {o for o in getattr(context, 'selected_objects', []) if _is_live_object(o)}
        changed = False
        for i in tuple(getattr(self, 'fast_interactive_materialized', set())):
            o = self.dynamic_objs[i] if i < len(self.dynamic_objs) else None
            if not _is_live_object(o) or o not in selected:
                changed = self._fast_interactive_rebatch_index(i, False) or changed
        if changed:
            self._fast_interactive_rebuild_groups()

    def _fast_body_pick_geometry(self, index):
        """Evaluated visual geometry and the same pose/scale used by materialization."""
        cache = getattr(self, 'dynamic_cache', [])
        if index < 0 or index >= len(cache) or index >= len(self.dynamic_source_names):
            return None
        obj = self.dynamic_objs[index] if index < len(self.dynamic_objs) else None
        if _is_live_object(obj):
            matrix = obj.matrix_world.copy()
        else:
            obj = bpy.data.objects.get(str(self.dynamic_source_names[index]))
            if not _is_live_object(obj):
                return None
            pose = cache[index]
            q = Quaternion((pose[6], pose[3], pose[4], pose[5])).normalized()
            factor = float(self.dynamic_scales[index]) if index < len(self.dynamic_scales) else 1.0
            (_, _, scale) = world_pose_scale(obj)
            matrix = Matrix.LocRotScale(Vector(pose[:3]), q, Vector(scale) * factor)
        try:
            evaluated = obj.evaluated_get(self.depsgraph)
        except (AttributeError, ReferenceError, RuntimeError):
            return None
        return (evaluated, matrix)

    def _fast_body_world_corners(self, index):
        geometry = self._fast_body_pick_geometry(index)
        if geometry is None:
            return None
        (obj, matrix) = geometry
        return [matrix @ Vector(v) for v in obj.bound_box]

    @staticmethod
    def _fast_ray_bounds(origin, direction, corners):
        """Ray vs visual bounds, retaining zero-width slabs for flat geometry."""
        if not corners or len(corners) != 8:
            return None
        p = corners[0]
        edges = [corners[k] - p for k in (4, 3, 1)]
        lengths = [edge.length for edge in edges]
        axes = [edge / length if length > 1e-12 else None for (edge, length) in zip(edges, lengths)]
        for (i, axis) in enumerate(axes):
            if axis is not None:
                continue
            known = [v for v in axes if v is not None]
            if len(known) >= 2:
                replacement = known[0].cross(known[1])
            elif known:
                seed = min((Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))), key=lambda v: abs(v.dot(known[0])))
                replacement = known[0].cross(seed)
            else:
                replacement = Vector((1, 0, 0))
            if replacement.length < 1e-12:
                return None
            axes[i] = replacement.normalized()
        try:
            inverse = Matrix(axes).transposed().inverted()
        except ValueError:
            return None
        local_origin = inverse @ (origin - p)
        local_dir = inverse @ direction
        tmin = -float('inf')
        tmax = float('inf')
        for (coord, speed, length) in zip(local_origin, local_dir, lengths):
            if abs(speed) < 1e-12:
                if coord < -1e-07 or coord > length + 1e-07:
                    return None
                continue
            t0 = -coord / speed
            t1 = (length - coord) / speed
            if t0 > t1:
                (t0, t1) = (t1, t0)
            tmin = max(tmin, t0)
            tmax = min(tmax, t1)
            if tmax < tmin - 1e-07:
                return None
        if tmax < 0:
            return None
        return max(0.0, tmin)

    def _interactive_mouse_ray(self, context, event):
        region = getattr(self, '_region', None) or getattr(context, 'region', None)
        rv3d = getattr(self, '_rv3d', None) or getattr(getattr(context, 'space_data', None), 'region_3d', None)
        if region is None or rv3d is None:
            return None
        coord = self._interactive_region_coord(event)
        try:
            return (view3d_utils.region_2d_to_origin_3d(region, rv3d, coord), view3d_utils.region_2d_to_vector_3d(region, rv3d, coord).normalized())
        except Exception:
            return None

    @staticmethod
    def _interactive_ray_mesh(obj, matrix, origin, direction):
        """Return forward world-space surface depth, including nonuniform scale."""
        try:
            inv = matrix.inverted()
            local_dir = inv.to_3x3() @ direction
            if local_dir.length < 1e-12:
                return None
            (hit, loc, _normal, _face) = obj.ray_cast(inv @ origin, local_dir.normalized())
            if not hit:
                return None
            depth = (matrix @ loc - origin).dot(direction)
            return depth if depth >= 0 else None
        except (ValueError, ReferenceError, RuntimeError):
            return None

    def _fast_pick_body_hit(self, context, event, ray=None):
        ray = ray or self._interactive_mouse_ray(context, event)
        if ray is None:
            return (None, float('inf'))
        (origin, direction) = ray
        best = None
        depth = float('inf')
        for i in range(len(getattr(self, 'dynamic_cache', []))):
            geometry = self._fast_body_pick_geometry(i)
            if geometry is None:
                continue
            (obj, matrix) = geometry
            corners = [matrix @ Vector(v) for v in obj.bound_box]
            broad = self._fast_ray_bounds(origin, direction, corners)
            if broad is None or broad > depth:
                continue
            hit = self._interactive_ray_mesh(obj, matrix, origin, direction)
            if hit is not None and hit < depth:
                best = i
                depth = hit
        return (best, depth)

    def _fast_pick_body_index(self, context, event):
        """Nearest visible body surface; oriented bounds only reject candidates."""
        return self._fast_pick_body_hit(context, event)[0]

    def _fast_interactive_pick(self, context, event):
        ray = self._interactive_mouse_ray(context, event)
        (idx, body_depth) = self._fast_pick_body_hit(context, event, ray)
        (collider, static_depth) = self._fast_pick_static_hit(context, event, ray)
        if static_depth < body_depth:
            idx = None
        shift = bool(event.shift)
        ctrl = bool(event.ctrl)
        materialized = set(getattr(self, 'fast_interactive_materialized', set()) or set())
        if idx is None:
            if not shift and (not ctrl):
                self._fast_interactive_clear_selection(context, rebuild=False)
                for o in list(context.selected_objects):
                    try:
                        o.select_set(False)
                    except Exception:
                        pass
                self._fast_interactive_rebuild_groups()
            if collider is not None:
                if ctrl:
                    collider.select_set(False)
                elif shift:
                    collider.select_set(not collider.select_get())
                else:
                    collider.select_set(True)
                if collider.select_get():
                    context.view_layer.objects.active = collider
                return True
            return False
        existing = self.dynamic_objs[idx] if idx < len(self.dynamic_objs) else None
        if idx in materialized and _is_live_object(existing):
            if shift or ctrl:
                try:
                    existing.select_set(False)
                except Exception:
                    pass
                self._fast_interactive_rebatch_index(idx, True)
                return True
            self._fast_interactive_clear_selection(context, except_indices={idx}, rebuild=False)
            try:
                existing.select_set(True)
                context.view_layer.objects.active = existing
            except Exception:
                pass
            self._fast_interactive_rebuild_groups()
            return True
        if ctrl:
            return True
        if not shift:
            self._fast_interactive_clear_selection(context, rebuild=False)
            try:
                for o in list(context.selected_objects):
                    if _is_live_object(o):
                        o.select_set(False)
            except Exception:
                pass
        o = self._fast_interactive_materialize_index(context, idx, True, False)
        self._fast_interactive_rebuild_groups()
        return o is not None

    def _interactive_region_coord(self, event):
        """Mouse coordinate in the VIEW_3D region captured when the modal started."""
        region = getattr(self, '_region', None)
        if region is None:
            return (float(getattr(event, 'mouse_region_x', 0.0)), float(getattr(event, 'mouse_region_y', 0.0)))
        try:
            return (float(event.mouse_x - region.x), float(event.mouse_y - region.y))
        except Exception:
            return (float(getattr(event, 'mouse_region_x', 0.0)), float(getattr(event, 'mouse_region_y', 0.0)))

    def _interactive_bounds_box_hit(self, context, corners, a, b):
        """Rectangle vs projected bounds silhouette, clipped at the near plane."""
        region = getattr(self, '_region', None) or getattr(context, 'region', None)
        rv3d = getattr(self, '_rv3d', None) or getattr(getattr(context, 'space_data', None), 'region_3d', None)
        if region is None or rv3d is None or (not corners):
            return False
        clips = [rv3d.perspective_matrix @ Vector((*v, 1.0)) for v in corners]
        visible = [v for v in clips if v.z + v.w >= 0 and v.w > 1e-08]
        for (i, j) in ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)):
            (p, q) = (clips[i], clips[j])
            dp = p.z + p.w
            dq = q.z + q.w
            if (dp < 0) != (dq < 0):
                point = p.lerp(q, dp / (dp - dq))
                if point.w > 1e-08:
                    visible.append(point)
        points = sorted(set((((v.x / v.w + 1) * region.width * 0.5, (v.y / v.w + 1) * region.height * 0.5) for v in visible)))
        if not points:
            return False
        (x1, x2) = sorted((float(a[0]), float(b[0])))
        (y1, y2) = sorted((float(a[1]), float(b[1])))
        if points[-1][0] < x1 or points[0][0] > x2 or max((p[1] for p in points)) < y1 or (min((p[1] for p in points)) > y2):
            return False

        def cross(o, p, q):
            return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
        lower = []
        upper = []
        for (sequence, hull) in ((points, lower), (reversed(points), upper)):
            for point in sequence:
                while len(hull) >= 2 and cross(hull[-2], hull[-1], point) <= 0:
                    hull.pop()
                hull.append(point)
        hull = lower[:-1] + upper[:-1] if len(points) > 1 else points
        rectangle = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
        for (p, q) in zip(hull, hull[1:] + hull[:1]):
            axis = (p[1] - q[1], q[0] - p[0])
            hp = [v[0] * axis[0] + v[1] * axis[1] for v in hull]
            rp = [v[0] * axis[0] + v[1] * axis[1] for v in rectangle]
            if max(hp) < min(rp) - 1e-07 or max(rp) < min(hp) - 1e-07:
                return False
        return True

    def _fast_interactive_box_indices(self, context, a, b):
        """Bodies whose oriented visual-bounds silhouette intersects the rectangle."""
        return [i for i in range(len(getattr(self, 'dynamic_cache', []))) if self._interactive_bounds_box_hit(context, self._fast_body_world_corners(i), a, b)]

    def _fast_selectable_static(self, o):
        """Eligible static primitives: never silently promote a triangle Mesh."""
        if not _is_live_object(o):
            return False
        try:
            return str((getattr(self, 'static_collision', {}).get(o.name) or {}).get('shape', 'MESH')) not in {'MESH', 'TERRAIN'} and (not bool(o.hide_select))
        except Exception:
            return False

    def _fast_static_screen_hits(self, context, a, b):
        hits = []
        for obj in getattr(self, 'static_objs', []):
            if not self._fast_selectable_static(obj):
                continue
            try:
                evaluated = obj.evaluated_get(self.depsgraph)
                corners = [obj.matrix_world @ Vector(v) for v in evaluated.bound_box]
                if self._interactive_bounds_box_hit(context, corners, a, b):
                    hits.append(obj)
            except (ReferenceError, RuntimeError):
                continue
        return hits

    def _fast_pick_static_hit(self, context, event, ray=None):
        ray = ray or self._interactive_mouse_ray(context, event)
        if ray is None:
            return (None, float('inf'))
        (origin, direction) = ray
        nearest = None
        depth = float('inf')
        for obj in getattr(self, 'static_objs', []):
            if not _is_live_object(obj):
                continue
            try:
                if obj.hide_viewport or obj.hide_get():
                    continue
                evaluated = obj.evaluated_get(self.depsgraph)
            except (ReferenceError, RuntimeError):
                continue
            hit = self._interactive_ray_mesh(evaluated, obj.matrix_world, origin, direction)
            if hit is not None and hit < depth:
                nearest = obj
                depth = hit
        return (nearest if nearest is not None and self._fast_selectable_static(nearest) else None, depth)

    def _fast_interactive_apply_box(self, context, a, b, mode='REPLACE'):
        """Apply custom batched Box Select without materializing the entire pile."""
        hits = self._fast_interactive_box_indices(context, a, b)
        static_hits = self._fast_static_screen_hits(context, a, b)
        hitset = set(hits)
        mode = str(mode or 'REPLACE').upper()
        if mode == 'REPLACE':
            try:
                for o in list(context.selected_objects):
                    if _is_live_object(o):
                        o.select_set(False)
            except Exception:
                pass
            self._fast_interactive_clear_selection(context, rebuild=False)
        if mode == 'SUBTRACT':
            for o in static_hits:
                try:
                    o.select_set(False)
                except Exception:
                    pass
            changed = False
            for i in tuple(getattr(self, 'fast_interactive_materialized', set())):
                if i not in hitset:
                    continue
                o = self.dynamic_objs[i] if i < len(self.dynamic_objs) else None
                if _is_live_object(o):
                    try:
                        o.select_set(False)
                    except Exception:
                        pass
                changed = self._fast_interactive_rebatch_index(i, False) or changed
            if changed:
                self._fast_interactive_rebuild_groups()
            return len(hits)
        active = None
        changed = False
        for i in hits:
            o = self._fast_interactive_materialize_index(context, i, True, False)
            if o is not None:
                active = o
                changed = True
        for o in static_hits:
            try:
                o.select_set(True)
                if active is None:
                    active = o
            except Exception:
                pass
        if changed:
            self._fast_interactive_rebuild_groups()
        if active is not None:
            try:
                context.view_layer.objects.active = active
            except Exception:
                pass
        return len(hits) + len(static_hits)

    def _convert_dynamic_to_kinematic(self, index):
        if index < 0 or index >= len(self.dynamic_objs) or index in self.kinematic_indices:
            return False
        o = self.dynamic_objs[index]
        if not _is_live_object(o):
            return False
        old = self.dynamic_handles[index]
        self.world.destroy(old)
        src = bpy.data.objects.get(self.dynamic_source_names[index])
        if not src:
            return False
        (p, q, _) = world_pose_scale(o)
        data = self.source_collision.get(src.name)
        scale_factor = self.dynamic_scales[index] if index < len(self.dynamic_scales) else 1.0
        (mf, mr, md) = _physics_material(src)
        h = add_body(self.world, World.KINEMATIC, src, self.depsgraph, p, q, bpy.context.scene.tumblify.convex_simplify, prepared=data, instance_scale=scale_factor, friction=mf, restitution=mr, density=md)
        self.dynamic_handles[index] = h
        self.kinematic_indices.add(index)
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
        return True

    def _convert_static_to_kinematic(self, index):
        if index < 0 or index >= len(self.static_objs) or index in self.static_kinematic_indices:
            return False
        o = self.static_objs[index]
        data = self.static_collision.get(o.name)
        if data is None:
            return False
        if str(data.get('shape', 'MESH')) in {'MESH', 'TERRAIN'}:
            return False
        (p, q, _) = world_pose_scale(o)
        h = add_body(self.world, World.KINEMATIC, o, self.depsgraph, p, q, bpy.context.scene.tumblify.convex_simplify, prepared=data, friction=bpy.context.scene.tumblify.static_friction, restitution=0.0, density=0.0)
        self.world.destroy(self.static_handles[index])
        self.static_handles[index] = h
        self.static_kinematic_indices.add(index)
        return True

    def _convert_dynamic_to_dynamic(self, index):
        if index < 0 or index >= len(self.dynamic_objs) or index not in self.kinematic_indices:
            return False
        o = self.dynamic_objs[index]
        if not _is_live_object(o):
            self.kinematic_indices.discard(index)
            return False
        self.world.destroy(self.dynamic_handles[index])
        src = bpy.data.objects.get(self.dynamic_source_names[index]) if index < len(self.dynamic_source_names) else None
        if not src:
            src = o
        (p, q, _) = world_pose_scale(o)
        data = self.source_collision.get(src.name)
        if data is None:
            data = _prepare_collision(src, self.depsgraph, bpy.context.scene.tumblify.convex_simplify)
        sc = self.dynamic_scales[index] if index < len(self.dynamic_scales) else 1.0
        (mf, mr, md) = _physics_material(src)
        h = add_body(self.world, World.DYNAMIC, src, self.depsgraph, p, q, bpy.context.scene.tumblify.convex_simplify, prepared=data, instance_scale=sc, friction=mf, restitution=mr, density=md)
        self.dynamic_handles[index] = h
        self.kinematic_indices.discard(index)
        if index < len(self.dynamic_cache):
            self.dynamic_cache[index] = [float(p.x), float(p.y), float(p.z), float(q[0]), float(q[1]), float(q[2]), float(q[3])]
        return True

    def _convert_static_to_static(self, index):
        if index < 0 or index >= len(self.static_objs) or index not in self.static_kinematic_indices:
            return False
        o = self.static_objs[index]
        if not _is_live_object(o):
            self.static_kinematic_indices.discard(index)
            return False
        self.world.destroy(self.static_handles[index])
        (p, q, _) = world_pose_scale(o)
        data = self.static_collision.get(o.name)
        h = add_body(self.world, World.STATIC, o, self.depsgraph, p, q, bpy.context.scene.tumblify.convex_simplify, prepared=data, friction=bpy.context.scene.tumblify.static_friction, restitution=0.0, density=0.0)
        self.static_handles[index] = h
        self.static_kinematic_indices.discard(index)
        return True

    def _promote_frozen_static_to_kinematic(self, index):
        if index < 0 or index >= len(self.static_objs):
            return False
        o = self.static_objs[index]
        if not o or not bool(o.get(FROZEN_PROP, False)):
            return False
        if self.static_handles[index] is not None:
            self.world.destroy(self.static_handles[index])
        src_name = str(o.get('Tumblify_SourceName', ''))
        src = bpy.data.objects.get(src_name) or o
        (p, q, _) = world_pose_scale(o)
        data = self.static_collision.get(o.name)
        sc = float(o.get('Tumblify_InstanceScale', 1.0))
        (mf, mr, md) = _physics_material(src)
        h = add_body(self.world, World.KINEMATIC, src, self.depsgraph, p, q, bpy.context.scene.tumblify.convex_simplify, prepared=data, instance_scale=sc, friction=mf, restitution=mr, density=md)
        self.static_objs[index] = None
        self.static_handles[index] = None
        di = len(self.dynamic_objs)
        self.dynamic_objs.append(o)
        self.dynamic_handles.append(h)
        self.dynamic_source_names.append(src.name)
        self.dynamic_scales.append(sc)
        self.dynamic_cache.append([float(p.x), float(p.y), float(p.z), float(q[0]), float(q[1]), float(q[2]), float(q[3])])
        self.kinematic_indices.add(di)
        return True

    def _activate_selected_for_move(self, context):
        activated = 0
        rejected_mesh = 0
        self.moving_fill_emitters = set()
        fill_enabled = bool(getattr(context.scene.tumblify, 'fill_emitter', False))
        active_emitters = set(self._active_emitter_objects()) if fill_enabled else set()
        for o in list(context.selected_objects):
            if o in active_emitters:
                data = next((d for d in getattr(self, 'fill_emitters', []) if d and d.get('obj') is o), None)
                if data is None:
                    try:
                        o.select_set(False)
                    except Exception:
                        pass
                    self.report({'WARNING'}, f'Fill Emitter collision data is unavailable for {o.name}')
                    continue
                try:
                    st = context.scene.tumblify
                    handles = _fill_add_interactive_container(self.world, data, st.static_friction, float(getattr(st, 'fill_interactive_margin', 0.01)), getattr(st, 'fill_interactive_proxy_mode', 'AUTO'))
                    if not handles:
                        _fill_restore_static_container(self.world, data, st.static_friction)
                        try:
                            o.select_set(False)
                        except Exception:
                            pass
                        self.report({'WARNING'}, f'Cannot move Fill Emitter {o.name}: moving collision proxy could not be built')
                        continue
                    self.fill_container_handles = []
                    for fd in getattr(self, 'fill_emitters', []):
                        h = fd.get('handle') if fd else None
                        if isinstance(h, (list, tuple)):
                            self.fill_container_handles.extend(h)
                        elif h is not None:
                            self.fill_container_handles.append(h)
                    self.moving_fill_emitters.add(o.name)
                    activated += 1
                except Exception as exc:
                    try:
                        _fill_restore_static_container(self.world, data, context.scene.tumblify.static_friction)
                    except Exception:
                        pass
                    try:
                        o.select_set(False)
                    except Exception:
                        pass
                    self.report({'WARNING'}, f'Cannot move Fill Emitter {o.name}: {exc}')
                continue
            if o in self.dynamic_objs:
                i = self.dynamic_objs.index(o)
                if i not in self.kinematic_indices and self._convert_dynamic_to_kinematic(i):
                    activated += 1
                elif i in self.kinematic_indices:
                    activated += 1
            elif o in self.static_objs:
                i = self.static_objs.index(o)
                if bool(o.get(FROZEN_PROP, False)):
                    if self._promote_frozen_static_to_kinematic(i):
                        activated += 1
                elif str((self.static_collision.get(o.name) or {}).get('shape', 'MESH')) in {'MESH', 'TERRAIN'}:
                    try:
                        o.select_set(False)
                    except Exception:
                        pass
                    rejected_mesh += 1
                elif self._convert_static_to_kinematic(i):
                    activated += 1
        if rejected_mesh:
            self.report({'WARNING'}, f'{rejected_mesh} exact Mesh collider(s) cannot move during simulation')
        return activated

    def _update_fill_emitter_containers(self, force=False):
        if not bool(getattr(self, 'fill_collision_enabled', True)):
            return
        names = set(getattr(self, 'moving_fill_emitters', set()))
        if not names and (not force):
            return
        st = bpy.context.scene.tumblify
        rebuilt = False
        for data in getattr(self, 'fill_emitters', []):
            o = data.get('obj') if data else None
            if not _is_live_object(o):
                continue
            if names and o.name not in names and (not force):
                continue
            moved = _fill_update_pose_from_object(data)
            if moved or force:
                _fill_rebuild_container(self.world, data, st.static_friction)
                rebuilt = True
        if rebuilt:
            self.fill_container_handles = []
            for data in getattr(self, 'fill_emitters', []):
                h = data.get('handle') if data else None
                if isinstance(h, (list, tuple)):
                    self.fill_container_handles.extend(h)
                elif h is not None:
                    self.fill_container_handles.append(h)

    def _finish_fill_emitter_move(self):
        if not getattr(self, 'moving_fill_emitters', None):
            return
        try:
            if bool(getattr(self, 'live_interactive', False)):
                for data in getattr(self, 'fill_emitters', []):
                    o = data.get('obj') if data else None
                    if _is_live_object(o) and o.name in self.moving_fill_emitters:
                        _fill_update_pose_from_object(data)
            else:
                self._update_fill_emitter_containers(force=True)
        finally:
            self.moving_fill_emitters = set()

    def _toggle_fill_collision(self, context):
        """Toggle the runtime Fill Emitter shell without changing the Blender emitter.

        Turning it off releases the spawned pile immediately.  Turning it back on
        rebuilds the shell at the emitter's current transform.
        """
        if not getattr(self, 'fill_emitters', None):
            self.report({'WARNING'}, 'Fill Emitter collision is not active in this run')
            return False
        st = context.scene.tumblify
        enabled = bool(getattr(self, 'fill_collision_enabled', True))
        if enabled:
            for data in self.fill_emitters:
                _fill_destroy_container(self.world, data)
            self.fill_container_handles = []
            self.fill_collision_enabled = False
            self.report({'INFO'}, 'Fill Emitter collision OFF: contents released')
        else:
            self.fill_container_handles = []
            for data in self.fill_emitters:
                _fill_update_pose_from_object(data)
                if bool(getattr(self, 'live_interactive', False)):
                    self.fill_container_handles.extend(_fill_add_interactive_container(self.world, data, st.static_friction, float(getattr(st, 'fill_interactive_margin', 0.01)), getattr(st, 'fill_interactive_proxy_mode', 'AUTO')))
                else:
                    self.fill_container_handles.extend(_fill_add_container(self.world, data, st.static_friction))
            self.fill_collision_enabled = True
            self.report({'INFO'}, 'Fill Emitter collision ON')
        if context.area:
            context.area.tag_redraw()
        return True

    def _step_fill_emitter_motion(self, context):
        """Drive the Fill Emitter as a real kinematic compound with adaptive substeps."""
        if not bool(getattr(self, 'fill_collision_enabled', True)):
            return False
        names = set(getattr(self, 'moving_fill_emitters', set()))
        if not names:
            return False
        moving = []
        max_dist = 0.0
        max_ang = 0.0
        for data in getattr(self, 'fill_emitters', []):
            o = data.get('obj') if data else None
            if not _is_live_object(o) or o.name not in names:
                continue
            h = data.get('handle')
            if not data.get('interactive_proxy') or h is None:
                continue
            (p, q, _) = world_pose_scale(o)
            tp = Vector(p)
            tq = Quaternion((q[3], q[0], q[1], q[2])).normalized()
            sp = Vector(data.get('origin', p))
            sq = data.get('rot', tq).copy().normalized()
            dist = (tp - sp).length
            max_dist = max(max_dist, dist)
            try:
                dot = max(-1.0, min(1.0, abs(float(sq.dot(tq)))))
                max_ang = max(max_ang, 2.0 * math.acos(dot))
            except Exception:
                pass
            moving.append((data, int(h), sp, sq, tp, tq))
        if not moving:
            return False
        st = context.scene.tumblify
        margin = max(0.001, float(getattr(st, 'fill_interactive_margin', 0.01)))
        linear_step = max(0.003, margin * 0.75, self._emitter_radius() * 0.01)
        linear_steps = int(math.ceil(max_dist / linear_step)) if max_dist > 1e-09 else 1
        angular_steps = int(math.ceil(max_ang / math.radians(2.0))) if max_ang > 1e-09 else 1
        steps = max(2, min(24, max(linear_steps, angular_steps)))
        subdt = self.dt / float(steps)
        motion_substeps = max(2, min(8, int(st.physics_substeps) * 2))
        for step in range(1, steps + 1):
            t = step / float(steps)
            for (data, h, sp, sq, tp, tq) in moving:
                pp = sp.lerp(tp, t)
                try:
                    qq = sq.slerp(tq, t)
                except Exception:
                    qq = tq.copy()
                self.world.target(h, pp, (qq.x, qq.y, qq.z, qq.w), subdt)
            self._update_object_kinematics()
            self.world.step(subdt, motion_substeps)
        for (data, h, sp, sq, tp, tq) in moving:
            data['origin'] = tp
            data['rot'] = tq
        return True

    @staticmethod
    def _remap_runtime_indices(indices, removed):
        removed = sorted(set(removed))
        out = set()
        for old in set(indices):
            if old in removed:
                continue
            out.add(old - sum((1 for r in removed if r < old)))
        return out

    def _remove_dynamic_runtime_indices(self, indices, delete_objects=False):
        removed = sorted({i for i in indices if 0 <= i < len(self.dynamic_objs)})
        if not removed:
            return 0
        for i in reversed(removed):
            try:
                if i < len(self.dynamic_handles) and self.dynamic_handles[i] is not None:
                    self.world.destroy(self.dynamic_handles[i])
            except Exception:
                pass
            o = self.dynamic_objs[i] if i < len(self.dynamic_objs) else None
            for arrname in ('dynamic_objs', 'dynamic_handles', 'dynamic_source_names', 'dynamic_scales', 'dynamic_cache'):
                arr = getattr(self, arrname, None)
                if arr is not None and i < len(arr):
                    arr.pop(i)
            if delete_objects and _is_live_object(o):
                try:
                    bpy.data.objects.remove(o, do_unlink=True)
                except Exception:
                    pass
        self.kinematic_indices = self._remap_runtime_indices(getattr(self, 'kinematic_indices', set()), removed)
        self.session_indices = self._remap_runtime_indices(getattr(self, 'session_indices', set()), removed)
        try:
            self.session_objects = [o for o in self.session_objects if _is_live_object(o)]
        except Exception:
            pass
        try:
            for group in getattr(self, 'fast_groups', {}).values():
                old_indices = list(group.get('indices', []))
                new = []
                for old in old_indices:
                    if old in removed:
                        continue
                    new.append(old - sum((1 for r in removed if r < old)))
                group['indices'] = new
        except Exception:
            pass
        return len(removed)

    def _prune_removed_runtime_objects(self):
        dead = [i for (i, o) in enumerate(list(getattr(self, 'dynamic_objs', []))) if o is not None and (not _is_live_object(o))]
        if dead:
            self._remove_dynamic_runtime_indices(dead, False)
        dead_static = [i for (i, o) in enumerate(list(getattr(self, 'static_objs', []))) if o is not None and (not _is_live_object(o))]
        if dead_static:
            removed = sorted(set(dead_static))
            for i in reversed(removed):
                try:
                    if i < len(self.static_handles) and self.static_handles[i] is not None:
                        self.world.destroy(self.static_handles[i])
                except Exception:
                    pass
                if i < len(self.static_objs):
                    self.static_objs.pop(i)
                if i < len(self.static_handles):
                    self.static_handles.pop(i)
            self.static_kinematic_indices = self._remap_runtime_indices(getattr(self, 'static_kinematic_indices', set()), removed)
            try:
                old_count = int(getattr(self, 'fast_static_count', 0))
                self.fast_static_count = max(0, old_count - sum((1 for i in removed if i < old_count)))
            except Exception:
                pass

    def _delete_selected_live_runtime(self, context):
        selected = [o for o in list(context.selected_objects) if _is_live_object(o)]
        if not selected:
            return (0, 0)
        dyn = []
        unsupported = 0
        for o in selected:
            found = False
            for (i, d) in enumerate(self.dynamic_objs):
                if _is_live_object(d) and d == o:
                    dyn.append(i)
                    found = True
                    break
            if not found:
                unsupported += 1
        count = self._remove_dynamic_runtime_indices(dyn, True)
        return (count, unsupported)

    def _release_unselected_kinematics(self, context):
        self._prune_removed_runtime_objects()
        selected = {o for o in context.selected_objects if _is_live_object(o)}
        for i in tuple(self.kinematic_indices):
            o = self.dynamic_objs[i] if i < len(self.dynamic_objs) else None
            if not _is_live_object(o):
                self.kinematic_indices.discard(i)
                continue
            if o not in selected:
                self._convert_dynamic_to_dynamic(i)
        for i in tuple(self.static_kinematic_indices):
            o = self.static_objs[i] if i < len(self.static_objs) else None
            if not _is_live_object(o):
                self.static_kinematic_indices.discard(i)
                continue
            if o not in selected:
                _release_static_as_dynamic(self, i)

    def _release_all_kinematics(self):
        self._prune_removed_runtime_objects()
        for i in tuple(self.kinematic_indices):
            self._convert_dynamic_to_dynamic(i)
        for i in tuple(self.static_kinematic_indices):
            _release_static_as_dynamic(self, i)

    def _update_object_kinematics(self):
        self._prune_removed_runtime_objects()
        for i in tuple(self.kinematic_indices):
            if i >= len(self.dynamic_objs):
                continue
            o = self.dynamic_objs[i]
            if not _is_live_object(o):
                continue
            (p, q, _) = world_pose_scale(o)
            self.world.target(self.dynamic_handles[i], p, q, self.dt)
        for i in tuple(self.static_kinematic_indices):
            if i >= len(self.static_objs):
                continue
            o = self.static_objs[i]
            if not _is_live_object(o):
                continue
            (p, q, _) = world_pose_scale(o)
            self.world.target(self.static_handles[i], p, q, self.dt)

    def _adjust_emitter_size(self, context, factor):
        st = context.scene.tumblify
        if getattr(self, 'emitter_mode', 'FIXED') in {'CUSTOM', 'COLLECTION'}:
            for eo in self._active_emitter_objects():
                eo.scale = tuple((max(0.001, float(v) * factor) for v in eo.scale))
        else:
            st.emitter_radius = max(0.05, min(50.0, float(st.emitter_radius) * factor))
        update_emitter_guide(context.scene)
        if context.area:
            context.area.tag_redraw()

    def _fast_random_rotation_for_source(self, src):
        try:
            (_, base_q, _) = world_pose_scale(src)
            q = Quaternion((base_q[3], base_q[0], base_q[1], base_q[2]))
        except Exception:
            q = Quaternion((1, 0, 0, 0))
        st = bpy.context.scene.tumblify
        q = (_fast_emitter_rotation_quat(bpy.context.scene) @ q).normalized()
        if bool(getattr(st, 'fast_random_rotation', False)):
            angles = (random.uniform(-math.pi, math.pi) if bool(getattr(st, 'fast_random_rot_x', True)) else 0.0, random.uniform(-math.pi, math.pi) if bool(getattr(st, 'fast_random_rot_y', True)) else 0.0, random.uniform(-math.pi, math.pi) if bool(getattr(st, 'fast_random_rot_z', True)) else 0.0)
            q = (q @ Euler(angles, 'XYZ').to_quaternion()).normalized()
        return q

    def _fast_stack_rotate_preview(self, emitter_q):
        """Rigidly rotate Stack about its bottom member, or Spread about the cursor.

        The stacking axis and all offsets rotate with the cursor Empty,
        while the original per-member orientations and scales are preserved.
        """
        initial = getattr(self, 'fast_stack_rotate_initial', None)
        start = getattr(self, 'fast_native_rotate_start_q', None)
        if not initial or start is None:
            return
        if getattr(self,'fast_snap_enabled',False) and getattr(self,'fast_snap_valid',False):
            _fast_snap.rotate(self,emitter_q);return
        delta = (emitter_q @ start.inverted()).normalized()
        anchor = Vector(initial[0][1]) if int(bpy.context.scene.tumblify.fast_objects_per_tick)==1 and int(bpy.context.scene.tumblify.fast_stack_count)>1 else Vector(getattr(self,'fast_native_rotate_pivot',self.fast_preview_target))
        rebuilt = []
        for (src, old_pos, old_quat, factor) in initial:
            quat = (delta @ old_quat).normalized()
            pos = anchor + delta @ (Vector(old_pos) - anchor)
            rebuilt.append((src, pos, quat, factor))
        self.fast_stack_specs = rebuilt
        members=getattr(self,'fast_batch_members',[])
        for member,spec in zip((m for column in members for m in column),rebuilt):member[1]=spec[2].copy()
        samples=getattr(self,'fast_stack_rotate_samples',getattr(self,'fast_batch_samples',[]))
        self.fast_batch_samples=[sample.copy() if int(bpy.context.scene.tumblify.fast_objects_per_tick)==1 and int(bpy.context.scene.tumblify.fast_stack_count)>1 else delta@sample for sample in samples]
        if rebuilt:self.fast_spread_offset=Vector(rebuilt[0][1])-Vector(self.fast_preview_target)
        for (ghost, (src, pos, quat, factor)) in zip(getattr(self, 'fast_stack_ghosts', []) or [], rebuilt):
            if _is_live_object(ghost):
                ghost.matrix_world = Matrix.LocRotScale(pos, quat, Vector(src.scale) * factor)

    def _fast_native_rotate_sync(self, context=None):
        """Mirror the real Blender Empty rotation into Fast spawn orientation/preview.

        The transform itself is owned entirely by Blender's TRANSFORM_OT_rotate.  This
        helper only mirrors the resulting Empty orientation into the Fast scene setting
        and visual held preview; it does not implement any transform interaction.
        """
        scene = getattr(self, '_tumblify_runtime_context', {}).get('scene') or (getattr(context, 'scene', None) if context is not None else None) or bpy.context.scene
        empty = bpy.data.objects.get(FAST_CURSOR_NAME)
        if empty is None:
            return
        try:
            q = empty.matrix_world.to_quaternion().normalized()
        except Exception:
            try:
                q = empty.rotation_quaternion.normalized()
            except Exception:
                return
        if getattr(self,'fast_snap_enabled',False) and getattr(self,'fast_snap_valid',False):
            q=self.fast_snap_rotate_main_align.inverted()@q
        _fast_set_emitter_rotation_quat(scene, q)
        if getattr(self, 'fast_stack_rotate_initial', None):
            self._fast_stack_rotate_preview(q)
            if getattr(context, 'area', None):
                try:
                    context.area.tag_redraw()
                except Exception:
                    pass
            return
        start_q = getattr(self, 'fast_native_rotate_start_q', None)
        start_preview_q = getattr(self, 'fast_native_rotate_preview_q', None)
        o = getattr(self, 'fast_preview_obj', None)
        src = getattr(self, 'fast_preview_source', None)
        if start_q is not None and start_preview_q is not None and _is_live_object(o) and (src is not None) and (o.parent is not empty):
            try:
                delta = (q @ start_q.inverted()).normalized()
                pq = (delta @ start_preview_q).normalized()
                o.matrix_world = Matrix.LocRotScale(Vector(getattr(self, 'fast_preview_target', o.matrix_world.translation)), pq, Vector(src.scale) * float(getattr(self, 'fast_preview_scale', 1.0)))
            except Exception:
                pass
        if getattr(context, 'area', None):
            try:
                context.area.tag_redraw()
            except Exception:
                pass

    def _fast_native_rotate_restore_selection(self, context=None):
        """Restore user selection after Blender finishes rotating the helper Empty."""
        context = context or bpy.context
        empty = bpy.data.objects.get(FAST_CURSOR_NAME)
        try:
            if empty is not None:
                empty.hide_select = bool(getattr(self, 'fast_native_rotate_old_hide_select', True))
                empty.select_set(False, view_layer=context.view_layer)
        except Exception:
            pass
        try:
            for o in list(context.selected_objects):
                o.select_set(False, view_layer=context.view_layer)
            for name in tuple(getattr(self, 'fast_native_rotate_selection', ()) or ()):
                o = bpy.data.objects.get(name)
                if o is not None and (not o.hide_get(view_layer=context.view_layer)):
                    o.select_set(True, view_layer=context.view_layer)
            active = bpy.data.objects.get(str(getattr(self, 'fast_native_rotate_active', '') or ''))
            if active is not None:
                context.view_layer.objects.active = active
        except Exception:
            pass

    def _fast_end_emitter_rotate(self, context, *, rebuild_preview=True):
        """Release Fast rotation state once, before helper deletion on Stop/disable."""
        if not bool(getattr(self, 'fast_native_rotate', False)):
            return
        scene = getattr(self, '_tumblify_runtime_context', {}).get('scene') or context.scene
        ghost = getattr(self, 'fast_preview_obj', None)
        if _is_live_object(ghost) and ghost.parent is not None:
            try:
                wm = ghost.matrix_world.copy()
                ghost.parent = None
                ghost.matrix_world = wm
            except Exception:
                pass
        try:
            if rebuild_preview and getattr(self, 'fast_stack_rotate_initial', None):
                self._fast_stack_rotate_preview(_fast_emitter_rotation_quat(scene))
            elif rebuild_preview and context.scene is scene:
                self._fast_preview_create(context, getattr(self, 'fast_preview_source', None), getattr(self, 'fast_preview_scale', 1.0), True)
        except Exception:
            pass
        self.fast_native_rotate = False
        try:
            self._fast_native_rotate_restore_selection(context)
        except Exception:
            pass
        for name in ('fast_native_rotate_start_q', 'fast_native_rotate_preview_q', 'fast_native_rotate_selection', 'fast_native_rotate_active', 'fast_native_rotate_old_hide_select', 'fast_stack_rotate_initial','fast_native_rotate_pivot','fast_stack_rotate_samples'):
            try:
                delattr(self, name)
            except Exception:
                pass

    def _fast_begin_emitter_rotate(self, context, event=None):
        """Invoke Blender's native Rotate operator on the Fast cursor Empty."""
        (empty, _ray) = _fast_ensure_cursor_helpers(context.scene)
        _fast_update_cursor_helpers(context.scene, getattr(self, 'fast_preview_target', Vector((0, 0, 0))))
        try:
            self.fast_native_rotate_selection = tuple((o.name for o in context.selected_objects))
            self.fast_native_rotate_active = getattr(context.view_layer.objects.active, 'name', '') if context.view_layer.objects.active else ''
        except Exception:
            self.fast_native_rotate_selection = ()
            self.fast_native_rotate_active = ''
        self.fast_native_rotate_old_hide_select = bool(getattr(empty, 'hide_select', True))
        self.fast_native_rotate_start_q = _fast_emitter_rotation_quat(context.scene)
        self.fast_native_rotate_pivot=Vector(self.fast_preview_target).copy()
        self.fast_stack_rotate_samples=[v.copy() for v in getattr(self,'fast_batch_samples',[])]
        self.fast_stack_rotate_initial = [(src, Vector(pos).copy(), quat.copy(), float(scale)) for (src, pos, quat, scale) in getattr(self, 'fast_stack_specs', []) or []] if getattr(self, 'fast_stack_specs', None) else None
        self.fast_snap_rotate_alignments=[q.copy() for q in getattr(self,'fast_snap_alignments',[])]
        self.fast_snap_rotate_members=list(getattr(self,'fast_snap_members_used',[]))
        self.fast_snap_rotate_main_align=Vector((0,0,1)).rotation_difference(getattr(self,'fast_snap_normal',Vector((0,0,1))))
        pose = self._fast_preview_pose()
        self.fast_native_rotate_preview_q = None
        if pose is not None:
            try:
                self.fast_native_rotate_preview_q = Quaternion((pose[6], pose[3], pose[4], pose[5])).normalized()
            except Exception:
                pass
        try:
            for o in list(context.selected_objects):
                o.select_set(False)
            empty.hide_select = False
            empty.hide_set(False)
            empty.select_set(True)
            context.view_layer.objects.active = empty
            empty.rotation_mode = 'QUATERNION'
            empty.rotation_quaternion = (self.fast_snap_rotate_main_align@self.fast_native_rotate_start_q if getattr(self,'fast_snap_enabled',False) and getattr(self,'fast_snap_valid',False) else self.fast_native_rotate_start_q)
            ghost = getattr(self, 'fast_preview_obj', None)
            if _is_live_object(ghost) and (not getattr(self, 'fast_stack_specs', None)):
                wm = ghost.matrix_world.copy()
                ghost.parent = empty
                ghost.matrix_world = wm
            self.fast_native_rotate = True
            bpy.ops.transform.rotate('INVOKE_DEFAULT')
        except Exception as exc:
            self.fast_native_rotate = False
            self._fast_native_rotate_restore_selection()
            self.report({'WARNING'}, f'Blender Rotate could not start: {exc}')
            return False
        runtime = self
        window = context.window
        started_at = time.monotonic()
        seen_transform = [False]
        missing_ticks = [0]

        def _poll_native_rotate():
            if _runtime_owner_shutdown(runtime) or not bool(getattr(runtime, 'fast_native_rotate', False)):
                return None
            try:
                with bpy.context.temp_override(**getattr(runtime, '_tumblify_runtime_context', {})):
                    runtime._fast_native_rotate_sync(bpy.context)
            except Exception:
                pass
            try:
                ops = getattr(window, 'modal_operators', ())
                running = any((str(getattr(op, 'bl_idname', '')).lower() in {'transform_ot_rotate', 'transform_ot_trackball'} for op in ops))
                seen_transform[0] = seen_transform[0] or running
                missing_ticks[0] = 0 if running else missing_ticks[0] + 1
                running = running or missing_ticks[0] < 5 or (not seen_transform[0] and time.monotonic() - started_at < 0.7)
            except Exception:
                running = True
            if running:
                return 0.03
            with bpy.context.temp_override(**getattr(runtime, '_tumblify_runtime_context', {})):
                runtime._fast_end_emitter_rotate(bpy.context)
            return None
        try:
            _register_owned_app_timer(_poll_native_rotate, first_interval=0.03)
        except Exception:
            pass
        return True

    def _fast_preview_remove_visual(self):
        # Include inactive pooled ghosts; never leave hidden helpers after a run.
        objects=list(getattr(self, 'fast_stack_ghosts', []) or [])
        objects.append(getattr(self, 'fast_preview_obj', None))
        objects.extend(ghost for _source,ghost in getattr(self, '_fast_preview_pool', []))
        seen=set()
        for ghost in objects:
            if _is_live_object(ghost) and ghost.as_pointer() not in seen:
                seen.add(ghost.as_pointer())
                try:bpy.data.objects.remove(ghost, do_unlink=True)
                except Exception:pass
        self.fast_stack_ghosts=[]
        self.fast_preview_obj=None
        self._fast_preview_pool=[]
        self._fast_preview_used=set()

    def _fast_preview_acquire_visual(self, context, src, p, q, scale):
        # Reuse linked source copies: adding/removing scene objects on every drop
        # rebuilds Blender dependency relations even with instanced simulation.
        pool=getattr(self, '_fast_preview_pool', [])
        used=getattr(self, '_fast_preview_used', set())
        ghost=next((g for source,g in pool if source is src and _is_live_object(g) and g.as_pointer() not in used),None)
        if ghost is None:
            ghost=src.copy()
            if ghost.animation_data:ghost.animation_data_clear()
            ghost.parent=None
            ghost.name=FAST_GHOST_NAME
            context.scene.collection.objects.link(ghost)
            ghost.hide_select=True
            ghost.hide_render=True
            ghost.display_type='TEXTURED'
            ghost['Tumblify_Helper']=True
            try:
                ghost.color=(0.65,0.85,1.0,0.24)
                ghost.show_transparent=True
            except Exception:pass
            pool.append((src,ghost))
        used.add(ghost.as_pointer())
        if ghost.hide_get():ghost.hide_set(False)
        ghost.matrix_world=Matrix.LocRotScale(Vector(p),q,Vector(src.scale)*float(scale))
        self._fast_preview_pool=pool
        self._fast_preview_used=used
        return ghost

    def _fast_preview_finish_visuals(self):
        used=getattr(self, '_fast_preview_used', set())
        pool=[]
        idle=0
        # Retain at most ten spare source copies in addition to active previews.
        for source,ghost in getattr(self, '_fast_preview_pool', []):
            if not _is_live_object(ghost):continue
            if ghost.as_pointer() not in used:
                idle+=1
                if idle>10:
                    bpy.data.objects.remove(ghost,do_unlink=True)
                    continue
                if not ghost.hide_get():ghost.hide_set(True)
            pool.append((source,ghost))
        self._fast_preview_pool=pool

    def _fast_preview_destroy(self):
        h = getattr(self, 'fast_preview_handle', None)
        if h is not None:
            try:
                self.world.destroy(h)
            except Exception:
                pass
        self.fast_preview_handle = None
        self._fast_preview_remove_visual()
        self.fast_snap_picker=None;self.fast_snap_enabled=False;self.fast_snap_valid=False

    def _fast_preview_pose(self):
        o = getattr(self, 'fast_preview_obj', None)
        if _is_live_object(o):
            try:
                (p, q, _s) = o.matrix_world.decompose()
                q = q.normalized()
                return [float(p.x), float(p.y), float(p.z), float(q.x), float(q.y), float(q.z), float(q.w)]
            except Exception:
                pass
        h = getattr(self, 'fast_preview_handle', None)
        if h is None:
            return None
        try:
            (vals, _) = self.world.get_bulk([h])
            return vals[0] if vals else None
        except Exception:
            return None

    def _fast_make_preview_visual(self, context, src, p, q, scale):
        self.fast_preview_obj=self._fast_preview_acquire_visual(context,src,p,q,scale)
        self.fast_stack_ghosts=[]

    def _fast_choose_preview_source(self, other=False):
        sources = list(getattr(self, 'sources', []) or [])
        if not sources:
            return None
        current = getattr(self, 'fast_preview_source', None)
        if other and len(sources) > 1 and (current in sources):
            pool = [o for o in sources if o is not current]
            return random.choice(pool)
        return _weighted_source_choice(sources) or random.choice(sources)

    def _fast_stack_world_aabb(self, source, quat, factor, center):
        """Cheap projected world AABB from the 8 local bound_box corners.

        Unlike source.dimensions.z this accounts for the source pivot, object
        rotation and nonuniform scale.  No evaluated mesh/BVH is needed.
        """
        scale = Vector(source.scale) * float(factor)
        corners = [Vector(center) + quat @ Vector((float(c[0]) * scale.x, float(c[1]) * scale.y, float(c[2]) * scale.z)) for c in source.bound_box]
        return (Vector((min((v.x for v in corners)), min((v.y for v in corners)), min((v.z for v in corners)))), Vector((max((v.x for v in corners)), max((v.y for v in corners)), max((v.z for v in corners)))))

    def _fast_stack_axis_interval(self, source, quat, factor, center, axis):
        """Projection of the eight oriented, scaled bounds onto stack axis."""
        scale = Vector(source.scale) * float(factor)
        values = [(Vector(center) + quat @ Vector((float(c[0]) * scale.x, float(c[1]) * scale.y, float(c[2]) * scale.z))).dot(axis) for c in source.bound_box]
        return (min(values), max(values))

    def _fast_make_stack_preview(self, context, src, p, q, preserve=False):
        """Place independent columns on the cursor plane; preserve random choices on edits."""
        st=context.scene.tumblify
        columns=max(1,min(10,int(st.fast_objects_per_tick)))
        height=max(1,min(10,int(st.fast_stack_count)))
        members=getattr(self,'fast_batch_members',[]) if preserve else []
        samples=getattr(self,'fast_batch_samples',[]) if preserve else []
        members=[list(column[:height]) for column in members[:columns]]
        samples=[v.copy() for v in samples[:columns]]
        manual_q=_fast_emitter_rotation_quat(context.scene)
        axis=(manual_q@Vector((0,0,1))).normalized()
        smin,smax=sorted((float(st.fast_scale_min),float(st.fast_scale_max)))
        for col in range(columns):
            if col>=len(members):members.append([])
            if col>=len(samples):
                angle=random.uniform(0.0,math.tau);radius=math.sqrt(random.random())
                samples.append(manual_q@Vector((radius*math.cos(angle),radius*math.sin(angle),0)))
            while len(members[col])<height:
                first=col==0 and not members[col]
                source=src if first else self._fast_choose_preview_source(False)
                if source is None:break
                quat=q.copy() if first else self._fast_random_rotation_for_source(source)
                members[col].append([source,quat,random.uniform(smin,smax)])
        self.fast_batch_members=members
        self.fast_batch_samples=samples
        if getattr(self,'fast_snap_enabled',False):
            _fast_snap.rebuild(self,context);return
        specs=[];ghosts=[]
        origin=Vector(self.fast_preview_target)
        for column,sample in zip(members,samples):
            base=origin+sample*float(st.fast_spread)
            top=None
            for source,quat,random_factor in column:
                factor=float(self.fast_preview_scale)*random_factor
                pos=base.copy()
                lo,hi=self._fast_stack_axis_interval(source,quat,factor,pos,axis)
                if top is not None:
                    pos+=axis*(top+0.005-lo)
                    lo,hi=self._fast_stack_axis_interval(source,quat,factor,pos,axis)
                top=hi
                specs.append((source,pos,quat.copy(),factor))
                ghosts.append(self._fast_preview_acquire_visual(context,source,pos,quat,factor))
        self.fast_stack_specs=specs
        self.fast_stack_ghosts=ghosts
        self.fast_preview_obj=ghosts[0] if ghosts else None
        self.fast_spread_offset=specs[0][1]-origin if specs else Vector((0,0,0))

    def _fast_preview_create(self, context, src=None, scale=None, preserve_rotation=False):
        if not getattr(self,'fast_runtime',False):return False
        self._fast_preview_used=set()
        src=src or self._fast_choose_preview_source(False)
        if src is None:return False
        self.fast_preview_source=src
        if scale is None:scale=float(getattr(self,'fast_preview_scale',1.0) or 1.0)
        self.fast_preview_scale=max(0.05,min(20.0,float(scale)))
        self.fast_preview_handle=None
        q=self._fast_random_rotation_for_source(src) if not preserve_rotation else Quaternion((1,0,0,0))
        self._fast_make_stack_preview(context,src,self.fast_preview_target,q,preserve_rotation)
        self._fast_preview_finish_visuals()
        return bool(self.fast_stack_specs)

    def _fast_preview_sync_visual(self):
        if getattr(self, 'fast_stack_specs', None):
            return
        pose = self._fast_preview_pose()
        o = getattr(self, 'fast_preview_obj', None)
        src = getattr(self, 'fast_preview_source', None)
        if pose is None or not _is_live_object(o) or src is None:
            return
        try:
            p = Vector((pose[0], pose[1], pose[2]))
            q = Quaternion((pose[6], pose[3], pose[4], pose[5]))
            o.matrix_world = Matrix.LocRotScale(p, q, Vector(src.scale) * float(self.fast_preview_scale))
        except Exception:
            pass

    def _fast_preview_set_target(self, context, target):
        """Move the visual-only held Fast preview to the mouse target."""
        if getattr(self,'fast_snap_enabled',False):
            self.fast_snap_point=Vector(target);self.fast_snap_valid=True
            self.fast_preview_target=Vector(target);self._fast_preview_used=set()
            _fast_snap.rebuild(self,context);self._fast_preview_finish_visuals();return
        self.fast_preview_target = Vector(target)
        _fast_update_cursor_helpers(context.scene, self.fast_preview_target)
        spread_target = self.fast_preview_target + getattr(self, 'fast_spread_offset', Vector((0, 0, 0)))
        if getattr(self, 'fast_stack_specs', None):
            old_base = self.fast_stack_specs[0][1]
            delta = spread_target - old_base
            self.fast_stack_specs = [(src, p + delta, q, scale) for (src, p, q, scale) in self.fast_stack_specs]
            for (ghost, (_src, pos, quat, scale)) in zip(getattr(self, 'fast_stack_ghosts', []), self.fast_stack_specs):
                if _is_live_object(ghost):
                    ghost.matrix_world = Matrix.LocRotScale(pos, quat, Vector(_src.scale) * scale)
            return
        o = getattr(self, 'fast_preview_obj', None)
        src = getattr(self, 'fast_preview_source', None)
        if _is_live_object(o) and src is not None:
            try:
                (_p, q, _s) = o.matrix_world.decompose()
                o.matrix_world = Matrix.LocRotScale(spread_target, q.normalized(), Vector(src.scale) * float(self.fast_preview_scale))
            except Exception:
                pass

    def _fast_preview_hold(self):
        if getattr(self,'fast_snap_enabled',False) and time.monotonic()-getattr(self,'fast_snap_last_refresh',0)>.05:
            _fast_snap.refresh(self,bpy.context)
        return

    def _fast_set_source_collision(self, context, src, shape):
        if src is None or not _is_live_object(src):
            return False
        shape = str(shape)
        if shape not in FAST_COLLISION_CYCLE:
            shape = 'DETECT'
        src.tumblify.shape = shape
        fit = _fast_collision_fit(src, self.depsgraph)
        self.fast_source_fits[src.name] = fit
        self.fast_source_shapes[src.name] = fit.get('shape', 'HULL')
        self.source_collision[src.name] = _prepare_collision(src, self.depsgraph, context.scene.tumblify.convex_simplify, shape_override=fit)
        try:
            _cache_native_collision_assets(self.world, self.source_collision[src.name])
        except Exception:
            pass
        _fast_refresh_shape_summary(context.scene, self.depsgraph)
        if getattr(self, 'fast_preview_source', None) is src:
            self._fast_preview_create(context, src, getattr(self, 'fast_preview_scale', 1.0), True)
        if bool(getattr(context.scene.tumblify, 'show_collision_debug', False)):
            try:
                _clear_debug_collection()
                for fo in _fast_source_objects(context.scene):
                    create_collision_debug(context.scene, fo, self.depsgraph, shape_override=_fast_collision_fit(fo, self.depsgraph))
            except Exception:
                pass
        label = 'Auto → ' + fit.get('shape', 'HULL').title() if shape == 'DETECT' else shape.title()
        self.report({'INFO'}, f'Fast collision: {src.name} = {label}')
        if context.area:
            context.area.tag_redraw()
        return True

    def _fast_preview_drop(self, context):
        if getattr(self,'fast_snap_enabled',False) and not getattr(self,'fast_snap_valid',False):return False
        src = getattr(self, 'fast_preview_source', None)
        if src is None:
            return False
        if getattr(self, 'fast_stack_specs', None):
            specs = list(getattr(self, 'fast_stack_specs', []) or [])
            if not specs:
                return False
            made = 0
            for (source, p, q, factor) in specs:
                (mf, mr, md) = _physics_material(source)
                try:
                    h = add_body(self.world, World.DYNAMIC, source, self.depsgraph, p, (q.x, q.y, q.z, q.w), context.scene.tumblify.convex_simplify, prepared=self.source_collision[source.name], instance_scale=factor, friction=mf, restitution=mr, density=md)
                except Exception:
                    continue
                idx = len(self.dynamic_handles)
                self.session_indices.add(idx)
                self.dynamic_handles.append(h)
                self.dynamic_source_names.append(source.name)
                self.dynamic_scales.append(factor)
                self.dynamic_cache.append([float(p.x), float(p.y), float(p.z), float(q.x), float(q.y), float(q.z), float(q.w)])
                if self.fast_mode:
                    self.dynamic_objs.append(None)
                    if source.name in self.fast_groups:
                        self.fast_groups[source.name]['indices'].append(idx)
                else:
                    dup = source.copy()
                    dup.parent = None
                    dup.hide_select = False
                    dup.tumblify.role = 'NONE'
                    if dup.animation_data:
                        dup.animation_data_clear()
                    self.collection.objects.link(dup)
                    dup.matrix_world = Matrix.LocRotScale(p, q, Vector(source.scale) * factor)
                    _sync_visual_state_from_source(source, dup)
                    dup['Tumblify_SourceName'] = source.name
                    dup['Tumblify_InstanceScale'] = factor
                    dup[FROZEN_PROP] = False
                    dup[RUNTIME_PROP] = True
                    self.dynamic_objs.append(dup)
                    self.session_objects.append(dup)
                made += 1
            self._fast_preview_create(context)
            if getattr(self,'fast_snap_enabled',False):_fast_snap.refresh(self,context)
            return made > 0
        pose = self._fast_preview_pose()
        if pose is None:
            return False
        p = Vector((pose[0], pose[1], pose[2]))
        q = Quaternion((pose[6], pose[3], pose[4], pose[5])).normalized()
        self.fast_preview_handle = None
        (mf, mr, md) = _physics_material(src)
        st = context.scene.tumblify
        scale_factor = float(self.fast_preview_scale) * random.uniform(min(st.fast_scale_min, st.fast_scale_max), max(st.fast_scale_min, st.fast_scale_max))
        try:
            h = add_body(self.world, World.DYNAMIC, src, self.depsgraph, p, (q.x, q.y, q.z, q.w), context.scene.tumblify.convex_simplify, prepared=self.source_collision[src.name], instance_scale=scale_factor, friction=mf, restitution=mr, density=md)
        except Exception:
            self._fast_preview_remove_visual()
            return False
        pose = [float(p.x), float(p.y), float(p.z), float(q.x), float(q.y), float(q.z), float(q.w)]
        idx = len(self.dynamic_handles)
        self.session_indices.add(len(self.dynamic_handles))
        self.dynamic_handles.append(h)
        self.dynamic_source_names.append(src.name)
        self.dynamic_scales.append(float(scale_factor))
        self.dynamic_cache.append(list(pose))
        if self.fast_mode:
            self.dynamic_objs.append(None)
            if src.name in self.fast_groups:
                self.fast_groups[src.name]['indices'].append(idx)
        else:
            dup = src.copy()
            dup.parent = None
            dup.hide_select = False
            dup.tumblify.role = 'NONE'
            if dup.animation_data:
                dup.animation_data_clear()
            self.collection.objects.link(dup)
            dup.matrix_world = Matrix.LocRotScale(p, q, Vector(src.scale) * float(scale_factor))
            _sync_visual_state_from_source(src, dup)
            dup['Tumblify_SourceName'] = src.name
            dup['Tumblify_InstanceScale'] = float(scale_factor)
            dup[FROZEN_PROP] = False
            dup[RUNTIME_PROP] = True
            self.dynamic_objs.append(dup)
            self.session_objects.append(dup)
        nxt = self._fast_choose_preview_source(False)
        return self._fast_preview_create(context, nxt, getattr(self, 'fast_preview_scale', 1.0), False)

    def _fast_preview_scale_by(self, context, factor):
        self.fast_preview_scale = max(0.05, min(20.0, float(getattr(self, 'fast_preview_scale', 1.0)) * float(factor)))
        self.fast_scale_label_until=time.monotonic()+1.5
        self._fast_preview_create(context, getattr(self, 'fast_preview_source', None), self.fast_preview_scale, True)
        if context.area:
            context.area.tag_redraw()

    def _adjust_spawn_rate(self, context, factor):
        st = context.scene.tumblify
        st.spawn_rate = max(1.0, min(2000.0, float(st.spawn_rate) * factor))
        if context.area:
            context.area.tag_redraw()

    def _restore_interactive_instance_results(self):
        for (inst, state) in list(getattr(self, 'interactive_instance_visibility', {}).items()):
            if not _is_live_object(inst):
                continue
            try:
                inst.hide_viewport = bool(state[0])
                inst.hide_render = bool(state[1])
                if 'Tumblify_ActiveInstanceSnapshot' in inst:
                    del inst['Tumblify_ActiveInstanceSnapshot']
            except Exception:
                pass

    def _commit_interactive_instance_results(self, context):
        """Commit Interactive poses without destroying existing Combined GN graphs.

        Reusing the original instancer Object/modifier/helper is essential for Blender
        Undo safety: native G/R operators may have undo snapshots that reference those
        IDs. Replacing them after Interactive can leave stale depsgraph references.
        """
        originals = [o for o in getattr(self, 'interactive_instance_results', []) if _is_live_object(o)]
        if not originals:
            return 0
        self._sync()
        groups = {}
        preserved = set(getattr(self, 'preexisting_dynamic', set())) | set(getattr(self, '_promoted_static_objects', set()))
        for (index, (src_name, v, sc)) in enumerate(zip(self.dynamic_source_names, self.dynamic_cache, self.dynamic_scales)):
            obj = self.dynamic_objs[index] if index < len(self.dynamic_objs) else None
            if _is_live_object(obj) and obj in preserved:
                continue
            src = bpy.data.objects.get(str(src_name))
            if src is None:
                continue
            g = groups.setdefault(str(src_name), {'src': src, 'transforms': [], 'scales': []})
            g['transforms'].append(list(v))
            g['scales'].append(float(sc))
        cols = list(getattr(originals[0], 'users_collection', []) or [])
        target = cols[0] if cols else self.collection or context.collection
        by_source = {}
        for inst in originals:
            key = str(inst.get('Tumblify_SourceName', ''))
            by_source.setdefault(key, []).append(inst)
        made = 0
        used = set()
        for (src_name, g) in groups.items():
            candidates = by_source.get(str(src_name), [])
            inst = next((x for x in candidates if x not in used and _is_live_object(x)), None)
            if inst is not None:
                if _update_instance_result_in_place(context.scene, inst, g['src'], g['transforms'], g['scales'], self.depsgraph):
                    used.add(inst)
                    made += len(g['transforms'])
                    continue
            o = _create_instance_result(context.scene, target, g['src'], g['transforms'], g['scales'], self.depsgraph)
            if o is not None:
                made += len(g['transforms'])
        for inst in originals:
            if 'Tumblify_ActiveInstanceSnapshot' in inst:
                del inst['Tumblify_ActiveInstanceSnapshot']
            if inst in used or not _is_live_object(inst):
                continue
            try:
                inst.hide_viewport = True
                inst.hide_render = True
                inst.hide_select = True
                inst['Tumblify_StaleInteractiveResult'] = True
            except Exception:
                pass
        for o in list(getattr(self, 'dynamic_objs', [])):
            if _is_live_object(o) and o not in preserved:
                try:
                    bpy.data.objects.remove(o, do_unlink=True)
                except Exception:
                    pass
        self.dynamic_objs = [o if _is_live_object(o) and o in preserved else None for o in self.dynamic_objs]
        try:
            _clear_fast_collection()
        except Exception:
            pass
        self.fast_groups = {}
        return made

    def _finish_keep(self, context):
        global _RUNTIME, STOP_REQUESTED
        _restore_transform_hud(self, context)
        if self._timer:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        if getattr(self, '_draw_handle', None):
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            except Exception:
                pass
            self._draw_handle = None
        if getattr(self, 'fast_runtime', False):
            _retire_native_rotate_target(self, context)
            self._fast_end_emitter_rotate(context, rebuild_preview=False)
            self._fast_preview_destroy()
            _fast_clear_cursor_helpers()
        _finish_runtime_move(self)
        self._sync()
        kept = 0
        if bool(getattr(self, 'interactive_existing', False)):
            if getattr(self, 'interactive_instance_mode', False):
                kept = self._commit_interactive_instance_results(context)
        elif context.scene.tumblify.result_output == 'INSTANCES':
            try:
                kept = _runtime_to_instance_results(self, context)
            except Exception:
                if self.fast_mode:
                    self._materialize_fast()
        elif self.fast_mode:
            self._materialize_fast()
        for (i, o) in enumerate(self.dynamic_objs):
            if not _is_live_object(o):
                continue
            if o in getattr(self, 'preexisting_dynamic', set()):
                continue
            o[FROZEN_PROP] = True
            o[RUNTIME_PROP] = True
            o.tumblify.role = 'NONE'
            if i < len(self.dynamic_source_names):
                o['Tumblify_SourceName'] = self.dynamic_source_names[i]
            if i < len(self.dynamic_scales):
                o['Tumblify_InstanceScale'] = float(self.dynamic_scales[i])
        _restore_mesh_collider_selection(getattr(self, '_mesh_selection_lock', None))
        try:
            self.world.close()
        except Exception:
            pass
        _solid_state_restore()
        _RUNTIME = None
        STOP_REQUESTED = False
        try:
            _apply_global_instance_display(context.scene)
        except Exception:
            pass
        if getattr(self, 'fast_runtime', False) and context.scene.tumblify.result_output == 'OBJECTS' and (not bool(getattr(self, 'interactive_existing', False))):
            _force_fast_result_mesh_display(self)
        if not getattr(self, 'fast_runtime', False):
            pass
        if bool(getattr(self, 'interactive_existing', False)):
            if getattr(self, 'interactive_instance_mode', False):
                self.report({'INFO'}, f'Fast Interactive finished. Updated {kept} batched instance(s)')
            else:
                self.report({'INFO'}, f'Fast Interactive finished. Kept transforms for {len([o for o in self.dynamic_objs if _is_live_object(o)])} object(s)')
        else:
            msg = f"Finished. Kept {(kept if context.scene.tumblify.result_output == 'INSTANCES' else len([o for o in self.dynamic_objs if _is_live_object(o)]))} result(s)"
            self.report({'INFO'}, msg)
        return {'FINISHED'}

    def _cancel_run(self, context, delete_all=False):
        global _RUNTIME, STOP_REQUESTED
        _restore_transform_hud(self, context)
        if self._timer:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        if getattr(self, '_draw_handle', None):
            try:
                bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            except Exception:
                pass
            self._draw_handle = None
        if getattr(self, 'fast_runtime', False):
            _retire_native_rotate_target(self, context)
            self._fast_end_emitter_rotate(context, rebuild_preview=False)
            self._fast_preview_destroy()
            _fast_clear_cursor_helpers()
        for (o, m) in getattr(self, 'preexisting_matrices', {}).items():
            try:
                if _is_live_object(o):
                    o.matrix_world = m
            except Exception:
                pass
        try:
            self.world.close()
        except Exception:
            pass
        if getattr(self, 'interactive_instance_mode', False):
            self._restore_interactive_instance_results()
        _restore_mesh_collider_selection(getattr(self, '_mesh_selection_lock', None))
        _solid_state_restore()
        _RUNTIME = None
        STOP_REQUESTED = False
        try:
            _clear_fast_collection()
        except Exception:
            pass
        for o in list(getattr(self, 'session_objects', [])):
            try:
                if o in getattr(self, 'preexisting_dynamic', set()):
                    continue
                if _is_live_object(o):
                    bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass
        c = getattr(self, 'collection', None)
        try:
            if c is not None and c.name in bpy.data.collections and bool(c.get('Tumblify_SpawnSession', False)):
                for o in list(c.objects):
                    if o in getattr(self, 'preexisting_dynamic', set()):
                        continue
                    if _is_live_object(o):
                        bpy.data.objects.remove(o, do_unlink=True)
                bpy.data.collections.remove(c)
        except Exception:
            pass
        if delete_all:
            _schedule_clear_spawned()
        self.report({'INFO'}, 'Advanced cancelled')
        return {'CANCELLED'}

    def _promote_all_static_to_dynamic(self):
        st = bpy.context.scene.tumblify
        for (i, o) in enumerate(list(self.static_objs)):
            if not _is_live_object(o) or self.static_handles[i] is None:
                continue
            try:
                if str((self.static_collision.get(o.name) or {}).get('shape', 'MESH')) in {'MESH', 'TERRAIN'}:
                    continue
            except Exception:
                continue
            try:
                self.world.destroy(self.static_handles[i])
                src_name = str(o.get('Tumblify_SourceName', ''))
                src = bpy.data.objects.get(src_name) or o
                data = self.static_collision.get(o.name) or self.source_collision.get(src_name) or _prepare_collision(src, self.depsgraph, st.convex_simplify)
                (p, q, _) = world_pose_scale(o)
                sc = float(o.get('Tumblify_InstanceScale', 1.0))
                (mf, mr, md) = _physics_material(src)
                h = add_body(self.world, World.DYNAMIC, src, self.depsgraph, p, q, st.convex_simplify, prepared=data, instance_scale=sc, friction=mf, restitution=mr, density=md)
                self.dynamic_objs.append(o)
                self.dynamic_handles.append(h)
                self.dynamic_source_names.append(src.name)
                self.dynamic_scales.append(sc)
                self.dynamic_cache.append([float(p.x), float(p.y), float(p.z), float(q[0]), float(q[1]), float(q[2]), float(q[3])])
                self.static_objs[i] = None
                self.static_handles[i] = None
            except Exception:
                pass

    def _enter_live_interactive(self, context):
        self.fast_snap_picker=None
        if self.live_interactive:
            return True
        self._interactive_resume_left = bool(getattr(self, 'left', False))
        self.left = False
        try:
            self._sync()
            if getattr(self, 'fast_groups', None):
                self.fast_interactive_materialized = set()
                self._fast_interactive_rebuild_groups()
            self.session_objects = [o for o in self.dynamic_objs if _is_live_object(o)]
            for o in self.session_objects:
                try:
                    o.hide_select = False
                    _force_real_mesh_display(o)
                except Exception:
                    pass
            self._sync()
            self.live_interactive = True
            _hide_transform_hud(self, context)
            context.scene.tumblify.emitter_interaction = 'OFF'
            for info in list(getattr(self, 'brush_handles', {}).values()):
                try:
                    self.world.destroy(info[0] if isinstance(info, tuple) else info)
                except Exception:
                    pass
            self.brush_handles = {}
            if getattr(self, 'fast_groups', None):
                prefix = 'Fast' if getattr(self, 'fast_runtime', False) else 'Advanced'
                extra = '' if getattr(self, 'fast_runtime', False) else '; F3 Fill release'
                self.report({'INFO'}, f'{prefix} Batched Interactive: click body to select, G move, R rotate, Shift+E return{extra}')
            else:
                self.report({'INFO'}, 'Live Interactive: F3 Fill release, G move, R rotate, Shift+E return')
            if context.area:
                context.area.tag_redraw()
            return True
        except Exception as e:
            _restore_transform_hud(self, context)
            self.live_interactive = False
            self.report({'ERROR'}, f'Live Interactive failed: {e}')
            return False

    def _exit_live_interactive(self, context):
        if not self.live_interactive:
            return True
        try:
            self.pending_move_finish = False
            self.moving_selected = False
            self._release_all_kinematics()
            context.scene.tumblify.emitter_interaction = 'OFF'
            for info in list(getattr(self, 'brush_handles', {}).values()):
                try:
                    self.world.destroy(info[0] if isinstance(info, tuple) else info)
                except Exception:
                    pass
            self.brush_handles = {}
            if getattr(self, 'fill_emitters', None) and bool(getattr(self, 'fill_collision_enabled', True)):
                self.fill_container_handles = []
                for data in self.fill_emitters:
                    self.fill_container_handles.extend(_fill_restore_static_container(self.world, data, context.scene.tumblify.static_friction))
            _restore_transform_hud(self, context)
            self.live_interactive = False
            self.fast_interactive_materialized = set()
            self.interactive_box_armed = False
            self.interactive_box_drag = False
            self.interactive_box_start = None
            self.interactive_box_end = None
            self.interactive_mouse_pending = False
            self.interactive_mouse_dragging = False
            st = context.scene.tumblify
            if getattr(self, 'fast_runtime', False):
                try:
                    _rebatch_runtime_after_interactive(self, context.scene, force_mesh=True)
                except Exception as e:
                    self.fast_groups = {}
                    self.fast_mode = False
                    self.report({'WARNING'}, f'Could not restore Fast mesh display: {e}')
                self.fast_sim_enabled = True
                self.fast_click_enabled = True
                self.fast_preview_scale = float(getattr(self, 'fast_preview_scale', 1.0) or 1.0)
                self.fast_preview_target = _fast_view_depth(context, getattr(self, 'fast_preview_target', None))
                self._fast_preview_create(context)
                self.report({'INFO'}, 'Fast Drop resumed')
                if context.area:
                    context.area.tag_redraw()
                return True
            if st.spawn_backend == 'FAST':
                try:
                    _rebatch_runtime_after_interactive(self, context.scene, force_mesh=False)
                except Exception as e:
                    self.fast_groups = {}
                    self.fast_mode = False
                    self.report({'WARNING'}, f'Could not restore Realtime batched display; using materialized objects: {e}')
            else:
                self.fast_groups = {}
                self.fast_mode = False
            self.left = bool(getattr(self, '_interactive_resume_left', False))
            try:
                _apply_global_instance_display(context.scene)
            except Exception:
                pass
            self.report({'INFO'}, 'Advanced resumed: spawning ' + ('ON' if self.left else 'paused') + (' (Realtime ready)' if self.fast_mode else ' (materialized)'))
            if context.area:
                context.area.tag_redraw()
            return True
        except Exception as e:
            self.report({'ERROR'}, f'Return to Advanced failed: {e}')
            return False

    def _modal_impl(self, context, event):
        global _RUNTIME, STOP_REQUESTED
        # Modal operators invoked by an N-panel button retain its UI region.
        # Always run Fast interaction in the real viewport, including Snap,
        # transforms and picking; event coordinates must use the same region.
        if getattr(self, 'fast_runtime', False) and context.area and context.area.type == 'VIEW_3D':
            region = _fast_viewport_region(context)
            if context.region != region:
                view_event = _FastViewportEvent(event, region)
                with context.temp_override(region=region):
                    return self._modal_impl(bpy.context, view_event)
        if getattr(self, 'fast_runtime', False) and (not self.live_interactive):
            if bool(getattr(self, 'fast_native_rotate', False)):
                return {'PASS_THROUGH'}
            adjust = getattr(self, 'fast_spread_adjust', None)
            if adjust is not None:
                if event.type == 'MOUSEMOVE':
                    context.scene.tumblify.fast_spread = max(0.0, adjust[1] + (event.mouse_x - adjust[0]) * (0.001 if event.shift else 0.01))
                    if context.area:
                        context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                if event.value == 'PRESS' and event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'S', 'ESC', 'RIGHTMOUSE'}:
                    if event.type in {'ESC', 'RIGHTMOUSE'}:
                        context.scene.tumblify.fast_spread = adjust[1]
                    self.fast_spread_adjust = None
                    self.fast_spread_circle_until = time.monotonic() + 1.2
                    if context.area:
                        context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                return {'RUNNING_MODAL'}
            if event.type=='V' and event.value=='PRESS' and not (event.ctrl or event.alt or event.shift):
                self.fast_hold_spawning=False;self.fast_hold_acc=0
                self.fast_snap_enabled=not bool(getattr(self,'fast_snap_enabled',False))
                if self.fast_snap_enabled:
                    self.fast_snap_previous_target=self.fast_preview_target.copy()
                    self.fast_snap_picker=_fast_snap.SurfacePicker(self,context)
                    self.fast_snap_bounds={obj.name:tuple(tuple(c) for c in obj.evaluated_get(self.depsgraph).bound_box) for obj in self.sources}
                    _fast_snap.refresh(self,context,event)
                else:
                    self.fast_snap_valid=False
                    self.fast_preview_target=_fast_mouse_3d_point(context,event,getattr(self,'fast_snap_previous_target',self.fast_preview_target)) or self.fast_preview_target
                    for helper in _fast_ensure_cursor_helpers(context.scene):helper.hide_set(False)
                    _fast_update_cursor_helpers(context.scene,self.fast_preview_target)
                    self._fast_preview_create(context,getattr(self,'fast_preview_source',None),getattr(self,'fast_preview_scale',1),True)
                if context.area:context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.type == 'S' and event.value == 'PRESS' and (not (event.ctrl or event.alt or event.shift)):
                self.fast_hold_spawning = False
                self.fast_hold_acc = 0.0
                self.fast_spread_adjust = (event.mouse_x, float(context.scene.tumblify.fast_spread))
                if context.area:
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.type == 'MOUSEMOVE':
                self.fast_snap_mouse=(event.mouse_region_x,event.mouse_region_y)
                if getattr(self,'fast_snap_enabled',False):_fast_snap.refresh(self,context,event)
                else:
                    p = _fast_mouse_3d_point(context, event, getattr(self, 'fast_preview_target', None))
                    if p is not None:self._fast_preview_set_target(context,p)
                if context.area:context.area.tag_redraw()
            if event.value == 'PRESS' and event.type == 'E' and (not event.shift):
                enabled = not bool(getattr(self, 'fast_sim_enabled', False))
                self.fast_sim_enabled = enabled
                self.fast_click_enabled = enabled
                if not enabled:
                    self.fast_hold_spawning = False
                    self.fast_hold_acc = 0.0
                self.report({'INFO'}, f"Fast physics: {('ON' if enabled else 'PAUSED')}")
                if context.area:
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS' and event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
                if event.ctrl or event.shift:
                    self._fast_preview_scale_by(context, 1.12 if event.type == 'WHEELUPMOUSE' else 1.0 / 1.12)
                    return {'RUNNING_MODAL'}
                return {'PASS_THROUGH'}
            if event.value=='PRESS' and event.type in {'MINUS','EQUAL','NUMPAD_MINUS','NUMPAD_PLUS'} and not (event.ctrl or event.alt):
                st=context.scene.tumblify
                change=-1 if event.type in {'MINUS','NUMPAD_MINUS'} else 1
                prop='fast_stack_count' if event.shift else 'fast_objects_per_tick'
                setattr(st,prop,max(1,min(10,int(getattr(st,prop))+change)))
                if context.area:context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS' and event.type == 'RIGHTMOUSE':
                src = self._fast_choose_preview_source(True)
                self._fast_preview_create(context, src, getattr(self, 'fast_preview_scale', 1.0), False)
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS' and event.type == 'R' and event.shift and (not (event.ctrl or event.alt)):
                st = context.scene.tumblify
                st.fast_random_rotation = not bool(st.fast_random_rotation)
                self._fast_preview_create(context, getattr(self, 'fast_preview_source', None), getattr(self, 'fast_preview_scale', 1.0), False)
                self.report({'INFO'}, f"Fast random rotation: {('ON' if st.fast_random_rotation else 'OFF')}")
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS' and event.type == 'R' and (not (event.ctrl or event.alt or event.shift)):
                self._fast_begin_emitter_rotate(context, event)
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS' and event.type == 'Q' and (not (event.ctrl or event.alt or event.shift)):
                try:
                    bpy.ops.tumblify.fast_rotation_axes_popup('INVOKE_DEFAULT')
                except Exception as exc:
                    self.report({'WARNING'}, f'Rotation axes menu failed: {exc}')
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and bool(getattr(self, 'fast_hold_spawning', False)):
                self.fast_hold_spawning = False
                self.fast_hold_acc = 0.0
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and bool(getattr(self, 'fast_click_enabled', False)) and bool(getattr(self, 'fast_sim_enabled', False)):
                if event.value == 'PRESS':
                    self._fast_preview_drop(context)
                    self.fast_hold_spawning = True
                    self.fast_hold_acc = 0.0
                    if context.area:
                        context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
        if not self.fast_runtime and (not self.live_interactive) and (event.type == 'Q') and (event.value == 'PRESS') and (not (event.ctrl or event.alt or event.shift)):
            try:
                bpy.ops.tumblify.fast_rotation_axes_popup('INVOKE_DEFAULT')
            except Exception as exc:
                self.report({'WARNING'}, f'Rotation axes menu failed: {exc}')
            return {'RUNNING_MODAL'}
        if not self.fast_runtime and (not self.live_interactive) and (event.value == 'PRESS') and (event.type == 'R') and event.shift and (not (event.ctrl or event.alt)):
            st = context.scene.tumblify
            st.advanced_random_rotation = not st.advanced_random_rotation
            self.report({'INFO'}, f"Advanced random rotation: {('ON' if st.advanced_random_rotation else 'OFF')}")
            _tag_view3d_redraw_all()
            return {'RUNNING_MODAL'}
        if not self.fast_runtime and not self.live_interactive and event.value == 'PRESS' and (event.type in {'MINUS', 'EQUAL', 'NUMPAD_MINUS', 'NUMPAD_PLUS'}) and (not self.moving_selected):
            self._adjust_spawn_rate(context, 0.8 if event.type in {'MINUS', 'NUMPAD_MINUS'} else 1.25)
            return {'RUNNING_MODAL'}
        if not getattr(self, 'fast_runtime', False) and event.value == 'PRESS' and getattr(self, 'fill_emitters', None) and (event.type == 'F3' or (not self.live_interactive and event.type in {'THREE', 'NUMPAD_3'})):
            self._toggle_fill_collision(context)
            return {'RUNNING_MODAL'}
        if event.type == 'E' and (not event.shift) and (event.value == 'PRESS') and (not self.moving_selected) and (not self.live_interactive) and (not getattr(self, 'fast_runtime', False)):
            self.left = not self.left
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'E' and event.shift and (event.value == 'PRESS') and (not self.moving_selected):
            try:
                if getattr(self, 'live_interactive', False):
                    self._exit_live_interactive(context)
                else:
                    if getattr(self, 'fast_runtime', False):
                        self.fast_sim_enabled = True
                        self.fast_click_enabled = False
                        self.fast_hold_spawning = False
                        self.fast_hold_acc = 0.0
                    self._enter_live_interactive(context)
            except Exception as e:
                self.report({'ERROR'}, f'Interactive toggle failed: {e}')
            return {'RUNNING_MODAL'}
        if self.live_interactive and (not self.moving_selected):
            batched = self._batched_interactive_active()
            if event.type == 'B' and event.value == 'PRESS':
                self.interactive_box_armed = not bool(self.interactive_box_armed)
                self.interactive_mouse_pending = False
                self.interactive_box_drag = False
                self.interactive_box_start = None
                self.interactive_box_end = None
                if context.area:
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'PRESS' and event.type in {'ESC', 'RIGHTMOUSE'} and (self.interactive_mouse_pending or self.interactive_box_drag or self.interactive_box_armed):
                self.interactive_mouse_pending = False
                self.interactive_mouse_dragging = False
                self.interactive_box_drag = False
                self.interactive_box_armed = False
                self.interactive_box_start = None
                self.interactive_box_end = None
                if context.area:
                    context.area.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                self.interactive_mouse_pending = True
                self.interactive_mouse_dragging = False
                self.interactive_box_start = self._interactive_region_coord(event)
                self.interactive_box_end = self.interactive_box_start
                self.interactive_box_mode = 'SUBTRACT' if event.ctrl else 'ADD' if event.shift else 'REPLACE'
                return {'RUNNING_MODAL'} if batched or self.interactive_box_armed else {'PASS_THROUGH'}
            if event.type == 'MOUSEMOVE' and self.interactive_mouse_pending:
                pt = self._interactive_region_coord(event)
                a = self.interactive_box_start
                if a is not None and (pt[0] - a[0]) ** 2 + (pt[1] - a[1]) ** 2 >= 49:
                    self.interactive_mouse_dragging = True
                    self.interactive_box_drag = True
                if self.interactive_box_drag:
                    self.interactive_box_end = pt
                    if context.area:
                        context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self.interactive_mouse_pending:
                self.interactive_mouse_pending = False
                was_drag = bool(self.interactive_mouse_dragging)
                a = self.interactive_box_start
                end = self._interactive_region_coord(event)
                mode = self.interactive_box_mode
                self.interactive_mouse_dragging = False
                self.interactive_box_drag = False
                self.interactive_box_armed = False
                self.interactive_box_start = None
                self.interactive_box_end = None
                if was_drag and a is not None:
                    if batched:
                        self._fast_interactive_apply_box(context, a, end, mode)
                    else:
                        try:
                            bpy.ops.view3d.select_box('EXEC_DEFAULT', xmin=int(min(a[0], end[0])), xmax=int(max(a[0], end[0])), ymin=int(min(a[1], end[1])), ymax=int(max(a[1], end[1])), mode={'ADD': 'ADD', 'SUBTRACT': 'SUB', 'REPLACE': 'SET'}[mode])
                        except Exception as exc:
                            self.report({'WARNING'}, f'Box selection failed: {exc}')
                    if context.area:
                        context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                if batched:
                    self._fast_interactive_pick(context, event)
                    if context.area:
                        context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
                return {'PASS_THROUGH'}
        if self.live_interactive and (not self.moving_selected) and (event.value == 'PRESS') and (event.type in {'X', 'DEL'}):
            (n, unsupported) = self._delete_selected_live_runtime(context)
            if unsupported:
                self.report({'WARNING'}, f'{unsupported} selected object(s) are static/non-runtime and were not deleted while simulation is active')
            if n:
                self.report({'INFO'}, f'Deleted {n} live physics object(s)')
            return {'RUNNING_MODAL'}
        if self.live_interactive and event.type in {'G', 'R'} and (event.value == 'PRESS'):
            n = self._activate_selected_for_move(context)
            if n:
                self.moving_selected = True
                self.pending_move_finish = False
                return {'PASS_THROUGH'}
            self.report({'WARNING'}, 'Interactive: select a spawned object or movable collider before transform')
            return {'RUNNING_MODAL'}
        if self.moving_selected and event.value == 'PRESS' and (event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE', 'RIGHTMOUSE', 'ESC'}):
            self.pending_move_finish = True
            return {'PASS_THROUGH'}
        if getattr(self, 'cancel_delete_all_requested', False):
            return self._cancel_run(context, delete_all=True)
        if event.type == 'ESC' and event.value == 'PRESS':
            return self._cancel_run(context)
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            return self._finish_keep(context)
        if STOP_REQUESTED:
            STOP_REQUESTED = False
            return self._finish_keep(context)
        if event.type == 'TIMER':
            if self.pending_move_finish:
                _finish_runtime_move(self)
            self._release_unselected_kinematics(context)
            radius = self._emitter_radius()
            self.last_brush_radius = radius
            if not getattr(self, 'fast_runtime', False):
                update_emitter_guide(context.scene)
            if self.left and (not self.live_interactive):
                self.spawn_acc += self.dt
                interval = 1.0 / max(1.0, float(context.scene.tumblify.spawn_rate))
                emitted = 0
                while self.spawn_acc >= interval and emitted < 32:
                    self.spawn_acc -= interval
                    self._spawn()
                    emitted += 1
            if getattr(self, 'fast_runtime', False) and (not self.live_interactive):
                self._fast_preview_hold()
                if bool(getattr(self, 'fast_hold_spawning', False)) and bool(getattr(self, 'fast_click_enabled', False)) and bool(getattr(self, 'fast_sim_enabled', False)):
                    self.fast_hold_acc = float(getattr(self, 'fast_hold_acc', 0.0)) + float(self.dt)
                    interval = 0.12
                    emitted = 0
                    while self.fast_hold_acc >= interval and emitted < 4:
                        self.fast_hold_acc -= interval
                        successful = True
                        successful = self._fast_preview_drop(context)
                        if not successful:
                            self.fast_hold_spawning = False
                            self.fast_hold_acc = 0.0
                            break
                        emitted += 1
            self._update_object_kinematics()
            swept = False
            if self.live_interactive and getattr(self, 'moving_fill_emitters', None):
                swept = self._step_fill_emitter_motion(context)
            if not swept and (not getattr(self, 'fast_runtime', False) or bool(getattr(self, 'fast_sim_enabled', False)) or self.live_interactive):
                self.world.step(self.dt, int(getattr(self, 'runtime_substeps', context.scene.tumblify.physics_substeps)))
            self._sync()
            if getattr(self, 'fast_runtime', False) and (not self.live_interactive):
                self._fast_preview_sync_visual()
            if context.area:
                context.area.tag_redraw()
        return {'PASS_THROUGH'}

    def modal(self, context, event):
        return _dispatch_runtime_modal(self, context, event, 'Tumblify realtime simulation')

def _clear_bake_object(obj):
    ad=getattr(obj,'animation_data',None)
    current=ad.action if ad else None
    if current and current.name.startswith((EMITTER_BAKE_ACTION_PREFIX,EMITTER_BAKE_ACTION_PREFIX)):
        try: ad.action=None
        except Exception: pass
        try:
            if current.users==0: bpy.data.actions.remove(current)
        except Exception: pass
    prev=str(obj.get(BAKE_PREV_ACTION_PROP,''))
    if prev:
        try:
            obj.animation_data_create().action=bpy.data.actions.get(prev)
        except Exception: pass
    elif ad and ad.action and ad.action.name.startswith((EMITTER_BAKE_ACTION_PREFIX,EMITTER_BAKE_ACTION_PREFIX)):
        try: ad.action=None
        except Exception: pass
    old_mode=str(obj.get(BAKE_PREV_ROT_PROP,''))
    if old_mode:
        try: obj.rotation_mode=old_mode
        except Exception: pass
    for k in (BAKE_PREV_ACTION_PROP,BAKE_PREV_ROT_PROP,EMITTER_BAKE_PROP):
        try:
            if k in obj: del obj[k]
        except Exception: pass

def _bake_new_container(scene,prefix=EMITTER_BAKE_ACTION_PREFIX,label='Tumblify Emitter Bake'):
    """Create one layered Action shared by every baked physics body via Action Slots.

    One Action/Layer/Strip per bake is substantially cheaper than one Action hierarchy per body,
    especially for thousands of objects. Each object still gets its own slot/channelbag.
    """
    safe=re.sub(r'[^A-Za-z0-9_.-]+','_',scene.name).strip('_') or 'Scene'
    act=bpy.data.actions.new(prefix+safe)
    layer=act.layers.new(label)
    strip=layer.strips.new(type='KEYFRAME')
    return act,strip


def _bake_track_key_indices(track,n):
    """Losslessly remove only interior frames of fully stationary spans."""
    if n<=2:
        return list(range(n))
    eps=1.0e-7
    if _np is not None:
        try:
            a=_np.ctypeslib.as_array(track).reshape((n,7))
            same_prev=_np.all(_np.abs(a[1:-1]-a[:-2])<=eps,axis=1)
            same_next=_np.all(_np.abs(a[1:-1]-a[2:])<=eps,axis=1)
            mid=_np.nonzero(~(same_prev & same_next))[0]+1
            return _np.concatenate((_np.array([0],dtype=_np.int32),mid.astype(_np.int32),_np.array([n-1],dtype=_np.int32)))
        except Exception:
            pass
    keep=[0]
    for i in range(1,n-1):
        a=7*(i-1); b=7*i; c=7*(i+1)
        same_prev=all(abs(float(track[b+k])-float(track[a+k]))<=eps for k in range(7))
        same_next=all(abs(float(track[b+k])-float(track[c+k]))<=eps for k in range(7))
        if not (same_prev and same_next): keep.append(i)
    keep.append(n-1)
    return keep


def _bake_create_action(obj,track,start_frame,shared_action=None,shared_strip=None):
    """Write one native body track as bulk FCurves into a shared layered Action slot."""
    _clear_bake_object(obj)
    ad=obj.animation_data_create(); prev=ad.action
    if prev: obj[BAKE_PREV_ACTION_PROP]=prev.name
    else: obj[BAKE_PREV_ACTION_PROP]=''
    obj[BAKE_PREV_ROT_PROP]=obj.rotation_mode

    if shared_action is None or shared_strip is None:
        # Scripting/backward-compatible fallback. UI paths create one shared action up front.
        shared_action,shared_strip=_bake_new_container(bpy.context.scene)
    act=shared_action; strip=shared_strip
    slot=act.slots.new(obj.id_type,obj.name)
    bag=strip.channelbag(slot,ensure=True)
    ad.action=act; ad.action_slot=slot
    obj.rotation_mode='QUATERNION'

    n=len(track)//7
    if n<=0: return act
    indices=_bake_track_key_indices(track,n)
    m=len(indices)
    channels=[('location',0,0),('location',1,1),('location',2,2),('rotation_quaternion',0,6),('rotation_quaternion',1,3),('rotation_quaternion',2,4),('rotation_quaternion',3,5)]
    # Blender 5.2 enum backing values are an internal implementation detail and
    # can exceed a C/RNA int on some builds.  Never bulk-write interpolation by
    # numeric enum value; assign the stable identifier string instead.

    arr2=None
    if _np is not None:
        try: arr2=_np.ctypeslib.as_array(track).reshape((n,7))
        except Exception: arr2=None
    for path,index,src in channels:
        fcu=bag.fcurves.new(data_path=path,index=index,group_name='Tumblify Emitter Bake')
        pts=fcu.keyframe_points; pts.add(m)
        if arr2 is not None:
            try:
                idx=_np.asarray(indices,dtype=_np.int32)
                co=_np.empty((m,2),dtype=_np.float32)
                co[:,0]=float(start_frame)+idx
                co[:,1]=arr2[idx,src]
                pts.foreach_set('co',co.reshape(-1))
            except Exception:
                co=None
        else:
            co=None
        if co is None:
            packed=array('f')
            for ii in indices:
                packed.extend((float(start_frame+int(ii)),float(track[7*int(ii)+src])))
            try: pts.foreach_set('co',packed)
            except Exception:
                for j in range(m): pts[j].co=(packed[2*j],packed[2*j+1])
        for kp in pts:
            kp.interpolation='LINEAR'
        # Frames are already strictly sorted and interpolation is LINEAR, so per-FCurve update()
        # is unnecessary and disproportionately expensive for large bakes.
    return act


class TU_OT_post_run(bpy.types.Operator):
    bl_idname = 'tumblify.post_run'
    bl_label = 'Start Interactive'
    bl_options = {'REGISTER', 'INTERNAL'}
    _timer = None

    def invoke(self, context, event):
        global _RUNTIME, STOP_REQUESTED
        if context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, 'Run from a 3D View')
            return {'CANCELLED'}
        if _RUNTIME is not None:
            self.report({'WARNING'}, 'Stop the current simulation first')
            return {'CANCELLED'}
        frozen = _frozen_spawned(context.scene)
        if not frozen:
            self.report({'WARNING'}, 'No frozen physics objects to settle')
            return {'CANCELLED'}
        selected_frozen = [o for o in frozen if o.select_get()]
        if not selected_frozen:
            self.report({'WARNING'}, 'Select the frozen objects you want to run in Interactive')
            return {'CANCELLED'}
        selected_names = {o.name for o in selected_frozen}
        try:
            self.depsgraph = context.evaluated_depsgraph_get()
            st = context.scene.tumblify
            simplify = st.convex_simplify
            override = None
            fast = getattr(st, 'mode', 'DROP') == 'FAST'
            self.world = _configure_world(World(st.physics_threads), st, fast=fast)
            _add_builtin_floor(self.world, st, fast=fast)
            self.static_objs = []
            self.static_handles = []
            self.static_collision = []
            self.static_kinematic_indices = set()
            base_statics = list(_advanced_runtime_static_objects(context.scene) if context.scene.tumblify.mode == 'DROP' else _scene_role_objects(context.scene, 'STATIC'))
            for o in base_statics:
                pdata = _prepare_collision(o, self.depsgraph, simplify)
                h = add_body(self.world, World.STATIC, o, self.depsgraph, simplify=simplify, prepared=pdata, friction=st.static_friction, restitution=0.0, density=0.0)
                self.static_objs.append(o)
                self.static_handles.append(h)
                self.static_collision.append(pdata)
            self._mesh_selection_lock = _lock_mesh_collider_selection(context.scene)
            self._post_passive_selection_lock = {}
            self.passive_frozen_handles = []
            self.dynamic_objs = []
            self.dynamic_handles = []
            self.dynamic_collision = []
            self.dynamic_material = []
            self.dynamic_scales = []
            self.kinematic_indices = set()
            cache = {}

            def frozen_setup(o):
                src_name = str(o.get('Tumblify_SourceName', ''))
                src = bpy.data.objects.get(src_name)
                if src is not None:
                    key = (src.name, src.tumblify.shape)
                    pdata = cache.get(key)
                    if pdata is None:
                        pdata = _prepare_collision(src, self.depsgraph, simplify)
                        _cache_native_collision_assets(self.world, pdata)
                        cache[key] = pdata
                    return (pdata, float(o.get('Tumblify_InstanceScale', 1.0)), src)
                pdata = _prepare_collision(o, self.depsgraph, simplify)
                _cache_native_collision_assets(self.world, pdata)
                return (pdata, 1.0, o)
            for o in frozen:
                if o.name in selected_names:
                    continue
                (pdata, sc, material_obj) = frozen_setup(o)
                (p0, q0, _) = world_pose_scale(o)
                h = add_body(self.world, World.STATIC, material_obj, self.depsgraph, p_override=p0, q_override=q0, simplify=simplify, prepared=pdata, instance_scale=sc, friction=st.static_friction, restitution=0.0, density=0.0)
                self.passive_frozen_handles.append(h)
                try:
                    self._post_passive_selection_lock[o.name] = bool(o.hide_select)
                    o.select_set(False)
                    o.hide_select = True
                except Exception:
                    pass
            for o in selected_frozen:
                (pdata, sc, material_obj) = frozen_setup(o)
                (p0, q0, _) = world_pose_scale(o)
                (mf, mr, md) = _physics_material(material_obj)
                h = add_body(self.world, World.DYNAMIC, material_obj, self.depsgraph, p_override=p0, q_override=q0, simplify=simplify, prepared=pdata, instance_scale=sc, friction=mf, restitution=mr, density=md)
                self.dynamic_objs.append(o)
                self.dynamic_handles.append(h)
                self.dynamic_collision.append(pdata)
                self.dynamic_material.append(material_obj)
                self.dynamic_scales.append(sc)
            for o in context.view_layer.objects:
                try:
                    o.select_set(False)
                except Exception:
                    pass
            self.dt = 1.0 / max(1.0, float(st.physics_frequency))
            self.moving_selected = False
            self.pending_move_finish = False
            STOP_REQUESTED = False
            _set_runtime_owner(self, context)
            self._region = context.region
            self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(self._draw_hotkeys, (), 'WINDOW', 'POST_PIXEL')
            self._timer = context.window_manager.event_timer_add(self.dt, window=context.window)
            context.window_manager.modal_handler_add(self)
            _hide_transform_hud(self, context)
            self.report({'INFO'}, f'Interactive: {len(self.dynamic_objs)} selected dynamic | {len(self.passive_frozen_handles)} frozen static | {st.post_substeps} substeps')
            return {'RUNNING_MODAL'}
        except Exception as e:
            _restore_transform_hud(self, context)
            try:
                if getattr(self, '_draw_handle', None):
                    bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
                    self._draw_handle = None
                if hasattr(self, 'world'):
                    self.world.close()
                _restore_mesh_collider_selection(getattr(self, '_mesh_selection_lock', None))
                for (name, old) in getattr(self, '_post_passive_selection_lock', {}).items():
                    o = bpy.data.objects.get(name)
                    if o:
                        o.hide_select = old
            except Exception:
                pass
            _solid_state_restore()
            _RUNTIME = None
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

    def _draw_hotkeys(self):
        try:
            font = 0
            lines = ['INTERACTIVE', 'G  Move selected physics objects', 'X / Delete  Delete selected post objects', 'LMB drag  Box Select   Shift add / Ctrl subtract', 'Esc  Stop simulation']
            if getattr(self,'fast_runtime',False) and not getattr(self,'live_interactive',False) and time.monotonic()<getattr(self,'fast_scale_label_until',0.0):
                point=view3d_utils.location_3d_to_region_2d(self._region,self._rv3d,Vector(self.fast_preview_target))
                if point is not None:
                    label=f"Scale  {float(self.fast_preview_scale):.2f}x"
                    blf.size(font,22.0)
                    width,height=blf.dimensions(font,label)
                    x=max(8.0,min(float(point.x)+24.0,self._region.width-width-8.0))
                    y=max(8.0,min(float(point.y)+24.0,self._region.height-height-8.0))
                    blf.color(font,0.02,0.02,0.02,1.0);blf.position(font,x+1,y-1,0);blf.draw(font,label)
                    blf.color(font,0.35,0.9,1.0,1.0);blf.position(font,x,y,0);blf.draw(font,label)
            blf.size(font, 13.0)
            blf.color(font, 0.92, 0.92, 0.92, 1.0)
            y = 24.0
            for line in reversed(lines):
                blf.position(font, 18.0, y, 0)
                blf.draw(font, line)
                y += 18.0
            if bool(getattr(self, '_mouse_box_drag', False)):
                _draw_interactive_selection_box(getattr(self, '_mouse_box_origin', None), getattr(self, '_mouse_box_current', None))
        except Exception:
            pass

    def _convert_dynamic_to_kinematic(self, index):
        if index < 0 or index >= len(self.dynamic_objs) or index in self.kinematic_indices:
            return index in self.kinematic_indices
        o = self.dynamic_objs[index]
        if not _is_live_object(o):
            return False
        self.world.destroy(self.dynamic_handles[index])
        (p, q, _) = world_pose_scale(o)
        mat = self.dynamic_material[index]
        (mf, mr, md) = _physics_material(mat)
        h = add_body(self.world, World.KINEMATIC, mat, self.depsgraph, p_override=p, q_override=q, simplify=bpy.context.scene.tumblify.convex_simplify, prepared=self.dynamic_collision[index], instance_scale=self.dynamic_scales[index], friction=mf, restitution=mr, density=md)
        self.dynamic_handles[index] = h
        self.kinematic_indices.add(index)
        return True

    def _convert_dynamic_to_dynamic(self, index):
        if index < 0 or index >= len(self.dynamic_objs) or index not in self.kinematic_indices:
            return False
        o = self.dynamic_objs[index]
        if not _is_live_object(o):
            self.kinematic_indices.discard(index)
            return False
        self.world.destroy(self.dynamic_handles[index])
        (p, q, _) = world_pose_scale(o)
        mat = self.dynamic_material[index]
        (mf, mr, md) = _physics_material(mat)
        h = add_body(self.world, World.DYNAMIC, mat, self.depsgraph, p_override=p, q_override=q, simplify=bpy.context.scene.tumblify.convex_simplify, prepared=self.dynamic_collision[index], instance_scale=self.dynamic_scales[index], friction=mf, restitution=mr, density=md)
        self.dynamic_handles[index] = h
        self.kinematic_indices.discard(index)
        return True

    def _convert_static_to_kinematic(self, index):
        if index < 0 or index >= len(self.static_objs) or index in self.static_kinematic_indices:
            return index in self.static_kinematic_indices
        o = self.static_objs[index]
        if not _is_live_object(o):
            return False
        if o.tumblify.shape in {'MESH', 'TERRAIN'}:
            self.report({'WARNING'}, 'Exact Mesh collider is static-only. Move it while Interactive is stopped.')
            return False
        self.world.destroy(self.static_handles[index])
        (p, q, _) = world_pose_scale(o)
        h = add_body(self.world, World.KINEMATIC, o, self.depsgraph, p_override=p, q_override=q, simplify=bpy.context.scene.tumblify.convex_simplify, prepared=self.static_collision[index], friction=bpy.context.scene.tumblify.static_friction, restitution=0.0, density=0.0)
        self.static_handles[index] = h
        self.static_kinematic_indices.add(index)
        return True

    def _convert_static_to_static(self, index):
        if index < 0 or index >= len(self.static_objs) or index not in self.static_kinematic_indices:
            return False
        o = self.static_objs[index]
        if not _is_live_object(o):
            self.static_kinematic_indices.discard(index)
            return False
        self.world.destroy(self.static_handles[index])
        (p, q, _) = world_pose_scale(o)
        h = add_body(self.world, World.STATIC, o, self.depsgraph, p_override=p, q_override=q, simplify=bpy.context.scene.tumblify.convex_simplify, prepared=self.static_collision[index], friction=bpy.context.scene.tumblify.static_friction, restitution=0.0, density=0.0)
        self.static_handles[index] = h
        self.static_kinematic_indices.discard(index)
        return True

    def _activate_selected_for_move(self, context):
        n = 0
        rejected = 0
        for o in list(context.selected_objects):
            if o in self.dynamic_objs:
                i = self.dynamic_objs.index(o)
                if i in self.kinematic_indices or self._convert_dynamic_to_kinematic(i):
                    n += 1
            elif o in self.static_objs:
                i = self.static_objs.index(o)
                if o.tumblify.shape in {'MESH', 'TERRAIN'}:
                    try:
                        o.select_set(False)
                    except Exception:
                        pass
                    rejected += 1
                elif i in self.static_kinematic_indices or self._convert_static_to_kinematic(i):
                    n += 1
        if rejected:
            self.report({'WARNING'}, f'{rejected} exact Mesh collider(s) cannot move during Interactive')
        return n

    def _release_unselected_kinematics(self, context):
        selected = set(context.selected_objects)
        for i in tuple(self.kinematic_indices):
            if i >= len(self.dynamic_objs):
                continue
            o = self.dynamic_objs[i]
            if not _is_live_object(o):
                continue
            if o not in selected:
                self._convert_dynamic_to_dynamic(i)
        for i in tuple(self.static_kinematic_indices):
            if i >= len(self.static_objs):
                continue
            o = self.static_objs[i]
            if not _is_live_object(o):
                continue
            if o not in selected:
                _release_static_as_dynamic(self, i)

    def _release_all_kinematics(self):
        for i in tuple(self.kinematic_indices):
            if i < len(self.dynamic_objs) and _is_live_object(self.dynamic_objs[i]):
                self._convert_dynamic_to_dynamic(i)
            else:
                self.kinematic_indices.discard(i)
        for i in tuple(self.static_kinematic_indices):
            if i < len(self.static_objs) and _is_live_object(self.static_objs[i]):
                _release_static_as_dynamic(self, i)
            else:
                self.static_kinematic_indices.discard(i)

    def _update_kinematics(self):
        for i in tuple(self.kinematic_indices):
            if i >= len(self.dynamic_objs):
                continue
            o = self.dynamic_objs[i]
            if _is_live_object(o):
                (p, q, _) = world_pose_scale(o)
                self.world.target(self.dynamic_handles[i], p, q, self.dt)
        for i in tuple(self.static_kinematic_indices):
            if i >= len(self.static_objs):
                continue
            o = self.static_objs[i]
            if _is_live_object(o):
                (p, q, _) = world_pose_scale(o)
                self.world.target(self.static_handles[i], p, q, self.dt)

    def _sync(self):
        if not self.dynamic_handles:
            return
        (vals, awake) = self.world.get_bulk(self.dynamic_handles)
        _solid_state_sync(self,awake)
        for (i, (o, v, a)) in enumerate(zip(self.dynamic_objs, vals, awake)):
            if i in self.kinematic_indices or not _is_live_object(o) or (not a):
                continue
            o.location = (v[0], v[1], v[2])
            o.rotation_mode = 'QUATERNION'
            o.rotation_quaternion = (v[6], v[3], v[4], v[5])
        try:
            if _regular_display_proxy_needed(bpy.context.scene.tumblify):
                _sync_regular_display_proxy(bpy.context.scene)
        except Exception:
            pass

    def delete_selected_runtime(self, context):
        """Delete selected dynamic Post bodies without invalidating Box3D handles.

        Static colliders (and any non-post object) are deliberately rejected while the
        solver is live; the user must stop Post Simulation before deleting those.
        """
        selected = list(context.selected_objects)
        if not selected:
            return 0
        dyn_indices = []
        unsupported = False
        for o in selected:
            found = False
            for (i, d) in enumerate(self.dynamic_objs):
                if _is_live_object(d) and d == o:
                    dyn_indices.append(i)
                    found = True
                    break
            if not found:
                unsupported = True
        if unsupported:
            return -1
        for i in sorted(set(dyn_indices), reverse=True):
            try:
                self.world.destroy(self.dynamic_handles[i])
            except Exception:
                pass
            o = self.dynamic_objs[i]
            for arrname in ('dynamic_objs', 'dynamic_handles', 'dynamic_collision', 'dynamic_material', 'dynamic_scales'):
                arr = getattr(self, arrname)
                if i < len(arr):
                    arr.pop(i)
            try:
                if _is_live_object(o):
                    bpy.data.objects.remove(o, do_unlink=True)
            except Exception:
                pass
        removed = sorted(set(dyn_indices))

        def remap(indices):
            out = set()
            for old in indices:
                if old in removed:
                    continue
                shift = sum((1 for r in removed if r < old))
                out.add(old - shift)
            return out
        self.kinematic_indices = remap(self.kinematic_indices)
        return len(removed)

    def _finish(self, context):
        global _RUNTIME, STOP_REQUESTED
        _restore_transform_hud(self, context)
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        if getattr(self, '_draw_handle', None):
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        _finish_runtime_move(self)
        self._sync()
        for o in self.dynamic_objs:
            if _is_live_object(o):
                o[FROZEN_PROP] = True
                o[RUNTIME_PROP] = True
                o.tumblify.role = 'NONE'
        _restore_mesh_collider_selection(getattr(self, '_mesh_selection_lock', None))
        for (name, old) in getattr(self, '_post_passive_selection_lock', {}).items():
            o = bpy.data.objects.get(name)
            if o:
                o.hide_select = old
        self.world.close()
        _solid_state_restore()
        _RUNTIME = None
        STOP_REQUESTED = False
        self.report({'INFO'}, f'Interactive stopped. Frozen {len(self.dynamic_objs)} settled objects')
        return {'FINISHED'}

    def _modal_impl(self, context, event):
        global STOP_REQUESTED
        if self.moving_selected and event.value == 'PRESS' and (event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE', 'RIGHTMOUSE', 'ESC'}):
            self.pending_move_finish = True
            return {'PASS_THROUGH'}
        if event.type == 'ESC' or STOP_REQUESTED:
            return self._finish(context)
        if event.value == 'PRESS' and event.type in {'X', 'DEL'}:
            n = self.delete_selected_runtime(context)
            if n < 0:
                self.report({'WARNING'}, 'Selected object cannot be deleted safely during Interactive. Stop the simulation first.')
            elif n:
                self.report({'INFO'}, f'Deleted {n} post-simulation object(s)')
            return {'RUNNING_MODAL'}
        if not self.moving_selected:
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                self._mouse_box_origin = (int(event.mouse_region_x), int(event.mouse_region_y))
                self._mouse_box_drag = False
                self._mouse_box_current = self._mouse_box_origin
                self._mouse_box_mode = 'SUB' if event.ctrl else 'ADD' if event.shift else 'SET'
                return {'PASS_THROUGH'}
            if event.type == 'MOUSEMOVE' and getattr(self, '_mouse_box_origin', None) is not None:
                (ax, ay) = self._mouse_box_origin
                if (event.mouse_region_x - ax) ** 2 + (event.mouse_region_y - ay) ** 2 >= 49:
                    self._mouse_box_drag = True
                if self._mouse_box_drag:
                    self._mouse_box_current = (int(event.mouse_region_x), int(event.mouse_region_y))
                    if context.area:
                        context.area.tag_redraw()
                    return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and (getattr(self, '_mouse_box_origin', None) is not None):
                (ax, ay) = self._mouse_box_origin
                drag = bool(self._mouse_box_drag)
                self._mouse_box_origin = None
                self._mouse_box_current = None
                self._mouse_box_drag = False
                if context.area:
                    context.area.tag_redraw()
                if drag:
                    try:
                        (bx, by) = (int(event.mouse_region_x), int(event.mouse_region_y))
                        bpy.ops.view3d.select_box('EXEC_DEFAULT', xmin=min(ax, bx), xmax=max(ax, bx), ymin=min(ay, by), ymax=max(ay, by), mode=self._mouse_box_mode)
                    except Exception as exc:
                        self.report({'WARNING'}, f'Box selection failed: {exc}')
                    return {'RUNNING_MODAL'}
            if event.type == 'B' and event.value == 'PRESS':
                try:
                    bpy.ops.view3d.select_box('INVOKE_DEFAULT', mode='SUB' if event.ctrl else 'ADD' if event.shift else 'SET')
                    return {'RUNNING_MODAL'}
                except Exception:
                    return {'PASS_THROUGH'}
        if event.type == 'G' and event.value == 'PRESS':
            n = self._activate_selected_for_move(context)
            if n:
                self.moving_selected = True
                self.pending_move_finish = False
                return {'PASS_THROUGH'}
        if event.type == 'TIMER':
            if self.pending_move_finish:
                _finish_runtime_move(self)
            self._release_unselected_kinematics(context)
            self._update_kinematics()
            self.world.step(self.dt, int(context.scene.tumblify.post_substeps))
            self._sync()
            if context.area:
                context.area.tag_redraw()
        return {'PASS_THROUGH'}

    def modal(self, context, event):
        return _dispatch_runtime_modal(self, context, event, 'Tumblify Interactive simulation')

class TU_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname=__package__
    custom_presets: CollectionProperty(type=TU_PresetItem)
    preset_index: IntProperty(default=0,min=0)
    editor_preset: EnumProperty(name='Preset',items=_pref_editor_items,update=_pref_selection_update)
    editor_values: PointerProperty(type=TU_PresetDraft)
    editor_dirty: BoolProperty(default=False,options={'SKIP_SAVE'})
    editor_loading: BoolProperty(default=False,options={'SKIP_SAVE'})
    def draw(self,context):
        layout=self.layout

        brand=layout.box()
        brand.label(text='Tumblify', icon='MOD_PHYSICS')
        brand.label(text='Physics-Based Object Placement for Blender')
        brand.label(text='Developed by Andrei Petrukovich')
        brand.label(text='Powered by Box3D Physics Engine')
        brand.label(text='Box3D is developed by Erin Catto and licensed under MIT.')
        brand.label(text='Tumblify is an independent Blender add-on and is not an official Box3D product.')

        links=layout.box()
        links.label(text='Links', icon='URL')
        row=links.row(align=True)
        op=row.operator('wm.url_open', text='Andrei Petrukovich', icon='URL'); op.url='https://www.artstation.com/andrewp'
        op=row.operator('wm.url_open', text='Box3D Engine', icon='URL'); op.url='https://github.com/erincatto/box3d'
        op=row.operator('wm.url_open', text='Box3D License', icon='TEXT'); op.url='https://github.com/erincatto/box3d/blob/f36d061ba26998c87cb7dc593a4065aaa03fb7eb/LICENSE'

        box=layout.box()
        ps=layout.box(); ps.label(text='Simulation Presets',icon='PRESET')
        row=ps.row(align=True); row.prop(self,'editor_preset',text='Preset')
        row.operator('tumblify.preset_editor_reload',text='',icon='FILE_REFRESH')
        draft=self.editor_values
        section=ps.column(align=True)
        section.label(text='Simulation Timing')
        row=section.row(align=True); row.prop(draft,'physics_frequency'); row.prop(draft,'physics_substeps')
        section.separator(); section.label(text='Contact Solver')
        for field in ('solver_scale','contact_hertz','contact_damping','contact_push_speed','contact_recycle_distance'):
            section.prop(draft,field)
        section.separator(); section.label(text='Thin Objects')
        row=section.row(align=True); row.prop(draft,'precision_auto_bullet'); row.prop(draft,'precision_disable_recycling')
        row=ps.row(align=True)
        row.enabled=self.editor_dirty
        row.operator('tumblify.preset_editor_save',text='Save as Preset Defaults',icon='FILE_TICK')
        ps.label(text='Unsaved changes' if self.editor_dirty else 'Saved values',icon='ERROR' if self.editor_dirty else 'CHECKMARK')
        ps.separator()
        row=ps.row(align=True)
        row.operator('tumblify.preset_save',text='New From Scene',icon='ADD')
        row.operator('tumblify.preset_remove',text='Delete User Preset',icon='REMOVE')
        ps.operator('tumblify.preset_reset',text='Restore Factory Presets',icon='LOOP_BACK')
        ps.label(text='Factory reset keeps user-created presets.',icon='INFO')
        box.label(text='Box3D Diagnostics',icon='PREFERENCES')
        row=box.row(); row.scale_y=1.4
        row.operator('tumblify.check',text='Validate Box3D Setup',icon='CHECKMARK')

def _draw_contact_solver(layout, s):
    box=layout.box()
    box.label(text='Contact Solver',icon='MOD_PHYSICS')
    if s.contact_preset == 'CUSTOM':
        row=box.row(align=True); row.label(text='Custom contact tuning',icon='PREFERENCES'); row.operator('tumblify.preset_save',text='',icon='ADD')
    row=box.row(align=True)
    row.prop(s,'solver_scale',text='Physics Scale')
    row.prop(s,'contact_hertz',text='Hertz')
    row=box.row(align=True)
    row.prop(s,'contact_damping',text='Damping')
    row.prop(s,'contact_push_speed',text='Push-out')
    row=box.row(align=True)
    row.prop(s,'contact_recycle_distance',text='Recycle Distance')
    row.prop(s,'precision_auto_bullet',text='Thin CCD',toggle=True)
    if s.precision_auto_bullet:
        box.prop(s,'precision_bullet_thickness',text='CCD Thickness')
    box.prop(s,'precision_disable_recycling',text='Fresh Contacts for Thin Bodies')


def _draw_container_settings(layout,obj):
    st=obj.tumblify
    layout.prop(st, 'container_form', text='Form')
    layout.prop(st, 'container_wall_depth_override', text='Wall Thickness (0 = Auto)')
    layout.prop(st, 'container_open_top', text='Add Lid', invert_checkbox=True)
    layout.prop(st, 'container_top_offset')
    layout.prop(st, 'container_bottom_offset')


def _draw_list_collision_controls(layout,s,kind):
    context=bpy.context
    collision=layout.column(); collision.separator(); collision.label(text='Collision',icon='MOD_PHYSICS')
    row=collision.row(align=True); row.prop(s,'assign_shape',text='Shape')
    assign=row.row(align=True); assign.enabled=kind!='DYNAMIC' or s.assign_shape not in {'MESH','TERRAIN'}
    op=assign.operator('tumblify.set_list_proxy',text='Assign',icon='CHECKMARK'); op.list_kind=kind; op.shape=s.assign_shape
    if s.assign_shape in {'HULL','COMPOUND','LOOSE'}: collision.prop(s,'convex_simplify',slider=True)
    if s.assign_shape=='COMPOUND': collision.prop(s,'compound_max_hulls')
    if s.assign_shape=='LOOSE':
        collision.prop(s,'loose_max_parts')
        collision.prop(s,'loose_auto_split_connected',toggle=True)
        if s.loose_auto_split_connected: collision.prop(s,'loose_split_angle',text='Split Angle')
        collision.prop(s,'auto_fit_tolerance',text='Part Fit Tolerance')
    if s.assign_shape=='CONTAINER':
        obj=context.view_layer.objects.active
        if obj and obj.select_get() and obj.type=='MESH':
            box=collision.box();box.label(text=obj.name+' - Container')
            _draw_container_settings(box,obj)
        else:collision.label(text='Select a container to edit its settings')
    if kind=='STATIC':_draw_surface_accuracy(collision,context)
    row=collision.row(align=True)
    row.operator('tumblify.join_compound',text='Join Compound',icon='AUTOMERGE_ON')
    op=row.operator('tumblify.toggle_collision',text='Show Collision',icon='HIDE_OFF',depress=s.show_collision_debug); op.list_kind=kind


def _draw_surface_accuracy(layout,context):
    fast=context.scene.tumblify.mode=='FAST'
    obj=context.view_layer.objects.active
    if obj is None or not obj.select_get():return
    obj=_collision_target_from_object(context.scene,obj)
    if obj is None or obj.type!='MESH':return
    source=fast and _collision_target_is_source(context.scene,obj)
    shape=(_fast_collision_fit(obj,context.evaluated_depsgraph_get())['shape'] if source else _fast_static_collision_shape(obj)) if fast else obj.tumblify.shape
    if not fast and shape not in {'MESH','TERRAIN'}:return
    box=layout.box();box.label(text=obj.name+' — Collision')
    if fast:
        op=box.operator_menu_enum('tumblify.viewport_set_collision','shape',text='Collision Model: '+SHAPE_LABELS.get(shape,shape),icon=SHAPE_ICONS.get(shape,'MESH_DATA'));op.source_name=obj.name
    box.label(text='Changes apply on next simulation')
    if shape=='TERRAIN':
        box.prop(obj.tumblify,'terrain_resolution');box.label(text='Local XY upper surface; no overhangs')
    elif shape=='MESH':
        box.prop(obj.tumblify,'mesh_quality')
        if obj.tumblify.mesh_quality=='CUSTOM':box.prop(obj.tumblify,'mesh_detail_percent',slider=True)
        box.prop(obj.tumblify,'mesh_thickness_mode')
        if obj.tumblify.mesh_thickness_mode=='INWARD':box.prop(obj.tumblify,'mesh_thickness')
    elif shape in {'HULL','COMPOUND','LOOSE'}:
        settings=context.scene.tumblify;box.prop(settings,'convex_simplify',slider=True)
        if shape=='COMPOUND':box.prop(settings,'compound_max_hulls')
        if shape=='LOOSE':
            box.prop(settings,'loose_max_parts');box.prop(settings,'loose_auto_split_connected')
            if settings.loose_auto_split_connected:box.prop(settings,'loose_split_angle')
    elif shape=='CONTAINER':
        _draw_container_settings(box,obj)



def _draw_emitter_collision(layout,context):
    st=context.scene.tumblify
    emitters=[obj for obj in _advanced_emitter_objects(context.scene) if obj.type=='MESH']
    box=layout.box();box.label(text='Collision',icon='MOD_PHYSICS')
    if not emitters:box.label(text='Empty emitters use a spherical Fill volume');return
    active=context.view_layer.objects.active
    obj=active if active in emitters else emitters[0]
    row=box.row(align=True);row.prop(st,'assign_shape',text='Shape')
    row.operator('tumblify.set_emitter_proxy',text='Assign').shape=st.assign_shape
    box.label(text=f'{obj.name}: {obj.tumblify.shape.title()}')
    box.label(text='Changes apply on next simulation')
    if st.spawn_location=='VOLUME' and not st.fill_emitter:box.label(text='Enable Fill Emitter for volume collision')
    shape=obj.tumblify.shape
    if shape in {'HULL','COMPOUND','LOOSE'}:box.prop(st,'convex_simplify',slider=True)
    if shape=='COMPOUND':box.prop(st,'compound_max_hulls')
    if shape=='LOOSE':box.prop(st,'loose_max_parts');box.prop(st,'loose_auto_split_connected')
    if shape=='CONTAINER':_draw_container_settings(box,obj)
    if shape=='MESH':
        box.prop(obj.tumblify,'mesh_quality')
        if obj.tumblify.mesh_quality=='CUSTOM':box.prop(obj.tumblify,'mesh_detail_percent')
        box.prop(obj.tumblify,'mesh_thickness_mode')
        if obj.tumblify.mesh_thickness_mode=='INWARD':box.prop(obj.tumblify,'mesh_thickness')
        box.label(text='Thickness affects open surfaces only')
        if obj.tumblify.mesh_thickness_mode=='AUTO':box.label(text='Auto: size-based, half on each side')
        if obj.tumblify.mesh_thickness_mode=='INWARD':box.label(text='Extrudes against face normals')
    if shape=='TERRAIN':box.prop(obj.tumblify,'terrain_resolution')
    box.operator('tumblify.toggle_collision',text='Show Collision',icon='HIDE_OFF',depress=st.show_collision_debug)


def _draw_result_selection(layout):
    row=layout.row(align=True);row.enabled=_RUNTIME is None
    row.operator('tumblify.select_results',text='Select Last Drop',icon='RESTRICT_SELECT_OFF').scope='LAST'
    row.operator('tumblify.select_results',text='Select All',icon='SELECT_SET').scope='ALL'


def _draw_drop_result_actions(layout):
    _draw_result_selection(layout)
    row=layout.row(align=True);row.enabled=_RUNTIME is None;row.operator_context='INVOKE_DEFAULT'
    row.operator('tumblify.delete_last_drop',text='Delete Last Drop',icon='TRASH')
    row.operator('tumblify.clear_spawned',text='Cleanup Scene',icon='TRASH').cleanup_scene=True


class TU_PT_panel(bpy.types.Panel):
    bl_label = 'Tumblify'
    bl_idname = 'TU_PT_paint'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Tumblify'

    def draw(self, context):
        s = context.scene.tumblify
        l = self.layout
        l.prop(s, 'mode', expand=True)
        display=l.row(align=True);display.label(text='Display')
        for value,icon in (('MESH','MESH_DATA'),('POINT','POINTCLOUD_DATA'),('BOX','CUBE')):display.prop_enum(s,'instance_display',value,text='',icon=icon)
        if s.mode == 'FAST':
            sources = l.box()
            sources.label(text='Dynamic', icon='MESH_CUBE')
            row = sources.row(align=True)
            row.operator('tumblify.fast_capture', text='Use Selected', icon='RESTRICT_SELECT_OFF')
            row.operator('tumblify.fast_capture', text='', icon='ADD').append = True
            row.operator('tumblify.toggle_collision', text='Show Collision', icon='HIDE_OFF', depress=s.show_collision_debug)
            srcs = _fast_source_objects(context.scene)
            sources.label(text=f'{len(srcs)} dynamic object(s)')
            if srcs and getattr(s, 'fast_shape_summary', ''):
                sources.label(text=s.fast_shape_summary)
            statics = l.box()
            statics.label(text='Static Colliders', icon='ANCHOR_CENTER')
            statics.prop(s, 'fast_static_scope', text='Colliders')
            if s.fast_static_scope=='SCENE':
                rec=_get_scene_cache(context.scene)
                row=statics.row(align=True);row.operator_context='INVOKE_DEFAULT'
                row.operator('tumblify.prepare_scene',text='Prepare Scene' if rec is None else 'Prepare Scene Again',icon='FILE_REFRESH')
                statics.label(text=(f"Ready: {len(rec['objects'])} colliders" if rec else 'Prepare Scene before starting'),icon='CHECKMARK' if rec else 'INFO')
            if s.fast_static_scope == 'SELECTED':
                row = statics.row(align=True)
                row.operator('tumblify.fast_capture_statics', text='Use Selected Colliders', icon='RESTRICT_SELECT_OFF')
                row.operator('tumblify.fast_capture_statics', text='', icon='ADD').append = True
                row.label(text=str(len(_fast_static_objects(context.scene, _fast_source_objects(context.scene)))))
            _draw_surface_accuracy(l, context)
            physics = l.box()
            header=physics.row(align=True)
            header.prop(s,'ui_fast_simulation',text='Simulation',icon='TRIA_DOWN' if s.ui_fast_simulation else 'TRIA_RIGHT',emboss=False)
            if s.ui_fast_simulation:
                row = physics.row(align=True)
                row.prop(s, 'contact_preset', text='Preset')
                row.operator('tumblify.preset_preferences', text='', icon='PREFERENCES')
                row = physics.row(align=True)
                physics.prop(s, 'fast_spread', text='Spread Radius')
                row.prop(s, 'fast_objects_per_tick', text='Spread Count')
                row.prop(s, 'fast_stack_count', text='Stack Height')
                physics.label(text=f'Objects / Drop: {int(s.fast_objects_per_tick)*int(s.fast_stack_count)}')
                row = physics.row(align=True)
                row.prop(s, 'fast_scale_min', text='Scale From')
                row.prop(s, 'fast_scale_max', text='To')
                row = physics.row(align=True)
                row.prop(s, 'fast_sleep', text='Sleep')
                row.prop(s, 'fast_floor', text='Floor')
                physics.prop(s, 'gravity_strength', text='Gravity (m/s²)')
                if s.contact_preset == 'CUSTOM':
                    physics.prop(s, 'contact_hertz', text='Contact Hertz')
                physics.label(text='Output')
                row = physics.row(align=True)
                row.prop_enum(s, 'result_output', 'INSTANCES', text='Combined')
                row.prop_enum(s, 'result_output', 'OBJECTS', text='Separated')
                (_fhz, _fsub) = (int(s.physics_frequency), int(s.physics_substeps))
                physics.label(text=f'{_fhz * _fsub} solver Hz ({_fhz} Hz x {_fsub})', icon='TIME')
            run = l.box()
            row = run.row(align=True)
            row.scale_y = 1.65
            row.operator('tumblify.fast_start', text='Start Fast Drop', icon='PLAY')
            row.operator('tumblify.fast_interactive_start', text='Interactive', icon='VIEW_PAN')
            results = l.box()
            head = results.row(align=True)
            head.prop(s, 'ui_fast_results', text='Result Tools', icon='TRIA_DOWN' if s.ui_fast_results else 'TRIA_RIGHT', emboss=False)
            if s.ui_fast_results:
                _draw_result_selection(results)
                row = results.row(align=True)
                row.operator('tumblify.realize_instances', text='Realize Instances')
                row.operator('tumblify.join_all_instances', text='Join All')
                row = results.row(align=True)
                row.operator('tumblify.delete_last_fast', text='Delete Last', icon='TRASH')
                row.operator('tumblify.delete_all_fast', text='Delete All', icon='TRASH')
            return
        configured_emitters = _advanced_emitter_objects(context.scene)
        advanced_ready = bool(getattr(s, 'advanced_emitter_ready', False) and configured_emitters)
        if not advanced_ready:
            setup = l.box()
            row = setup.row(align=True)
            row.scale_y = 2.0
            row.operator('tumblify.prepare_emitter', text='Create Emitter', icon='MESH_UVSPHERE')
            use = row.row(align=True)
            active = context.view_layer.objects.active
            can_use = bool(active and active.select_get() and (active.type == 'MESH') and (active.name not in {BRUSH_NAME, GUIDE_NAME, LEGACY_ADV_DEMO_SOURCE_NAME}))
            use.enabled = can_use
            use.operator('tumblify.use_active_emitter', text='Use Selected', icon='EYEDROPPER')
            _draw_drop_result_actions(l)
            return
        primary = l.box()
        row = primary.row(align=True)
        row.prop(s, 'emitter_object', text='Emitter')
        row.operator('tumblify.use_active_emitter', text='', icon='EYEDROPPER')
        row.operator('tumblify.toggle_emitter_wire', text='', icon='MOD_WIREFRAME', depress=bool(s.emitter_wire_display))
        tabs = primary.row(align=True)
        tabs.scale_y = 1.15
        tabs.prop_enum(s, 'ui_advanced_run_tab', 'REALTIME', text='Real Time')
        tabs.prop_enum(s, 'ui_advanced_run_tab', 'OFFLINE', text='Offline')
        tabs.prop_enum(s, 'ui_advanced_run_tab', 'BAKE', text='Bake')
        run_tab = str(getattr(s, 'ui_advanced_run_tab', 'REALTIME'))
        if run_tab in {'OFFLINE', 'BAKE'}:
            primary.prop(s, 'drop_offline_count', text='Objects')
        if run_tab == 'BAKE':
            row = primary.row(align=True)
            row.prop(s, 'emitter_bake_frames', text='Frames')
            row.prop(s, 'emitter_bake_frame_start', text='Start Frame')
        primary.prop(s, 'spawn_rate', text='Spawn Rate')
        output = primary.row(align=True)
        output.scale_y = 1.25
        if run_tab=='BAKE':
            combined=output.row(align=True);combined.enabled=False
            combined.operator('tumblify.toggle_result_output',text='Combined',depress=False)
            output.operator('tumblify.toggle_result_output',text='Separated',depress=True)
        else:
            output.prop_enum(s, 'result_output', 'INSTANCES', text='Combined')
            output.prop_enum(s, 'result_output', 'OBJECTS', text='Separated')
        action = primary.row(align=True)
        action.scale_y = 1.65
        can_spawn = _advanced_spawn_ready(context.scene) and _RUNTIME is None
        if run_tab == 'REALTIME':
            start = action.row(align=True)
            start.enabled = can_spawn
            start.operator('tumblify.advanced_start', text='Start Drop', icon='PLAY')
            action.operator('tumblify.fast_interactive_start', text='Interactive', icon='VIEW_PAN')
        elif run_tab == 'OFFLINE':
            start = action.row(align=True)
            start.enabled = can_spawn
            start.operator('tumblify.advanced_start', text='Generate Offline', icon='REC')
        else:
            start = action.row(align=True)
            start.enabled = can_spawn
            start.operator('tumblify.emitter_bake', text='Bake Animation', icon='KEY_HLT')
            action.operator('tumblify.emitter_clear_bake', text='', icon='X')
        details = l.box()
        details.label(text='Details', icon='PREFERENCES')
        grid = details.grid_flow(row_major=True, columns=2, even_columns=True, even_rows=True, align=True)
        detail_buttons = (('EMITTER', 'Emitter', 'MESH_UVSPHERE'), ('SOURCES', 'Dynamic', 'OUTLINER_OB_MESH'), ('STATIC', 'Static', 'ANCHOR_CENTER'), ('RANDOM', 'Randomization', 'RNDCURVE'), ('PHYSICS', 'Physics', 'PHYSICS'))
        for (value, label, icon) in detail_buttons:
            grid.prop_enum(s, 'ui_advanced_detail', value, text=label, icon=icon)
        details.separator(factor=0.7)
        content = details.box()
        page = str(getattr(s, 'ui_advanced_detail', 'EMITTER'))
        src = s.dynamic_sources[s.dynamic_index].obj if 0 <= s.dynamic_index < len(s.dynamic_sources) else None
        if page in {'SOURCES', 'COLLISION'}:
            dyn = content.column()
            dyn.label(text=f'Dynamic — {len(s.dynamic_sources)}', icon='OUTLINER_OB_MESH')
            row = dyn.row()
            row.template_list('TU_UL_object_refs', 'dynamic', s, 'dynamic_sources', s, 'dynamic_index', rows=4)
            tools = row.column(align=True)
            tools.operator('tumblify.mark_source', text='', icon='ADD')
            op = tools.operator('tumblify.remove_role_item', text='', icon='REMOVE')
            op.list_kind = 'DYNAMIC'
            op = tools.operator('tumblify.list_menu', text='', icon='DOWNARROW_HLT')
            op.list_kind = 'DYNAMIC'
            _draw_list_collision_controls(dyn, s, 'DYNAMIC')
            if 0 <= s.dynamic_index < len(s.dynamic_sources):
                src = s.dynamic_sources[s.dynamic_index].obj
                if src:
                    dyn.separator()
                    dyn.label(text=f'Selected: {src.name}')
                    dyn.prop(src.tumblify, 'material_preset', text='Physics Material')
                    dyn.prop(src.tumblify, 'mass_multiplier', text='Mass Multiplier')
                    dyn.prop(src.tumblify, 'spawn_weight', text='Spawn Weight %')
                    total_weight = sum((max(0.0, float(getattr(o.tumblify, 'spawn_weight', 100.0))) for o in _scene_role_objects(context.scene, 'SOURCE')))
                    if total_weight > 1e-09:
                        eff = 100.0 * max(0.0, float(src.tumblify.spawn_weight)) / total_weight
                        dyn.label(text=f'Effective probability: {eff:.1f}%')
        elif page == 'STATIC':
            static_count = len(s.static_objects)
            stat = content.column()
            stat.label(text=f'Static Colliders — {static_count}', icon='ANCHOR_CENTER')
            row = stat.row()
            row.template_list('TU_UL_object_refs', 'static', s, 'static_objects', s, 'static_index', rows=4)
            tools = row.column(align=True)
            tools.operator('tumblify.mark_static', text='', icon='ADD')
            op = tools.operator('tumblify.remove_role_item', text='', icon='REMOVE')
            op.list_kind = 'STATIC'
            op = tools.operator('tumblify.list_menu', text='', icon='DOWNARROW_HLT')
            op.list_kind = 'STATIC'
            _draw_list_collision_controls(stat, s, 'STATIC')
        elif page == 'EMITTER':
            emit = content.column()
            emit.label(text='Emitter Settings', icon='EMPTY_AXIS')
            emit.prop(s, 'emitter_collection', text='Collection')
            emit.prop(s, 'spawn_location', text='Spawn Location')
            if s.spawn_location == 'VOLUME':
                emit.prop(s, 'fill_emitter', text='Fill Emitter', toggle=True)
                if s.fill_emitter:
                    eo = s.emitter_object
                    if eo and eo.name != BRUSH_NAME:
                        try:
                            _shape_name = dict(((i[0], i[1]) for i in SHAPES)).get(eo.tumblify.shape, eo.tumblify.shape)
                        except Exception:
                            _shape_name = 'Auto Shell'
                        emit.label(text=f'Container collision: {_shape_name}', icon='MOD_PHYSICS')
                if s.emitter_collection:
                    emit.label(text=f'Collection emitter: {len(_collection_emitter_objects(s.emitter_collection, context.scene))} object(s).')
                elif not s.emitter_object or s.emitter_object.name == BRUSH_NAME:
                    emit.prop(s, 'emitter_radius')
            else:
                surf = s.emitter_object
                if not s.emitter_collection and (surf is None or surf.type != 'MESH'):
                    emit.label(text='Choose a Mesh emitter for Surface', icon='INFO')
                if not s.emitter_collection and surf and (surf.type == 'MESH'):
                    emit.prop_search(s, 'scatter_vertex_group', surf, 'vertex_groups', text='Mask')
                emit.prop(s, 'scatter_magnetized', text='Magnetized', toggle=True)
                if s.scatter_magnetized:
                    col = emit.column(align=True)
                    col.prop(s, 'scatter_magnet_strength')
                    col.prop(s, 'scatter_magnet_damping')
                if run_tab == 'REALTIME':
                    emit.prop(s, 'scatter_stop_delay', text='Stop Delay')
            _draw_emitter_collision(emit,context)
        elif page == 'RANDOM':
            rand = content.column()
            rand.label(text='Randomization', icon='RNDCURVE')
            row = rand.row(align=True)
            row.prop(s, 'random_scale_min', text='Scale Min')
            row.prop(s, 'random_scale_max', text='Max')
            rand.prop(s, 'advanced_random_rotation', text='Random Rotation', toggle=True)
            row = rand.row(align=True)
            row.enabled = s.advanced_random_rotation
            row.label(text='Rotation')
            row.prop(s, 'random_rot_x', toggle=True)
            row.prop(s, 'random_rot_y', toggle=True)
            row.prop(s, 'random_rot_z', toggle=True)
        elif page == 'PHYSICS':
            physics = content.column()
            physics.label(text='Physics', icon='PHYSICS')
            row = physics.row(align=True)
            row.prop(s, 'contact_preset', text='Preset')
            row.operator('tumblify.preset_preferences', text='', icon='PREFERENCES')
            if s.contact_preset != 'CUSTOM':
                row.operator('tumblify.preset_save', text='', icon='ADD')
            row = physics.row(align=True)
            row.prop(s, 'physics_frequency', text='Frequency')
            row.prop(s, 'physics_substeps', text='Multiplier')
            physics.label(text=f'Solver sub-step rate: ~{int(s.physics_frequency) * int(s.physics_substeps)} Hz', icon='TIME')
            _draw_contact_solver(physics, s)
            floor = physics.box()
            floor.prop(s, 'enable_floor', text='Enable Floor')
            if s.enable_floor:
                row = floor.row(align=True)
                row.prop(s, 'floor_z', text='Floor Z')
                row.prop(s, 'static_friction', text='Friction')
            grav = physics.row(align=True)
            try:
                gravity_supported = bool(getattr(get_lib(), 'has_gravity_control', False))
            except Exception:
                gravity_supported = False
            grav.enabled = gravity_supported
            grav.prop(s, 'gravity_strength', text='Gravity Strength')
            if not gravity_supported:
                physics.label(text='Gravity control requires an updated native DLL', icon='INFO')
            physics.prop(s, 'self_collision', text='Self Collision')
            sleep = physics.box()
            sleep.prop(s, 'forced_sleep', text='Forced Sleep')
            if s.forced_sleep:
                row = sleep.row(align=True)
                row.prop(s, 'sleep_linear_threshold', text='Linear')
                row.prop(s, 'sleep_angular_threshold', text='Angular')
                sleep.prop(s, 'sleep_delay', text='Delay')
            physics.prop(s, 'physics_threads', text='Threads (0 = Auto)')
        if run_tab == 'BAKE':
            cache_box = l.box()
            head = cache_box.row(align=True)
            head.prop(s, 'ui_bake_cache', text='Bake Cache', icon='TRIA_DOWN' if s.ui_bake_cache else 'TRIA_RIGHT', emboss=False)
            if s.ui_bake_cache:
                cache_ui = cache_box.column()
                cache_ui.prop(s, 'emitter_bake_cache_mode', expand=True)
                if s.emitter_bake_cache_mode == 'AUTO':
                    cache_ui.prop(s, 'emitter_bake_cache_memory_mb')
                fps = float(context.scene.render.fps) / max(1e-06, float(context.scene.render.fps_base))
                duration = max(0.0, float(s.emitter_bake_frames - 1) / max(1e-06, fps))
                bodies = min(max(1, int(s.drop_offline_count)), max(1, 1 + int(math.floor(duration * max(1.0, float(s.spawn_rate)) + 1e-09))))
                (backend, estimate) = select_backend(s.emitter_bake_cache_mode, bodies, s.emitter_bake_frames, s.emitter_bake_cache_memory_mb)
                cache_ui.label(text=f'Estimated data: {estimate / 1024 ** 2:.1f} MiB — {backend.title()}')
                if s.emitter_bake_cache_mode != 'MEMORY':
                    cache_ui.prop(s, 'emitter_bake_cache_directory')
                    cache_ui.prop(s, 'emitter_bake_cache_keep')
                cache_ui.label(text='Keyframes still use Blender memory')
        l.separator(factor=1.8)
        result = l.box()
        result.label(text='Result Tools', icon='TOOL_SETTINGS')
        _draw_drop_result_actions(result)
        row = result.row(align=True)
        row.operator('tumblify.realize_instances', text='Realize Instances')
        row.operator('tumblify.join_all_instances', text='Join All')
        row = result.row(align=True)
        row.operator('tumblify.fix_selected', text='Fix Selected', icon='LOCKED')
        row.operator('tumblify.unfix_selected', text='Unfix', icon='UNLOCKED')

        # Panel.draw() remains read-only. Emitter wire/guide synchronization is
        # handled by property callbacks and operators, never during UI draw.

classes = (TU_PresetItem, TU_PresetDraft, TU_OT_preset_editor_save, TU_OT_preset_editor_reload, TU_OT_preset_edit_factory, TU_OT_preset_save, TU_OT_preset_remove, TU_OT_preset_reset, TU_OT_preset_preferences, TumblifyObjectSettings, TumblifyObjectRef, TumblifySceneSettings, TU_UL_object_refs, TU_OT_list_select, TU_MT_object_proxy, TU_MT_list_proxy, TU_OT_object_proxy_menu, TU_OT_set_object_proxy, TU_MT_list_menu, TU_OT_toggle_emitter_wire, TU_OT_prepare_emitter, TU_OT_use_active_emitter, TU_OT_fast_capture, TU_OT_prepare_scene, TU_OT_fast_start, TU_OT_advanced_start, TU_OT_fast_interactive_start, TU_OT_fast_set_collision, TU_OT_viewport_set_collision, TU_OT_fast_set_rotation_axes, TU_OT_fast_rotation_axes_popup, TU_MT_fast_rotation_axes_pie, TU_MT_fast_collision_popup, TU_OT_fast_capture_statics, TU_OT_fast_collision_overlay, TU_OT_assign_shape, TU_OT_mark_static, TU_OT_mark_source, TU_OT_remove_role_item, TU_OT_set_emitter_proxy, TU_OT_set_list_proxy, TU_OT_list_menu, TU_OT_list_action, TU_OT_join_compound, TU_OT_toggle_collision, TU_OT_toggle_result_output, TU_OT_check, TU_OT_auto_spawn_rate, TU_OT_clear_spawned, TU_OT_select_results, TU_OT_delete_last_fast, TU_OT_delete_all_fast, TU_OT_delete_last_drop, TU_OT_realize_instances, TU_OT_join_all_instances, TU_OT_fix_selected, TU_OT_unfix_selected, TU_OT_post_delete_all, TU_OT_post_select_all, TU_OT_post_join_selected, TU_OT_post_delete_selected, TU_OT_stop, TU_OT_drop_offline, TU_OT_emitter_clear_bake, TU_OT_emitter_bake, TU_OT_surface_run, TU_OT_run, TU_OT_post_run, TU_AddonPreferences, TU_PT_panel)

_CONTAINER_OBJECT_FIELDS = ('container_wall_thickness', 'container_safety_margin', 'container_proxy_depth', 'container_max_parts', 'container_radial_segments', 'container_height_segments', 'container_open_top', 'container_opening_clearance')

def _migrate_container_object_settings():
    scenes=list(bpy.data.scenes)
    current=getattr(bpy.context,'scene',None)
    if current in scenes:scenes.remove(current);scenes.insert(0,current)
    seen=set()
    for scene in scenes:
        if scene.get('Tumblify_ContainerObjectSettingsVersion',0)>=1:continue
        for obj in scene.objects:
            if obj.as_pointer() in seen:continue
            seen.add(obj.as_pointer());ost=obj.tumblify
            for name in _CONTAINER_OBJECT_FIELDS:
                if not ost.is_property_set(name):ost[name]=getattr(scene.tumblify,name)
        scene['Tumblify_ContainerObjectSettingsVersion']=1


def _tumblify_migrate_legacy_scene_data():
    """Migrate pre-Tumblify product namespaces without reintroducing them into runtime."""
    try:
        migrate_product_namespace(bpy)
        cleanup_retired_helpers(bpy)
        for scene in bpy.data.scenes:
            if hasattr(scene, 'tumblify'):
                migrate_legacy_property_group(scene, scene.tumblify, LEGACY_SCENE_SETTINGS_KEY)
                migrate_legacy_idprops(scene)
                st=scene.tumblify
                if bool(st.get('fast_stack_mode',False)) and not st.is_property_set('fast_stack_count'):
                    st.fast_stack_count=max(1,min(10,int(st.fast_objects_per_tick)))
                    st.fast_objects_per_tick=1
                if 'fast_stack_mode' in st:del st['fast_stack_mode']
            for obj in scene.objects:
                if hasattr(obj, 'tumblify'):
                    migrate_legacy_property_group(obj, obj.tumblify, LEGACY_OBJECT_SETTINGS_KEY)
                migrate_legacy_idprops(obj)
        # Rename surviving helper datablocks by prefix. Names are cosmetic and safe to migrate.
        for collection in (bpy.data.objects, bpy.data.collections, bpy.data.node_groups, bpy.data.actions):
            for datablock in list(collection):
                try:
                    migrated_name = migrate_legacy_datablock_name(datablock.name)
                    if migrated_name != datablock.name:
                        datablock.name = migrated_name
                except Exception:
                    pass
    except Exception:
        pass


def _tumblify_post_register_init():
    """Run datablock-dependent startup only after Blender leaves _RestrictData.

    Blender may call add-on register() while bpy.data is a restricted proxy.
    Accessing bpy.data.objects in that phase raises:
    '_RestrictData' object has no attribute 'objects'.
    Returning a short delay retries until normal datablock access is available.
    """
    if not hasattr(bpy.types.Scene, 'tumblify'):
        return None
    try:
        _ = bpy.data.objects
        scene = getattr(bpy.context, 'scene', None)
        _tumblify_migrate_legacy_scene_data()
        _migrate_container_object_settings()
        _remove_legacy_guide_object()
        if _RUNTIME is None:
            _solid_state_restore()
            for saved_scene in bpy.data.scenes:
                _recover_saved_runtime(saved_scene)
                saved_st = saved_scene.tumblify
                if saved_st.ui_advanced_detail == 'COLLISION':
                    saved_st.ui_advanced_detail = 'SOURCES'
                legacy_preset = saved_st.get('contact_preset')
                if legacy_preset == 'DEFAULT' or (isinstance(legacy_preset, int) and legacy_preset == len(_preset_items(saved_st, bpy.context))):
                    saved_st.contact_preset = 'BALANCED'
        if scene is not None:
            try:
                _normalize_instance_display(scene)
            except Exception:
                pass
            if bpy.data.objects.get(BRUSH_NAME) is not None:
                ensure_brush(scene)
            _sync_emitter_wire(scene)
            _sync_emitter_guides(scene)
            if bool(getattr(scene.tumblify, 'advanced_emitter_ready', False)):
                _advanced_remove_legacy_demo_source(scene)
            _apply_global_instance_display(scene)
        return None
    except (AttributeError, RuntimeError):
        return 0.1
    except Exception:
        return None


@persistent
def _tumblify_shutdown_before_history(_dummy=None):
    """Stop owned native resources before Undo/Redo replaces their Blender IDs."""
    global _HISTORY_HAS_RUNTIME_SNAPSHOTS
    clear_mesh_cache()
    _clear_scene_collisions()
    if _RUNTIME is None and _COLLISION_OVERLAY_OWNER is None:
        return
    _HISTORY_HAS_RUNTIME_SNAPSHOTS=True
    try: _shutdown_active_runtime(bpy.context)
    finally: _cancel_owned_app_timers()


@persistent
def _tumblify_recover_after_history(_dummy=None):
    """Recover live snapshots in this file's history after an owned interruption."""
    global _HISTORY_HAS_RUNTIME_SNAPSHOTS, STOP_REQUESTED
    if not _HISTORY_HAS_RUNTIME_SNAPSHOTS:
        return
    # Later Redo/Undo can restore another active snapshot after the owner stopped.
    # Keep this file-local latch until load/unregister, without restarting physics.
    STOP_REQUESTED=False
    if _RUNTIME is None:
        for scene in bpy.data.scenes:
            if hasattr(scene,'tumblify'):
                _recover_saved_runtime(scene)


@persistent
def _tumblify_shutdown_before_load(_dummy=None):
    """Close native workers/worlds while their launch scene RNA still exists."""
    global _HISTORY_HAS_RUNTIME_SNAPSHOTS
    _HISTORY_HAS_RUNTIME_SNAPSHOTS=False
    clear_mesh_cache()
    _clear_scene_collisions()
    try: _shutdown_active_runtime(bpy.context)
    finally: _cancel_owned_app_timers()


@persistent
def _tumblify_normalize_display_on_load(_dummy=None):
    """Keep the production Drop display on real textured meshes after loading a .blend.

    v0.14.16 normalized legacy BOX/POINT values here, but that migration lived inside
    the old Rigid Body load handler.  Rigid Body was removed in v0.14.17, so keep
    this display migration as an independent Physics Drop handler.
    """
    global STOP_REQUESTED
    for callback in tuple(_NATIVE_TRANSFORM_CLEANUPS):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    _NATIVE_TRANSFORM_CLEANUPS.clear()
    STOP_REQUESTED = False
    try:
        _tumblify_migrate_legacy_scene_data()
        _migrate_container_object_settings()
        for scene in bpy.data.scenes:
            if hasattr(scene, 'tumblify'):
                _recover_saved_runtime(scene)
                try:
                    _normalize_instance_display(scene)
                except Exception:
                    pass
                try:
                    _apply_global_instance_display(scene)
                except Exception:
                    pass
    except Exception:
        pass

def register():
    # The legacy add-on and the extension share class names; only one can be registered.
    others=[m for m in bpy.context.preferences.addons.keys() if m!=__package__ and (m=='Tumblify' or m.endswith('.tumblify'))]
    if others:
        raise RuntimeError('Another Tumblify is already enabled ('+', '.join(others)+'). Disable and remove it in Preferences > Add-ons, then enable this one.')
    for c in classes:bpy.utils.register_class(c)
    bpy.types.Object.tumblify=PointerProperty(type=TumblifyObjectSettings); bpy.types.Scene.tumblify=PointerProperty(type=TumblifySceneSettings)
    bpy.types.Scene.tumblify_list_menu_kind=StringProperty(default='STATIC')
    bpy.types.Scene.tumblify_proxy_menu_kind=StringProperty(default='STATIC')
    bpy.types.Scene.tumblify_proxy_menu_index=IntProperty(default=0)
    if _tumblify_shutdown_before_load not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_tumblify_shutdown_before_load)
    if _invalidate_scene_inputs not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_invalidate_scene_inputs)
    if _tumblify_normalize_display_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_tumblify_normalize_display_on_load)
    for name,callback in (('undo_pre',_tumblify_shutdown_before_history),
                          ('redo_pre',_tumblify_shutdown_before_history),
                          ('undo_post',_tumblify_recover_after_history),
                          ('redo_post',_tumblify_recover_after_history)):
        handlers=getattr(bpy.app.handlers,name)
        if callback not in handlers: handlers.append(callback)
    try:
        if not bpy.app.timers.is_registered(_tumblify_selection_sync_timer): bpy.app.timers.register(_tumblify_selection_sync_timer,first_interval=0.15,persistent=True)
    except Exception: pass
    # bpy.data can be _RestrictData while Preferences is enabling an add-on.
    # Defer all datablock-dependent cleanup/sync until registration is finished.
    try:
        if not bpy.app.timers.is_registered(_tumblify_post_register_init):
            bpy.app.timers.register(_tumblify_post_register_init,first_interval=0.05)
    except Exception:
        pass

def unregister():
    global STOP_REQUESTED, _HISTORY_HAS_RUNTIME_SNAPSHOTS
    _HISTORY_HAS_RUNTIME_SNAPSHOTS=False
    STOP_REQUESTED=True
    _shutdown_active_runtime(bpy.context)
    clear_mesh_cache()
    _clear_scene_collisions()
    _solid_state_restore()
    for prep in tuple(_PREPARATION_DISPLAYS):prep.close()
    _cancel_owned_app_timers()
    if _invalidate_scene_inputs in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_invalidate_scene_inputs)
    if _tumblify_shutdown_before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_tumblify_shutdown_before_load)
    if _tumblify_normalize_display_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_tumblify_normalize_display_on_load)
    for name,callback in (('undo_pre',_tumblify_shutdown_before_history),
                          ('redo_pre',_tumblify_shutdown_before_history),
                          ('undo_post',_tumblify_recover_after_history),
                          ('redo_post',_tumblify_recover_after_history)):
        handlers=getattr(bpy.app.handlers,name)
        if callback in handlers: handlers.remove(callback)
    try:
        if bpy.app.timers.is_registered(_tumblify_post_register_init): bpy.app.timers.unregister(_tumblify_post_register_init)
    except Exception: pass
    try:
        if bpy.app.timers.is_registered(_tumblify_selection_sync_timer): bpy.app.timers.unregister(_tumblify_selection_sync_timer)
    except Exception: pass
    try:
        _remove_legacy_guide_object(); _clear_display_proxy_collection(); _clear_fast_collection()
        for o in list(bpy.data.objects):
            _remove_emitter_guide_nodes(o); _restore_emitter_wire_object(o)
    except Exception:
        pass
    del bpy.types.Object.tumblify; del bpy.types.Scene.tumblify
    if hasattr(bpy.types.Scene,'tumblify_list_menu_kind'): del bpy.types.Scene.tumblify_list_menu_kind
    if hasattr(bpy.types.Scene,'tumblify_proxy_menu_kind'): del bpy.types.Scene.tumblify_proxy_menu_kind
    if hasattr(bpy.types.Scene,'tumblify_proxy_menu_index'): del bpy.types.Scene.tumblify_proxy_menu_index
    for c in reversed(classes):bpy.utils.unregister_class(c)
