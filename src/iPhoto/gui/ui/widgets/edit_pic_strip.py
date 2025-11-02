# demo_drag_drop_slider_barhandle.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, List
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPointF, QSize, Signal, Property, QMimeData
from PySide6.QtGui import (
    QPainter, QPaintEvent, QPixmap, QImage, QPen, QBrush,
    QImageReader, QColor, QPainterPath
)
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QFrame

# ---------- 简单亮度函数 ----------
def adjust_brightness(img: QImage, delta: float) -> QImage:
    if img.isNull():
        return img
    out = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
    k = int(255.0 * delta)
    w, h = img.width(), img.height()
    for y in range(h):
        src = img.constScanLine(y)
        dst = out.scanLine(y)
        for x in range(w):
            b = int(src[x*4 + 0]) + k
            g = int(src[x*4 + 1]) + k
            r = int(src[x*4 + 2]) + k
            a = int(src[x*4 + 3])
            b = max(0, min(255, b))
            g = max(0, min(255, g))
            r = max(0, min(255, r))
            dst[x*4 + 0] = b
            dst[x*4 + 1] = g
            dst[x*4 + 2] = r
            dst[x*4 + 3] = a
    return out


@dataclass
class Tick:
    value: float
    pix: QPixmap


class ThumbnailStripSlider(QFrame):
    valueChanged = Signal(float)
    valueCommitted = Signal(float)

    def __init__(
        self,
        *,
        ticks: int = 7,
        track_height: int = 56,
        corner_radius: float = 8.0,
        preview_fn: Callable[[QImage, float], QImage] = adjust_brightness,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setAcceptDrops(False)
        self.setMouseTracking(True)

        self._ticks = max(3, ticks)
        self._track_h = track_height
        self._corner_radius = corner_radius
        self._preview_fn = preview_fn

        self._min, self._max = -1.0, 1.0
        self._value = 0.0
        self._pressed = False

        self._base_img: QImage | None = None
        self._scaled: QImage | None = None
        self._tick_pix: List[Tick] = []
        self._scaled_h_cached = 0

        self.setMinimumHeight(self._track_h + 24)

    def setImage(self, img: QImage | QPixmap):
        if isinstance(img, QPixmap):
            img = img.toImage()
        if img.isNull():
            return
        self._base_img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self._scaled = None
        self._tick_pix.clear()
        self.update()

    def _trackRect(self) -> QRectF:
        m = 8
        w = self.width() - m * 2
        y = (self.height() - self._track_h) / 2
        return QRectF(m, y, w, self._track_h)

    def _ensureScaled(self, h: int):
        if self._base_img and (self._scaled is None or self._scaled_h_cached != h):
            self._scaled = self._base_img.scaledToHeight(h, Qt.SmoothTransformation)
            self._scaled_h_cached = h

    def _ensureTicks(self, rect: QRectF):
        if not self._base_img:
            return
        self._ensureScaled(int(rect.height()))
        if self._scaled is None or self._tick_pix:
            return
        values = [self._min + i*(self._max-self._min)/(self._ticks-1) for i in range(self._ticks)]
        for v in values:
            img = self._preview_fn(self._scaled, v)
            self._tick_pix.append(Tick(v, QPixmap.fromImage(img)))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._pressed = True
            self._updateValue(e.position().x())
            e.accept()

    def mouseMoveEvent(self, e):
        if self._pressed:
            self._updateValue(e.position().x())
            e.accept()

    def mouseReleaseEvent(self, e):
        if self._pressed and e.button() == Qt.LeftButton:
            self._pressed = False
            self._updateValue(e.position().x())
            self.valueCommitted.emit(self._value)
            e.accept()

    def _updateValue(self, x: float):
        tr = self._trackRect()
        t = (x - tr.left()) / tr.width()
        t = max(0.0, min(1.0, t))
        self._value = self._min + t*(self._max-self._min)
        self.valueChanged.emit(self._value)
        self.update()

    # --------- paintEvent（手柄变竖条） ----------
    def paintEvent(self, ev: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        tr = self._trackRect()
        radius = self._corner_radius
        path = QPainterPath()
        path.addRoundedRect(tr, radius, radius)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(self.palette().base().color().darker(110)))
        p.drawPath(path)

        # 画缩略图
        self._ensureTicks(tr)
        if self._tick_pix:
            seg_w = tr.width()/len(self._tick_pix)
            p.save()
            p.setClipPath(path)
            x = tr.left()
            for tick in self._tick_pix:
                pix = tick.pix
                if not pix.isNull():
                    # inside paintEvent, in the tick loop
                    target = QRectF(x, tr.top(), seg_w, tr.height())

                    # cover 裁切，保证缩略图铺满每段且不变形
                    ar_pix = pix.width() / pix.height()
                    ar_dst = target.width() / target.height()
                    if ar_pix > ar_dst:
                        # 图更“宽”，左右裁掉
                        h = pix.height()
                        w = int(h * ar_dst)
                        sx = (pix.width() - w) // 2
                        srect = QRectF(sx, 0, w, h)
                    else:
                        # 图更“高”，上下裁掉
                        w = pix.width()
                        h = int(w / ar_dst)
                        sy = (pix.height() - h) // 2
                        srect = QRectF(0, sy, w, h)

                    p.drawPixmap(target, pix, srect)  # 关键：带上 sourceRect
                x += seg_w
            p.restore()

        # 中线
        p.setPen(QPen(self.palette().base().color().lighter(160), 1.0))
        midx = tr.center().x()
        p.drawLine(QPointF(midx, tr.top()), QPointF(midx, tr.bottom()))

        # -------- 手柄：竖条样式 --------
        t = (self._value - self._min) / (self._max - self._min)
        cx = tr.left() + t * tr.width()
        handle_w = 4
        handle_h = tr.height() + 10  # 超出轨道一点点
        handle_rect = QRectF(cx - handle_w/2, tr.center().y() - handle_h/2, handle_w, handle_h)

        # 绘制柔和竖条
        grad_color = QColor(self.palette().highlight().color())
        grad_color.setAlpha(220)
        p.setBrush(grad_color)
        p.setPen(QPen(Qt.white, 1.0))
        p.drawRoundedRect(handle_rect, 2, 2)

# ---------------- 拖入图片窗口 ----------------
class DragDropWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("拖入图片 → Apple Photos 风格预览条")
        self.setAcceptDrops(True)

        self.hint = QLabel("把图片拖进来 👇")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet("QLabel { border: 2px dashed #888; padding: 24px; color: #666; }")

        self.slider = ThumbnailStripSlider()
        self.slider.valueChanged.connect(lambda v: self.value_lbl.setText(f"value = {v:+.2f}"))
        self.value_lbl = QLabel("value = +0.00")
        self.value_lbl.setAlignment(Qt.AlignCenter)
        self.value_lbl.setStyleSheet("color:#888;")

        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addWidget(self.slider)
        lay.addWidget(self.value_lbl)
        self.resize(820, 260)

    def dragEnterEvent(self, e):
        if self._is_image_mime(e.mimeData()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if not urls: return
        p = Path(urls[0].toLocalFile())
        img = QImageReader(str(p)).read()
        if img.isNull():
            self.hint.setText("❌ 无法读取图片")
            return
        self.slider.setImage(img)
        self.hint.setText(f"已载入：{p.name}")

    @staticmethod
    def _is_image_mime(md: QMimeData):
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in {
                    ".jpg",".jpeg",".png",".bmp",".tif",".tiff",".webp"}:
                    return True
        return False


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = DragDropWindow()
    w.show()
    sys.exit(app.exec())
