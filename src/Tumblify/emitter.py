# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Emitter geometry, guides, sampling and Fill helpers for Tumblify.

This module is intentionally runtime-light: it does not import addon.py and does
not own Blender modal/operator lifecycle.  UI callbacks and physics session
orchestration remain in addon.py.
"""

import bpy
import bmesh
import math
import random

from mathutils import Quaternion, Vector

from .bridge import World
from .constants import (
    BRUSH_NAME,
    GUIDE_NAME,
    GUIDE_MOD_NAME,
    GUIDE_GROUP_PREFIX,
    EMITTER_WIRE_PROP,
    EMITTER_SHOW_WIRE_PROP,
    EMITTER_ALL_EDGES_PROP,
)
from .collision import (
    world_pose_scale,
    _emitter_bound_box_without_guide,
    _emitter_world_bound_points,
    _long_axis,
    _cylinder_axis,
    _container_profile_contains,
)
from .display import _is_live_object


def _collection_emitter_objects(collection, scene=None):
    if collection is None:
        return []
    try:
        objects=list(collection.all_objects)
    except Exception:
        objects=list(collection.objects)
    out=[]
    for o in objects:
        if o is None or o.name in {BRUSH_NAME,GUIDE_NAME}: continue
        if scene is not None and o.name not in scene.objects: continue
        if o not in out: out.append(o)
    return out


def _configured_emitter_objects(scene):
    """Return custom emitter objects. Collection is exclusive and takes precedence."""
    st=scene.tumblify
    if getattr(st,'emitter_collection',None):
        return _collection_emitter_objects(st.emitter_collection,scene)
    o=getattr(st,'emitter_object',None)
    if o and o.name in scene.objects and o.name not in {BRUSH_NAME,GUIDE_NAME}:
        return [o]
    return []


def _restore_emitter_wire_object(o):
    if o is None: return
    try:
        if EMITTER_WIRE_PROP in o:
            prev=str(o[EMITTER_WIRE_PROP])
            if prev in {'BOUNDS','WIRE','SOLID','TEXTURED'}: o.display_type=prev
            del o[EMITTER_WIRE_PROP]
        if EMITTER_SHOW_WIRE_PROP in o:
            o.show_wire=bool(o[EMITTER_SHOW_WIRE_PROP]); del o[EMITTER_SHOW_WIRE_PROP]
        if EMITTER_ALL_EDGES_PROP in o:
            o.show_all_edges=bool(o[EMITTER_ALL_EDGES_PROP]); del o[EMITTER_ALL_EDGES_PROP]
    except Exception: pass


def _sync_emitter_wire(scene):
    """Apply optional Wire display to configured custom emitters.

    Wire display is an explicit user toggle and is OFF by default. It affects viewport
    presentation only; emitter collision/spawn behavior is unchanged.
    """
    if scene is None or not hasattr(scene,'tumblify'): return
    st=scene.tumblify
    desired_objects=[]
    if bool(getattr(st,'emitter_wire_display',False)):
        desired_objects=_configured_emitter_objects(scene)
        if not desired_objects:
            eo=getattr(st,'emitter_object',None)
            if eo and _is_live_object(eo): desired_objects=[eo]
    desired_names={o.name for o in desired_objects}
    for o in list(bpy.data.objects):
        if EMITTER_WIRE_PROP in o and o.name not in desired_names:
            _restore_emitter_wire_object(o)
    for o in desired_objects:
        try:
            if EMITTER_WIRE_PROP not in o:
                o[EMITTER_WIRE_PROP]=str(o.display_type)
                o[EMITTER_SHOW_WIRE_PROP]=bool(getattr(o,'show_wire',False))
                o[EMITTER_ALL_EDGES_PROP]=bool(getattr(o,'show_all_edges',False))
            o.display_type='WIRE'
            o.show_wire=True
            o.show_all_edges=True
        except Exception: pass


def _emitter_objects(scene):
    custom=_configured_emitter_objects(scene)
    if custom: return custom
    o=bpy.data.objects.get(BRUSH_NAME)
    if o and o.name in scene.objects: return [o]
    return []


def _remove_emitter_guide_nodes(o):
    if not _is_live_object(o): return
    try:
        mod=o.modifiers.get(GUIDE_MOD_NAME)
        if mod:
            ng=mod.node_group
            o.modifiers.remove(mod)
            if ng and ng.users==0: bpy.data.node_groups.remove(ng)
    except Exception:
        pass


def _ensure_emitter_guide_nodes(o):
    """Attach a fully procedural world-Z guide to the emitter itself.

    The node graph computes the emitter geometry center, transforms it to world
    space, builds a world-space vertical vector down to Z=0, then transforms
    that direction back into emitter-local space. No Python transform update is
    required, so the guide follows move/rotate/scale continuously.
    """
    if not _is_live_object(o) or not hasattr(o,'modifiers'):
        return False
    try:
        mod=o.modifiers.get(GUIDE_MOD_NAME)
        if mod and mod.node_group and int(mod.node_group.get('Tumblify_GuideVersion',0))>=2:
            return True
        if mod:
            old=mod.node_group
            o.modifiers.remove(mod)
            if old and old.users==0:
                try: bpy.data.node_groups.remove(old)
                except Exception: pass

        mod=o.modifiers.new(GUIDE_MOD_NAME,'NODES')
        ng=bpy.data.node_groups.new(GUIDE_GROUP_PREFIX+o.name,'GeometryNodeTree')
        ng['Tumblify_GuideVersion']=2
        ng.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
        ng.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
        nodes=ng.nodes; links=ng.links

        inp=nodes.new('NodeGroupInput'); inp.location=(-900,220)
        bbox=nodes.new('GeometryNodeBoundBox'); bbox.location=(-700,-40)
        add_center=nodes.new('ShaderNodeVectorMath'); add_center.operation='ADD'; add_center.location=(-500,-40)
        half_center=nodes.new('ShaderNodeVectorMath'); half_center.operation='MULTIPLY'; half_center.location=(-320,-40)
        half_center.inputs[1].default_value=(0.5,0.5,0.5)

        self_obj=nodes.new('GeometryNodeSelfObject'); self_obj.location=(-700,-300)
        obj_info=nodes.new('GeometryNodeObjectInfo'); obj_info.location=(-500,-300)
        try: obj_info.transform_space='ORIGINAL'
        except Exception: pass

        to_world=nodes.new('FunctionNodeTransformPoint'); to_world.location=(-100,-80)
        sep=nodes.new('ShaderNodeSeparateXYZ'); sep.location=(80,-80)
        neg_z=nodes.new('ShaderNodeMath'); neg_z.operation='MULTIPLY'; neg_z.location=(250,-80); neg_z.inputs[1].default_value=-1.0
        world_dir=nodes.new('ShaderNodeCombineXYZ'); world_dir.location=(430,-80)
        inv=nodes.new('FunctionNodeInvertMatrix'); inv.location=(-80,-300)
        to_local_dir=nodes.new('FunctionNodeTransformDirection'); to_local_dir.location=(620,-120)
        end_add=nodes.new('ShaderNodeVectorMath'); end_add.operation='ADD'; end_add.location=(820,-40)

        line=nodes.new('GeometryNodeCurvePrimitiveLine'); line.location=(1030,20); line.name='Tumblify Guide Line'
        join=nodes.new('GeometryNodeJoinGeometry'); join.location=(1240,180)
        out=nodes.new('NodeGroupOutput'); out.location=(1450,180)

        links.new(inp.outputs['Geometry'],bbox.inputs['Geometry'])
        links.new(bbox.outputs['Min'],add_center.inputs[0]); links.new(bbox.outputs['Max'],add_center.inputs[1])
        links.new(add_center.outputs['Vector'],half_center.inputs[0])
        links.new(self_obj.outputs['Self Object'],obj_info.inputs['Object'])
        links.new(half_center.outputs['Vector'],to_world.inputs['Vector'])
        links.new(obj_info.outputs['Transform'],to_world.inputs['Transform'])
        links.new(to_world.outputs['Vector'],sep.inputs['Vector'])
        links.new(sep.outputs['Z'],neg_z.inputs[0]); links.new(neg_z.outputs[0],world_dir.inputs['Z'])
        links.new(obj_info.outputs['Transform'],inv.inputs['Matrix'])
        links.new(world_dir.outputs['Vector'],to_local_dir.inputs['Direction'])
        links.new(inv.outputs['Matrix'],to_local_dir.inputs['Transform'])
        links.new(half_center.outputs['Vector'],end_add.inputs[0]); links.new(to_local_dir.outputs['Direction'],end_add.inputs[1])
        links.new(half_center.outputs['Vector'],line.inputs['Start']); links.new(end_add.outputs['Vector'],line.inputs['End'])
        links.new(inp.outputs['Geometry'],join.inputs['Geometry']); links.new(line.outputs['Curve'],join.inputs['Geometry'])
        links.new(join.outputs['Geometry'],out.inputs['Geometry'])
        mod.node_group=ng
        return True
    except Exception:
        try:
            mod=o.modifiers.get(GUIDE_MOD_NAME)
            if mod:
                ng=mod.node_group; o.modifiers.remove(mod)
                if ng and ng.users==0: bpy.data.node_groups.remove(ng)
        except Exception: pass
        return False


def _sync_emitter_guides(scene):
    if scene is None or not hasattr(scene,'tumblify'): return
    st=scene.tumblify
    # A collection emitter has no single meaningful centre. Attaching the normal
    # downward guide to one/all members creates a misleading extra pivot/ray in the
    # viewport. Collection emitters therefore use only their actual member geometry.
    collection_mode=bool(getattr(st,'emitter_collection',None))
    hide_guide=(getattr(st,'spawn_location','VOLUME')=='SURFACE' or bool(getattr(st,'fill_emitter',False)) or collection_mode)
    desired=[] if hide_guide else [o for o in _emitter_objects(scene) if o.type=='MESH']
    desired_names=set()
    for o in desired:
        if _is_live_object(o): desired_names.add(o.name)
    for o in list(bpy.data.objects):
        try:
            if o.modifiers.get(GUIDE_MOD_NAME) and o.name not in desired_names:
                _remove_emitter_guide_nodes(o)
        except Exception:
            pass
    for o in desired:
        _ensure_emitter_guide_nodes(o)


def _advanced_emitter_objects(scene):
    st=scene.tumblify
    if not bool(getattr(st,'advanced_emitter_ready',False)):
        return []
    # Advanced accepts both a custom emitter and the built-in fixed Tumblify_Brush.
    # _configured_emitter_objects() deliberately excludes BRUSH_NAME, so using it
    # here made Create Emitter immediately fall back to the onboarding UI even
    # though the emitter had been created successfully. Use the same resolver as
    # the Drop runtime so UI readiness, collider selection and spawning agree.
    objs=_emitter_objects(scene)
    return [o for o in objs if _is_live_object(o) and o.name in scene.objects]


def _advanced_emitter_bounds(scene):
    objs=_advanced_emitter_objects(scene)
    if not objs: return None
    pts=[]
    for o in objs:
        try: pts.extend(_emitter_world_bound_points(o))
        except Exception: pass
    if not pts: return None
    mn=Vector((min(p.x for p in pts),min(p.y for p in pts),min(p.z for p in pts)))
    mx=Vector((max(p.x for p in pts),max(p.y for p in pts),max(p.z for p in pts)))
    return mn,mx


def _scatter_surface_sampler(obj, group_name):
    """Build a weighted triangle sampler from a mesh vertex group.

    The returned entries are immutable for one simulation run.  Triangle probability is
    area * average vertex weight, so painted weights behave like a true surface-density mask.
    """
    if obj is None or obj.type!='MESH':
        raise RuntimeError('Surface emitter must be a Mesh object')
    me=obj.data
    me.calc_loop_triangles()
    group=None
    if group_name:
        group=obj.vertex_groups.get(group_name)
        if group is None:
            raise RuntimeError(f"Vertex group '{group_name}' was not found on emitter '{obj.name}'")
    weights=[1.0]*len(me.vertices)
    if group is not None:
        weights=[]
        for v in me.vertices:
            try: weights.append(max(0.0,min(1.0,float(group.weight(v.index)))))
            except RuntimeError: weights.append(0.0)
    entries=[]; total=0.0
    for tri in me.loop_triangles:
        ia,ib,ic=map(int,tri.vertices)
        a=Vector(me.vertices[ia].co); b=Vector(me.vertices[ib].co); c=Vector(me.vertices[ic].co)
        cross=(b-a).cross(c-a); area=0.5*cross.length
        if area<=1.0e-12: continue
        w=(weights[ia]+weights[ib]+weights[ic])/3.0
        score=area*w
        if score<=1.0e-12: continue
        n=cross.normalized()
        total+=score
        entries.append((total,a,b,c,n))
    if not entries:
        label=f"vertex group '{group_name}'" if group_name else 'mesh surface'
        raise RuntimeError(f'Surface emitter has no non-zero triangles in {label}')
    return entries,total


def _fill_point_inside(data,local_point):
    bvh=data.get('bvh')
    if bvh is None:
        return True
    mn=data['min']; mx=data['max']
    diag=max(1.0e-5,(mx-mn).length)
    # A slightly skewed ray avoids repeatedly hitting aligned triangle edges.
    direction=Vector((1.0,0.000173,0.000311)).normalized()
    eps=max(1.0e-6,diag*1.0e-6)
    origin=Vector(local_point)+direction*eps
    remaining=diag*3.0
    hits=0
    for _ in range(96):
        hit=bvh.ray_cast(origin,direction,remaining)
        loc=hit[0] if hit else None
        if loc is None:
            break
        step=(loc-origin).length+eps
        remaining-=step
        if remaining<=0.0:
            break
        hits+=1
        origin=loc+direction*eps
    return bool(hits & 1)


def _fill_destroy_container(world,data):
    """Destroy the current runtime containment proxy stored in *data*."""
    if not data:
        return
    raw=data.get('handle')
    handles=raw if isinstance(raw,(list,tuple)) else ([raw] if raw is not None else [])
    for h in list(handles):
        try: world.destroy(h)
        except Exception: pass
    data['handle']=None


def _fill_update_pose_from_object(data):
    """Refresh only the emitter body pose; collision vertices already include object scale."""
    if not data:
        return False
    o=data.get('obj')
    if not _is_live_object(o):
        return False
    p,q,_=world_pose_scale(o)
    rot=Quaternion((q[3],q[0],q[1],q[2]))
    old_p=Vector(data.get('origin',p)); old_q=data.get('rot',rot)
    moved=(old_p-Vector(p)).length_squared>1.0e-12
    try:
        moved=moved or abs(float(old_q.dot(rot)))<0.9999999
    except Exception:
        moved=True
    data['origin']=Vector(p); data['rot']=rot
    return moved


def _fill_make_bounds_kinematic(world,data,friction=0.7,margin=0.01):
    """Create one kinematic compound body representing the 5 open-top bounds walls."""
    p=data['origin']; q=data['rot']
    c=Vector(data.get('center',(0,0,0))); h=Vector(data.get('half',(0.5,0.5,0.5)))
    t=max(float(margin),0.005,min(h.x,h.y,h.z)*0.04)
    parts=[]
    for half,center in [
        ((h.x+t,h.y+t,t),(c.x,c.y,c.z-h.z-t*0.15)),
        ((t,h.y+t,h.z+t),(c.x-h.x-t*0.15,c.y,c.z)),
        ((t,h.y+t,h.z+t),(c.x+h.x+t*0.15,c.y,c.z)),
        ((h.x+t,t,h.z+t),(c.x,c.y-h.y-t*0.15,c.z)),
        ((h.x+t,t,h.z+t),(c.x,c.y+h.y+t*0.15,c.z)),
    ]:
        parts.append({'kind':'BOX','asset':-1,'pos':center,'rot':(0,0,0,1),'params':half})
    return world.add_compound_mixed(World.KINEMATIC,p,(q.x,q.y,q.z,q.w),parts,1.0,friction,0.0,0.0)


def _fill_detect_standard_shape(data):
    """Recognize dense standard emitter meshes so Interactive Fill does not fall back to a box.

    Recognition is deliberately conservative.  It is used only when the exact triangle-prism
    proxy would be too dense.  The tests operate in emitter-local collision space, where object
    scale is already baked into the vertices.
    """
    verts=[Vector(v) for v in data.get('verts',[])]
    if len(verts)<8:
        return None
    mn=Vector(data.get('min',(0,0,0))); mx=Vector(data.get('max',(0,0,0)))
    c=(mn+mx)*0.5; h=(mx-mn)*0.5
    if min(abs(h.x),abs(h.y),abs(h.z))<1.0e-6:
        return None
    norm=[]
    for v in verts:
        d=v-c
        norm.append(Vector((d.x/h.x,d.y/h.y,d.z/h.z)))
    n=float(len(norm))

    # Box / cuboid: practically every surface vertex has at least one normalized
    # coordinate on a +/-1 face.  Subdivided boxes are recognized as well.
    box_score=sum(1 for v in norm if abs(max(abs(v.x),abs(v.y),abs(v.z))-1.0)<0.035)/n
    if box_score>0.94:
        return ('BOX',None)

    # Sphere / ellipsoid: x^2+y^2+z^2 ~= 1 after normalization by local half extents.
    sphere_err=sum(abs(v.x*v.x+v.y*v.y+v.z*v.z-1.0) for v in norm)/n
    sphere_score=sum(1 for v in norm if abs(v.x*v.x+v.y*v.y+v.z*v.z-1.0)<0.10)/n
    if sphere_score>0.88 and sphere_err<0.12:
        return ('SPHERE',None)

    # Cylinder / elliptic cylinder.  Try all three principal local axes.  A closed
    # cylinder vertex belongs either to the side (radial ~= 1) or to an end cap
    # (axis ~= +/-1).  Pick the strongest axis only when the fit is unambiguous.
    best=None
    for axis in range(3):
        vals=[]
        for v in norm:
            a=(v.x,v.y,v.z)
            axial=a[axis]
            raxes=[j for j in range(3) if j!=axis]
            rr=(a[raxes[0]]*a[raxes[0]]+a[raxes[1]]*a[raxes[1]])**0.5
            vals.append(min(abs(rr-1.0),abs(abs(axial)-1.0)))
        score=sum(1 for e in vals if e<0.055)/n
        err=sum(vals)/n
        if best is None or (score,-err)>(best[0],-best[1]):
            best=(score,err,axis)
    if best and best[0]>0.90 and best[1]<0.075:
        return ('CYLINDER',best[2])
    return None


def _fill_prism_assets_from_triangles(world,triangles,thickness):
    """Turn a low-poly surface into thin convex prisms for one kinematic compound body."""
    assets=[]; half_t=max(0.0005,float(thickness)*0.5)
    for a,b,c in triangles:
        a=Vector(a); b=Vector(b); c=Vector(c)
        n=(b-a).cross(c-a)
        if n.length_squared<1.0e-14:
            continue
        n.normalize(); off=n*half_t
        pts=[tuple(a+off),tuple(b+off),tuple(c+off),tuple(a-off),tuple(b-off),tuple(c-off)]
        try:
            assets.append(world.create_hull_asset(pts,max_vertices=8))
        except Exception:
            continue
    return assets or None


def _fill_build_standard_kinematic_assets(world,data,margin=0.01,forced_kind=None,forced_axis=None):
    """Build a stable low-poly shell for recognized Box/Sphere/Cylinder emitters.

    This path is used mainly for dense standard primitives whose source mesh has too
    many triangles for the exact Interactive proxy.  Unlike the old fallback, sphere
    and cylinder emitters keep their characteristic shape instead of becoming a box.
    """
    detected=(str(forced_kind).upper(), forced_axis) if forced_kind else _fill_detect_standard_shape(data)
    if not detected:
        return (None,None)
    kind,axis=detected
    if kind=='CYLINDER' and axis is None:
        axis=_cylinder_axis((Vector(data['max'])-Vector(data['min']))*0.5)
    if kind=='CAPSULE' and axis is None:
        axis=_long_axis((Vector(data['max'])-Vector(data['min']))*0.5)
    mn=Vector(data['min']); mx=Vector(data['max']); c=(mn+mx)*0.5; h=(mx-mn)*0.5
    diag=max(1.0e-6,(mx-mn).length)
    thick=max(float(margin),diag*0.0025,0.0015)
    cache_key=(kind,int(axis) if axis is not None else -1,round(thick,6))
    if data.get('interactive_standard_key')==cache_key and data.get('interactive_standard_assets'):
        return (data['interactive_standard_assets'],kind)
    triangles=[]

    if kind=='BOX':
        x0,x1=c.x-h.x,c.x+h.x; y0,y1=c.y-h.y,c.y+h.y; z0,z1=c.z-h.z,c.z+h.z
        v=[Vector((x0,y0,z0)),Vector((x1,y0,z0)),Vector((x1,y1,z0)),Vector((x0,y1,z0)),
           Vector((x0,y0,z1)),Vector((x1,y0,z1)),Vector((x1,y1,z1)),Vector((x0,y1,z1))]
        faces=((0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
               (1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7))
        triangles=[(v[a],v[b],v[cidx]) for a,b,cidx in faces]

    elif kind=='SPHERE':
        # Low-poly ellipsoid: 12 longitude segments x 6 latitude bands = 120 tris.
        seg=12; bands=6
        rings=[]
        for j in range(1,bands):
            lat=-0.5*math.pi + math.pi*(j/float(bands))
            cl=math.cos(lat); sl=math.sin(lat)
            ring=[]
            for i in range(seg):
                ang=2.0*math.pi*(i/float(seg))
                ring.append(Vector((c.x+h.x*cl*math.cos(ang),c.y+h.y*cl*math.sin(ang),c.z+h.z*sl)))
            rings.append(ring)
        bot=Vector((c.x,c.y,c.z-h.z)); top=Vector((c.x,c.y,c.z+h.z))
        if rings:
            for i in range(seg):
                ni=(i+1)%seg
                triangles.append((bot,rings[0][ni],rings[0][i]))
                triangles.append((top,rings[-1][i],rings[-1][ni]))
            for r0,r1 in zip(rings[:-1],rings[1:]):
                for i in range(seg):
                    ni=(i+1)%seg
                    triangles.append((r0[i],r0[ni],r1[ni])); triangles.append((r0[i],r1[ni],r1[i]))

    elif kind=='CYLINDER':
        # Elliptic cylinder along the detected local principal axis.
        seg=16
        ax=int(axis)
        side=[j for j in range(3) if j!=ax]
        center=(c.x,c.y,c.z); half=(h.x,h.y,h.z)
        low=[]; high=[]
        for i in range(seg):
            ang=2.0*math.pi*(i/float(seg)); ca=math.cos(ang); sa=math.sin(ang)
            lo=list(center); hi=list(center)
            lo[ax]=center[ax]-half[ax]; hi[ax]=center[ax]+half[ax]
            lo[side[0]]=center[side[0]]+half[side[0]]*ca; hi[side[0]]=lo[side[0]]
            lo[side[1]]=center[side[1]]+half[side[1]]*sa; hi[side[1]]=lo[side[1]]
            low.append(Vector(lo)); high.append(Vector(hi))
        clo=list(center); chi=list(center); clo[ax]-=half[ax]; chi[ax]+=half[ax]
        clo=Vector(clo); chi=Vector(chi)
        for i in range(seg):
            ni=(i+1)%seg
            triangles.append((low[i],low[ni],high[ni])); triangles.append((low[i],high[ni],high[i]))
            triangles.append((clo,low[ni],low[i])); triangles.append((chi,high[i],high[ni]))

    elif kind=='CAPSULE':
        # Hollow capsule shell aligned to the assigned capsule axis.
        seg=16; ax=int(axis); side=[j for j in range(3) if j!=ax]
        center=(c.x,c.y,c.z); half=(h.x,h.y,h.z)
        radius=max(1.0e-5,min(half[side[0]],half[side[1]]))
        cyl_half=max(0.0,half[ax]-radius)
        rings=[]
        # Profile from lower pole to upper pole: hemispheres + optional cylinder.
        profile=[]
        hemi=4
        for j in range(hemi+1):
            a=-0.5*math.pi + (0.5*math.pi)*(j/float(hemi))
            profile.append((-cyl_half + radius*math.sin(a), radius*math.cos(a)))
        if cyl_half>1.0e-6:
            profile.append((cyl_half,radius))
        for j in range(1,hemi+1):
            a=(0.5*math.pi)*(j/float(hemi))
            profile.append((cyl_half + radius*math.sin(a), radius*math.cos(a)))
        for axial,rad in profile:
            ring=[]
            for i in range(seg):
                ang=2.0*math.pi*(i/float(seg)); vals=list(center)
                vals[ax]=center[ax]+axial
                vals[side[0]]=center[side[0]]+rad*math.cos(ang)
                vals[side[1]]=center[side[1]]+rad*math.sin(ang)
                ring.append(Vector(vals))
            rings.append(ring)
        for r0,r1 in zip(rings[:-1],rings[1:]):
            for i in range(seg):
                ni=(i+1)%seg
                triangles.append((r0[i],r0[ni],r1[ni])); triangles.append((r0[i],r1[ni],r1[i]))

    assets=_fill_prism_assets_from_triangles(world,triangles,thick)
    if assets:
        data['interactive_standard_key']=cache_key
        data['interactive_standard_assets']=assets
    return (assets,kind if assets else None)


def _fill_build_mesh_kinematic_assets(world,data,margin=0.01,max_triangles=384):
    """Build thin convex triangle-prisms for a true kinematic container shell.

    Each source triangle becomes a tiny 6-point convex prism.  All prisms are attached
    to ONE Box3D kinematic body, so interactive motion uses bp_set_target rather than
    repeatedly destroying/recreating static triangle meshes.  Very dense emitters fall
    back to an OBB container rather than sampling faces and accidentally creating holes.
    """
    cached=data.get('interactive_assets')
    if cached:
        return cached
    verts=[Vector(v) for v in data.get('verts',[])]
    inds=list(data.get('indices',[]))
    tris=[tuple(inds[i:i+3]) for i in range(0,len(inds)-2,3)]
    if not verts or not tris or len(tris)>int(max_triangles):
        return None
    diag=max(1.0e-6,(Vector(data['max'])-Vector(data['min'])).length)
    thick=max(float(margin),diag*0.0025,0.0015)
    assets=[]
    for ia,ib,ic in tris:
        try:
            a,b,c=verts[int(ia)],verts[int(ib)],verts[int(ic)]
        except Exception:
            continue
        n=(b-a).cross(c-a)
        if n.length_squared<1.0e-14:
            continue
        n.normalize(); off=n*(0.5*thick)
        pts=[tuple(a+off),tuple(b+off),tuple(c+off),tuple(a-off),tuple(b-off),tuple(c-off)]
        try: assets.append(world.create_hull_asset(pts,max_vertices=8))
        except Exception: continue
    if not assets:
        return None
    data['interactive_assets']=assets
    data['interactive_wall_thickness']=thick
    return assets


def _fill_candidate_clear(data,point,clearance):
    profile=data.get('cavity_profile')
    if profile:
        if not _container_profile_contains(profile,point,clearance):return False
    elif not _fill_point_inside(data,point):return False
    # Body clearance is measured against the actual assigned collision surfaces.
    surfaces=data.get('obstacles') or ([data] if not profile or data.get('assigned_shape')=='MESH' else [])
    for surface in surfaces:
        native=surface.get('native')
        if native and native.get('kind') in {'BOX','SPHERE','CAPSULE','CYLINDER'}:
            q=native['rot'];q=Quaternion((q[3],q[0],q[1],q[2]));local=q.inverted()@(point-Vector(native['pos']));params=native['params'];kind=native['kind']
            if kind=='BOX':distance=Vector(tuple(max(abs(local[i])-params[i],0.0) for i in range(3))).length
            elif kind=='SPHERE':distance=local.length-params[0]
            elif kind=='CAPSULE':distance=Vector((local.x,local.y-max(-params[0],min(params[0],local.y)),local.z)).length-params[1]
            else:
                radial=math.hypot(local.x,local.z)-params[1];vertical=abs(local.y)-params[0]
                distance=math.hypot(max(radial,0.0),max(vertical,0.0))+min(max(radial,vertical),0.0)
            if distance<clearance:return False
            continue
        if data.get('obstacles') and _fill_point_inside(surface,point):return False
        bvh=surface.get('bvh')
        nearest=bvh.find_nearest(point) if bvh is not None else None
        if nearest and nearest[0] is not None and nearest[3]<clearance:return False
    return True


def _fill_choose_point(fill_emitters,source_collision,src,scale_factor):
    """Spawn near the current fill front, starting at the bottom and rising gradually."""
    if not fill_emitters:
        return None
    pdata=source_collision.get(src.name,{})
    sh=Vector(pdata.get('half',(0.05,0.05,0.05))) * max(0.001,float(scale_factor))
    obj_extent=max(0.005,max(abs(sh.x),abs(sh.y),abs(sh.z)))
    # Spawn by the collision proxy's physical size, not by its centre only.  The old 0.35x
    # clearance could place a randomly rotated hull with its centre inside the cavity while
    # a large part of the proxy already intersected the wall.  Use a conservative radius
    # derived from the prepared collision bounds; Safety Margin is additive below.
    center=Vector(pdata.get('center',(0,0,0)))*max(.001,float(scale_factor))
    obj_radius=(Vector(tuple(abs(v) for v in center))+Vector(tuple(abs(v) for v in sh))).length
    if pdata.get('shape')=='SPHERE':obj_radius=max(abs(v) for v in sh)+center.length
    elif pdata.get('shape')=='HULL' and pdata.get('points'):obj_radius=max(Vector(v).length for v in pdata['points'])*max(.001,float(scale_factor))
    obj_volume=max(1.0e-9,8.0*abs(sh.x*sh.y*sh.z))
    # Prefer the least-filled emitter when a collection is used.
    def ratio(d):
        return float(d.get('used_volume',0.0))/max(1.0e-9,0.58*float(d.get('bounds_volume',1.0)))
    for data in sorted(fill_emitters,key=ratio):
        spawn_clearance=obj_radius+max(0.0,float(data.get('safety_margin',0.0)))
        mn=Vector(data['min']); mx=Vector(data['max']); height=max(1.0e-5,mx.z-mn.z)
        fill=max(0.0,min(0.96,ratio(data)))
        margin=spawn_clearance
        if height<=2.0*margin:continue
        usable=max(0.0,height-2.0*margin)
        level=mn.z+margin+usable*fill
        band=max(height*0.025,min(height*0.12,obj_extent*1.5))
        local=None
        # First try a narrow band around the fill front. As the emitter fills this
        # naturally moves upward, avoiding a permanent spawn point buried under the pile.
        for _ in range(72):
            z=max(mn.z+margin,min(mx.z-margin,random.uniform(level-band,level+band)))
            cand=Vector((random.uniform(mn.x,mx.x),random.uniform(mn.y,mx.y),z))
            prof=data.get('cavity_profile')
            inside=_fill_candidate_clear(data,cand,spawn_clearance)
            if inside:
                local=cand; break
        # Irregular/narrow emitters may have no cross-section at the exact level.
        # Relax downward/upward within the already-filled region before giving up.
        if local is None:
            top=max(mn.z+margin,min(mx.z-margin,level+band*2.0))
            for _ in range(96):
                cand=Vector((random.uniform(mn.x,mx.x),random.uniform(mn.y,mx.y),random.uniform(mn.z+margin,top)))
                prof=data.get('cavity_profile')
                inside=_fill_candidate_clear(data,cand,spawn_clearance)
                if inside:
                    local=cand; break
        if local is None:
            continue
        data['used_volume']=float(data.get('used_volume',0.0))+obj_volume
        return data['origin'] + data['rot'] @ local
    return None
