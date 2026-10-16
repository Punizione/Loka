"""Optional symbol-only KeyValues normalization; never used by legacy parsing.

Only structurally unique repairs are admitted. Values, keys, shader names and
opaque path/comment bytes are not synthesized, removed or re-encoded.
"""
import re
from .material_text import encoding,resource_key,TEXTURES,MATERIALS,NON_TEXTURES
from .vmt_parser import lex_vmt,VmtSyntaxError


_VECTORS=frozenset(('$color','$color2','$envmaptint','$selfillumtint','$phongtint',
                   '$refracttint','$reflecttint','$detailtint','$phongfresnelranges'))
_PLATFORMS=frozenset(('$WIN32','$X360','$PS3','$PC','$POSIX','$LINUX','$OSX','$LOWVIOLENCE','$HDR','$LDR'))
_NUMBER=r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
_THREE=re.compile(r'\s*'+_NUMBER+r'\s+'+_NUMBER+r'\s+'+_NUMBER+r'\s*')





class _Repair(Exception):
    def __init__(self,offset,old,new,kind,context):
        self.record={'offset':offset,'old':old,'new':new,'kind':kind,'context':context,
                     'coordinate_space':'bytes_before_this_step'}
        self.records=[self.record]

    @classmethod
    def from_records(cls,records):
        first=records[0]
        correction=cls(first['offset'],first['old'],first['new'],first['kind'],first['context'])
        correction.records=list(records)
        correction.record=correction.records[0]
        return correction


def _apply_records(data,records):
    for record in records:
        offset=record['offset'];old=record['old'].encode('ascii');new=record['new'].encode('ascii')
        if offset<0 or offset>len(data) or data[offset:offset+len(old)]!=old:
            raise ValueError('VMT 修复坐标不一致')
        data=data[:offset]+new+data[offset+len(old):]
    return data


def _proxy_wrapper_end(tokens,index):
    """Recognize a balanced anonymous layer containing only named proxy blocks."""
    cursor=index+1;children=0
    while cursor<len(tokens):
        key=tokens[cursor]
        if key.value=='}' and not key.quoted:return cursor if children else None
        if (not key.closed or not key.value or key.condition or key.value.startswith(('$','#'))
                or (key.value in ('{','}') and not key.quoted) or cursor+1>=len(tokens)):
            return None
        opening=tokens[cursor+1]
        if opening.value!='{' or opening.quoted:return None
        cursor+=2;depth=1
        while cursor<len(tokens) and depth:
            token=tokens[cursor]
            if not token.closed:return None
            if not token.quoted:
                if token.value=='{':depth+=1
                elif token.value=='}':depth-=1
            cursor+=1
        if depth:return None
        children+=1
    return None


def _failure(data,message,offset):
    return VmtSyntaxError(data,message,offset,code='normalization_error')


def _lex(data,*,with_eof_comment=False):
    lexed=lex_vmt(data)
    return (lexed.tokens,lexed.eof_comment) if with_eof_comment else lexed.tokens


def _composed_symbols(data,allowed,available_resources=frozenset()):
    """Prove a complete result without admitting another quote candidate."""
    check=data;records=[];allowed=set(allowed)|{'unwrap_proxy_block'}
    for _ in range(4096):
        try:
            _validate(check,False,quote_candidates=False,available_resources=available_resources)
            return records
        except ValueError:pass
        try:_validate(check,True,quote_candidates=False,available_resources=available_resources)
        except _Repair as correction:
            if any(r['kind'] not in allowed for r in correction.records):return None
            check=_apply_records(check,correction.records);records.extend(correction.records)
        except ValueError:return None
        else:return None
    return None


