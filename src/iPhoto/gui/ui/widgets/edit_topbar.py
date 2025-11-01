from __future__ import annotations
from typing import List
from math import floor
from PySide6.QtCore import Qt, QRectF, QPointF, QEasingCurve, Property, QPropertyAnimation, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QLinearGradient, QPainterPath
from PySide6.QtWidgets import QWidget, QApplication, QVBoxLayout


def _snap05(x: float) -> float:
    return floor(x) + 0.5


def _align_rect_05(r: QRectF) -> QRectF:
    x = _snap05(r.x()); y = _snap05(r.y())
    w = round(r.width()); h = round(r.height())
    return QRectF(x, y, w, h)


class SegmentedTopBar(QWidget):
    currentIndexChanged = Signal(int)

    def __init__(self, items: List[str] = None, parent=None):
        super().__init__(parent)
        self._items = items or ["Adjust", "Filters", "Crop"]
        self._index = 0
        self._anim_pos = float(self._index)
        self._anim = QPropertyAnimation(self, b"animPos", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        # 样式
        self.h_pad = 10
        self.v_pad = 6
        self.radius = 12                  # 外框圆角
        self.sep_inset = 6
        self.sep_width = 1.0
        self.height_hint = 36

        self.bg = QColor(48, 48, 48)
        self.border = QColor(70, 70, 70)
        self.text_active = QColor(250, 250, 250)
        self.text_inactive = QColor(180, 180, 180)
        self.frosty_a = QColor(255, 255, 255, 42)    # 选中主体
        self.frosty_b = QColor(255, 255, 255, 18)    # 顶部轻高光

        self.setMinimumHeight(self.height_hint)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    # ---------- API ----------
    def items(self) -> List[str]: return list(self._items)

    def setItems(self, items: List[str]):
        self._items = list(items) or ["Item"]
        self._index = max(0, min(self._index, len(self._items) - 1))
        self._anim_pos = float(self._index)
        self.update()

    def currentIndex(self) -> int: return self._index

    def setCurrentIndex(self, i: int, animate: bool = True):
        i = max(0, min(i, len(self._items) - 1))
        if i == self._index: return
        src, dst = float(self._index), float(i)
        self._index = i
        if animate:
            self._anim.stop(); self._anim.setStartValue(src); self._anim.setEndValue(dst); self._anim.start()
        else:
            self._anim_pos = dst; self.update()
        self.currentIndexChanged.emit(self._index)

    def getAnimPos(self) -> float: return self._anim_pos
    def setAnimPos(self, v: float): self._anim_pos = v; self.update()
    animPos = Property(float, getAnimPos, setAnimPos)

    # ---------- 交互 ----------
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton: return
        idx = self._index_from_x(e.position().x())
        if idx is not None: self.setCurrentIndex(idx, animate=True)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Left, Qt.Key_A): self.setCurrentIndex(self._index-1)
        elif e.key() in (Qt.Key_Right, Qt.Key_D): self.setCurrentIndex(self._index+1)
        else: super().keyPressEvent(e)

    # ---------- 绘制 ----------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        w, h = self.width(), self.height()
        OVERDRAW = max(1.0, self.devicePixelRatioF() * 0.8)

        # 外框（0.5 像素对齐）
        outer = _align_rect_05(QRectF(1.0, 1.0, w - 2.0, h - 2.0))
        outer_path = QPainterPath(); outer_path.addRoundedRect(outer, self.radius, self.radius)

        # 背景
        bg_rect = outer.adjusted(-OVERDRAW, -OVERDRAW, OVERDRAW, OVERDRAW)
        bg_path = QPainterPath()
        bg_path.addRoundedRect(bg_rect, self.radius + OVERDRAW, self.radius + OVERDRAW)
        p.setPen(Qt.NoPen); p.setBrush(self.bg); p.drawPath(bg_path)

        # 槽区域（仅用于计算段落）
        inner = _align_rect_05(outer.adjusted(self.h_pad, self.v_pad, -self.h_pad, -self.v_pad))
        seg_rects, sep_x = self._segment_rects_and_boundaries(inner)

        # ===== 选中背景：圆角矩形，覆盖 outer 边角 =====
        if seg_rects:
            band_rect = self._lerp_rects(seg_rects, self._anim_pos)

            # 若在最左/右，延伸到 outer 的边缘
            leftmost = (self._anim_pos < 0.001)
            rightmost = (self._anim_pos > len(seg_rects)-1.001)
            # 水平覆盖更广一些，确保在动画过渡中圆角仍贴合外框
            band_rect = QRectF(
                band_rect.left()  - (self.h_pad if leftmost else OVERDRAW),
                outer.top() - OVERDRAW,
                band_rect.width() + (2 * OVERDRAW if not (leftmost or rightmost) else self.h_pad + OVERDRAW),
                outer.height() + 2 * OVERDRAW
            )
            band_rect = _align_rect_05(band_rect)

            # 圆角矩形路径
            band_path = QPainterPath()
            band_path.addRoundedRect(band_rect, self.radius, self.radius)

            # 限制在 outer 内部 → 能覆盖两端但不会溢出边框
            sel_path = outer_path.intersected(band_path)

            # 主体
            p.setPen(Qt.NoPen)
            p.setBrush(self.frosty_a)
            p.drawPath(sel_path)

            # 顶部轻高光
            grad = QLinearGradient(band_rect.topLeft(), band_rect.bottomLeft())
            grad.setColorAt(0.0, self.frosty_b)
            grad.setColorAt(0.6, QColor(255, 255, 255, 0))
            p.setBrush(grad)
            p.drawPath(sel_path)

        # 分隔线
        p.setPen(QPen(QColor(90, 90, 90), self.sep_width))
        y1 = _snap05(inner.top() + self.sep_inset)
        y2 = _snap05(inner.bottom() - self.sep_inset)
        for x in sep_x:
            x05 = _snap05(x)
            p.drawLine(QPointF(x05, y1), QPointF(x05, y2))

        # 文本
        for i, r in enumerate(seg_rects):
            f = QFont(self.font()); f.setBold(i == round(self._anim_pos))
            p.setFont(f)
            p.setPen(self.text_active if i == round(self._anim_pos) else self.text_inactive)
            p.drawText(r, Qt.AlignCenter, self._items[i])

        # 最后描边外框
        p.setPen(QPen(self.border, 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(outer_path)

    # ---------- 几何 ----------
    def _segment_rects_and_boundaries(self, inner: QRectF):
        n = len(self._items)
        if n <= 0: return [], []
        w_each = inner.width() / n
        rects, sep_x = [], []
        for i in range(n):
            x = inner.left() + i * w_each
            rects.append(QRectF(x, inner.top(), w_each, inner.height()))
            if i > 0: sep_x.append(x)
        usable = []
        for i, r in enumerate(rects):
            left_bound  = inner.left()  if i == 0   else sep_x[i-1] + self.sep_width/2
            right_bound = inner.right() if i == n-1 else sep_x[i]   - self.sep_width/2
            usable.append(QRectF(left_bound, inner.top(), right_bound - left_bound, inner.height()))
        return usable, sep_x

    def _index_from_x(self, x: float) -> int | None:
        inner = QRectF(self.h_pad, self.v_pad, self.width()-2*self.h_pad, self.height()-2*self.v_pad)
        n = len(self._items)
        if n <= 0: return None
        idx = int((x - inner.left()) // (inner.width() / n))
        return idx if 0 <= idx < n else None

    @staticmethod
    def _lerp(a: float, b: float, t: float) -> float: return a + (b - a) * t

    def _lerp_rects(self, rects: List[QRectF], pos: float) -> QRectF:
        n = len(rects)
        if n == 1 or pos <= 0: return rects[0]
        if pos >= n - 1: return rects[-1]
        i = int(pos); t = pos - i
        r1, r2 = rects[i], rects[i+1]
        x = self._lerp(r1.left(),  r2.left(),  t)
        w = self._lerp(r1.width(), r2.width(), t)
        return QRectF(x, r1.top(), w, r1.height())


# ---------------- Demo ----------------
if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    root = QWidget(); lay = QVBoxLayout(root)
    lay.setContentsMargins(20,20,20,20); lay.setSpacing(16)
    bar = SegmentedTopBar(["Adjust", "Filters", "Crop"]); bar.setFixedHeight(44); lay.addWidget(bar)
    bar2 = SegmentedTopBar(["Basic", "Color", "Details", "Optics"]); bar2.setFixedHeight(44); lay.addWidget(bar2)
    root.setStyleSheet("QWidget { background: #1f1f1f; }")
    root.resize(560, 180); root.setWindowTitle("Segmented Top Bar – round highlight covers outer corners")
    root.show(); sys.exit(app.exec())
