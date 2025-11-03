# -*- coding: utf-8 -*-
import sys, ctypes, numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QSlider, QDoubleSpinBox, QFileDialog, QPushButton
)
from OpenGL import GL as gl

# ==================== Shaders ====================
VERT_SRC = r"""
#version 330
layout(location=0) in vec2 aPos;
layout(location=1) in vec2 aUV;
out vec2 vUV;
void main(){ vUV = aUV; gl_Position = vec4(aPos, 0.0, 1.0); }
"""

FRAG_SRC = r"""
#version 330
precision highp float;
uniform sampler2D uTex;
uniform float uSat;   // [-1,1]
uniform float uVib;   // [-1,1]
uniform float uCast;  // [0,1]
uniform vec3  uGain;  // per-channel WB gain (linear)
in vec2 vUV; out vec4 FragColor;

void main(){
  vec3 c = texture(uTex, vUV).rgb;

  // 去偏色（按强度混合到灰世界增益）
  c *= mix(vec3(1.0), uGain, clamp(uCast, 0.0, 1.0));

  // Saturation + Vibrance（中灰增强更多，高光/暗部更保守）
  float luma = dot(c, vec3(0.299, 0.587, 0.114));
  vec3  chroma = c - vec3(luma);
  float satAmt = 1.0 + uSat;
  float vibAmt = 1.0 + uVib;
  float w = 1.0 - clamp(abs(luma - 0.5) * 2.0, 0.0, 1.0);
  chroma *= satAmt * mix(1.0, vibAmt, w);

  vec3 outc = vec3(luma) + chroma;
  FragColor = vec4(clamp(outc, 0.0, 1.0), 1.0);
}
"""

# Compute Shader：统计 S 平均/中位、高/低光占比、肤色占比、线性均值（算灰世界增益）
COMPUTE_SRC = r"""
#version 430
layout(local_size_x=16, local_size_y=16, local_size_z=1) in;
layout(binding=0) uniform sampler2D uTex;

struct GroupStats {
  float sumS;           // 0x00
  float sumLinR;        // 0x04
  float sumLinG;        // 0x08
  float sumLinB;        // 0x0c
  uint  countN;         // 0x10
  uint  countVHi;       // 0x14
  uint  countVLo;       // 0x18
  uint  countSkin;      // 0x1c
  uint  hist[64];       // 0x20 .. +256
};                      // 有效负载 288B，数组 stride 可能被实现对齐到 >288

layout(std430, binding=1) buffer StatsBuf { GroupStats gs[]; };

shared float s_sumS;
shared float s_sumLinR, s_sumLinG, s_sumLinB;
shared uint  s_countN, s_countVHi, s_countVLo, s_countSkin;
shared uint  s_hist[64];

vec3 to_linear(vec3 x){
  const float a = 0.055;
  vec3 y;
  for(int i=0;i<3;i++){
    y[i] = (x[i] <= 0.04045) ? (x[i]/12.92) : pow((x[i]+a)/(1.0+a), 2.4);
  }
  return y;
}
vec3 rgb2hsv(vec3 c){
  float r=c.r,g=c.g,b=c.b;
  float mx = max(r, max(g,b));
  float mn = min(r, min(g,b));
  float d  = mx - mn + 1e-8;
  float h = 0.0;
  if(mx==r)      h = mod((g-b)/d, 6.0);
  else if(mx==g) h = ((b-r)/d) + 2.0;
  else           h = ((r-g)/d) + 4.0;
  h /= 6.0;
  float s = d/(mx+1e-8);
  float v = mx;
  return vec3(h,s,v);
}

void main(){
  uvec2 gid = gl_WorkGroupID.xy;
  uvec2 lid = gl_LocalInvocationID.xy;
  uvec2 gsz = gl_NumWorkGroups.xy;
  uint groupIndex = gid.y*gsz.x + gid.x;

  if(lid.x==0 && lid.y==0){
    s_sumS = 0.0;
    s_sumLinR = s_sumLinG = s_sumLinB = 0.0;
    s_countN = s_countVHi = s_countVLo = s_countSkin = 0u;
    for(int i=0;i<64;i++) s_hist[i] = 0u;
  }
  barrier();

  ivec2 size = textureSize(uTex, 0);
  ivec2 base = ivec2(gid * uvec2(16,16));
  ivec2 p = base + ivec2(lid);

  if(p.x < size.x && p.y < size.y){
    vec3 srgb = texelFetch(uTex, p, 0).rgb;
    vec3 hsv  = rgb2hsv(srgb);
    float S = hsv.g, V = hsv.b;
    vec3 lin = to_linear(srgb);

    atomicAdd(s_countN, 1u);
    if(V > 0.90) atomicAdd(s_countVHi, 1u);
    if(V < 0.05) atomicAdd(s_countVLo, 1u);

    float Hdeg = hsv.r * 360.0;
    if(Hdeg>10.0 && Hdeg<50.0 && S>0.1 && S<0.6) atomicAdd(s_countSkin, 1u);

    int bin = int(clamp(floor(S*64.0), 0.0, 63.0));
    atomicAdd(s_hist[bin], 1u);

    // float 累加（共享变量）
    s_sumS    += S;
    s_sumLinR += lin.r;
    s_sumLinG += lin.g;
    s_sumLinB += lin.b;
  }
  barrier();

  if(lid.x==0 && lid.y==0){
    gs[groupIndex].sumS    = s_sumS;
    gs[groupIndex].sumLinR = s_sumLinR;
    gs[groupIndex].sumLinG = s_sumLinG;
    gs[groupIndex].sumLinB = s_sumLinB;
    gs[groupIndex].countN  = s_countN;
    gs[groupIndex].countVHi= s_countVHi;
    gs[groupIndex].countVLo= s_countVLo;
    gs[groupIndex].countSkin = s_countSkin;
    for(int i=0;i<64;i++) gs[groupIndex].hist[i] = s_hist[i];
  }
}
"""