def _premature_shader_close(data):
    """Remove one proven early root close without changing an existing subtree."""
    try:lexed=lex_vmt(data)
    except ValueError:return None
    depth=0;closing=None
    for token in lexed.tokens:
        if token.kind=='open':depth+=1
        elif token.kind=='close':
            depth-=1
            if depth==0:closing=token;break
            if depth<0:return None
    if closing is None:return None
    candidate=data[:closing.start]+data[closing.end:]
    try:
        prefix=_validate(data[:closing.end],False)
        document=_validate(candidate,False)
    except ValueError:return None
    if len(prefix.nodes)!=1 or len(document.nodes)!=1:return None
    original=prefix.nodes[0];root=document.nodes[0]
    if (not original.is_block or root.key!=original.key or root.conditions!=original.conditions
            or root.children[:len(original.children)]!=original.children):return None
    # A later named block might be another shader, and directives/Patch have
    # inheritance semantics. Only trailing scalar shader parameters qualify.
    added=root.children[len(original.children):]
    if not added or any(n.is_block or not n.key.value.startswith('$') for n in added):return None
    pending=[root]
    while pending:
        node=pending.pop()
        if node.key.value.lower() in ('patch','include','#include','#base'):return None
        if node.is_block:pending.extend(node.children)
    # Splicing a delimiter must not merge tokens or turn payload into comments.
    # Compare the original byte spans after translating past the one deletion.
    def unchanged(before,after):
        before=[t for t in before if t is not closing]
        if len(before)!=len(after):return False
        for old,new in zip(before,after):
            shift=1 if old.start>closing.start else 0
            if (old.value,old.kind,old.quoted,old.closed,old.condition,old.start-shift,old.end-shift)!=(
                    new.value,new.kind,new.quoted,new.closed,new.condition,new.start,new.end):return False
        return True
    if not unchanged(lexed.tokens,document.tokens) or not unchanged(lexed.comments,document.comments):return None
    # Earlier closes belong to the preserved subtrees; later deletions leave
    # the first root closed before these $parameters. This is the only valid
    # deletion that can preserve the established tree and admit that suffix.
    return _Repair(closing.start,'}','','remove_premature_shader_close',root.key.value).records


def _payload_symbol_repair(data):
    """Admit one complete, token-preserving interpretation of malformed text.

    These are delimiter insertions, not shader-name or parameter-name guesses.
    Legal input has already won before this is called. Every candidate must
    parse without further recovery and retain every other original token.
    """
    try:lexed=lex_vmt(data)
    except ValueError:return None
    tokens=lexed.tokens;candidates=[]
    identifier=re.compile(r'[A-Za-z_][A-Za-z_0-9]*')
    merged=re.compile(r'(\$[A-Za-z_][A-Za-z_0-9]*)([ \t]+)('+_NUMBER+r')')

    def record(offset,new,kind,context):
        return _Repair(offset,'',new,kind,'/'.join(context)).record

    def signature(token):
        return token.value,token.kind,token.quoted,token.closed,token.condition

    def candidate(records,index,replacement,*,proxy_start=None):
        after=_apply_records(data,records)
        try:document=_validate(after,False)
        except ValueError:return
        expected=[signature(t) for t in tokens]
        expected[index:index+1]=replacement
        if [signature(t) for t in document.tokens]!=expected:return
        if proxy_start is not None:
            # Proxies contain named blocks. The inserted string cannot be
            # interpreted as a second proxy identifier, and its removal must
            # restore that exact original body's opening and all its fields.
            pending=list(document.nodes);found=False
            while pending:
                node=pending.pop()
                if node.is_block:
                    if (node.key.value.lower()=='proxies'
                            and any(n.key.start==proxy_start for n in node.children)):
                        found=all(n.is_block for n in node.children)
                        break
                    pending.extend(node.children)
            if not found:return
        candidates.append(records)

    def observe(event):
        if event.kind!='entry' or not event.context:return
        index=event.index;key=tokens[index];value_index=event.value_index
        value=tokens[value_index];context=(*event.context,key.value)
        if key.closed and key.quoted:
            match=merged.fullmatch(key.value)
            if match and value.kind=='close':
                # Split only a dangling "$parameter number" token. Inserting
                # two quotes preserves the original spelling and separator.
                left=key.start+1+len(match[1]);right=left+len(match[2])
                records=[record(right,'"','split_parameter_quote',context),
                         record(left,'"','split_parameter_quote',context)]
                candidate(records,index,[(match[1],'atom',True,True,False),
                                         (match[3],'atom',True,True,False)])
        if event.previous is not None:
            previous_key,previous_value=event.previous
            # Only punctuation immediately after a completed quoted material
            # value, through end of its line, may become a comment. A following
            # parameter or value on that line prevents this candidate entirely.
            if (previous_key.value.startswith('$') and previous_value.quoted
                    and previous_value.end==key.start and not key.quoted
                    and key.closed and re.fullmatch(r'[、，。；：！？]+',key.value)):
                end=min([p for p in (data.find(b'\r',key.end),data.find(b'\n',key.end)) if p>=0]
                        or [len(data)])
                if not data[key.end:end].strip(b' \t'):
                    candidate([record(key.start,'//','comment_punctuation_suffix',event.context)],index,[])
        if (len(event.context)>=2 and event.context[-1].lower()=='proxies'
                and identifier.fullmatch(key.value) and value.closed and value.quoted
                and re.fullmatch(r'[A-Za-z_0-9]+(?:[ \t]+[A-Za-z_0-9]+)+',value.value)
                and value_index+1<len(tokens) and tokens[value_index+1].kind=='open'):
            # The only intervening token contains several words, so it cannot
            # compete with the established single identifier as a proxy name.
            # Keep that damaged text verbatim inside an auditable comment.
            records=[record(value.end,'*/','comment_intrusive_proxy_text',context),
                     record(value.start,'/*','comment_intrusive_proxy_text',context)]
            candidate(records,value_index,[],proxy_start=key.start)

    try:lexed.parse(on_event=observe)
    except ValueError:pass
    return candidates[0] if len(candidates)==1 else None


