# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Fast surface placement: filtered surface rays, bbox contact and a surface reticle."""
import bpy, math, time, gpu
from mathutils import Vector, Quaternion, Matrix
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
try:import numpy as np
except ImportError:np=None

Z=Vector((0,0,1))

def box_contact(source,quat,factor,point,normal,bounds=None):
    """Centre the oriented bounding box over point, with its lowest projection on the plane."""
    scale=Vector(source.scale)*factor
    corners=[quat@Vector((c[0]*scale.x,c[1]*scale.y,c[2]*scale.z)) for c in (bounds or source.bound_box)]
    centre=sum(corners,Vector())/len(corners)
    bottom=min(p.dot(normal) for p in corners)
    return Vector(point)-centre+normal*(centre.dot(normal)-bottom)


def _record(a,source,matrix,data=None):
    if data:
        c=Vector(data['center']);h=Vector(data['half']);p,q,_=a.world_pose_scale(source);rot=Quaternion((q[3],q[0],q[1],q[2]))
        corners=[p+rot@Vector((c.x+x*h.x,c.y+y*h.y,c.z+z*h.z)) for x in (-1,1) for y in (-1,1) for z in (-1,1)]
    else:corners=[matrix@Vector(v) for v in source.bound_box]
    low=tuple(min(v[i] for v in corners) for i in range(3));high=tuple(max(v[i] for v in corners) for i in range(3))
    return (source,matrix.copy(),matrix.inverted_safe(),low,high)


class SurfacePicker:
    """AABB broad phase + evaluated object ray casts. Preview ghosts are never queried."""
    def __init__(self,owner,context):
        from . import addon as a
        self.owner=owner;self.static=[];self.geometry={}
        for obj in list(owner.static_objs)+list(a._frozen_spawned(context.scene)):
            if not a._is_live_object(obj) or obj.type!='MESH' or obj.hide_get():continue
            geometry=bpy.data.objects.get(str(obj.get('Tumblify_SourceName',''))) if obj.get(a.FROZEN_PROP) else None
            self.static.append(_record(a,geometry or obj,obj.matrix_world,None if geometry else getattr(owner,'static_collision',{}).get(obj.name)))
        for inst in getattr(owner,'instance_static_results',[]):
            source=bpy.data.objects.get(str(inst.get('Tumblify_SourceName',''))) or a._instance_result_helper(inst)
            if source is None:continue
            for pose,factor in a._instance_result_entries(inst):
                q=Quaternion((pose[6],pose[3],pose[4],pose[5]));matrix=Matrix.LocRotScale(Vector(pose[:3]),q,Vector(source.scale)*factor)
                self.static.append(_record(a,source,matrix))
        self.static_arrays=self.arrays(self.static)

    def arrays(self,records):
        if np is None or not records:return None
        return (np.asarray([r[3] for r in records]),np.asarray([r[4] for r in records]))

    def candidates(self,records,arrays,origin,direction):
        if arrays is not None:
            lo,hi=arrays;o=np.asarray(origin);d=np.asarray(direction);parallel=np.abs(d)<1e-12
            inv=np.divide(1.0,d,out=np.full(3,1e30),where=~parallel)
            t0=(lo-o)*inv;t1=(hi-o)*inv;near=np.minimum(t0,t1).max(axis=1);far=np.maximum(t0,t1).min(axis=1)
            valid=(far>=np.maximum(near,0))&~np.any(parallel&((o<lo)|(o>hi)),axis=1)
            ids=np.flatnonzero(valid);return [(max(0,float(near[i])),records[int(i)]) for i in ids[np.argsort(near[ids])]]
        out=[]
        for rec in records:
            near=0;far=1e30
            for i in range(3):
                if abs(direction[i])<1e-12:
                    if origin[i]<rec[3][i] or origin[i]>rec[4][i]:far=-1;break
                else:
                    p=(rec[3][i]-origin[i])/direction[i];q=(rec[4][i]-origin[i])/direction[i];near=max(near,min(p,q));far=min(far,max(p,q))
            if far>=near:out.append((near,rec))
        return sorted(out,key=lambda v:v[0])

    def cast(self,context,origin,direction,orient_to_ray=True):
        from . import addon as a
        direction=Vector(direction).normalized();origin=Vector(origin);candidates=self.candidates(self.static,self.static_arrays,origin,direction)
        dynamic=[];owner=self.owner
        for i,pose in enumerate(owner.dynamic_cache):
            if i>=len(owner.dynamic_source_names):break
            source=bpy.data.objects.get(owner.dynamic_source_names[i])
            if source is None:continue
            q=Quaternion((pose[6],pose[3],pose[4],pose[5]));factor=owner.dynamic_scales[i]
            matrix=Matrix.LocRotScale(Vector(pose[:3]),q,Vector(source.scale)*factor)
            dynamic.append(_record(a,source,matrix))
        candidates+=self.candidates(dynamic,self.arrays(dynamic),origin,direction);candidates.sort(key=lambda v:v[0])
        best=None;distance=float('inf')
        enabled,height=getattr(owner,'fast_snap_floor_config',a._builtin_floor_config(context.scene.tumblify,fast=True))
        if enabled and abs(direction.z)>1e-9:
            t=(height-origin.z)/direction.z
            if t>=0:best=(origin+direction*t,Z.copy() if direction.z<0 else -Z,None);distance=t
        best_raw=best[1].copy() if best else None
        for near,(source,matrix,inv,lo,hi) in candidates:
            if near>distance:break
            key=source.as_pointer()
            evaluated=source.evaluated_get(owner.depsgraph)
            try:
                local_direction=inv.to_3x3()@direction
                if local_direction.length_squared<1e-20:continue
                hit,point,normal,index=evaluated.ray_cast(inv@origin,local_direction.normalized())
            except (RuntimeError,ReferenceError):continue
            if not hit:continue
            point=matrix@point;depth=(point-origin).dot(direction)
            if depth<0 or depth>=distance:continue
            normal=matrix.to_3x3().inverted_safe().transposed()@normal
            if normal.length_squared<1e-20:continue
            normal.normalize()
            raw=normal.copy()
            if orient_to_ray and normal.dot(direction)>0:normal.negate()
            best=(point,normal,source);best_raw=raw;distance=depth
        self.last_raw_normal=best_raw
        return best


    def cast_near(self,context,point,normal,reach,source_hint=None,winding=1):
        """Find the closest supporting surface near this plane, not the highest shelf."""
        point=Vector(point);normal=Vector(normal).normalized()
        origin=point+normal*reach;best=None;best_distance=float('inf')
        epsilon=max(1e-6,reach*1e-6)
        for _ in range(64):
            hit=self.cast(context,origin,-normal,orient_to_ray=False)
            if hit is None:break
            position,n,source=hit;height=(position-point).dot(normal)
            if height < -reach:break
            if source==source_hint and winding<0:n=-n
            supporting=n.dot(normal)>.05
            distance=abs(height)
            if supporting and distance<best_distance:
                best=(position,n,source);best_distance=distance
            if best_distance<=epsilon or (height<0 and best is not None and distance>=best_distance+epsilon):break
            origin=position-normal*epsilon
        return best


