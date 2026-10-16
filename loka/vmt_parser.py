"""Lossless VMT syntax trees, byte tokens and structured diagnostics."""
from dataclasses import dataclass
import codecs
import re

from .material_text import encoding

_NUMBER=r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
_NUMERIC_VECTOR=re.compile(r'\s*'+_NUMBER+r'(?:\s+'+_NUMBER+r')+\s*')


class VmtSyntaxError(ValueError):
    def __init__(self,source,message,offset,*,code='invalid_syntax',context=(),expected=()):
        self.code=code;self.offset=offset;self.context=tuple(context);self.expected=tuple(expected)
        start=3 if source.startswith(b'\xef\xbb\xbf') else 0
        prefix=source[start:max(start,offset)].decode(encoding(source))
        lines=re.split(r'\r\n|\r|\n',prefix)
        self.line=len(lines);self.column=len(lines[-1])+1
        path='/'.join(self.context)
        super().__init__(message+(': '+path if path else '')+
                         f' (byte {offset}, line {self.line}, column {self.column})')


@dataclass(frozen=True)
class VmtToken:
    value:str
    start:int
    end:int
    quoted:bool=False
    closed:bool=True
    condition:bool=False
    kind:str='atom'


@dataclass(frozen=True)
class VmtLexed:
    source:bytes
    codec:str
    tokens:tuple[VmtToken,...]
    comments:tuple[VmtToken,...]
    eof_comment:bool=False

    def parse(self,*,on_event=None,max_depth=128):
        return _Parser(self,on_event,max_depth).parse()


@dataclass(frozen=True)
class VmtNode:
    key:VmtToken
    value:VmtToken|None=None
    children:tuple|None=None
    opening:VmtToken|None=None
    closing:VmtToken|None=None
    conditions:tuple[VmtToken,...]=()

    @property
    def is_block(self):return self.children is not None


@dataclass(frozen=True)
class VmtDocument:
    source:bytes
    codec:str
    tokens:tuple[VmtToken,...]
    comments:tuple[VmtToken,...]
    nodes:tuple[VmtNode,...]

    def pairs(self):
        def walk(nodes,context):
            for node in nodes:
                if node.is_block:yield from walk(node.children,(*context,node.key.value.lower()))
                else:yield node.key,node.value,context
        return walk(self.nodes,())


@dataclass(frozen=True)
class VmtParseEvent:
    kind:str
    index:int
    context:tuple[str,...]=()
    previous:tuple[VmtToken,VmtToken]|None=None
    value_index:int|None=None
    role:str=''
    roots:int=0


def lex_vmt(data,*,max_tokens=200000):
    if not isinstance(data,bytes):raise TypeError('VMT 输入必须为 bytes')
    codec=encoding(data);tokens=[];comments=[];i=3 if data.startswith(b'\xef\xbb\xbf') else 0
    ends={};decoder=codecs.getincrementaldecoder(codec)();start=i
    for offset in range(i,len(data)):
        if decoder.decode(data[offset:offset+1]):ends[start]=offset+1;start=offset+1
    decoder.decode(b'',final=True)
    eof_comment=False
    while i<len(data):
        if data[i] in b' \t\r\n':i+=1;continue
        start=i
        if len(tokens)+len(comments)>=max_tokens:
            raise VmtSyntaxError(data,'VMT Token 数量超过限制',i,code='token_limit')
        if data.startswith(b'//',i):
            endings=[p for p in (data.find(b'\r',i),data.find(b'\n',i)) if p>=0]
            end=min(endings) if endings else len(data)
            comments.append(VmtToken(data[i:end].decode(codec),i,end,kind='line_comment'))
            eof_comment=not endings;i=end
            continue
        if data.startswith(b'/*',i):
            end=data.find(b'*/',i+2)
            if end<0:raise VmtSyntaxError(data,'VMT 注释未闭合',i,code='unclosed_comment',expected=('*/',))
            end+=2;comments.append(VmtToken(data[i:end].decode(codec),i,end,kind='block_comment'));i=end
            continue
        if data[i]==34:
            i+=1
            while i<len(data):
                if data[i]==34:break
                if data[i]==92 and i+1<len(data) and data[i+1]==34:i+=2
                else:i=ends[i]
            closed=i<len(data)
            tokens.append(VmtToken(data[start+1:i].decode(codec),start,i+1 if closed else i,True,closed))
            if closed:i+=1
        elif data[i] in b'{}':
            tokens.append(VmtToken(chr(data[i]),i,i+1,kind='open' if data[i]==123 else 'close'));i+=1
        elif data[i]==91:
            while i<len(data) and data[i] not in b']\r\n{}':i=ends[i]
            closed=i<len(data) and data[i]==93
            if closed:i+=1
            content=data[start+1:i-1 if closed else i].decode(codec)
            vector=bool(_NUMERIC_VECTOR.fullmatch(content))
            tokens.append(VmtToken(data[start:i].decode(codec),start,i,False,closed,not vector,
                                   'vector' if vector else 'condition'))
        else:
            while i<len(data) and data[i] not in b' \t\r\n{}"':i=ends[i]
            tokens.append(VmtToken(data[start:i].decode(codec),start,i))
        if '\0' in tokens[-1].value:
            raise VmtSyntaxError(data,'VMT 包含 NUL',data.index(b'\0',start),code='nul')
    return VmtLexed(data,codec,tuple(tokens),tuple(comments),eof_comment)


