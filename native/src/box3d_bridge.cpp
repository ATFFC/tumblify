// SPDX-FileCopyrightText: 2026 CG Corner addons
// SPDX-License-Identifier: GPL-3.0-only
// Tumblify is distributed under GNU GPL version 3. See LICENSE.

#include <box3d/box3d.h>
#include <array>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <exception>
#include <fstream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#define BP_API extern "C" __declspec(dllexport)
#else
#define BP_API extern "C" __attribute__((visibility("default")))
#endif

struct BPWorld {
    b3WorldId world{};
    std::vector<b3BodyId> bodies;
    // Mesh shapes keep a pointer to b3MeshData for their full lifetime.
    std::vector<b3MeshData*> meshes;
    std::vector<b3HeightFieldData*> heightFields;
    // Reusable source convex hulls. Building these once is critical for fast interactive spawning.
    std::vector<b3HullData*> hullAssets;
    int workers = 1;
    bool selfCollision = true;
    bool forcedSleep = true;
    bool nativeSleepEnabled = true;
    float nativeSleepThreshold = 0.05f;
    float sleepLinear = 0.05f;
    float sleepAngular = 0.15f;
    float sleepDelay = 0.5f;
    std::vector<float> lowSpeedTime;
    // Forced-sleep candidate state. The stored pose is an ANCHOR for the whole
    // candidate interval, not merely the previous frame. This prevents slow drift
    // or slow tipping from accumulating while still passing per-frame thresholds.
    std::vector<b3Pos> sleepPrevPos;
    std::vector<b3Quat> sleepPrevRot;
    std::vector<uint8_t> sleepPrevValid;
    // Wake-counter style hysteresis: newly-created or freshly-woken bodies must
    // remain awake for a minimum time before they are even eligible for forced sleep.
    std::vector<float> sleepAwakeTime;
    std::vector<uint8_t> sleepWasAwake;
    // Characteristic collision radius in solver/world units. Used to convert small angular
    // changes into motion at the outer surface so tall/long bodies cannot sleep mid-fall.
    std::vector<float> sleepRadius;
    // Radius of each reusable hull asset in local solver units.
    std::vector<float> hullAssetRadius;
    float solverScale = 1.0f;
};
static thread_local std::string g_error;
static void set_error(const char* s){ g_error = s ? s : "unknown error"; }
static b3Quat quat(float x,float y,float z,float w){ return {{x,y,z},w}; }
static b3Pos pos(float x,float y,float z){ return {x,y,z}; }
static float bp_scale(const BPWorld* w,float v){ return v*(w?w->solverScale:1.0f); }
static float bp_unscale(const BPWorld* w,float v){ float s=(w&&w->solverScale>1.0e-8f)?w->solverScale:1.0f; return v/s; }
static b3Pos bp_pos(const BPWorld* w,float x,float y,float z){ float s=(w?w->solverScale:1.0f); return {x*s,y*s,z*s}; }
static float bp_density(const BPWorld* w,float density){ float s=(w&&w->solverScale>1.0e-8f)?w->solverScale:1.0f; return density/(s*s*s); }
static b3ShapeDef make_shape(float friction,float restitution,float density){
    b3ShapeDef sd=b3DefaultShapeDef(); sd.density=density; sd.baseMaterial.friction=friction; sd.baseMaterial.restitution=restitution; return sd;
}
static void configure_dynamic_filter(BPWorld* w,int type,b3ShapeDef& sd){
    // A shared negative group index disables collisions between spawned dynamic bodies
    // while preserving collisions with the floor, static colliders, and kinematic emitters.
    if(w && type==2 && !w->selfCollision) sd.filter.groupIndex=-1;
}
static int add_id(BPWorld* w,b3BodyId id,float radius=0.0f){
    w->bodies.push_back(id);
    w->lowSpeedTime.push_back(0.0f);
    w->sleepPrevPos.push_back({0,0,0});
    w->sleepPrevRot.push_back({{0,0,0},1});
    w->sleepPrevValid.push_back(0);
    w->sleepAwakeTime.push_back(0.0f);
    w->sleepWasAwake.push_back(0);
    w->sleepRadius.push_back(std::max(0.0f,radius));
    return int(w->bodies.size())-1;
}
static b3BodyId body(BPWorld* w,int h){ if(!w || h<0 || h>=int(w->bodies.size())) return b3_nullBodyId; return w->bodies[h]; }
static b3BodyId create_body(BPWorld* w,int type,float px,float py,float pz,float qx,float qy,float qz,float qw){
    b3BodyDef bd=b3DefaultBodyDef();
    bd.type=(b3BodyType)type;
    bd.position=bp_pos(w,px,py,pz);
    bd.rotation=quat(qx,qy,qz,qw);
    if(type==b3_dynamicBody){
        bd.enableSleep=w?w->nativeSleepEnabled:true;
        bd.sleepThreshold=(w?std::max(0.0f,w->nativeSleepThreshold):0.05f);
        bd.isAwake=true;
    }
    return b3CreateBody(w->world,&bd);
}

BP_API const char* bp_version(){ return "tumblify-native-box3d-main-f36d061-test-135"; }
BP_API const char* bp_error(){ return g_error.c_str(); }
BP_API void* bp_create_world(int requested_workers){
    try {
        auto* w=new BPWorld(); b3WorldDef wd=b3DefaultWorldDef(); wd.gravity={0.0f,0.0f,-9.81f}; wd.enableSleep=true;
        if(requested_workers<=0){ unsigned hw=std::thread::hardware_concurrency(); requested_workers=std::max(1,std::min(8,int(hw>2?hw-1:1))); }
        wd.workerCount=(uint32_t)std::max(1,std::min(32,requested_workers));
        w->world=b3CreateWorld(&wd); w->workers=b3World_GetWorkerCount(w->world); return w;
    } catch(...) { set_error("bp_create_world failed"); return nullptr; }
}
BP_API void bp_destroy_world(void* p){
    auto* w=(BPWorld*)p; if(!w)return;
    // Mesh shapes reference these buffers, so destroy the world/shapes first.
    if(B3_IS_NON_NULL(w->world))b3DestroyWorld(w->world);
    for(auto* m:w->meshes){ if(m) b3DestroyMesh(m); }
    for(auto* h:w->heightFields){ if(h) b3DestroyHeightField(h); }
    for(auto* h:w->hullAssets){ if(h) b3DestroyHull(h); }
    delete w;
}
BP_API int bp_worker_count(void* p){ auto* w=(BPWorld*)p; return w?w->workers:0; }
BP_API int bp_set_gravity(void* p,float gx,float gy,float gz){
    auto* w=(BPWorld*)p; if(!w)return 0;
    b3World_SetGravity(w->world,{gx*w->solverScale,gy*w->solverScale,gz*w->solverScale});
    return 1;
}
BP_API int bp_set_solver_tuning(void* p,float solver_scale,float hertz,float damping_ratio,float contact_speed,float recycle_distance){
    auto* w=(BPWorld*)p; if(!w)return 0;
    if(!w->bodies.empty() || !w->hullAssets.empty() || !w->meshes.empty()){ set_error("Solver tuning must be configured before creating collision bodies"); return 0; }
    w->solverScale=std::max(1.0f,std::min(50.0f,solver_scale));
    b3World_SetGravity(w->world,{0.0f,0.0f,-9.81f*w->solverScale});
    b3World_SetContactTuning(w->world,std::max(1.0f,hertz),std::max(0.1f,damping_ratio),std::max(0.01f,contact_speed)*w->solverScale);
    b3World_SetContactRecycleDistance(w->world,std::max(0.0f,recycle_distance));
    return 1;
}
BP_API int bp_set_body_bullet(void* p,int handle,int enabled){
    auto* w=(BPWorld*)p; auto id=body(w,handle); if(B3_IS_NULL(id) || b3Body_GetType(id)!=b3_dynamicBody)return 0;
    b3Body_SetBullet(id,enabled!=0); return 1;
}
BP_API int bp_set_body_contact_recycling(void* p,int handle,int enabled){
    auto* w=(BPWorld*)p; auto id=body(w,handle); if(B3_IS_NULL(id) || b3Body_GetType(id)!=b3_dynamicBody)return 0;
    b3Body_EnableContactRecycling(id,enabled!=0); return 1;
}
BP_API int bp_set_self_collision(void* p,int enabled){ auto* w=(BPWorld*)p; if(!w)return 0; w->selfCollision=enabled!=0; return 1; }
BP_API int bp_set_forced_sleep(void* p,int enabled,float linear_threshold,float angular_threshold,float delay){
    auto* w=(BPWorld*)p; if(!w)return 0;
    w->forcedSleep=enabled!=0;
    w->sleepLinear=std::max(0.0f,linear_threshold)*w->solverScale;
    w->sleepAngular=std::max(0.0f,angular_threshold);
    w->sleepDelay=std::max(0.0f,delay);
    std::fill(w->lowSpeedTime.begin(),w->lowSpeedTime.end(),0.0f);
    std::fill(w->sleepPrevValid.begin(),w->sleepPrevValid.end(),uint8_t(0));
    std::fill(w->sleepAwakeTime.begin(),w->sleepAwakeTime.end(),0.0f);
    std::fill(w->sleepWasAwake.begin(),w->sleepWasAwake.end(),uint8_t(0));
    return 1;
}
BP_API int bp_set_native_sleep(void* p,int enabled,float threshold){
    auto* w=(BPWorld*)p; if(!w)return 0;
    w->nativeSleepEnabled=enabled!=0;
    // The add-on scales the whole physics world manually, so scale the native
    // speed threshold as well to preserve the same real-world value.
    w->nativeSleepThreshold=std::max(0.0f,threshold)*w->solverScale;
    for(b3BodyId id:w->bodies){
        if(B3_IS_NULL(id) || b3Body_GetType(id)!=b3_dynamicBody) continue;
        b3Body_SetSleepThreshold(id,w->nativeSleepThreshold);
        b3Body_EnableSleep(id,w->nativeSleepEnabled);
        if(!w->nativeSleepEnabled) b3Body_SetAwake(id,true);
    }
    return 1;
}

