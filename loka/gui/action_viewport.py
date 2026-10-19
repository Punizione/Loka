"""Hardware Qt OpenGL view of the compiled model, without external GL bindings."""
import math
import struct
from shiboken6 import VoidPtr

from PySide6.QtCore import Signal, QTimer, Qt
from PySide6.QtGui import QImage, QMatrix4x4, QSurfaceFormat, QVector3D
from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from ..i18n import tr, display_text


def preview_surface_format():
    fmt=QSurfaceFormat()
    fmt.setVersion(2,1);fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
    fmt.setDepthBufferSize(24);fmt.setAlphaBufferSize(8);fmt.setSamples(0)
    return fmt


def _matrix(values):
    return QMatrix4x4(*[float(values[column*4+row]) for row in range(4) for column in range(4)])


def camera_view(pose,mode,*,reference=None):
    view=QMatrix4x4()
    view.lookAt(QVector3D(0,0,0),QVector3D(1,0,0),QVector3D(0,0,1))
    if mode!='follow':return view,''
    attachments={key.casefold():value for key,value in pose.attachments.items()}
    anchors={key.casefold():value for key,value in (reference or {}).items()}
    for name in ('attach_camera','camera','valvebiped.attach_camera','valvebiped.camera'):
        if name not in attachments:continue
        matrix=attachments[name]
        if len(matrix)!=16 or not all(math.isfinite(v) for v in matrix):continue
        inverse,valid=_matrix(matrix).inverted()
        if valid:
            if reference is None:return view*inverse,''
            if name in anchors:return view*_matrix(anchors[name])*inverse,''
            return view,tr('模型摄像头没有有效的初始基准；当前使用固定第一人称视角。')
    return view,tr('模型没有有效 camera/attach_camera；当前使用固定第一人称视角。')


def projection_matrix(width,height,fov=90):
    # Expose horizontal FOV, converting to Qt's vertical perspective angle.
    aspect=max(width,1)/max(height,1)
    vertical=math.degrees(2*math.atan(math.tan(math.radians(float(fov)/2))/aspect))
    projection=QMatrix4x4()
    projection.perspective(vertical,aspect,.05,4096.)
    return projection


def preview_rect(width,height):
    """Use every framebuffer pixel, including wide and tall page layouts."""
    width,height=max(int(width),1),max(int(height),1)
    return 0,0,width,height


def pack_mesh(mesh):
    count=len(mesh.positions)
    if any(len(values)!=count for values in (mesh.normals,mesh.uvs,mesh.bone_indices,mesh.bone_weights)):
        raise ValueError(tr('预览网格顶点属性长度不一致。'))
    vertices=bytearray(count*56)
    for i,(position,normal,uv,bones,weights) in enumerate(zip(
            mesh.positions,mesh.normals,mesh.uvs,mesh.bone_indices,mesh.bone_weights)):
        struct.pack_into('<14f',vertices,i*56,*(tuple(position)+tuple(normal)+tuple(uv)+tuple(bones)+tuple(weights)))
    return bytes(vertices),struct.pack('<'+str(len(mesh.indices))+'I',*mesh.indices)


_VERTEX='''#version 120
attribute vec3 position;
attribute vec3 normal;
attribute vec2 texCoord;
attribute vec3 boneIndex;
attribute vec3 boneWeight;
uniform mat4 bones[BONE_COUNT];
uniform mat4 mvp;
varying vec3 surfaceNormal;
varying vec3 surfacePosition;
varying vec2 uv;
void main() {
    mat4 skin=boneWeight.x*bones[int(boneIndex.x)]
             +boneWeight.y*bones[int(boneIndex.y)]
             +boneWeight.z*bones[int(boneIndex.z)];
    if (dot(boneWeight,vec3(1.0))<0.0001) skin=mat4(1.0);
    vec4 point=skin*vec4(position,1.0);
    surfacePosition=point.xyz;
    surfaceNormal=normalize(mat3(skin)*normal);
    uv=texCoord;
    gl_Position=mvp*point;
}
'''