# ==================== 小工具 ====================
def qimage_to_rgb_np(qimg: QImage) -> np.ndarray:
    qimg = qimg.convertToFormat(QImage.Format_RGB888).copy()
    w, h = qimg.width(), qimg.height()
    arr = np.frombuffer(bytes(qimg.bits()), dtype=np.uint8).reshape((h, qimg.bytesPerLine()))
    return arr[:, :w*3].reshape((h, w, 3)).copy()

def smoothstep(e0, e1, x):
    t = np.clip((x-e0)/(e1-e0 + 1e-8), 0, 1)
    return t*t*(3-2*t)

# —— 聚合映射（含抬底+放大，避免 sat/vib 过小被量化到 0）——
def aggregate_params_from_stats(Sbar, S50, Hpct, Dpct, SkinPct, CastMag, t):
    k_hi   = max(0.35, 1 - smoothstep(0.02, 0.15, Hpct))
    k_skin = 0.6 + 0.4 * (1 - np.clip(SkinPct, 0, 1))
    k_sat0 = (1 - S50)**0.6
    k_vib0 = (1 - Sbar)

    base_sat = 0.25 + 0.75 * k_sat0
    base_vib = 0.25 + 0.75 * k_vib0
    AMP_SAT, AMP_VIB = 1.6, 1.4

    sat  = AMP_SAT * 0.9 * t * base_sat * k_hi * k_skin
    vib  = AMP_VIB * 0.7 * t * base_vib * k_hi * k_skin
    cast = 0.8 * abs(t) * np.clip(CastMag/0.4, 0, 1)

    eps = 0.01 * max(0.0, abs(t))
    if abs(sat) < eps: sat = np.sign(t) * eps
    if abs(vib) < eps: vib = np.sign(t) * eps

    return float(np.clip(sat,-1,1)), float(np.clip(vib,-1,1)), float(np.clip(cast,0,1))