BP_API int bp_add_floor(void* p,float z,float friction){ auto* w=(BPWorld*)p; if(!w)return -1; b3BodyDef bd=b3DefaultBodyDef(); bd.position=bp_pos(w,0,0,z-0.5f); auto id=b3CreateBody(w->world,&bd); auto hull=b3MakeBoxHull(10000.0f*w->solverScale,10000.0f*w->solverScale,0.5f*w->solverScale); auto sd=make_shape(std::max(0.0f,friction),0.0f,0.0f); b3CreateHullShape(id,&sd,&hull.base); return add_id(w,id); }
BP_API int bp_add_box(void* p,int type,float px,float py,float pz,float qx,float qy,float qz,float qw,float hx,float hy,float hz,float cx,float cy,float cz,float friction,float restitution,float density){
    auto* w=(BPWorld*)p; if(!w)return -1; auto id=create_body(w,type,px,py,pz,qx,qy,qz,qw); auto hull=b3MakeOffsetBoxHull(std::max(bp_scale(w,hx),0.001f),std::max(bp_scale(w,hy),0.001f),std::max(bp_scale(w,hz),0.001f),{bp_scale(w,cx),bp_scale(w,cy),bp_scale(w,cz)}); auto sd=make_shape(friction,restitution,type==2?std::max(1.0e-9f,bp_density(w,density)):0.0f); configure_dynamic_filter(w,type,sd); b3CreateHullShape(id,&sd,&hull.base); float rr=std::sqrt(bp_scale(w,hx)*bp_scale(w,hx)+bp_scale(w,hy)*bp_scale(w,hy)+bp_scale(w,hz)*bp_scale(w,hz)); return add_id(w,id,rr);
}
BP_API int bp_add_sphere(void* p,int type,float px,float py,float pz,float qx,float qy,float qz,float qw,float radius,float cx,float cy,float cz,float friction,float restitution,float density){
    auto* w=(BPWorld*)p; if(!w)return -1; auto id=create_body(w,type,px,py,pz,qx,qy,qz,qw); b3Sphere s{}; s.center={bp_scale(w,cx),bp_scale(w,cy),bp_scale(w,cz)}; s.radius=std::max(bp_scale(w,radius),0.001f); auto sd=make_shape(friction,restitution,type==2?std::max(1.0e-9f,bp_density(w,density)):0.0f); configure_dynamic_filter(w,type,sd); b3CreateSphereShape(id,&sd,&s); float rr=std::sqrt(s.center.x*s.center.x+s.center.y*s.center.y+s.center.z*s.center.z)+s.radius; return add_id(w,id,rr);
}
BP_API int bp_add_capsule_axis(void* p,int type,float px,float py,float pz,float qx,float qy,float qz,float qw,float half_segment,float radius,float cx,float cy,float cz,int axis,float friction,float restitution,float density){
    auto* w=(BPWorld*)p; if(!w)return -1; auto id=create_body(w,type,px,py,pz,qx,qy,qz,qw); b3Capsule c{}; float h=std::max(0.0f,bp_scale(w,half_segment));
    c.center1={bp_scale(w,cx),bp_scale(w,cy),bp_scale(w,cz)}; c.center2=c.center1;
    axis=std::max(0,std::min(2,axis));
    if(axis==0){ c.center1.x-=h; c.center2.x+=h; }
    else if(axis==1){ c.center1.y-=h; c.center2.y+=h; }
    else { c.center1.z-=h; c.center2.z+=h; }
    c.radius=std::max(bp_scale(w,radius),0.001f); auto sd=make_shape(friction,restitution,type==2?std::max(1.0e-9f,bp_density(w,density)):0.0f); configure_dynamic_filter(w,type,sd); b3ShapeId shape=b3CreateCapsuleShape(id,&sd,&c);
    if(B3_IS_NULL(shape)){ b3DestroyBody(id); set_error("Box3D failed to attach capsule shape"); return -1; }
    float cc=std::sqrt(bp_scale(w,cx)*bp_scale(w,cx)+bp_scale(w,cy)*bp_scale(w,cy)+bp_scale(w,cz)*bp_scale(w,cz));
    return add_id(w,id,cc+h+c.radius);
}
BP_API int bp_add_cylinder_axis(void* p,int type,float px,float py,float pz,float qx,float qy,float qz,float qw,float half_height,float radius,float cx,float cy,float cz,int sides,int axis,float friction,float restitution,float density){
    auto* w=(BPWorld*)p; if(!w)return -1; auto id=create_body(w,type,px,py,pz,qx,qy,qz,qw);
    b3HullData* cyl=b3CreateCylinder(std::max(0.002f,2.0f*bp_scale(w,half_height)),std::max(bp_scale(w,radius),0.001f),0.0f,std::max(8,std::min(24,sides)));
    if(!cyl){ b3DestroyBody(id); set_error("Box3D cylinder creation failed"); return -1; }
    // Box3D's generated cylinder is aligned to local Y. Rotate only the shape, not the rigid body.
    const float s45=0.7071067811865476f; axis=std::max(0,std::min(2,axis));
    b3Quat local_q=quat(0,0,0,1);
    if(axis==0) local_q=quat(0,0,-s45,s45);      // Y -> X
    else if(axis==2) local_q=quat(s45,0,0,s45);  // Y -> Z
    b3Transform local={{bp_scale(w,cx),bp_scale(w,cy),bp_scale(w,cz)},local_q};
    auto sd=make_shape(friction,restitution,type==2?std::max(1.0e-9f,bp_density(w,density)):0.0f); configure_dynamic_filter(w,type,sd); b3ShapeId shape=b3CreateTransformedHullShape(id,&sd,cyl,local,{1,1,1}); b3DestroyHull(cyl);
    if(B3_IS_NULL(shape)){ b3DestroyBody(id); set_error("Box3D failed to attach cylinder shape"); return -1; }
    float cc=std::sqrt(bp_scale(w,cx)*bp_scale(w,cx)+bp_scale(w,cy)*bp_scale(w,cy)+bp_scale(w,cz)*bp_scale(w,cz));
    float hh=std::max(0.0f,bp_scale(w,half_height)), rr=std::max(0.0f,bp_scale(w,radius));
    return add_id(w,id,cc+std::sqrt(hh*hh+rr*rr));
}
BP_API int bp_create_hull_asset(void* p,const float* xyz,int point_count,int requested_max_vertices){
    auto* w=(BPWorld*)p;
    if(!w || !xyz || point_count<4){ set_error("Convex hull asset requires at least 4 vertices"); return -1; }

    // Box3D v0.1.0 is alpha software. Keep its QuickHull input deliberately small and clean.
    // Blender has already produced a convex shell, so this is only a final safety reduction.
    const int hard_limit = 48;
    const int requested = std::max(8, std::min(hard_limit, requested_max_vertices));

    std::vector<b3Vec3> input;
    input.reserve((size_t)point_count);
    for(int i=0;i<point_count;++i){
        float x=xyz[3*i], y=xyz[3*i+1], z=xyz[3*i+2];
        if(std::isfinite(x)&&std::isfinite(y)&&std::isfinite(z)) input.push_back({bp_scale(w,x),bp_scale(w,y),bp_scale(w,z)});
    }
    if(input.size()<4){ set_error("Convex hull asset has too few finite vertices"); return -1; }

    // Deterministic farthest-point reduction, seeded with axis extrema.
    std::vector<int> selected;
    selected.reserve((size_t)requested);
    auto add_unique=[&](int idx){
        if(idx<0) return;
        if(std::find(selected.begin(),selected.end(),idx)==selected.end() && (int)selected.size()<requested) selected.push_back(idx);
    };
    for(int axis=0;axis<3;++axis){
        int imin=0,imax=0;
        for(int i=1;i<(int)input.size();++i){
            float v = axis==0?input[i].x:(axis==1?input[i].y:input[i].z);
            float mn = axis==0?input[imin].x:(axis==1?input[imin].y:input[imin].z);
            float mx = axis==0?input[imax].x:(axis==1?input[imax].y:input[imax].z);
            if(v<mn) imin=i; if(v>mx) imax=i;
        }
        add_unique(imin); add_unique(imax);
    }
    std::vector<float> nearest(input.size(), std::numeric_limits<float>::max());
    auto update_nearest=[&](int si){
        const b3Vec3& a=input[(size_t)si];
        for(size_t i=0;i<input.size();++i){
            float dx=input[i].x-a.x,dy=input[i].y-a.y,dz=input[i].z-a.z;
            float d=dx*dx+dy*dy+dz*dz;
            if(d<nearest[i]) nearest[i]=d;
        }
    };
    for(int i:selected) update_nearest(i);
    while((int)selected.size()<requested && selected.size()<input.size()){
        int best=-1; float bestd=-1.0f;
        for(int i=0;i<(int)input.size();++i){
            if(std::find(selected.begin(),selected.end(),i)!=selected.end()) continue;
            if(nearest[(size_t)i]>bestd){ bestd=nearest[(size_t)i]; best=i; }
        }
        if(best<0) break;
        selected.push_back(best); update_nearest(best);
    }

    std::vector<b3Vec3> pts; pts.reserve(selected.size());
    for(int i:selected) pts.push_back(input[(size_t)i]);
    if(pts.size()<4){ set_error("Convex hull asset has too few reduced vertices"); return -1; }

    // Reject nearly-flat point clouds before entering Box3D. This avoids known fragile paths in
    // the alpha QuickHull implementation. Thin assets should use Box/Cylinder/Mesh as appropriate.
    b3Vec3 mn=pts[0], mx=pts[0];
    for(const b3Vec3& v:pts){
        mn.x=std::min(mn.x,v.x); mn.y=std::min(mn.y,v.y); mn.z=std::min(mn.z,v.z);
        mx.x=std::max(mx.x,v.x); mx.y=std::max(mx.y,v.y); mx.z=std::max(mx.z,v.z);
    }
    float ex=mx.x-mn.x, ey=mx.y-mn.y, ez=mx.z-mn.z;
    float largest=std::max(ex,std::max(ey,ez));
    if(!(largest>1.0e-6f)){ set_error("Convex hull asset is degenerate"); return -1; }
    int thin = ex<=ey && ex<=ez ? 0 : (ey<=ez ? 1 : 2);
    float ext = thin==0?ex:(thin==1?ey:ez);
    float minThickness=std::max(0.0005f,largest*1.0e-4f);
    if(ext < minThickness){
        // Compound decomposition often creates slab-like convex pieces. Give only those nearly-flat
        // parts a tiny collision thickness while keeping the QuickHull input at <= requested points.
        int baseCount=std::max(2,std::min((int)pts.size(),requested/2));
        std::vector<b3Vec3> padded; padded.reserve((size_t)baseCount*2);
        float center = thin==0?0.5f*(mn.x+mx.x):(thin==1?0.5f*(mn.y+mx.y):0.5f*(mn.z+mx.z));
        float hh=0.5f*minThickness;
        for(int i=0;i<baseCount;++i){
            int src=(baseCount==1)?0:(int)((long long)i*((int)pts.size()-1)/(baseCount-1));
            b3Vec3 a=pts[(size_t)src], b=a;
            if(thin==0){a.x=center-hh;b.x=center+hh;} else if(thin==1){a.y=center-hh;b.y=center+hh;} else {a.z=center-hh;b.z=center+hh;}
            padded.push_back(a); padded.push_back(b);
        }
        pts.swap(padded);
    }

    b3HullData* hull=b3CreateHull(pts.data(),(int)pts.size(),std::min(requested,(int)pts.size()));
    if(!hull){ set_error("Box3D could not build cached convex hull asset; increase Convex Simplify or use another proxy"); return -1; }
    float assetRadius=0.0f;
    for(const b3Vec3& v:pts){ assetRadius=std::max(assetRadius,std::sqrt(v.x*v.x+v.y*v.y+v.z*v.z)); }
    w->hullAssets.push_back(hull);
    w->hullAssetRadius.push_back(assetRadius);
    return (int)w->hullAssets.size()-1;
}
BP_API int bp_add_hull_asset(void* p,int type,float px,float py,float pz,float qx,float qy,float qz,float qw,int asset,float scale,float friction,float restitution,float density){
    auto* w=(BPWorld*)p;
    if(!w || asset<0 || asset>=int(w->hullAssets.size()) || !w->hullAssets[(size_t)asset]){ set_error("Invalid convex hull asset handle"); return -1; }
    auto id=create_body(w,type,px,py,pz,qx,qy,qz,qw);
    auto sd=make_shape(friction,restitution,type==2?std::max(1.0e-9f,bp_density(w,density)):0.0f); configure_dynamic_filter(w,type,sd);
    float sc=std::max(0.001f,scale); b3Transform local={{0,0,0},quat(0,0,0,1)};
    b3ShapeId shape=b3CreateTransformedHullShape(id,&sd,w->hullAssets[(size_t)asset],local,{sc,sc,sc});
    if(B3_IS_NULL(shape)){ b3DestroyBody(id); set_error("Box3D failed to attach cached convex hull"); return -1; }
    float rr=(asset<(int)w->hullAssetRadius.size()?w->hullAssetRadius[(size_t)asset]:0.0f)*sc;
    return add_id(w,id,rr);
}
BP_API int bp_add_compound_hull_assets(void* p,int type,float px,float py,float pz,float qx,float qy,float qz,float qw,const int* assets,int asset_count,float scale,float friction,float restitution,float density){
    auto* w=(BPWorld*)p;
    if(!w || !assets || asset_count<=0){ set_error("Compound Hull requires at least one convex asset"); return -1; }
    for(int i=0;i<asset_count;++i){
        int a=assets[i];
        if(a<0 || a>=int(w->hullAssets.size()) || !w->hullAssets[(size_t)a]){ set_error("Invalid Compound Hull asset handle"); return -1; }
    }
    auto id=create_body(w,type,px,py,pz,qx,qy,qz,qw);
    auto sd=make_shape(friction,restitution,type==2?std::max(1.0e-9f,bp_density(w,density)):0.0f); configure_dynamic_filter(w,type,sd);
    float sc=std::max(0.001f,scale); b3Transform local={{0,0,0},quat(0,0,0,1)};
    for(int i=0;i<asset_count;++i){
        b3ShapeId shape=b3CreateTransformedHullShape(id,&sd,w->hullAssets[(size_t)assets[i]],local,{sc,sc,sc});
        if(B3_IS_NULL(shape)){
            b3DestroyBody(id); set_error("Box3D failed to attach a Compound Hull part"); return -1;
        }
    }
    float rr=0.0f;
    for(int i=0;i<asset_count;++i){ int a=assets[i]; if(a>=0 && a<(int)w->hullAssetRadius.size()) rr=std::max(rr,w->hullAssetRadius[(size_t)a]*sc); }
    return add_id(w,id,rr);
}
BP_API int bp_add_compound_mixed(void* p,int type,float px,float py,float pz,float qx,float qy,float qz,float qw,const int* kinds,const int* assets,const float* part_data,int part_count,float scale,float friction,float restitution,float density){
    auto* w=(BPWorld*)p;
    if(!w || !kinds || !assets || !part_data || part_count<=0){ set_error("Mixed compound requires at least one part"); return -1; }
    auto id=create_body(w,type,px,py,pz,qx,qy,qz,qw);
    auto sd=make_shape(friction,restitution,type==2?std::max(1.0e-9f,bp_density(w,density)):0.0f); configure_dynamic_filter(w,type,sd);
    float sc=std::max(0.001f,scale);
    float bodyRadius=0.0f;
    for(int i=0;i<part_count;++i){
        const float* d=part_data + 10*i;
        float lx=bp_scale(w,d[0]*sc), ly=bp_scale(w,d[1]*sc), lz=bp_scale(w,d[2]*sc);
        float qx2=d[3], qy2=d[4], qz2=d[5], qw2=d[6];
        float qn=std::sqrt(qx2*qx2+qy2*qy2+qz2*qz2+qw2*qw2);
        if(qn>1.0e-8f){ qx2/=qn; qy2/=qn; qz2/=qn; qw2/=qn; } else { qx2=qy2=qz2=0.0f; qw2=1.0f; }
        b3Quat lq=quat(qx2,qy2,qz2,qw2);
        b3Transform local={{lx,ly,lz},lq};
        b3ShapeId shape{};
        switch(kinds[i]){
            case 0: { // BOX, analytically generated 8-vertex Box3D hull
                float hx=std::max(0.001f,bp_scale(w,std::abs(d[7])*sc)), hy=std::max(0.001f,bp_scale(w,std::abs(d[8])*sc)), hz=std::max(0.001f,bp_scale(w,std::abs(d[9])*sc));
                b3BoxHull box=b3MakeTransformedBoxHull(hx,hy,hz,local);
                shape=b3CreateHullShape(id,&sd,&box.base);
            } break;
            case 1: { // SPHERE
                b3Sphere sp{}; sp.center={lx,ly,lz}; sp.radius=std::max(0.001f,bp_scale(w,std::abs(d[7])*sc));
                shape=b3CreateSphereShape(id,&sd,&sp);
            } break;
            case 2: { // CAPSULE, canonical local axis is Y
                float hs=std::max(0.0f,bp_scale(w,std::abs(d[7])*sc)), r=std::max(0.001f,bp_scale(w,std::abs(d[8])*sc));
                // Rotated local Y axis from quaternion.
                b3Vec3 ay={2.0f*(qx2*qy2-qw2*qz2), 1.0f-2.0f*(qx2*qx2+qz2*qz2), 2.0f*(qy2*qz2+qw2*qx2)};
                b3Capsule cp{}; cp.center1={lx-ay.x*hs,ly-ay.y*hs,lz-ay.z*hs}; cp.center2={lx+ay.x*hs,ly+ay.y*hs,lz+ay.z*hs}; cp.radius=r;
                shape=b3CreateCapsuleShape(id,&sd,&cp);
            } break;
            case 3: { // CYLINDER, Box3D cylinder is canonical Y axis
                float hh=std::max(0.001f,bp_scale(w,std::abs(d[7])*sc)), r=std::max(0.001f,bp_scale(w,std::abs(d[8])*sc));
                int sides=std::max(8,std::min(24,(int)std::lround(d[9])));
                b3HullData* cyl=b3CreateCylinder(2.0f*hh,r,0.0f,sides);
                if(cyl){ shape=b3CreateTransformedHullShape(id,&sd,cyl,local,{1,1,1}); b3DestroyHull(cyl); }
            } break;
            default: { // HULL asset with optional local transform
                int a=assets[i];
                if(a<0 || a>=int(w->hullAssets.size()) || !w->hullAssets[(size_t)a]){ b3DestroyBody(id); set_error("Invalid mixed compound hull asset"); return -1; }
                shape=b3CreateTransformedHullShape(id,&sd,w->hullAssets[(size_t)a],local,{sc,sc,sc});
            } break;
        }
        if(B3_IS_NULL(shape)){ b3DestroyBody(id); set_error("Box3D failed to attach mixed compound part"); return -1; }
        float localCenter=std::sqrt(lx*lx+ly*ly+lz*lz);
        float partRadius=0.0f;
        if(kinds[i]==0){ float hx=bp_scale(w,std::abs(d[7])*sc), hy=bp_scale(w,std::abs(d[8])*sc), hz=bp_scale(w,std::abs(d[9])*sc); partRadius=std::sqrt(hx*hx+hy*hy+hz*hz); }
        else if(kinds[i]==1){ partRadius=bp_scale(w,std::abs(d[7])*sc); }
        else if(kinds[i]==2){ partRadius=bp_scale(w,(std::abs(d[7])+std::abs(d[8]))*sc); }
        else if(kinds[i]==3){ float hh=bp_scale(w,std::abs(d[7])*sc), r=bp_scale(w,std::abs(d[8])*sc); partRadius=std::sqrt(hh*hh+r*r); }
        else { int a=assets[i]; if(a>=0 && a<(int)w->hullAssetRadius.size()) partRadius=w->hullAssetRadius[(size_t)a]*sc; }
        bodyRadius=std::max(bodyRadius,localCenter+partRadius);
    }
    return add_id(w,id,bodyRadius);
}
BP_API int bp_add_hull(void* p,int type,float px,float py,float pz,float qx,float qy,float qz,float qw,const float* xyz,int point_count,int requested_max_vertices,float friction,float restitution,float density){
    auto* w=(BPWorld*)p;
    if(!w || !xyz || point_count<4){ set_error("Convex hull requires at least 4 vertices"); return -1; }

    // b3CreateHull accepts arbitrary input points but the finalized hull is capped by Box3D's
    // uint8 index representation. Feeding tens of thousands of Blender render vertices to the
    // alpha v0.1.0 hull builder is both wasteful and has proven fragile. Validate and reduce the
    // input before entering Box3D. Blender-side code already supplies hull-surface vertices; this
    // is a second native safety net for modifiers / unusual meshes.
    std::vector<b3Vec3> valid;
    valid.reserve((size_t)std::min(point_count, 4096));
    const int max_input = 512;
    int stride = point_count > max_input ? (point_count + max_input - 1) / max_input : 1;
    for(int i=0;i<point_count;i+=stride){
        float x=xyz[3*i], y=xyz[3*i+1], z=xyz[3*i+2];
        if(std::isfinite(x) && std::isfinite(y) && std::isfinite(z)) valid.push_back({bp_scale(w,x),bp_scale(w,y),bp_scale(w,z)});
    }
    float directHullRadius=0.0f;
    for(const b3Vec3& v:valid){ directHullRadius=std::max(directHullRadius,std::sqrt(v.x*v.x+v.y*v.y+v.z*v.z)); }

    // Always preserve the six axis extrema, which makes the reduction conservative for common assets.
    if(point_count>max_input){
        for(int axis=0; axis<3; ++axis){
            int imin=0, imax=0;
            float mn=xyz[axis], mx=xyz[axis];
            for(int i=1;i<point_count;++i){
                float v=xyz[3*i+axis];
                if(!std::isfinite(v)) continue;
                if(v<mn){mn=v; imin=i;} if(v>mx){mx=v; imax=i;}
            }
            valid.push_back({bp_scale(w,xyz[3*imin]),bp_scale(w,xyz[3*imin+1]),bp_scale(w,xyz[3*imin+2])});
            valid.push_back({bp_scale(w,xyz[3*imax]),bp_scale(w,xyz[3*imax+1]),bp_scale(w,xyz[3*imax+2])});
        }
    }
    if(valid.size()<4){ set_error("Convex hull has fewer than 4 finite vertices"); return -1; }

    // Remove nearly-identical points in native code as a second safety net. Blender already
    // deduplicates, but ctypes input can still contain values that only differ below useful
    // collision precision.
    {
        std::vector<b3Vec3> unique;
        unique.reserve(valid.size());
        const float eps2 = 1.0e-12f;
        for(const b3Vec3& p0 : valid){
            bool duplicate=false;
            for(const b3Vec3& p1 : unique){
                float dx=p0.x-p1.x, dy=p0.y-p1.y, dz=p0.z-p1.z;
                if(dx*dx+dy*dy+dz*dz <= eps2){ duplicate=true; break; }
            }
            if(!duplicate) unique.push_back(p0);
        }
        valid.swap(unique);
    }
    if(valid.size()<4){ set_error("Convex hull has fewer than 4 unique vertices"); return -1; }

    // Box3D rejects coplanar / insufficient-volume point clouds by design. Blender assets can
    // legitimately be paper-thin, so detect a collapsed bounding-box axis and add a very small
    // collision thickness. This affects only degenerate hulls; normal 3D meshes are untouched.
    float mn[3]={valid[0].x,valid[0].y,valid[0].z};
    float mx[3]={valid[0].x,valid[0].y,valid[0].z};
    for(const b3Vec3& v: valid){
        mn[0]=std::min(mn[0],v.x); mn[1]=std::min(mn[1],v.y); mn[2]=std::min(mn[2],v.z);
        mx[0]=std::max(mx[0],v.x); mx[1]=std::max(mx[1],v.y); mx[2]=std::max(mx[2],v.z);
    }
    float ext[3]={mx[0]-mn[0],mx[1]-mn[1],mx[2]-mn[2]};
    float largest=std::max(ext[0],std::max(ext[1],ext[2]));
    int thin = ext[0] <= ext[1] && ext[0] <= ext[2] ? 0 : (ext[1] <= ext[2] ? 1 : 2);
    float min_thickness=std::max(0.0005f,largest*1.0e-4f);
    if(largest>0.0f && ext[thin] < min_thickness){
        float c=0.5f*(mn[thin]+mx[thin]), h=0.5f*min_thickness;
        std::vector<b3Vec3> padded; padded.reserve(valid.size()*2);
        for(b3Vec3 v: valid){
            b3Vec3 a=v,b=v;
            if(thin==0){a.x=c-h;b.x=c+h;} else if(thin==1){a.y=c-h;b.y=c+h;} else {a.z=c-h;b.z=c+h;}
            padded.push_back(a); padded.push_back(b);
        }
        valid.swap(padded);
    }

    // Keep the final hull below Box3D's byte-sized index limit. Blender has already built and
    // simplified the convex shell, so avoid imposing another aggressive native simplification.
    int max_vertices=std::max(8,std::min<int>(std::min(240,requested_max_vertices),(int)valid.size()));
    b3HullData* hull=b3CreateHull(valid.data(),(int)valid.size(),max_vertices);
    if(!hull){
        // Last-resort safety fallback: use the local bounds rather than aborting the entire
        // interactive session. This only happens for pathological point clouds that Box3D's
        // alpha hull builder still rejects after sanitization.
        float hx=std::max(0.00025f,0.5f*(mx[0]-mn[0]));
        float hy=std::max(0.00025f,0.5f*(mx[1]-mn[1]));
        float hz=std::max(0.00025f,0.5f*(mx[2]-mn[2]));
        b3BoxHull fallback=b3MakeOffsetBoxHull(hx,hy,hz,{0.5f*(mn[0]+mx[0]),0.5f*(mn[1]+mx[1]),0.5f*(mn[2]+mx[2])});
        auto id=create_body(w,type,px,py,pz,qx,qy,qz,qw);
        auto sd=make_shape(friction,restitution,type==2?std::max(1.0e-9f,bp_density(w,density)):0.0f); configure_dynamic_filter(w,type,sd);
        b3ShapeId shape=b3CreateHullShape(id,&sd,&fallback.base);
        if(B3_IS_NULL(shape)){ b3DestroyBody(id); set_error("Box3D failed to create convex-hull fallback shape"); return -1; }
        return add_id(w,id,directHullRadius);
    }
    auto id=create_body(w,type,px,py,pz,qx,qy,qz,qw);
    auto sd=make_shape(friction,restitution,type==2?std::max(1.0e-9f,bp_density(w,density)):0.0f); configure_dynamic_filter(w,type,sd);
    b3ShapeId shape=b3CreateHullShape(id,&sd,hull); // geometry is cloned by Box3D
    b3DestroyHull(hull);
    if(B3_IS_NULL(shape)){
        b3DestroyBody(id);
        set_error("Box3D failed to attach convex hull shape");
        return -1;
    }
    return add_id(w,id,directHullRadius);
}
BP_API int bp_add_mesh_static(void* p,float px,float py,float pz,float qx,float qy,float qz,float qw,const float* xyz,int vertex_count,const int32_t* indices,int triangle_count,float friction,float restitution){
    auto* w=(BPWorld*)p; if(!w || !xyz || !indices || vertex_count<3 || triangle_count<1){ set_error("Invalid mesh collision data"); return -1; }
    std::vector<b3Vec3> vertices((size_t)vertex_count);
    for(int i=0;i<vertex_count;i++) vertices[(size_t)i]={bp_scale(w,xyz[3*i]),bp_scale(w,xyz[3*i+1]),bp_scale(w,xyz[3*i+2])};
    b3MeshDef md{};
    md.vertices=vertices.data();
    md.indices=const_cast<int32_t*>(indices);
    md.materialIndices=nullptr;
    md.weldTolerance=0.0001f*w->solverScale;
    md.vertexCount=vertex_count;
    md.triangleCount=triangle_count;
    md.weldVertices=true;
    md.useMedianSplit=false;
    md.identifyEdges=true;
    b3MeshData* mesh=b3CreateMesh(&md,nullptr,0);
    if(!mesh){ set_error("Box3D could not build triangle mesh"); return -1; }
    auto id=create_body(w,0,px,py,pz,qx,qy,qz,qw);
    auto sd=make_shape(friction,restitution,0.0f);
    b3CreateMeshShape(id,&sd,mesh,{1.0f,1.0f,1.0f});
    w->meshes.push_back(mesh);
    return add_id(w,id);
}
BP_API int bp_set_target(void* p,int handle,float px,float py,float pz,float qx,float qy,float qz,float qw,float dt){ auto* w=(BPWorld*)p; auto id=body(w,handle); if(B3_IS_NULL(id))return 0; b3WorldTransform t={bp_pos(w,px,py,pz),quat(qx,qy,qz,qw)}; b3Body_SetTargetTransform(id,t,dt,true); return 1; }
BP_API int bp_set_transform(void* p,int handle,float px,float py,float pz,float qx,float qy,float qz,float qw){ auto* w=(BPWorld*)p; auto id=body(w,handle); if(B3_IS_NULL(id))return 0; b3Body_SetTransform(id,bp_pos(w,px,py,pz),quat(qx,qy,qz,qw)); return 1; }
BP_API int bp_set_enabled(void* p,int handle,int enabled){ auto* w=(BPWorld*)p; auto id=body(w,handle); if(B3_IS_NULL(id))return 0; if(enabled){ if(!b3Body_IsEnabled(id)) b3Body_Enable(id); } else { if(b3Body_IsEnabled(id)) b3Body_Disable(id); } return 1; }
BP_API int bp_destroy_body(void* p,int handle){ auto* w=(BPWorld*)p; if(!w || handle<0 || handle>=int(w->bodies.size()))return 0; auto id=w->bodies[(size_t)handle]; if(B3_IS_NULL(id))return 0; b3DestroyBody(id); w->bodies[(size_t)handle]=b3_nullBodyId; return 1; }
BP_API int bp_apply_radial_drag(void* p,const int* handles,int count,float cx,float cy,float cz,float radius,float strength,float damping){
    auto* w=(BPWorld*)p;
    if(!w || !handles || count<=0 || !(radius>0.0f)) return 0;
    cx=bp_scale(w,cx); cy=bp_scale(w,cy); cz=bp_scale(w,cz); radius=bp_scale(w,radius); strength=bp_scale(w,strength);
    const float r2=radius*radius;
    int affected=0;
    for(int i=0;i<count;++i){
        b3BodyId id=body(w,handles[i]);
        if(B3_IS_NULL(id) || b3Body_GetType(id)!=b3_dynamicBody) continue;
        b3Pos c=b3Body_GetWorldCenter(id);
        float dx=cx-float(c.x), dy=cy-float(c.y), dz=cz-float(c.z);
        float d2=dx*dx+dy*dy+dz*dz;
        if(d2>=r2) continue;
        float d=std::sqrt(std::max(d2,1.0e-12f));
        float t=1.0f-d/radius;
        // Smoothstep falloff: zero at the outer edge, full near the emitter center.
        float fall=t*t*(3.0f-2.0f*t);
        b3Vec3 v=b3Body_GetLinearVelocity(id);
        float mass=b3Body_GetMass(id);
        if(!(mass>0.0f) || !std::isfinite(mass)) continue;
        float invd=d>1.0e-6f?1.0f/d:0.0f;
        b3Vec3 force={
            mass*fall*(strength*dx*invd-damping*v.x),
            mass*fall*(strength*dy*invd-damping*v.y),
            mass*fall*(strength*dz*invd-damping*v.z)
        };
        b3Body_ApplyForceToCenter(id,force,true);
        ++affected;
    }
    return affected;
}

