"""Cached page background for the OpenGL viewport's own opaque composition."""
import weakref

from PySide6.QtCore import QObject,QEvent,QPoint,Qt
from PySide6.QtGui import QImage,QPainter,QRegion
from PySide6.QtWidgets import QWidget


class PreviewBackdrop(QObject):
    def __init__(self,window,artwork):
        super().__init__(window)
        self.window=window
        self.artwork=artwork
        self._cache=weakref.WeakKeyDictionary()
        self._views=weakref.WeakSet()
        self._watched=weakref.WeakSet()
        self._rendering=False
        self._watch(window);self._watch(artwork)
        effect=artwork.graphicsEffect()
        if effect is not None and hasattr(effect,'opacityChanged'):
            effect.opacityChanged.connect(self.invalidate)

    def _watch(self,widget):
        if widget not in self._watched:
            widget.installEventFilter(self);self._watched.add(widget)
            animation=getattr(widget,'backgroundColorAni',None)
            if animation is not None:animation.valueChanged.connect(self.invalidate)

    def attach(self,viewport):
        self._views.add(viewport)
        self._watch(viewport)
        viewport.set_background_provider(self)

    def invalidate(self,*_):
        self._cache.clear()
        for view in tuple(self._views):
            try:view.update()
            except RuntimeError:self._views.discard(view)

    def eventFilter(self,widget,event):
        kinds=(QEvent.Resize,QEvent.Show,QEvent.Hide,QEvent.PaletteChange,
               QEvent.ApplicationPaletteChange,QEvent.StyleChange,QEvent.DevicePixelRatioChange)
        if event.type() in kinds or event.type()==QEvent.Move and widget is not self.window:
            self.invalidate()
        return False

    def image_for(self,viewport):
        if self._rendering:return QImage()
        chain=[];widget=viewport.parentWidget()
        while widget is not None:
            chain.append(widget);self._watch(widget)
            if widget is self.window:break
            widget=widget.parentWidget()
        if not chain or chain[-1] is not self.window:return QImage()
        origin=viewport.mapTo(self.window,QPoint())
        ratio=viewport.devicePixelRatioF()
        key=(origin.x(),origin.y(),viewport.width(),viewport.height(),ratio,
             tuple((w.palette().cacheKey(),w.styleSheet()) for w in chain))
        entry=self._cache.get(viewport)
        if entry is not None and entry[0]==key:return entry[1]
        # QSize/QPoint scaling uses Qt's rounding, including x.5 DPI results.
        # Round widget origins separately, matching native child rasterization.
        physical=viewport.size()*ratio
        device_origin=origin*ratio
        width=max(1,physical.width());height=max(1,physical.height())
        image=QImage(width,height,QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(self.window.palette().window().color())
        self._rendering=True
        painter=QPainter(image)
        try:
            # Paint each ancestor's own surface; never render GL children,
            # buttons, popups or other foreground content into the backdrop.
            for ancestor in reversed(chain):
                offset=ancestor.mapTo(self.window,QPoint())*ratio-device_origin
                painter.save();painter.translate(offset.x()/ratio,offset.y()/ratio)
                ancestor.render(painter,QPoint(),QRegion(),QWidget.RenderFlag(0))
                painter.restore()
                if ancestor is self.window and not self.artwork.isHidden():
                    pixmap=self.artwork.pixmap()
                    if pixmap is not None and not pixmap.isNull():
                        painter.save()
                        effect=self.artwork.graphicsEffect()
                        painter.setOpacity(effect.opacity() if effect is not None else 1.)
                        offset=self.artwork.mapTo(self.window,QPoint())*ratio-device_origin
                        painter.translate(offset.x()/ratio,offset.y()/ratio)
                        painter.drawPixmap(QPoint(),pixmap)
                        painter.restore()
        finally:
            painter.end();self._rendering=False
        self._cache[viewport]=(key,image)
        return image