def _validate(data,repair,*,quote_candidates=True,available_resources=frozenset()):
    lexed=lex_vmt(data);values=lexed.tokens;eof_comment=lexed.eof_comment
    def change(offset,old,new,kind,context,*,witness_resource=None):
        if not repair:raise _failure(data,'VMT 缺失/不匹配的语法符号: '+context,offset)
        correction=_Repair(offset,old,new,kind,context)
        if witness_resource is not None:correction.record['witness_resource']=witness_resource
        raise correction

    def closing_suffix(position):
        try:return all(t.value=='}' and not t.quoted for t in _lex(data[position:]))
        except ValueError:return False

    def atom(token,role,context):
        if not token.closed:
            raw=data[token.start+1:]
            line_length=min([p for p in (raw.find(b'\r'),raw.find(b'\n')) if p>=0] or [len(raw)])
            candidate=raw[:line_length]
            if (role=='value' and candidate.strip() and b'//' not in candidate and b'/*' not in candidate
                    and not any(c in candidate.decode(encoding(data)) for c in '{}"')
                    and closing_suffix(token.start+1+line_length)):
                change(token.start+1+line_length,'','"','close_quote',context)
            raise _failure(data,'VMT 字符串无法唯一恢复: '+context,token.start)
        if (not token.value and not (role=='value' and token.quoted)) or (not token.quoted and token.value in ('{','}')):
            raise _failure(data,'VMT 键/值缺失: '+context,token.start)
        return token

    def vector(key,token):
        if key.value.lower() not in _VECTORS or not token.quoted:return
        raw=data[token.start+1:token.end-1];stripped=raw.strip();start=len(raw)-len(raw.lstrip())
        if not stripped:return
        left=chr(stripped[0]);right=chr(stripped[-1]);pairs={'[':']','{':'}'}
        if left in pairs:
            if right==pairs[left]:return
            inner=stripped[1:-1] if right in ']}' else stripped[1:]
            if not _THREE.fullmatch(inner.decode('ascii',errors='replace')):
                raise _failure(data,'VMT 数值向量不完整/有歧义: '+key.value,token.start)
            if right in ']}':change(token.start+1+start+len(stripped)-1,right,pairs[left],'vector_close',key.value)
            change(token.start+1+start+len(stripped),'',pairs[left],'vector_close',key.value)
        elif right in ']}' and _THREE.fullmatch(stripped[:-1].decode('ascii',errors='replace')):
            change(token.start+1+start,'','[' if right==']' else '{','vector_open',key.value)

    def condition(index):
        token=values[index]
        if token.condition:
            if token.closed:return index+1
            expression=token.value[1:].strip().upper()
            if expression.lstrip('!') not in _PLATFORMS:raise _failure(data,'VMT 条件括号无法唯一恢复: '+token.value,token.start)
            change(token.end,'',']','condition_close',token.value)
        if not token.quoted and token.value.endswith(']') and token.value[:-1].upper().lstrip('!') in _PLATFORMS:
            change(token.start,'','[','condition_open',token.value)
        return index

    def observe(event):
        index=event.index;context=event.context;previous=event.previous
        if event.kind=='atom':
            if repair and event.role=='value' and values[index].quoted and not values[index].closed:
                atom(values[index],event.role,'/'.join(context))
        elif event.kind=='condition':
            condition(index)
        elif event.kind=='root':
            token=values[index]
            if event.roots:condition(index)
            if event.roots and token.value=='}' and not token.quoted and all(t.value=='}' and not t.quoted for t in values[index:]):
                change(token.start,'}','','remove_extra_close_block','root')
        elif event.kind=='missing_close':
            ending=re.search(rb'\r\n|\r|\n',data)
            boundary=(ending.group().decode('ascii') if ending else '\n') if eof_comment else ''
            change(len(data),'',boundary+'}','close_block_after_comment' if eof_comment else 'close_block','/'.join(context))
        elif event.kind=='before_entry':
            token=values[index]
            if repair and context[-1].lower()=='proxies' and token.value=='{' and not token.quoted:
                closing=_proxy_wrapper_end(values,index)
                if closing is not None:
                    # Both braces belong to one proven wrapper. Apply them as
                    # a unit so a partial edit cannot reparent later proxies.
                    records=[_Repair(at,old,'','unwrap_proxy_block','/'.join(context)).record
                             for at,old in ((values[closing].start,'}'),(token.start,'{'))]
                    raise _Repair.from_records(records)
            if previous is not None:
                updated=condition(index)
                if updated!=index:return
                key,last=previous
                if repair and key.value.lower() in TEXTURES:
                    between=data[last.end:token.start]
                    inline=bool(between) and not between.strip(b' \t')
                    endings=[p for p in (data.find(b'\r',token.start),data.find(b'\n',token.start)) if p>=0]
                    end=min(endings) if endings else len(data)
                    safe_composition={'remove_extra_close_block','close_block','close_block_after_comment','join_texture_path'}
                    # Join only an unquoted path split just after a separator,
                    # with a real package texture proving the resulting name.
                    if (inline and not last.quoted and not token.quoted and last.value.endswith(('\\','/'))
                            and re.fullmatch(r'[\w.-]+(?:[/\\][\w.-]+)*',token.value)):
                        witness=resource_key(last.value+token.value,'.vtf')
                        following=values[index+1] if index+1<len(values) else None
                        next_key=following is not None and (following.value.startswith('$') or
                            (following.value=='}' and not following.quoted))
                        if witness in available_resources and next_key:
                            change(last.end,between.decode('ascii'),'','join_texture_path',
                                   '/'.join((*context,key.value)),witness_resource=witness)
                    # A specific same-line explanatory note can lack its //.
                    # Preserve the note bytes; never consume an arbitrary key,
                    # a quoted value, or a subsequent line as commentary.
                    note=re.fullmatch(r'(?:这是|此为|这里是)(?:原|原始的?|原来的)?(\$[A-Za-z_][A-Za-z_0-9]*)',token.value)
                    if (quote_candidates and inline and not token.quoted and note and note[1].lower() in TEXTURES
                            and len(_lex(data[token.start:end]))==1):
                        structural=_composed_symbols(data,{'remove_extra_close_block','close_block','close_block_after_comment'})
                        if structural is not None:
                            if structural:
                                raise _Repair.from_records(structural)
                        else:
                            candidate=data[:token.start]+b'//'+data[token.start:]
                            if _composed_symbols(candidate,safe_composition,available_resources) is not None:
                                change(token.start,'','//','comment_marker_insert','/'.join((*context,key.value)))
                if token.quoted and not token.closed and not last.quoted and last.end==token.start:
                    line=data[token.start+1:].splitlines()[0] if data[token.start+1:] else b''
                    if not line.strip() and closing_suffix(token.start+1+len(line)):
                        change(last.start,'','"','open_quote',key.value)
            if repair and previous is not None and token.value.startswith('\\\\') and not token.quoted:
                prior_key,last=previous
                between=data[last.end:token.start]
                endings=[p for p in (data.find(b'\r',token.start),data.find(b'\n',token.start)) if p>=0]
                end=min(endings) if endings else len(data)
                explanation=data[token.start+2:end].decode(encoding(data)).strip()
                suffix=_lex(data[token.start:end]);following=_lex(data[end:])
                cjk=lambda c:'\u3400'<=c<='\u9fff'
                punctuation='，。；：！？、（）'
                explanatory=(bool(explanation) and cjk(explanation[0]) and any(cjk(c) for c in explanation) and any(c in punctuation for c in explanation)
                             and all(cjk(c) or c in punctuation or c in '0123456789 +-%,;:!?()' for c in explanation))
                inline=(last.quoted or last.value!='{') and not between.strip() and b'\r' not in between and b'\n' not in between
                if inline and explanatory and len(suffix)==1 and following and following[0].value=='}' and not following[0].quoted:
                    candidate=data[:token.start]+b'//'+data[token.start+2:]
                    # Candidate must parse completely. Only independently safe
                    # terminal extra closes may coexist with this marker typo.
                    check=candidate;valid=False
                    for _ in range(len(following)+1):
                        try:_validate(check,False);valid=True;break
                        except ValueError:pass
                        try:_validate(check,True,quote_candidates=quote_candidates)
                        except _Repair as correction:
                            if any(r['kind'] not in ('remove_extra_close_block','unwrap_proxy_block') for r in correction.records):break
                            check=_apply_records(check,correction.records)
                        except ValueError:break
                    if valid:change(token.start,'\\\\','//','comment_marker','/'.join(context))

        elif event.kind=='entry':
            key=values[index];value_index=event.value_index;value=values[value_index]
            if not context:
                if key.value.lower() not in ('#include','#base') and (value.value!='{' or value.quoted):
                    condition(value_index)
                    if value.value.startswith('$') or value.value.lower() in ('proxies','include','insert','replace','}'):
                        change(values[value_index-1].end,'','{','open_block',key.value)
                return
            if repair and quote_candidates and value.quoted and value.closed and not value.value:
                # Decode from an established token boundary: encoded trail
                # bytes are not ASCII separators. Repairs still use raw spans.
                tail=data[value.end:].decode(encoding(data))
                witness=re.match(r'([^\s"{}]+)"(?=[ \t]*(?:\r|\n|}|$))',tail)
                if witness and ('/' in witness[1] or '\\' in witness[1]) and not witness[1].startswith(('$','#','[')):
                    blocks={'remove_extra_close_block','close_block','close_block_after_comment'}
                    structural=_composed_symbols(data,blocks)
                    if structural is not None:
                        # Existing legal content wins over a quote heuristic:
                        # apply only its actual missing/extra block correction.
                        if structural:
                            raise _Repair.from_records(structural)
                    else:
                        at=value.end-1;candidate=data[:at]+data[at+1:]
                        if _composed_symbols(candidate,blocks|{'comment_marker'}) is not None:
                            change(at,'"','','duplicate_value_quote','/'.join((*context,key.value)))
            known_block=(key.value.lower()=='proxies' or (context[0].lower()=='patch' and key.value.lower() in ('insert','replace')))
            if known_block and (value.quoted or value.value!='{'):
                condition(value_index)
                if value.value=='}' or (value_index+1<len(values) and values[value_index+1].value=='{'):
                    change(values[value_index-1].end,'','{','open_block','/'.join((*context,key.value)))
                raise _failure(data,'VMT 已知块缺少唯一可恢复的内容: '+key.value,value.start)

            if value.closed and value.kind=='atom':vector(key,value)

    return lexed.parse(on_event=observe)