BP_API int bp_apply_point_springs(void* p,const int* handles,const float* targets_xyz,int count,float strength,float damping,float gravity_comp){
    auto* w=(BPWorld*)p;
    if(!w || !handles || !targets_xyz || count<=0) return 0;
    int affected=0;
    strength=std::max(0.0f,strength); damping=std::max(0.0f,damping); gravity_comp=std::max(0.0f,gravity_comp);
    for(int i=0;i<count;++i){
        b3BodyId id=body(w,handles[i]);
        if(B3_IS_NULL(id) || b3Body_GetType(id)!=b3_dynamicBody) continue;
        b3Pos c=b3Body_GetWorldCenter(id);
        b3Vec3 v=b3Body_GetLinearVelocity(id);
        float mass=b3Body_GetMass(id);
        if(!(mass>0.0f) || !std::isfinite(mass)) continue;
        const float tx=bp_scale(w,targets_xyz[3*i]), ty=bp_scale(w,targets_xyz[3*i+1]), tz=bp_scale(w,targets_xyz[3*i+2]);
        b3Vec3 force={
            mass*(strength*(tx-float(c.x))-damping*v.x),
            mass*(strength*(ty-float(c.y))-damping*v.y),
            mass*(strength*(tz-float(c.z))-damping*v.z + gravity_comp*9.81f*w->solverScale)
        };
        b3Body_ApplyForceToCenter(id,force,true);
        ++affected;
    }
    return affected;
}
BP_API int bp_step(void* p,float dt,int substeps){
    auto* w=(BPWorld*)p; if(!w)return 0;
    b3World_Step(w->world,dt,std::max(1,std::min(16,substeps)));
    if(w->forcedSleep){
        // Wake-counter style forced sleep inspired by PhysX/Bullet behavior:
        // 1) a body must stay awake for a minimum grace period;
        // 2) once it becomes a sleep candidate, remember an ANCHOR pose;
        // 3) require the body to remain near that same anchor for the ENTIRE delay;
        // 4) any cumulative translation/rotation drift resets the timer completely.
        // This is deliberately different from comparing each frame to the previous
        // frame, which can incorrectly sleep a tall body that is slowly tipping.
        const float l2=w->sleepLinear*w->sleepLinear, a2=w->sleepAngular*w->sleepAngular;
        const float lg=w->sleepLinear*2.0f, ag=w->sleepAngular*2.0f;
        const float lg2=lg*lg, ag2=ag*ag;
        const float step=std::max(0.0f,dt);
        const float minAwakeTime=0.40f;
        // Candidate drift budget is measured across the WHOLE sleep window.
        // Keep it small in world units but scale it with the configured threshold.
        const float anchorPosEps=std::max(5.0e-4f*w->solverScale, w->sleepLinear*0.15f);
        const float anchorRotEps=std::max(2.5e-3f, w->sleepAngular*0.10f);
        for(size_t i=0;i<w->bodies.size();++i){
            b3BodyId id=w->bodies[i];
            const bool validDynamic=B3_IS_NON_NULL(id) && b3Body_GetType(id)==b3_dynamicBody && b3Body_IsEnabled(id);
            const bool awake=validDynamic && b3Body_IsAwake(id);
            if(!validDynamic || !awake){
                if(i<w->lowSpeedTime.size())w->lowSpeedTime[i]=0.0f;
                if(i<w->sleepPrevValid.size())w->sleepPrevValid[i]=0;
                if(i<w->sleepAwakeTime.size())w->sleepAwakeTime[i]=0.0f;
                if(i<w->sleepWasAwake.size())w->sleepWasAwake[i]=0;
                continue;
            }

            // A transition from sleeping -> awake starts a fresh grace interval.
            if(i<w->sleepWasAwake.size() && !w->sleepWasAwake[i]){
                w->sleepAwakeTime[i]=0.0f;
                w->lowSpeedTime[i]=0.0f;
                w->sleepPrevValid[i]=0;
            }
            if(i<w->sleepWasAwake.size())w->sleepWasAwake[i]=1;
            if(i<w->sleepAwakeTime.size())w->sleepAwakeTime[i]+=step;

            b3Vec3 lv=b3Body_GetLinearVelocity(id), av=b3Body_GetAngularVelocity(id);
            const float ls=lv.x*lv.x+lv.y*lv.y+lv.z*lv.z, as=av.x*av.x+av.y*av.y+av.z*av.z;
            const b3Pos cp=b3Body_GetPosition(id);
            const b3Quat cq=b3Body_GetRotation(id);
            const bool velocityLow=(ls<=l2 && as<=a2);
            const bool velocityGuard=(ls<=lg2 && as<=ag2);

            // Never force-sleep immediately after creation/wake.
            if(i<w->sleepAwakeTime.size() && w->sleepAwakeTime[i] < minAwakeTime){
                w->lowSpeedTime[i]=0.0f;
                w->sleepPrevValid[i]=0;
                continue;
            }

            // Only enter the candidate state when motion is genuinely low.
            if(i>=w->sleepPrevValid.size() || !w->sleepPrevValid[i]){
                if(velocityLow){
                    w->sleepPrevPos[i]=cp;
                    w->sleepPrevRot[i]=cq;
                    w->sleepPrevValid[i]=1;
                    w->lowSpeedTime[i]=0.0f;
                } else {
                    w->lowSpeedTime[i]=0.0f;
                }
                continue;
            }

            // Compare against the candidate ANCHOR, not the previous simulation step.
            const b3Pos ap=w->sleepPrevPos[i];
            const float dx=float(cp.x-ap.x), dy=float(cp.y-ap.y), dz=float(cp.z-ap.z);
            const float linearDelta=std::sqrt(std::max(0.0f,dx*dx+dy*dy+dz*dz));
            const b3Quat aq=w->sleepPrevRot[i];
            float qdot=std::fabs(cq.v.x*aq.v.x+cq.v.y*aq.v.y+cq.v.z*aq.v.z+cq.s*aq.s);
            qdot=std::max(0.0f,std::min(1.0f,qdot));
            const float dangle=2.0f*std::acos(qdot);
            const float radius=(i<w->sleepRadius.size())?std::max(0.0f,w->sleepRadius[i]):0.0f;
            const float surfaceDelta=linearDelta + dangle*radius;
            const bool stayedNearAnchor=(surfaceDelta<=anchorPosEps && dangle<=anchorRotEps);

            if(stayedNearAnchor && velocityGuard){
                w->lowSpeedTime[i]+=step;
                if(w->lowSpeedTime[i]>=w->sleepDelay){
                    b3Body_SetLinearVelocity(id,{0,0,0});
                    b3Body_SetAngularVelocity(id,{0,0,0});
                    b3Body_SetAwake(id,false);
                    w->lowSpeedTime[i]=0.0f;
                    w->sleepPrevValid[i]=0;
                    w->sleepAwakeTime[i]=0.0f;
                    w->sleepWasAwake[i]=0;
                }
            } else {
                // Any accumulated drift or renewed motion restarts the whole candidate window.
                w->lowSpeedTime[i]=0.0f;
                w->sleepPrevValid[i]=0;
            }
        }
    }
    return 1;
}
BP_API int bp_get_transforms(void* p,const int* handles,int count,float* out7,uint8_t* awake){ auto* w=(BPWorld*)p; if(!w||!handles||!out7)return 0; for(int i=0;i<count;i++){ auto id=body(w,handles[i]); if(B3_IS_NULL(id))return 0; b3Pos p0=b3Body_GetPosition(id); b3Quat q=b3Body_GetRotation(id); float* o=out7+7*i; o[0]=bp_unscale(w,float(p0.x));o[1]=bp_unscale(w,float(p0.y));o[2]=bp_unscale(w,float(p0.z));o[3]=q.v.x;o[4]=q.v.y;o[5]=q.v.z;o[6]=q.s; if(awake)awake[i]=b3Body_IsAwake(id)?1:0;} return 1; }


