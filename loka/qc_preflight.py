"""Prepare conversion-only QC names from indexed MDL facts, before planning.

Do not guess at quote boundaries in malformed QC text. Re-emit a checked copy
from the already decoded descriptors, retaining their exported SMD paths and
subtract/IK decisions. The original QC, SMDs and input binaries stay untouched.
"""
from pathlib import Path
import shutil
import unicodedata

from .core.writers.qc import QcFile49
from .core.naming import create_corrective_animation_name,create_corrective_animation_smd_relative_path_file_name
from .decompiler import SmdTextWriter
from .weapon_qc import parse_qc, _resource_tokens, _patch


def _clean_name(value):
    return ''.join(c for c in value if c not in '\"\'`“”‘’'
                   and unicodedata.category(c) not in ('Cc','Cf')).strip()


class _PreparedQcWriter(QcFile49):
    def __init__(self,*args,corrective_bindings=(),**kwargs):
        super().__init__(*args,**kwargs)
        self.corrective_bindings=corrective_bindings

    def set_up_corrective_subtract_animation_blocks(self):
        # Exported SMDs already embody this setup. Re-selecting an idle after
        # renaming would risk changing its basis without re-exporting the SMD.
        pass

    def write_corrective_animation_block(self):
        for name,path in self.corrective_bindings:
            self._emit()
            self._emit('$animation "'+name+'" "'+path+'" {')
            self._emit('}')


def _symbol_fields(data):
    for kind,items in (('sequence',data.theSequenceDescs or []),
                       ('animation',data.theAnimationDescs or []),
                       ('weightlist',data.theWeightLists or [])):
        for index,item in enumerate(items):
            for attribute in ('theName','theCorrectiveAnimationName',
                              'theSemanticAnimationName','theSemanticSubtractName','theCorrectiveAnimationSymbol'):
                value=getattr(item,attribute,'')
                # Empty animation names are implied references, not declarations.
                if value or (kind=='sequence' and attribute=='theName'):
                    yield kind,index,item,attribute,value


def _repair_resources(text,root,model,repairs):
    changes=[];copies={}
    for token in _resource_tokens(text):
        if _clean_name(token.value)==token.value:continue
        source=(root/token.value.replace('\\','/')).resolve()
        if not source.is_relative_to(root.resolve()) or not source.is_file():
            raise ValueError('QC 前置校验：资源缺失或越界: '+str(source))
        if source not in copies:
            index=len(copies)
            while True:
                relative=f'loka_qc_assets/resource_{index}'+source.suffix.lower()
                destination=root/relative
                if not destination.exists():break
                index+=1
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,destination);copies[source]=relative
            repairs.append(dict(model=model,kind='resource',original=token.value,repaired=relative))
        changes.append((token.start,token.end,'"'+copies[source]+'"'))
    return _patch(text,changes)


def prepare_conversion_qcs(models):
    """Return ``(model -> checked QC path, repair records)``.

    ``models`` contains ``(model key, completed MdlDecompiler)`` pairs. All
    names are reserved together so a repaired include name cannot shadow a
    distinct valid primary name. Equal original labels across models share a
    mapping, preserving primary-over-include overrides. Local duplicates get
    distinct indexed symbols so none of their motions or layers are discarded.
    """
    models=list(models);fields=[];repairs=[];correctives={}
    for model,dec in models:
        fields.extend((model,*field) for field in _symbol_fields(dec.mdl_file_data))
        correctives[model]=[(getattr(animation,'theCorrectiveAnimationSymbol','') or create_corrective_animation_name(animation.theName),
            create_corrective_animation_smd_relative_path_file_name(animation.theName,dec.result.model_name,
                registry=dec.registry)) for animation in dec.mdl_file_data.theCorrectiveAnimationDescs or []]
    values=list(dict.fromkeys(field[-1] for field in fields))
    reserved={value.casefold() for value in values if value and _clean_name(value)==value}
    mapping={value:value for value in values if value and _clean_name(value)==value}
    repaired_labels={}
    for value in values:
        if value in mapping:continue
        if value.casefold() in repaired_labels:
            mapping[value]=repaired_labels[value.casefold()]
            continue
        base=_clean_name(value) or 'loka_unnamed'
        candidate=base;index=1
        while candidate.casefold() in reserved:
            candidate=base+'_loka_'+str(index);index+=1
        reserved.add(candidate.casefold());mapping[value]=candidate
        repaired_labels[value.casefold()]=candidate
    for model,kind,index,item,attribute,original in fields:
        repaired=mapping[original]
        if repaired==original:continue
        setattr(item,attribute,repaired)
        repairs.append(dict(model=model,kind=kind,index=index,attribute=attribute,
                            original=original,repaired=repaired))

    # MDL layers address sequence indexes, so equal labels can still describe
    # different motions. Keep every descriptor and re-emit indexed references
    # with a distinct conversion-only symbol for each later occurrence.
    for model,dec in models:
        seen=set()
        for index,sequence in enumerate(dec.mdl_file_data.theSequenceDescs or []):
            original=sequence.theName
            if original.casefold() not in seen:
                seen.add(original.casefold());continue
            number=1
            candidate=original+'_loka_'+str(number)
            while candidate.casefold() in reserved:
                number+=1;candidate=original+'_loka_'+str(number)
            reserved.add(candidate.casefold());seen.add(candidate.casefold())
            sequence.theName=candidate
            repairs.append(dict(model=model,kind='sequence',index=index,attribute='theName',
                                original=original,repaired=candidate))

    paths={}
    for model,dec in models:
        original=dec.result.qc_file
        if not original:continue
        original=Path(original);root=original.parent
        changed=any(r['model']==model for r in repairs)
        if changed:
            output=SmdTextWriter()
            writer=_PreparedQcWriter(output.write_line,dec.mdl_file_data,dec.vtx_file_data,
                                     dec.phy_file_data,dec.result.model_name,
                                     registry=dec.registry,options=dec.options,
                                     corrective_bindings=[(mapping.get(name,name),path) for name,path in correctives[model]])
            if dec.result.model_role=='animation_library':writer.write_animation_library()
            else:writer.write_all()
            # SmdTextWriter uses the same CRLF serialization as raw decompile.
            text='\r\n'.join(output.lines)+'\r\n'
        else:text=original.read_text(encoding='utf-8-sig')
        text=_repair_resources(text,root,model,repairs)
        commands=parse_qc(text)
        expected=[s.theName for s in dec.mdl_file_data.theSequenceDescs or [] if s.theAnimDescIndexes]
        if [c.name for c in commands if c.kind=='sequence']!=expected:
            raise ValueError('QC 前置校验：sequence 声明与 MDL 索引不符: '+model)
        if any(c.kind in ('include','declaresequence','declareanimation') for c in commands):
            raise ValueError('QC 前置校验：含未展开的 include/declare: '+model)
        if any(r['model']==model for r in repairs):
            checked=original.with_suffix('.checked.qc')
            with checked.open('w',encoding='utf-8',newline='') as handle:handle.write(text)
            paths[model]=checked
        else:paths[model]=original
    return paths,repairs