# ==================== GL 视图（渲染 + 统计） ====================
class GLView(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.program = None
        self.vao = self.vbo = None
        self.tex = None

        # uniforms
        self.uTex = self.uSat = self.uVib = self.uCast = self.uGain = -1
        # params
        self.sat = 0.0
        self.vib = 0.0
        self.cast = 0.0
        self.gain = (1.0, 1.0, 1.0)

        # compute
        self.cs_prog = None
        self.ssbo = None
        self.stats = None   # (Sbar, S50, Hpct, Dpct, SkinPct, CastMag, gains[3])
        self.setMinimumSize(900, 600)

    # ---------- GL lifecycle ----------
    def initializeGL(self):
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glDisable(gl.GL_BLEND)

        # program
        vs = gl.glCreateShader(gl.GL_VERTEX_SHADER)
        gl.glShaderSource(vs, VERT_SRC); gl.glCompileShader(vs)
        if gl.glGetShaderiv(vs, gl.GL_COMPILE_STATUS) != gl.GL_TRUE:
            raise RuntimeError(gl.glGetShaderInfoLog(vs).decode())
        fs = gl.glCreateShader(gl.GL_FRAGMENT_SHADER)
        gl.glShaderSource(fs, FRAG_SRC); gl.glCompileShader(fs)
        if gl.glGetShaderiv(fs, gl.GL_COMPILE_STATUS) != gl.GL_TRUE:
            raise RuntimeError(gl.glGetShaderInfoLog(fs).decode())
        self.program = gl.glCreateProgram()
        gl.glAttachShader(self.program, vs); gl.glAttachShader(self.program, fs)
        gl.glLinkProgram(self.program)
        if gl.glGetProgramiv(self.program, gl.GL_LINK_STATUS) != gl.GL_TRUE:
            raise RuntimeError(gl.glGetProgramInfoLog(self.program).decode())
        gl.glDeleteShader(vs); gl.glDeleteShader(fs)

        self.uTex  = gl.glGetUniformLocation(self.program, b"uTex")
        self.uSat  = gl.glGetUniformLocation(self.program, b"uSat")
        self.uVib  = gl.glGetUniformLocation(self.program, b"uVib")
        self.uCast = gl.glGetUniformLocation(self.program, b"uCast")
        self.uGain = gl.glGetUniformLocation(self.program, b"uGain")

        # quad
        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(self.vao)
        quad = np.array([
            -1, -1, 0, 0,
             1, -1, 1, 0,
            -1,  1, 0, 1,
             1,  1, 1, 1
        ], dtype=np.float32)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, quad.nbytes, quad, gl.GL_STATIC_DRAW)
        stride = 16
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, False, stride, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(1, 2, gl.GL_FLOAT, False, stride, ctypes.c_void_p(8))
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)

        # texture
        self.tex = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

        # compute program + ssbo
        try:
            cs = gl.glCreateShader(gl.GL_COMPUTE_SHADER)
            gl.glShaderSource(cs, COMPUTE_SRC); gl.glCompileShader(cs)
            if gl.glGetShaderiv(cs, gl.GL_COMPILE_STATUS) != gl.GL_TRUE:
                raise RuntimeError(gl.glGetShaderInfoLog(cs).decode())
            self.cs_prog = gl.glCreateProgram()
            gl.glAttachShader(self.cs_prog, cs); gl.glLinkProgram(self.cs_prog)
            if gl.glGetProgramiv(self.cs_prog, gl.GL_LINK_STATUS) != gl.GL_TRUE:
                raise RuntimeError(gl.glGetProgramInfoLog(self.cs_prog).decode())
            gl.glDeleteShader(cs)
            self.ssbo = gl.glGenBuffers(1)
            print("[GPU] Compute enabled (OpenGL >= 4.3)")
        except Exception as e:
            self.cs_prog = None
            print("[GPU] Compute init failed, fallback to CPU stats. Reason:", e)

    def resizeGL(self, w, h):
        gl.glViewport(0, 0, w, h)

    def paintGL(self):
        gl.glClearColor(0.08, 0.08, 0.09, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glUseProgram(self.program)
        gl.glActiveTexture(gl.GL_TEXTURE0); gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        if self.uTex  != -1: gl.glUniform1i(self.uTex, 0)
        if self.uSat  != -1: gl.glUniform1f(self.uSat,  self.sat)
        if self.uVib  != -1: gl.glUniform1f(self.uVib,  self.vib)
        if self.uCast != -1: gl.glUniform1f(self.uCast, self.cast)
        if self.uGain != -1: gl.glUniform3f(self.uGain, *[float(x) for x in self.gain])
        gl.glBindVertexArray(self.vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    # ---------- 接口 ----------
    def setImage(self, np_rgb_u8: np.ndarray):
        h, w = np_rgb_u8.shape[:2]
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        if np_rgb_u8.shape[2] == 3:
            rgba = np.concatenate([np_rgb_u8, np.full((h, w, 1), 255, dtype=np.uint8)], axis=2)
        else:
            rgba = np_rgb_u8
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, rgba)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

        # 统计（GPU优先）
        self.stats = self.compute_stats(w, h)
        self.update()

    def setParams(self, sat, vib, cast, gain3):
        self.sat  = float(sat)
        self.vib  = float(vib)
        self.cast = float(cast)
        self.gain = (float(gain3[0]), float(gain3[1]), float(gain3[2]))
        self.update()

    # ---------- GPU 统计（Compute Shader） ----------
    def compute_stats(self, W, H):
        if self.cs_prog is None:
            # 极简 CPU 回退：只做 WB 增益，其他统计设默认（避免阻塞演示）
            # 你也可以接入完整 CPU 统计函数
            gains = np.array([1.0,1.0,1.0], dtype=np.float32)
            return (0.5, 0.5, 0.0, 0.0, 0.0, 0.0, gains)

        gx, gy = (W + 15)//16, (H + 15)//16
        group_count = gx * gy

        # SSBO 过量分配（320B/组，防实现对齐）
        gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, self.ssbo)
        gl.glBufferData(gl.GL_SHADER_STORAGE_BUFFER, group_count * 320, None, gl.GL_DYNAMIC_READ)
        gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, 0)

        # 绑定并 dispatch
        gl.glUseProgram(self.cs_prog)
        loc = gl.glGetUniformLocation(self.cs_prog, b"uTex")
        if loc != -1: gl.glUniform1i(loc, 0)
        gl.glActiveTexture(gl.GL_TEXTURE0); gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glBindBufferBase(gl.GL_SHADER_STORAGE_BUFFER, 1, self.ssbo)
        gl.glDispatchCompute(gx, gy, 1)
        gl.glMemoryBarrier(gl.GL_SHADER_STORAGE_BARRIER_BIT | gl.GL_BUFFER_UPDATE_BARRIER_BIT)

        # 读取 SSBO
        gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, self.ssbo)
        total_size = gl.glGetBufferParameteriv(gl.GL_SHADER_STORAGE_BUFFER, gl.GL_BUFFER_SIZE)
        if not isinstance(total_size, int): total_size = int(total_size)

        ptr = gl.glMapBuffer(gl.GL_SHADER_STORAGE_BUFFER, gl.GL_READ_ONLY)
        if not ptr:
            gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, 0)
            return (0.5,0.5,0,0,0,0,np.array([1,1,1], np.float32))
        raw = ctypes.string_at(ptr, total_size)
        gl.glUnmapBuffer(gl.GL_SHADER_STORAGE_BUFFER)
        gl.glBindBuffer(gl.GL_SHADER_STORAGE_BUFFER, 0)

        stride = total_size // max(group_count,1)
        if stride < 288:  # 不应发生
            stride = 288

        sumS = 0.0
        sumLinR = sumLinG = sumLinB = 0.0
        countN = countVHi = countVLo = countSkin = 0
        hist = np.zeros(64, dtype=np.uint64)

        for i in range(group_count):
            base = i * stride
            if base + 288 > len(raw): break
            s_sumS    = np.frombuffer(raw, dtype='<f4', count=1, offset=base + 0x00)[0]
            s_sumLinR = np.frombuffer(raw, dtype='<f4', count=1, offset=base + 0x04)[0]
            s_sumLinG = np.frombuffer(raw, dtype='<f4', count=1, offset=base + 0x08)[0]
            s_sumLinB = np.frombuffer(raw, dtype='<f4', count=1, offset=base + 0x0c)[0]
            s_countN   = np.frombuffer(raw, dtype='<u4', count=1, offset=base + 0x10)[0]
            s_countVHi = np.frombuffer(raw, dtype='<u4', count=1, offset=base + 0x14)[0]
            s_countVLo = np.frombuffer(raw, dtype='<u4', count=1, offset=base + 0x18)[0]
            s_countSkin= np.frombuffer(raw, dtype='<u4', count=1, offset=base + 0x1c)[0]
            s_hist = np.frombuffer(raw, dtype='<u4', count=64, offset=base + 0x20)

            sumS     += float(s_sumS)
            sumLinR  += float(s_sumLinR)
            sumLinG  += float(s_sumLinG)
            sumLinB  += float(s_sumLinB)
            countN   += int(s_countN)
            countVHi += int(s_countVHi)
            countVLo += int(s_countVLo)
            countSkin+= int(s_countSkin)
            hist     += s_hist.astype(np.uint64)

        if countN == 0:
            gains = np.array([1.0,1.0,1.0], dtype=np.float32)
            return (0.5,0.5,0,0,0,0,gains)

        Sbar = sumS / countN
        cumsum = np.cumsum(hist)
        half = countN // 2
        bin_idx = int(np.searchsorted(cumsum, half))
        bin_idx = min(max(bin_idx, 0), 63)
        S50 = (bin_idx + 0.5) / 64.0

        Hpct = countVHi / countN
        Dpct = countVLo / countN
        SkinPct = countSkin / countN

        means = np.array([sumLinR, sumLinG, sumLinB], dtype=np.float64) / max(countN, 1)
        means += 1e-8
        m = float(np.mean(means))
        gains = (m/means).astype(np.float32)
        CastMag = float(np.linalg.norm(np.log(gains)))

        return (float(Sbar), float(S50), float(Hpct), float(Dpct),
                float(SkinPct), float(CastMag), gains)

