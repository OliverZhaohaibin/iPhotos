# -*- coding: utf-8 -*-
"""
OpenGL Cropper — No Black Bars + Edge-Push Auto Zoom-Out + Idle Fade-Out
(纵向/横向压力 → 缩小 + 反向平移；保持无黑边；空闲时隐藏框外内容)
Deps: pip install PySide6 PyOpenGL Pillow
"""

import sys, math, time
from dataclasses import dataclass
from typing import Optional, Tuple

from PIL import Image
from OpenGL import GL as gl

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog, QWidget, QVBoxLayout
from PySide6.QtOpenGLWidgets import QOpenGLWidget

# ===========================
# Math primitives
# ===========================
@dataclass
class Vec2:
    x: float
    y: float
    def __add__(self, o): return Vec2(self.x + o.x, self.y + o.y)
    def __sub__(self, o): return Vec2(self.x - o.x, self.y - o.y)
    def __mul__(self, k: float): return Vec2(self.x * k, self.y * k)

@dataclass
class Rect:
    cx: float; cy: float; w: float; h: float
    def l(self): return self.cx - self.w * 0.5
    def r(self): return self.cx + self.w * 0.5
    def b(self): return self.cy - self.h * 0.5
    def t(self): return self.cy + self.h * 0.5
    def set_l(self, L): self.w = self.r() - L; self.cx = L + self.w*0.5
    def set_r(self, R): self.w = R - self.l(); self.cx = self.l() + self.w*0.5
    def set_b(self, B): self.h = self.t() - B; self.cy = B + self.h*0.5
    def set_t(self, T): self.h = T - self.b(); self.cy = self.b() + self.h*0.5
    def as_corners(self):
        return [Vec2(self.l(), self.b()), Vec2(self.r(), self.b()),
                Vec2(self.r(), self.t()), Vec2(self.l(), self.t())]
    def fit_aspect(self, aspect: float):
        cur = self.w / max(1e-6, self.h)
        if cur > aspect:
            self.h = self.w / aspect
        else:
            self.w = self.h * aspect

# ===========================
# Camera (world Y up, screen Y down)
# ===========================
class Camera2D:
    def __init__(self):
        self.center = Vec2(0, 0)
        self.scale  = 1.0          # screen_px per world_unit
        self.vw = 1; self.vh = 1

    def set_viewport(self, w:int, h:int):
        self.vw, self.vh = max(1, w), max(1, h)

    def world_to_screen(self, p:Vec2) -> Vec2:
        hvx = self.vw/(2*self.scale); hvy = self.vh/(2*self.scale)
        L = self.center.x - hvx; R = self.center.x + hvx
        B = self.center.y - hvy; T = self.center.y + hvy
        x_ndc = (p.x - L)/(R - L)*2 - 1
        y_ndc = (p.y - B)/(T - B)*2 - 1
        x_px = (x_ndc + 1) * 0.5 * self.vw
        y_px = (1 - y_ndc) * 0.5 * self.vh
        return Vec2(x_px, y_px)

    def screen_to_world(self, x_px:float, y_px:float) -> Vec2:
        x_ndc = 2*x_px/self.vw - 1
        y_ndc = 1 - 2*y_px/self.vh
        hvx = self.vw/(2*self.scale); hvy = self.vh/(2*self.scale)
        L = self.center.x - hvx; R = self.center.x + hvx
        B = self.center.y - hvy; T = self.center.y + hvy
        wx = L + (x_ndc + 1)*0.5*(R-L)
        wy = B + (y_ndc + 1)*0.5*(T-B)
        return Vec2(wx, wy)

    def screen_vec_to_world_vec(self, dpx:Tuple[float,float]) -> Vec2:
        a = self.screen_to_world(0,0); b = self.screen_to_world(dpx[0], dpx[1])
        return Vec2(b.x - a.x, b.y - a.y)

    def fit_rect(self, rect:Rect, padding_px:int=20) -> Tuple[Vec2, float]:
        canvas_w = max(1, self.vw - 2*padding_px)
        canvas_h = max(1, self.vh - 2*padding_px)
        sx = canvas_w / rect.w
        sy = canvas_h / rect.h
        scale = min(sx, sy)
        return Vec2(rect.cx, rect.cy), scale