def rebuild(owner,context):
    if not getattr(owner,'fast_snap_valid',False):return
    point=owner.fast_snap_point;normal=owner.fast_snap_normal;align=Z.rotation_difference(normal)
    st=context.scene.tumblify;specs=[];alignments=[];used_members=[]
    if getattr(owner,'fast_snap_picker',None) is None:owner.fast_snap_picker=SurfacePicker(owner,context)
    for column_index,(column,sample) in enumerate(zip(owner.fast_batch_members,owner.fast_batch_samples)):
        offset=Vector((0,0,0)) if column_index==0 else align@sample*st.fast_spread
        offset-=normal*offset.dot(normal)
        base=point+offset;n=normal
        if offset.length_squared>1e-12:
            reach=max(.05,st.fast_spread*2+.025)
            hit=owner.fast_snap_picker.cast_near(context,base,normal,reach,getattr(owner,'fast_snap_object',None),getattr(owner,'fast_snap_winding_sign',1))
            if hit is None:continue
            base,n,_=hit
        qa=Z.rotation_difference(n);top=None
        for member in column:
            source,quat,random_factor=member
            q=qa@quat;factor=owner.fast_preview_scale*random_factor
            bounds=getattr(owner,'fast_snap_bounds',{}).get(source.name,source.bound_box)
            p=box_contact(source,q,factor,base,n,bounds)+n*.0001
            scale=Vector(source.scale)*factor
            values=[(p+q@Vector((c[0]*scale.x,c[1]*scale.y,c[2]*scale.z))).dot(n) for c in bounds]
            lo,hi=min(values),max(values)
            if top is not None:p+=n*(top+.005-lo);hi+=top+.005-lo
            top=hi;specs.append((source,p,q,factor));alignments.append(qa.copy());used_members.append(member)
    owner.fast_stack_specs=specs;owner.fast_snap_alignments=alignments;owner.fast_snap_members_used=used_members
    owner.fast_spread_offset=Vector((0,0,0))
    ghosts=[]
    for source,p,q,factor in specs:ghosts.append(owner._fast_preview_acquire_visual(context,source,p,q,factor))
    owner.fast_stack_ghosts=ghosts;owner.fast_preview_obj=ghosts[0] if ghosts else None