# ==================== UI（含聚合控制） ====================
class Slider(QWidget):
    def __init__(self, title, mn, mx, step, init, cb, scale=1000, decimals=None):
        super().__init__()
        h = QHBoxLayout(self)
        h.addWidget(QLabel(title))
        self._scale = int(scale)
        self.sld = QSlider(Qt.Horizontal); h.addWidget(self.sld, 1)
        self.box = QDoubleSpinBox(); h.addWidget(self.box)
        if decimals is None:
            decimals = max(0, len(str(self._scale))-1)
        self.box.setDecimals(decimals)
        self.box.setRange(float(mn), float(mx))
        self.box.setSingleStep(float(step))
        self.box.setValue(float(init))
        self.sld.setRange(int(mn*self._scale), int(mx*self._scale))
        self.sld.setSingleStep(max(1, int(step*self._scale)))
        self.sld.setValue(int(init*self._scale))

        def s2b(v):
            val = v/self._scale
            self.box.setValue(val); cb(val)
        def b2s(v):
            self.sld.setValue(int(round(v*self._scale))); cb(v)

        self.sld.valueChanged.connect(s2b)
        self.box.valueChanged.connect(b2s)

    def set_value_silent(self, val: float):
        self.sld.blockSignals(True); self.box.blockSignals(True)
        self.sld.setValue(int(round(val * self._scale)))
        self.box.setValue(float(val))
        self.sld.blockSignals(False); self.box.blockSignals(False)