# ===========================
# Crop box
# ===========================
class Handle:
    NONE=0; L=1; R=2; B=3; T=4; LT=5; RT=6; RB=7; LB=8
    INSIDE=-1

def _dist_to_segment(px, py, ax, ay, bx, by) -> float:
    vx, vy = bx-ax, by-ay
    if vx == 0 and vy == 0:
        return math.hypot(px-ax, py-ay)
    t = max(0.0, min(1.0, ((px-ax)*vx+(py-ay)*vy)/(vx*vx+vy*vy)))
    qx, qy = ax + t*vx, ay + t*vy
    return math.hypot(px-qx, py-qy)

class CropBox:
    def __init__(self):
        self.rect = Rect(0,0,100,100)
        self.min_w = 10; self.min_h = 10
        self.hit_pad_px = 8

    def set_to_image_bounds(self, img_w:int, img_h:int):
        self.rect = Rect(0,0,img_w, img_h)

    def hit_test(self, screen_pt:Vec2, cam:Camera2D) -> int:
        r = self.rect
        c = [cam.world_to_screen(v) for v in r.as_corners()]  # LB, RB, RT, LT
        # corners
        corner_map = [Handle.LB, Handle.RB, Handle.RT, Handle.LT]
        for i, h in enumerate(corner_map):
            if math.hypot(screen_pt.x - c[i].x, screen_pt.y - c[i].y) <= self.hit_pad_px:
                return h
        # edges
        edges = [(0,1,Handle.B),(1,2,Handle.R),(2,3,Handle.T),(3,0,Handle.L)]
        for i,j,h in edges:
            if _dist_to_segment(screen_pt.x, screen_pt.y, c[i].x, c[i].y, c[j].x, c[j].y) <= self.hit_pad_px:
                return h
        # inside
        wpt = cam.screen_to_world(screen_pt.x, screen_pt.y)
        if (r.l() <= wpt.x <= r.r()) and (r.b() <= wpt.y <= r.t()):
            return Handle.INSIDE
        return Handle.NONE

    def drag_edge(self, handle:int, d:Vec2, img_bounds:Rect, lock_aspect:Optional[float]=None):
        r = self.rect
        if handle in (Handle.L, Handle.LT, Handle.LB):
            new_l = r.l() + d.x
            new_l = min(new_l, r.r() - self.min_w)
            new_l = max(new_l, img_bounds.l())
            r.set_l(new_l)
        if handle in (Handle.R, Handle.RT, Handle.RB):
            new_r = r.r() + d.x
            new_r = max(new_r, r.l() + self.min_w)
            new_r = min(new_r, img_bounds.r())
            r.set_r(new_r)
        if handle in (Handle.B, Handle.LB, Handle.RB):
            new_b = r.b() + d.y
            new_b = min(new_b, r.t() - self.min_h)
            new_b = max(new_b, img_bounds.b())
            r.set_b(new_b)
        if handle in (Handle.T, Handle.LT, Handle.RT):
            new_t = r.t() + d.y
            new_t = max(new_t, r.b() + self.min_h)
            new_t = min(new_t, img_bounds.t())
            r.set_t(new_t)
        if lock_aspect:
            r.fit_aspect(lock_aspect)

