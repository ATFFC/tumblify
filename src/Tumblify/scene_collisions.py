# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Explicit Fast Scene preparation and a bounded, batched viewport wire preview."""
import bpy, time, math, gpu
from mathutils import Vector, Quaternion, Matrix
from gpu_extras.batch import batch_for_shader
from bpy.app.handlers import persistent

_CACHES = {}
_INVALIDATION_REASONS = {}
_JOB = None
_WIRE = None
_WIRE_SHADER = None
_HANDLE = None
_WIRE_SCENE = None
_WIRE_TOKEN = None
_WIRE_VERTICES = 0
MAX_PREVIEW_EDGES = 120000


def _addon():
    from . import addon
    return addon


def global_settings(scene):
    st=scene.tumblify
    return tuple(getattr(st,key) for key in ('convex_simplify','compound_max_hulls','loose_max_parts','loose_auto_split_connected','loose_split_angle','auto_fit_tolerance'))+(tuple(o.as_pointer() for o in _addon()._fast_source_objects(scene)),)


def get_scene_cache(scene):
    rec=_CACHES.get(scene.as_pointer())
    if rec is not None and rec['settings']!=global_settings(scene):
        invalidate_scene(scene)
        return None
    return rec


def invalidate_scene(scene,reason='configuration'):
    _INVALIDATION_REASONS[scene.as_pointer()]=reason
    _CACHES.pop(scene.as_pointer(),None)
    if _WIRE_SCENE==scene.as_pointer():clear_wire()


def clear_wire():
    global _WIRE,_HANDLE,_WIRE_SCENE,_WIRE_VERTICES,_WIRE_TOKEN
    if _HANDLE is not None:
        try:bpy.types.SpaceView3D.draw_handler_remove(_HANDLE,'WINDOW')
        except (ValueError,RuntimeError):pass
    _WIRE=None;_HANDLE=None;_WIRE_SCENE=None;_WIRE_VERTICES=0;_WIRE_TOKEN=None


def clear_scene_caches():
    _CACHES.clear();clear_wire()
    if _JOB is not None:_JOB.cancel(bpy.context)


def validate_scene_cache(scene):
    a=_addon();rec=get_scene_cache(scene)
    if rec is None:return None
    # Cheap membership validation before starting a new world; never evaluates meshes.
    current=a._static_collider_candidates(scene,scene.objects,a._fast_source_objects(scene))
    if {o.as_pointer() for o in current}!=rec['members']:
        invalidate_scene(scene);return None
    return rec


def cached_static_data(scene,obj):
    rec=get_scene_cache(scene)
    data=rec['prepared'].get(obj.as_pointer()) if rec is not None else None
    # Native hull handles belong to one World; never store them in the session snapshot.
    return {key:value for key,value in data.items() if key not in {'hull_asset','native_mixed_parts','compound_assets'}} if data is not None else None


def preview_objects(scene,sources):
    rec=get_scene_cache(scene)
    if rec is None:return []
    excluded=set(sources)
    return [o for o in rec['objects'] if _addon()._is_live_object(o) and o not in excluded]


def _draw_wire():
    if _WIRE is None:return
    context=bpy.context;scene=context.scene
    if scene is None or scene.as_pointer()!=_WIRE_SCENE:return
    if scene.tumblify.mode!='FAST' or scene.tumblify.fast_static_scope!='SCENE' or not scene.tumblify.show_collision_debug:return
    shader=_WIRE_SHADER
    gpu.state.depth_test_set('LESS_EQUAL');gpu.state.blend_set('ALPHA')
    try:
        shader.bind();shader.uniform_float('color',(1.0,.25,.06,.8));_WIRE.draw(shader)
    finally:
        gpu.state.depth_test_set('NONE');gpu.state.blend_set('NONE')


