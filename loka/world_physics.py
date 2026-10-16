"""Include-only weapon world poses and strict collision-hull roundtrip checks."""
from copy import copy,deepcopy
import hashlib,json,math,os,struct
from pathlib import Path

MANIFEST='_loka_world_physics.json'
SOURCE_PHY='_loka_world_physics_source.phy'
SURFACE_TOLERANCE=.001*.0254  # one thousandth of a Source unit, in PHY meters


def _leaf_meshes(phy):
    solids=phy.theSourcePhyCollisionDatas or []
    if len(solids)!=1:raise ValueError('include W 碰撞体暂需单一 solid')
    meshes=[m for m in solids[0].theConvexMeshes if not m.flags & 3]
    if len(meshes)!=1:raise ValueError('include W 碰撞体暂需单一凸包')
    return meshes


def collision_signature(phy):
    mesh=_leaf_meshes(phy)[0]
    vertices=[[v.vertex.x,v.vertex.y,v.vertex.z] for v in mesh.theVertices]
    faces=[list(f.vertexIndex) for f in mesh.theFaces]
    return {'vertices':vertices,'faces':faces}


def _planes(shape):
    vertices=shape['vertices'];faces=shape['faces']
    if len(vertices)<4 or len(faces)<4 or any(len(p)!=3 or not all(math.isfinite(v) for v in p) for p in vertices):
        raise ValueError('碰撞体顶点无效')
    center=[sum(p[i] for p in vertices)/len(vertices) for i in range(3)]
    planes=[]
    for face in faces:
        if len(face)!=3 or any(isinstance(i,bool) or not isinstance(i,int) or not 0<=i<len(vertices) for i in face):
            raise ValueError('碰撞体面索引无效')
        a,b,c=(vertices[i] for i in face)
        u=[b[i]-a[i] for i in range(3)];v=[c[i]-a[i] for i in range(3)]
        n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
        length=math.sqrt(sum(x*x for x in n))
        if length<1e-15:continue
        n=[x/length for x in n]
        if sum(n[i]*(center[i]-a[i]) for i in range(3))>0:n=[-x for x in n]
        planes.append((n,sum(n[i]*a[i] for i in range(3))))
    if len(planes)<4:raise ValueError('碰撞体没有有效封闭面')
    return planes


def validate_collision_signature(expected,actual):
    source_planes=_planes(expected);target_planes=_planes(actual)
    error=max(0.,max(sum(n[i]*p[i] for i in range(3))-d
                    for points,planes in ((expected['vertices'],target_planes),(actual['vertices'],source_planes))
                    for p in points for n,d in planes))
    if error>SURFACE_TOLERANCE:
        raise ValueError(f'include W 回编碰撞体形状/坐标不符: {error:.9g} m')
    return {'max_surface_error_meters':error,'source_vertices':len(expected['vertices']),
            'target_vertices':len(actual['vertices'])}


def _same_bones(left,right):
    if len(left)!=len(right):return False
    for a,b in zip(left,right):
        if (a.theName,a.parentBoneIndex)!=(b.theName,b.parentBoneIndex):return False
        for name in ('position','rotation',*(f'poseToBoneColumn{i}' for i in range(4))):
            x=getattr(a,name);y=getattr(b,name)
            for axis in ('x','y','z'):
                p=getattr(x,axis);q=getattr(y,axis)
                if not math.isfinite(p) or not math.isfinite(q) or abs(p-q)>1e-6:return False
    return True


def _bone_signature(bones):
    return [{'name':b.theName,'parent':b.parentBoneIndex,
             'pose_to_bone':[[getattr(getattr(b,f'poseToBoneColumn{i}'),axis) for axis in ('x','y','z')]
                             for i in range(4)]} for b in bones]


