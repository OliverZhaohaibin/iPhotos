

# `agent.md` – LexiPhoto 开发基础原则

## 1. 总体理念

* **相册=文件夹**：任何文件夹都可能是一个相册；不依赖数据库。
* **原始文件不可变**：**禁止直接改动照片/视频**（重命名、剪裁、写入 EXIF 等），除非用户明确开启“整理/修复”模式。
* **人类决策写 manifest**：封面、精选、排序、标签等信息一律写到 `manifest.json` 等旁车文件中。
* **缓存可丢弃**：缩略图、索引（index.jsonl）、配对结果（links.json）等文件随时可删，软件要能自动重建。
* **Live Photo 配对**：基于 `content.identifier` 强配优先，弱配（同名/时间邻近）次之；结果写入 `links.json`。

---

## 2. 文件与目录约定

* **标志文件**

  * `.lexi.album.json`：完整 manifest（推荐）
  * `.lexi.album`：最小标志（空文件，代表“这是一个相册”）

* **隐藏工作目录**（可删）：

  ```
  /<Album>/.lexiphoto/
    manifest.json      # 可选 manifest 位置
    index.jsonl        # 扫描索引
    links.json         # Live 配对与逻辑组
    featured.json      # 精选 UI 卡片
    thumbs/            # 缩略图缓存
    manifest.bak/      # 历史备份
    locks/             # 并发锁
  ```

* **原始照片/视频**

  * 保持在相册目录下，不移动不改名。
  * 支持 HEIC/JPEG/PNG/MOV/MP4 等。

---

## 3. 数据与 Schema

* **Manifest (`album`)**：权威数据源，必须符合 `schemas/album.schema.json`。
* **Index (`index.jsonl`)**：一行一个资产快照；删掉可重建。
* **Links (`links.json`)**：Live Photo 配对缓存；删掉可重建。
* **Featured (`featured.json`)**：精选照片 UI 布局（裁剪框、标题等），可选。

---

## 4. 编码规则

* **目录结构固定**（见 `src/lexiphoto/…`，模块分为 `models/`, `io/`, `core/`, `cache/`, `utils/`）。
* **数据类**：统一用 `dataclass` 定义（见 `models/types.py`）。
* **错误处理**：必须抛出自定义错误（见 `errors.py`），禁止裸 `Exception`。
* **写文件**：必须原子操作（`*.tmp` → `replace()`），manifest 必须在写前备份到 `.lexiphoto/manifest.bak/`。
* **锁**：写 `manifest/links/index` 前必须检查 `.lexiphoto/locks/`，避免并发冲突。

---

## 5. AI 代码生成原则

* **不要写死路径**：始终通过 `Path` 拼接。
* **不要写死 JSON**：必须用 `jsonschema` 校验；必要时给出默认值。
* **不要隐式改原件**：写入 EXIF/QuickTime 元数据只能在 `repair.py` 内，且必须受 `write_policy.touch_originals=true` 控制。
* **输出必须可运行**：完整函数/类，而不是片段。
* **注释必须清楚**：写明输入、输出、边界条件。
* **跨平台**：Windows/macOS/Linux 都能跑。
* **外部依赖**：只能调用声明在 `pyproject.toml` 的依赖。涉及 ffmpeg/exiftool 时，必须用 wrapper（`utils/ffmpeg.py`、`utils/exiftool.py`）。
* **缓存策略**：索引/缩略图/配对都要检测是否存在并增量更新，不可全量覆盖。

---

## 6. 模块职责

