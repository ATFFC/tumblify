# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

import ctypes, os
from array import array
class CollisionGeometryError(ValueError):
    """Unsupported collider geometry; callers may skip automatic Scene candidates."""


_C_INT_MIN = -(2**31)
_C_INT_MAX = 2**31 - 1

def _checked_c_int(value, name='value'):
    v = int(value)
    if v < _C_INT_MIN or v > _C_INT_MAX:
        raise OverflowError(f"{name}={v} is outside signed 32-bit C int range")
    return v



class Box3DLib:
    def __init__(self):
        base=os.path.dirname(__file__)
        names=[os.path.join(base,'native','bin','box3d_blender.dll'), os.path.join(base,'native','bin','libbox3d_blender.so'), os.path.join(base,'native','bin','libbox3d_blender.dylib')]
        path=next((p for p in names if os.path.exists(p)),None)
        if not path: raise RuntimeError(f"Native Box3D bridge not found in {os.path.join(base,'native','bin')}. Reinstall the Tumblify release package, then restart Blender.")
        self.lib=ctypes.CDLL(path)
        L=self.lib
        if not hasattr(L,'bp_add_heightfield_static'):raise RuntimeError('This Tumblify build requires its matching native DLL. Install the complete ZIP and restart Blender.')
        L.bp_version.restype=ctypes.c_char_p; L.bp_error.restype=ctypes.c_char_p
        L.bp_create_world.argtypes=[ctypes.c_int]; L.bp_create_world.restype=ctypes.c_void_p
        L.bp_destroy_world.argtypes=[ctypes.c_void_p]; L.bp_worker_count.argtypes=[ctypes.c_void_p]; L.bp_worker_count.restype=ctypes.c_int
        self.has_self_collision=hasattr(L,'bp_set_self_collision')
        if self.has_self_collision:
            L.bp_set_self_collision.argtypes=[ctypes.c_void_p,ctypes.c_int]; L.bp_set_self_collision.restype=ctypes.c_int
        self.has_gravity_control=hasattr(L,'bp_set_gravity')
        if self.has_gravity_control:
            L.bp_set_gravity.argtypes=[ctypes.c_void_p,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_set_gravity.restype=ctypes.c_int
        self.has_solver_tuning=hasattr(L,'bp_set_solver_tuning')
        if self.has_solver_tuning:
            L.bp_set_solver_tuning.argtypes=[ctypes.c_void_p,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_set_solver_tuning.restype=ctypes.c_int
        self.has_body_bullet=hasattr(L,'bp_set_body_bullet')
        if self.has_body_bullet:
            L.bp_set_body_bullet.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int]; L.bp_set_body_bullet.restype=ctypes.c_int
        self.has_contact_recycling=hasattr(L,'bp_set_body_contact_recycling')
        if self.has_contact_recycling:
            L.bp_set_body_contact_recycling.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int]; L.bp_set_body_contact_recycling.restype=ctypes.c_int
        self.has_forced_sleep=hasattr(L,'bp_set_forced_sleep')
        if self.has_forced_sleep:
            L.bp_set_forced_sleep.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_set_forced_sleep.restype=ctypes.c_int
        self.has_native_sleep=hasattr(L,'bp_set_native_sleep')
        if self.has_native_sleep:
            L.bp_set_native_sleep.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_float]; L.bp_set_native_sleep.restype=ctypes.c_int
        L.bp_add_floor.argtypes=[ctypes.c_void_p,ctypes.c_float,ctypes.c_float]; L.bp_add_floor.restype=ctypes.c_int
        common=[ctypes.c_void_p,ctypes.c_int]+[ctypes.c_float]*7
        L.bp_add_box.argtypes=common+[ctypes.c_float]*9; L.bp_add_box.restype=ctypes.c_int
        L.bp_add_sphere.argtypes=common+[ctypes.c_float]*7; L.bp_add_sphere.restype=ctypes.c_int
        L.bp_add_capsule_axis.argtypes=common+[ctypes.c_float]*5+[ctypes.c_int]+[ctypes.c_float]*3; L.bp_add_capsule_axis.restype=ctypes.c_int
        L.bp_add_cylinder_axis.argtypes=common+[ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_int,ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_add_cylinder_axis.restype=ctypes.c_int
        L.bp_add_hull.argtypes=common+[ctypes.POINTER(ctypes.c_float),ctypes.c_int,ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_add_hull.restype=ctypes.c_int
        L.bp_create_hull_asset.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_float),ctypes.c_int,ctypes.c_int]; L.bp_create_hull_asset.restype=ctypes.c_int
        L.bp_add_hull_asset.argtypes=common+[ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_add_hull_asset.restype=ctypes.c_int
        self.has_compound=hasattr(L,'bp_add_compound_hull_assets')
        if self.has_compound:
            L.bp_add_compound_hull_assets.argtypes=common+[ctypes.POINTER(ctypes.c_int),ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_add_compound_hull_assets.restype=ctypes.c_int
        self.has_mixed_compound=hasattr(L,'bp_add_compound_mixed')
        if self.has_mixed_compound:
            L.bp_add_compound_mixed.argtypes=common+[ctypes.POINTER(ctypes.c_int),ctypes.POINTER(ctypes.c_int),ctypes.POINTER(ctypes.c_float),ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_add_compound_mixed.restype=ctypes.c_int
        L.bp_add_mesh_static.argtypes=[ctypes.c_void_p]+[ctypes.c_float]*7+[ctypes.POINTER(ctypes.c_float),ctypes.c_int,ctypes.POINTER(ctypes.c_int32),ctypes.c_int,ctypes.c_float,ctypes.c_float]; L.bp_add_mesh_static.restype=ctypes.c_int
        L.bp_add_heightfield_static.argtypes=[ctypes.c_void_p]+[ctypes.c_float]*7+[ctypes.POINTER(ctypes.c_float),ctypes.POINTER(ctypes.c_uint8),ctypes.c_int,ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float]
        L.bp_add_heightfield_static.restype=ctypes.c_int
        L.bp_set_target.argtypes=[ctypes.c_void_p,ctypes.c_int]+[ctypes.c_float]*8; L.bp_set_target.restype=ctypes.c_int
        L.bp_set_transform.argtypes=[ctypes.c_void_p,ctypes.c_int]+[ctypes.c_float]*7; L.bp_set_transform.restype=ctypes.c_int
        L.bp_set_enabled.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int]; L.bp_set_enabled.restype=ctypes.c_int
        L.bp_destroy_body.argtypes=[ctypes.c_void_p,ctypes.c_int]; L.bp_destroy_body.restype=ctypes.c_int
        L.bp_step.argtypes=[ctypes.c_void_p,ctypes.c_float,ctypes.c_int]; L.bp_step.restype=ctypes.c_int
        L.bp_get_transforms.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int),ctypes.c_int,ctypes.POINTER(ctypes.c_float),ctypes.POINTER(ctypes.c_uint8)]; L.bp_get_transforms.restype=ctypes.c_int
        L.bp_apply_radial_drag.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int),ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_apply_radial_drag.restype=ctypes.c_int
        self.has_point_springs=hasattr(L,'bp_apply_point_springs')
        if self.has_point_springs:
            L.bp_apply_point_springs.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int),ctypes.POINTER(ctypes.c_float),ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_float]; L.bp_apply_point_springs.restype=ctypes.c_int
        self.has_fast_drop=hasattr(L,'bp_drop_bake_begin') and hasattr(L,'bp_drop_bake_take_cache')
        self.has_disk_bake=hasattr(L,'bp_drop_bake_begin_disk')
        self.has_exact_frame_bake=hasattr(L,'bp_drop_bake_begin_cached')
        if self.has_fast_drop:
            L.bp_drop_bake_begin.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int),ctypes.c_int,ctypes.POINTER(ctypes.c_float),ctypes.c_float,ctypes.c_float,ctypes.c_float,ctypes.c_int,ctypes.c_float,ctypes.c_float,ctypes.c_int,ctypes.c_int,ctypes.c_float]; L.bp_drop_bake_begin.restype=ctypes.c_void_p
            if self.has_disk_bake:
                L.bp_drop_bake_begin_disk.argtypes=L.bp_drop_bake_begin.argtypes+[ctypes.c_char_p]
                L.bp_drop_bake_begin_disk.restype=ctypes.c_void_p
            if self.has_exact_frame_bake:
                L.bp_drop_bake_begin_cached.argtypes=L.bp_drop_bake_begin.argtypes+[ctypes.c_char_p,ctypes.c_int]
                L.bp_drop_bake_begin_cached.restype=ctypes.c_void_p
            L.bp_drop_bake_state.argtypes=[ctypes.c_void_p]; L.bp_drop_bake_state.restype=ctypes.c_int
            L.bp_drop_bake_progress.argtypes=[ctypes.c_void_p]; L.bp_drop_bake_progress.restype=ctypes.c_int
            L.bp_drop_bake_error.argtypes=[ctypes.c_void_p]; L.bp_drop_bake_error.restype=ctypes.c_char_p
            L.bp_drop_bake_cancel.argtypes=[ctypes.c_void_p]
            L.bp_drop_bake_get_transforms.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_float)]; L.bp_drop_bake_get_transforms.restype=ctypes.c_int
            L.bp_drop_bake_take_cache.argtypes=[ctypes.c_void_p]; L.bp_drop_bake_take_cache.restype=ctypes.c_void_p
            L.bp_drop_bake_destroy.argtypes=[ctypes.c_void_p]; L.bp_drop_bake_destroy.restype=None
            # Transform-cache ABI. ctypes defaults unspecified arguments to c_int,
            # which truncates/overflows 64-bit cache pointers on Windows.
            L.bp_cache_frame_count.argtypes=[ctypes.c_void_p]; L.bp_cache_frame_count.restype=ctypes.c_int
            L.bp_cache_body_count.argtypes=[ctypes.c_void_p]; L.bp_cache_body_count.restype=ctypes.c_int
            L.bp_cache_fps.argtypes=[ctypes.c_void_p]; L.bp_cache_fps.restype=ctypes.c_float
            L.bp_cache_get_body_track.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.POINTER(ctypes.c_float)]; L.bp_cache_get_body_track.restype=ctypes.c_int
            L.bp_cache_destroy.argtypes=[ctypes.c_void_p]; L.bp_cache_destroy.restype=None
    def version(self): return self.lib.bp_version().decode('utf8','replace')
    def error(self): return self.lib.bp_error().decode('utf8','replace')