def _same_default_pose(left,right):
    """Recognize a previously converted wrapper without overwriting local actions."""
    from .core.writers.animation import calc_animation,_stored_euler_to_quaternion
    a=left.theAnimationDescs[0];b=right.theAnimationDescs[0];s=left.theSequenceDescs[0]
    # calc_animation does not implement FRAMEANIM, movement extraction or
    # local-hierarchy modifiers. Never equate their bind-pose fallback outputs.
    if any(x.flags & 0x40 or x.movementCount or x.theMovements
           or x.localHierarchyCount or x.theLocalHierarchies for x in (a,b)):return False
    if (a.frameCount!=b.frameCount or not math.isclose(a.fps,b.fps,rel_tol=1e-6)
            or a.flags & 4 or s.flags & 20 or a.animBlock or a.theIkRules
            or s.theAutoLayers or s.theIkLocks or s.theAnimDescIndexes!=[0]
            or any(i>=0 for i in s.paramIndex)):return False
    for frame in range(a.frameCount):
        p=calc_animation(left,None,a,frame);q=calc_animation(right,None,b,frame)
        if p.keys()!=q.keys():return False
        for index,x in p.items():
            y=q[index]
            if any(not math.isfinite(getattr(v,k)) for v in (x.position,y.position,x.rotation,y.rotation) for k in ('x','y','z')):return False
            if any(abs(getattr(x.position,k)-getattr(y.position,k))>.001 for k in ('x','y','z')):return False
            u=_stored_euler_to_quaternion(x.rotation);v=_stored_euler_to_quaternion(y.rotation)
            norm=math.sqrt(sum(getattr(u,k)**2 for k in 'xyzw')*sum(getattr(v,k)**2 for k in 'xyzw'))
            dot=abs(sum(getattr(u,k)*getattr(v,k) for k in 'xyzw'))/norm
            if 2*math.acos(min(1.,dot))>math.radians(.1):return False
    return True


def prepare_include_world_physics(dec,mdl_path,output_path):
    """Materialize the verified provider's default pose for the world compiler."""
    data=dec.mdl_file_data
    if not Path(mdl_path).name.lower().startswith('w_'):
        return
    local=bool(data.localAnimationCount or data.localSequenceCount)
    if local and (data.localAnimationCount!=1 or data.localSequenceCount!=1 or not data.theModelGroups):return
    phy=dec.phy_file_data
    if phy is None:
        if Path(mdl_path).with_suffix('.phy').is_file():raise ValueError('include-only W PHY 无法读取，不能省略碰撞体')
        return
    from .core.constants import STUDIOHDR_FLAGS_STATIC_PROP
    from .decompiler import MdlDecompiler,DecompileOptions
    from .core.mdl_data import SourceMdlFileData49
    from .core.writers.animation import calculate_first_anim_desc_frame_lines_for_physics
    from .vmodel import copy_include_dependencies
    if not data.theModelGroups or data.version<48 or data.flags & STUDIOHDR_FLAGS_STATIC_PROP:
        if local:return
        raise ValueError('include-only W PHY 缺少可验证的动作来源或坐标格式')
    try:meshes=_leaf_meshes(phy)
    except ValueError:
        if local:return
        raise
    if not data.theBones or data.theBones[0].parentBoneIndex!=-1 or (len(data.theBones)>1 and meshes[0].theBoneIndex!=0):
        if local:return
        raise ValueError('include-only W PHY 不是已支持的根骨骼凸包')
    copied=copy_include_dependencies(mdl_path,data,output_path,dec.options.game_path)
    candidates=[]
    for path in copied:
        if Path(path).suffix.lower()!='.mdl':continue
        provider=MdlDecompiler(DecompileOptions(handle_vmodel_includes=False))
        provider.mdl_file_data=SourceMdlFileData49();provider._read_mdl_file(path)
        owner=provider.mdl_file_data
        if not _same_bones(data.theBones,owner.theBones) or not owner.localAnimationCount or not owner.localSequenceCount:
            continue
        if owner.checksum!=phy.checksum and data.checksum!=phy.checksum:continue
        sequence=owner.theSequenceDescs[0];animation=owner.theAnimationDescs[0]
        if (sequence.theAnimDescIndexes!=[0] or sequence.theAutoLayers or sequence.theIkLocks
                or any(i>=0 for i in sequence.paramIndex) or animation.flags & 4 or animation.animBlock
                or animation.theIkRules or sequence.flags & 20 or animation.flags & 0x40
                or animation.movementCount or animation.theMovements
                or animation.localHierarchyCount or animation.theLocalHierarchies):continue
        if local and not _same_default_pose(data,owner):continue
        frame=calculate_first_anim_desc_frame_lines_for_physics(owner)
        if not all(math.isfinite(getattr(v,a)) for v in (frame.position,frame.rotation) for a in ('x','y','z')):
            raise ValueError('include W 动作首帧包含非有限姿态')
        candidates.append((str(path),owner))
    # A PHY-owned provider must be unique; never use an arbitrary first include.
    matching=[c for c in candidates if c[1].checksum==phy.checksum]
    candidates=matching or candidates
    if len(candidates)!=1:
        if local:return
        raise ValueError('include W PHY 未找到唯一且骨架一致的动作提供者')
    path,owner=candidates[0]
    if not local:
        animations,sequences=deepcopy(([owner.theAnimationDescs[0]],[owner.theSequenceDescs[0]]))
        data.theAnimationDescs=animations;data.theSequenceDescs=sequences
        data.localAnimationCount=data.localSequenceCount=1
    data.theIncludeWorldPhysics=collision_signature(phy)
    payload=Path(mdl_path).with_suffix('.phy').read_bytes()
    data.theIncludeWorldPhysics.update(source_phy_sha256=hashlib.sha256(payload).hexdigest(),
        source_phy_checksum=phy.checksum,bones=_bone_signature(data.theBones))
    Path(output_path,SOURCE_PHY).write_bytes(payload)
    dec.result.included_files=list(dict.fromkeys([*dec.result.included_files,*copied]))
    dec.result.written_files.extend(p for p in copied if p not in dec.result.written_files)
    dec.result.warnings=[w for w in dec.result.warnings if w!='PHY file checksum does not match MDL file checksum.']
    dec.result.warnings.append('W 模物理动作已从校验匹配的 include 提供者恢复: '+Path(path).name)