# ===========================
# Shaders
# ===========================
IMG_VERT = r"""
#version 330 core
layout(location=0) in vec2 aPos;   // image local coords (centered)
layout(location=1) in vec2 aUV;
out vec2 vUV;

// image model transform
uniform vec2  uImgOffset;  // world
uniform float uImgScale;   // scalar

// camera (view/projection)
uniform vec2  uCenter;     // world
uniform float uScale;      // screen_px/world
uniform vec2  uViewport;   // px
uniform int   uFlipV;

vec2 world_to_ndc(vec2 p){
    float hvx = uViewport.x / (2.0*uScale);
    float hvy = uViewport.y / (2.0*uScale);
    float L = uCenter.x - hvx;
    float R = uCenter.x + hvx;
    float B = uCenter.y - hvy;
    float T = uCenter.y + hvy;
    float x = (p.x - L) / (R - L) * 2.0 - 1.0;
    float y = (p.y - B) / (T - B) * 2.0 - 1.0;
    return vec2(x, y);
}
void main(){
    vec2 modelPos = aPos * uImgScale + uImgOffset;
    vec2 ndc = world_to_ndc(modelPos);
    gl_Position = vec4(ndc, 0.0, 1.0);
    vUV = (uFlipV==1) ? vec2(aUV.x, 1.0 - aUV.y) : aUV;
}
"""

IMG_FRAG = r"""
#version 330 core
in vec2 vUV;
out vec4 FragColor;
uniform sampler2D uTex;
void main(){
    FragColor = texture(uTex, vUV);
}
"""

FLAT_VERT = r"""
#version 330 core
layout(location=0) in vec2 aPos;  // already in NDC
void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }
"""

FLAT_FRAG = r"""
#version 330 core
uniform vec4 uColor;
out vec4 FragColor;
void main(){ FragColor = uColor; }
"""

