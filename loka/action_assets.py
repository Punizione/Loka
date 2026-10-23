"""Load preview assets in a worker, following compiled material search paths."""
from dataclasses import replace

from .action_materials import MaterialResolver, PreviewMaterial
from .action_preview import PreviewModel
from .vpk import safe_archive_path


def resolve_model_material(resolver,name,search_directories):
    warnings=[]
    normalized=name.replace('\\','/').removesuffix('.vmt').strip('/')
    directories=[str(d).replace('\\','/').strip('/') for d in search_directories] or ['']
    candidates=list(dict.fromkeys((directory+'/' if directory else '')+normalized for directory in directories))
    if normalized not in candidates:candidates.append(normalized)
    for candidate in candidates:
        try:
            key=safe_archive_path('materials/'+candidate+'.vmt').lower()
            if resolver._read(key,warnings) is None:continue
            material=resolver.resolve(candidate)
            return replace(material,warnings=tuple(dict.fromkeys([*warnings,*material.warnings])))
        except (ValueError,OSError) as error:warnings.append(str(error))
    return PreviewMaterial(name,warnings=tuple(warnings+[f'未找到模型材质: {name}；搜索目录: '+', '.join(directories)]))


def load_preview_assets(session, *, cancelled=None, reload_models=False):
    def check():
        if cancelled and cancelled():raise InterruptedError('动作矫正预览载入已取消。')
    check();session.check_unchanged()
    if reload_models:session._preview_models.clear()
    resolver=MaterialResolver(session.pack_dir,session.options.game_path)
    models=[];materials=[]
    for index,item in enumerate(session.models):
        check()
        model=session._preview_models.get(index)
        if model is None:
            model=PreviewModel.load(item.model_path)
            session._preview_models[index]=model
        from .action_reload_preview import configure_session_reload_preview
        configure_session_reload_preview(session,index,model)
        from .action_qc_preview import configure_session_qc_preview
        configure_session_qc_preview(session,index,model)
        cache={};bound=[]
        for mesh in model.meshes:
            check()
            if mesh.material not in cache:
                cache[mesh.material]=resolve_model_material(resolver,mesh.material,model.data.theTexturePaths or [])
            bound.append(cache[mesh.material])
        models.append(model);materials.append(bound)
    check();session.check_unchanged()
    return session,models,materials