// -----------------------------------------------------------------------------
// High-throughput Rigid Body helpers
// -----------------------------------------------------------------------------

struct BPTransformCache {
    int frameCount = 0;
    int bodyCount = 0;
    float fps = 24.0f;
    std::vector<float> transforms; // frame-major, body-major: px,py,pz,qx,qy,qz,qw
    std::filesystem::path diskPath;
    std::ofstream diskWriter;
    std::vector<float> block;
    int blockFrames = 0;

    bool onDisk() const { return !diskPath.empty(); }
    void initializeDisk(const std::filesystem::path& path) {
        diskPath=path;
        // At most 8 MiB plus one body track. Extremely large scenes still need one frame.
        const size_t frameValues=size_t(bodyCount)*7u;
        blockFrames=int(std::max(size_t(1),std::min(size_t(32),size_t(8*1024*1024)/(frameValues*sizeof(float)))));
        block.resize(frameValues*size_t(blockFrames));
        diskWriter.exceptions(std::ios::failbit|std::ios::badbit);
        diskWriter.open(diskPath,std::ios::binary|std::ios::trunc);
        const char magic[8]={'P','U','P','C','A','C','H','1'};
        const uint32_t dimensions[4]={1u,uint32_t(frameCount),uint32_t(bodyCount),uint32_t(blockFrames)};
        const uint32_t tail[9]={7u,0u,0u,0u,0u,0u,0u,0u,0u};
        static_assert(sizeof(float)==4,"Cache requires float32");
        // Windows x64 little-endian v1 header, exactly 64 bytes. Complete flag is at byte 32.
        diskWriter.write(magic,8);
        diskWriter.write(reinterpret_cast<const char*>(dimensions),sizeof(dimensions));
        diskWriter.write(reinterpret_cast<const char*>(&fps),sizeof(fps));
        diskWriter.write(reinterpret_cast<const char*>(tail),sizeof(tail));
    }
    float* frameDestination(int frame,int bodyIndex) {
        const size_t local=onDisk()?size_t(frame%blockFrames):size_t(frame);
        return (onDisk()?block.data():transforms.data())+(local*size_t(bodyCount)+size_t(bodyIndex))*7u;
    }
    void finishFrame(int frame) {
        if(!onDisk())return;
        const int count=frame%blockFrames+1;
        if(count!=blockFrames && frame+1!=frameCount)return;
        // Each block is body-major, so reading a single object's animation takes one read per block.
        std::vector<float> track(size_t(count)*7u);
        for(int i=0;i<bodyCount;++i){
            for(int f=0;f<count;++f)
                std::memcpy(track.data()+size_t(f)*7u,block.data()+(size_t(f)*size_t(bodyCount)+size_t(i))*7u,7u*sizeof(float));
            diskWriter.write(reinterpret_cast<const char*>(track.data()),std::streamsize(track.size()*sizeof(float)));
        }
    }
    void finalizeDisk() {
        if(!onDisk())return;
        diskWriter.flush();
        diskWriter.seekp(32,std::ios::beg);
        const uint32_t complete=1;
        diskWriter.write(reinterpret_cast<const char*>(&complete),sizeof(complete));
        diskWriter.flush();diskWriter.close();
        std::vector<float>().swap(block);
    }
};

