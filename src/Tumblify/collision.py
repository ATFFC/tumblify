# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Tumblify collision geometry and automatic collision fitting.

This module owns geometry extraction/fitting only. Blender operators, UI and runtime
world orchestration remain in addon.py. Immutable session geometry reuse is isolated in
mesh_cache.py; native worlds and their assets remain independent.
"""

import bpy, bmesh, math, json
from .bridge import CollisionGeometryError
from .mesh_cache import mesh_key, get_mesh, put_mesh
try:
    import numpy as _np
except Exception:
    _np=None
from mathutils import Quaternion, Vector, Matrix
from mathutils.bvhtree import BVHTree

from .constants import (
    GUIDE_MOD_NAME, DISPLAY_MOD_NAME, MANUAL_COMPOUND_PROP,
    MESH_COLLISION_TRI_BUDGET, MESH_COLLISION_DECIMATE_TRIGGER,
)

def world_pose_scale(obj):
    p,q,s=obj.matrix_world.decompose()
    q=q.normalized()
    return p,(q.x,q.y,q.z,q.w),(s.x,s.y,s.z)

def _evaluated_mesh_data(obj,depsgraph,need_triangles=False):
    """Return evaluated local vertices with object scale baked in.

    The emitter guide is a viewport-only Geometry Nodes modifier. It must never
    become part of a Box3D collision proxy, source hull, static mesh, or preview
    bounds. Temporarily disable only that modifier while evaluating physics
    geometry, then restore it immediately.
    """
    disabled_mods=[]
    try:
        if hasattr(obj,'modifiers'):
            for mod_name in (GUIDE_MOD_NAME, DISPLAY_MOD_NAME):
                mod=obj.modifiers.get(mod_name)
                if mod is not None and bool(mod.show_viewport):
                    disabled_mods.append(mod)
                    mod.show_viewport=False
            if disabled_mods:
                try: depsgraph.update()
                except Exception: pass
        eo=obj.evaluated_get(depsgraph)
        me=eo.to_mesh()
        try:
            _,_,scale=world_pose_scale(obj)
            verts=[(float(v.co.x*scale[0]),float(v.co.y*scale[1]),float(v.co.z*scale[2])) for v in me.vertices]
            indices=[]
            if need_triangles:
                me.calc_loop_triangles()
                indices=[idx for tri in me.loop_triangles for idx in tri.vertices]
            return verts,indices
        finally:
            eo.to_mesh_clear()
    finally:
        if disabled_mods:
            try:
                for mod in disabled_mods: mod.show_viewport=True
                depsgraph.update()
            except Exception: pass

def _emitter_bound_box_without_guide(obj, depsgraph=None):
    """Return emitter-local evaluated bounding-box corners with viewport helpers excluded.

    The downward emitter guide is Geometry Nodes geometry attached to the emitter object.
    Blender can therefore include that line in Object.bound_box / evaluated bounds.  Spawn
    sampling must never see it: it is a visual direction cue only.
    """
    if obj is None:
        return []
    if obj.type=='EMPTY':
        r=max(0.05,float(obj.empty_display_size))
        return [Vector((x*r,y*r,z*r)) for x in (-1,1) for y in (-1,1) for z in (-1,1)]
    dg=depsgraph
    if dg is None:
        try: dg=bpy.context.evaluated_depsgraph_get()
        except Exception: dg=None
    disabled=[]
    try:
        if hasattr(obj,'modifiers'):
            for mod_name in (GUIDE_MOD_NAME, DISPLAY_MOD_NAME):
                mod=obj.modifiers.get(mod_name)
                if mod is not None and bool(mod.show_viewport):
                    disabled.append(mod); mod.show_viewport=False
            if disabled and dg is not None:
                try: dg.update()
                except Exception: pass
        eo=obj.evaluated_get(dg) if dg is not None else obj
        try:
            return [Vector(c) for c in eo.bound_box]
        except Exception:
            return [Vector(c) for c in obj.bound_box]
    finally:
        if disabled:
            try:
                for mod in disabled: mod.show_viewport=True
                if dg is not None: dg.update()
            except Exception: pass

def _emitter_world_bound_points(obj, depsgraph=None):
    """World-space emitter bounds that deliberately ignore the viewport guide ray."""
    corners=_emitter_bound_box_without_guide(obj,depsgraph)
    if not corners:
        return []
    try:
        return [obj.matrix_world @ c for c in corners]
    except Exception:
        return []

def _evaluated_collision_mesh_data(obj,depsgraph,target_triangles=MESH_COLLISION_TRI_BUDGET):
    """Return a robust triangle surface for a static Box3D Mesh collider.

    Low/medium density meshes are passed through unchanged.  Very dense evaluated meshes are
    decimated on a temporary Blender object *only for physics*.  The render/source object and its
    modifiers are never edited.  This prevents scan/subdivision micro-triangles from producing a
    pathological contact manifold while retaining a concave surface collider.

    Returns (verts, indices, source_triangle_count, collision_triangle_count).
    """
    disabled_mods=[]
    eo=None; me=None; tmp_obj=None; tmp_me=None; dec_eval=None; dec_me=None
    try:
        if hasattr(obj,'modifiers'):
            for mod_name in (GUIDE_MOD_NAME,DISPLAY_MOD_NAME):
                mod=obj.modifiers.get(mod_name)
                if mod is not None and bool(mod.show_viewport):
                    disabled_mods.append(mod); mod.show_viewport=False
            if disabled_mods:
                try: depsgraph.update()
                except Exception: pass

        eo=obj.evaluated_get(depsgraph)
        me=eo.to_mesh()
        if me is None:raise CollisionGeometryError('No evaluated Mesh geometry')
        me.calc_loop_triangles()
        source_tris=len(me.loop_triangles)
        if len(me.vertices)<3 or source_tris<1:
            raise CollisionGeometryError('Evaluated Mesh has no triangle faces (empty, vertices-only or edges-only geometry)')
        _,_,scale=world_pose_scale(obj)
        try:cache_key=mesh_key(me,scale,obj.tumblify,target_triangles)
        except (AttributeError,TypeError,ValueError,RuntimeError,OverflowError):cache_key=None
        cached=get_mesh(cache_key)
        if cached is not None:return cached

        def collect(mesh):
            verts=[(float(v.co.x*scale[0]),float(v.co.y*scale[1]),float(v.co.z*scale[2])) for v in mesh.vertices]
            mesh.calc_loop_triangles()
            indices=[idx for tri in mesh.loop_triangles for idx in tri.vertices]
            return verts,indices,len(mesh.loop_triangles)

        # Static triangle meshes are one-sided zero-thickness surfaces in Box3D.
        # Open shells (e.g. a crate with no top) receive a thin collision-only
        # Solidify modifier, avoiding gaps between zero-thickness faces without
        # closing the intentional opening. Closed manifold meshes stay untouched.
        edges={}
        for tri in me.loop_triangles:
            ids=tri.vertices
            for i in range(3):
                edge=tuple(sorted((ids[i],ids[(i+1)%3])))
                edges[edge]=edges.get(edge,0)+1
        open_surface=any(v==1 for v in edges.values())
        settings=obj.tumblify
        mode=settings.mesh_quality
        target=max(1,round(source_tris*settings.mesh_detail_percent/100.0)) if mode=='CUSTOM' else max(500,min(3500,int(target_triangles)))
        needs_decimate=mode=='CUSTOM' and source_tris>target
        open_surface=open_surface and settings.mesh_thickness_mode!='NONE' 
        if not (open_surface or needs_decimate):
            verts,indices,out_tris=collect(me)
            return put_mesh(cache_key,verts,indices,source_tris,out_tris)

        try:
            tmp_me=bpy.data.meshes.new_from_object(eo,depsgraph=depsgraph,preserve_all_data_layers=False)
        except TypeError:
            tmp_me=bpy.data.meshes.new_from_object(eo,depsgraph=depsgraph)
        tmp_obj=bpy.data.objects.new("__Tumblify_CollisionSurface",tmp_me)
        bpy.context.scene.collection.objects.link(tmp_obj)
        if open_surface:
            ext=tmp_me.vertices
            if ext:
                mins=[min(v.co[i] for v in ext) for i in range(3)]
                maxs=[max(v.co[i] for v in ext) for i in range(3)]
                size=max(0.001,max(maxs[i]-mins[i] for i in range(3)))
                thickness=max(0.002,min(0.08,size*0.012))
                mod=tmp_obj.modifiers.new('Tumblify Collision Thickness','SOLIDIFY')
                mod.thickness=thickness if settings.mesh_thickness_mode=='AUTO' else settings.mesh_thickness
                mod.offset=0.0 if settings.mesh_thickness_mode=='AUTO' else -1.0
                mod.use_rim=True
        if needs_decimate:
            dec=tmp_obj.modifiers.new('Tumblify Collision Decimate','DECIMATE')
            dec.decimate_type='COLLAPSE'
            dec.ratio=max(0.001,min(1.0,float(target)/max(1.0,float(source_tris))))
            try: dec.use_collapse_triangulate=True
            except Exception: pass
        try: depsgraph.update()
        except Exception: pass
        dec_eval=tmp_obj.evaluated_get(depsgraph)
        dec_me=dec_eval.to_mesh()
        verts,indices,out_tris=collect(dec_me)
        if len(verts)<3 or len(indices)<3:
            verts,indices,out_tris=collect(me)
        return put_mesh(cache_key,verts,indices,source_tris,out_tris)
    finally:
        try:
            if dec_eval is not None and dec_me is not None: dec_eval.to_mesh_clear()
        except Exception: pass
        try:
            if eo is not None and me is not None: eo.to_mesh_clear()
        except Exception: pass
        try:
            if tmp_obj is not None and tmp_obj.name in bpy.data.objects: bpy.data.objects.remove(tmp_obj,do_unlink=True)
        except Exception: pass
        try:
            if tmp_me is not None and tmp_me.users==0: bpy.data.meshes.remove(tmp_me)
        except Exception: pass
        if disabled_mods:
            try:
                for mod in disabled_mods: mod.show_viewport=True
                depsgraph.update()
            except Exception: pass

def _local_bounds(obj,depsgraph):
    """Local AABB with object scale baked in; avoid evaluated-mesh creation when no real modifiers exist."""
    verts=None
    try:
        real_mods=[m for m in obj.modifiers if m.show_viewport and m.name not in {GUIDE_MOD_NAME,DISPLAY_MOD_NAME}]
        if not real_mods and getattr(obj,'bound_box',None):
            _,_,scale=world_pose_scale(obj)
            verts=[(float(c[0])*scale[0],float(c[1])*scale[1],float(c[2])*scale[2]) for c in obj.bound_box]
    except Exception:
        verts=None
    if verts is None:
        verts,_=_evaluated_mesh_data(obj,depsgraph,False)
    if not verts:
        return Vector((0,0,0)),Vector((.001,.001,.001))
    mins=[min(p[a] for p in verts) for a in range(3)]
    maxs=[max(p[a] for p in verts) for a in range(3)]
    center=Vector(((mins[0]+maxs[0])*.5,(mins[1]+maxs[1])*.5,(mins[2]+maxs[2])*.5))
    half=Vector((max((maxs[0]-mins[0])*.5,.001),max((maxs[1]-mins[1])*.5,.001),max((maxs[2]-mins[2])*.5,.001)))
    return center,half

def _long_axis(half):
    # 0=X, 1=Y, 2=Z. Long axis is appropriate for elongated capsule proxies.
    vals=(float(half.x),float(half.y),float(half.z))
    return max(range(3), key=lambda i:(vals[i], i))

def _cylinder_axis(half):
    """Choose the cylinder axis whose perpendicular bbox dimensions are most circular.

    This handles both long/thin cylinders (e.g. bottles, pipes) and short/wide cylinders
    (e.g. tyres, coins): in either case the two radial bbox dimensions should be the closest pair.
    """
    vals=(max(float(half.x),1.0e-9),max(float(half.y),1.0e-9),max(float(half.z),1.0e-9))
    def score(axis):
        radial=[vals[i] for i in range(3) if i!=axis]
        circularity=abs(radial[0]-radial[1])/max(radial[0],radial[1])
        # Deterministic tie-break: prefer the axis whose radial fit occupies more of its bbox.
        fill=min(radial)/max(radial)
        return (circularity,-fill,axis)
    return min(range(3),key=score)

def _axis_radius_half(half,axis,fit_inside=True):
    vals=(float(half.x),float(half.y),float(half.z))
    radial=[vals[i] for i in range(3) if i!=axis]
    # Use the smaller radial half-extent to keep the round proxy inside the evaluated AABB.
    radius=min(radial) if fit_inside else max(radial)
    return max(radius,0.001), vals[axis]

def _axis_map_point(v,axis):
    # Geometry helpers are authored along local Z. Reorient them to X/Y/Z without touching body rotation.
    x,y,z=v
    if axis==0: return (z,x,y)   # Z -> X
    if axis==1: return (x,z,y)   # Z -> Y
    return (x,y,z)

def _dedupe_points(points, digits=6):
    seen=set(); out=[]
    for pt in points:
        k=(round(float(pt[0]),digits),round(float(pt[1]),digits),round(float(pt[2]),digits))
        if k not in seen:
            seen.add(k); out.append((float(pt[0]),float(pt[1]),float(pt[2])))
    return out

def _prefilter_hull_input(points,budget=8000):
    """Linear-time safety reduction for extremely dense render meshes.

    Keeps the point farthest from the bounds center in each 3D grid cell, then adds axis extrema.
    A clean Blender convex hull is still built afterwards. This prevents a million-vertex render
    mesh from blocking the interactive start just to discover an outer shell with far fewer points.
    """
    n=len(points)
    if n<=budget:
        return points
    mins=[min(p[a] for p in points) for a in range(3)]
    maxs=[max(p[a] for p in points) for a in range(3)]
    ext=[max(maxs[a]-mins[a],1.0e-12) for a in range(3)]
    center=[0.5*(mins[a]+maxs[a]) for a in range(3)]
    # ~cube-root(budget) bins per axis => at most roughly budget occupied cells.
    cells=max(4,int(round(budget ** (1.0/3.0))))
    chosen={}
    for pt in points:
        ix=min(cells-1,max(0,int((pt[0]-mins[0])/ext[0]*cells)))
        iy=min(cells-1,max(0,int((pt[1]-mins[1])/ext[1]*cells)))
        iz=min(cells-1,max(0,int((pt[2]-mins[2])/ext[2]*cells)))
        key=(ix,iy,iz)
        dx=pt[0]-center[0]; dy=pt[1]-center[1]; dz=pt[2]-center[2]
        d2=dx*dx+dy*dy+dz*dz
        old=chosen.get(key)
        if old is None or d2>old[0]:
            chosen[key]=(d2,pt)
    out=[v[1] for v in chosen.values()]
    # Preserve global extrema explicitly.
    for axis in range(3):
        out.append(min(points,key=lambda p:p[axis]))
        out.append(max(points,key=lambda p:p[axis]))
    return _dedupe_points(out,6)

def _convex_hull_from_points(points):
    """Build a clean Blender convex hull from POINTS ONLY.

    No source faces/edges are imported into the BMesh. This is important for concave/high-poly
    source meshes: every face in the result is therefore generated by Blender's convex-hull op.
    Returns compact (vertices, faces) using only vertices referenced by hull faces.
    """
    pts=_dedupe_points(points,6)
    if len(pts)<4:
        return pts,[]
    bm=bmesh.new()
    try:
        for p in pts:
            bm.verts.new(p)
        bm.verts.ensure_lookup_table()
        try:
            bmesh.ops.convex_hull(bm,input=list(bm.verts),use_existing_faces=False)
        except Exception:
            return pts,[]
        bm.normal_update()
        faces_src=[f for f in bm.faces if f.is_valid and len(f.verts)>=3]
        if not faces_src:
            return pts,[]
        used=[]; used_set=set()
        for f in faces_src:
            for v in f.verts:
                if v not in used_set:
                    used_set.add(v); used.append(v)
        index={v:i for i,v in enumerate(used)}
        verts=[(float(v.co.x),float(v.co.y),float(v.co.z)) for v in used]
        faces=[tuple(index[v] for v in f.verts) for f in faces_src]
        return verts,faces
    finally:
        bm.free()

def _farthest_point_reduce(points,target):
    """Deterministic farthest-point reduction of an already convex point set.

    This keeps points distributed over the whole shell instead of selecting by mesh order.
    The reduced points are *not* used as faces directly; Blender Convex Hull is rebuilt from them.
    """
    n=len(points)
    target=max(4,min(int(target),n))
    if n<=target:
        return list(points)
    pts=[Vector(p) for p in points]
    # Seed with all six axis extrema when possible, then fill using farthest-point sampling.
    seeds=[]
    for axis in range(3):
        seeds.append(min(range(n),key=lambda i: pts[i][axis]))
        seeds.append(max(range(n),key=lambda i: pts[i][axis]))
    selected=[]; selected_set=set()
    for i in seeds:
        if i not in selected_set:
            selected.append(i); selected_set.add(i)
            if len(selected)>=target: break
    # Squared distance to nearest selected sample.
    nearest=[float('inf')]*n
    for si in selected:
        sp=pts[si]
        for i,p in enumerate(pts):
            d=(p-sp).length_squared
            if d<nearest[i]: nearest[i]=d
    while len(selected)<target:
        best=max((i for i in range(n) if i not in selected_set),key=lambda i: nearest[i])
        selected.append(best); selected_set.add(best)
        bp=pts[best]
        for i,p in enumerate(pts):
            if i in selected_set: continue
            d=(p-bp).length_squared
            if d<nearest[i]: nearest[i]=d
    return [points[i] for i in selected]

def _evaluated_convex_geometry(obj,depsgraph,simplify=0.65):
    """Clean collision hull based on Blender's standard convex-hull operation.

    1) Read evaluated vertex positions only (modifiers + object scale).
    2) Build Blender Convex Hull on a fresh point-only BMesh.
    3) If needed, reduce the already-correct hull vertex set and rebuild Convex Hull again.

    This avoids retaining any concave/source topology and keeps the debug proxy identical to the
    point set that is sent to Box3D.
    """
    raw,_=_evaluated_mesh_data(obj,depsgraph,False)
    raw=_dedupe_points(raw,6)
    if len(raw)<4:
        return raw,[]
    # High-poly render meshes can contain hundreds of thousands or millions of vertices.
    # A convex hull needs only the outer point cloud, so cap the initial BMesh workload before
    # calling Blender's standard convex-hull operation. The final shell is still generated by
    # bmesh.ops.convex_hull, not by the prefilter itself.
    raw=_prefilter_hull_input(raw,8000)

    verts,faces=_convex_hull_from_points(raw)
    if not faces:
        return verts,faces

    # Box3D v0.1 uses byte-sized hull indices. 240 leaves a little safety margin.
    # Slider controls the vertex budget rather than mutating hull topology with dissolve.
    # 0.0 -> up to 48 verts, 1.0 -> 12 verts. The native hull input is kept deliberately conservative.
    t=max(0.0,min(1.0,float(simplify)))
    target=max(12,min(48,int(round(48.0-(48.0-12.0)*t))))
    if len(verts)>target:
        reduced=_farthest_point_reduce(verts,target)
        verts2,faces2=_convex_hull_from_points(reduced)
        if faces2:
            verts,faces=verts2,faces2
    return verts,faces

def _poly_volume(vertices,faces):
    """Absolute signed tetrahedral volume for a consistently-oriented closed polygon mesh."""
    if not vertices or not faces: return 0.0
    vol=0.0
    try:
        for face in faces:
            if len(face)<3: continue
            a=Vector(vertices[face[0]])
            for i in range(1,len(face)-1):
                b=Vector(vertices[face[i]]); c=Vector(vertices[face[i+1]])
                vol += a.dot(b.cross(c))/6.0
    except Exception:
        return 0.0
    return abs(float(vol))

def _triangle_set_volume(vertices,triangles):
    """Signed-volume estimate for source triangles. Useful only as a coarse convexity test."""
    vol=0.0
    try:
        for tri in triangles:
            a=Vector(vertices[tri[0]]); b=Vector(vertices[tri[1]]); c=Vector(vertices[tri[2]])
            vol += a.dot(b.cross(c))/6.0
    except Exception:
        return 0.0
    return abs(float(vol))

def _compound_split_cluster(vertices,triangles):
    """Split one triangle cluster spatially along its widest centroid axis.

    This is deliberately deterministic and dependency-free. It is not VHACD: it is a fast
    spatial decomposition designed to create stable Box3D compound proxies from Blender meshes.
    """
    if len(triangles)<4: return None
    cents=[]
    for tri in triangles:
        a=vertices[tri[0]]; b=vertices[tri[1]]; c=vertices[tri[2]]
        cents.append(((a[0]+b[0]+c[0])/3.0,(a[1]+b[1]+c[1])/3.0,(a[2]+b[2]+c[2])/3.0))
    mins=[min(c[a] for c in cents) for a in range(3)]
    maxs=[max(c[a] for c in cents) for a in range(3)]
    ext=[maxs[a]-mins[a] for a in range(3)]
    axis=max(range(3),key=lambda a:ext[a])
    if ext[axis] <= 1.0e-8: return None
    order=sorted(range(len(triangles)),key=lambda i:cents[i][axis])
    mid=len(order)//2
    if mid<2 or len(order)-mid<2: return None
    left=[triangles[i] for i in order[:mid]]; right=[triangles[i] for i in order[mid:]]
    return left,right

def _compound_cluster_score(vertices,triangles):
    """Prefer splitting large spatial clusters first."""
    if not triangles: return 0.0
    used=set(i for tri in triangles for i in tri)
    if len(used)<4: return 0.0
    pts=[vertices[i] for i in used]
    mins=[min(p[a] for p in pts) for a in range(3)]; maxs=[max(p[a] for p in pts) for a in range(3)]
    ex=maxs[0]-mins[0]; ey=maxs[1]-mins[1]; ez=maxs[2]-mins[2]
    return max(ex*ey*ez,1.0e-12)*max(1,len(triangles))

def _compound_hull_for_triangles(vertices,triangles,simplify=0.65):
    used=[]; seen=set()
    for tri in triangles:
        for i in tri:
            if i not in seen:
                seen.add(i); used.append(vertices[i])
    pts=_dedupe_points(used,6)
    if len(pts)<4: return None
    pts=_prefilter_hull_input(pts,2500)
    hv,hf=_convex_hull_from_points(pts)
    if not hf: return None
    # Compound pieces should stay smaller than standalone hulls: many low-complexity shapes are
    # considerably faster than many 48-vertex shapes. Reuse Convex Simplify as the detail control.
    t=max(0.0,min(1.0,float(simplify)))
    target=max(10,min(32,int(round(32.0-(32.0-10.0)*t))))
    if len(hv)>target:
        reduced=_farthest_point_reduce(hv,target)
        hv2,hf2=_convex_hull_from_points(reduced)
        if hf2: hv,hf=hv2,hf2
    return {'points':hv,'faces':hf,'max_vertices':min(32,max(8,len(hv)))}

def _evaluated_compound_geometry(obj,depsgraph,simplify=0.65,max_hulls=8):
    """Approximate a concave mesh by several convex hulls attached to one rigid body.

    The first iteration uses deterministic recursive spatial decomposition. It is intentionally
    lightweight and requires no external VHACD dependency. Convex meshes automatically collapse
    to a single part when their mesh volume closely matches their global convex hull volume.
    """
    verts,indices=_evaluated_mesh_data(obj,depsgraph,True)
    if len(verts)<4 or len(indices)<3:
        pts,faces=_evaluated_convex_geometry(obj,depsgraph,simplify)
        return [{'points':pts,'faces':faces,'max_vertices':min(32,max(8,len(pts)))}] if faces else []
    triangles=[tuple(indices[i:i+3]) for i in range(0,len(indices)-2,3)]
    if not triangles: return []
    max_hulls=max(2,min(32,int(max_hulls)))

    # Avoid needlessly splitting meshes that are already essentially convex.
    global_hull=_compound_hull_for_triangles(verts,triangles,simplify)
    if global_hull:
        hv=_poly_volume(global_hull['points'],global_hull['faces'])
        mv=_triangle_set_volume(verts,triangles)
        if hv>1.0e-10 and mv>1.0e-10 and (hv-mv)/hv <= 0.035:
            return [global_hull]

    clusters=[triangles]
    while len(clusters)<max_hulls:
        candidates=[(_compound_cluster_score(verts,c),i) for i,c in enumerate(clusters)]
        candidates=[x for x in candidates if len(clusters[x[1]])>=8 and x[0]>0.0]
        if not candidates: break
        _,idx=max(candidates,key=lambda x:x[0])
        split=_compound_split_cluster(verts,clusters[idx])
        if not split: break
        a,b=split
        clusters[idx]=a; clusters.append(b)

    parts=[]
    for cluster in clusters:
        part=_compound_hull_for_triangles(verts,cluster,simplify)
        if part: parts.append(part)
    if not parts and global_hull: parts=[global_hull]
    return parts

def _auto_pca_frame(points):
    """Return a stable local PCA frame as (origin, basis vectors, local coordinates).

    The basis is used only while building the collision proxy; Box3D still receives ordinary
    local-space convex points, so no native API change is required.
    """
    pts=[Vector(p) for p in points]
    if not pts:
        return Vector((0,0,0)), (Vector((1,0,0)),Vector((0,1,0)),Vector((0,0,1))), []
    origin=sum(pts,Vector((0,0,0)))/len(pts)
    basis=(Vector((1,0,0)),Vector((0,1,0)),Vector((0,0,1)))
    if _np is not None and len(pts)>=4:
        try:
            a=_np.asarray([[p.x-origin.x,p.y-origin.y,p.z-origin.z] for p in pts],dtype=_np.float64)
            cov=(a.T@a)/max(1,len(pts))
            vals,vecs=_np.linalg.eigh(cov)
            order=_np.argsort(vals)[::-1]
            m=vecs[:,order]
            # Keep a right-handed frame to avoid mirrored proxy geometry.
            if _np.linalg.det(m)<0.0: m[:,2]*=-1.0
            basis=tuple(Vector((float(m[0,i]),float(m[1,i]),float(m[2,i]))) for i in range(3))
        except Exception:
            pass
    local=[Vector(((p-origin).dot(basis[0]),(p-origin).dot(basis[1]),(p-origin).dot(basis[2]))) for p in pts]
    return origin,basis,local

def _auto_from_local(v,origin,basis):
    return origin + basis[0]*v[0] + basis[1]*v[1] + basis[2]*v[2]

def _auto_primitive_part(vertices,triangles,simplify=0.65,fit_tolerance=0.025):
    """Fit an oriented primitive only when it genuinely follows the source cluster.

    The previous implementation selected primitives mainly from bounding-box proportions. That
    created large volumes of empty collision space. v0.13.4 evaluates PCA-oriented Box/Sphere/
    Capsule candidates against both convex-hull volume and surface gap. If none is sufficiently
    close, the part stays a reduced local Convex Hull.
    """
    used=sorted({i for tri in triangles for i in tri})
    if len(used)<4: return None
    pts=[Vector(vertices[i]) for i in used]
    hull=_compound_hull_for_triangles(vertices,triangles,simplify)
    if not hull: return None
    hv=max(1.0e-12,_poly_volume(hull['points'],hull['faces']))
    origin,basis,local=_auto_pca_frame(pts)
    mins=Vector((min(p.x for p in local),min(p.y for p in local),min(p.z for p in local)))
    maxs=Vector((max(p.x for p in local),max(p.y for p in local),max(p.z for p in local)))
    lc=(mins+maxs)*0.5; half=(maxs-mins)*0.5
    center=_auto_from_local(lc,origin,basis)
    diag=max(1.0e-8,(maxs-mins).length)
    tol=max(0.005,min(0.15,float(fit_tolerance)))
    candidates=[]

    # Oriented box candidate.
    boxv=max(1.0e-12,8.0*max(half.x,1e-8)*max(half.y,1e-8)*max(half.z,1e-8))
    gaps=[min(max(0.0,half.x-abs(p.x-lc.x)),max(0.0,half.y-abs(p.y-lc.y)),max(0.0,half.z-abs(p.z-lc.z)))/diag for p in local]
    box_gap=(sum(gaps)/len(gaps) if gaps else 1.0)
    box_over=max(0.0,boxv/hv-1.0)
    if box_over <= 0.10+tol*2.0 and box_gap <= tol*0.75:
        bx,by,bz=max(half.x,1e-5),max(half.y,1e-5),max(half.z,1e-5)
        pv,pf=_box_geometry(bx,by,bz)
        pv=[tuple(_auto_from_local(Vector(v)+lc,origin,basis)) for v in pv]
        nq=_basis_quaternion(basis)
        candidates.append((box_over+box_gap*3.0+0.010,{
            'points':pv,'faces':pf,'max_vertices':8,'fit':'Box',
            'native':{'kind':'BOX','pos':tuple(center),'rot':_quat_tuple_xyzw(nq),'params':(float(bx),float(by),float(bz))}
        }))

    # Sphere candidate; center is the PCA OBB center, radius encloses all source vertices.
    rel=[p-center for p in pts]; r=max((v.length for v in rel),default=0.0)
    if r>1.0e-8:
        sv=4.0*math.pi*r*r*r/3.0
        sg=sum(max(0.0,r-v.length) for v in rel)/max(1,len(rel))/diag
        so=max(0.0,sv/hv-1.0)
        if so <= 0.12+tol*2.0 and sg <= tol*0.70:
            pv,pf=_sphere_geometry(r,segments=10,rings=5)
            pv=[(x+center.x,y+center.y,z+center.z) for x,y,z in pv]
            candidates.append((so+sg*3.0,{
                'points':pv,'faces':pf,'max_vertices':32,'fit':'Sphere',
                'native':{'kind':'SPHERE','pos':tuple(center),'rot':(0.0,0.0,0.0,1.0),'params':(float(r),0.0,0.0)}
            }))

    # Capsule along the dominant PCA axis.
    axis=basis[0]
    ts=[(p-center).dot(axis) for p in pts]
    radial=[]
    for p,t in zip(pts,ts):
        radial.append(((p-center)-axis*t).length)
    r=max(radial,default=0.0)
    if r>1.0e-8:
        tmin,tmax=min(ts),max(ts); half_len=max(abs(tmin),abs(tmax)); seg=max(0.0,half_len-r)
        cv=math.pi*r*r*(2.0*seg)+(4.0/3.0)*math.pi*r*r*r
        cg=[]
        for p,t in zip(pts,ts):
            tc=max(-seg,min(seg,t))
            q=center+axis*tc
            cg.append(max(0.0,r-(p-q).length)/diag)
        cg=sum(cg)/max(1,len(cg)); co=max(0.0,cv/hv-1.0)
        # Only consider genuinely elongated regions.
        cross=max(half.y,half.z)/max(1.0e-8,min(half.y,half.z))
        elong=half.x/max(1.0e-8,(half.y+half.z)*0.5)
        if elong>=1.45 and cross<=1.55 and co<=0.14+tol*2.5 and cg<=tol*0.80:
            pv,pf=_capsule_geometry(seg,r,segments=10,hemi_rings=3)
            # Capsule generator is Z-aligned. Map local Z to PCA X, local X/Y to PCA Y/Z.
            out=[]
            for x,y,z in pv:
                q=center + basis[1]*x + basis[2]*y + basis[0]*z
                out.append(tuple(q))
            # Native Box3D capsule uses local Y as its canonical long axis. Twist around that
            # axis is irrelevant, so a minimal Y->PCA-axis rotation is exact and stable.
            nq=Vector((0,1,0)).rotation_difference(basis[0]).normalized()
            candidates.append((co+cg*3.0+0.005,{
                'points':out,'faces':pf,'max_vertices':32,'fit':'Capsule',
                'native':{'kind':'CAPSULE','pos':tuple(center),'rot':_quat_tuple_xyzw(nq),'params':(float(seg),float(r),0.0)}
            }))

    # Cylinder candidate. Test all PCA axes because both long cylinders and thin discs are common.
    # The fit is deliberately conservative: the two radial extents must be close and the enclosing
    # cylinder may not add much empty volume versus the local convex hull.
    local_axes=(basis[0],basis[1],basis[2])
    half_vals=(float(half.x),float(half.y),float(half.z))
    for ai in range(3):
        radial_ids=[j for j in range(3) if j!=ai]
        ra,rb=half_vals[radial_ids[0]],half_vals[radial_ids[1]]
        axial=half_vals[ai]
        if axial<=1.0e-8 or min(ra,rb)<=1.0e-8:
            continue
        roundness=max(ra,rb)/max(1.0e-8,min(ra,rb))
        if roundness>1.30:
            continue
        radius=max(ra,rb)
        cv=math.pi*radius*radius*(2.0*axial)
        co=max(0.0,cv/hv-1.0)
        gaps=[]
        for lp in local:
            vals=(lp.x-lc.x,lp.y-lc.y,lp.z-lc.z)
            av=abs(vals[ai])
            rv=math.sqrt(vals[radial_ids[0]]*vals[radial_ids[0]] + vals[radial_ids[1]]*vals[radial_ids[1]])
            gaps.append(min(max(0.0,axial-av),max(0.0,radius-rv))/diag)
        cg=sum(gaps)/max(1,len(gaps))
        if co<=0.14+tol*2.5 and cg<=tol*0.80:
            pv,pf=_cylinder_geometry(axial,radius,(0,0,0),12)
            # Generator is Y-aligned. Rotate its local Y onto the selected PCA axis.
            axis_vec=local_axes[ai]
            nq=Vector((0,1,0)).rotation_difference(axis_vec).normalized()
            rm=nq.to_matrix()
            out=[]
            for x,y,z in pv:
                qv=center + rm @ Vector((x,y,z))
                out.append(tuple(qv))
            candidates.append((co+cg*3.0+0.008,{
                'points':out,'faces':pf,'max_vertices':24,'fit':'Cylinder',
                'native':{'kind':'CYLINDER','pos':tuple(center),'rot':_quat_tuple_xyzw(nq),'params':(float(axial),float(radius),12.0)}
            }))

    if candidates:
        return min(candidates,key=lambda x:x[0])[1]
    hull=dict(hull); hull['fit']='Hull'; return hull

def _loose_triangle_components(indices):
    triangles=[tuple(indices[i:i+3]) for i in range(0,len(indices)-2,3)]
    if not triangles: return []
    vert_to_tris={}
    for ti,tri in enumerate(triangles):
        for v in tri: vert_to_tris.setdefault(v,[]).append(ti)
    seen=set(); out=[]
    for seed in range(len(triangles)):
        if seed in seen: continue
        stack=[seed]; seen.add(seed); comp=[]
        while stack:
            ti=stack.pop(); tri=triangles[ti]; comp.append(tri)
            for v in tri:
                for nb in vert_to_tris.get(v,()):
                    if nb not in seen: seen.add(nb); stack.append(nb)
        out.append(comp)
    return out

def _normal_patch_components(vertices,triangles,split_angle,max_parts):
    """Split one connected triangle surface into contiguous normal-direction patches.

    Unlike a simple sharp-edge split, the comparison is made against the seed/patch normal,
    so curvature accumulates. A smooth torus therefore still separates into several curved
    sectors even when every individual edge has a small dihedral angle. This is intentional:
    each sector can become its own convex hull without closing the torus hole.
    """
    if not triangles:
        return []
    tri_normals=[]
    for tri in triangles:
        try:
            a,b,c=(Vector(vertices[i]) for i in tri)
            n=(b-a).cross(c-a)
            if n.length_squared>1.0e-18: n.normalize()
            else: n=Vector((0.0,0.0,1.0))
        except Exception:
            n=Vector((0.0,0.0,1.0))
        tri_normals.append(n)
    edge_to_tris={}
    for ti,tri in enumerate(triangles):
        for a,b in ((tri[0],tri[1]),(tri[1],tri[2]),(tri[2],tri[0])):
            e=(a,b) if a<b else (b,a)
            edge_to_tris.setdefault(e,[]).append(ti)
    neighbors=[set() for _ in triangles]
    for linked in edge_to_tris.values():
        if len(linked)<2: continue
        for a in linked:
            for b in linked:
                if a!=b: neighbors[a].add(b)

    def build(angle):
        coslim=math.cos(max(math.radians(1.0),min(math.radians(120.0),float(angle))))
        unseen=set(range(len(triangles))); regions=[]
        while unseen:
            seed=min(unseen); unseen.remove(seed)
            region=[seed]; stack=[seed]
            ref=tri_normals[seed].copy()
            accum=ref.copy()
            while stack:
                cur=stack.pop()
                # A slowly updated mean normal avoids over-fragmenting triangulated planes while
                # still preventing a patch from wrapping all the way around a curved surface.
                mean=accum.normalized() if accum.length_squared>1.0e-18 else ref
                for nb in neighbors[cur]:
                    if nb not in unseen: continue
                    n=tri_normals[nb]
                    if n.dot(ref)>=coslim and n.dot(mean)>=coslim:
                        unseen.remove(nb); region.append(nb); stack.append(nb); accum+=n
            regions.append([triangles[i] for i in region])
        return regions

    # Respect the user part budget without blindly merging distant patches (which could close a
    # ring/torus hole). If there are too many patches, relax the angular threshold and rebuild.
    angle=float(split_angle); limit=max(2,int(max_parts)); regions=build(angle)
    for _ in range(10):
        if len(regions)<=limit: break
        angle=min(math.radians(120.0),angle*1.22+math.radians(1.0))
        regions=build(angle)
    return regions

def _loose_hull_for_triangles(vertices,triangles,simplify):
    """Keep island geometry; give a planar sheet a small collision-only volume."""
    pts=[Vector(vertices[i]) for i in sorted({i for tri in triangles for i in tri})]
    if len(pts)<3:return None
    origin=pts[0];axis=max((p-origin for p in pts),key=lambda v:v.length_squared)
    normal=max((axis.cross(p-origin) for p in pts),key=lambda v:v.length_squared)
    if normal.length_squared<1e-20:return None
    normal.normalize()
    extent=max((p-origin).length for p in pts)
    if max(abs((p-origin).dot(normal)) for p in pts)<=max(1e-7,extent*1e-6):
        thickness=max(.002,min(.08,extent*.012))
        expanded=[tuple(p+normal*(side*thickness*.5)) for side in (-1,1) for p in pts]
        hv,hf=_convex_hull_from_points(expanded)
        return {'points':hv,'faces':hf,'max_vertices':min(32,max(8,len(hv)))} if hf else None
    return _compound_hull_for_triangles(vertices,triangles,simplify)


def _evaluated_loose_parts_geometry(obj,depsgraph,simplify=0.65,max_parts=32,fit_tolerance=0.025):
    verts,indices=_evaluated_mesh_data(obj,depsgraph,True)
    if len(verts)<4 or len(indices)<3: return []
    comps=_loose_triangle_components(indices)
    if not comps: return []
    max_parts=max(1,min(128,int(max_parts)))

    # v0.13.43: Loose Parts can now also make useful collision from a *single* connected
    # concave mesh. This is especially important for ring/torus emitters: one Convex Hull
    # necessarily closes the centre hole, while several local convex surface patches preserve it.
    auto_split=False; split_angle=math.radians(28.0)
    try:
        st=bpy.context.scene.tumblify
        auto_split=bool(getattr(st,'loose_auto_split_connected',True))
        split_angle=float(getattr(st,'loose_split_angle',split_angle))
    except Exception:
        auto_split=True
    generated_patches=False
    if len(comps)==1 and auto_split and max_parts>1:
        patches=_normal_patch_components(verts,comps[0],split_angle,max_parts)
        if len(patches)>1:
            comps=patches[:max_parts]
            generated_patches=True

    if len(comps)>max_parts:
        raise CollisionGeometryError(f"'{obj.name}': {len(comps)} loose islands exceed Max Loose Parts ({max_parts}). Increase the limit or split the collider into objects; islands are never merged across empty space")
    parts=[]
    for comp in comps:
        if generated_patches:
            # For generated curved surface patches, fitting Box/Sphere/Cylinder would frequently
            # bridge empty space. A local convex hull is more predictable and preserves openings.
            part=_loose_hull_for_triangles(verts,comp,simplify)
            if part:
                part=dict(part); part['fit']='Auto Split Hull'
        else:
            part=_loose_hull_for_triangles(verts,comp,simplify)
            if part:
                part=dict(part);part['fit']='Loose Hull'
        if part: parts.append(part)
    return parts

def _container_mesh_bvh(obj,depsgraph):
    """Return scaled-local triangle geometry and a BVH for cavity sampling."""
    try:
        verts_raw, inds_raw, _source_tris, _collision_tris=_evaluated_collision_mesh_data(obj,depsgraph)
        pdata={'verts':verts_raw,'indices':inds_raw}
    except Exception:
        return None
    verts=[Vector(v) for v in pdata.get('verts',[])]
    inds=list(pdata.get('indices',[]))
    faces=[tuple(map(int,inds[i:i+3])) for i in range(0,len(inds)-2,3)]
    if len(verts)<4 or not faces:
        return None
    try: bvh=BVHTree.FromPolygons(verts,faces,all_triangles=True)
    except Exception: return None
    mn=Vector((min(v.x for v in verts),min(v.y for v in verts),min(v.z for v in verts)))
    mx=Vector((max(v.x for v in verts),max(v.y for v in verts),max(v.z for v in verts)))
    return verts,faces,bvh,mn,mx

def _container_solid_inside(bvh,mn,mx,p):
    """Parity test against the render solid; False is expected for the hollow cavity."""
    diag=max(1.0e-5,(mx-mn).length)
    direction=Vector((1.0,0.000173,0.000311)).normalized()
    eps=max(1.0e-6,diag*1.0e-6)
    origin=Vector(p)+direction*eps
    remaining=diag*3.0; hits=0
    for _ in range(96):
        loc,normal,index,distance=bvh.ray_cast(origin,direction,remaining)
        if loc is None: break
        step=(loc-origin).length+eps
        remaining-=step
        if remaining<=0.0: break
        hits+=1; origin=loc+direction*eps
    return bool(hits & 1)

def _container_sample_ring(bvh,centre_xy,z,sectors,max_dist):
    """Robust angular cavity sampling.

    Each radial sector is sampled with several nearby rays instead of one exact ray.  The median
    hit radius is projected back onto the sector centre angle.  Small holes, handles, decorative
    cut-outs and sparse topology therefore do not destroy the whole ring, while the resulting
    points still form a deterministic angular profile suitable for convex wall sectors.
    """
    pts=[]
    cx,cy=centre_xy
    eps=max(1.0e-6,max_dist*1.0e-6)
    step=(2.0*math.pi)/max(3,sectors)
    # Keep the sub-rays inside one sector; five samples are still cheap compared with building
    # Blender evaluated meshes / BVHs and dramatically improve robustness around local holes.
    offsets=(-0.32,-0.16,0.0,0.16,0.32)
    for i in range(sectors):
        centre_angle=step*i
        radii=[]
        for frac in offsets:
            a=centre_angle+step*frac
            d=Vector((math.cos(a),math.sin(a),0.0))
            origin=Vector((cx,cy,z))+d*eps
            loc,normal,index,distance=bvh.ray_cast(origin,d,max_dist)
            if loc is None:
                continue
            radii.append(math.hypot(float(loc.x)-cx,float(loc.y)-cy))
        if not radii:
            pts.append(None)
            continue
        radii.sort()
        r=radii[len(radii)//2]
        pts.append(Vector((cx+math.cos(centre_angle)*r,cy+math.sin(centre_angle)*r,z)))

    # Angular hulls can bridge local holes, so tolerate substantially more missed sectors than
    # the old one-ray profile.  Missing sectors are interpolated from the nearest valid neighbours.
    hits=sum(p is not None for p in pts)
    if hits<max(4,int(math.ceil(sectors*0.50))):
        return None
    for i,p in enumerate(pts):
        if p is not None:
            continue
        prev=next_=None
        prev_steps=next_steps=0
        for k in range(1,sectors):
            q=pts[(i-k)%sectors]
            if q is not None:
                prev=q; prev_steps=k; break
        for k in range(1,sectors):
            q=pts[(i+k)%sectors]
            if q is not None:
                next_=q; next_steps=k; break
        if prev is None or next_ is None:
            return None
        # Interpolate radii rather than XYZ positions so interpolation stays centred on the cavity.
        rp=math.hypot(prev.x-cx,prev.y-cy); rn=math.hypot(next_.x-cx,next_.y-cy)
        t=prev_steps/max(1.0,float(prev_steps+next_steps))
        r=rp+(rn-rp)*t
        a=step*i
        pts[i]=Vector((cx+math.cos(a)*r,cy+math.sin(a)*r,z))
    return pts

def _container_cavity_profile(obj,depsgraph,settings=None,geometry=None):
    """Sample an open, star-shaped inner cavity in local scaled coordinates.

    Automatic detection establishes the cavity centre, wall profile, floor and mouth.  Per-object
    Container Height / Bottom Offset then adjust the usable collision volume without changing the
    render mesh.  This is useful for vessels whose decorative base extends below the real cavity or
    whose rim / upper geometry makes automatic height detection conservative.
    """
    geo=geometry if geometry is not None else _container_mesh_bvh(obj,depsgraph)
    if not geo: return None
    verts,faces,bvh,mn,mx=geo
    st=getattr(obj,'tumblify',None)
    requested=max(6,min(48,int(getattr(st,'container_radial_segments',16)) if st else 16))
    bands=max(1,min(12,int(getattr(st,'container_height_segments',4)) if st else 4))
    # Radial Segments now maps 1:1 to angular wall sectors. The old implementation
    # silently reduced this through Max Parts / Height Segments, making the control appear broken.
    sectors=requested
    centre=Vector(((mn.x+mx.x)*0.5,(mn.y+mx.y)*0.5,(mn.z+mx.z)*0.5))
    diag=max(1.0e-5,(mx-mn).length)

    # Refine the XY centre from the cavity itself instead of trusting render bounds.
    for _pass in range(2):
        samples=[]
        for jj in range(10):
            tt=(jj+1)/11.0
            zz=mn.z+(mx.z-mn.z)*tt
            cp=Vector((centre.x,centre.y,zz))
            if _container_solid_inside(bvh,mn,mx,cp):
                continue
            rr=_container_sample_ring(bvh,(centre.x,centre.y),zz,max(8,min(sectors,16)),diag*1.5)
            if not rr:
                continue
            samples.append((sum(p.x for p in rr)/len(rr),sum(p.y for p in rr)/len(rr)))
        if not samples:
            break
        nx=sum(v[0] for v in samples)/len(samples); ny=sum(v[1] for v in samples)/len(samples)
        delta=math.hypot(nx-centre.x,ny-centre.y)
        centre.x,centre.y=nx,ny
        if delta<diag*1.0e-4:
            break

    # Find a vertically contiguous cavity run.
    probe_count=max(20,bands*6+8)
    valid=[]
    for j in range(probe_count):
        t=(j+1)/(probe_count+1)
        z=mn.z+(mx.z-mn.z)*t
        cp=Vector((centre.x,centre.y,z))
        if _container_solid_inside(bvh,mn,mx,cp):
            continue
        ring=_container_sample_ring(bvh,(centre.x,centre.y),z,sectors,diag*1.5)
        if ring:
            radii=[math.hypot(p.x-centre.x,p.y-centre.y) for p in ring]
            med=sorted(radii)[len(radii)//2]
            if med>diag*0.01 and max(radii)<diag*1.25:
                valid.append((z,ring))
    if len(valid)<2:
        return None

    step=(mx.z-mn.z)/(probe_count+1)
    runs=[]; run=[]
    for item in valid:
        if run and item[0]-run[-1][0]>step*2.2:
            runs.append(run); run=[]
        run.append(item)
    if run: runs.append(run)
    run=max(runs,key=lambda r:(len(r),r[-1][0]-r[0][0]))
    if len(run)<2: return None

    z_lo,auto_z_hi=run[0][0],run[-1][0]
    open_top=bool(getattr(st,'container_open_top',True)) if st else True
    opening_clear=max(0.0,float(getattr(st,'container_opening_clearance',0.01)) if st else 0.01)
    if open_top and opening_clear>0.0:
        auto_z_hi=max(z_lo+max(diag*0.005,(mx.z-mn.z)*0.02),auto_z_hi-opening_clear)

    # Detect the actual inner bottom from a point known to be in the cavity.
    mid_z=(z_lo+auto_z_hi)*0.5
    origin=Vector((centre.x,centre.y,mid_z))
    loc,normal,index,distance=bvh.ray_cast(origin,Vector((0,0,-1)),diag*1.5)
    detected_bottom=float(loc.z) if loc is not None else float(z_lo)
    detected_bottom=min(detected_bottom,z_lo)

    # Per-object manual correction.  These values intentionally live on the collision object,
    # because two containers in the same scene can require different cavity corrections.
    ost=getattr(obj,'tumblify',None)
    bottom_offset=float(getattr(ost,'container_bottom_offset',0.0)) if ost else 0.0
    height_override=max(0.0,float(getattr(ost,'container_height_override',0.0)) if ost else 0.0)
    bottom_z=detected_bottom+bottom_offset
    min_height=max(diag*0.005,(mx.z-mn.z)*0.02,1.0e-5)
    z_hi=(bottom_z+height_override) if height_override>1.0e-9 else auto_z_hi
    z_hi=max(bottom_z+min_height,z_hi)

    # Interpolate/extrapolate the detected wall profile to the adjusted floor/top.  Outside the
    # detected run, reuse the nearest valid ring and translate it in Z; this intentionally creates
    # a virtual extension rather than inventing a closed hull across the opening.
    def ring_at_z(z):
        if z<=run[0][0]:
            src=run[0][1]
            return [Vector((p.x,p.y,z)) for p in src]
        if z>=run[-1][0]:
            src=run[-1][1]
            return [Vector((p.x,p.y,z)) for p in src]
        hi=1
        while hi<len(run) and run[hi][0]<z: hi+=1
        lo=max(0,hi-1); hi=min(len(run)-1,hi)
        z0,r0=run[lo]; z1,r1=run[hi]
        t=0.0 if abs(z1-z0)<1.0e-12 else max(0.0,min(1.0,(z-z0)/(z1-z0)))
        out=[]
        for a,b in zip(r0,r1):
            out.append(Vector((a.x+(b.x-a.x)*t,a.y+(b.y-a.y)*t,z)))
        return out

    rings=[]
    ring_count=bands+1
    for j in range(ring_count):
        t=j/max(1,ring_count-1)
        z=bottom_z+(z_hi-bottom_z)*t
        rings.append(ring_at_z(z))

    # Approximate adjusted cavity volume for bottom-up Fill level.
    areas=[]
    for ring in rings:
        area=0.0
        for i in range(sectors):
            a=ring[i]; b=ring[(i+1)%sectors]
            area += a.x*b.y-b.x*a.y
        areas.append(abs(area)*0.5)
    volume=0.0
    for i in range(len(rings)-1):
        dz=max(0.0,rings[i+1][0].z-rings[i][0].z)
        volume+=0.5*(areas[i]+areas[i+1])*dz
    return {'centre':centre,'mn':mn,'mx':mx,'bvh':bvh,'rings':rings,'sectors':sectors,
            'bands':bands,'bottom_z':bottom_z,'detected_bottom_z':detected_bottom,
            'height_override':height_override,'bottom_offset':bottom_offset,
            'z_min':bottom_z,'z_max':z_hi,'auto_z_max':auto_z_hi,
            'open_top':open_top,'volume':max(1.0e-9,volume),'diag':diag}

def _container_profile_radius(profile,z,angle):
    rings=profile.get('rings') or []
    sectors=int(profile.get('sectors',0))
    if not rings or sectors<3: return None
    z0=rings[0][0].z; z1=rings[-1][0].z
    if z<profile.get('bottom_z',z0) or z>z1: return None
    if len(rings)==1: k=0; tz=0.0
    else:
        f=(z-z0)/max(1.0e-9,z1-z0)*(len(rings)-1)
        k=max(0,min(len(rings)-2,int(math.floor(f)))); tz=max(0.0,min(1.0,f-k))
    af=(angle%(2.0*math.pi))/(2.0*math.pi)*sectors
    i0=int(math.floor(af))%sectors; i1=(i0+1)%sectors; ta=af-math.floor(af)
    c=profile['centre']
    def rr(ring,i):
        p=ring[i]; return math.hypot(p.x-c.x,p.y-c.y)
    ra0=(1-ta)*rr(rings[k],i0)+ta*rr(rings[k],i1)
    if len(rings)==1: return ra0
    ra1=(1-ta)*rr(rings[k+1],i0)+ta*rr(rings[k+1],i1)
    return (1-tz)*ra0+tz*ra1

def _container_profile_contains(profile,p,clearance=0.0):
    if not profile: return False
    p=Vector(p); c=profile['centre']
    if profile.get('polygon_sections'):
        if not profile['z_min']+clearance <= p.z <= profile['z_max']-clearance:
            return False
        rings = profile['rings']
        k = next(i for i in range(len(rings)-1) if p.z <= rings[i+1][0].z)
        low,high = rings[k],rings[k+1]
        fraction = (p.z-low[0].z)/(high[0].z-low[0].z)
        ring = [a.lerp(b,fraction) for a,b in zip(low,high)]
        for a,b in zip(ring,ring[1:]+ring[:1]):
            edge = b.xy-a.xy
            delta = p.xy-a.xy
            if edge.x*delta.y-edge.y*delta.x < clearance*edge.length:
                return False
        return True
    offset=float(profile.get('wall_offset',0.0))
    if p.z<profile.get('bottom_z',profile['z_min'])+offset+clearance or p.z>profile['z_max']-clearance:
        return False
    dx=p.x-c.x; dy=p.y-c.y; r=math.hypot(dx,dy)
    ang=math.atan2(dy,dx)
    limit=_container_profile_radius(profile,p.z,ang)
    return limit is not None and limit-offset>=clearance and r<=limit-offset-clearance

def _box_container_profile(obj, geo):
    """Build a rectangular proxy from local evaluated mesh bounds."""
    _, _, bvh, lo, hi = geo
    st = obj.tumblify
    size = hi-lo
    depth = min(min(size)*0.2, max(0.003,min(size)*0.025))
    centre = (lo+hi)*0.5
    top = max(lo.z+3*depth, hi.z+st.container_top_offset)
    floor = min(top-2*depth, max(lo.z+depth, lo.z+depth+st.container_bottom_offset))
    max_wall_depth = min(size.x,size.y)*0.45
    wall_depth = min(max_wall_depth,max(0.002,st.container_wall_depth_override)) if st.container_wall_depth_override else depth
    x0,x1 = lo.x+wall_depth,hi.x-wall_depth
    y0,y1 = lo.y+wall_depth,hi.y-wall_depth
    rings = [[Vector((x,y,z)) for x,y in ((x1,y0),(x1,y1),(x0,y1),(x0,y0))]
             for z in (floor,top)]

    # Find the underside of the body separately to retain any disconnected
    # feet and chassis under it when the usable floor is moved.
    up = Vector((0,0,1))
    hits = []
    for dx,dy in ((0,0),(-0.2,0),(0.2,0),(0,-0.2),(0,0.2)):
        origin = Vector((centre.x+dx*size.x,centre.y+dy*size.y,lo.z-depth))
        hit = bvh.ray_cast(origin,up,size.z+2*depth)[0]
        if hit is not None:
            hits.append(hit.z)
    support_cutoff = sorted(hits)[len(hits)//2] if hits else lo.z
    return {
        'shape':'BOX', 'geo':geo, 'centre':centre, 'rings':rings, 'depth':depth, 'wall_depth':wall_depth,
        'bottom_z':floor, 'z_min':floor, 'z_max':top, 'base_z':support_cutoff,
        'volume':(x1-x0)*(y1-y0)*(top-floor), 'open_top':st.container_open_top,
        'polygon_sections':True,
    }


def _container_collision_profile(obj, depsgraph):
    """Fit the inner shell in scaled local +Z, independently of the base and supports."""
    geo = _container_mesh_bvh(obj, depsgraph)
    if geo is None:
        return None
    _, _, bvh, lo, hi = geo
    st = obj.tumblify
    size = hi - lo
    if min(size) <= 1e-6:
        return None
    if st.container_form == 'BOX':
        return _box_container_profile(obj, geo)
    centre = (lo + hi) * 0.5
    reach = size.length * 1.5
    radial_count = 16  # Two 16-point rings fit the native hull vertex budget.
    step = size.z / 64
    samples = []
    min_radius = min(size.x, size.y) * 0.06
    down = Vector((0, 0, -1))

    def sample(z):
        ring = _container_sample_ring(bvh, centre.xy, z, radial_count, reach)
        if ring and min(math.hypot(p.x-centre.x,p.y-centre.y) for p in ring) > min_radius:
            return ring
        return None

    for i in range(64):
        z = lo.z + (i + 0.5) * step
        ring = sample(z)
        if ring is not None:
            samples.append((z, ring))
    runs = []
    for item in samples:
        if not runs or item[0] - runs[-1][-1][0] > 2.1 * step:
            runs.append([])
        runs[-1].append(item)
    run = max(runs, key=len, default=[])
    if len(run) < 3:
        return _box_container_profile(obj, geo) if st.container_form == 'AUTO' else None
    low, high = run[0][0], run[-1][0]
    # Probe the floor from several interior points. A single ray can hit a
    # central bump, hole or decorative detail instead of the floor.
    middle_ring = run[len(run) // 2][1]
    probe_offsets = [Vector((0, 0, 0))]
    for i in (0, 4, 8, 12):
        delta = middle_ring[i] - centre
        probe_offsets.append(Vector((delta.x, delta.y, 0)) * 0.3)
    floor_hits = []
    interior_probes = []
    underside_hits = []
    for offset in probe_offsets:
        origin = Vector((centre.x, centre.y, (low+high)*0.5)) + offset
        hit, normal, _, _ = bvh.ray_cast(origin, down, reach)
        if hit is not None:
            floor_hits.append(hit.z)
            interior_probes.append((origin.copy(), normal.z))
        origin.z = lo.z - step
        hit = bvh.ray_cast(origin, -down, reach)[0]
        if hit is not None:
            underside_hits.append(hit.z)
    if not floor_hits:
        return None
    # A solid watertight block also returns lateral rays. Reject it when
    # every floor probe starts in material and hits a downward-facing exterior.
    if all(normal_z < -0.5 and _container_solid_inside(bvh,lo,hi,origin)
           for origin, normal_z in interior_probes):
        return None
    def box_bounds(ring):
        return (ring[8].x, ring[0].x, ring[12].y, ring[4].y)

    if st.container_form == 'AUTO':
        errors = []
        for _, ring in run[::max(1, len(run) // 9)]:
            x0, x1, y0, y1 = box_bounds(ring)
            span = min(x1 - x0, y1 - y0)
            errors.extend(min(abs(p.x-x0), abs(p.x-x1), abs(p.y-y0), abs(p.y-y1)) / span for p in ring)
        if sorted(errors)[len(errors) // 2] <= 0.035:
            return _box_container_profile(obj, geo)

    detected_floor = sorted(floor_hits)[len(floor_hits) // 2]
    underside = sorted(underside_hits)[len(underside_hits) // 2] if underside_hits else detected_floor
    depth = max(0.003, min(size) * 0.025)
    base = min(detected_floor - depth, underside)
    base = max(lo.z, base)
    if detected_floor - base < depth:
        base = detected_floor - depth

    # Find the last enclosed section more precisely without using decorative
    # handles or the global object height as the rim.
    top_outside = min(hi.z, high + 2 * step)
    for _ in range(8):
        z = (high + top_outside) * 0.5
        if sample(z) is None:
            top_outside = z
        else:
            high = z
    if high <= detected_floor + depth:
        return None

    # Round vessels retain the sampled taper, including through an
    # optional virtual extension above the detected rim.
    rim = high
    high = max(detected_floor+2*depth, rim+st.container_top_offset)
    bands = 6
    wall_rings = []
    for i in range(bands+1):
        z = detected_floor + (high-detected_floor)*i/bands
        sample_z = max(low,min(rim,z))
        ring = sample(sample_z) or min(run,key=lambda pair:abs(pair[0]-sample_z))[1]
        wall_rings.append([Vector((p.x,p.y,z)) for p in ring])
    floor_min = max(base+depth*0.5, detected_floor-depth*0.5)
    floor = min(high-depth, max(floor_min, detected_floor + st.container_bottom_offset))
    def ring_at(z):
        f = max(0, min(bands, (z-detected_floor)/(high-detected_floor)*bands))
        k = min(bands-1, int(f))
        return [Vector((*a.lerp(b, f-k).xy,z)) for a,b in zip(wall_rings[k],wall_rings[k+1])]

    fill_rings = [ring_at(floor)] + [ring for ring in wall_rings if ring[0].z > floor]
    def area(ring):
        return 0.5 * abs(sum(p.x*q.y-p.y*q.x for p,q in zip(ring,ring[1:]+ring[:1])))
    volume = sum((area(a)+area(b))*.5*(b[0].z-a[0].z) for a,b in zip(fill_rings,fill_rings[1:]))
    return {
        'shape':'ROUND', 'geo':geo, 'centre':centre, 'rings':fill_rings, 'wall_rings':wall_rings,
        'wall_depth':max(0.003,st.container_wall_depth_override) if st.container_wall_depth_override else 0.0,
        'bottom_z':floor, 'z_min':floor, 'z_max':high, 'base_z':base, 'depth':depth,
        'volume':volume, 'open_top':st.container_open_top, 'polygon_sections':True,
        'floor_outline':ring_at(floor), 'base_outline':wall_rings[0],
    }


def _container_support_points(geo, cutoff):
    """Clip the mesh below the base and keep disconnected feet separate."""
    verts, faces, _, lo, hi = geo
    epsilon = max(1e-6, (hi-lo).length * 1e-5)
    groups = {}
    parents = {}

    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for face in faces:
        polygon = [verts[i] for i in face]
        if min(p.z for p in polygon) >= cutoff-epsilon:
            continue
        clipped = []
        for a,b in zip(polygon, polygon[1:]+polygon[:1]):
            if a.z <= cutoff:
                clipped.append(a)
            if (a.z < cutoff < b.z) or (b.z < cutoff < a.z):
                clipped.append(a.lerp(b, (cutoff-a.z)/(b.z-a.z)))
        keys = []
        for point in clipped:
            key = tuple(round(v/epsilon) for v in point)
            groups[key] = point
            parents.setdefault(key, key)
            keys.append(key)
        for key in keys[1:]:
            parents[root(key)] = root(keys[0])
    components = {}
    for key, point in groups.items():
        components.setdefault(root(key), []).append(point)
    return list(components.values())


def _box_container_parts(profile):
    _, _, _, lo, hi = profile['geo']
    plate_depth = profile['depth']
    depth = profile['wall_depth']
    floor, top = profile['bottom_z'], profile['z_max']
    x0,y0,z0 = lo
    x1,y1,_ = hi
    parts = []

    def box(lower, upper, role):
        centre = (Vector(lower)+Vector(upper))*0.5
        half = (Vector(upper)-Vector(lower))*0.5
        points, faces = _box_geometry(*half)
        parts.append({
            'points':[tuple(centre+Vector(p)) for p in points], 'faces':faces, 'fit':role,
            'native':{'kind':'BOX', 'pos':tuple(centre), 'rot':(0,0,0,1), 'params':tuple(half)},
        })

    box((x0,y0,z0), (x0+depth,y1,top), 'Container Wall')
    box((x1-depth,y0,z0), (x1,y1,top), 'Container Wall')
    box((x0+depth,y0,z0), (x1-depth,y0+depth,top), 'Container Wall')
    box((x0+depth,y1-depth,z0), (x1-depth,y1,top), 'Container Wall')
    box((x0+depth,y0+depth,floor-plate_depth), (x1-depth,y1-depth,floor), 'Container Floor')
    if not profile['open_top']:
        box((x0+depth,y0+depth,top-plate_depth), (x1-depth,y1-depth,top), 'Container Lid')

    _add_container_supports(profile,parts)
    return parts


def _add_container_supports(profile, parts):
    for points in _container_support_points(profile['geo'],profile['base_z']):
        if len(points) < 4 or min(max(p[i] for p in points)-min(p[i] for p in points) for i in range(3)) <= 1e-4:
            continue
        points, faces = _hull_debug_geometry(_farthest_point_reduce(points,32))
        if faces:
            parts.append({'points':points,'faces':faces,'max_vertices':min(48,max(8,len(points))),
                          'fit':'Container Support'})


def _container_surface_parts(obj, depsgraph, *, profile=None):
    profile = profile if profile is not None else _container_collision_profile(obj, depsgraph)
    if not profile:
        return []
    if profile['shape'] == 'BOX':
        return _box_container_parts(profile)
    rings = profile['wall_rings']
    depth = profile['depth']
    wall_depth = profile['wall_depth']
    _, _, bvh, _, _ = profile['geo']
    parts = []

    def add(points, role):
        points, faces = _hull_debug_geometry(points)
        if faces:
            parts.append({'points':points, 'faces':faces, 'max_vertices':min(48,max(8,len(points))), 'fit':role})

    def outer_point(p, outward):
        # A second hit just outside the inner face estimates material
        # thickness. Single-sided walls use a small automatic proxy depth.
        hit = bvh.ray_cast(p + outward * 1e-4, outward, depth * 5)[0]
        return p + outward * depth if hit is None else hit

    for band, (lower, upper) in enumerate(zip(rings, rings[1:])):
        # Extend only the bottom edge enough to meet a lowered floor.
        wall_bottom = max(profile['base_z'], lower[0].z-depth)
        edge_bottom = lower if band else [Vector((p.x,p.y,wall_bottom)) for p in lower]
        for i in range(len(lower)):
            j = (i+1) % len(lower)
            edge = lower[j] - lower[i]
            outward = Vector((edge.y,-edge.x,0)).normalized()
            front = [lower[i],lower[j],upper[i],upper[j]]
            if wall_depth:
                # Use each ring's normal so neighbouring height bands
                # share the same outer edge, even on a tapered vessel.
                top_edge = upper[j] - upper[i]
                top_normal = Vector((top_edge.y,-top_edge.x,0)).normalized()
                offsets = (outward,outward,top_normal,top_normal)
                back = [p+normal*wall_depth for p,normal in zip(front,offsets)]
            else:
                back = [outer_point(p, outward) for p in front]
                # Single-sided faces and bevels still need a physical wall.
                back = [b if (b-p).length >= 0.003 else p+outward*depth for p,b in zip(front,back)]
            if band == 0:
                front[:2] = [edge_bottom[i], edge_bottom[j]]
                back[:2] = [Vector((p.x,p.y,wall_bottom)) for p in back[:2]]
            add([tuple(p) for p in front+back], 'Container Wall')

    def plate(outline, bottom, top, role):
        # Overlap the walls to avoid seams at the floor and corners.
        centre = sum(outline, Vector())/len(outline)
        add([(p.x+normal.x*depth*.5,p.y+normal.y*depth*.5,z)
             for p in outline for normal in [(p-centre).normalized()] for z in (bottom,top)], role)

    base = profile['base_z']
    floor = profile['bottom_z']
    # Match the original bottom footprint even when the usable floor moves.
    # The plate underneath the insert stays fixed at the detected underside.
    plate(profile['base_outline'], base, base+depth*.5, 'Container Base')
    plate(profile['floor_outline'], floor-depth*.5, floor, 'Container Floor')
    if not profile['open_top']:
        plate(rings[-1], profile['z_max']-depth*.5, profile['z_max'], 'Container Lid')

    _add_container_supports(profile,parts)
    return parts


def _data_as_compound_parts(data):
    sh=data.get('shape'); c=Vector(data.get('center',(0,0,0))); h=Vector(data.get('half',(0.001,0.001,0.001)))
    if sh=='BOX':
        v,f=_box_geometry(h.x,h.y,h.z); return [{'points':[(x+c.x,y+c.y,z+c.z) for x,y,z in v],'faces':f,'max_vertices':8,'fit':'Box'}]
    if sh=='SPHERE':
        v,f=_sphere_geometry(max(h),12,6); return [{'points':[(x+c.x,y+c.y,z+c.z) for x,y,z in v],'faces':f,'max_vertices':32,'fit':'Sphere'}]
    if sh=='CAPSULE':
        axis=int(data.get('axis',_long_axis(h))); r,long_half=_axis_radius_half(h,axis,True); hs=max(0.0,long_half-r)
        v,f=_capsule_geometry(hs,r,12,3); v=[_axis_map_point(x,axis) for x in v]; return [{'points':[(x+c.x,y+c.y,z+c.z) for x,y,z in v],'faces':f,'max_vertices':32,'fit':'Capsule'}]
    if sh=='CYLINDER':
        axis=int(data.get('axis',_cylinder_axis(h))); r,long_half=_axis_radius_half(h,axis,True); v,f=_cylinder_geometry(long_half,r,(0,0,0),12); v=[_axis_map_point(x,axis) for x in v]
        return [{'points':[(x+c.x,y+c.y,z+c.z) for x,y,z in v],'faces':f,'max_vertices':24,'fit':'Cylinder'}]
    if sh=='HULL': return [{'points':data.get('points',[]),'faces':data.get('faces',[]),'max_vertices':data.get('max_vertices',32),'fit':'Hull'}]
    if sh in {'COMPOUND','LOOSE','MANUAL','CONTAINER'}: return list(data.get('parts',[]))
    return []

def _axis_y_quaternion(axis):
    target=(Vector((1,0,0)),Vector((0,1,0)),Vector((0,0,1)))[max(0,min(2,int(axis)))]
    return Vector((0,1,0)).rotation_difference(target).normalized()

def _quat_tuple_xyzw(q):
    q=q.normalized()
    return (float(q.x),float(q.y),float(q.z),float(q.w))

def _basis_quaternion(basis):
    """Quaternion whose local XYZ axes match the supplied orthonormal basis."""
    try:
        b0,b1,b2=basis
        m=Matrix(((b0.x,b1.x,b2.x),(b0.y,b1.y,b2.y),(b0.z,b1.z,b2.z)))
        return m.to_quaternion().normalized()
    except Exception:
        return Quaternion((1.0,0.0,0.0,0.0))

def _native_parts_from_data(data):
    """Return exact native primitive descriptors where available.

    Geometry is already object-scale baked by _prepare_collision. Compound/automatic pieces that
    don't retain exact primitive parameters intentionally remain HULL parts. Manual Compound v2
    can therefore preserve Box/Sphere/Capsule/Cylinder exactly without changing older proxies.
    """
    sh=data.get('shape'); c=Vector(data.get('center',(0,0,0))); h=Vector(data.get('half',(0.001,0.001,0.001)))
    ident=(0.0,0.0,0.0,1.0)
    if sh=='BOX':
        return [{'kind':'BOX','pos':tuple(c),'rot':ident,'params':(float(h.x),float(h.y),float(h.z))}]
    if sh=='SPHERE':
        return [{'kind':'SPHERE','pos':tuple(c),'rot':ident,'params':(float(max(h)),0.0,0.0)}]
    if sh=='CAPSULE':
        axis=int(data.get('axis',_long_axis(h))); r,long_half=_axis_radius_half(h,axis,True); hs=max(0.0,long_half-r)
        return [{'kind':'CAPSULE','pos':tuple(c),'rot':_quat_tuple_xyzw(_axis_y_quaternion(axis)),'params':(float(hs),float(r),0.0)}]
    if sh=='CYLINDER':
        # Mixed/compound cylinders use the same robust convex representation as standalone
        # cylinders so collision behaviour does not depend on how the source was authored.
        axis=int(data.get('axis',_cylinder_axis(h))); r,long_half=_axis_radius_half(h,axis,True)
        verts,_faces=_cylinder_geometry(long_half,r,(0,0,0),16)
        verts=[_axis_map_point(v,axis) for v in verts]
        pts=[(float(v[0]+c.x),float(v[1]+c.y),float(v[2]+c.z)) for v in verts]
        return [{'kind':'HULL','pos':(0,0,0),'rot':ident,'params':(0,0,0),'points':pts,'max_vertices':32,'fit':'Cylinder'}]
    if sh=='HULL':
        return [{'kind':'HULL','pos':(0,0,0),'rot':ident,'params':(0,0,0),'points':data.get('points',[]),'max_vertices':data.get('max_vertices',32)}]
    if sh in {'MANUAL','LOOSE','CONTAINER'} and data.get('mixed_parts'):
        return list(data['mixed_parts'])
    if sh in {'COMPOUND','LOOSE','MANUAL','CONTAINER'}:
        return [{'kind':'HULL','pos':(0,0,0),'rot':ident,'params':(0,0,0),'points':part.get('points',[]),'max_vertices':part.get('max_vertices',32)} for part in data.get('parts',[]) if len(part.get('points',[]))>=4]
    return []

def _manual_mixed_parts(obj):
    raw=obj.get(MANUAL_COMPOUND_PROP,'')
    if not raw: return []
    try: payload=json.loads(raw) if isinstance(raw,str) else raw
    except Exception: return []
    parts=payload.get('parts',[]) if isinstance(payload,dict) else []
    try: _,_,sc=world_pose_scale(obj)
    except Exception: sc=(1,1,1)
    abs_sc=tuple(max(1.0e-8,abs(float(x))) for x in sc)
    uniform=max(abs_sc)-min(abs_sc) <= max(abs_sc)*1.0e-4
    us=sum(abs_sc)/3.0
    out=[]
    for part in parts:
        kind=str(part.get('kind','HULL')).upper()
        # Non-uniform object scale can shear an arbitrarily rotated primitive. Keep collision exact
        # by falling back to its stored hull shell instead of pretending it is still a primitive.
        if kind in {'BOX','SPHERE','CAPSULE','CYLINDER'} and uniform:
            pos=part.get('pos',(0,0,0)); rot=part.get('rot',(0,0,0,1)); par=part.get('params',(0,0,0))
            out.append({'kind':kind,'pos':(float(pos[0])*sc[0],float(pos[1])*sc[1],float(pos[2])*sc[2]),'rot':tuple(float(x) for x in rot),'params':(float(par[0])*us,float(par[1])*us,float(par[2]) if kind=='CYLINDER' else float(par[2])*us)})
            continue
        pts=[(float(p[0])*sc[0],float(p[1])*sc[1],float(p[2])*sc[2]) for p in part.get('points',[])]
        if len(pts)>=4:
            out.append({'kind':'HULL','pos':(0,0,0),'rot':(0,0,0,1),'params':(0,0,0),'points':pts,'max_vertices':int(part.get('max_vertices',32))})
    return out

def _manual_compound_parts(obj):
    raw=obj.get(MANUAL_COMPOUND_PROP,'')
    if not raw: return []
    try: payload=json.loads(raw) if isinstance(raw,str) else raw
    except Exception: return []
    parts=payload.get('parts',[]) if isinstance(payload,dict) else []
    try:
        _,_,sc=world_pose_scale(obj)
    except Exception: sc=(1,1,1)
    out=[]
    for part in parts:
        pts=[(float(p[0])*sc[0],float(p[1])*sc[1],float(p[2])*sc[2]) for p in part.get('points',[])]
        if len(pts)>=4: out.append({'points':pts,'faces':part.get('faces',[]),'max_vertices':int(part.get('max_vertices',32)),'fit':part.get('fit','Manual')})
    return out

def _box_geometry(hx,hy,hz):
    v=[(-hx,-hy,-hz),(hx,-hy,-hz),(hx,hy,-hz),(-hx,hy,-hz),
       (-hx,-hy,hz),(hx,-hy,hz),(hx,hy,hz),(-hx,hy,hz)]
    f=[(0,1,2,3),(4,7,6,5),(0,4,5,1),(1,5,6,2),(2,6,7,3),(4,0,3,7)]
    return v,f

def _sphere_geometry(radius,segments=20,rings=10):
    from math import sin,cos,pi
    verts=[(0,0,radius),(0,0,-radius)]
    for j in range(1,rings):
        th=pi*j/rings; z=radius*cos(th); rr=radius*sin(th)
        for i in range(segments):
            a=2*pi*i/segments; verts.append((rr*cos(a),rr*sin(a),z))
    faces=[]
    first=2
    for i in range(segments): faces.append((0,first+i,first+(i+1)%segments))
    for j in range(rings-2):
        a0=2+j*segments; a1=a0+segments
        for i in range(segments): faces.append((a0+i,a1+i,a1+(i+1)%segments,a0+(i+1)%segments))
    last=2+(rings-2)*segments
    for i in range(segments): faces.append((1,last+(i+1)%segments,last+i))
    return verts,faces

def _capsule_geometry(half_segment,radius,segments=20,hemi_rings=5):
    from math import sin,cos,pi
    # Rings from south pole to north pole. Cylinder is represented by the two equator rings.
    verts=[(0,0,-half_segment-radius)]
    rings=[]
    for j in range(1,hemi_rings+1):
        t=(pi*0.5)*j/hemi_rings
        rr=radius*sin(t); z=-half_segment-radius*cos(t)
        ring=[]
        for i in range(segments):
            a=2*pi*i/segments; ring.append(len(verts)); verts.append((rr*cos(a),rr*sin(a),z))
        rings.append(ring)
    # Upper hemisphere includes equator only once by starting above it.
    for j in range(hemi_rings-1,0,-1):
        t=(pi*0.5)*j/hemi_rings
        rr=radius*sin(t); z=half_segment+radius*cos(t)
        ring=[]
        for i in range(segments):
            a=2*pi*i/segments; ring.append(len(verts)); verts.append((rr*cos(a),rr*sin(a),z))
        rings.append(ring)
    top=len(verts); verts.append((0,0,half_segment+radius))
    faces=[]
    if rings:
        for i in range(segments): faces.append((0,rings[0][(i+1)%segments],rings[0][i]))
        for a,b in zip(rings,rings[1:]):
            for i in range(segments): faces.append((a[i],a[(i+1)%segments],b[(i+1)%segments],b[i]))
        for i in range(segments): faces.append((top,rings[-1][i],rings[-1][(i+1)%segments]))
    return verts,faces

def _cylinder_geometry(half_height,radius,center,segments=12):
    from math import sin,cos,pi
    cx,cy,cz=center; verts=[]
    for z in (cz-half_height,cz+half_height):
        for i in range(segments):
            a=2*pi*i/segments; verts.append((cx+radius*cos(a),cy+radius*sin(a),z))
    faces=[]
    faces.append(tuple(range(segments-1,-1,-1))); faces.append(tuple(range(segments,2*segments)))
    for i in range(segments):
        j=(i+1)%segments; faces.append((i,j,segments+j,segments+i))
    return verts,faces

def _hull_debug_geometry(points):
    bm=bmesh.new()
    try:
        for p in points: bm.verts.new(p)
        bm.verts.ensure_lookup_table()
        if len(bm.verts)>=4:
            try: bmesh.ops.convex_hull(bm,input=list(bm.verts),use_existing_faces=False)
            except Exception: pass
        verts=[tuple(v.co) for v in bm.verts if v.is_valid]
        idx={v:i for i,v in enumerate([v for v in bm.verts if v.is_valid])}
        faces=[]
        for f in bm.faces:
            if f.is_valid:
                face=tuple(idx[v] for v in f.verts if v in idx)
                if len(face)>=3: faces.append(face)
        if not faces:
            # Degenerate input: show the exact point cloud as tiny tetra-free vertices is less useful,
            # so use its bounds, matching the native fallback behavior conceptually.
            if verts:
                xs=[p[0] for p in verts]; ys=[p[1] for p in verts]; zs=[p[2] for p in verts]
                cx=(min(xs)+max(xs))*0.5; cy=(min(ys)+max(ys))*0.5; cz=(min(zs)+max(zs))*0.5
                hv=(max(xs)-min(xs))*0.5; hh=(max(ys)-min(ys))*0.5; hd=(max(zs)-min(zs))*0.5
                bv,bf=_box_geometry(max(hv,.00025),max(hh,.00025),max(hd,.00025))
                verts=[(x+cx,y+cy,z+cz) for x,y,z in bv]; faces=bf
        return verts,faces
    finally:
        bm.free()

def _auto_hull_surface_samples(obj,depsgraph):
    """Area-weighted samples from a lightweight convex envelope of the evaluated mesh.

    Auto collision is about the object's gross silhouette, not bevels, ribs, screws or
    disconnected detail shells.  Fitting primitives to the convex envelope makes the
    classifier much more robust on production assets while keeping Hull as the safe
    fallback whenever the envelope is not clearly primitive-shaped.
    """
    disabled_mods=[]
    bm=None
    try:
        if hasattr(obj,'modifiers'):
            for mod_name in (GUIDE_MOD_NAME,DISPLAY_MOD_NAME):
                mod=obj.modifiers.get(mod_name)
                if mod is not None and bool(mod.show_viewport):
                    disabled_mods.append(mod); mod.show_viewport=False
            if disabled_mods:
                try: depsgraph.update()
                except Exception: pass
        eo=obj.evaluated_get(depsgraph); me=eo.to_mesh()
        try:
            if not me.vertices: return [],None,None,None
            _,_,scale=world_pose_scale(obj)
            pts=[Vector((float(v.co.x*scale[0]),float(v.co.y*scale[1]),float(v.co.z*scale[2]))) for v in me.vertices]
            if len(pts)<4: return [],None,None,None
            bm=bmesh.new()
            bmv=[bm.verts.new(tuple(v)) for v in pts]
            bm.verts.ensure_lookup_table()
            try:
                res=bmesh.ops.convex_hull(bm,input=list(bm.verts),use_existing_faces=False)
                # Interior/unused input vertices are irrelevant after hull construction.
                unused=[]
                for key in ('geom_interior','geom_unused'):
                    unused.extend([g for g in res.get(key,[]) if isinstance(g,bmesh.types.BMVert) and g.is_valid])
                if unused:
                    try: bmesh.ops.delete(bm,geom=list(dict.fromkeys(unused)),context='VERTS')
                    except Exception: pass
            except Exception:
                return [],None,None,None
            bm.normal_update()
            verts=[v.co.copy() for v in bm.verts if v.is_valid]
            faces=[f for f in bm.faces if f.is_valid and len(f.verts)>=3]
            if not verts or not faces: return [],None,None,None
            mn=Vector((min(v.x for v in verts),min(v.y for v in verts),min(v.z for v in verts)))
            mx=Vector((max(v.x for v in verts),max(v.y for v in verts),max(v.z for v in verts)))
            out=[]
            total_area=0.0
            for f in faces:
                area=float(f.calc_area())
                if area<=1.0e-12: continue
                n=f.normal.copy()
                if n.length<=1.0e-12: continue
                n.normalize(); c=f.calc_center_median().copy()
                out.append((c,n,area)); total_area+=area
            # Closed convex hull volume via signed tetrahedra over triangulated face fans.
            vol=0.0
            for f in faces:
                vs=[v.co for v in f.verts]
                if len(vs)<3: continue
                a=vs[0]
                for i in range(1,len(vs)-1):
                    b=vs[i]; c=vs[i+1]
                    vol+=a.dot(b.cross(c))/6.0
            vol=abs(float(vol))
            return out,mn,mx,vol
        finally:
            eo.to_mesh_clear()
    finally:
        if bm is not None:
            try: bm.free()
            except Exception: pass
        if disabled_mods:
            try:
                for mod in disabled_mods: mod.show_viewport=True
                depsgraph.update()
            except Exception: pass

def _weighted_median(values):
    """Median for (value, weight) pairs without importing statistics/numpy."""
    vals=sorted((float(v),max(0.0,float(w))) for v,w in values if math.isfinite(float(v)))
    if not vals: return 0.0
    total=sum(w for _,w in vals)
    if total<=1.0e-12: return vals[len(vals)//2][0]
    target=total*0.5; acc=0.0
    for v,w in vals:
        acc+=w
        if acc>=target: return v
    return vals[-1][0]

def _primitive_fit_quality(samples,dist_fn,normal_fn,dist_tol=0.055,normal_tol=0.24):
    """Geometric whole-object fit score using distance + normal agreement.

    Returns (quality, inlier_ratio, mean_distance, mean_normal_error).  Triangle area
    is the weight, so a bevel cannot outweigh the large planar faces of a box merely
    because it contains many tiny triangles.
    """
    if not samples: return (-1.0,0.0,999.0,999.0)
    tw=0.0; iw=0.0; de=0.0; ne=0.0
    for p,n,w in samples:
        w=max(float(w),1.0e-12); d=max(0.0,float(dist_fn(p))); pe=normal_fn(p,n)
        nn=max(0.0,min(1.0,float(pe)))
        tw+=w; de+=w*d; ne+=w*nn
        if d<=dist_tol and nn<=normal_tol: iw+=w
    if tw<=0.0: return (-1.0,0.0,999.0,999.0)
    inlier=iw/tw; md=de/tw; mn=ne/tw
    # Strongly reward supported surface area; penalize geometric residual more than normals.
    quality=inlier-1.45*md-0.30*mn
    return quality,inlier,md,mn

def _auto_collision_result(obj,depsgraph):
    """Return a conservative whole-object primitive fit for automatic collision.

    The classifier deliberately prefers a correct Hull over a wrong primitive.
    Box has an explicit early lock; Cylinder must prove two end caps, a round
    side wall, and a nearly constant radius along one local axis.  This prevents
    cubes from becoming cylinders and cones/frustums from passing as cylinders.
    """
    # Fit the gross convex envelope, not the detailed render surface.  Real
    # production meshes often contain bevels, ribs, caps, internal shells and
    # disconnected detail geometry that otherwise biases primitive fitting.
    try:
        samples,mn,mx,hull_volume=_auto_hull_surface_samples(obj,depsgraph)
    except Exception:
        samples,mn,mx,hull_volume=[],None,None,None
    if not samples or mn is None or mx is None:
        return {'shape':'HULL'}

    center=(mn+mx)*0.5
    half=(mx-mn)*0.5
    hv=(float(half.x),float(half.y),float(half.z))
    diag=max((mx-mn).length,1.0e-8)
    if min(hv)<=1.0e-7:
        return {'shape':'HULL'}

    # Convex-hull volume is deterministic even for open/non-manifold source
    # meshes and is therefore safe as a coarse shape prior.
    aabb_volume=max(1.0e-12,8.0*hv[0]*hv[1]*hv[2])
    volume_ratio=(float(hull_volume)/aabb_volume) if hull_volume is not None and hull_volume>1.0e-12 else None

    # ------------------------------------------------------------------ BOX
    def box_dist(p):
        d=p-center
        return min(abs(abs(d.x)-half.x),abs(abs(d.y)-half.y),abs(abs(d.z)-half.z))/diag

    def box_norm(p,n):
        d=p-center
        vals=(abs(abs(d.x)-half.x),abs(abs(d.y)-half.y),abs(abs(d.z)-half.z))
        ax=min(range(3),key=lambda i: vals[i])
        pred=Vector((0,0,0)); pred[ax]=1.0 if d[ax]>=0.0 else -1.0
        return 1.0-abs(float(n.dot(pred)))

    boxq=_primitive_fit_quality(samples,box_dist,box_norm,0.025,0.16)

    # Hard Box lock must prove *both* AABB-face agreement and AABB occupancy.
    # Surface fit alone is not enough: cylinders, barrels and many mechanical
    # assets contain large planar regions close to the six AABB planes.  For a
    # closed mesh require >= 87% occupancy; this leaves generous room for bevels
    # while remaining clearly above a cylinder's theoretical pi/4 (~78.5%).
    # If volume is unavailable (open/non-manifold mesh), accept Box only when the
    # fit is essentially exact; otherwise prefer the safe Hull fallback.
    # Box must be almost literally a rectangular convex envelope.  This is
    # intentionally strict: a false Hull is acceptable; a huge false AABB Box
    # on a detailed prop is not.
    box_volume_ok=(volume_ratio is not None and volume_ratio>=0.93)
    box_open_exact=(volume_ratio is None and boxq[1]>=0.995 and boxq[2]<=0.004 and boxq[3]<=0.030)
    if (box_volume_ok and boxq[1]>=0.955 and boxq[2]<=0.010 and boxq[3]<=0.075) or box_open_exact:
        return {'shape':'BOX'}

    # --------------------------------------------------------------- SPHERE
    radii=[((p-center).length,w) for p,_n,w in samples]
    radius=max(_weighted_median(radii),1.0e-8)
    sphere_iso=min(hv)/max(hv)

    def sphere_dist(p):
        return abs((p-center).length-radius)/diag

    def sphere_norm(p,n):
        r=p-center
        if r.length<=1.0e-12:
            return 1.0
        return 1.0-abs(float(n.dot(r.normalized())))

    sphq=_primitive_fit_quality(samples,sphere_dist,sphere_norm,0.036,0.18) if sphere_iso>=0.86 else (-1.0,0.0,999.0,999.0)

    # --------------------------------------------------------------- CAPSULE
    capsule_candidates=[]
    for axis in range(3):
        side=[i for i in range(3) if i!=axis]
        r0,r1=hv[side[0]],hv[side[1]]
        radial_iso=min(r0,r1)/max(r0,r1)
        if radial_iso<0.88:
            continue
        ah=max(hv[axis],1.0e-8)
        cr=max((r0+r1)*0.5,1.0e-8)
        if ah<=cr*1.12:
            continue
        hs=max(0.0,ah-cr)

        def cap_dist(p,axis=axis,side=side,cr=cr,hs=hs):
            d=p-center; vals=(d.x,d.y,d.z)
            axial=max(-hs,min(hs,vals[axis])); da=vals[axis]-axial
            radial=math.hypot(vals[side[0]],vals[side[1]])
            return abs(math.hypot(radial,da)-cr)/diag

        def cap_norm(p,n,axis=axis,side=side,hs=hs):
            d=p-center; vals=(d.x,d.y,d.z)
            axial=max(-hs,min(hs,vals[axis]))
            pred=Vector((0,0,0)); pred[side[0]]=vals[side[0]]; pred[side[1]]=vals[side[1]]; pred[axis]=vals[axis]-axial
            if pred.length<=1.0e-12:
                return 1.0
            pred.normalize()
            return 1.0-abs(float(n.dot(pred)))

        cq=_primitive_fit_quality(samples,cap_dist,cap_norm,0.038,0.19)
        model_volume=math.pi*cr*cr*(2.0*hs)+(4.0/3.0)*math.pi*cr**3
        aabb_volume=max(1.0e-12,8.0*hv[0]*hv[1]*hv[2])
        capsule_candidates.append((cq,axis,radial_iso,model_volume/aabb_volume))
    capq=max(capsule_candidates,key=lambda x:x[0][0]) if capsule_candidates else None

    # -------------------------------------------------------------- CYLINDER
    # Cylinder acceptance is intentionally strict.  For each axis we require:
    # * circular bbox in the perpendicular plane,
    # * a meaningful lower AND upper cap with axial normals,
    # * a meaningful side wall,
    # * side normals pointing radially,
    # * radius staying almost constant along the axis.
    cyl_candidates=[]
    total_area=sum(max(float(w),0.0) for _p,_n,w in samples) or 1.0

    for axis in range(3):
        side=[i for i in range(3) if i!=axis]
        r0,r1=hv[side[0]],hv[side[1]]
        radial_iso=min(r0,r1)/max(r0,r1)
        if radial_iso<0.90:
            continue

        ah=max(hv[axis],1.0e-8)
        axis_vec=Vector((1.0 if axis==0 else 0.0,1.0 if axis==1 else 0.0,1.0 if axis==2 else 0.0))
        side_rows=[]
        lower_cap_rows=[]
        upper_cap_rows=[]
        lower_cap=upper_cap=side_area=0.0
        side_radial_error=0.0

        for p,n,w in samples:
            w=max(float(w),0.0)
            d=p-center; vals=(d.x,d.y,d.z)
            axial=float(vals[axis])
            radial_vec=Vector((0,0,0)); radial_vec[side[0]]=vals[side[0]]; radial_vec[side[1]]=vals[side[1]]
            radial=radial_vec.length
            nd=float(n.dot(axis_vec))

            # A cap must be geometrically near one end and have an axial normal.
            end_tol=max(ah*0.10,diag*0.012)
            if abs(axial+ah)<=end_tol and nd<=-0.82:
                lower_cap+=w
                lower_cap_rows.append((vals[side[0]],vals[side[1]],w))
                continue
            if abs(axial-ah)<=end_tol and nd>=0.82:
                upper_cap+=w
                upper_cap_rows.append((vals[side[0]],vals[side[1]],w))
                continue

            # Side wall: normal is mostly perpendicular to the axis and points
            # outward radially.  This rejects square/rectangular side walls.
            if abs(nd)<=0.35 and radial>1.0e-10:
                rv=radial_vec/radial
                radial_align=abs(float(n.dot(rv)))
                if radial_align>=0.88:
                    side_area+=w
                    side_radial_error+=w*(1.0-radial_align)
                    side_rows.append((axial,radial,w))

        low_frac=lower_cap/total_area
        high_frac=upper_cap/total_area
        side_frac=side_area/total_area
        if low_frac<0.012 or high_frac<0.012 or side_frac<0.18 or not side_rows:
            continue

        def cap_spread_ratio(rows):
            # A true circular end cap occupies a 2D area in its radial plane.
            # A cylinder side strip that merely happens to face +/-candidate-axis
            # is narrow in one radial direction, so its covariance is nearly 1D.
            if len(rows)<3:
                return 0.0
            tw=sum(max(0.0,w) for _u,_v,w in rows) or 1.0
            mu=sum(u*w for u,_v,w in rows)/tw
            mv=sum(v*w for _u,v,w in rows)/tw
            a=sum((u-mu)*(u-mu)*w for u,_v,w in rows)/tw
            c=sum((v-mv)*(v-mv)*w for _u,v,w in rows)/tw
            b=sum((u-mu)*(v-mv)*w for u,v,w in rows)/tw
            tr=a+c
            disc=max(0.0,(a-c)*(a-c)+4.0*b*b) ** 0.5
            lmax=max((tr+disc)*0.5,1.0e-12)
            lmin=max((tr-disc)*0.5,0.0)
            return lmin/lmax

        lower_spread=cap_spread_ratio(lower_cap_rows)
        upper_spread=cap_spread_ratio(upper_cap_rows)

        def cap_angular_coverage(rows, bins=12):
            # A real circular cap surrounds the candidate axis.  A false "cap"
            # made from cylinder side facets (the ambiguous 2x2x2 case) occupies
            # only a narrow angular sector in the candidate radial plane even
            # when its covariance happens to look two-dimensional.
            if len(rows)<3:
                return 0.0
            tw=sum(max(0.0,w) for _u,_v,w in rows) or 1.0
            mu=sum(u*w for u,_v,w in rows)/tw
            mv=sum(v*w for _u,v,w in rows)/tw
            radii=[(math.hypot(u-mu,v-mv),u,v,w) for u,v,w in rows]
            max_r=max((r for r,_u,_v,_w in radii),default=0.0)
            if max_r<=1.0e-10:
                return 0.0
            occupied=set()
            # Ignore points extremely close to the cap centre: they do not
            # provide useful angular evidence and can make sparse fan caps noisy.
            for r,u,v,w in radii:
                if r < max_r*0.18 or w<=0.0:
                    continue
                ang=(math.atan2(v-mv,u-mu)+2.0*math.pi)%(2.0*math.pi)
                occupied.add(min(bins-1,int(ang/(2.0*math.pi)*bins)))
            return len(occupied)/float(bins)

        lower_coverage=cap_angular_coverage(lower_cap_rows)
        upper_coverage=cap_angular_coverage(upper_cap_rows)
        # A true cap should be both two-dimensional and distributed around the
        # full axis. This is the decisive ambiguity breaker for an exactly
        # cubic AABB cylinder (diameter == height).
        if min(lower_spread,upper_spread)<0.16 or min(lower_coverage,upper_coverage)<0.34:
            continue

        sw=sum(w for _a,_r,w in side_rows) or 1.0
        mean_a=sum(a*w for a,_r,w in side_rows)/sw
        mean_r=sum(r*w for _a,r,w in side_rows)/sw
        var_a=sum((a-mean_a)*(a-mean_a)*w for a,_r,w in side_rows)/sw
        cov_ar=sum((a-mean_a)*(r-mean_r)*w for a,r,w in side_rows)/sw
        slope=(cov_ar/var_a) if var_a>1.0e-12 else 0.0
        const_r=math.sqrt(max(0.0,sum((r-mean_r)**2*w for _a,r,w in side_rows)/sw))/max(mean_r,1.0e-8)
        slope_norm=abs(slope)*ah/max(mean_r,1.0e-8)
        mean_radial_normal_error=side_radial_error/sw

        # Cone/frustum protection and low-poly square protection.
        if const_r>0.16 or slope_norm>0.12 or mean_radial_normal_error>0.14:
            continue

        cr=max(_weighted_median([(r,w) for _a,r,w in side_rows]),1.0e-8)

        def cyl_dist(p,axis=axis,side=side,cr=cr,ah=ah):
            d=p-center; vals=(d.x,d.y,d.z)
            radial=math.hypot(vals[side[0]],vals[side[1]])
            return min(abs(radial-cr),abs(abs(vals[axis])-ah))/diag

        def cyl_norm(p,n,axis=axis,side=side,cr=cr,ah=ah,axis_vec=axis_vec):
            d=p-center; vals=(d.x,d.y,d.z)
            radial=math.hypot(vals[side[0]],vals[side[1]])
            sd=abs(radial-cr); cd=abs(abs(vals[axis])-ah)
            if cd<sd:
                pred=axis_vec if vals[axis]>=0.0 else -axis_vec
            else:
                if radial<=1.0e-12:
                    return 1.0
                pred=Vector((0,0,0)); pred[side[0]]=vals[side[0]]/radial; pred[side[1]]=vals[side[1]]/radial
            return 1.0-abs(float(n.dot(pred)))

        cq=_primitive_fit_quality(samples,cyl_dist,cyl_norm,0.036,0.18)
        if cq[1]<0.60 or cq[0]<0.44:
            continue

        # Prefer the axis with the strongest balanced cap evidence.  This is
        # what fixes near-cubic cylinders where AABB dimensions alone are ambiguous.
        cap_balance=min(low_frac,high_frac)
        cap_2d=min(lower_spread,upper_spread)
        cap_coverage=min(lower_coverage,upper_coverage)
        score=(cq[0] + 0.20*cap_balance + 0.08*side_frac + 0.14*cap_2d
               + 0.24*cap_coverage - 0.45*slope_norm - 0.30*const_r)
        cyl_candidates.append((score,cq,axis,radial_iso,const_r,slope_norm,low_frac,high_frac,side_frac,cap_2d,cap_coverage))

    cylq=max(cyl_candidates,key=lambda x:x[0]) if cyl_candidates else None

    # ---------------------------------------------------------- final choice
    candidates=[]

    def adjusted(name,q,target=None,tol=0.25,extra_ok=True,axis=None,bonus=0.0):
        quality,inlier,md,mn=q
        if not extra_ok:
            return
        if volume_ratio is not None and target is not None:
            quality-=0.10*min(1.0,abs(volume_ratio-target)/max(target,tol))
        quality+=bonus
        candidates.append((quality,inlier,-md,-mn,name,axis))

    # BOX remains gated in the final competition as well.  Without this gate
    # a merely decent planar fit could still beat Cylinder/Hull after missing the
    # early lock.
    box_candidate_ok=((volume_ratio is not None and volume_ratio>=0.91 and boxq[1]>=0.93
                       and boxq[2]<=0.014 and boxq[3]<=0.095) or box_open_exact)
    adjusted('BOX',boxq,1.0,0.12,box_candidate_ok,bonus=-0.08)
    adjusted('SPHERE',sphq,math.pi/6.0,0.20,sphere_iso>=0.86)
    if capq is not None:
        adjusted('CAPSULE',capq[0],capq[3],0.22,True,capq[1])
    if cylq is not None:
        _score,cq,axis,_ri,_cv,_sl,_lf,_hf,_sf,_c2d,_ccov=cylq
        adjusted('CYLINDER',cq,math.pi/4.0,0.25,True,axis,bonus=0.10)

    if not candidates:
        return {'shape':'HULL'}

    quality,inlier,neg_md,neg_mn,name,axis=max(candidates)
    if inlier<0.74 or quality<0.60:
        return {'shape':'HULL'}

    result={'shape':name}
    if axis is not None:
        result['axis']=int(axis)
    return result



def _evaluated_terrain_data(obj,depsgraph):
    """Sample local scaled XY upper surface. Missing samples produce native holes."""
    from mathutils.bvhtree import BVHTree
    from mathutils import Vector
    disabled=[]
    for name in (GUIDE_MOD_NAME,DISPLAY_MOD_NAME):
        mod=obj.modifiers.get(name)
        if mod is not None and mod.show_viewport:disabled.append(mod);mod.show_viewport=False
    if disabled:depsgraph.update()
    eo=None;me=None
    try:
        eo=obj.evaluated_get(depsgraph);me=eo.to_mesh()
        me.calc_loop_triangles()
        _,_,scale=world_pose_scale(obj)
        vertices=[Vector((v.co.x*scale[0],v.co.y*scale[1],v.co.z*scale[2])) for v in me.vertices]
        faces=[tuple(t.vertices) for t in me.loop_triangles]
        if not faces:raise CollisionGeometryError('Terrain has no surface')
        tree=BVHTree.FromPolygons(vertices,faces,all_triangles=True,epsilon=0.00001)
        lo=[min(v[i] for v in vertices) for i in range(3)];hi=[max(v[i] for v in vertices) for i in range(3)]
        if hi[0]-lo[0]<1e-6 or hi[1]-lo[1]<1e-6:raise CollisionGeometryError('Terrain needs a surface across local X and Y')
        resolution=int(obj.tumblify.terrain_resolution)
        width=hi[0]-lo[0];depth=hi[1]-lo[1];longest=max(width,depth)
        nx=max(2,round((resolution-1)*width/longest)+1);nz=max(2,round((resolution-1)*depth/longest)+1)
        dx=width/(nx-1);dz=depth/(nz-1);heights=[];valid=[]
        margin=max(1.0,hi[2]-lo[2]);distance=hi[2]-lo[2]+2*margin
        for j in range(nz):
            for i in range(nx):
                hit=tree.ray_cast(Vector((lo[0]+i*dx,hi[1]-j*dz,hi[2]+margin)),Vector((0,0,-1)),distance)[0]
                valid.append(hit is not None);heights.append(float(hit.z) if hit is not None else lo[2])
        materials=[];indices=[]
        for j in range(nz-1):
            for i in range(nx-1):
                a=j*nx+i;b=a+1;c=a+nx;d=c+1
                present=all(valid[k] for k in (a,b,c,d));materials.append(0 if present else 255)
                if present:indices.extend((a,c,b,b,c,d))
        if not indices:raise CollisionGeometryError('Terrain sampling found no complete cells; increase Grid Resolution or use Mesh')
        verts=[(lo[0]+i*dx,hi[1]-j*dz,heights[j*nx+i]) for j in range(nz) for i in range(nx)]
        return {'heights':heights,'materials':materials,'nx':nx,'nz':nz,'dx':dx,'dz':dz,'origin':(lo[0],hi[1],0.0),'verts':verts,'indices':indices}
    finally:
        if eo is not None and me is not None:eo.to_mesh_clear()
        for mod in disabled:mod.show_viewport=True
        if disabled:depsgraph.update()