def normalize_vmt(data:bytes,*,available_resources=())->tuple[bytes,list[dict]]:
    """Return validated bytes and replayable ASCII repair records, or fail."""
    if not isinstance(data,bytes):raise TypeError('VMT 输入必须为 bytes')
    available_resources=frozenset(str(path).replace('\\','/').lower() for path in available_resources)
    repairs=[];after=data
    # Legal input wins. Once recovery starts, finish its deterministic repair
    # pass before accepting syntax: leftover split paths can accidentally pair
    # into bogus keys/values after only one repair.
    try:
        _validate(after,False,available_resources=available_resources)
        return after,repairs
    except ValueError:pass
    premature=_premature_shader_close(after)
    if premature is not None:
        return _apply_records(after,premature),premature
    # Prefer an already meaningful key/value interpretation that needs only
    # closing-block correction. No resource witness participates in this proof.
    structural=_composed_symbols(after,{'remove_extra_close_block','close_block','close_block_after_comment'})
    if structural is not None:
        after=_apply_records(after,structural)
        _validate(after,False)
        return after,structural
    payload=_payload_symbol_repair(after)
    if payload is not None:
        return _apply_records(after,payload),payload
    for _ in range(4096):
        try:
            _validate(after,True,available_resources=available_resources)
        except _Repair as correction:
            after=_apply_records(after,correction.records);repairs.extend(correction.records)
        else:
            _validate(after,False,available_resources=available_resources)
            return after,repairs
    raise ValueError('VMT 修复次数超过安全范围')