class _Parser:
    def __init__(self,lexed,on_event,max_depth):
        self.lexed=lexed;self.tokens=lexed.tokens;self.on_event=on_event;self.max_depth=max_depth;self.roots=0

    def event(self,kind,index,context=(),previous=None,*,value_index=None,role=''):
        if self.on_event is not None:
            self.on_event(VmtParseEvent(kind,index,context,previous,value_index,role,self.roots))

    def fail(self,code,message,index,context=(),expected=()):
        offset=self.tokens[index].start if index<len(self.tokens) else len(self.lexed.source)
        raise VmtSyntaxError(self.lexed.source,message,offset,code=code,context=context,expected=expected)

    def atom(self,index,role,context):
        self.event('atom',index,context,role=role)
        token=self.tokens[index]
        diagnostic_context=context[:-1] if role=='value' else context
        if not token.closed:
            if token.kind in ('vector','condition'):
                self.fail('unclosed_'+token.kind,'VMT 向量或条件括号未闭合',index,diagnostic_context,(']',))
            self.fail('unclosed_string','VMT 字符串未闭合',index,diagnostic_context,('"',))
        if token.kind=='vector' and role=='key':
            self.fail('vector_without_entry','VMT 数值向量没有关联键',index,context,('key',))
        if token.kind in ('open','close'):
            code='missing_value' if role=='value' else 'unexpected_'+token.kind
            message=f'VMT 键 {context[-1]!r} 缺少值' if role=='value' else 'VMT 键位置出现花括号'
            self.fail(code,message,index,diagnostic_context,('value','{') if role=='value' else ('key',))
        if token.condition:self.fail('condition_without_entry','VMT 条件没有关联键值',index,context,('key',))
        if not token.value and role=='key':self.fail('empty_key','VMT 键名为空',index,context,('key',))
        return token

    def conditions(self,index,context,previous=None):
        result=[]
        while index<len(self.tokens) and self.tokens[index].condition:
            self.event('condition',index,context,previous)
            token=self.tokens[index]
            if not token.closed:self.fail('unclosed_condition','VMT 条件括号未闭合',index,context,(']',))
            result.append(token);index+=1
        return index,result

    def entry(self,index,context,depth,previous=None,*,root=False):
        self.event('root' if root else 'before_entry',index,context,previous)
        key=self.atom(index,'key',context)
        value_index,conditions=self.conditions(index+1,context,previous)
        if value_index>=len(self.tokens):self.fail('missing_value',f'VMT 键 {key.value!r} 缺少值',value_index,context,('value','{'))
        self.event('entry',index,context,previous,value_index=value_index)
        value=self.tokens[value_index]
        directive=root and key.value.lower() in ('#include','#base')
        if root and not directive and key.value.startswith(('$','#','[')):
            self.fail('invalid_root','VMT 根着色器/块有歧义',index,context,('shader',))
        if value.kind=='open':
            if directive:self.fail('directive_path','VMT include/base 缺少路径',value_index,context,('value',))
            children,closing,next_index=self.block(value_index+1,(*context,key.value),depth+1)
            node=VmtNode(key,children=children,opening=value,closing=closing)
            if root:self.roots+=1
        else:
            if root and not directive:self.fail('expected_open','VMT 根块缺少左花括号',value_index,context,('{',))
            value=self.atom(value_index,'value',(*context,key.value))
            node=VmtNode(key,value=value);next_index=value_index+1
        next_index,suffix=self.conditions(next_index,context,(key,value))
        node=VmtNode(node.key,node.value,node.children,node.opening,node.closing,tuple(conditions+suffix))
        return node,next_index,(key,value)

    def block(self,index,context,depth):
        if depth>self.max_depth:self.fail('nesting_limit','VMT 嵌套过深',index,context)
        nodes=[];previous=None
        while index<len(self.tokens):
            if self.tokens[index].kind=='close':return tuple(nodes),self.tokens[index],index+1
            node,index,previous=self.entry(index,context,depth,previous)
            nodes.append(node)
        self.event('missing_close',index,context,previous)
        self.fail('missing_close','VMT 花括号未闭合',index,context,('}',))

    def parse(self):
        nodes=[];index=0
        while index<len(self.tokens):
            node,index,_=self.entry(index,(),0,root=True);nodes.append(node)
        if not nodes:self.fail('no_roots','VMT 没有着色器块或 include/base 指令',0,expected=('shader','#include','#base'))
        return VmtDocument(self.lexed.source,self.lexed.codec,self.tokens,self.lexed.comments,tuple(nodes))


def parse_vmt(data,*,on_event=None,max_depth=128,max_tokens=200000):
    return lex_vmt(data,max_tokens=max_tokens).parse(on_event=on_event,max_depth=max_depth)