// -----------------------------------------------------------------------------
// High-throughput Physics Drop offline helper
// -----------------------------------------------------------------------------

struct BPDropBakeJob {
    BPWorld* world = nullptr; // owned by Python until job is destroyed
    std::vector<int> handles;
    std::vector<float> targets; // optional xyz anchors for surface magnetization
    float spawnRate = 20.0f;
    float settleTime = 3.0f;
    float physicsHz = 30.0f;
    int frequencyMultiplier = 1;
    float magnetStrength = 55.0f;
    float magnetDamping = 10.0f;
    bool magnetized = false;
    bool captureFrames = false;
    float outputFps = 24.0f;
    int requestedFrames = 0;
    std::filesystem::path cachePath;
    BPTransformCache* cache = nullptr;
    std::atomic<int> progress{0}; // 0..1000
    std::atomic<int> state{0}; // 0 running, 1 complete, -1 error, -2 cancelled
    std::atomic<int> cancel{0};
    std::vector<float> finalTransforms;
    std::string error;
    std::thread worker;
};

static void bp_drop_bake_worker(BPDropBakeJob* j){
    try {
        if(!j || !j->world || j->handles.empty()){
            if(j){ j->error="Invalid native Drop bake job"; j->state=-1; }
            return;
        }
        const int count=(int)j->handles.size();
        const double dt=1.0/std::max(1.0f,j->physicsHz);
        const double spawnRate=std::max(1.0f,j->spawnRate);
        const double spawnDuration=count>1 ? (double)(count-1)/spawnRate : 0.0;
        const double endTime=(j->captureFrames && j->requestedFrames>0)
            ? double(j->requestedFrames-1)/double(j->outputFps)
            : spawnDuration+std::max(0.0f,j->settleTime);
        const double stepCount=std::ceil(endTime/dt)+1;
        if(!std::isfinite(stepCount)||stepCount>std::numeric_limits<int>::max())throw std::runtime_error("Bake duration exceeds native step limit");
        const int totalSteps=std::max(1,(int)stepCount);
        int active=0;
        int outFrame=0;
        int outFrames=0;
        if(j->captureFrames){
            const double requestedFrames=j->requestedFrames>0 ? double(j->requestedFrames) : std::ceil(endTime*j->outputFps)+1;
            if(!std::isfinite(requestedFrames)||requestedFrames>std::numeric_limits<int>::max())throw std::runtime_error("Bake exceeds native frame limit");
            outFrames=std::max(1,(int)requestedFrames);
            if(uint64_t(count)>(uint64_t(std::numeric_limits<int64_t>::max())-64u)/(uint64_t(outFrames)*28u))throw std::runtime_error("Bake cache exceeds file size limit");
            j->cache=new BPTransformCache(); j->cache->frameCount=outFrames; j->cache->bodyCount=count; j->cache->fps=j->outputFps;
            if(!j->cachePath.empty())j->cache->initializeDisk(j->cachePath);
            else {
                if(size_t(count)>std::numeric_limits<size_t>::max()/size_t(outFrames)/7u/sizeof(float))throw std::runtime_error("Bake cache exceeds memory address space");
                j->cache->transforms.resize((size_t)outFrames*(size_t)count*7u);
            }
        }
        auto captureFrame=[&](int f){
            if(!j->cache || f<0 || f>=j->cache->frameCount) return;
            for(int i=0;i<count;++i){
                b3BodyId id=body(j->world,j->handles[(size_t)i]);
                float* o=j->cache->frameDestination(f,i);
                if(B3_IS_NULL(id)){ for(int k=0;k<7;++k)o[k]=0.0f; o[6]=1.0f; continue; }
                b3Pos p=b3Body_GetPosition(id); b3Quat q=b3Body_GetRotation(id);
                o[0]=bp_unscale(j->world,(float)p.x); o[1]=bp_unscale(j->world,(float)p.y); o[2]=bp_unscale(j->world,(float)p.z); o[3]=q.v.x; o[4]=q.v.y; o[5]=q.v.z; o[6]=q.s;
            }
            j->cache->finishFrame(f);
        };
        if(j->captureFrames){ captureFrame(0); outFrame=1; }
        for(int step=0; step<totalSteps; ++step){
            if(j->cancel.load()){ j->state=-2; return; }
            const double t=(double)step*dt;
            const int wanted=std::min(count,1+(int)std::floor(t*spawnRate));
            while(active<wanted){
                b3BodyId id=body(j->world,j->handles[(size_t)active]);
                if(B3_IS_NON_NULL(id) && !b3Body_IsEnabled(id)) b3Body_Enable(id);
                ++active;
            }
            if(j->magnetized && active>0 && j->targets.size()>=(size_t)active*3u){
                bp_apply_point_springs(j->world,j->handles.data(),j->targets.data(),active,j->magnetStrength,j->magnetDamping,1.0f);
            }
            bp_step(j->world,(float)dt,std::max(1,std::min(16,j->frequencyMultiplier)));
            if(j->captureFrames && j->cache){
                const double simTime=(double)(step+1)*dt;
                while(outFrame<j->cache->frameCount && (double)outFrame/(double)j->cache->fps <= simTime + 1.0e-9){ captureFrame(outFrame); ++outFrame; }
            }
            j->progress=(int)std::min(1000.0,std::floor(1000.0*(double)(step+1)/(double)totalSteps));
        }
        if(j->captureFrames && j->cache){ while(outFrame<j->cache->frameCount){ captureFrame(outFrame); ++outFrame; } }
        j->finalTransforms.resize((size_t)count*7u);
        for(int i=0;i<count;++i){
            b3BodyId id=body(j->world,j->handles[(size_t)i]);
            if(B3_IS_NULL(id)){ j->error="Could not capture Drop final transform"; j->state=-1; return; }
            b3Pos p=b3Body_GetPosition(id); b3Quat q=b3Body_GetRotation(id);
            float* o=j->finalTransforms.data()+7u*(size_t)i;
            o[0]=bp_unscale(j->world,(float)p.x); o[1]=bp_unscale(j->world,(float)p.y); o[2]=bp_unscale(j->world,(float)p.z);
            o[3]=q.v.x; o[4]=q.v.y; o[5]=q.v.z; o[6]=q.s;
        }
        if(j->cache)j->cache->finalizeDisk();
        j->progress=1000; j->state=1;
    } catch(const std::exception& e){
        if(j){ j->error=e.what(); j->state=-1; }
    } catch(...){
        if(j){ j->error="Native Drop bake failed"; j->state=-1; }
    }
}

