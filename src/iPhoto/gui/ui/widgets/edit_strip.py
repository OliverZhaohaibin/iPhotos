from __future__ import annotations
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPainterPath
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout

class BWSlider(QWidget):
    valueChanged = Signal(float)

    def __init__(self, name: str = "Intensity", parent: QWidget | None = None):
        super().__init__(parent)
        self._name = name
        self._v = 0.5
        self._dragging = False
        self._hover = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        # 尺寸
        self.setMinimumHeight(48)
        self.setMinimumWidth(260)
        self.track_height = 30
        self.radius = 10
        self.h_padding = 14
        self.line_width = 3

        # 颜色
        self.c_left  = QColor(132, 132, 132)
        self.c_right = QColor(54, 54, 54)
        self.c_bg    = QColor(42, 42, 42)
        self.c_line  = QColor(0, 122, 255)
        self.c_text  = QColor(235, 235, 235)

    # -------- API --------
    def value(self) -> float: return self._v
    def setValue(self, v: float, emit=True):
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self._v) > 1e-6:
            self._v = v
            self.update()
            if emit: self.valueChanged.emit(self._v)
    def setName(self, name: str): self._name = name; self.update()

    # -------- Events --------
    def enterEvent(self, _): self._hover=True; self.update()
    def leaveEvent(self, _): self._hover=False; self.update()

    def mousePressEvent(self, e):
        if e.button()==Qt.LeftButton:
            self._dragging=True
            self._set_by_pos(e.position().x())
            self.setCursor(Qt.ClosedHandCursor)
    def mouseMoveEvent(self, e):
        if self._dragging: self._set_by_pos(e.position().x())
    def mouseReleaseEvent(self, e):
        if e.button()==Qt.LeftButton and self._dragging:
            self._dragging=False
            self.unsetCursor()
    def wheelEvent(self, e):
        self.setValue(self._v + (e.angleDelta().y()/120.0)*0.01)
    def keyPressEvent(self, e):
        step=0.01
        if e.key() in (Qt.Key_Left, Qt.Key_A): self.setValue(self._v-step)
        elif e.key() in (Qt.Key_Right, Qt.Key_D): self.setValue(self._v+step)
        else: super().keyPressEvent(e)

    # -------- Paint --------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()

        # 轨道
        track_h = self.track_height
        track_rect = QRectF(self.h_padding, (h-track_h)/2, w-2*self.h_padding, track_h)
        x_line = track_rect.left() + self._v * track_rect.width()

        round_path = QPainterPath()
        round_path.addRoundedRect(track_rect, self.radius, self.radius)

        # 底色
        p.setPen(Qt.NoPen); p.setBrush(self.c_bg)
        p.drawPath(round_path)

        # 左右分色（用 clip 保证靠蓝线边缘为直角）
        p.save()
        p.setClipPath(round_path)
        p.setClipRect(QRectF(track_rect.left(), track_rect.top(),
                             max(0.0, x_line - track_rect.left()), track_h),
                      Qt.IntersectClip)
        p.fillRect(track_rect, self.c_left)
        p.restore()

        p.save()
        p.setClipPath(round_path)
        p.setClipRect(QRectF(x_line, track_rect.top(),
                             max(0.0, track_rect.right() - x_line), track_h),
                      Qt.IntersectClip)
        p.fillRect(track_rect, self.c_right)
        p.restore()

        # 蓝色竖线：clip 到圆角路径，端帽 Flat，防止超出圆角
        p.save()
        p.setClipPath(round_path)
        pen = QPen(self.c_line)
        pen.setWidth(self.line_width)
        pen.setCapStyle(Qt.FlatCap)   # 端帽不外伸
        p.setPen(pen)
        p.drawLine(QPointF(x_line, track_rect.top()),
                   QPointF(x_line, track_rect.bottom()))
        p.restore()

        # 粗体文本直接叠加在条上
        f = QFont(self.font())
        f.setBold(True)               # ✅ 加粗
        p.setFont(f); p.setPen(self.c_text)

        left_rect  = QRectF(track_rect.left()+10, track_rect.top(),
                            track_rect.width()/2-12, track_h)
        right_rect = QRectF(track_rect.center().x(), track_rect.top(),
                            track_rect.width()/2-10, track_h)
        p.drawText(left_rect,  Qt.AlignVCenter | Qt.AlignLeft,  self._name)
        p.drawText(right_rect, Qt.AlignVCenter | Qt.AlignRight, f"{self._v:.2f}")

        if self._hover and not self._dragging:
            self.setCursor(Qt.OpenHandCursor)

    # -------- Util --------
    def _set_by_pos(self, x: float):
        L = self.h_padding
        R = self.width() - self.h_padding
        if R <= L: return
        self.setValue((x - L) / (R - L))

# --- Demo ---
if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    win = QWidget(); lay = QVBoxLayout(win)
    lay.setContentsMargins(16,16,16,16); lay.setSpacing(12)
    for name, v in [("Intensity",0.50), ("Neutrals",0.00), ("Tone",0.00), ("Grain",0.00)]:
        s = BWSlider(name); s.setValue(v, emit=False); lay.addWidget(s)
    win.setWindowTitle("B&W Slider (bold text, clipped thumb)")
    win.resize(560, 240)
    win.show(); sys.exit(app.exec())
