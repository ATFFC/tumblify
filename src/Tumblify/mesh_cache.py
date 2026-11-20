# SPDX-FileCopyrightText: 2026 CG Corner addons
# SPDX-License-Identifier: GPL-3.0-only
# Tumblify is distributed under GNU GPL version 3. See LICENSE.

"""Bounded, immutable, content-addressed Mesh preprocessing cache.

Only packed geometry is retained. Native worlds and native mesh handles are never shared.
Evaluated vertices/topology are hashed on every lookup, including dependency-driven changes.
"""
from collections import OrderedDict
from array import array
import hashlib,struct

_LIMIT_BYTES=256*1024*1024
_entries=OrderedDict()
_bytes=0
_hits=0
_misses=0

class PackedVertices:
    __slots__=('packed_bytes','_values')
    def __init__(self,values):
        self.packed_bytes=array('f',(float(c) for p in values for c in p)).tobytes()
        self._values=memoryview(self.packed_bytes).cast('f')
    def __len__(self):return len(self._values)//3
    def __getitem__(self,index):
        if isinstance(index,slice):return [self[i] for i in range(*index.indices(len(self)))]
        if index<0:index+=len(self)
        if index<0 or index>=len(self):raise IndexError(index)
        k=index*3;return (self._values[k],self._values[k+1],self._values[k+2])
    def __iter__(self):
        v=self._values
        for i in range(0,len(v),3):yield (v[i],v[i+1],v[i+2])

class PackedIndices:
    __slots__=('packed_bytes','_values')
    def __init__(self,values):
        self.packed_bytes=array('i',values).tobytes()
        self._values=memoryview(self.packed_bytes).cast('i')
    def __len__(self):return len(self._values)
    def __getitem__(self,index):
        if isinstance(index,slice):return list(self._values[index])
        return self._values[index]
    def __iter__(self):return iter(self._values)

def mesh_key(mesh,scale,settings,target):
    # Bulk reads are fast and avoid a fragile hand-maintained dependency/modifier signature.
    coordinates=array('f',[0.0])*(len(mesh.vertices)*3)
    triangles=array('i',[0])*(len(mesh.loop_triangles)*3)
    mesh.vertices.foreach_get('co',coordinates)
    mesh.loop_triangles.foreach_get('vertices',triangles)
    h=hashlib.blake2b(digest_size=24)
    h.update(memoryview(coordinates).cast('B'));h.update(memoryview(triangles).cast('B'))
    h.update(struct.pack('<QQ3d',len(mesh.vertices),len(mesh.loop_triangles),*map(float,scale)))
    # Preserve original polygon/edge structure too: equal triangulations can have different modifier input.
    for collection,field,width,kind in ((mesh.edges,'vertices',2,'i'),(mesh.loops,'vertex_index',1,'i'),(mesh.loops,'edge_index',1,'i'),(mesh.polygons,'loop_start',1,'i'),(mesh.polygons,'loop_total',1,'i'),(mesh.polygons,'material_index',1,'i'),(mesh.polygons,'use_smooth',1,'b')):
        values=array(kind,[0])*(len(collection)*width)
        collection.foreach_get(field,values)
        h.update(struct.pack('<Q',len(values)));h.update(memoryview(values).cast('B'))
    # Solidify/Decimate can also receive mesh custom data. Include it conservatively.
    for attribute in sorted(mesh.attributes,key=lambda a:a.name):
        if attribute.name=='position':continue
        header=repr((attribute.name,attribute.domain,attribute.data_type,len(attribute.data))).encode('utf-8')
        h.update(struct.pack('<I',len(header)));h.update(header)
        if not len(attribute.data):continue
        first=attribute.data[0]
        for prop in first.bl_rna.properties:
            if prop.identifier=='rna_type' or prop.type not in {'FLOAT','INT','BOOLEAN','STRING'}:continue
            field=prop.identifier
            width=prop.array_length if prop.is_array else 1
            if prop.type=='STRING':
                for item in attribute.data:
                    value=str(getattr(item,field)).encode('utf-8','surrogatepass')
                    h.update(struct.pack('<I',len(value)));h.update(value)
            else:
                kind='f' if prop.type=='FLOAT' else ('b' if prop.type=='BOOLEAN' else 'i')
                values=array(kind,[0])*(len(attribute.data)*width)
                attribute.data.foreach_get(field,values)
                h.update(memoryview(values).cast('B'))
    h.update(repr((settings.mesh_quality,float(settings.mesh_detail_percent),settings.mesh_thickness_mode,float(settings.mesh_thickness),int(target))).encode('utf-8'))
    return h.digest()

def get_mesh(key):
    global _hits,_misses
    if key is None:_misses+=1;return None
    item=_entries.get(key)
    if item is None:_misses+=1;return None
    _entries.move_to_end(key);_hits+=1
    return item[0]

def put_mesh(key,vertices,indices,source_triangles,collision_triangles):
    global _bytes
    value=(PackedVertices(vertices),PackedIndices(indices),int(source_triangles),int(collision_triangles))
    size=len(value[0].packed_bytes)+len(value[1].packed_bytes)+1024
    old=_entries.pop(key,None)
    if old:_bytes-=old[1]
    if key is not None and size<=_LIMIT_BYTES:
        while _entries and _bytes+size>_LIMIT_BYTES:
            _,(_,released)=_entries.popitem(last=False);_bytes-=released
        _entries[key]=(value,size);_bytes+=size
    return value

def clear_mesh_cache(*_args):
    global _bytes,_hits,_misses
    _entries.clear();_bytes=0;_hits=0;_misses=0

def mesh_cache_info():
    return {'hits':_hits,'misses':_misses,'entries':len(_entries),'bytes':_bytes,'limit_bytes':_LIMIT_BYTES}