static void* bp_drop_bake_begin_impl(void* p,const int* handles,int count,const float* targets_xyz,float spawn_rate,float settle_time,float physics_hz,int frequency_multiplier,float magnet_strength,float magnet_damping,int magnetized,int capture_frames,float output_fps,const char* cache_path,int requested_frames=0){
    auto* w=(BPWorld*)p;
    if(!w || !handles || count<=0 || !(spawn_rate>0.0f) || !(physics_hz>0.0f)) return nullptr;
    if(capture_frames && (!std::isfinite(output_fps) || output_fps<=0.0f || requested_frames<0)){set_error("Invalid bake frame range or FPS");return nullptr;}
    try {
        auto owner=std::make_unique<BPDropBakeJob>(); auto* j=owner.get(); j->world=w; j->handles.assign(handles,handles+count);
        if(targets_xyz) j->targets.assign(targets_xyz,targets_xyz+(size_t)count*3u);
        j->spawnRate=spawn_rate; j->settleTime=std::max(0.0f,settle_time); j->physicsHz=physics_hz;
        j->frequencyMultiplier=std::max(1,std::min(16,frequency_multiplier));
        j->magnetStrength=magnet_strength; j->magnetDamping=magnet_damping; j->magnetized=(magnetized!=0); j->captureFrames=(capture_frames!=0); j->outputFps=std::max(1.0e-6f,output_fps);j->requestedFrames=requested_frames;
        if(capture_frames && cache_path && cache_path[0])j->cachePath=std::filesystem::u8path(cache_path);
        j->worker=std::thread(bp_drop_bake_worker,j); return owner.release();
    } catch(...){ set_error("Could not start native Drop bake"); return nullptr; }
}
BP_API void* bp_drop_bake_begin(void* p,const int* handles,int count,const float* targets_xyz,float spawn_rate,float settle_time,float physics_hz,int frequency_multiplier,float magnet_strength,float magnet_damping,int magnetized,int capture_frames,float output_fps){
    return bp_drop_bake_begin_impl(p,handles,count,targets_xyz,spawn_rate,settle_time,physics_hz,frequency_multiplier,magnet_strength,magnet_damping,magnetized,capture_frames,output_fps,nullptr);
}
BP_API void* bp_drop_bake_begin_disk(void* p,const int* handles,int count,const float* targets_xyz,float spawn_rate,float settle_time,float physics_hz,int frequency_multiplier,float magnet_strength,float magnet_damping,int magnetized,int capture_frames,float output_fps,const char* cache_path){
    if(!cache_path || !cache_path[0] || !capture_frames){set_error("Disk bake requires a cache path and frame capture");return nullptr;}
    return bp_drop_bake_begin_impl(p,handles,count,targets_xyz,spawn_rate,settle_time,physics_hz,frequency_multiplier,magnet_strength,magnet_damping,magnetized,capture_frames,output_fps,cache_path);
}
BP_API void* bp_drop_bake_begin_cached(void* p,const int* handles,int count,const float* targets_xyz,float spawn_rate,float settle_time,float physics_hz,int frequency_multiplier,float magnet_strength,float magnet_damping,int magnetized,int capture_frames,float output_fps,const char* cache_path,int requested_frames){
    if(!capture_frames || requested_frames<1){set_error("Cached bake requires an exact positive frame count");return nullptr;}
    return bp_drop_bake_begin_impl(p,handles,count,targets_xyz,spawn_rate,settle_time,physics_hz,frequency_multiplier,magnet_strength,magnet_damping,magnetized,capture_frames,output_fps,cache_path,requested_frames);
}
BP_API int bp_drop_bake_state(void* p){ auto* j=(BPDropBakeJob*)p; return j?j->state.load():-1; }
BP_API int bp_drop_bake_progress(void* p){ auto* j=(BPDropBakeJob*)p; return j?j->progress.load():0; }
BP_API const char* bp_drop_bake_error(void* p){ auto* j=(BPDropBakeJob*)p; return j?j->error.c_str():"Invalid Drop bake job"; }
BP_API void bp_drop_bake_cancel(void* p){ auto* j=(BPDropBakeJob*)p; if(j)j->cancel=1; }
BP_API int bp_drop_bake_get_transforms(void* p,float* out7){
    auto* j=(BPDropBakeJob*)p; if(!j || !out7 || j->state.load()!=1 || j->finalTransforms.empty())return 0;
    std::memcpy(out7,j->finalTransforms.data(),j->finalTransforms.size()*sizeof(float)); return 1;
}
BP_API void* bp_drop_bake_take_cache(void* p){
    auto* j=(BPDropBakeJob*)p; if(!j || j->state.load()!=1 || !j->cache)return nullptr;
    if(j->worker.joinable()) j->worker.join(); auto* c=j->cache; j->cache=nullptr; return c;
}
BP_API void bp_drop_bake_destroy(void* p){
    auto* j=(BPDropBakeJob*)p; if(!j)return; j->cancel=1; if(j->worker.joinable())j->worker.join(); delete j->cache; delete j;
}