def physics_export_coordinates(data,frame):
    if not getattr(data,'theIncludeWorldPhysics',None):return data,frame
    # This PHY already stores its hull in the root's collision coordinates.
    # Feeding the provider/root bind translations back through the parser's normal
    # single-solid transform applies both a second time (the old 8.4279-unit drift).
    transformed=copy(data);transformed.theBones=deepcopy(data.theBones)
    for axis in ('x','y','z'):setattr(transformed.theBones[0].poseToBoneColumn3,axis,0.)
    adjusted=deepcopy(frame)
    for axis in ('x','y','z'):setattr(adjusted.position,axis,0.)
    return transformed,adjusted


def validate_compiled_world_physics(model,qc):
    manifest=Path(qc).parent/MANIFEST
    if not manifest.is_file():return None
    from .decompiler import MdlDecompiler,DecompileOptions
    from .core.mdl_data import SourceMdlFileData49
    dec=MdlDecompiler(DecompileOptions(handle_vmodel_includes=False))
    dec.mdl_file_data=SourceMdlFileData49();dec._read_mdl_file(str(model));dec._read_phy_file(str(model))
    if dec.phy_file_data is None:raise ValueError('include W 回编碰撞体无法完整读取')
    expected=json.loads(manifest.read_text(encoding='utf-8'))
    return validate_collision_signature(expected,collision_signature(dec.phy_file_data))


def preserve_compiled_world_physics(model,qc,*,staging_root):
    """Retain the source hull when recooking optimizes away collision vertices."""
    manifest=Path(qc).parent/MANIFEST
    if not manifest.is_file():return
    model=Path(model).resolve()
    if not model.is_relative_to(Path(staging_root).resolve()):raise ValueError('W PHY 回填必须在内部编译暂存目录')
    from .decompiler import MdlDecompiler,DecompileOptions
    from .core.mdl_data import SourceMdlFileData49
    dec=MdlDecompiler(DecompileOptions(handle_vmodel_includes=False))
    dec.mdl_file_data=SourceMdlFileData49();dec._read_mdl_file(str(model))
    expected=json.loads(manifest.read_text(encoding='utf-8'));actual=_bone_signature(dec.mdl_file_data.theBones)
    bones=expected['bones']
    if len(actual)!=len(bones):raise ValueError('W PHY 回填骨骼数量改变')
    for a,b in zip(actual,bones):
        if (a['name'],a['parent'])!=(b['name'],b['parent']):raise ValueError('W PHY 回填骨骼映射改变')
        if any(not math.isfinite(x) or not math.isfinite(y) or abs(x-y)>1e-6
               for p,q in zip(a['pose_to_bone'],b['pose_to_bone']) for x,y in zip(p,q)):
            raise ValueError('W PHY 回填绑定坐标改变')
    if dec.mdl_file_data.theBones[0].physicsBoneIndex!=0:raise ValueError('W PHY 回填根骨骼物理索引不符')
    source=(manifest.parent/SOURCE_PHY).resolve()
    if not source.is_relative_to(manifest.parent.resolve()):raise ValueError('W PHY 缓存路径越界')
    payload=source.read_bytes()
    if hashlib.sha256(payload).hexdigest()!=expected['source_phy_sha256'] or len(payload)<16:
        raise ValueError('W PHY 原始缓存已改变')
    if struct.unpack_from('<i',payload,12)[0]!=expected['source_phy_checksum']:raise ValueError('W PHY 来源归属改变')
    patched=bytearray(payload);struct.pack_into('<i',patched,12,dec.mdl_file_data.checksum)
    physical=model.with_suffix('.phy');pending=physical.with_suffix('.phy.pending')
    pending.write_bytes(patched);os.replace(pending,physical)
    validate_compiled_world_physics(model,qc)