# ===========================
# Renderer
# ===========================
class Renderer:
    def __init__(self):
        self.img_prog = None
        self.flat_prog = None
        self.vao = None
        self.vbo = None
        self.uvbo = None
        self.tex = 0
        self.img_w = 0; self.img_h = 0

    @staticmethod
    def _compile(src, typ):
        sh = gl.glCreateShader(typ)
        gl.glShaderSource(sh, src)
        gl.glCompileShader(sh)
        if not gl.glGetShaderiv(sh, gl.GL_COMPILE_STATUS):
            raise RuntimeError(gl.glGetShaderInfoLog(sh).decode())
        return sh

    def init_gl(self):
        vs = self._compile(IMG_VERT, gl.GL_VERTEX_SHADER)
        fs = self._compile(IMG_FRAG, gl.GL_FRAGMENT_SHADER)
        self.img_prog = gl.glCreateProgram()
        gl.glAttachShader(self.img_prog, vs)
        gl.glAttachShader(self.img_prog, fs)
        gl.glLinkProgram(self.img_prog)
        if not gl.glGetProgramiv(self.img_prog, gl.GL_LINK_STATUS):
            raise RuntimeError(gl.glGetProgramInfoLog(self.img_prog).decode())
        gl.glDeleteShader(vs); gl.glDeleteShader(fs)

        fvs = self._compile(FLAT_VERT, gl.GL_VERTEX_SHADER)
        ffs = self._compile(FLAT_FRAG, gl.GL_FRAGMENT_SHADER)
        self.flat_prog = gl.glCreateProgram()
        gl.glAttachShader(self.flat_prog, fvs)
        gl.glAttachShader(self.flat_prog, ffs)
        gl.glLinkProgram(self.flat_prog)
        if not gl.glGetProgramiv(self.flat_prog, gl.GL_LINK_STATUS):
            raise RuntimeError(gl.glGetProgramInfoLog(self.flat_prog).decode())
        gl.glDeleteShader(fvs); gl.glDeleteShader(ffs)

        self.vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(self.vao)
        self.vbo = gl.glGenBuffers(1)
        self.uvbo = gl.glGenBuffers(1)
        gl.glEnableVertexAttribArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, False, 0, None)
        gl.glEnableVertexAttribArray(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.uvbo)
        gl.glVertexAttribPointer(1, 2, gl.GL_FLOAT, False, 0, None)
        gl.glBindVertexArray(0)

        self.tex = gl.glGenTextures(1)

    def upload_image(self, pil_img:Image.Image):
        img = pil_img.convert("RGBA")
        self.img_w, self.img_h = img.size
        data = img.tobytes("raw", "RGBA", 0, -1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8,
                        self.img_w, self.img_h, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)

    def draw_image(self, cam:Camera2D, img_offset:Vec2, img_scale:float):
        if self.img_w == 0: return
        gl.glUseProgram(self.img_prog)
        L=-self.img_w*0.5; R=self.img_w*0.5
        B=-self.img_h*0.5; T=self.img_h*0.5
        pos = [L,B, R,B, R,T, L,T]
        uv  = [0,0,  1,0,  1,1,  0,1]

        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, len(pos)*4, (gl.GLfloat*len(pos))(*pos), gl.GL_DYNAMIC_DRAW)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.uvbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, len(uv)*4, (gl.GLfloat*len(uv))(*uv), gl.GL_DYNAMIC_DRAW)

        gl.glUniform2f(gl.glGetUniformLocation(self.img_prog, "uCenter"), cam.center.x, cam.center.y)
        gl.glUniform1f(gl.glGetUniformLocation(self.img_prog, "uScale"), cam.scale)
        gl.glUniform2f(gl.glGetUniformLocation(self.img_prog, "uViewport"), cam.vw, cam.vh)
        gl.glUniform1i(gl.glGetUniformLocation(self.img_prog, "uFlipV"), 0)
        gl.glUniform2f(gl.glGetUniformLocation(self.img_prog, "uImgOffset"), img_offset.x, img_offset.y)
        gl.glUniform1f(gl.glGetUniformLocation(self.img_prog, "uImgScale"), img_scale)

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glUniform1i(gl.glGetUniformLocation(self.img_prog, "uTex"), 0)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    def draw_overlay(self, cam:Camera2D, crop:CropBox, is_faded_out: bool=False):
        """根据 is_faded_out:
           - True: 遮罩 α=1.0，且不绘制边框与手柄；
           - False: 遮罩 α=0.55，正常绘制边框与手柄。
        """
        def px2ndc(x,y): return (2*x/cam.vw - 1, 1 - 2*y/cam.vh)
        c = [cam.world_to_screen(v) for v in crop.rect.as_corners()]  # LB, RB, RT, LT
        Lp, Bp, Rp, Tp = c[0].x, c[0].y, c[2].x, c[2].y

        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glUseProgram(self.flat_prog)

        overlay_alpha = 1.0 if is_faded_out else 0.55
        gl.glUniform4f(gl.glGetUniformLocation(self.flat_prog, "uColor"), 0, 0, 0, overlay_alpha)

        quads = []
        quads.append([px2ndc(0,0), px2ndc(cam.vw,0), px2ndc(cam.vw,Tp), px2ndc(0,Tp)])
        quads.append([px2ndc(0,Bp), px2ndc(cam.vw,Bp), px2ndc(cam.vw,cam.vh), px2ndc(0,cam.vh)])
        quads.append([px2ndc(0,Tp), px2ndc(Lp,Tp), px2ndc(Lp,Bp), px2ndc(0,Bp)])
        quads.append([px2ndc(Rp,Tp), px2ndc(cam.vw,Tp), px2ndc(cam.vw,Bp), px2ndc(Rp,Bp)])

        for q in quads:
            v = []
            for (x,y) in q: v += [x,y]
            vbo = gl.glGenBuffers(1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, len(v)*4, (gl.GLfloat*len(v))(*v), gl.GL_DYNAMIC_DRAW)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, False, 0, None)
            gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
            gl.glDisableVertexAttribArray(0)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            gl.glDeleteBuffers(1, [vbo])

        # 如果隐藏，就不画边框与手柄，直接返回
        if is_faded_out:
            gl.glUseProgram(0)
            gl.glDisable(gl.GL_BLEND)
            return

        # 边框
        gl.glUniform4f(gl.glGetUniformLocation(self.flat_prog, "uColor"), 1.0, 0.85, 0.2, 1.0)
        poly = [c[0], c[1], c[2], c[3], c[0]]
        v = []
        for p in poly:
            x,y = px2ndc(p.x, p.y); v += [x,y]
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, len(v)*4, (gl.GLfloat*len(v))(*v), gl.GL_DYNAMIC_DRAW)
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, False, 0, None)
        # Core Profile 跨平台可用的线宽安全值
        gl.glLineWidth(1.0)
        gl.glDrawArrays(gl.GL_LINE_STRIP, 0, len(poly))
        gl.glDisableVertexAttribArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glDeleteBuffers(1, [vbo])

        # 手柄
        gl.glUniform4f(gl.glGetUniformLocation(self.flat_prog, "uColor"), 1.0, 0.85, 0.2, 1.0)
        size_px = 7
        for pt in [c[0], c[1], c[2], c[3]]:
            q = [
                px2ndc(pt.x - size_px, pt.y - size_px),
                px2ndc(pt.x + size_px, pt.y - size_px),
                px2ndc(pt.x + size_px, pt.y + size_px),
                px2ndc(pt.x - size_px, pt.y + size_px),
            ]
            vv = []
            for (x,y) in q: vv += [x,y]
            hb = gl.glGenBuffers(1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, hb)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, len(vv)*4, (gl.GLfloat*len(vv))(*vv), gl.GL_DYNAMIC_DRAW)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, False, 0, None)
            gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
            gl.glDisableVertexAttribArray(0)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            gl.glDeleteBuffers(1, [hb])

        gl.glUseProgram(0)
        gl.glDisable(gl.GL_BLEND)