def normalized_material_pairs(data):
    """Read typed references from the same syntax tree used for validation."""
    document=_validate(data,False)
    return list(document.pairs()),document.codec


def patch_normalized_material(data,mapping):
    """Use existing typed-reference rules on the opt-in byte-correct tokens."""
    pairs,codec=normalized_material_pairs(data);changes=[];decisions=[]
    for key,value,context in pairs:
        name=key.value.lower()
        if 'proxies' in context:continue
        kind='.vtf' if name in TEXTURES else '.vmt' if name in MATERIALS else None
        if kind is None:
            candidate=resource_key(value.value,'.vtf')
            if (name.startswith('$') and name not in NON_TEXTURES and candidate in mapping
                    and not re.fullmatch(r'[-+]?\d+(?:\.\d+)?',value.value)):
                raise ValueError('未知 VMT 参数引用了待移动纹理，无法安全改写: '+key.value)
            continue
        old=resource_key(value.value,kind)
        if old not in mapping:continue
        target=mapping[old].removeprefix('materials/')
        if not value.value.lower().endswith(kind):target=target[:-len(kind)]
        new=('"'+target+'"') if value.quoted or any(c.isspace() for c in target) else target
        changes.append((value.start,value.end,new.encode(codec)))
        decisions.append({'key':key.value,'source':value.value,'target':target})
    for start,end,new in sorted(changes,reverse=True):data=data[:start]+new+data[end:]
    _validate(data,False)
    return data,decisions