_LIB=None
def get_lib():
    global _LIB
    if _LIB is None: _LIB=Box3DLib()
    return _LIB

class BulkTransformReader:
    def __init__(self,world,handles):
        self.world=world; self.handles=list(map(int,handles)); self.n=len(self.handles)
        self._hs=(ctypes.c_int*self.n)(*self.handles) if self.n else None
        self.out=(ctypes.c_float*max(1,7*self.n))(); self.awake=(ctypes.c_uint8*max(1,self.n))()
    def read(self):
        if not self.n: return self.out,self.awake
        if not self.world.api.lib.bp_get_transforms(self.world.ptr,self._hs,self.n,self.out,self.awake): raise RuntimeError('Box3D bulk transform read failed')
        return self.out,self.awake
    def snapshot_array(self):
        a=array('f')
        if self.n: a.frombytes(ctypes.string_at(ctypes.addressof(self.out),ctypes.sizeof(ctypes.c_float)*7*self.n))
        return a

class DropBakeJob:
    def __init__(self,world,handles,targets,spawn_rate,settle_time,physics_hz,multiplier,magnet_strength=55.0,magnet_damping=10.0,magnetized=False,capture_frames=False,output_fps=24.0,cache_path=None,output_frames=None):
        self.world=world; self.api=world.api; self.ptr=None; self.count=len(handles)
        if not getattr(self.api,'has_fast_drop',False): raise RuntimeError('Native Offline Drop requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
        if self.count > _C_INT_MAX:
            raise OverflowError(f'body count={self.count} is outside signed 32-bit C int range')
        checked_handles=[_checked_c_int(h, f'body handle[{i}]') for i,h in enumerate(handles)]
        hs=(ctypes.c_int*self.count)(*checked_handles); self._handles=hs
        flat=[]
        if targets:
            for p in targets:
                if p is None: flat.extend((0.0,0.0,0.0))
                else: flat.extend(float(x) for x in p[:3])
        self._targets=(ctypes.c_float*len(flat))(*flat) if flat else None
        count_i=_checked_c_int(self.count,'body count')
        mult_i=_checked_c_int(multiplier,'frequency multiplier')
        arguments=(world.ptr,self._handles,count_i,self._targets,float(spawn_rate),float(settle_time),float(physics_hz),mult_i,float(magnet_strength),float(magnet_damping),int(bool(magnetized)),int(bool(capture_frames)),float(output_fps))
        if output_frames is not None:
            if not capture_frames or not getattr(self.api,'has_exact_frame_bake',False):
                raise RuntimeError('Emitter Bake requires its updated native DLL for exact frame ranges. Install the complete release and restart Blender.')
            frames_i=_checked_c_int(output_frames,'output frame count')
            if frames_i<1:raise ValueError('Output frame count must be positive')
            self._cache_path=os.fspath(cache_path).encode('utf-8') if cache_path is not None else None
            self.ptr=self.api.lib.bp_drop_bake_begin_cached(*arguments,self._cache_path,frames_i)
        elif cache_path is not None:
            if not capture_frames or not getattr(self.api,'has_disk_bake',False):
                raise RuntimeError('Disk Emitter Bake requires its rebuilt native DLL and frame capture. Install the complete release and restart Blender.')
            self._cache_path=os.fspath(cache_path).encode('utf-8')
            self.ptr=self.api.lib.bp_drop_bake_begin_disk(*arguments,self._cache_path)
        else:
            self.ptr=self.api.lib.bp_drop_bake_begin(*arguments)
        if not self.ptr: raise RuntimeError(self.api.error() or 'Could not start native Offline Drop')
    @property
    def state(self): return int(self.api.lib.bp_drop_bake_state(self.ptr)) if self.ptr else -1
    @property
    def progress(self): return int(self.api.lib.bp_drop_bake_progress(self.ptr)) if self.ptr else 0
    @property
    def error(self):
        if not self.ptr: return ''
        p=self.api.lib.bp_drop_bake_error(self.ptr); return p.decode('utf8','replace') if p else ''
    def cancel(self):
        if self.ptr: self.api.lib.bp_drop_bake_cancel(self.ptr)
    def transforms(self):
        if not self.ptr or self.state!=1: raise RuntimeError('Offline Drop job is not complete')
        out=(ctypes.c_float*max(1,7*self.count))()
        if not self.api.lib.bp_drop_bake_get_transforms(self.ptr,out): raise RuntimeError('Could not read Offline Drop transforms')
        return [[out[7*i+j] for j in range(7)] for i in range(self.count)]
    def take_cache(self):
        if not self.ptr or self.state!=1: return None
        p=self.api.lib.bp_drop_bake_take_cache(self.ptr)
        return TransformCache(self.api,p) if p else None
    def close(self):
        if self.ptr:
            self.api.lib.bp_drop_bake_destroy(self.ptr); self.ptr=None
    def __del__(self): self.close()

class TransformCache:
    def __init__(self, api, ptr):
        self.api=api; self.ptr=ptr
        if not self.ptr: raise RuntimeError('Invalid Box3D transform cache')
        self.frame_count=int(api.lib.bp_cache_frame_count(ptr))
        self.body_count=int(api.lib.bp_cache_body_count(ptr))
        self.fps=float(api.lib.bp_cache_fps(ptr))
    def close(self):
        if self.ptr:
            self.api.lib.bp_cache_destroy(self.ptr); self.ptr=None
    def __del__(self): self.close()
    def body_track(self,index):
        if not self.ptr: raise RuntimeError('Transform cache is closed')
        n=7*self.frame_count; out=(ctypes.c_float*max(1,n))()
        if not 0 <= int(index) < self.body_count: raise IndexError('Body index is outside the bake cache')
        if not self.api.lib.bp_cache_get_body_track(self.ptr,int(index),out): raise RuntimeError(self.api.error() or 'Box3D cache body-track read failed')
        return out

class World:
    STATIC=0; KINEMATIC=1; DYNAMIC=2
    def __init__(self,threads=0):
        self.api=get_lib(); self.ptr=self.api.lib.bp_create_world(int(threads))
        if not self.ptr: raise RuntimeError('Could not create Box3D world')
    def close(self):
        if self.ptr: self.api.lib.bp_destroy_world(self.ptr); self.ptr=None
    def __del__(self): self.close()
    @property
    def workers(self): return self.api.lib.bp_worker_count(self.ptr)
    def set_solver_tuning(self,scale=1.0,hertz=30.0,damping=10.0,push_speed=3.0,recycle_distance=0.05):
        if not getattr(self.api,'has_solver_tuning',False):
            if abs(float(scale)-1.0)>1e-6 or abs(float(hertz)-30.0)>1e-6 or abs(float(damping)-10.0)>1e-6 or abs(float(push_speed)-3.0)>1e-6 or abs(float(recycle_distance)-0.05)>1e-6:
                raise RuntimeError('Precision solver tuning requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
            return True
        ok=self.api.lib.bp_set_solver_tuning(self.ptr,float(scale),float(hertz),float(damping),float(push_speed),float(recycle_distance))
        if not ok: raise RuntimeError(self.api.error() or 'Box3D solver tuning failed')
        return True
    def set_bullet(self,handle,enabled=True):
        if not getattr(self.api,'has_body_bullet',False):
            if enabled: raise RuntimeError('Dynamic CCD requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
            return True
        return bool(self.api.lib.bp_set_body_bullet(self.ptr,int(handle),int(bool(enabled))))
    def set_contact_recycling(self,handle,enabled=True):
        if not getattr(self.api,'has_contact_recycling',False):
            if not enabled: raise RuntimeError('Per-body contact recycling control requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
            return True
        return bool(self.api.lib.bp_set_body_contact_recycling(self.ptr,int(handle),int(bool(enabled))))
    def set_self_collision(self,enabled=True):
        if not getattr(self.api,'has_self_collision',False):
            if not enabled:
                raise RuntimeError('Disabling Self Collision requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
            return True
        return bool(self.api.lib.bp_set_self_collision(self.ptr,int(bool(enabled))))
    def set_gravity(self,gravity):
        if not getattr(self.api,'has_gravity_control',False):
            return False
        x,y,z=gravity
        return bool(self.api.lib.bp_set_gravity(self.ptr,float(x),float(y),float(z)))
    def set_forced_sleep(self,enabled=True,linear=0.05,angular=0.15,delay=0.5):
        if not getattr(self.api,'has_forced_sleep',False):
            if enabled: raise RuntimeError('Forced Sleep requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
            return True
        return bool(self.api.lib.bp_set_forced_sleep(self.ptr,int(bool(enabled)),float(linear),float(angular),float(delay)))
    def set_native_sleep(self,enabled=True,threshold=0.05):
        if not getattr(self.api,'has_native_sleep',False):
            raise RuntimeError('Native Box3D sleep control requires the v0.13.82+ DLL. Reinstall the Tumblify release package and restart Blender.')
        return bool(self.api.lib.bp_set_native_sleep(self.ptr,int(bool(enabled)),float(threshold)))
    @staticmethod
    def pose(p,q): return [float(p[0]),float(p[1]),float(p[2]),float(q[0]),float(q[1]),float(q[2]),float(q[3])]
    def add_floor(self,z,friction=0.7): return self.api.lib.bp_add_floor(self.ptr,float(z),float(friction))
    def add_box(self,t,p,q,h,c=(0,0,0),friction=0.55,restitution=0.0,density=1.0): return self.api.lib.bp_add_box(self.ptr,int(t),*self.pose(p,q),*map(float,h),*map(float,c),float(friction),float(restitution),float(density))
    def add_sphere(self,t,p,q,r,c=(0,0,0),friction=0.55,restitution=0.0,density=1.0): return self.api.lib.bp_add_sphere(self.ptr,int(t),*self.pose(p,q),float(r),*map(float,c),float(friction),float(restitution),float(density))
    def add_capsule(self,t,p,q,halfseg,r,c=(0,0,0),axis=2,friction=0.55,restitution=0.0,density=1.0): return self.api.lib.bp_add_capsule_axis(self.ptr,int(t),*self.pose(p,q),float(halfseg),float(r),*map(float,c),int(axis),float(friction),float(restitution),float(density))
    def add_cylinder(self,t,p,q,halfheight,r,c=(0,0,0),sides=12,axis=2,friction=0.55,restitution=0.0,density=1.0): return self.api.lib.bp_add_cylinder_axis(self.ptr,int(t),*self.pose(p,q),float(halfheight),float(r),*map(float,c),int(sides),int(axis),float(friction),float(restitution),float(density))
    def create_hull_asset(self,points,max_vertices=240):
        flat=[float(c) for pt in points for c in pt]
        arr=(ctypes.c_float*len(flat))(*flat)
        h=self.api.lib.bp_create_hull_asset(self.ptr,arr,len(points),int(max_vertices))
        if h < 0: raise RuntimeError(self.api.error() or 'Box3D cached convex hull creation failed')
        return h
    def add_hull_asset(self,t,p,q,asset,scale=1.0,friction=0.55,restitution=0.0,density=1.0):
        h=self.api.lib.bp_add_hull_asset(self.ptr,int(t),*self.pose(p,q),int(asset),float(scale),float(friction),float(restitution),float(density))
        if h < 0: raise RuntimeError(self.api.error() or 'Box3D cached convex hull attach failed')
        return h
    def add_compound_hull_assets(self,t,p,q,assets,scale=1.0,friction=0.55,restitution=0.0,density=1.0):
        if not getattr(self.api,'has_compound',False):
            raise RuntimeError('Compound Hull requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
        vals=[int(a) for a in assets]
        if not vals: raise RuntimeError('Compound Hull has no convex parts')
        arr=(ctypes.c_int*len(vals))(*vals)
        h=self.api.lib.bp_add_compound_hull_assets(self.ptr,int(t),*self.pose(p,q),arr,len(vals),float(scale),float(friction),float(restitution),float(density))
        if h < 0: raise RuntimeError(self.api.error() or 'Box3D Compound Hull attach failed')
        return h
    def add_compound_mixed(self,t,p,q,parts,scale=1.0,friction=0.55,restitution=0.0,density=1.0):
        if not getattr(self.api,'has_mixed_compound',False):
            raise RuntimeError('Mixed Manual Compound requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
        if not parts:
            raise RuntimeError('Mixed Manual Compound has no parts')
        kind_map={'BOX':0,'SPHERE':1,'CAPSULE':2,'CYLINDER':3,'HULL':4}
        kinds=[]; assets=[]; data=[]
        for part in parts:
            kind=str(part.get('kind','HULL')).upper()
            if kind not in kind_map: kind='HULL'
            kinds.append(kind_map[kind]); assets.append(int(part.get('asset',-1)))
            pos=part.get('pos',(0,0,0)); rot=part.get('rot',(0,0,0,1)); params=part.get('params',(0,0,0))
            data.extend([float(pos[0]),float(pos[1]),float(pos[2]),float(rot[0]),float(rot[1]),float(rot[2]),float(rot[3]),float(params[0]),float(params[1]),float(params[2])])
        ka=(ctypes.c_int*len(kinds))(*kinds); aa=(ctypes.c_int*len(assets))(*assets); da=(ctypes.c_float*len(data))(*data)
        h=self.api.lib.bp_add_compound_mixed(self.ptr,int(t),*self.pose(p,q),ka,aa,da,len(parts),float(scale),float(friction),float(restitution),float(density))
        if h < 0: raise RuntimeError(self.api.error() or 'Box3D mixed compound attach failed')
        return h
    def add_hull(self,t,p,q,points,max_vertices=64,friction=0.55,restitution=0.0,density=1.0):
        flat=[float(c) for pt in points for c in pt]
        arr=(ctypes.c_float*len(flat))(*flat)
        h=self.api.lib.bp_add_hull(self.ptr,int(t),*self.pose(p,q),arr,len(points),int(max_vertices),float(friction),float(restitution),float(density))
        if h < 0: raise RuntimeError(self.api.error() or 'Box3D convex hull creation failed')
        return h
    def add_heightfield_static(self,p,q,heights,materials,nx,nz,dx,dz,friction=0.55,restitution=0.0):
        if len(heights)!=nx*nz or len(materials)!=(nx-1)*(nz-1):raise ValueError('Invalid terrain dimensions')
        samples=(ctypes.c_float*len(heights))(*heights);cells=(ctypes.c_uint8*len(materials))(*materials)
        h=self.api.lib.bp_add_heightfield_static(self.ptr,*self.pose(p,q),samples,cells,nx,nz,dx,dz,friction,restitution)
        if h<0:raise RuntimeError(self.api.error() or 'Height field creation failed')
        return h

    def add_mesh_static(self,p,q,vertices,indices,friction=0.55,restitution=0.0):
        if len(vertices)<3 or len(indices)<3 or len(indices)%3:
            raise CollisionGeometryError('Mesh collision needs vertices and complete triangle faces')
        vertex_bytes=getattr(vertices,'packed_bytes',None)
        index_bytes=getattr(indices,'packed_bytes',None)
        if vertex_bytes is not None and index_bytes is not None:
            # Native code receives fresh input buffers; cached data cannot be modified by a world.
            verts=(ctypes.c_float*(len(vertex_bytes)//4)).from_buffer_copy(vertex_bytes)
            inds=(ctypes.c_int32*(len(index_bytes)//4)).from_buffer_copy(index_bytes)
        else:
            flat=[float(c) for pt in vertices for c in pt]
            verts=(ctypes.c_float*len(flat))(*flat)
            inds=(ctypes.c_int32*len(indices))(*[int(i) for i in indices])
        h=self.api.lib.bp_add_mesh_static(self.ptr,*self.pose(p,q),verts,len(vertices),inds,len(indices)//3,float(friction),float(restitution))
        if h < 0:
            message=self.api.error() or 'Box3D mesh creation failed'
            if message in {'Invalid mesh collision data','Box3D could not build triangle mesh'}:raise CollisionGeometryError(message)
            raise RuntimeError(message)
        return h
    def target(self,h,p,q,dt): return bool(self.api.lib.bp_set_target(self.ptr,int(h),*self.pose(p,q),float(dt)))
    def set_transform(self,h,p,q): return bool(self.api.lib.bp_set_transform(self.ptr,int(h),*self.pose(p,q)))
    def enabled(self,h,v): return bool(self.api.lib.bp_set_enabled(self.ptr,int(h),int(bool(v))))
    def destroy(self,h): return bool(self.api.lib.bp_destroy_body(self.ptr,int(h)))
    def step(self,dt,substeps=1): return bool(self.api.lib.bp_step(self.ptr,float(dt),max(1,int(substeps))))
    def radial_drag(self,handles,center,radius,strength=35.0,damping=5.0):
        if not handles or radius<=0.0: return 0
        n=len(handles); hs=(ctypes.c_int*n)(*map(int,handles))
        return int(self.api.lib.bp_apply_radial_drag(self.ptr,hs,n,float(center[0]),float(center[1]),float(center[2]),float(radius),float(strength),float(damping)))
    def point_springs(self,handles,targets,strength=55.0,damping=10.0,gravity_comp=1.0):
        if not getattr(self.api,'has_point_springs',False):
            raise RuntimeError('Magnetized Physics Scatter requires a rebuilt Box3D DLL. Reinstall the Tumblify release package and restart Blender.')
        if not handles or not targets: return 0
        n=min(len(handles),len(targets))
        hs=(ctypes.c_int*n)(*map(int,handles[:n]))
        flat=[float(c) for p in targets[:n] for c in p]
        pts=(ctypes.c_float*(3*n))(*flat)
        return int(self.api.lib.bp_apply_point_springs(self.ptr,hs,pts,n,float(strength),float(damping),float(gravity_comp)))
    def drop_bake_begin(self,handles,targets,spawn_rate,settle_time,physics_hz,multiplier=1,magnet_strength=55.0,magnet_damping=10.0,magnetized=False,capture_frames=False,output_fps=24.0,cache_path=None,output_frames=None):
        return DropBakeJob(self,handles,targets,spawn_rate,settle_time,physics_hz,multiplier,magnet_strength,magnet_damping,magnetized,capture_frames,output_fps,cache_path,output_frames)
    def bulk_reader(self,handles): return BulkTransformReader(self,handles)
    def get_bulk(self,handles):
        n=len(handles)
        if not n: return [],[]
        hs=(ctypes.c_int*n)(*handles); out=(ctypes.c_float*(7*n))(); awake=(ctypes.c_uint8*n)()
        if not self.api.lib.bp_get_transforms(self.ptr,hs,n,out,awake): raise RuntimeError('Box3D bulk transform read failed')
        return [[out[7*i+j] for j in range(7)] for i in range(n)], [bool(awake[i]) for i in range(n)]
