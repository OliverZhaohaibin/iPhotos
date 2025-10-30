

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