# ===========================
# GL Viewport + Interaction
# ===========================
def ease_out_cubic(t: float) -> float:
    return 1 - (1 - t)**3

def ease_in_quad(t: float) -> float:
    return t * t

def cursor_for_handle(h: int) -> Qt.CursorShape:
    return {
        Handle.L: Qt.SizeHorCursor,
        Handle.R: Qt.SizeHorCursor,
        Handle.T: Qt.SizeVerCursor,
        Handle.B: Qt.SizeVerCursor,
        Handle.LT: Qt.SizeFDiagCursor,
        Handle.RB: Qt.SizeFDiagCursor,
        Handle.RT: Qt.SizeBDiagCursor,
        Handle.LB: Qt.SizeBDiagCursor,
        Handle.INSIDE: Qt.OpenHandCursor,
        Handle.NONE: Qt.ArrowCursor
    }.get(h, Qt.ArrowCursor)

class GLViewport(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.r = Renderer()
        self.cam = Camera2D()
        self.crop = CropBox()

        self.img_offset = Vec2(0, 0)  # world
        self.img_scale  = 1.0
        self._img_scale_clamp = (0.02, 40.0)

        self._drag_state = 0   # 0 idle, 1 drag-edge, 2 drag-image
        self._drag_handle = Handle.NONE
        self._last_mouse_px = Vec2(0,0)

        self._edge_threshold_px = 48

        self._idle_timer = QTimer(self); self._idle_timer.setInterval(1000)
        self._idle_timer.timeout.connect(self._auto_fit_start)

        self._anim_timer = QTimer(self); self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._on_anim_tick)
        self._anim_active = False; self._anim_t0 = 0.0; self._anim_dur = 0.30
        self._anim_start_center = Vec2(0,0); self._anim_start_scale = 1.0
        self._anim_end_center = Vec2(0,0);   self._anim_end_scale  = 1.0

        # ⭐️ 新增：空闲隐藏状态
        self._is_faded_out = False

    # ---------- GL ----------
    def initializeGL(self):
        gl.glDisable(gl.GL_DEPTH_TEST)
        self.r.init_gl()

    def resizeGL(self, w:int, h:int):
        gl.glViewport(0,0,w,h)
        self.cam.set_viewport(w,h)

    def paintGL(self):
        gl.glClearColor(0.08,0.08,0.08,1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        self.r.draw_image(self.cam, self.img_offset, self.img_scale)
        if self.r.img_w > 0:
            # 传递隐藏状态
            self.r.draw_overlay(self.cam, self.crop, self._is_faded_out)

    # ---------- Helpers ----------
    def _current_image_bounds_world(self) -> Rect:
        iw, ih = self.r.img_w, self.r.img_h
        s = self.img_scale
        return Rect(self.img_offset.x, self.img_offset.y, iw*s, ih*s)

    def _clamp_offset_to_cover_crop(self, offset:Vec2, scale:float) -> Vec2:
        c = self.crop.rect
        iw, ih = self.r.img_w, self.r.img_h
        hw = (iw*scale) * 0.5
        hh = (ih*scale) * 0.5
        min_ox = c.r() - hw
        max_ox = c.l() + hw
        min_oy = c.t() - hh
        max_oy = c.b() + hh
        return Vec2(
            max(min_ox, min(max_ox, offset.x)),
            max(min_oy, min(max_oy, offset.y))
        )

    def _dynamic_min_scale_to_cover_crop(self) -> float:
        if self.r.img_w <= 0 or self.r.img_h <= 0: return 0.0
        c = self.crop.rect
        return max(c.w / self.r.img_w, c.h / self.r.img_h)

    def _crop_edges_screen(self) -> Tuple[float, float, float, float]:
        r = self.crop.rect
        L_px = self.cam.world_to_screen(Vec2(r.l(), r.cy)).x
        R_px = self.cam.world_to_screen(Vec2(r.r(), r.cy)).x
        T_px = self.cam.world_to_screen(Vec2(r.cx, r.t())).y
        B_px = self.cam.world_to_screen(Vec2(r.cx, r.b())).y
        return L_px, R_px, T_px, B_px

    # ---------- Auto-fit ----------
    def _restart_idle(self):
        self._idle_timer.start()

    def _stop_idle(self):
        self._idle_timer.stop()

    def _stop_anim(self):
        self._anim_active=False; self._anim_timer.stop()

    def _auto_fit_start(self):
        self._idle_timer.stop()
        if self.r.img_w == 0: return
        target_center, target_scale = self.cam.fit_rect(self.crop.rect, padding_px=20)
        self._anim_active = True
        self._anim_t0 = time.perf_counter()
        self._anim_start_center = self.cam.center
        self._anim_start_scale  = self.cam.scale
        self._anim_end_center   = target_center
        self._anim_end_scale    = target_scale
        # 动画进行中不隐藏
        self._is_faded_out = False
        self._anim_timer.start()

    def _on_anim_tick(self):
        if not self._anim_active: return
        t = (time.perf_counter() - self._anim_t0) / self._anim_dur
        if t >= 1.0:
            self.cam.center = self._anim_end_center
            self.cam.scale  = self._anim_end_scale
            self._stop_anim()
            # 动画结束 → 进入隐藏状态
            self._is_faded_out = True
            self.update(); return
        u = ease_out_cubic(max(0.0, min(1.0, t)))
        cx = self._anim_start_center.x + (self._anim_end_center.x - self._anim_start_center.x)*u
        cy = self._anim_start_center.y + (self._anim_end_center.y - self._anim_start_center.y)*u
        sc = self._anim_start_scale * (self._anim_end_scale / self._anim_start_scale) ** u
        self.cam.center = Vec2(cx, cy)
        self.cam.scale  = sc
        self.update()

    # ---------- Auto zoom-out while pushing edges (with eased pressure + pan) ----------
    def _auto_shrink_on_drag(self, dpx: Tuple[float, float]):
        """拖边贴窗时：按压力缩小，并沿压力相反方向叠加平移；保持无黑边。"""
        if self.r.img_w == 0: return
        L_px, R_px, T_px, B_px = self._crop_edges_screen()
        vw, vh = self.cam.vw, self.cam.vh
        thr = float(self._edge_threshold_px)

        pressure = 0.0
        d_offset = Vec2(0, 0)                 # 额外世界平移 (给图片和裁剪框)
        d_world  = self.cam.screen_vec_to_world_vec(dpx)  # 鼠标一步对应的世界向量

        # 左边向外推（dx<0）：向右平移（+x），带入左侧内容
        if self._drag_handle in (Handle.L, Handle.LT, Handle.LB) and dpx[0] < 0:
            margin = L_px
            if margin < thr:
                p = (thr - margin)/thr
                pressure = max(pressure, p)
                d_offset.x = max(d_offset.x, d_world.x * -p)
        # 右边向外推（dx>0）：向左平移（-x）
        if self._drag_handle in (Handle.R, Handle.RT, Handle.RB) and dpx[0] > 0:
            margin = vw - R_px
            if margin < thr:
                p = (thr - margin)/thr
                pressure = max(pressure, p)
                d_offset.x = min(d_offset.x, d_world.x * -p)

        # 上边向外推（dy<0）：向下平移（-y）
        if self._drag_handle in (Handle.T, Handle.LT, Handle.RT) and dpx[1] < 0:
            margin = T_px
            if margin < thr:
                p = (thr - margin)/thr
                pressure = max(pressure, p)
                d_offset.y = min(d_offset.y, d_world.y * -p)
        # 下边向外推（dy>0）：向上平移（+y）
        if self._drag_handle in (Handle.B, Handle.LB, Handle.RB) and dpx[1] > 0:
            margin = vh - B_px
            if margin < thr:
                p = (thr - margin)/thr
                pressure = max(pressure, p)
                d_offset.y = max(d_offset.y, d_world.y * -p)

        if pressure <= 0.0:
            return

        eased_pressure = ease_in_quad(min(1.0, pressure))

        dyn_min = self._dynamic_min_scale_to_cover_crop()
        min_allowed = max(self._img_scale_clamp[0], dyn_min)
        max_allowed = self._img_scale_clamp[1]

        k_max = 0.05  # 单次事件最大缩小比例
        factor = 1.0 - k_max * eased_pressure
        new_scale_raw = self.img_scale * factor
        new_scale = max(min_allowed, min(max_allowed, new_scale_raw))

        # 1. 缩放围绕裁剪框中心进行（稳定）
        anchor = Vec2(self.crop.rect.cx, self.crop.rect.cy)
        s = new_scale / max(1e-12, self.img_scale)
        new_offset = Vec2(
            anchor.x + (self.img_offset.x - anchor.x) * s,
            anchor.y + (self.img_offset.y - anchor.y) * s
        )

        # 2. 叠加“反压力方向”的平移
        pan_gain = 0.75 + 0.25 * eased_pressure
        final_d_offset = d_offset * pan_gain

        new_offset = new_offset + final_d_offset

        # 3. 核心：裁剪框同步平移，保持相对位置不变
        self.crop.rect.cx += final_d_offset.x
        self.crop.rect.cy += final_d_offset.y

        # 4. 夹紧，确保无黑边
        new_offset = self._clamp_offset_to_cover_crop(new_offset, new_scale)

        self.img_scale = new_scale
        self.img_offset = new_offset

    # ---------- Interaction ----------
    def mousePressEvent(self, ev):
        if self.r.img_w == 0: return
        # 任何交互都取消隐藏 / 停止动画与空闲
        self._is_faded_out = False
        self._stop_idle(); self._stop_anim()
        p = Vec2(ev.position().x(), ev.position().y())
        self._last_mouse_px = p
        h = self.crop.hit_test(p, self.cam)
        if h == Handle.INSIDE:
            self._drag_state = 2; self._drag_handle = Handle.INSIDE
            self.setCursor(Qt.ClosedHandCursor)
        elif h != Handle.NONE:
            self._drag_state = 1; self._drag_handle = h
            self.setCursor(cursor_for_handle(h))
        else:
            self._drag_state = 0; self._drag_handle = Handle.NONE
            self.setCursor(Qt.ArrowCursor)
        self.update()

    def mouseMoveEvent(self, ev):
        if self.r.img_w == 0: return
        # 悬停或拖动：若处于隐藏则立刻恢复
        if self._is_faded_out:
            self._is_faded_out = False
            self.update()
        # 移动即打断动画
        self._stop_anim()

        p = Vec2(ev.position().x(), ev.position().y())
        if self._drag_state == 0:
            h = self.crop.hit_test(p, self.cam)
            self.setCursor(cursor_for_handle(h))
        elif self._drag_state == 2:  # 拖图片（平移）
            dpx = (p.x - self._last_mouse_px.x, p.y - self._last_mouse_px.y)
            dw  = self.cam.screen_vec_to_world_vec(dpx)
            tentative = self.img_offset + dw
            self.img_offset = self._clamp_offset_to_cover_crop(tentative, self.img_scale)
            self.update(); self._restart_idle()
        elif self._drag_state == 1:  # 拖边/角（裁剪）
            dpx = (p.x - self._last_mouse_px.x, p.y - self._last_mouse_px.y)
            dw  = self.cam.screen_vec_to_world_vec(dpx)
            img_bounds = self._current_image_bounds_world()
            self.crop.drag_edge(self._drag_handle, dw, img_bounds, lock_aspect=None)
            self._auto_shrink_on_drag(dpx)
            self.update(); self._restart_idle()

        self._last_mouse_px = p

    def mouseReleaseEvent(self, ev):
        self._drag_state = 0
        self._drag_handle = Handle.NONE
        self.setCursor(Qt.ArrowCursor)
        self._restart_idle()

    def wheelEvent(self, ev):
        if self.r.img_w == 0: return
        # 滚轮交互同样取消隐藏
        self._is_faded_out = False
        self._stop_idle(); self._stop_anim()

        steps = ev.angleDelta().y()
        factor = math.pow(1.0015, steps)

        dyn_min = self._dynamic_min_scale_to_cover_crop()
        min_allowed = max(self._img_scale_clamp[0], dyn_min)
        max_allowed = self._img_scale_clamp[1]

        new_scale_raw = self.img_scale * factor
        new_scale = max(min_allowed, min(max_allowed, new_scale_raw))

        anchor = self.cam.screen_to_world(ev.position().x(), ev.position().y())
        s = new_scale / max(1e-12, self.img_scale)
        new_offset = Vec2(
            anchor.x + (self.img_offset.x - anchor.x) * s,
            anchor.y + (self.img_offset.y - anchor.y) * s
        )
        new_offset = self._clamp_offset_to_cover_crop(new_offset, new_scale)

        self.img_scale = new_scale
        self.img_offset = new_offset
        self.update(); self._restart_idle()

    # ---------- API ----------
    def open_image(self, path: str):
        img = Image.open(path)
        self.r.upload_image(img)

        self.img_offset = Vec2(0, 0)
        self.img_scale  = 1.0
        self.crop.set_to_image_bounds(self.r.img_w, self.r.img_h)
        center, scale = self.cam.fit_rect(self.crop.rect, padding_px=20)
        self.cam.center, self.cam.scale = center, scale
        # 打开新图时不隐藏
        self._is_faded_out = False
        self.update()

# ============================
# Main Window
# ============================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenGL Cropper (No Black Bars + Idle Fade-Out)")
        self.resize(1100, 700)

        self.view = GLViewport()
        container = QWidget(); lay = QVBoxLayout(container)
        lay.setContentsMargins(0,0,0,0); lay.addWidget(self.view)
        self.setCentralWidget(container)

        m = self.menuBar().addMenu("文件")
        act_open = QAction("打开...", self); act_open.triggered.connect(self._on_open)
        m.addAction(act_open)

    def _on_open(self):
        fn, _ = QFileDialog.getOpenFileName(self, "选择图片", "", "Images (*.png *.jpg *.jpeg *.bmp)")
        if fn:
            self.view.open_image(fn)

# ============================
# Entrypoint
# ============================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow(); win.show()
    sys.exit(app.exec())
