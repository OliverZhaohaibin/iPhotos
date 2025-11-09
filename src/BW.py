# -*- coding: utf-8 -*-
"""
Minimal standalone GPU-accelerated Black & White viewer demo.
Four sliders: Intensity / Neutrals / Tone / Grain.
"""

import sys, ctypes, numpy as np
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QImage, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtOpenGL import (
    QOpenGLFunctions_3_3_Core, QOpenGLShaderProgram, QOpenGLShader, QOpenGLVertexArrayObject
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSlider, QPushButton
)
from OpenGL import GL as gl


# ======================= GLSL shaders =======================
VERT_SRC = r"""
#version 330 core
out vec2 vUV;
void main() {
    const vec2 POS[3] = vec2[3](
        vec2(-1.0, -1.0),
        vec2( 3.0, -1.0),
        vec2(-1.0,  3.0)
    );
    const vec2 UVS[3] = vec2[3](
        vec2(0.0, 0.0),
        vec2(2.0, 0.0),
        vec2(0.0, 2.0)
    );
    vUV = UVS[gl_VertexID];
    gl_Position = vec4(POS[gl_VertexID], 0.0, 1.0);
}
"""

FRAG_SRC = r"""
#version 330 core
in vec2 vUV;
out vec4 FragColor;

uniform sampler2D uTex;
uniform float uIntensity;   // 黑白强度 [0,1]
uniform float uNeutrals;    // 中性灰 [-1,1]
uniform float uTone;        // 对比 [-1,1]
uniform float uGrain;       // 颗粒 [0,1]
uniform vec2  uTexSize;

// 灰度计算（BT.709）
float luminance(vec3 c){ return dot(c, vec3(0.2126, 0.7152, 0.0722)); }

// gamma 调整（中性灰）
float gamma_adjust(float x, float n){
    float g = pow(2.0, -n); // neutrals=+1 -> gamma≈0.5 提亮
    return pow(clamp(x,0.0,1.0), g);
}

// 对比 S 曲线
float contrast_adjust(float x, float t){
    float k = 1.0 + 2.0 * t;
    float logit = log(x / (1.0 - x));
    float y = 1.0 / (1.0 + exp(-logit * k));
    return clamp(y, 0.0, 1.0);
}

// 简单颗粒噪声
float grain_noise(vec2 uv, float grain){
    if (grain <= 0.0) return 0.0;
    float n = fract(sin(dot(uv, vec2(12.9898,78.233))) * 43758.5453);
    return (n - 0.5) * 0.2 * grain;
}

void main(){
    vec2 uv = vUV;
    vec3 c = texture(uTex, uv).rgb;
    float gray = luminance(c);
    // 黑白强度混合
    c = mix(c, vec3(gray), uIntensity);
    gray = luminance(c);
    // Neutrals
    gray = gamma_adjust(gray, uNeutrals);
    // Tone
    gray = contrast_adjust(gray, uTone);
    // Grain
    gray = gray + grain_noise(uv * uTexSize, uGrain);
    FragColor = vec4(vec3(clamp(gray,0.0,1.0)), 1.0);
}
"""

# ======================= OpenGL widget =======================
class GLBWViewer(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)
        self._img = None
        self._tex_id = 0
        self._shader = None
        self._vao = None
        self._uniforms = {}
        self.params = dict(Intensity=1.0, Neutrals=0.0, Tone=0.0, Grain=0.0)

    def initializeGL(self):
        self.gl = QOpenGLFunctions_3_3_Core()
        self.gl.initializeOpenGLFunctions()
        self._vao = QOpenGLVertexArrayObject(self)
        self._vao.create()
        self._vao.bind()

        prog = QOpenGLShaderProgram(self)
        prog.addShaderFromSourceCode(QOpenGLShader.Vertex, VERT_SRC)
        prog.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAG_SRC)
        prog.link()
        self._shader = prog
        for n in ["uTex","uIntensity","uNeutrals","uTone","uGrain","uTexSize"]:
            self._uniforms[n] = prog.uniformLocation(n)
        print("[GL] Shader linked; uniforms:", self._uniforms)

    def paintGL(self):
        gl.glClearColor(0,0,0,1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        if not self._shader or not self._img: return
        self._shader.bind()
        if self._vao: self._vao.bind()
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex_id)
        self.gl.glUniform1i(self._uniforms["uTex"], 0)

        # 上传滑块参数
        self.gl.glUniform1f(self._uniforms["uIntensity"], float(self.params["Intensity"]))
        self.gl.glUniform1f(self._uniforms["uNeutrals"], float(self.params["Neutrals"]))
        self.gl.glUniform1f(self._uniforms["uTone"], float(self.params["Tone"]))
        self.gl.glUniform1f(self._uniforms["uGrain"], float(self.params["Grain"]))
        if self._img:
            w, h = self._img.width(), self._img.height()
            self.gl.glUniform2f(self._uniforms["uTexSize"], float(w), float(h))
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        if self._vao: self._vao.release()
        self._shader.release()

    def load_image(self, path: str):
        img = QImage(path)
        if img.isNull(): return
        self._img = img.convertToFormat(QImage.Format_RGBA8888)
        self.makeCurrent()
        self._upload_texture()
        self.doneCurrent()
        self.update()

    def _upload_texture(self):
        if self._tex_id:
            gl.glDeleteTextures(1, np.array([self._tex_id], np.uint32))
        tex = gl.glGenTextures(1)
        if isinstance(tex, (list,tuple)): tex=tex[0]
        self._tex_id = int(tex)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._tex_id)
        w,h = self._img.width(), self._img.height()
        ptr = self._img.constBits()
        nbytes = self._img.sizeInBytes()
        if hasattr(ptr,"setsize"): ptr.setsize(nbytes)
        gl.glTexImage2D(gl.GL_TEXTURE_2D,0,gl.GL_RGBA8,w,h,0,gl.GL_RGBA,gl.GL_UNSIGNED_BYTE,ptr)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        print(f"[GL] Texture uploaded {w}x{h} id={self._tex_id}")


# ======================= GUI MainWindow =======================
class BWMain(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GPU Black & White Demo")
        self.resize(1000, 700)
        self.viewer = GLBWViewer()
        layout = QVBoxLayout()
        layout.addWidget(self.viewer, 1)

        # 控件区
        panel = QWidget()
        pl = QVBoxLayout(panel)
        def add_slider(name, rng=(-100,100), init=0):
            hl = QHBoxLayout()
            lab = QLabel(f"{name}: {init}")
            sld = QSlider(Qt.Horizontal)
            sld.setRange(*rng)
            sld.setValue(init)
            sld.valueChanged.connect(lambda v,n=name,l=lab:self._on_change(n,v/100.0,l))
            hl.addWidget(lab)
            hl.addWidget(sld)
            pl.addLayout(hl)

        add_slider("Intensity", (0,100), 100)
        add_slider("Neutrals", (-100,100), 0)
        add_slider("Tone", (-100,100), 0)
        add_slider("Grain", (0,100), 0)

        btn = QPushButton("Open Image")
        btn.clicked.connect(self.open_image)
        pl.addWidget(btn)
        layout.addWidget(panel)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

    def _on_change(self, name, val, label):
        label.setText(f"{name}: {val:+.2f}")
        self.viewer.params[name] = val
        self.viewer.update()

    def open_image(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Open Image", "", "Images (*.png *.jpg *.jpeg *.bmp)")
        if fn:
            self.viewer.load_image(fn)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = BWMain()
    win.show()
    sys.exit(app.exec())