BP_API int bp_cache_frame_count(void* p){ auto* c=(BPTransformCache*)p; return c?c->frameCount:0; }
BP_API int bp_cache_body_count(void* p){ auto* c=(BPTransformCache*)p; return c?c->bodyCount:0; }
BP_API float bp_cache_fps(void* p){ auto* c=(BPTransformCache*)p; return c?c->fps:0.0f; }
BP_API int bp_cache_get_body_track(void* p,int body_index,float* out7frames){
    auto* c=(BPTransformCache*)p; if(!c || !out7frames || body_index<0 || body_index>=c->bodyCount)return 0;
    if(c->onDisk()){
        try {
            std::ifstream input(c->diskPath,std::ios::binary);
            input.exceptions(std::ios::failbit|std::ios::badbit);
            for(int first=0;first<c->frameCount;){
                const int count=std::min(c->blockFrames,c->frameCount-first);
                const uint64_t offset=64u+uint64_t(first)*uint64_t(c->bodyCount)*28u+uint64_t(body_index)*uint64_t(count)*28u;
                input.seekg(std::streamoff(offset),std::ios::beg);
                input.read(reinterpret_cast<char*>(out7frames+size_t(first)*7u),std::streamsize(count)*28);
                first+=count;
            }
            return 1;
        } catch(const std::exception& e){set_error(e.what());return 0;}
    }
    for(int f=0;f<c->frameCount;++f){
        const float* src=c->transforms.data()+((size_t)f*(size_t)c->bodyCount+(size_t)body_index)*7u;
        std::memcpy(out7frames+(size_t)f*7u,src,7u*sizeof(float));
    }
    return 1;
}
BP_API void bp_cache_destroy(void* p){ delete (BPTransformCache*)p; }


