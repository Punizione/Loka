"""Startup-scoped UI language; business data and saved diagnostics stay original."""
from functools import lru_cache
import re
from string import Formatter


LANGUAGES=('zh_CN','en_US')
_FORMATTER=Formatter()
_HAN=re.compile(r'[\u3400-\u9fff]')


@lru_cache(maxsize=1)
def english_catalog():
    # Static imports keep all catalogs discoverable by the existing freezer.
    from .locales.en_main import MESSAGES as main
    from .locales.en_actions import MESSAGES as actions
    from .locales.en_messages import MESSAGES as backend
    messages={}
    for catalog in (main,actions,backend):
        for source,translated in catalog.items():
            # Repeated shared terms belong to the first presentation catalog.
            messages.setdefault(source,translated)
    return messages


def _localized_fields(source):
    from .locales.en_messages import LOCALIZED_FIELDS
    return LOCALIZED_FIELDS.get(source,())


def _opaque(value):
    """Never translate source paths or bare artifact filenames as labels."""
    if '\n' in value or '\r' in value:return False
    if re.match(r'^(?:[A-Za-z]:[/\\]|[/\\])',value):return True
    if re.fullmatch(r'[^\s<>:"|?*：；]+[/\\][^\s<>:"|?*：；]+',value):return True
    return bool(re.fullmatch(r'[^<>:"|?*：；\\/\r\n]+\.[A-Za-z0-9]{1,8}',value))


class Translator:
    def __init__(self,language='zh_CN',messages=None):
        self.language=language if language in LANGUAGES else 'zh_CN'
        self._messages=messages
        self._patterns=None
        self._prefixes=None
        self._stages=None
        self._rendered=lru_cache(maxsize=2048)(self._display_text)

    @property
    def messages(self):
        if self._messages is None:self._messages=english_catalog()
        return self._messages

    def tr(self,source,/,*args,**kwargs):
        source=str(source)
        text=self.messages.get(source,source) if self.language=='en_US' else source
        return text.format(*args,**kwargs) if args or kwargs else text

    def _prepare_patterns(self):
        patterns=[];prefixes=[];stages={}
        for source,translated in self.messages.items():
            try:parts=list(_FORMATTER.parse(source))
            except ValueError:continue
            fields=[field for _,field,_,_ in parts if field is not None]
            if fields and _HAN.search(source):
                expression='';render_fields=[]
                for index,(literal,field,spec,_) in enumerate(parts):
                    expression+=re.escape(literal)
                    if field is None:continue
                    following=parts[index+1][0] if index+1<len(parts) else ''
                    integer=spec.endswith('d') or re.match(r'^\s*(?:个|项|帧|处|字节|次|阶段)',following)
                    number=spec.endswith(('f','g','e')) or re.match(r'^\s*秒',following)
                    expression+=r'([+-]?\d+)' if integer else r'([+-]?\d+(?:\.\d+)?)' if number else '(.*?)'
                    # Captures are opaque data unless their producer explicitly
                    # identifies a nested diagnostic or a display-only label.
                    # A translated word can also be a valid QC/resource name.
                    localize=(field in {'error','warning','message','summary','note','kind','display_name'}
                              or field in _localized_fields(source)
                              or bool(re.search(r'(?:来源武器|目标武器)[:：]\s*$',literal)))
                    if literal.endswith(('"',"'",'“')) and following.startswith(('"',"'",'”')):localize=False
                    render_fields.append((field,localize))
                specificity=sum(len(literal) for literal,_,_,_ in parts)
                sentence=parts[-1][0].endswith(('。','！','？'))
                patterns.append((specificity,re.compile(expression,re.S if '\n' in source else 0),render_fields,translated,sentence))
            elif source and len(source.strip())>=2 and _HAN.search(source):
                prefixes.append((source,translated))
            stage=re.fullmatch(r'(\d+/\d+)\s+(.+)',source)
            target=re.fullmatch(r'(\d+/\d+)\s+(.+)',translated)
            if stage and target:stages[stage[2]]=target[2]
        self._patterns=sorted(patterns,key=lambda p:p[0],reverse=True)
        self._prefixes=sorted(prefixes,key=lambda p:len(p[0]),reverse=True)
        self._stages=stages

    def display_text(self,text,_depth=0):
        return self._rendered(str(text),_depth)

    def _display_text(self,text,_depth):
        """Render original backend messages without rewriting their payloads.

        Exact messages and anchored templates are translated. Captured paths,
        identifiers and unknown text retain their original bytes/characters.
        Prefix matching supports the existing nested ``message + str(error)``
        diagnostics without a global substring replacement over user paths.
        """
        text=str(text)
        if self.language!='en_US' or not _HAN.search(text) or _depth>8:return text
        if _opaque(text):return text
        if text in self.messages:return self.messages[text]
        if self._patterns is None:self._prepare_patterns()
        for _,pattern,fields,translated,sentence in self._patterns:
            match=pattern.match(text) if sentence else pattern.fullmatch(text)
            if match is None:continue
            values={name:self.display_text(value,_depth+1) if localize else value
                    for (name,localize),value in zip(fields,match.groups())}
            try:
                # Captures have already been formatted by their producer.
                rendered=''.join(literal+(values[field] if field is not None else '')
                                 for literal,field,_,_ in _FORMATTER.parse(translated))
                return rendered+self.display_text(text[match.end():],_depth+1)
            except (KeyError,ValueError):continue
        whitespace=re.fullmatch(r'([ \t]*)(.*?)([ \t]*)',text,re.S)
        if whitespace and (whitespace[1] or whitespace[3]):
            return whitespace[1]+self.display_text(whitespace[2],_depth+1)+whitespace[3]
        for source,translated in self._prefixes:
            if text.startswith(source):
                return translated+self.display_text(text[len(source):],_depth+1)
        stage=re.fullmatch(r'(\d+/\d+)(\s+)(.+)',text,re.S)
        if stage and stage[3] in self._stages:
            return stage[1]+stage[2]+self._stages[stage[3]]
        if '\n' in text:
            return '\n'.join(self.display_text(line,_depth+1) for line in text.split('\n'))
        return text


_translator=Translator()


def set_language(language):
    """Select a launch's effective language, independent of persisted settings."""
    global _translator
    _translator=Translator(language)
    return _translator.language


def get_language():
    return _translator.language


def tr(source,/,*args,**kwargs):
    return _translator.tr(source,*args,**kwargs)


def display_text(text):
    return _translator.display_text(text)


def install_qt_translations(app,language=None):
    """Translate toolkit-owned controls before widgets are constructed."""
    if app is None:return
    from PySide6.QtCore import QLibraryInfo,QLocale,QTranslator
    from qfluentwidgets import FluentTranslator
    for translator in getattr(app,'_loka_translators',()):app.removeTranslator(translator)
    locale=QLocale(language if language in LANGUAGES else get_language())
    QLocale.setDefault(locale)
    qt=QTranslator(app)
    qt.load(locale,'qtbase','_',QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    fluent=FluentTranslator(locale,app)
    app._loka_translators=(qt,fluent)
    app.installTranslator(qt);app.installTranslator(fluent)