_FRAGMENT='''#version 120
uniform sampler2D baseTexture;
uniform sampler2D bumpTexture;
uniform vec3 tint;
uniform int useBump;
uniform int alphaTest;
uniform int selfIllum;
uniform int phong;
uniform int translucent;
varying vec3 surfaceNormal;
varying vec3 surfacePosition;
varying vec2 uv;
void main() {
    vec4 base=texture2D(baseTexture,uv);
    if(alphaTest!=0 && base.a<0.5) discard;
    vec3 n=normalize(surfaceNormal);
    if(useBump!=0) {
        vec3 dp1=dFdx(surfacePosition),dp2=dFdy(surfacePosition);
        vec2 duv1=dFdx(uv),duv2=dFdy(uv);
        vec3 t=dp1*duv2.y-dp2*duv1.y;
        vec3 b=-dp1*duv2.x+dp2*duv1.x;
        float scale=max(dot(t,t),dot(b,b));
        if(scale>0.000001) {
            vec3 detail=texture2D(bumpTexture,uv).xyz*2.0-1.0;
            n=normalize(t*inversesqrt(scale)*detail.x+b*inversesqrt(scale)*detail.y+n*detail.z);
        }
    }
    vec3 light=normalize(vec3(-0.5,-0.3,0.8));
    float diffuse=0.40+0.60*abs(dot(n,light));
    if(selfIllum!=0) diffuse=1.0;
    float highlight=0.0;
    if(phong!=0 && selfIllum==0) {
        vec3 eye=normalize(-surfacePosition);
        highlight=pow(max(dot(n,normalize(light+eye)),0.0),24.0)*0.15;
    }
    gl_FragColor=vec4(base.rgb*tint*diffuse+vec3(highlight),translucent!=0?base.a:1.0);
}
'''

_BACKDROP_VERTEX='''#version 120
attribute vec2 position;
attribute vec2 texCoord;
varying vec2 uv;
void main() { uv=texCoord; gl_Position=vec4(position,0.0,1.0); }
'''
_BACKDROP_FRAGMENT='''#version 120
uniform sampler2D background;
varying vec2 uv;
void main() {
    vec3 page=texture2D(background,uv).rgb;
    gl_FragColor=vec4(page*0.95+vec3(0.075,0.085,0.10)*0.05,1.0);
}
'''