* **models/**：数据类 + manifest/index/links 的加载与保存。
* **io/**：扫描文件系统、读取元数据、生成缩略图、写旁车。
* **core/**：算法逻辑（配对、排序、精选管理）。
* **cache/**：索引与锁的实现。
* **utils/**：通用工具（hash、json、logging、外部工具封装）。
* **schemas/**：JSON Schema。
* **cli.py**：Typer 命令行入口。
* **app.py**：高层门面，协调各模块。

---

## 7. 代码风格

* 遵循 **PEP8**，行宽 100。
* 类型提示必须写全（`Optional[str]`、`list[Path]` 等）。
* 函数命名：动词开头（`scan_album`、`pair_live`）。
* 类命名：首字母大写（`Album`, `IndexStore`）。
* 异常命名：`XxxError`。

---

## 8. 测试与健壮性

* 所有模块必须有 `pytest` 单测。
* 对输入文件缺失/损坏要能报错不崩。
* `index.jsonl`、`links.json` 不存在时必须自动重建。
* 多端同步冲突时按 manifest 的 `conflict.strategy` 处理。

---

## 9. 安全开关

* 默认：

  * 不改原件
  * 不整理目录
  * 不写入 EXIF
* 用户显式允许时：

  * 在 `repair.py` 使用 `exiftool`/`ffmpeg` 写回
  * 必须先生成 `.backup`

---

## 10. 最小命令集

* `lexi init`：初始化相册
* `lexi scan`：生成/更新索引
* `lexi pair`：生成/更新配对
* `lexi cover set`：设置封面
* `lexi feature add/rm`：管理精选
* `lexi report`：输出相册统计与异常

---

## 11. OpenGL 开发规范

### 1. 涉及文件清单

目前工程中涉及 OpenGL 直接调用或 GL 上下文管理的文件如下：

* **核心图像查看器 (Pure GL)**

  * `src/iPhoto/gui/ui/widgets/gl_image_viewer.py`（Widget 宿主与事件处理）
  * `src/iPhoto/gui/ui/widgets/gl_renderer.py`（GL 渲染指令封装）
  * `src/iPhoto/gui/ui/widgets/gl_image_viewer.vert`（Vertex Shader）
  * `src/iPhoto/gui/ui/widgets/gl_image_viewer.frag`（Fragment Shader）

* **地图组件 (GL Backed)**

  * `maps/map_widget/map_gl_widget.py`（继承自 `QOpenGLWidget`，但主要使用 `QPainter` 混合绘制）

---

### 2. GL 版本标准

* **OpenGL 版本**：**3.3 Core Profile**
* **GLSL 版本**：`#version 330 core`
* **Qt 接口**：必须使用 `QOpenGLFunctions_3_3_Core` 调用 API，禁止使用固定管线指令。
* **Surface Format**

```python
fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.CoreProfile)
```

---

### 3. Context 开发规范

#### ✔ 架构分离

* **Widget 层 (`GLImageViewer`)**

  * 负责事件处理（鼠标、键盘、滚轮、Resize）
  * 管理生命周期（`initializeGL / resizeGL / paintGL`）
  * 保证在资源创建/销毁前调用 `makeCurrent()` / `doneCurrent()`

* **Renderer 层 (`GLRenderer`)**

  * 持有所有 GL 资源（Program / VAO / Buffer / Texture）
  * 不依赖 Qt Widget，只负责“发 GL 指令”
  * 禁止在构造函数中创建 GL 资源（必须在 Context 激活后再做）

#### ✔ 资源生命周期

* **创建**

  * 必须在 `initializeGL()` 内执行
  * 或由 Widget 在 `makeCurrent()` 后显式调用 `renderer.initialize()`

* **销毁**

  * 必须在 Context 活跃时删除纹理/VAO/program（Python GC 不可靠）
  * 需要一个显式的 `shutdown()` 或 `destroy_resources()` 方法

* **上下文安全**

  * 所有涉及 GL 的函数都必须“假定有可能 Context 尚未创建”
  * 若 Context 不存在：跳过绘制并打印 warning（不能崩溃）

* **防御性编程**

  * 每个渲染入口前都应检查资源是否初始化：
    `if self._program is None: return`

---

### 4. 坐标系与 Y 轴统一说明

#### ✔ 原则：**逻辑层使用 Top-Left，渲染层在 Shader 中统一 Flip**

* **UI 逻辑坐标系（Python侧）**

  * 原点为左上角 `(0, 0)`
  * Y 轴向下
  * 所有 Crop / Pan / Zoom 操作都在此坐标系下运行
  * `CropBoxState` 存储归一化坐标（0~1）也遵循此体系

* **纹理上传**

  * `QImage` 原始数据直接上传
  * **禁止在 CPU 端做 `mirrored()`**（避免额外遍历 & 复制）

* **Shader 中处理 Flip（统一）**

```glsl
// gl_image_viewer.frag
uv.y = 1.0 - uv.y;
```

这样可确保 GPU 显示的方向与 UI 逻辑坐标一致，不会因为 Qt / OpenGL 的 Y 轴差异引起“倒置 / 上下颠倒 / 拖动反向”等问题。

---


## 12. Python 性能优化规范

### 1. 总体原则

* **性能优先级**：高频调用的图像处理、数组运算、像素级操作必须极致优化
* **优先级顺序**：NumPy 向量化 > Numba JIT > 纯 Python 循环
* **内存效率**：避免不必要的复制，尽量原地修改（in-place operations）
* **测量先行**：优化前必须先测量，避免过早优化

---

### 2. Numba JIT 加速规范

#### ✔ 适用场景

* **像素级循环**：逐像素处理图像数据（如调色、滤镜）
* **复杂数学运算**：无法向量化的分支逻辑或递归
* **小数据集密集计算**：数据量小但计算密集的场景

#### ✔ 使用规范

**必须使用 `@jit` 装饰的场景：**

```python
from numba import jit

@jit(nopython=True, cache=True)
def process_pixels(buffer: np.ndarray, width: int, height: int) -> None:
    """使用 Numba 加速的像素级处理循环。
    
    - nopython=True: 强制纯 JIT 模式，不回退到 Python
    - cache=True: 缓存编译结果，加快后续启动
    """
    for y in range(height):
        for x in range(width):
            # 像素级操作
            pixel_offset = y * width * 4 + x * 4
            buffer[pixel_offset] = process_channel(buffer[pixel_offset])
```

**支持的 Numba 特性：**

* ✅ 数值运算（加减乘除、指数、对数、三角函数）
* ✅ NumPy 数组索引与切片
* ✅ `for` 循环、`while` 循环
* ✅ 条件分支（`if/elif/else`）
* ✅ 数学函数（`math.sin`, `math.exp`, `math.log` 等）
* ✅ 元组返回（`return (r, g, b)`）

**不支持的特性（必须避免）：**

* ❌ 字符串操作
* ❌ Python 对象（`dict`, `list`, 自定义类）
* ❌ 文件 I/O
* ❌ Qt 对象（`QImage.pixelColor()` 等）

#### ✔ 内联小函数

对于高频调用的辅助函数，使用 `inline="always"` 强制内联：

```python
@jit(nopython=True, inline="always")
def clamp(value: float, min_val: float, max_val: float) -> float:
    """小型数学辅助函数必须内联以消除函数调用开销。"""
    if value < min_val:
        return min_val
    if value > max_val:
        return max_val
    return value
```

#### ✔ 实际案例参考

参见工程中的实现：

* `src/iPhoto/core/filters/algorithms.py`：核心算法（纯 Numba，无依赖）
* `src/iPhoto/core/filters/jit_executor.py`：JIT 加速的图像处理执行器

---

### 3. NumPy 向量化规范

#### ✔ 适用场景

* **全图操作**：整幅图像的亮度、对比度、色彩调整
* **数组运算**：可以用广播（broadcasting）表达的操作
* **并行性**：NumPy 自动利用 SIMD 指令和多核

#### ✔ 使用规范

**必须使用 NumPy 向量化的场景：**

```python
import numpy as np

# ❌ 错误：逐像素循环（纯 Python）
for y in range(height):
    for x in range(width):
        rgb[y, x] = rgb[y, x] * brightness

# ✅ 正确：向量化操作（自动并行）
rgb = rgb * brightness
```

**常见向量化操作：**

```python
# 1. 通道归一化
rgb = rgb.astype(np.float32) / 255.0

# 2. 色彩空间转换（RGB → 灰度）
luma = rgb[:, :, 0] * 0.2126 + rgb[:, :, 1] * 0.7152 + rgb[:, :, 2] * 0.0722

# 3. 伽马校正（向量化幂运算）
rgb = np.power(np.clip(rgb, 0.0, 1.0), gamma)

# 4. 条件筛选与混合
mask = luma > 0.5
rgb[mask] = rgb[mask] * 1.2  # 仅处理亮区

# 5. 广播运算（避免循环）
rgb = rgb * gain[None, None, :]  # gain: [r_gain, g_gain, b_gain]
```

#### ✔ 内存优化

**原地修改（in-place）：**

```python
# ❌ 错误：创建新数组（浪费内存）
rgb = np.clip(rgb, 0.0, 1.0)

# ✅ 正确：原地修改（节省内存）
np.clip(rgb, 0.0, 1.0, out=rgb)

# ✅ 复用数组避免分配
np.power(rgb, gamma, out=rgb)
```

**避免不必要的复制：**

```python
# ❌ 错误：触发复制
rgb_copy = rgb.astype(np.float32)
rgb_copy = rgb_copy / 255.0

# ✅ 正确：复用类型转换
rgb = rgb.astype(np.float32, copy=False) / 255.0
```

#### ✔ 实际案例参考

参见工程中的实现：

* `src/iPhoto/core/filters/numpy_executor.py`：黑白效果的 NumPy 向量化实现

---

### 4. 性能分层策略

工程采用**三层回退策略**，保证最大兼容性的同时追求极致性能：

```
┌─────────────────────────────────────────┐
│  1. NumPy 向量化（最快）                │  ← 优先
│     直接操作整个数组，SIMD 加速          │
├─────────────────────────────────────────┤
│  2. Numba JIT（次快）                   │  ← 回退
│     编译为机器码，适合复杂逻辑          │
├─────────────────────────────────────────┤
│  3. QColor 逐像素（最慢，兼容性最强）   │  ← 最终回退
│     纯 Python + Qt API，保证能运行      │
└─────────────────────────────────────────┘
```

**代码模式：**

```python
def apply_filter(image: QImage, params) -> None:
    """应用滤镜（自动选择最优路径）。"""
    
    # 1️⃣ 尝试 NumPy 向量化
    if _try_numpy_path(image, params):
        return
    
    # 2️⃣ 回退到 Numba JIT
    if _try_numba_path(image, params):
        return
    
    # 3️⃣ 最终回退到 QColor 逐像素
    _fallback_qcolor_path(image, params)
```

---

### 5. 代码示例对比

#### ❌ 反面案例：纯 Python 循环

```python
def adjust_brightness_bad(image: QImage, brightness: float) -> None:
    """性能极差：逐像素调用 Qt API，无法优化。"""
    width = image.width()
    height = image.height()
    for y in range(height):
        for x in range(width):
            color = image.pixelColor(x, y)  # 每次调用都有 Python 开销
            r = min(1.0, color.redF() + brightness)
            g = min(1.0, color.greenF() + brightness)
            b = min(1.0, color.blueF() + brightness)
            image.setPixelColor(x, y, QColor.fromRgbF(r, g, b))
```

**问题：**
* 每像素两次 Python ↔ C++ 边界跨越（`pixelColor()` + `setPixelColor()`）
* 无法编译器优化
* 1920x1080 图像需要 ~400 万次函数调用

---

#### ✅ 正面案例 1：NumPy 向量化

```python
def adjust_brightness_numpy(image: QImage, brightness: float) -> bool:
    """最佳性能：向量化操作，无循环。"""
    try:
        # 获取像素缓冲区
        buffer = np.frombuffer(image.bits(), dtype=np.uint8)
        pixels = buffer.reshape((image.height(), image.bytesPerLine()))
        rgb = pixels[:, :image.width() * 4].reshape((image.height(), image.width(), 4))
        
        # 向量化调整（单次操作全图）
        rgb[:, :, :3] = np.clip(
            rgb[:, :, :3].astype(np.float32) + brightness * 255.0,
            0, 255
        ).astype(np.uint8)
        return True
    except Exception:
        return False  # 回退到 Numba 或 QColor
```

**优势：**
* 单次操作处理全图（~10ms for 1920x1080）
* 自动 SIMD 加速
* 内存连续访问，缓存友好

---

#### ✅ 正面案例 2：Numba JIT（处理复杂逻辑）

```python
@jit(nopython=True, cache=True)
def _adjust_with_tone_curve(
    buffer: np.ndarray,
    width: int,
    height: int,
    brightness: float,
    contrast: float
) -> None:
    """Numba 加速：支持分支逻辑的像素级处理。"""
    for y in range(height):
        row_offset = y * width * 4
        for x in range(width):
            pixel_offset = row_offset + x * 4
            r = buffer[pixel_offset + 2] / 255.0
            g = buffer[pixel_offset + 1] / 255.0
            b = buffer[pixel_offset] / 255.0
            
            # 复杂的色调曲线（无法简单向量化）
            r = _apply_tone_curve(r, brightness, contrast)
            g = _apply_tone_curve(g, brightness, contrast)
            b = _apply_tone_curve(b, brightness, contrast)
            
            buffer[pixel_offset + 2] = int(min(255, max(0, r * 255.0)))
            buffer[pixel_offset + 1] = int(min(255, max(0, g * 255.0)))
            buffer[pixel_offset] = int(min(255, max(0, b * 255.0)))

@jit(nopython=True, inline="always")
def _apply_tone_curve(value: float, brightness: float, contrast: float) -> float:
    """辅助函数：复杂的色调曲线调整。"""
    adjusted = value + brightness
    if adjusted > 0.65:
        adjusted += (adjusted - 0.65) * contrast
    elif adjusted < 0.35:
        adjusted -= (0.35 - adjusted) * contrast
    return max(0.0, min(1.0, adjusted))
```

**优势：**
* 编译为机器码（~50ms for 1920x1080，比纯 Python 快 50-100 倍）
* 支持复杂分支逻辑
* 自动类型推断和优化

---

### 6. 性能测量与验证

**必须在优化前后测量：**

```python
import time

def benchmark_filter(image: QImage, iterations: int = 10) -> float:
    """测量滤镜平均执行时间（毫秒）。"""
    times = []
    for _ in range(iterations):
        img_copy = image.copy()
        start = time.perf_counter()
        apply_filter(img_copy, params)
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    return sum(times) / len(times)

# 对比三种实现
print(f"QColor 回退: {benchmark_filter(image, 'qcolor'):.1f} ms")
print(f"Numba JIT:  {benchmark_filter(image, 'numba'):.1f} ms")
print(f"NumPy 向量: {benchmark_filter(image, 'numpy'):.1f} ms")
```

**预期性能比（1920x1080 图像）：**

| 方法 | 典型耗时 | 性能比 |
|------|---------|--------|
| QColor 逐像素 | 5000 ms | 1× (基准) |
| Numba JIT | 50 ms | 100× |
| NumPy 向量化 | 10 ms | 500× |

---

### 7. 最佳实践清单

#### ✅ DO（必须做）

* 对所有像素级循环使用 `@jit(nopython=True, cache=True)`
* 对全图数组操作优先使用 NumPy 向量化
* 在 Numba 函数中使用小的内联辅助函数（`inline="always"`）
* 使用 `np.clip(..., out=array)` 原地修改以节省内存
* 提供分层回退策略（NumPy → Numba → QColor）
* 在生产环境前对性能关键路径进行基准测试

#### ❌ DON'T（禁止做）

* 在 Numba `nopython=True` 模式中使用 Python 对象（`dict`, `list`, 字符串）
* 在 Numba 函数中调用 Qt API（`QImage.pixelColor()` 等）
* 在热点循环中分配大量临时数组（使用 `out=` 参数复用）
* 对小数据集（< 1000 元素）过度优化（编译开销 > 收益）
* 跳过性能测量就提交"优化"代码

---

### 8. 工程实例索引

参考以下文件学习最佳实践：

| 文件 | 功能 | 优化技术 |
|------|------|---------|
| `src/iPhoto/core/filters/algorithms.py` | 纯算法（无依赖） | Numba JIT + 内联函数 |
| `src/iPhoto/core/filters/jit_executor.py` | 图像调整执行器 | Numba 像素级循环 |
| `src/iPhoto/core/filters/numpy_executor.py` | 黑白效果 | NumPy 向量化 |
| `src/iPhoto/core/filters/fallback_executor.py` | 兼容性回退 | QColor 逐像素 |
| `src/iPhoto/core/filters/facade.py` | 统一入口 | 分层策略模式 |

---