def refresh(owner,context,event=None):
    if event is not None:owner.fast_snap_mouse=(event.mouse_region_x,event.mouse_region_y)
    coord=getattr(owner,'fast_snap_mouse',None)
    if coord is None:return
    region=owner._region;rv3d=owner._rv3d
    if not (0<=coord[0]<region.width and 0<=coord[1]<region.height):
        owner.fast_snap_valid=False;return
    if getattr(owner,'fast_snap_picker',None) is None:owner.fast_snap_picker=SurfacePicker(owner,context)
    origin=view3d_utils.region_2d_to_origin_3d(region,rv3d,coord,clamp=max(100,rv3d.view_distance*10))
    direction=view3d_utils.region_2d_to_vector_3d(region,rv3d,coord)
    hit=owner.fast_snap_picker.cast(context,origin,direction)
    was_valid=getattr(owner,'fast_snap_valid',False)
    old_point=getattr(owner,'fast_snap_point',None);old_normal=getattr(owner,'fast_snap_normal',None)
    owner.fast_snap_valid=hit is not None
    owner.fast_snap_last_refresh=time.monotonic()
    if hit is None:
        owner._fast_preview_used=set();owner._fast_preview_finish_visuals();return
    owner.fast_snap_point,owner.fast_snap_normal,owner.fast_snap_object=hit
    raw=getattr(owner.fast_snap_picker,'last_raw_normal',None)
    owner.fast_snap_winding_sign=-1 if raw is not None and raw.dot(hit[1])<0 else 1
    owner.fast_preview_target=owner.fast_snap_point.copy()
    from . import addon as a
    empty,ray=a._fast_ensure_cursor_helpers(context.scene)
    empty.location=owner.fast_preview_target
    if not empty.hide_get():empty.hide_set(True)
    if not ray.hide_get():ray.hide_set(True)
    if was_valid and old_point is not None and context.scene.tumblify.fast_spread==0 and (old_point-hit[0]).length_squared<1e-12 and (old_normal-hit[1]).length_squared<1e-12:return
    owner._fast_preview_used=set();rebuild(owner,context);owner._fast_preview_finish_visuals()
    owner.fast_snap_last_refresh=time.monotonic()


def rotate(owner,emitter_q):
    initial=owner.fast_stack_rotate_initial;start=owner.fast_native_rotate_start_q
    delta=emitter_q@start.inverted();alignments=owner.fast_snap_rotate_alignments
    main=owner.fast_snap_rotate_main_align;world_delta=main@delta@main.inverted()
    for member,spec,qa in zip(owner.fast_snap_rotate_members,initial,alignments):member[1]=qa.inverted()@world_delta@spec[2]
    owner.fast_batch_samples=[delta@v for v in owner.fast_stack_rotate_samples]
    owner._fast_preview_used=set();rebuild(owner,bpy.context);owner._fast_preview_finish_visuals()


def draw(owner):
    if not getattr(owner,'fast_snap_enabled',False) or not getattr(owner,'fast_snap_valid',False):return
    region=owner._region;rv3d=owner._rv3d;p=owner.fast_snap_point;n=owner.fast_snap_normal
    centre=view3d_utils.location_3d_to_region_2d(region,rv3d,p)
    if centre is None:return
    other=view3d_utils.region_2d_to_location_3d(region,rv3d,(centre.x+16,centre.y),p)
    radius=(other-p).length;u=n.orthogonal().normalized();v=n.cross(u).normalized();lines=[]
    for i in range(48):
        for t in (i/48,(i+1)/48):
            point=view3d_utils.location_3d_to_region_2d(region,rv3d,p+radius*(u*math.cos(t*math.tau)+v*math.sin(t*math.tau)))
            if point is not None:lines.append(tuple(point))
    for axis in (u,v):
        for sign in (-1,1):
            point=view3d_utils.location_3d_to_region_2d(region,rv3d,p+axis*radius*.7*sign)
            if point is not None:lines.append(tuple(point))
    if len(lines)%2:return
    shader=gpu.shader.from_builtin('UNIFORM_COLOR');gpu.state.blend_set('ALPHA')
    try:
        gpu.state.line_width_set(2);shader.bind();shader.uniform_float('color',(.2,1,.55,1));batch_for_shader(shader,'LINES',{'pos':lines}).draw(shader)
    finally:gpu.state.line_width_set(1);gpu.state.blend_set('NONE')