class ActionViewport(QOpenGLWidget):
    warning=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setFormat(preview_surface_format())
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_TranslucentBackground,True)
        self.model=None;self.materials=()
        self.camera_mode='fixed'
        self.fov=90
        self.sequence_index=0;self.frame=0.
        self.frame_span=None
        self.render_error='';self.draw_calls=0
        self._functions=None;self._program=None
        self._resources=[];self._pending_upload=False
        self._model_textures={}
        self._pose=None;self._reported=set()
        self._camera_reference=None
        self._background_provider=None
        self._background_program=None;self._background_buffer=None
        self._background_texture=None;self._background_key=None

    def set_background_provider(self,provider):
        self._background_provider=provider
        self._background_key=None
        self.update()

    def _report(self,message):
        if message and message not in self._reported:
            self._reported.add(message);self.warning.emit(display_text(message))

    def set_model(self,model,materials):
        # Hidden viewports may not paint again. Retire the previous model now,
        # rather than retaining its material images and GPU allocations.
        if self._resources or self._model_textures or self._program is not None:
            context=self.context()
            if context is not None and context.isValid():
                self.makeCurrent()
                try:self._destroy()
                finally:self.doneCurrent()
        self.model=model;self.materials=tuple(materials)
        self._pending_upload=True;self.render_error='';self._pose=None
        self._camera_reference=None
        self._reported.clear();self.draw_calls=0
        self.update()

    def set_frame(self,index,frame,*,frame_span=None):
        self.sequence_index=int(index);self.frame=max(float(frame),0.)
        self.frame_span=frame_span
        if self.model:
            try:
                self._pose=(self.model.sample(self.sequence_index,self.frame,frame_span=frame_span)
                    if frame_span is not None else self.model.sample(self.sequence_index,self.frame))
                if self._camera_reference is None:self._camera_reference=dict(self._pose.attachments)
                for message in self.model.warnings:self._report(message)
            except Exception as error:
                self._pose=None;self._report(tr('成品动作求值失败: {error}',error=display_text(str(error))))
        self.update()

    def set_camera_mode(self,mode):
        self.camera_mode='follow' if mode=='follow' else 'fixed';self.update()

    def set_fov(self,value):
        value=float(value)
        if not math.isfinite(value) or not 70<=value<=160:raise ValueError(tr('无效的摄像头 FOV。'))
        self.fov=value;self.update()

    def initializeGL(self):
        self.render_error=''
        self._functions=self.context().functions()
        self._functions.initializeOpenGLFunctions()
        self.context().aboutToBeDestroyed.connect(self.release_resources)
        self._pending_upload=True

    def showEvent(self,event):
        super().showEvent(event)
        QTimer.singleShot(200,self._check_context)

    def _check_context(self):
        if self.isVisible() and not self.isValid():
            self.render_error=tr('无法创建硬件 OpenGL 预览，请检查显卡驱动；当前配置未附带软件渲染库。')
            self._report(self.render_error)

    def _texture(self,image):
        key=id(image) if image is not None else None
        if key in self._model_textures:return self._model_textures[key]
        if image is None:
            raw=bytes((220,40,200,255,55,30,60,255,55,30,60,255,220,40,200,255))
            source=QImage(raw,2,2,8,QImage.Format_RGBA8888).copy()
        else:source=QImage(image.rgba,image.width,image.height,image.width*4,QImage.Format_RGBA8888).copy()
        texture=QOpenGLTexture(source,QOpenGLTexture.DontGenerateMipMaps)
        texture.setMinMagFilters(QOpenGLTexture.Linear,QOpenGLTexture.Linear)
        texture.setWrapMode(QOpenGLTexture.Repeat)
        self._model_textures[key]=texture
        return texture

    def _upload(self):
        self._destroy()
        if self.model is None:return
        if self._pose is None:self.set_frame(self.sequence_index,self.frame,frame_span=self.frame_span)
        if self._pose is None:raise ValueError(tr('没有可绘制的成品动作姿态。'))
        count=len(self._pose.bone_matrices)
        if not count:raise ValueError(tr('成品模型没有可绘制的骨骼。'))
        program=QOpenGLShaderProgram(self)
        self._program=program
        if not program.addShaderFromSourceCode(QOpenGLShader.Vertex,_VERTEX.replace('BONE_COUNT',str(count))):
            raise ValueError(program.log())
        if not program.addShaderFromSourceCode(QOpenGLShader.Fragment,_FRAGMENT):raise ValueError(program.log())
        for location,name in enumerate(('position','normal','texCoord','boneIndex','boneWeight')):
            program.bindAttributeLocation(name,location)
        if not program.link():raise ValueError(program.log())
        for mesh,material in zip(self.model.meshes,self.materials):
            raw,indices=pack_mesh(mesh)
            vbo=QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            ibo=QOpenGLBuffer(QOpenGLBuffer.IndexBuffer)
            if not vbo.create() or not ibo.create():raise ValueError(tr('显卡无法分配预览缓冲区。'))
            vbo.bind();vbo.allocate(raw,len(raw));vbo.release()
            ibo.bind();ibo.allocate(indices,len(indices));ibo.release()
            base=self._texture(material.texture)
            bump=self._texture(material.normal_texture) if material.normal_texture else None
            self._resources.append((vbo,ibo,base,bump,material,len(mesh.indices)))
        if len(self.model.meshes)!=len(self.materials):raise ValueError(tr('预览材质与网格数量不一致。'))
        if not self.model.meshes:self._report(tr('成品模型没有可显示的 LOD0 网格。'))

    def paintGL(self):
        functions=self._functions
        if functions is None:return
        # Application pages supply an aligned backdrop and compose it here.
        # This avoids relying on native QWidget underlay alpha composition.
        if self._background_provider is not None:functions.glClearColor(.075,.085,.10,1.)
        else:functions.glClearColor(.075*.05,.085*.05,.10*.05,.05)
        functions.glClear(0x4000|0x0100)
        try:
            if self._background_provider is not None:self._paint_background()
            if self.model is None or self.render_error:return
            if self._pending_upload:
                self._pending_upload=False;self._upload()
            if self._pose is None or self._program is None:return
            scale=self.devicePixelRatioF()
            x,y,width,height=preview_rect(self.width(),self.height())
            physical=self.size()*scale
            functions.glViewport(int(x*scale),int(y*scale),physical.width(),physical.height())
            functions.glEnable(0x0B71)  # GL_DEPTH_TEST
            functions.glFrontFace(0x0900)  # Source/D3D front winding: GL_CW
            projection=projection_matrix(width,height,self.fov)
            view,note=camera_view(self._pose,self.camera_mode,reference=self._camera_reference)
            self._report(note)
            program=self._program
            program.bind()
            program.setUniformValue(program.uniformLocation('mvp'),projection*view)
            palette=[float(value) for matrix in self._pose.bone_matrices for value in matrix]
            functions.glUniformMatrix4fv(program.uniformLocation('bones[0]'),len(self._pose.bone_matrices),0,palette)
            program.setUniformValue1i(program.uniformLocation('baseTexture'),0)
            program.setUniformValue1i(program.uniformLocation('bumpTexture'),1)
            # Opaque geometry writes depth before translucent surfaces.
            for vbo,ibo,base,bump,material,count in sorted(self._resources,key=lambda item:item[4].translucent or item[4].additive):
                if material.translucent or material.additive:
                    functions.glEnable(0x0BE2)
                    if material.additive:
                        # Add emission without making empty/black glow geometry
                        # cover the underlying application background.
                        functions.glBlendFuncSeparate(0x0302,1,0,1)
                    else:functions.glBlendFuncSeparate(0x0302,0x0303,1,0x0303)
                    functions.glDepthMask(False)
                else:functions.glDisable(0x0BE2);functions.glDepthMask(True)
                if material.no_cull:functions.glDisable(0x0B44)
                else:functions.glEnable(0x0B44);functions.glCullFace(0x0405)
                program.setUniformValue(program.uniformLocation('tint'),QVector3D(*material.tint))
                for name,value in (('useBump',bump is not None),('alphaTest',material.alpha_test),
                                   ('selfIllum',material.self_illum),('phong',material.phong),
                                   ('translucent',material.translucent or material.additive)):
                    program.setUniformValue1i(program.uniformLocation(name),int(value))
                base.bind(0)
                if bump:bump.bind(1)
                vbo.bind();ibo.bind()
                for location,size,offset in ((0,3,0),(1,3,12),(2,2,24),(3,3,32),(4,3,44)):
                    program.enableAttributeArray(location);program.setAttributeBuffer(location,0x1406,offset,size,56)
                functions.glDrawElements(0x0004,count,0x1405,VoidPtr(0))
                self.draw_calls+=1
                for location in range(5):program.disableAttributeArray(location)
                ibo.release();vbo.release();base.release(0)
                if bump:bump.release(1)
            functions.glDepthMask(True);functions.glDisable(0x0BE2);functions.glDisable(0x0B44)
            program.release()
        except Exception as error:
            self.render_error=tr('OpenGL 预览不可用: {error}',error=display_text(str(error)))
            self._report(self.render_error)

    def _paint_background(self):
        image=self._background_provider.image_for(self)
        if image.isNull():return
        if self._background_program is None:
            program=QOpenGLShaderProgram(self);self._background_program=program
            if not program.addShaderFromSourceCode(QOpenGLShader.Vertex,_BACKDROP_VERTEX):raise ValueError(program.log())
            if not program.addShaderFromSourceCode(QOpenGLShader.Fragment,_BACKDROP_FRAGMENT):raise ValueError(program.log())
            program.bindAttributeLocation('position',0);program.bindAttributeLocation('texCoord',1)
            if not program.link():raise ValueError(program.log())
            buffer=QOpenGLBuffer(QOpenGLBuffer.VertexBuffer);self._background_buffer=buffer
            if not buffer.create():raise ValueError(tr('无法创建预览背景缓冲区。'))
            vertices=struct.pack('<16f',-1,-1,0,1, 1,-1,1,1, -1,1,0,0, 1,1,1,0)
            buffer.bind();buffer.allocate(vertices,len(vertices));buffer.release()
        if self._background_key!=image.cacheKey():
            if self._background_texture:self._background_texture.destroy()
            texture=QOpenGLTexture(image,QOpenGLTexture.DontGenerateMipMaps)
            texture.setMinMagFilters(QOpenGLTexture.Linear,QOpenGLTexture.Linear)
            texture.setWrapMode(QOpenGLTexture.ClampToEdge)
            self._background_texture=texture;self._background_key=image.cacheKey()
        functions=self._functions
        scale=self.devicePixelRatioF()
        physical=self.size()*scale
        functions.glViewport(0,0,physical.width(),physical.height())
        functions.glDisable(0x0B71);functions.glDisable(0x0BE2);functions.glDisable(0x0B44)
        functions.glDepthMask(False)
        program=self._background_program;program.bind()
        program.setUniformValue1i(program.uniformLocation('background'),0)
        self._background_texture.bind(0);self._background_buffer.bind()
        for location,offset in ((0,0),(1,8)):
            program.enableAttributeArray(location);program.setAttributeBuffer(location,0x1406,offset,2,16)
        functions.glDrawArrays(0x0005,0,4)  # GL_TRIANGLE_STRIP
        for location in (0,1):program.disableAttributeArray(location)
        self._background_buffer.release();self._background_texture.release(0);program.release()
        functions.glDepthMask(True)

    def _destroy_background(self):
        if self._background_texture:self._background_texture.destroy()
        if self._background_buffer:self._background_buffer.destroy()
        if self._background_program:
            self._background_program.removeAllShaders();self._background_program.deleteLater()
        self._background_texture=None;self._background_buffer=None;self._background_program=None
        self._background_key=None

    def _destroy(self):
        for vbo,ibo,base,bump,_,_ in self._resources:
            vbo.destroy();ibo.destroy()
        for texture in self._model_textures.values():texture.destroy()
        self._model_textures.clear()
        self._resources=[]
        if self._program:
            self._program.removeAllShaders();self._program.deleteLater();self._program=None

    def release_resources(self):
        if self.context() and self.context().isValid():
            self.makeCurrent();self._destroy();self._destroy_background();self.doneCurrent()
        self._pending_upload=True