def build_scene_wire(scene,objects,progress=None):
    """Use prepared physical geometry; sample wire edges for viewport display only."""
    global _WIRE,_HANDLE,_WIRE_SCENE,_WIRE_VERTICES,_WIRE_SHADER,_WIRE_TOKEN
    token=(scene.as_pointer(),id(get_scene_cache(scene)),tuple(obj.as_pointer() for obj in objects))
    if _WIRE_TOKEN==token:return _WIRE_VERTICES//2
    clear_wire();a=_addon();points=[]
    budget=max(6,MAX_PREVIEW_EDGES//max(1,len(objects)))
    for index,obj in enumerate(objects):
        data=cached_static_data(scene,obj)
        if not data:continue
        p,q,_=a.world_pose_scale(obj)
        transform=Matrix.Translation(Vector(p))@Quaternion((q[3],q[0],q[1],q[2])).to_matrix().to_4x4()
        if data['shape'] in {'MESH','TERRAIN'}:
            verts=data['verts'];indices=data['indices'];count=len(indices)//3
            stride=max(1,math.ceil(count/max(1,budget//3)))
            faces=(indices[i*3:i*3+3] for i in range(0,count,stride))
            groups=[(verts,faces)]
        else:
            groups=[(part.get('points',[]),part.get('faces',[])) for part in a._data_as_compound_parts(data)]
        remaining=budget
        for verts,faces in groups:
            edges=set()
            for face in faces:
                for i in range(len(face)):
                    edge=tuple(sorted((int(face[i]),int(face[(i+1)%len(face)]))))
                    edges.add(edge)
                    if len(edges)>=remaining:break
                if len(edges)>=remaining:break
            for i,j in sorted(edges):
                if len(points)//2>=MAX_PREVIEW_EDGES:break
                points.extend((tuple(transform@Vector(verts[i])),tuple(transform@Vector(verts[j]))))
            remaining-=len(edges)
            if remaining<=0:break
        if progress:progress.update(100*(index+1)/max(1,len(objects)),f'Collision preview {index+1}/{len(objects)}')
    _WIRE_SCENE=scene.as_pointer();_WIRE_VERTICES=len(points);_WIRE_TOKEN=token
    if points and not bpy.app.background:
        _WIRE_SHADER=gpu.shader.from_builtin('UNIFORM_COLOR');_WIRE=batch_for_shader(_WIRE_SHADER,'LINES',{'pos':points})
        _HANDLE=bpy.types.SpaceView3D.draw_handler_add(_draw_wire,(),'WINDOW','POST_VIEW')
    return len(points)//2


def _empty_skipped_object(rec, obj, depsgraph):
    """An ignored instance-only/empty emitter is not a physical collider.

    Geometry Nodes may reevaluate it whenever debug helpers are linked. Keep
    the cache while it still has no faces; real newly generated faces invalidate.
    """
    if rec is None or obj.as_pointer() not in rec.get('empty_skipped', set()):
        return False
    try:
        evaluated = obj.evaluated_get(depsgraph)
        return evaluated.type == 'MESH' and len(evaluated.data.polygons) == 0
    except (ReferenceError, RuntimeError):
        return False


@persistent
def invalidate_updated_inputs(scene,depsgraph):
    rec=_CACHES.get(scene.as_pointer())
    job=_JOB if _JOB is not None and _JOB.scene==scene else None
    if rec is None and job is None:return
    if job is not None and getattr(job,'processing',False):return
    members=rec['members'] if rec else job.members
    meshes=rec['meshes'] if rec else job.meshes
    a=_addon()
    for update in depsgraph.updates:
        item=update.id;item=getattr(item,'original',item)
        if isinstance(item,bpy.types.Object):
            if item.as_pointer() in members and (update.is_updated_geometry or update.is_updated_transform):
                if _empty_skipped_object(rec, item, depsgraph):continue
                invalidate_scene(scene,'Updated '+item.name)
                if job is not None:job.dirty=True;job.dirty_reason=getattr(item,'name','input')
                return
            if item.type=='MESH' and item.as_pointer() not in members and a._static_collider_candidates(scene,[item],a._fast_source_objects(scene)):
                invalidate_scene(scene,'New input '+item.name)
                if job is not None:job.dirty=True;job.dirty_reason=item.name
                return
        elif isinstance(item,bpy.types.Mesh) and item.as_pointer() in meshes:
            owners=[obj for obj in rec.get('input_objects', []) if obj.data == item] if rec else []
            if owners and all(_empty_skipped_object(rec, obj, depsgraph) for obj in owners):continue
            invalidate_scene(scene,'Updated mesh '+item.name)
            if job is not None:job.dirty=True;job.dirty_reason=getattr(item,'name','input')
            return


class TU_OT_prepare_scene(bpy.types.Operator):
    bl_idname='tumblify.prepare_scene';bl_label='Prepare Scene'
    bl_description='Prepare Fast Scene collider geometry explicitly; reuse it until inputs change'

    @classmethod
    def poll(cls,context):return _JOB is None and _addon()._RUNTIME is None

    def _begin(self,context):
        global _JOB
        a=_addon();self.scene=context.scene;invalidate_scene(self.scene)
        self.objects=a._static_collider_candidates(self.scene,self.scene.objects,a._fast_source_objects(self.scene))
        self.members={o.as_pointer() for o in self.objects};self.meshes={o.data.as_pointer() for o in self.objects}
        self.index=0;self.prepared={};self.accepted=[];self.skipped=[];self.dirty=False;self.dirty_reason='';self.processing=False;self.done=False
        self.settings=global_settings(self.scene);self.dg=context.evaluated_depsgraph_get()
        self.progress=a._RealtimePreparation(context,'Fast Scene',action='Preparing scene',synchronous=False)
        self.progress.begin();_JOB=self

    def _step(self,context):
        a=_addon();deadline=time.monotonic()+.025
        while self.index<len(self.objects) and time.monotonic()<deadline:
            obj=self.objects[self.index]
            self.progress.update(95*self.index/max(1,len(self.objects)),f'Colliders {self.index+1}/{len(self.objects)} | {obj.name}')
            self.processing=True
            try:
                data=a._prepare_collision(obj,self.dg,self.scene.tumblify.convex_simplify,shape_override=a._fast_static_collision_fit(obj,self.dg))
                self.prepared[obj.as_pointer()]=data;self.accepted.append(obj)
            except a.CollisionGeometryError as ex:self.skipped.append({'name':obj.name,'reason':str(ex)})
            finally:self.processing=False
            self.index+=1
        if self.index>=len(self.objects) or getattr(self,'modal_job',False):
            # Geometry extraction can temporarily toggle modifiers on the source.
            # Consume those owned dependency updates before publishing/yielding.
            self.processing=True
            try:self.dg.update()
            finally:self.processing=False
        if self.index<len(self.objects):return {'RUNNING_MODAL'}
        if self.dirty or self.settings!=global_settings(self.scene):
            self.report({'WARNING'},'Scene inputs changed during preparation: '+self.dirty_reason+'. Prepare Scene again.');self.cancel(context);return {'CANCELLED'}
        _CACHES[self.scene.as_pointer()]={'members':self.members,'meshes':self.meshes,'objects':self.accepted,'prepared':self.prepared,'skipped':self.skipped,'settings':self.settings,'input_objects':list(self.objects),'empty_skipped':{obj.as_pointer() for obj in self.objects if obj.as_pointer() not in self.prepared and len(obj.evaluated_get(self.dg).data.polygons)==0}}
        self.progress.update(100,f'Ready: {len(self.accepted)} colliders',force=True)
        self._close(context)
        if self.scene.tumblify.show_collision_debug:a._refresh_collision_debug(self.scene)
        self.report({'INFO'},f'Scene prepared: {len(self.accepted)} colliders, {len(self.skipped)} skipped')
        return {'FINISHED'}

    def _close(self,context):
        global _JOB
        if self.done:return
        self.done=True
        if getattr(self,'timer',None) is not None:
            context.window_manager.event_timer_remove(self.timer);self.timer=None
        self.progress.close();_JOB=None;_addon()._tag_view3d_redraw_all()

    def cancel(self,context):
        invalidate_scene(self.scene);self._close(context)

    def execute(self,context):
        self._begin(context)
        try:
            while True:
                result=self._step(context)
                if result!={'RUNNING_MODAL'}:return result
        except Exception as ex:
            self.cancel(context);self.report({'ERROR'},str(ex));return {'CANCELLED'}

    def invoke(self,context,event):
        self._begin(context);self.modal_job=True;self.timer=context.window_manager.event_timer_add(.01,window=context.window)
        context.window_manager.modal_handler_add(self);return {'RUNNING_MODAL'}

    def modal(self,context,event):
        if self.done:return {'CANCELLED'}
        if event.type=='ESC':self.cancel(context);return {'CANCELLED'}
        if event.type=='TIMER':
            try:return self._step(context)
            except Exception as ex:
                self.cancel(context);self.report({'ERROR'},str(ex));return {'CANCELLED'}
        return {'RUNNING_MODAL'}
