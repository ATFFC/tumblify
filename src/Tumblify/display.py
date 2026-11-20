# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Tumblify display/result subsystem.

Geometry Nodes batching, Combined result construction/realization, visual state
synchronization, and result collection helpers. This module must not import
addon.py or own modal/runtime globals.
"""
import bpy
from mathutils import Quaternion, Vector, Euler, Matrix

from .constants import (
    SPAWN_COLLECTION, FAST_COLLECTION, DISPLAY_MOD_NAME, DISPLAY_GROUP_PREFIX,
    DISPLAY_PROXY_COLLECTION, DISPLAY_PROXY_PROP, DISPLAY_PREV_TYPE_PROP,
    INSTANCE_RESULT_MOD_NAME, INSTANCE_RESULT_PROP, INSTANCE_HELPER_PROP,
    INSTANCE_SOURCE_COLLECTION_PROP, FROZEN_PROP, RUNTIME_PROP, FIXED_PROP,
    GUIDE_MOD_NAME,
    FAST_CURSOR_NAME, FAST_CURSOR_RAY_NAME, FAST_GHOST_NAME,
)
from .collision import world_pose_scale, _local_bounds
from .compat import archive_legacy_combined_results



# Runtime-only Workbench colors. Object/material shader data is never replaced.
_SOLID_COLOR_PREV='Tumblify_PreSimulationColor'
_SOLID_VIEWS=[]
_SOLID_COLORED={}
_ACTIVE_COLOR=(0.12,0.8,0.2,1.0)

def _solid_state_begin():
    if _SOLID_VIEWS:return
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type!='VIEW_3D':continue
            shading=area.spaces.active.shading
            _SOLID_VIEWS.append((shading,shading.color_type))
            shading.color_type='OBJECT'

def _solid_state_color(obj,awake):
    if not _is_live_object(obj):return
    key=obj.as_pointer()
    if key not in _SOLID_COLORED:
        if _SOLID_COLOR_PREV not in obj:obj[_SOLID_COLOR_PREV]=tuple(obj.color)
        _SOLID_COLORED[key]=obj
    target=_ACTIVE_COLOR
    if any(abs(a-b)>1e-5 for a,b in zip(obj.color,target)):obj.color=target

def _solid_state_sync(owner,awake):
    for i,obj in enumerate(getattr(owner,'dynamic_objs',[])):
        if i<len(awake):
            _solid_state_color(obj,True)
            if _is_live_object(obj) and obj.type=='MESH' and not obj.get(INSTANCE_RESULT_PROP):
                mode=bpy.context.scene.tumblify.instance_display;mod=obj.modifiers.get(DISPLAY_MOD_NAME)
                if (mode!='MESH' and (mod is None or obj.get('Tumblify_ViewportMode')!=mode)) or (mode=='MESH' and mod is not None):
                    _configure_instance_display(obj,obj,bpy.context.scene)

def _solid_state_restore():
    for obj in list(bpy.data.objects):
        if _is_live_object(obj) and _SOLID_COLOR_PREV in obj:
            obj.color=obj[_SOLID_COLOR_PREV];del obj[_SOLID_COLOR_PREV]
            if not any(obj.get(key,False) for key in (FROZEN_PROP,RUNTIME_PROP,INSTANCE_RESULT_PROP)):
                _remove_display_modifier(obj)
                if 'Tumblify_ViewportMode' in obj:del obj['Tumblify_ViewportMode']
    _SOLID_COLORED.clear()
    for shading,original in _SOLID_VIEWS:
        try:
            if shading.color_type=='OBJECT':shading.color_type=original
        except (ReferenceError,RuntimeError):pass
    _SOLID_VIEWS.clear()


def _is_live_object(o):
    try:
        return o is not None and o.name in bpy.data.objects
    except ReferenceError:
        return False
    except Exception:
        return False


def _spawn_master_collection(scene, create=False):
    """Return the master result collection without mutating the scene on read.

    Tumblify collections are created lazily only when a simulation actually needs
    to write results. UI polling, load handlers and cleanup queries must keep a
    pristine/new scene untouched.
    """
    c=bpy.data.collections.get(SPAWN_COLLECTION)
    if c is None:
        if not create:
            return None
        c=bpy.data.collections.new(SPAWN_COLLECTION)
        scene.collection.children.link(c)
    elif c.name not in [x.name for x in scene.collection.children]:
        if create:
            try:
                scene.collection.children.link(c)
            except Exception:
                pass
    return c


def _spawn_session_collections(scene):
    out=[]; seen=set()
    def walk(c):
        for child in c.children:
            try: key=child.as_pointer()
            except Exception: key=id(child)
            if key in seen: continue
            seen.add(key)
            if bool(child.get('Tumblify_SpawnSession',False)): out.append(child)
            walk(child)
    walk(scene.collection)
    return out


def _spawned_objects(scene):
    # Read-only query: never create Tumblify collections just to inspect state.
    master=_spawn_master_collection(scene, create=False)
    out=[]; seen=set()
    if master is not None:
        for o in list(master.objects):
            if o.as_pointer() not in seen: out.append(o); seen.add(o.as_pointer())
    for c in _spawn_session_collections(scene):
        for o in list(c.objects):
            try: key=o.as_pointer()
            except Exception: continue
            if key not in seen: out.append(o); seen.add(key)
    return out


def spawned_collection(scene,new_run=False,label='Drop'):
    # This is a write path: creating the result hierarchy is intentional here.
    master=_spawn_master_collection(scene, create=True)
    if not new_run:
        last=str(scene.get('Tumblify_LastSpawnCollection',''))
        c=bpy.data.collections.get(last) if last else None
        return c if c is not None else master
    idx=1
    while bpy.data.collections.get(f'Tumblify_{label}_{idx:03d}') is not None: idx+=1
    c=bpy.data.collections.new(f'Tumblify_{label}_{idx:03d}')
    c['Tumblify_SpawnSession']=True; c['Tumblify_Mode']=label
    scene.collection.children.link(c)
    scene['Tumblify_LastSpawnCollection']=c.name
    return c


def _fast_collection(scene):
    c=bpy.data.collections.get(FAST_COLLECTION)
    if not c:
        c=bpy.data.collections.new(FAST_COLLECTION); scene.collection.children.link(c)
    return c


def _clear_fast_collection():
    c=bpy.data.collections.get(FAST_COLLECTION)
    if c:
        for o in list(c.objects):
            me=o.data if getattr(o,'type',None)=='MESH' else None
            for m in list(o.modifiers):
                if m.type=='NODES' and m.node_group:
                    ng=m.node_group
                    o.modifiers.remove(m)
                    if ng.users==0: bpy.data.node_groups.remove(ng)
            bpy.data.objects.remove(o,do_unlink=True)
            if me and me.users==0: bpy.data.meshes.remove(me)
        try: bpy.data.collections.remove(c)
        except Exception: pass


def _remove_display_modifier(obj):
    if obj is None or not hasattr(obj,'modifiers'): return
    mod=obj.modifiers.get(DISPLAY_MOD_NAME)
    if mod is None: return
    ng=mod.node_group
    try: obj.modifiers.remove(mod)
    except Exception: return
    if ng and ng.users==0:
        try: bpy.data.node_groups.remove(ng)
        except Exception: pass


def _restore_instance_display_type(obj):
    if not _is_live_object(obj): return
    try:
        if DISPLAY_PREV_TYPE_PROP in obj:
            prev=str(obj[DISPLAY_PREV_TYPE_PROP])
            if prev in {'BOUNDS','WIRE','SOLID','TEXTURED'}: obj.display_type=prev
            del obj[DISPLAY_PREV_TYPE_PROP]
    except Exception: pass


def _remember_instance_display_type(obj):
    if not _is_live_object(obj): return
    try:
        if DISPLAY_PREV_TYPE_PROP not in obj:
            obj[DISPLAY_PREV_TYPE_PROP]=str(obj.display_type)
    except Exception: pass


def _viewport_shape_group(mode):
    name='Tumblify_Viewport_'+mode
    ng=bpy.data.node_groups.get(name)
    if ng is not None and (mode!='POINT' or ng.get('Tumblify_PointSizeVersion')==2):return ng
    if ng is None:
        ng=bpy.data.node_groups.new(name,'GeometryNodeTree')
        ng.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
        ng.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    else:ng.nodes.clear()
    ng['Tumblify_Owned']=True
    if mode=='POINT':ng['Tumblify_PointSizeVersion']=2
    n=ng.nodes;l=ng.links;inp=n.new('NodeGroupInput');out=n.new('NodeGroupOutput')
    bounds=n.new('GeometryNodeBoundBox');l.new(inp.outputs['Geometry'],bounds.inputs['Geometry'])
    if mode=='BOX':l.new(bounds.outputs['Bounding Box'],out.inputs['Geometry'])
    else:
        add=n.new('ShaderNodeVectorMath');add.operation='ADD'
        mul=n.new('ShaderNodeVectorMath');mul.operation='SCALE';mul.inputs['Scale'].default_value=.5
        l.new(bounds.outputs['Min'],add.inputs[0]);l.new(bounds.outputs['Max'],add.inputs[1]);l.new(add.outputs['Vector'],mul.inputs[0])
        points=n.new('GeometryNodePoints');points.inputs['Count'].default_value=1
        l.new(mul.outputs['Vector'],points.inputs['Position'])
        size=n.new('ShaderNodeVectorMath');size.operation='SUBTRACT'
        l.new(bounds.outputs['Max'],size.inputs[0]);l.new(bounds.outputs['Min'],size.inputs[1])
        xyz=n.new('ShaderNodeSeparateXYZ');l.new(size.outputs['Vector'],xyz.inputs['Vector'])
        def math_node(operation,left,right):
            node=n.new('ShaderNodeMath');node.operation=operation
            l.new(left,node.inputs[0])
            if isinstance(right,(int,float)):node.inputs[1].default_value=right
            else:l.new(right,node.inputs[1])
            return node.outputs['Value']
        maximum=math_node('MAXIMUM',math_node('MAXIMUM',xyz.outputs['X'],xyz.outputs['Y']),xyz.outputs['Z'])
        # Flat meshes use their smallest nonzero extent rather than disappearing.
        extents=[]
        for axis in ('X','Y','Z'):
            zero=math_node('LESS_THAN',xyz.outputs[axis],1e-9)
            extents.append(math_node('ADD',xyz.outputs[axis],math_node('MULTIPLY',zero,maximum)))
        minimum=math_node('MINIMUM',math_node('MINIMUM',extents[0],extents[1]),extents[2])
        radius=math_node('MAXIMUM',math_node('MULTIPLY',minimum,.5),1e-6)
        l.new(radius,points.inputs['Radius']);l.new(points.outputs['Points'],out.inputs['Geometry'])
    return ng


def _configure_instance_display(obj,src,scene):
    if not _is_live_object(obj):return
    mode=scene.tumblify.instance_display
    obj['Tumblify_ViewportMode']=mode
    if obj.get(INSTANCE_RESULT_PROP):
        result=obj.modifiers.get(INSTANCE_RESULT_MOD_NAME)
        if result is None:return
        if mode=='MESH':
            _remove_display_modifier(obj);result.show_viewport=True
        else:
            helper=_instance_result_helper(obj) or src
            if helper is None:return
            _build_batch_proxy_nodes(scene,obj,helper,bpy.context.evaluated_depsgraph_get())
            result.show_viewport=False
            obj.modifiers[DISPLAY_MOD_NAME].show_render=False
        return
    mod=obj.modifiers.get(DISPLAY_MOD_NAME)
    if mode=='MESH':
        _remove_display_modifier(obj);return
    if mod is None:mod=obj.modifiers.new(name=DISPLAY_MOD_NAME,type='NODES')
    mod.node_group=_viewport_shape_group(mode);mod.show_viewport=True;mod.show_render=False
    obj.display_type='TEXTURED'


def _batch_visual_helper(scene,obj,src,depsgraph):
    helper=bpy.data.objects.get(obj.get('Tumblify_BatchMeshHelper',''))
    if helper is not None:return helper
    helper=src.copy();helper.modifiers.clear();helper.constraints.clear();helper.data=_evaluated_visual_mesh(src,depsgraph,f'Tumblify_Batch_{src.name}_Mesh')
    if helper.animation_data:helper.animation_data_clear()
    helper.parent=None;helper.matrix_world=Matrix.Identity(4)
    helper.name=f'Tumblify_Batch_{src.name}_Source'
    _fast_collection(scene).objects.link(helper)
    helper.hide_viewport=False;helper.hide_render=True;helper.hide_select=True
    helper['Tumblify_BatchHelper']=True;helper['Tumblify_Helper']=True
    helper.hide_set(True);obj['Tumblify_BatchMeshHelper']=helper.name
    return helper


def _build_batch_proxy_nodes(scene,obj,src,depsgraph,force_mesh=False):
    mode=scene.tumblify.instance_display
    mod=obj.modifiers.get(DISPLAY_MOD_NAME)
    if mod is None:mod=obj.modifiers.new(name=DISPLAY_MOD_NAME,type='NODES')
    old=mod.node_group
    if old and old.users==1:
        mod.node_group=None;bpy.data.node_groups.remove(old)
    ng=bpy.data.node_groups.new(f'Tumblify_InstanceDisplay_{src.name}','GeometryNodeTree');mod.node_group=ng
    ng['Tumblify_Owned']=True;mod.show_viewport=True;mod.show_render=True
    ng.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
    ng.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    n=ng.nodes;l=ng.links;inp=n.new('NodeGroupInput');out=n.new('NodeGroupOutput')
    rot=n.new('GeometryNodeInputNamedAttribute');rot.data_type='FLOAT_VECTOR';rot.inputs['Name'].default_value='b3_rot'
    scale=n.new('GeometryNodeInputNamedAttribute');scale.data_type='FLOAT_VECTOR';scale.inputs['Name'].default_value='b3_scale'
    euler=n.new('FunctionNodeEulerToRotation');l.new(rot.outputs['Attribute'],euler.inputs['Euler'])
    def instance(geometry):
        inst=n.new('GeometryNodeInstanceOnPoints');l.new(inp.outputs['Geometry'],inst.inputs['Points']);l.new(geometry,inst.inputs['Instance']);l.new(euler.outputs['Rotation'],inst.inputs['Rotation']);l.new(scale.outputs['Attribute'],inst.inputs['Scale']);return inst.outputs['Instances']
    # Combined results already have a permanent hidden visual snapshot; reuse it.
    helper=_instance_result_helper(obj) if obj.get(INSTANCE_RESULT_PROP) else _batch_visual_helper(scene,obj,src,depsgraph)
    info=n.new('GeometryNodeObjectInfo');info.inputs['Object'].default_value=helper;info.inputs['As Instance'].default_value=True
    mesh=instance(info.outputs['Geometry'])
    if mode=='MESH':l.new(mesh,out.inputs['Geometry']);return
    center,half=_local_bounds(src,depsgraph)
    if mode=='BOX':
        cube=n.new('GeometryNodeMeshCube');cube.inputs['Size'].default_value=tuple(max(.002,2*v) for v in half)
        transform=n.new('GeometryNodeTransform');transform.inputs['Translation'].default_value=tuple(center);l.new(cube.outputs['Mesh'],transform.inputs['Geometry']);viewport=instance(transform.outputs['Geometry'])
    else:
        points=n.new('GeometryNodeMeshToPoints');points.mode='VERTICES';l.new(inp.outputs['Geometry'],points.inputs['Mesh'])
        xyz=n.new('ShaderNodeSeparateXYZ');radius=n.new('ShaderNodeMath');radius.operation='MULTIPLY';radius.inputs[1].default_value=min((abs(float(v)) for v in half if abs(float(v))>1e-9),default=1e-6)
        l.new(scale.outputs['Attribute'],xyz.inputs['Vector']);l.new(xyz.outputs['X'],radius.inputs[0]);l.new(radius.outputs['Value'],points.inputs['Radius']);viewport=points.outputs['Points']
    is_viewport=n.new('GeometryNodeIsViewport');switch=n.new('GeometryNodeSwitch');switch.input_type='GEOMETRY'
    l.new(is_viewport.outputs['Is Viewport'],switch.inputs['Switch']);l.new(mesh,switch.inputs['False']);l.new(viewport,switch.inputs['True']);l.new(switch.outputs['Output'],out.inputs['Geometry'])


def _display_proxy_collection(scene):
    c=bpy.data.collections.get(DISPLAY_PROXY_COLLECTION)
    if c is None:
        c=bpy.data.collections.new(DISPLAY_PROXY_COLLECTION); scene.collection.children.link(c)
    return c


def _clear_display_proxy_collection():
    c=bpy.data.collections.get(DISPLAY_PROXY_COLLECTION)
    if c is None: return
    for o in list(c.objects):
        me=getattr(o,'data',None)
        for m in list(getattr(o,'modifiers',[])):
            if m.type=='NODES' and m.node_group:
                ng=m.node_group
                try: o.modifiers.remove(m)
                except Exception: pass
                if ng and ng.users==0:
                    try: bpy.data.node_groups.remove(ng)
                    except Exception: pass
        try: bpy.data.objects.remove(o,do_unlink=True)
        except Exception: pass
        if me is not None and getattr(me,'users',1)==0:
            try: bpy.data.meshes.remove(me)
            except Exception: pass
    try: bpy.data.collections.remove(c)
    except Exception: pass


def _regular_display_proxy_needed(st):return False


def _sync_regular_display_proxy(scene):return None


def _pile_objects(scene):
    return [o for o in scene.objects if not o.get('Tumblify_Helper') and not o.get('Tumblify_BatchHelper') and o.get(INSTANCE_HELPER_PROP) is not True and (o.get(FROZEN_PROP) or o.get(RUNTIME_PROP) or o.get(INSTANCE_RESULT_PROP) or o.get('Tumblify_EmitterBake'))]


def _apply_global_instance_display(scene):
    depsgraph=bpy.context.evaluated_depsgraph_get()
    for obj in _pile_objects(scene):
        if obj.get('Tumblify_BatchPreview'):continue
        _configure_instance_display(obj,bpy.data.objects.get(str(obj.get('Tumblify_SourceName',''))) or obj,scene)
    fc=bpy.data.collections.get(FAST_COLLECTION)
    if fc:
        for obj in list(fc.objects):
            if not obj.get('Tumblify_BatchPreview'):continue
            src=bpy.data.objects.get(str(obj.get('Tumblify_SourceName','')))
            if src is not None:_build_batch_proxy_nodes(scene,obj,src,depsgraph)


def _new_fast_proxy(scene,src,depsgraph,force_mesh=False):
    me=bpy.data.meshes.new(f"Tumblify_Fast_{src.name}_Points")
    obj=bpy.data.objects.new(f"Tumblify_Fast_{src.name}",me)
    _fast_collection(scene).objects.link(obj)
    obj.hide_select=True;obj['Tumblify_BatchPreview']=True
    obj['Tumblify_SourceName']=src.name;obj['Tumblify_SnapshotSchema']=1
    _build_batch_proxy_nodes(scene,obj,src,depsgraph)
    _solid_state_color(obj,True)
    helper=bpy.data.objects.get(obj.get('Tumblify_BatchMeshHelper',''))
    if helper is not None:_solid_state_color(helper,True)
    return {'object':obj,'mesh':me,'indices':[],'count':-1}


def _rebatch_runtime_after_interactive(owner, scene, force_mesh=False):
    """Return a materialized live pile to the shared batched display path.

    Live Interactive needs real Blender objects for selection/G/R, so entering it
    materializes every body.  Keeping those objects after returning to Drop makes
    _sync() write thousands of individual Object transforms every timer tick.
    This helper removes only the temporary materialized runtime objects and rebuilds
    the per-source point/instance batches around the *existing* Box3D body handles.
    No native body is recreated and the current transforms stay in dynamic_cache.
    """
    if bool(getattr(owner, 'interactive_existing', False)):
        return 0
    handles=list(getattr(owner,'dynamic_handles',[]))
    if not handles:
        return 0
    cache=getattr(owner,'dynamic_cache',[])
    scales=getattr(owner,'dynamic_scales',[])
    names=getattr(owner,'dynamic_source_names',[])
    objs=getattr(owner,'dynamic_objs',[])
    if len(cache) < len(handles):
        return 0
    depsgraph=getattr(owner,'depsgraph',None) or bpy.context.evaluated_depsgraph_get()
    _clear_fast_collection()
    groups={}
    for src_name in dict.fromkeys(names):
        src=bpy.data.objects.get(str(src_name))
        if src is None or not _is_live_object(src):
            continue
        groups[src.name]=_new_fast_proxy(scene,src,depsgraph,force_mesh=force_mesh)
    if not groups:
        return 0
    removed=0
    preexisting=set(getattr(owner,'preexisting_dynamic',set())) | set(getattr(owner,'_promoted_static_objects',set()))
    # Interactive has already released all temporary kinematics before this runs,
    # therefore dynamic_cache is the authoritative pose for every native body.
    for i in range(len(handles)):
        src_name=str(names[i]) if i < len(names) else ''
        g=groups.get(src_name)
        if g is not None and not (i<len(objs) and _is_live_object(objs[i]) and objs[i] in preexisting):
            g['indices'].append(i)
        if i < len(objs):
            o=objs[i]
            if _is_live_object(o) and o not in preexisting:
                try:
                    bpy.data.objects.remove(o,do_unlink=True)
                    removed+=1
                except Exception:
                    continue
                objs[i]=None
    owner.session_objects=[o for o in getattr(owner,'session_objects',[]) if _is_live_object(o)]
    owner.fast_groups=groups
    owner.fast_mode=True
    for g in groups.values():
        _fast_proxy_write(g,cache,scales)
    return removed


def _fast_proxy_write(group,cache,scales,awake=None):
    obj=group.get('object')
    if _is_live_object(obj):
        _solid_state_color(obj,True)
        helper=bpy.data.objects.get(obj.get('Tumblify_BatchMeshHelper',''))
        if helper is not None:_solid_state_color(helper,True)
    # A Batch proxy mesh is deliberately deleted when instances are materialized.
    # Never touch an RNA wrapper after that datablock has been removed: Blender raises
    # ReferenceError instead of returning a normal invalid/None value.
    try:
        me=group.get('mesh')
        if me is None:
            return False
        me_name=me.name
        if bpy.data.meshes.get(me_name) is not me:
            return False
    except (ReferenceError, RuntimeError):
        return False
    inds=group.get('indices',[]); n=len(inds)
    if group.get('count')!=n:
        me.clear_geometry()
        if n:
            me.from_pydata([(cache[i][0],cache[i][1],cache[i][2]) for i in inds],[],[])
        me.update()
        for name in ('b3_rot','b3_scale'):
            old=me.attributes.get(name)
            if old: me.attributes.remove(old)
            me.attributes.new(name=name,type='FLOAT_VECTOR',domain='POINT')
        group['count']=n
    if not n: return True
    coords=[]; rots=[]; ss=[]
    for i in inds:
        v=cache[i]
        coords.extend((v[0],v[1],v[2]))
        try:
            e=Quaternion((v[6],v[3],v[4],v[5])).to_euler('XYZ')
            rots.extend((e.x,e.y,e.z))
        except Exception:
            rots.extend((0.0,0.0,0.0))
        sc=float(scales[i]); ss.extend((sc,sc,sc))
    me.vertices.foreach_set('co',coords)
    ar=me.attributes.get('b3_rot'); asc=me.attributes.get('b3_scale')
    if ar: ar.data.foreach_set('vector',rots)
    if asc: asc.data.foreach_set('vector',ss)
    me.update()
    return True


def _recover_saved_runtime(scene):
    """Promote saved live display data to frozen results, without native physics.

    Point meshes already contain the last displayed poses. Reuse their graph and
    evaluated source helper instead of materializing thousands of Blender objects.
    Only call after load/startup, when no runtime owns these datablocks.
    """
    recovered=0
    target=None
    existing_results=list(_spawned_objects(scene))
    owned_sources={str(o.get('Tumblify_SourceName','')) for o in existing_results
                   if o.get('Tumblify_ActiveInstanceSnapshot',False)}
    # Pass 11 already promoted some old batches but left the legacy originals
    # untouched. The retained batch-helper reference proves this object's origin.
    legacy_recoveries=[o for o in existing_results
                       if o.get(INSTANCE_RESULT_PROP,False)
                       and 'Tumblify_BatchMeshHelper' in o
                       and not o.get('Tumblify_SnapshotSchema',0)
                       and not o.get('Tumblify_LegacyArchiveApplied',False)]
    # Held ghosts/cursor guides have no native body and must not become results.
    for obj in list(scene.objects):
        if (obj.get('Tumblify_Helper',False) and
                (obj.name in {FAST_CURSOR_NAME,FAST_CURSOR_RAY_NAME}
                 or obj.name==FAST_GHOST_NAME or obj.name.startswith(FAST_GHOST_NAME+'_stack'))):
            data=obj.data
            bpy.data.objects.remove(obj,do_unlink=True)
            if isinstance(data,bpy.types.Mesh) and data.users==0:
                bpy.data.meshes.remove(data)
    fc=bpy.data.collections.get(FAST_COLLECTION)
    if fc is not None and fc.name in scene.collection.children:
        for obj in list(fc.objects):
            if not obj.get('Tumblify_BatchPreview',False) or obj.type!='MESH':
                continue
            count=len(obj.data.vertices)
            if not count:
                continue
            mod=obj.modifiers.get(DISPLAY_MOD_NAME)
            helper=bpy.data.objects.get(str(obj.get('Tumblify_BatchMeshHelper','')))
            if helper is None:
                src=bpy.data.objects.get(str(obj.get('Tumblify_SourceName','')))
                if src is None:
                    # Keep unresolved data intact rather than deleting a saved pile.
                    continue
                _build_batch_proxy_nodes(scene,obj,src,bpy.context.evaluated_depsgraph_get(),force_mesh=True)
                mod=obj.modifiers.get(DISPLAY_MOD_NAME)
                helper=bpy.data.objects.get(str(obj.get('Tumblify_BatchMeshHelper','')))
            if helper is None or mod is None or mod.node_group is None:
                continue
            if target is None:
                target=spawned_collection(scene,True,'Recovered')
            target.objects.link(obj)
            fc.objects.unlink(obj)
            helpers=_instance_source_collection(target,True)
            if helper.name not in helpers.objects:
                helpers.objects.link(helper)
            if helper.name in fc.objects:
                fc.objects.unlink(helper)
            helper['Tumblify_InstanceHelper']=True
            obj[INSTANCE_HELPER_PROP]=helper.name
            mod.name=INSTANCE_RESULT_MOD_NAME
            mod.show_viewport=True; mod.show_render=True
            obj[INSTANCE_RESULT_PROP]=True
            obj['Tumblify_InstanceCount']=count
            obj[FROZEN_PROP]=True; obj[RUNTIME_PROP]=True; obj[FIXED_PROP]=False
            obj.tumblify.role='NONE'
            obj.hide_select=False; obj.hide_viewport=False; obj.hide_render=False
            obj.hide_set(False)
            del obj['Tumblify_BatchPreview']
            if not obj.get('Tumblify_SnapshotSchema',0):
                legacy_recoveries.append(obj)
            recovered+=count
        if not fc.objects and not fc.children:
            bpy.data.collections.remove(fc)
    # Materialized selected bodies and Separated runs are already real objects.
    # Their saved Blender transforms are authoritative, including an active G/R.
    for obj in _spawned_objects(scene):
        if (obj.get(RUNTIME_PROP,False) and not obj.get(FROZEN_PROP,False)
                and not obj.get(FIXED_PROP,False) and not obj.get('Tumblify_EmitterBake',False)
                and not obj.get('Tumblify_Helper',False)):
            obj[FROZEN_PROP]=True
            obj.tumblify.role='NONE'
            recovered+=1
    # New saves explicitly identify hidden originals whose bodies were imported
    # into the live batch. Do not infer ownership from visibility in legacy files.
    unresolved=any(o.get('Tumblify_BatchPreview',False) and o.type=='MESH'
                   and len(o.data.vertices) for o in scene.objects)
    if legacy_recoveries and not unresolved:
        sources={str(o.get('Tumblify_SourceName','')) for o in legacy_recoveries
                 if o.get('Tumblify_SourceName','')}-owned_sources
        archive_legacy_combined_results(_spawned_objects(scene),sources)
        # Do not reapply Fix after a user explicitly Unfixes an archived result.
        for obj in legacy_recoveries:
            obj['Tumblify_LegacyArchiveApplied']=True
    if recovered and not unresolved:
        for obj in list(_spawned_objects(scene)):
            if not obj.get('Tumblify_ActiveInstanceSnapshot',False):
                continue
            _remove_instance_result_helper(obj)
            me=obj.data
            bpy.data.objects.remove(obj,do_unlink=True)
            if me.users==0:
                bpy.data.meshes.remove(me)
    return recovered


def _instance_source_collection(session_collection, create=False):
    if session_collection is None: return None
    for child in list(session_collection.children):
        if bool(child.get(INSTANCE_SOURCE_COLLECTION_PROP,False)):
            return child
    if not create: return None
    name=f"{session_collection.name}_Sources"
    c=bpy.data.collections.get(name)
    if c is None:
        c=bpy.data.collections.new(name)
    if c.name not in [x.name for x in session_collection.children]:
        try: session_collection.children.link(c)
        except Exception: pass
    c[INSTANCE_SOURCE_COLLECTION_PROP]=True
    return c


def _cleanup_instance_source_collection(session_collection):
    if session_collection is None: return
    # Clean legacy helpers that older builds linked directly into the result collection.
    for helper in list(session_collection.objects):
        if not bool(helper.get('Tumblify_InstanceHelper',False)): continue
        me=getattr(helper,'data',None)
        try: bpy.data.objects.remove(helper,do_unlink=True)
        except Exception: pass
        if me is not None and getattr(me,'users',1)==0:
            try: bpy.data.meshes.remove(me)
            except Exception: pass
    c=_instance_source_collection(session_collection,False)
    if c is None: return
    # New builds keep all service-only GN source helpers in this child collection.
    for helper in list(c.objects):
        me=getattr(helper,'data',None)
        try: bpy.data.objects.remove(helper,do_unlink=True)
        except Exception: pass
        if me is not None and getattr(me,'users',1)==0:
            try: bpy.data.meshes.remove(me)
            except Exception: pass
    try:
        if not c.objects and not c.children:
            bpy.data.collections.remove(c)
    except Exception: pass


def _force_real_mesh_display(obj):
    """Expose a real Blender mesh and clear addon display-proxy state.

    Preserve a remembered pre-addon display type when available. Legacy BOUNDS
    objects without a remembered value are treated as stale Drop display state
    and restored to TEXTURED.
    """
    if not _is_live_object(obj): return
    had_prev=False
    try: had_prev=(DISPLAY_PREV_TYPE_PROP in obj)
    except Exception: pass
    _remove_display_modifier(obj)
    _restore_instance_display_type(obj)
    try:
        if (not had_prev) and obj.display_type=='BOUNDS': obj.display_type='TEXTURED'
        obj.hide_viewport=False; obj.hide_render=False
    except Exception: pass


def _force_fast_result_mesh_display(owner):
    _apply_global_instance_display(bpy.context.scene)


def _instance_result_helper(obj):
    try:
        name=str(obj.get(INSTANCE_HELPER_PROP,''))
        return bpy.data.objects.get(name) if name else None
    except Exception:
        return None


def _remove_instance_result_helper(obj):
    helper=_instance_result_helper(obj)
    if helper is None: return
    me=getattr(helper,'data',None)
    try: bpy.data.objects.remove(helper,do_unlink=True)
    except Exception: pass
    if me is not None and getattr(me,'users',1)==0:
        try: bpy.data.meshes.remove(me)
        except Exception: pass


def _sync_visual_state_from_source(src,dst):
    """Make a spawned/preview object visually match its source.

    Object copies normally share mesh data, but object-linked material slots and
    viewport presentation can diverge during runtime. Re-apply those explicitly
    so Fast/Advanced bodies keep the source materials and shading presentation.
    """
    if src is None or dst is None: return
    try:
        dst.display_type='TEXTURED'
        dst.color=src.color
        dst.show_transparent=src.show_transparent
        dst.show_in_front=src.show_in_front
    except Exception:
        pass
    try:
        for i,ss in enumerate(src.material_slots):
            if i >= len(dst.material_slots): break
            ds=dst.material_slots[i]
            try: ds.link=ss.link
            except Exception: pass
            try: ds.material=ss.material
            except Exception: pass
    except Exception:
        pass


def _evaluated_visual_mesh(src,depsgraph,name):
    """Create an evaluated helper mesh while preserving Blender visual data.

    Combined results must retain material slots, per-face material indices, smooth
    shading and evaluated mesh data layers. Reconstructing the helper via
    from_pydata() loses all of that, so copy the evaluated mesh directly and only
    bake the source world scale into its local coordinates.
    """
    disabled_mods=[]
    try:
        if hasattr(src,'modifiers'):
            for mod_name in (GUIDE_MOD_NAME,DISPLAY_MOD_NAME):
                mod=src.modifiers.get(mod_name)
                if mod is not None and bool(mod.show_viewport):
                    disabled_mods.append(mod); mod.show_viewport=False
            if disabled_mods:
                try: depsgraph.update()
                except Exception: pass
        eo=src.evaluated_get(depsgraph)
        try:
            me=bpy.data.meshes.new_from_object(eo,preserve_all_data_layers=True,depsgraph=depsgraph)
        except TypeError:
            me=bpy.data.meshes.new_from_object(eo,preserve_all_data_layers=True)
        me.name=name
        # new_from_object() returns evaluated local geometry. The previous Combined
        # helper baked object scale, so preserve that behaviour while keeping all
        # mesh attributes/material assignments intact.
        try:
            _,_,scale=world_pose_scale(src)
            sm=Matrix.Diagonal((float(scale[0]),float(scale[1]),float(scale[2]),1.0))
            me.transform(sm)
        except Exception:
            pass
        # Some modifier/evaluation paths preserve polygon material indices but do
        # not populate the mesh material table reliably. Mirror evaluated slots.
        try:
            if len(me.materials)==0:
                for slot in eo.material_slots:
                    if slot.material is not None: me.materials.append(slot.material)
        except Exception:
            pass
        try: me.update()
        except Exception: pass
        return me
    finally:
        if disabled_mods:
            try:
                for mod in disabled_mods: mod.show_viewport=True
                depsgraph.update()
            except Exception: pass


def _build_instance_result_nodes(scene,obj,src,depsgraph):
    mod=obj.modifiers.get(INSTANCE_RESULT_MOD_NAME)
    if mod is None: mod=obj.modifiers.new(name=INSTANCE_RESULT_MOD_NAME,type='NODES')
    old=mod.node_group
    if old and old.users==1:
        try:
            mod.node_group=None; bpy.data.node_groups.remove(old)
        except Exception: pass
    ng=bpy.data.node_groups.new(f"Tumblify_ResultInstances_{src.name}",'GeometryNodeTree'); mod.node_group=ng
    mod.show_viewport=True; mod.show_render=True
    ng.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
    ng.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    nodes=ng.nodes; links=ng.links
    inp=nodes.new('NodeGroupInput'); out=nodes.new('NodeGroupOutput')
    scl=nodes.new('GeometryNodeInputNamedAttribute'); scl.data_type='FLOAT_VECTOR'; scl.inputs['Name'].default_value='b3_scale'
    rot=nodes.new('GeometryNodeInputNamedAttribute'); rot.data_type='FLOAT_VECTOR'; rot.inputs['Name'].default_value='b3_rot'
    inst=nodes.new('GeometryNodeInstanceOnPoints'); links.new(inp.outputs['Geometry'],inst.inputs['Points'])
    hme=_evaluated_visual_mesh(src,depsgraph,f"Tumblify_Result_{src.name}_Mesh")
    # Preserve object-level visual state as well as evaluated mesh data.  A newly
    # constructed Object only inherits mesh material slots; it does not retain
    # OBJECT-linked material overrides from the source.  That made Combined GN
    # results lose materials after Enter while Realize Instances restored them.
    helper=src.copy()
    helper.data=hme
    try:
        if helper.animation_data: helper.animation_data_clear()
    except Exception:
        pass
    helper.parent=None
    helper.matrix_world=Matrix.Identity(4)
    helper.name=f"Tumblify_Result_{src.name}_Source"
    _sync_visual_state_from_source(src,helper)
    # Object Info in Geometry Nodes requires the helper to be linked into the scene/depsgraph.
    # Keeping it only in bpy.data.objects produces the point cloud but no rendered instances.
    session_collection=(obj.users_collection[0] if getattr(obj,'users_collection',None) else None)
    target_collection=_instance_source_collection(session_collection,True) if session_collection is not None else None
    if target_collection is None:
        target_collection=scene.collection
    try: target_collection.objects.link(helper)
    except Exception:
        pass
    helper.hide_viewport=False; helper.hide_render=True; helper.hide_select=True; helper['Tumblify_InstanceHelper']=True
    try: helper.hide_set(True)
    except Exception: pass
    obj[INSTANCE_HELPER_PROP]=helper.name
    info=nodes.new('GeometryNodeObjectInfo'); info.inputs['Object'].default_value=helper
    try: info.inputs['As Instance'].default_value=True
    except Exception: pass
    links.new(info.outputs['Geometry'],inst.inputs['Instance'])
    try:
        e2r=nodes.new('FunctionNodeEulerToRotation'); links.new(rot.outputs['Attribute'],e2r.inputs['Euler']); links.new(e2r.outputs['Rotation'],inst.inputs['Rotation'])
    except Exception:
        links.new(rot.outputs['Attribute'],inst.inputs['Rotation'])
    links.new(scl.outputs['Attribute'],inst.inputs['Scale']); links.new(inst.outputs['Instances'],out.inputs['Geometry'])


def _create_instance_result(scene,collection,src,transforms,scales,depsgraph):
    if not transforms: return None
    # Keep the visible instances in exactly the same world-space positions, but place
    # the instancer object's origin at the centre of the generated pile instead of
    # leaving every Combined result anchored at world 0,0,0.
    xs=[float(v[0]) for v in transforms]; ys=[float(v[1]) for v in transforms]; zs=[float(v[2]) for v in transforms]
    centre=Vector(((min(xs)+max(xs))*0.5,(min(ys)+max(ys))*0.5,(min(zs)+max(zs))*0.5))
    me=bpy.data.meshes.new(f"Tumblify_Instances_{src.name}_Points")
    me.from_pydata([(float(v[0])-centre.x,float(v[1])-centre.y,float(v[2])-centre.z) for v in transforms],[],[]); me.update()
    ar=me.attributes.new(name='b3_rot',type='FLOAT_VECTOR',domain='POINT')
    asc=me.attributes.new(name='b3_scale',type='FLOAT_VECTOR',domain='POINT')
    rots=[]; ss=[]
    for v,sc in zip(transforms,scales):
        try: e=Quaternion((v[6],v[3],v[4],v[5])).to_euler('XYZ'); rots.extend((e.x,e.y,e.z))
        except Exception: rots.extend((0.0,0.0,0.0))
        f=float(sc); ss.extend((f,f,f))
    ar.data.foreach_set('vector',rots); asc.data.foreach_set('vector',ss); me.update()
    obj=bpy.data.objects.new(f"{collection.name}_{src.name}_Instances",me); collection.objects.link(obj)
    obj.location=centre
    obj[INSTANCE_RESULT_PROP]=True; obj['Tumblify_SourceName']=src.name; obj['Tumblify_InstanceCount']=len(transforms)
    obj[FROZEN_PROP]=True; obj[RUNTIME_PROP]=True; obj[FIXED_PROP]=False; obj.tumblify.role='NONE'; obj.show_in_front=False
    # Combined/Offline results are user-facing launch targets (Realize, Join,
    # Interactive Selected). Keep the instancer itself explicitly selectable.
    # Only its hidden GN source helper is locked from selection.
    try:
        obj.hide_select=False
        obj.hide_viewport=False
        obj.hide_render=False
        obj.hide_set(False)
        obj.display_type='TEXTURED'
    except Exception: pass
    _build_instance_result_nodes(scene,obj,src,depsgraph)
    return obj


def _update_instance_result_in_place(scene,obj,src,transforms,scales,depsgraph):
    """Update an existing Combined result without replacing its Object/GN datablocks.

    Interactive uses native Blender transforms internally. Those transforms create
    undo snapshots that can still reference the original Combined object's Geometry
    Nodes graph. Deleting/recreating that graph on Apply leaves old undo snapshots
    with dangling modifier dependencies and can crash Blender during depsgraph rebuild.
    Keep the user-facing Object, modifier, node group and hidden source helper alive;
    only rewrite the point-cloud mesh and instance attributes in place.
    """
    if not _is_live_object(obj) or getattr(obj,'type',None)!='MESH' or not transforms:
        return False
    me=getattr(obj,'data',None)
    if me is None:
        return False
    xs=[float(v[0]) for v in transforms]; ys=[float(v[1]) for v in transforms]; zs=[float(v[2]) for v in transforms]
    centre=Vector(((min(xs)+max(xs))*0.5,(min(ys)+max(ys))*0.5,(min(zs)+max(zs))*0.5))
    verts=[(float(v[0])-centre.x,float(v[1])-centre.y,float(v[2])-centre.z) for v in transforms]
    try:
        me.clear_geometry()
    except Exception:
        # Fallback: preserve the Object/modifier graph but swap only the mesh datablock.
        # Do not explicitly remove the old mesh here; Blender Undo may still reference it.
        new_me=bpy.data.meshes.new(f"{me.name}_Updated")
        obj.data=new_me
        me=new_me
    me.from_pydata(verts,[],[])
    try:
        for a in list(me.attributes):
            if a.name in {'b3_rot','b3_scale'}:
                me.attributes.remove(a)
    except Exception:
        pass
    ar=me.attributes.new(name='b3_rot',type='FLOAT_VECTOR',domain='POINT')
    asc=me.attributes.new(name='b3_scale',type='FLOAT_VECTOR',domain='POINT')
    rots=[]; ss=[]
    for v,sc in zip(transforms,scales):
        try:
            e=Quaternion((v[6],v[3],v[4],v[5])).to_euler('XYZ')
            rots.extend((e.x,e.y,e.z))
        except Exception:
            rots.extend((0.0,0.0,0.0))
        f=float(sc); ss.extend((f,f,f))
    ar.data.foreach_set('vector',rots); asc.data.foreach_set('vector',ss)
    me.update()
    obj.location=centre
    obj[INSTANCE_RESULT_PROP]=True
    obj['Tumblify_SourceName']=src.name
    obj['Tumblify_InstanceCount']=len(transforms)
    obj[FROZEN_PROP]=True; obj[RUNTIME_PROP]=True; obj[FIXED_PROP]=False
    try:
        obj.hide_select=False; obj.hide_viewport=False; obj.hide_render=False; obj.hide_set(False)
    except Exception:
        pass
    # Existing result should already have its GN modifier/helper. Rebuild only if a
    # damaged/legacy file is missing them; otherwise preserve IDs for safe Undo.
    mod=obj.modifiers.get(INSTANCE_RESULT_MOD_NAME)
    helper=_instance_result_helper(obj)
    if mod is None or mod.node_group is None or helper is None:
        _build_instance_result_nodes(scene,obj,src,depsgraph)
    return True


def _instance_result_entries(obj):
    if not _is_live_object(obj) or not bool(obj.get(INSTANCE_RESULT_PROP,False)) or getattr(obj,'type',None)!='MESH': return []
    me=obj.data; ar=me.attributes.get('b3_rot'); asc=me.attributes.get('b3_scale'); out=[]
    base_q=obj.matrix_world.to_quaternion(); base_m=obj.matrix_world
    for i,v in enumerate(me.vertices):
        p=base_m @ v.co
        try: rv=ar.data[i].vector; q=(base_q @ Euler((rv[0],rv[1],rv[2]),'XYZ').to_quaternion()).normalized()
        except Exception: q=base_q.copy()
        try: sv=asc.data[i].vector; sc=float((abs(sv[0])+abs(sv[1])+abs(sv[2]))/3.0)
        except Exception: sc=1.0
        out.append(([float(p.x),float(p.y),float(p.z),float(q.x),float(q.y),float(q.z),float(q.w)],sc))
    return out


def _frozen_instance_results(scene):
    return [o for o in scene.objects if bool(o.get(INSTANCE_RESULT_PROP,False)) and bool(o.get(FROZEN_PROP,False)) and not bool(o.get(FIXED_PROP,False)) and not o.hide_get()]


def _runtime_to_instance_results(owner,context):
    inds=sorted(set(getattr(owner,'session_indices',set())))
    if not inds:
        # A run stopped before its first spawn still owns empty batch previews.
        # Retire them so the next run cannot collect them as static meshes.
        _clear_fast_collection()
        owner.fast_groups.clear(); owner.fast_mode=False
        return 0
    groups={}
    for i in inds:
        if i>=len(owner.dynamic_source_names) or i>=len(owner.dynamic_cache): continue
        src_name=owner.dynamic_source_names[i]; src=bpy.data.objects.get(src_name)
        if src is None: continue
        g=groups.setdefault(src_name,{'src':src,'transforms':[],'scales':[]})
        g['transforms'].append(list(owner.dynamic_cache[i])); g['scales'].append(float(owner.dynamic_scales[i]) if i<len(owner.dynamic_scales) else 1.0)
    count=0
    for g in groups.values():
        o=_create_instance_result(context.scene,owner.collection,g['src'],g['transforms'],g['scales'],owner.depsgraph)
        if o is not None: count+=len(g['transforms'])
    # Remove materialized current-run objects; prior runs/promoted bodies are left untouched.
    for i in inds:
        if i<len(owner.dynamic_objs):
            o=owner.dynamic_objs[i]
            if _is_live_object(o):
                try: bpy.data.objects.remove(o,do_unlink=True)
                except Exception: pass
    try: _clear_fast_collection()
    except Exception: pass
    owner.fast_groups.clear(); owner.fast_mode=False
    return count


def _realize_instance_object(context,inst,collection=None):
    """Realize one Combined result without touching sibling instancers in the session."""
    if not _is_live_object(inst) or not bool(inst.get(INSTANCE_RESULT_PROP,False)):
        return []
    if collection is None:
        cols=list(getattr(inst,'users_collection',[]) or [])
        collection=cols[0] if cols else getattr(context,'collection',None)
    if collection is None:
        return []
    src_name=str(inst.get('Tumblify_SourceName','')); src=bpy.data.objects.get(src_name); helper=_instance_result_helper(inst)
    base_data=getattr(helper,'data',None) if helper is not None else None
    made=[]
    for pose,sc in _instance_result_entries(inst):
        q=Quaternion((pose[6],pose[3],pose[4],pose[5]))
        if src is not None:
            dup=src.copy()
            if dup.animation_data: dup.animation_data_clear()
            _,_,base_scale=world_pose_scale(src); scale_vec=Vector(base_scale)*float(sc)
        elif base_data is not None:
            dup=bpy.data.objects.new('Tumblify_Realized',base_data.copy()); scale_vec=Vector((sc,sc,sc))
        else:
            continue
        dup.parent=None; dup.tumblify.role='NONE'; dup.hide_select=False
        try:
            dup.hide_viewport=False; dup.hide_render=False; dup.hide_set(False); dup.display_type='TEXTURED'
        except Exception:
            pass
        collection.objects.link(dup); dup.matrix_world=Matrix.LocRotScale(Vector(pose[:3]),q,scale_vec)
        dup['Tumblify_SourceName']=src_name; dup['Tumblify_InstanceScale']=float(sc); dup[FROZEN_PROP]=True; dup[RUNTIME_PROP]=True; dup[FIXED_PROP]=False
        made.append(dup)
    _remove_instance_result_helper(inst)
    me=getattr(inst,'data',None)
    try: bpy.data.objects.remove(inst,do_unlink=True)
    except Exception: pass
    if me is not None and getattr(me,'users',1)==0:
        try: bpy.data.meshes.remove(me)
        except Exception: pass
    for obj in made:_configure_instance_display(obj,bpy.data.objects.get(str(obj.get('Tumblify_SourceName',''))) or obj,context.scene)
    return made


def _realize_instance_results(context,collection):
    instancers=[o for o in list(collection.objects) if _is_live_object(o) and bool(o.get(INSTANCE_RESULT_PROP,False))]
    made=[]
    for inst in instancers:
        made.extend(_realize_instance_object(context,inst,collection))
    _cleanup_instance_source_collection(collection)
    return made