BP_API int bp_add_heightfield_static(void* p,float px,float py,float pz,float qx,float qy,float qz,float qw,const float* heights,const uint8_t* materials,int nx,int nz,float dx,float dz,float friction,float restitution){
 auto* w=(BPWorld*)p;
 if(!w||!heights||nx<2||nz<2||nx>2048||nz>2048||!std::isfinite(dx)||!std::isfinite(dz)||dx<=0||dz<=0){set_error("Invalid terrain grid");return -1;}
 std::vector<float> samples(heights,heights+size_t(nx)*nz);
 for(float h:samples) if(!std::isfinite(h)){set_error("Nonfinite terrain height");return -1;}
 auto bounds=std::minmax_element(samples.begin(),samples.end());
 b3HeightFieldDef def{};def.heights=samples.data();def.materialIndices=const_cast<uint8_t*>(materials);
 def.countX=nx;def.countZ=nz;def.scale={dx*w->solverScale,w->solverScale,dz*w->solverScale};
 def.globalMinimumHeight=*bounds.first-0.001f;def.globalMaximumHeight=*bounds.second+0.001f;
 def.clockwiseWinding=false;
 auto* hf=b3CreateHeightField(&def);if(!hf){set_error("Height field creation failed");return -1;}
 auto id=create_body(w,0,px,py,pz,qx,qy,qz,qw);auto sd=make_shape(friction,restitution,0);
 b3CreateHeightFieldShape(id,&sd,hf);w->heightFields.push_back(hf);return add_id(w,id);
}