class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QOpenGLWidget + Compute 聚合控制（零 readback）")
        self.resize(1280, 800)

        # GL 4.6 Core 优先
        fmt = QSurfaceFormat()
        fmt.setRenderableType(QSurfaceFormat.OpenGL)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setVersion(4, 6)
        fmt.setSwapBehavior(QSurfaceFormat.DoubleBuffer)
        fmt.setSwapInterval(1)
        QSurfaceFormat.setDefaultFormat(fmt)

        w = QWidget(); self.setCentralWidget(w)
        v = QVBoxLayout(w)

        # 顶部按钮
        top = QWidget(); v.addWidget(top, 0)
        th = QHBoxLayout(top)
        self.btn = QPushButton("Open…"); th.addWidget(self.btn, 0)
        th.addStretch(1)

        # GL 视图
        self.gl = GLView(); v.addWidget(self.gl, 1)

        # 控件区
        bar = QWidget(); v.addWidget(bar, 0)
        bh = QHBoxLayout(bar)
        # 主滑块 t [-1,1]，用于聚合映射
        self.s_master = Slider("Color Boost", -1.0, 1.0, 0.01, 0.0, self.on_master, scale=100)
        # 三个细调（毫级精度），允许用户覆盖
        self.s_sat  = Slider("Saturation", -1.0, 1.0, 0.001, 0.0, self.on_fine, scale=1000)
        self.s_vib  = Slider("Vibrance",   -1.0, 1.0, 0.001, 0.0, self.on_fine, scale=1000)
        self.s_cast = Slider("Cast(WB)",    0.0, 1.0, 0.001, 0.0, self.on_fine, scale=1000)
        for s in (self.s_master, self.s_sat, self.s_vib, self.s_cast):
            bh.addWidget(s)

        self.btn.clicked.connect(self.open_file)
        self._img_loaded = False
        self._last_auto = (0.0, 0.0, 0.0)  # 调试查看

    def open_file(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Open Image", "", "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)")
        if not fn: return
        qimg = QImage(fn)
        if qimg.isNull(): return
        arr = qimage_to_rgb_np(qimg)
        self.gl.makeCurrent()
        self.gl.setImage(arr)
        self._img_loaded = True
        # 载入后基于统计立即做一次聚合
        self.apply_aggregate_from_master(self.s_master.box.value())

    # —— 主滑块：做聚合，推送到三个细调 & 立即应用 ——
    def on_master(self, t):
        if not self._img_loaded or self.gl.stats is None:
            return
        self.apply_aggregate_from_master(float(t))

    def apply_aggregate_from_master(self, t):
        Sbar, S50, Hpct, Dpct, SkinPct, CastMag, gains = self.gl.stats
        sat, vib, cast = aggregate_params_from_stats(Sbar, S50, Hpct, Dpct, SkinPct, CastMag, t)
        self._last_auto = (sat, vib, cast)
        # 推送到细调滑块（静默，避免回调打断）
        self.s_sat.set_value_silent(sat)
        self.s_vib.set_value_silent(vib)
        self.s_cast.set_value_silent(cast)
        # 应用到 GPU
        self.gl.setParams(sat, vib, cast, gains)

    # —— 细调：用户覆盖，直接应用 ——
    def on_fine(self, _):
        if not self._img_loaded:
            return
        sat  = self.s_sat.box.value()
        vib  = self.s_vib.box.value()
        cast = self.s_cast.box.value()
        gains = self.gl.stats[6] if self.gl.stats is not None else (1.0,1.0,1.0)
        self.gl.setParams(sat, vib, cast, gains)

def main():
    app = QApplication(sys.argv)
    win = Main(); win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
