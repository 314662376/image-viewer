#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
光影相册 · Image Viewer
=======================
基于 PySide6 + Pillow 的轻量图片查看器 / 幻灯片播放器。

支持格式（统一由 Pillow 解码，HEIC/HEIF 由 pillow-heif 提供）：
    jpg / jpeg / jpe / jfif / png / webp / gif / bmp / tif / tiff / ico / heic / heif / avif

命令行：
    python image_viewer.py                     # 空白启动，Ctrl+O 打开
    python image_viewer.py D:\\photos            # 直接打开文件夹
    python image_viewer.py D:\\photos\\a.heic     # 打开单张，并载入同目录其它图片
    python image_viewer.py --selftest D:\\photos  # 环境自检：解码并打印报告（验证打包产物用）

快捷键：
    Space 播放/暂停（与按钮同一入口）   ←/→ 上一张/下一张   Home/End 第一张/最后一张
    ↑/↓ 放大/缩小（自动暂停）   0 适应窗口   1 原始大小   +/- 缩放
    鼠标滚轮 缩放（以指针位置为中心）   双击 适应/100%
    F11 全屏（真全屏，只剩画面）   Esc 退出全屏   T 显示/隐藏缩略图   H 显示/隐藏控制条
    Enter 一键投影（全屏 + 随机播放 + 自动开始）   R 随机播放
    Del/Backspace 删除当前图片到回收站
    Ctrl+O 打开图片   Ctrl+Shift+O 选择文件夹   Ctrl+R 包含子文件夹

全屏投影：
    进入全屏后菜单栏、缩略图条、控制条、状态栏全部隐藏，画面占满整屏；播放中
    还会隐藏鼠标指针，一动鼠标就恢复。全屏里按 H 可临时唤出控制条（改间隔用）。

    Enter 是"投影模式"一键入口：真全屏 + 自动打开随机 + 自动开始播放，三者
    顺序固定（先开随机、再全屏、最后起播），收尾只留一条浮层提示；已在全屏时
    再按 Enter 退出，播放与随机状态都保留。F11 仍是纯全屏，不改变播放/随机。

随机播放：
    不是每次 randrange —— 那样会连播同一张、有的图永远轮不到。用"本轮已放"集合
    洗牌：一轮内每张恰好出现一次，放完自动开新一轮；开关状态在按钮/菜单/R 键之间
    由 set_shuffle() 单一写入。

删除：
    Del 走 SHFileOperationW + FOF_ALLOWUNDO，是"移到回收站"而不是物理删除，可在
    资源管理器还原；删除后列表、缩略图、随机池同步前移，播放不中断。
"""

from __future__ import annotations

import ctypes
import datetime
import io
import os
import random
import re
import sys
import tempfile
import time
from ctypes import wintypes
from typing import List, Optional, Sequence, Set, Tuple

from PIL import Image, ImageCms, ImageOps

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------- HEIC 解码器
HEIF_OK = False
HEIF_ERR = ""
try:  # pillow-heif 装上即可直接解码 HEIC/HEIF（Windows 免装系统级解码器）
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_OK = True
except Exception as exc:  # 未安装或缺 libheif
    HEIF_ERR = f"{type(exc).__name__}: {exc}"

# ---------------------------------------------------------------- 常量
MAX_SIDE_STATIC = 4096   # 静态图最长边上限（避免超大图吃爆显存/内存）
MAX_SIDE_ANIM = 1600     # 动图最长边上限（多帧，内存要乘帧数）
MAX_FRAMES = 400         # 动图最多解码帧数
ANIM_PIXEL_BUDGET = 40_000_000  # 动图总像素预算（约 160MB RGBA），超出则截断帧数
THUMB_SIZE = 68          # 缩略图边长
THUMB_LIMIT = 1500       # 单个文件夹最多生成多少张缩略图

ZOOM_MIN = 0.05          # 最小缩放：原图像素的 5%
ZOOM_MAX = 16.0          # 最大缩放：原图像素的 1600%（再大只是糊成色块）
ZOOM_STEP = 1.25         # 方向键 / ＋－按钮 单次缩放倍率

SUPPORTED_EXT = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".pjpeg",
    ".png", ".apng",
    ".webp", ".gif", ".bmp", ".dib",
    ".tif", ".tiff", ".ico",
    ".heic", ".heif", ".avif",
}

DIALOG_FILTER = (
    "所有支持的图片 (*.jpg *.jpeg *.jpe *.jfif *.png *.apng *.webp *.gif "
    "*.bmp *.dib *.tif *.tiff *.ico *.heic *.heif *.avif);;"
    "常见图片 (*.jpg *.jpeg *.png *.webp *.gif *.bmp);;"
    "HEIC/HEIF (*.heic *.heif);;"
    "所有文件 (*.*)"
)


# ---------------------------------------------------------------- 回收站
# 删图必须可还原，所以走 shell 的 SHFileOperationW + FOF_ALLOWUNDO（＝移入回收站），
# 不用 os.remove —— 那玩意儿删掉就没了。
FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def send_to_recycle_bin(path: str) -> Tuple[bool, str]:
    """把单个文件移入回收站（可在资源管理器里还原）。返回 (是否成功, 失败原因)。"""
    if os.name != "nt":
        return False, "当前系统没有回收站"
    path = os.path.abspath(path)
    if len(path) >= 260:
        return False, "路径超过 259 字符，SHFileOperation 无法处理"
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0\0"        # 双 \0 结尾 = 列表结束
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    try:
        ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if ret != 0:
        return False, f"系统返回错误码 0x{ret & 0xFFFF:04X}"
    if op.fAnyOperationsAborted:
        return False, "操作被系统中止"
    if os.path.exists(path):
        return False, "文件仍存在，可能被其它程序占用"
    return True, ""

try:  # Pillow >= 9.1
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover
    RESAMPLE = Image.LANCZOS

STYLE = """
#content        { background: #0f1115; }
#controlBar     { background: #181b22; border-top: 1px solid #262b35; }
QPushButton     { color:#dfe4ec; background:#242a34; border:1px solid #2f3644;
                  border-radius:6px; padding:6px 12px; font-size:13px; }
QPushButton:hover    { background:#2e3644; }
QPushButton:pressed  { background:#39424f; }
QPushButton:disabled { color:#5a616d; background:#1b1f26; border-color:#232831; }
QPushButton#primary  { background:#2f6feb; border-color:#3a78f0; color:#ffffff; font-weight:600; }
QPushButton#primary:hover { background:#3d7cf5; }
QPushButton:checked  { background:#2f6feb; border-color:#3a78f0; color:#ffffff; font-weight:600; }
QPushButton:checked:hover { background:#3d7cf5; }
QSpinBox        { color:#dfe4ec; background:#242a34; border:1px solid #2f3644;
                  border-radius:6px; padding:4px 6px; min-width:54px; selection-background-color:#2f6feb; }
QLabel          { color:#9aa3b2; font-size:13px; }
QLabel#fileName { color:#e6ebf3; }
QListWidget     { background:#141821; border:0; border-top:1px solid #262b35; outline:0; }
QListWidget::item          { border:1px solid transparent; border-radius:4px; }
QListWidget::item:selected { border:1px solid #2f6feb; background:#1d2634; }
QMenuBar        { background:#141821; color:#c9d1de; }
QMenuBar::item:selected { background:#242a34; }
QMenu           { background:#1b202a; color:#dfe4ec; border:1px solid #2f3644; }
QMenu::item:selected { background:#2f6feb; color:#ffffff; }
QStatusBar      { background:#141821; color:#8b93a1; }
QStatusBar::item { border:0; }
QLabel#toast    { background: rgba(18,22,30,0.94); color:#e8eef7; border:1px solid #3d4a5f;
                  border-radius:10px; font-size:13px; }
#dropHint       { background: rgba(47,111,235,0.10); border:3px dashed #4b8bf5; border-radius:10px; }
"""


# ================================================================ 解码层
def pil_to_qimage(im: Image.Image) -> QImage:
    """Pillow Image -> QImage（深拷贝，脱离 Python 缓冲区生命周期）。"""
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    w, h = im.size
    buf = im.tobytes("raw", "RGBA")
    return QImage(buf, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


def _to_srgb(im: Image.Image) -> Image.Image:
    """带 ICC 色彩配置的图片（手机照、AdobeRGB 扫描件）转换到 sRGB，避免偏色。"""
    icc = im.info.get("icc_profile")
    if not icc:
        return im
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        dst = ImageCms.createProfile("sRGB")
        return ImageCms.profileToProfile(im, src, dst, outputMode="RGBA", renderingIntent=1)
    except Exception:
        return im  # 色彩配置损坏时按原样显示


def decode_image(path: str) -> Tuple[List[QImage], List[int]]:
    """
    统一解码入口：返回 (帧列表, 每帧时长ms)。
    静态图只有 1 帧；gif / 动图 webp / apng 返回多帧。
    """
    frames: List[QImage] = []
    durations: List[int] = []

    with Image.open(path) as im:  # 由 Pillow 按文件头嗅探真实格式，不依赖扩展名
        try:
            total = int(getattr(im, "n_frames", 1) or 1)
        except Exception:
            total = 1
        animated = total > 1
        limit = min(total, MAX_FRAMES) if animated else 1
        max_side = MAX_SIDE_ANIM if animated else MAX_SIDE_STATIC
        target: Optional[Tuple[int, int]] = None

        if not animated:
            try:  # JPEG 可在解码阶段直接降采样，避免超大照片全额解码占内存
                im.draft("RGB", (max_side, max_side))
            except Exception:
                pass

        for i in range(MAX_FRAMES):
            if i >= limit:
                break
            if animated:
                im.seek(i)          # 多帧格式：seek 后 Pillow 已完成帧合成/处置
            frame = im.copy()

            if not animated:
                frame = ImageOps.exif_transpose(frame)  # 按 EXIF 方向校正（竖拍照片）
                frame = _to_srgb(frame)

            if frame.mode != "RGBA":
                frame = frame.convert("RGBA")

            w, h = frame.size
            if target is None:
                scale = min(1.0, max_side / max(w, h))
                target = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
                if animated:  # 按首帧尺寸估算内存，超额就少解几帧
                    limit = min(limit, max(1, ANIM_PIXEL_BUDGET // (target[0] * target[1])))
            if frame.size != target:  # 用首帧尺寸统一缩放，防止逐帧抖动
                frame = frame.resize(target, RESAMPLE)

            frames.append(pil_to_qimage(frame))

            dur = 0
            if animated:
                try:
                    dur = int(im.info.get("duration", 0) or 0)
                except Exception:
                    dur = 0
                if dur <= 0:
                    dur = 80        # 部分 GIF 时长缺失，给个兜底
                elif dur < 20:
                    dur = 20        # 浏览器同款下限，避免狂刷帧
            durations.append(dur)

    if not frames:
        raise ValueError("文件里没有可解码的图像帧")
    return frames, durations


def natural_key(name: str):
    """文件名自然排序：img2 < img10。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def is_image_path(path: str) -> bool:
    """按扩展名判断是否是本程序支持的图片（实际解码仍以文件头为准）。"""
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXT


def scan_folder(folder: str, recursive: bool = False) -> List[str]:
    """扫描文件夹内受支持的图片；recursive=True 时连同子文件夹一起扫。"""
    found: List[str] = []
    if not recursive:
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        for n in names:
            full = os.path.join(folder, n)
            if is_image_path(n) and os.path.isfile(full):
                found.append(full)
        found.sort(key=lambda p: natural_key(os.path.basename(p)))
        return found

    for root, dirs, names in os.walk(folder):
        dirs.sort(key=natural_key)
        for n in names:
            full = os.path.join(root, n)
            if is_image_path(n) and os.path.isfile(full):
                found.append(full)
    # 按相对路径排序，保证子目录内的顺序也自然（img2 < img10）
    found.sort(key=lambda p: natural_key(os.path.relpath(p, folder)))
    return found


def has_images_below(folder: str) -> bool:
    """子目录里是否存在图片（用于"根目录为空"时的二次询问）。"""
    for _root, dirs, names in os.walk(folder):
        dirs.sort(key=natural_key)
        for n in names:
            if is_image_path(n):
                return True
    return False


def placeholder_pixmap(text: str, w: int = 1000, h: int = 560) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(QColor("#15181e"))
    p = QPainter(pm)
    p.setPen(QColor("#7b8492"))
    f = QFont()
    f.setPointSize(14)
    p.setFont(f)
    p.drawText(
        pm.rect().adjusted(60, 60, -60, -60),
        int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap),
        text,
    )
    p.end()
    return pm


# ================================================================ 浮层提示
class Toast(QLabel):
    """画布上的轻量浮层提示：鼠标穿透、自动消失，用于"跳过 N 个文件"这类非阻断信息。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.hide()
        self._area = QRect()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_over(self, area: QRect, text: str, ms: int = 3200) -> None:
        self._area = area
        self.setText(text)
        limit = max(260, area.width() - 140)
        br = self.fontMetrics().boundingRect(
            QRect(0, 0, limit, 1200),
            int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignCenter),
            text,
        )
        self.resize(br.width() + 46, br.height() + 24)
        self.reposition()
        self.show()
        self.raise_()
        self._timer.start(ms)

    def reposition(self) -> None:
        if self._area.isNull():
            return
        self.move(
            int(self._area.x() + (self._area.width() - self.width()) / 2),
            int(self._area.y() + 26),
        )


# ================================================================ 后台任务
class DecodeSignals(QObject):
    ok = Signal(int, object, object)   # token, frames, durations
    failed = Signal(int, str)          # token, message


class DecodeTask(QRunnable):
    """在子线程解码，避免 HEIC / 大图阻塞 UI。"""

    def __init__(self, token: int, path: str, signals: DecodeSignals):
        super().__init__()
        self.token = token
        self.path = path
        self.signals = signals

    def run(self) -> None:
        try:
            frames, durations = decode_image(self.path)
        except Exception as exc:
            self.signals.failed.emit(self.token, f"{type(exc).__name__}: {exc}")
            return
        self.signals.ok.emit(self.token, frames, durations)


class ThumbSignals(QObject):
    # 带上 path：删图会让行号整体前移，回来的结果必须再核对"这一行现在还是不是这张图"
    ok = Signal(int, int, str, object)   # generation, row, path, QImage
    failed = Signal(int, int, str)       # generation, row, path


class ThumbTask(QRunnable):
    def __init__(self, gen: int, row: int, path: str, signals: ThumbSignals):
        super().__init__()
        self.gen = gen
        self.row = row
        self.path = path
        self.signals = signals

    def run(self) -> None:
        try:
            with Image.open(self.path) as im:
                try:
                    im.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))  # JPEG 快速降采样
                except Exception:
                    pass
                im = ImageOps.exif_transpose(im)
                im.thumbnail((THUMB_SIZE, THUMB_SIZE), RESAMPLE)
                if im.mode != "RGBA":
                    im = im.convert("RGBA")
                qimg = pil_to_qimage(im)
        except Exception:
            self.signals.failed.emit(self.gen, self.row, self.path)
            return
        self.signals.ok.emit(self.gen, self.row, self.path, qimg)


# ================================================================ 画布
class ImageView(QGraphicsView):
    """带缩放 / 拖拽平移 / 适应窗口的画布。"""

    zoom_changed = Signal(float)      # 当前缩放比例（相对原图像素，1.0 = 100%）
    zoom_requested = Signal(float, QPoint)  # (倍率, 视口锚点) —— 用户主动缩放；QPoint(-1,-1) = 画布中心

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._item)

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QColor("#0f1115"))
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        # 缩放锚点由 zoom_by() 自己按坐标补偿，这里不需要 AnchorUnderMouse
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._fit = True

    # ---------- 对外 ----------
    def zoom(self) -> float:
        return self.transform().m11()

    def set_image(self, pm: QPixmap) -> None:
        self._item.setPixmap(pm)
        self.setSceneRect(self._item.boundingRect())
        self.fit_to_window()

    def update_frame(self, pm: QPixmap) -> None:
        """动图换帧：只换位图，不动当前缩放/位置。"""
        self._item.setPixmap(pm)

    def fit_to_window(self) -> None:
        if self._item.pixmap().isNull():
            return
        self._fit = True
        self.resetTransform()
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        self.zoom_changed.emit(self.zoom())

    def actual_size(self) -> None:
        if self._item.pixmap().isNull():
            return
        self._fit = False
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.resetTransform()
        self.centerOn(self._item)
        self.zoom_changed.emit(self.zoom())

    def fit_scale(self) -> float:
        """当前窗口尺寸下，"适应窗口"对应的缩放比例。"""
        pm = self._item.pixmap()
        if pm.isNull():
            return 1.0
        vp = self.viewport().size()
        iw, ih = max(1, pm.width()), max(1, pm.height())
        return max(1e-4, min(vp.width() / iw, vp.height() / ih))

    def zoom_limits(self) -> Tuple[float, float]:
        """缩放上下限（相对原图像素的比例，1.0 = 100%）。

        常规区间是 5% ~ 1600%。极端尺寸下让步给"适应窗口"比例：
        32×32 的小图在大窗口里适应比例能到 20 倍以上，若不放开上限，
        一滚轮就会从 2000% 猛掉到 1600%，观感很跳。所以取
        min(5%, fit) / max(1600%, fit)，保证适应比例永远在合法区间内。
        """
        fit = self.fit_scale()
        return min(ZOOM_MIN, fit), max(ZOOM_MAX, fit)

    def zoom_by(self, factor: float, anchor_pos: Optional[QPoint] = None) -> bool:
        """按倍率缩放，返回是否真的发生了变化（到顶/到底时为 False）。

        anchor_pos 为视口坐标锚点：缩放前后"该点下面的像素内容"保持不动
        （滚轮传鼠标位置）。不传则以画布中心为锚点（↑↓ / ＋－按钮）。

        这里不用 Qt 自带的 ViewportAnchor.AnchorUnderMouse：它内部依赖
        QGraphicsViewPrivate::lastMouseEvent，只有在收到真实鼠标移动时才会
        刷新，滚轮事件本身不更新它，实测会退回"以中心缩放"而把画面推偏。
        改成自己按滚轮坐标补偿滚动条，锚点精度可验证到 1px 内。
        """
        if self._item.pixmap().isNull() or factor <= 0:
            return False
        cur = self.zoom()
        lo, hi = self.zoom_limits()
        target = min(max(cur * factor, lo), hi)
        step = target / cur
        if abs(step - 1.0) < 1e-4:
            return False

        vp = self.viewport().rect()
        if anchor_pos is None or not vp.contains(anchor_pos):
            anchor_pos = vp.center()

        scene_pt = self.mapToScene(anchor_pos)      # 指针下的内容（缩放前）
        self._fit = False
        old_anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.scale(step, step)
        # 缩放后该内容跑到了哪里，用滚动条把它拉回指针位置（超出内容范围时自动被钳制）
        moved = self.mapFromScene(scene_pt) - anchor_pos
        if not moved.isNull():
            hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
            hbar.setValue(hbar.value() + moved.x())
            vbar.setValue(vbar.value() + moved.y())
        self.setTransformationAnchor(old_anchor)

        self.zoom_changed.emit(self.zoom())
        return True

    # ---------- 事件 ----------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit:
            self.fit_to_window()

    def wheelEvent(self, event) -> None:
        """滚轮缩放：锚点就是滚轮事件自带的鼠标位置，每格约 ±19%。"""
        delta = event.angleDelta().y()
        if delta == 0 or self._item.pixmap().isNull():
            event.ignore()
            return
        self.zoom_requested.emit(1.0015 ** delta, event.position().toPoint())
        event.accept()

    def keyPressEvent(self, event) -> None:
        """↑/↓ 缩放。焦点在画布上时走这里，焦点在别处时走 QAction 快捷键。"""
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.zoom_requested.emit(ZOOM_STEP if key == Qt.Key.Key_Up else 1 / ZOOM_STEP,
                                     QPoint(-1, -1))
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._item.pixmap().isNull():
            return
        if self._fit:
            self.actual_size()
        else:
            self.fit_to_window()
        event.accept()


# ================================================================ 主窗口
class ViewerWindow(QMainWindow):
    def __init__(self, initial: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("光影相册 · Image Viewer")
        self.resize(1280, 820)
        self.setStyleSheet(STYLE)
        self.setAcceptDrops(True)

        # ---- 状态 ----
        self.paths: List[str] = []
        self.index = -1
        self.playing = False
        self.frames: List[QImage] = []
        self.durations: List[int] = []
        self._frame = 0
        self._token = 0          # 每次切换图片自增，用于丢弃过期解码结果
        self._pending = -1
        self._thumb_gen = 0
        self._thumb_rows: List[int] = []
        self._thumbs_on = True   # 用户是否希望显示缩略图（与"有没有图"分开记）
        self._recursive = False  # 扫描文件夹时是否包含子文件夹
        self._chrome_on = True   # 用户是否希望显示菜单栏 + 控制条
        self._fullscreen = False # 真全屏：菜单/缩略图/控制条/状态栏全部隐藏
        self._fs_chrome = False  # 全屏下按 H 临时唤出控制条
        self._prev_state = Qt.WindowState.WindowNoState   # 进全屏前的窗口状态
        self._shuffle = False    # 随机播放
        self._visited: Set[int] = set()   # 本轮随机已放过哪些下标（放完一轮再重置）
        self._rand = random.Random()
        self._cursor_hidden = False

        # ---- 线程池 ----
        self._decode_pool = QThreadPool(self)
        self._decode_pool.setMaxThreadCount(2)
        self._thumb_pool = QThreadPool(self)
        self._thumb_pool.setMaxThreadCount(3)

        self._sig = DecodeSignals()
        self._sig.ok.connect(self._on_decoded)
        self._sig.failed.connect(self._on_decode_failed)
        self._tsig = ThumbSignals()
        self._tsig.ok.connect(self._on_thumb_ready)
        self._tsig.failed.connect(self._on_thumb_failed)

        # ---- 定时器 ----
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._auto_advance)
        self._anim = QTimer(self)
        self._anim.setSingleShot(True)
        self._anim.timeout.connect(self._next_frame)

        self._build_ui()
        self._build_actions()
        self._update_ui()

        if initial:
            self._open_initial(initial)

    # ------------------------------------------------------------ UI
    def _mkbtn(self, text: str, tip: str, slot, primary: bool = False,
               checkable: bool = False) -> QPushButton:
        btn = QPushButton(text)
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # 避免空格键被按钮抢走
        if checkable:
            btn.setCheckable(True)
        btn.clicked.connect(slot)
        if primary:
            btn.setObjectName("primary")
        return btn

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("content")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.view = ImageView()
        self.view.zoom_changed.connect(self._on_zoom)
        # 滚轮 / ↑↓ 都只发出"用户要缩放"的请求，缩放 + 自动暂停统一在这里处理
        self.view.zoom_requested.connect(self._on_zoom_requested)
        self.view.viewport().installEventFilter(self)   # 鼠标一动就把隐藏的光标还回来
        root.addWidget(self.view, 1)

        # 缩略图胶片条
        self.thumbs = QListWidget()
        self.thumbs.setViewMode(QListView.ViewMode.IconMode)
        self.thumbs.setFlow(QListView.Flow.LeftToRight)
        self.thumbs.setWrapping(False)
        self.thumbs.setResizeMode(QListView.ResizeMode.Adjust)
        self.thumbs.setMovement(QListView.Movement.Static)
        self.thumbs.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.thumbs.setGridSize(QSize(THUMB_SIZE + 14, THUMB_SIZE + 14))
        self.thumbs.setFixedHeight(THUMB_SIZE + 28)
        self.thumbs.setUniformItemSizes(True)
        self.thumbs.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.thumbs.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.thumbs.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.thumbs.itemClicked.connect(self._on_thumb_clicked)
        root.addWidget(self.thumbs, 0)

        # 控制条
        self.bar = QFrame()
        self.bar.setObjectName("controlBar")
        row = QHBoxLayout(self.bar)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(8)

        self.btn_open = self._mkbtn("打开", "打开图片或文件夹（Ctrl+O / Ctrl+Shift+O）", self.open_files)
        # 播放 / 暂停合并为一个切换按钮，与空格键完全等价
        self.btn_play = self._mkbtn("▶ 播放", "开始幻灯片（空格）", self.toggle_play, primary=True)
        self.btn_prev = self._mkbtn("‹ 上一张", "上一张（←）", self.prev_image)
        self.btn_next = self._mkbtn("下一张 ›", "下一张（→）", self.next_image)
        self.btn_shuffle = self._mkbtn(
            "随机", "随机播放：不按文件名顺序，一张不重复地打乱播放（R）",
            self.toggle_shuffle, checkable=True,
        )

        self.spin = QSpinBox()
        self.spin.setRange(1, 120)
        self.spin.setValue(4)
        self.spin.setSuffix(" 秒")
        self.spin.setToolTip("幻灯片切换间隔（1~120 秒）")
        self.spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.spin.setKeyboardTracking(False)
        self.spin.valueChanged.connect(self._on_interval_changed)
        self.spin.installEventFilter(self)   # 焦点在间隔框时把 ↑↓ 还给它

        self.btn_fit = self._mkbtn("适应窗口", "缩放至适应窗口（0）", self.view_fit)
        self.btn_one = self._mkbtn("1:1", "原始大小（1）", self.view_actual)
        self.btn_zoom_out = self._mkbtn("－", "缩小（↓ / -）", lambda: self.user_zoom(1 / ZOOM_STEP))
        self.btn_zoom_in = self._mkbtn("＋", "放大（↑ / +）", lambda: self.user_zoom(ZOOM_STEP))
        self.lbl_zoom = QLabel("100%")
        self.lbl_zoom.setFixedWidth(46)
        self.lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_name = QLabel("未打开图片")
        self.lbl_name.setObjectName("fileName")
        self.lbl_name.setFixedWidth(240)
        self.lbl_count = QLabel("0 / 0")
        self.lbl_count.setFixedWidth(76)
        self.lbl_count.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.btn_full = self._mkbtn("全屏", "全屏 / 退出全屏（F11）", self.toggle_fullscreen)

        row.addWidget(self.btn_open)
        row.addSpacing(4)
        row.addWidget(self.btn_play)
        row.addWidget(self.btn_prev)
        row.addWidget(self.btn_next)
        row.addWidget(self.btn_shuffle)
        row.addSpacing(10)
        row.addWidget(QLabel("间隔"))
        row.addWidget(self.spin)
        row.addSpacing(10)
        row.addWidget(self.btn_fit)
        row.addWidget(self.btn_one)
        row.addWidget(self.btn_zoom_out)
        row.addWidget(self.lbl_zoom)
        row.addWidget(self.btn_zoom_in)
        row.addStretch(1)
        row.addWidget(self.lbl_name)
        row.addWidget(self.lbl_count)
        row.addWidget(self.btn_full)

        root.addWidget(self.bar, 0)
        self.setCentralWidget(central)

        # 覆盖层：拖拽高亮框 + 浮层提示。绝对定位、鼠标穿透，不参与布局。
        self.drop_hint = QFrame(central)
        self.drop_hint.setObjectName("dropHint")
        self.drop_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.drop_hint.hide()
        self.toast = Toast(central)

        # 拖放事件必须冒泡到主窗口统一处理，所以子控件一律关掉 acceptDrops
        self.view.setAcceptDrops(False)
        self.thumbs.setAcceptDrops(False)

        # 状态栏
        self.lbl_decoder = QLabel()
        self.lbl_decoder.setStyleSheet("color:#8b93a1; padding-right:8px;")
        self.statusBar().addPermanentWidget(self.lbl_decoder)
        self.lbl_decoder.setText(
            f"Pillow {Image.__version__} · HEIC {'✓' if HEIF_OK else '✗ 未安装 pillow-heif'}"
        )
        self.statusBar().showMessage("拖入图片或文件夹即可开始")
        if not HEIF_OK:
            self.statusBar().showMessage(
                "提示：未检测到 pillow-heif，HEIC/HEIF 无法解码 —— 执行 pip install pillow-heif 后重启", 15000
            )

    def _build_actions(self) -> None:
        def act(text, shortcut, slot):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            self.addAction(a)
            return a

        self.act_open = act("打开图片…", QKeySequence.StandardKey.Open, self.open_files)
        self.act_open_dir = act("选择文件夹…", "Ctrl+Shift+O", self.choose_folder)
        self.act_play = act("播放/暂停", "Space", self.toggle_play)
        self.act_prev = act("上一张", "Left", self.prev_image)
        self.act_next = act("下一张", "Right", self.next_image)
        self.act_first = act("第一张", "Home", self.first_image)
        self.act_last = act("最后一张", "End", self.last_image)
        self.act_fit = act("适应窗口", "0", self.view_fit)
        self.act_actual = act("原始大小", "1", self.view_actual)
        self.act_zin = act("放大", None, lambda: self.user_zoom(ZOOM_STEP))
        self.act_zout = act("缩小", None, lambda: self.user_zoom(1 / ZOOM_STEP))
        # ↑/↓ 与 + / - 等价：放大/缩小并自动暂停幻灯片
        self.act_zin.setShortcuts([QKeySequence("+"), QKeySequence("="), QKeySequence("Up")])
        self.act_zin.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.act_zout.setShortcuts([QKeySequence("-"), QKeySequence("_"), QKeySequence("Down")])
        self.act_zout.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.act_full = act("全屏", "F11", self.toggle_fullscreen)
        # Enter：一键投影 —— 真全屏 + 自动开随机 + 自动开始播放（F11 仍是纯全屏）
        self.act_present = act("投影模式（全屏 + 随机播放）", None, self.enter_present)
        self.act_present.setShortcuts([QKeySequence("Return"), QKeySequence("Enter")])
        self.act_present.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.act_full.setToolTip("全屏（F11）；想连随机播放一起开用 Enter")
        self.act_esc = act("退出全屏", "Esc", self.exit_fullscreen)
        self.act_thumbs = act("显示/隐藏缩略图", "T", self.toggle_thumbs)
        self.act_chrome = act("显示/隐藏控制条", "H", self.toggle_chrome)
        self.act_help = act("格式与快捷键说明", "F1", self.show_help)
        self.act_quit = act("退出", "Ctrl+Q", self.close)

        # 随机播放 / 删除（删除＝移入回收站，可还原）
        self.act_shuffle = QAction("随机播放", self)
        self.act_shuffle.setCheckable(True)
        self.act_shuffle.setShortcut(QKeySequence("R"))
        self.act_shuffle.setToolTip("不按文件名顺序播放，一轮内不重复")
        self.act_shuffle.toggled.connect(self._on_shuffle_toggled)
        self.addAction(self.act_shuffle)
        self.act_del = act("删除当前图片到回收站", "Del", self.delete_current)
        self.act_del.setShortcuts([QKeySequence("Del"), QKeySequence("Backspace")])
        self.act_del.setShortcutContext(Qt.ShortcutContext.WindowShortcut)

        # 文件夹扫描范围：拖拽与菜单选择共用同一开关
        self.act_recursive = QAction("包含子文件夹", self)
        self.act_recursive.setCheckable(True)
        self.act_recursive.setShortcut(QKeySequence("Ctrl+R"))
        self.act_recursive.setToolTip("打开文件夹时是否连同子文件夹一起扫描")
        self.act_recursive.toggled.connect(self._on_recursive_toggled)
        self.addAction(self.act_recursive)

        m_file = self.menuBar().addMenu("文件")
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_open_dir)
        m_file.addAction(self.act_recursive)
        m_file.addSeparator()
        m_file.addAction(self.act_del)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

        m_play = self.menuBar().addMenu("播放")
        m_play.addAction(self.act_play)
        m_play.addAction(self.act_prev)
        m_play.addAction(self.act_next)
        m_play.addSeparator()
        m_play.addAction(self.act_shuffle)
        m_play.addSeparator()
        m_play.addAction(self.act_first)
        m_play.addAction(self.act_last)

        m_view = self.menuBar().addMenu("视图")
        m_view.addAction(self.act_fit)
        m_view.addAction(self.act_actual)
        m_view.addAction(self.act_zin)
        m_view.addAction(self.act_zout)
        m_view.addSeparator()
        m_view.addAction(self.act_present)
        m_view.addAction(self.act_full)
        m_view.addAction(self.act_thumbs)
        m_view.addAction(self.act_chrome)

        m_help = self.menuBar().addMenu("帮助")
        m_help.addAction(self.act_help)

        # 工具栏「打开」按钮下拉出两个入口，与菜单完全等价
        open_menu = QMenu(self)
        open_menu.addAction(self.act_open)
        open_menu.addAction(self.act_open_dir)
        self.btn_open.setMenu(open_menu)

    # ------------------------------------------------------------ 统一打开入口
    def _open_initial(self, target: str) -> None:
        """命令行传入的路径也走同一条入口，行为与拖拽/菜单一致。"""
        self.open_source([target])

    def open_files(self) -> None:
        start = os.path.dirname(self.paths[0]) if self.paths else ""
        files, _ = QFileDialog.getOpenFileNames(self, "打开图片", start, DIALOG_FILTER)
        if files:
            self.open_source(files)

    def choose_folder(self) -> None:
        """菜单「文件 → 选择文件夹…」：系统文件夹对话框，载入逻辑与拖拽完全一致。"""
        start = os.path.dirname(self.paths[0]) if self.paths else ""
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹", start)
        if folder:
            self.open_source([folder])

    def _on_recursive_toggled(self, checked: bool) -> None:
        self._recursive = checked
        self.statusBar().showMessage(
            "已开启：打开文件夹时连同子文件夹一起扫描" if checked else "已关闭：只扫描文件夹根目录",
            5000,
        )

    def open_source(self, paths: Sequence[str]) -> None:
        """
        拖拽与菜单选择的唯一入口，保证两种方式行为完全一致：
          · 文件夹   → 扫描（是否含子目录由「包含子文件夹」决定）后整目录载入
          · 图片文件 → 载入其所在目录，并定位到该文件
          · 空文件夹 / 全是不支持的文件 / 路径不存在 → 明确提示
        """
        items = [os.path.abspath(p) for p in paths if p]
        if not items:
            return

        missing = [p for p in items if not os.path.exists(p)]
        if missing:
            self._warn("路径不存在", "以下路径不存在或已被移动：\n\n" + "\n".join(missing[:8]))
            items = [p for p in items if os.path.exists(p)]
            if not items:
                return

        dirs = [p for p in items if os.path.isdir(p)]
        files = [p for p in items if os.path.isfile(p)]
        images = [p for p in files if is_image_path(p)]
        others = [p for p in files if not is_image_path(p)]

        # 1) 有文件夹：以第一个文件夹为准
        if dirs:
            folder = dirs[0]
            select = next((p for p in images if os.path.dirname(p) == folder), None)
            if not self._apply_folder(folder, select):
                return
            extra = len(files) - (1 if select else 0)
            if len(dirs) > 1:
                self._toast(
                    f"一次载入一个文件夹，已打开「{os.path.basename(folder)}」，"
                    f"其余 {len(dirs) - 1} 个文件夹未处理"
                )
            elif extra > 0:
                self._toast(f"已忽略随文件夹一起拖入的 {extra} 个文件")
            return

        # 2) 有图片文件：载入其所在目录并定位
        if images:
            folder = os.path.dirname(images[0])
            if not self._apply_folder(folder, images[0]):
                return
            if len({os.path.dirname(p) for p in images}) > 1:
                self._toast(f"已载入「{os.path.basename(folder)}」，其余 {len(images) - 1} 张来自其它文件夹，未一并载入")
            if others:
                self._toast(f"已跳过 {len(others)} 个不支持的文件")
            return

        # 3) 只有不支持的文件
        names = "、".join(os.path.basename(p) for p in others[:6])
        more = f" 等 {len(others)} 个文件" if len(others) > 6 else ""
        self._warn(
            "不支持的文件类型",
            f"没有找到可显示的图片，以下内容不受支持：\n\n{names}{more}\n\n"
            f"支持的格式：\n{self._ext_hint()}",
        )

    def _apply_folder(self, folder: str, select: Optional[str] = None) -> bool:
        """扫描并载入一个文件夹；载入成功返回 True，失败会给出明确提示。"""
        folder = os.path.abspath(folder)
        files = scan_folder(folder, self._recursive)

        # 根目录没图、但子目录里有 → 询问一次是否递归（不擅自改变用户设置）
        if not files and not self._recursive and has_images_below(folder):
            ret = QMessageBox.question(
                self,
                "子文件夹里有图片",
                f"「{os.path.basename(folder) or folder}」根目录下没有受支持的图片，\n"
                f"但它的子文件夹中存在图片。是否连同子文件夹一起载入？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ret == QMessageBox.StandardButton.Yes:
                self.act_recursive.setChecked(True)   # 同步菜单勾选状态
                files = scan_folder(folder, True)

        if not files:
            self._clear_view(f"没有可显示的图片\n\n{folder}")
            tip = (
                "子文件夹已包含在扫描范围内，其中也没有图片。"
                if self._recursive
                else "如果图片在子文件夹中，可勾选「文件 → 包含子文件夹」后重试。"
            )
            self._warn(
                "文件夹里没有图片",
                f"「{folder}」里没有受支持的图片。\n\n{tip}\n\n支持的格式：\n{self._ext_hint()}",
            )
            return False

        self.paths = files
        self._visited = set()          # 换文件夹＝新的一轮随机
        self._fill_thumbs(files)

        start = 0
        if select:
            sel = os.path.abspath(select)
            for i, p in enumerate(files):
                if os.path.abspath(p) == sel:
                    start = i
                    break

        self.show_index(start)
        scope = "（含子文件夹）" if self._recursive else ""
        self.statusBar().showMessage(f"已载入 {len(files)} 张图片{scope} · {folder}", 8000)
        return True

    def _clear_view(self, message: str) -> None:
        """清空当前状态并显示占位提示。"""
        self.paths = []
        self.index = -1
        self.playing = False
        self._visited = set()
        self._timer.stop()
        self._stop_animation()
        self.frames = []
        self.durations = []
        self._pending = -1          # 让在途的解码结果失效
        self.thumbs.clear()
        self.view.set_image(placeholder_pixmap(message))
        self._update_ui()

    def _warn(self, title: str, text: str) -> None:
        """阻断性提示：状态栏留一行 + 弹框说明。"""
        first = text.strip().splitlines()[0] if text.strip() else title
        self.statusBar().showMessage(f"{title}：{first}", 12000)
        QMessageBox.warning(self, title, text)

    def _toast(self, text: str, ms: int = 3600) -> None:
        """非阻断提示：画布顶部浮层。"""
        self.toast.show_over(self.view.geometry(), text, ms)

    @staticmethod
    def _ext_hint() -> str:
        return "  ".join(sorted(e.lstrip(".").upper() for e in SUPPORTED_EXT))

    def _fill_thumbs(self, files: Sequence[str]) -> None:
        self._thumb_gen += 1
        self._thumb_rows = []
        self.thumbs.clear()
        for i, path in enumerate(files):
            item = QListWidgetItem()
            item.setSizeHint(QSize(THUMB_SIZE + 10, THUMB_SIZE + 10))
            item.setToolTip(os.path.basename(path))
            item.setData(Qt.ItemDataRole.UserRole, path)   # 用于校验异步结果是否错行
            self.thumbs.addItem(item)
            self._thumb_rows.append(i)
        for i, path in enumerate(files[:THUMB_LIMIT]):
            self._thumb_pool.start(ThumbTask(self._thumb_gen, i, path, self._tsig))

    def _on_thumb_ready(self, gen: int, row: int, path: str, qimg) -> None:
        if gen != self._thumb_gen or row >= self.thumbs.count():
            return
        item = self.thumbs.item(row)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == path:
            item.setIcon(QPixmap.fromImage(qimg))

    def _on_thumb_failed(self, gen: int, row: int, path: str) -> None:
        if gen != self._thumb_gen or row >= self.thumbs.count():
            return
        item = self.thumbs.item(row)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == path:
            item.setText("✕")

    def _on_thumb_clicked(self, item: QListWidgetItem) -> None:
        row = self.thumbs.row(item)
        if 0 <= row < len(self.paths) and row != self.index:
            self.show_index(row)

    # ------------------------------------------------------------ 显示
    def show_index(self, index: int, restart_timer: bool = True) -> None:
        if not self.paths:
            return
        index %= len(self.paths)
        self.index = index
        self._visited.add(index)
        self._frame = 0
        self.frames = []
        self.durations = []
        self._stop_animation()

        self._token += 1
        self._pending = self._token
        self._decode_pool.start(DecodeTask(self._token, self.paths[index], self._sig))

        self._update_ui()
        if 0 <= index < self.thumbs.count():
            self.thumbs.blockSignals(True)
            self.thumbs.setCurrentRow(index)
            self.thumbs.blockSignals(False)
            self.thumbs.scrollToItem(self.thumbs.item(index), QListWidget.ScrollHint.EnsureVisible)

        if restart_timer and self.playing:
            self._schedule_next()

    def _on_decoded(self, token: int, frames, durations) -> None:
        if token != self._pending:      # 用户已经翻到别的图，丢弃旧结果
            return
        self.frames = frames
        self.durations = durations
        self._frame = 0
        self._display_frame(refit=True)
        self._start_animation()
        self.statusBar().showMessage(
            f"{os.path.basename(self.paths[self.index])} · {frames[0].width()}×{frames[0].height()}"
            + (f" · {len(frames)} 帧" if len(frames) > 1 else ""),
            4000,
        )

    def _on_decode_failed(self, token: int, message: str) -> None:
        if token != self._pending:
            return
        self.frames = []
        self._stop_animation()
        name = os.path.basename(self.paths[self.index]) if 0 <= self.index < len(self.paths) else ""
        hint = ""
        if name.lower().endswith((".heic", ".heif", ".avif")) and not HEIF_OK:
            hint = "\n\nHEIC/HEIF 需要 pillow-heif：pip install pillow-heif"
        self.view.set_image(placeholder_pixmap(f"无法显示这张图片\n\n{name}\n{message}{hint}"))
        self.statusBar().showMessage(f"{name} 解码失败：{message}", 8000)

    def _display_frame(self, refit: bool = False) -> None:
        if not self.frames:
            return
        pm = QPixmap.fromImage(self.frames[self._frame])
        if refit:
            self.view.set_image(pm)
        else:
            self.view.update_frame(pm)

    # ------------------------------------------------------------ 动图
    def _start_animation(self) -> None:
        self._anim.stop()
        if len(self.frames) > 1:
            self._anim.start(max(20, self.durations[0]))

    def _stop_animation(self) -> None:
        self._anim.stop()

    def _next_frame(self) -> None:
        if len(self.frames) < 2:
            return
        self._frame = (self._frame + 1) % len(self.frames)
        self._display_frame(refit=False)
        self._anim.start(max(20, self.durations[self._frame]))

    # ------------------------------------------------------------ 幻灯
    def _schedule_next(self) -> None:
        self._timer.stop()
        if self.playing and len(self.paths) > 1:
            self._timer.start(int(self.spin.value() * 1000))

    def _auto_advance(self) -> None:
        if not self.playing:
            return
        self.show_index(self._advance_index(1), restart_timer=True)

    def _advance_index(self, step: int) -> int:
        """算出下一个下标：随机模式下从"本轮还没放过"的池子里取，顺序模式 ±step。

        随机不是每次都 `randrange`——那样一张图可能连着放三遍、另一张永远轮不到。
        这里用"已放集合"洗牌：一轮内每张恰好出现一次，放完自动开新一轮。
        """
        n = len(self.paths)
        if n <= 1:
            return 0
        if self._shuffle and step > 0:
            pool = [i for i in range(n) if i not in self._visited and i != self.index]
            if not pool:
                self._visited = {self.index}
                pool = [i for i in range(n) if i != self.index]
            return self._rand.choice(pool)
        return self.index + step

    def toggle_play(self) -> None:
        """播放 / 暂停的唯一开关：按钮与空格键都调它，行为完全一致。"""
        if not self.paths:
            self.statusBar().showMessage("先打开一个文件夹或图片", 4000)
            return
        self.set_playing(not self.playing)

    def play(self) -> None:
        self.set_playing(True)

    def pause(self, reason: str = "") -> None:
        """暂停。reason 用于在状态栏说明"为什么暂停"（如手动缩放）。"""
        self.set_playing(False, reason=reason)

    def set_playing(self, on: bool, reason: str = "") -> None:
        """唯一的播放状态写入口：状态、定时器、按钮、菜单、状态栏一次性对齐。"""
        if not self.paths:
            return
        was = self.playing
        self.playing = bool(on)
        self._schedule_next()          # 播放则（重新）起表，暂停则停表
        self._update_ui()              # 按钮文字 / 可用性与状态栏同步

        if self.playing:
            tail = f"{reason} · " if reason else ""
            self.statusBar().showMessage(
                f"{tail}播放中 · 每 {self.spin.value()} 秒切换 · 空格暂停", 5000
            )
            return
        if not was and not reason:
            return                     # 本来就在暂停，不重复提示
        name = os.path.basename(self.paths[self.index]) if 0 <= self.index < len(self.paths) else ""
        prefix = f"已暂停（{reason}）" if reason else "已暂停"
        self.statusBar().showMessage(
            f"{prefix} · 停在第 {self.index + 1} 张 {name} · 空格继续", 6000
        )

    def next_image(self) -> None:
        if self.paths:
            self.show_index(self._advance_index(1))

    # ------------------------------------------------------------ 随机
    def toggle_shuffle(self) -> None:
        self.set_shuffle(self.btn_shuffle.isChecked())

    def _on_shuffle_toggled(self, on: bool) -> None:
        self.set_shuffle(on)

    def set_shuffle(self, on: bool) -> None:
        """随机开关的唯一写入口：按钮 / 菜单 / R 键共用，状态单一来源。"""
        on = bool(on)
        if on != self._shuffle:
            self._shuffle = on
            # 开启时把当前这张记为已放，避免马上又抽到它
            self._visited = {self.index} if 0 <= self.index < len(self.paths) else set()
        self.btn_shuffle.setChecked(self._shuffle)
        self.act_shuffle.setChecked(self._shuffle)
        self.statusBar().showMessage(
            "随机播放：开（一轮内不重复，R 切换）" if self._shuffle
            else "随机播放：关（按文件名顺序，R 切换）", 4000
        )

    # ------------------------------------------------------------ 删除
    def delete_current(self) -> None:
        """Del：把当前图片移到回收站（可还原），然后接着显示下一张。

        播放中删除不打断播放，只是把定时器重新计时。
        """
        if not (0 <= self.index < len(self.paths)):
            self.statusBar().showMessage("当前没有可删除的图片", 3000)
            return
        path = self.paths[self.index]
        name = os.path.basename(path)
        ok, err = send_to_recycle_bin(path)
        if not ok:
            self._warn("删除失败", f"无法把这张图片移到回收站：\n{path}\n\n原因：{err}")
            return

        row = self.index
        del self.paths[row]
        # 行号整体前移，已放集合也得跟着挪，否则随机播放会漏图/重图
        self._visited = {i - 1 if i > row else i for i in self._visited if i != row}
        if row < self.thumbs.count():
            self.thumbs.takeItem(row)          # 缩略图条同步移除

        if not self.paths:
            folder = os.path.dirname(path)
            self._clear_view(f"这个文件夹里已经没有图片了\n\n{folder}")
            self._toast(f"已移到回收站：{name}（文件夹已空）")
            return

        self.show_index(min(row, len(self.paths) - 1))
        self._toast(f"已移到回收站：{name} · 还剩 {len(self.paths)} 张")

    def prev_image(self) -> None:
        if self.paths:
            self.show_index(self.index - 1)

    def first_image(self) -> None:
        if self.paths:
            self.show_index(0)

    def last_image(self) -> None:
        if self.paths:
            self.show_index(len(self.paths) - 1)

    def _on_interval_changed(self, _value: int) -> None:
        if self.playing:
            self._schedule_next()

    # ------------------------------------------------------------ 视图
    def view_fit(self) -> None:
        self.view.fit_to_window()

    def view_actual(self) -> None:
        self.view.actual_size()

    def _on_zoom_requested(self, factor: float, pos: QPoint) -> None:
        """画布发来的缩放请求：滚轮带鼠标坐标，↑↓ 传 (-1,-1) 表示画布中心。"""
        self.user_zoom(factor, pos)

    def user_zoom(self, factor: float, anchor_pos: Optional[QPoint] = None) -> bool:
        """用户主动缩放（滚轮 / ↑↓ / ＋－按钮 / 菜单）：缩放并自动暂停。

        手动缩放＝接管画面，继续自动切下一张会突然跳图，所以任何主动缩放都
        顺手暂停；本来就没在播放时 pause() 是空操作，不会重复刷提示。
        """
        if not self.paths:
            self.statusBar().showMessage("先打开一个文件夹或图片", 3000)
            return False
        changed = self.view.zoom_by(factor, anchor_pos)
        if self.playing:
            self.pause(reason="手动缩放")
        if not changed:
            lo, hi = self.view.zoom_limits()
            if factor > 1:
                self.statusBar().showMessage(f"已到最大放大倍数 {hi:.0%}", 3000)
            else:
                self.statusBar().showMessage(f"已到最小缩小倍数 {lo:.0%}", 3000)
        return changed

    def _on_zoom(self, z: float) -> None:
        self.lbl_zoom.setText(f"{z * 100:.0f}%")
        self.lbl_zoom.setToolTip(f"当前缩放 {z * 100:.0f}%（↑ 放大 / ↓ 缩小 / 滚轮以光标为中心）")

    def toggle_thumbs(self) -> None:
        self._thumbs_on = not self._thumbs_on
        self._apply_chrome()

    def toggle_chrome(self) -> None:
        if self._fullscreen:
            self._fs_chrome = not self._fs_chrome   # 全屏下只临时唤出/收起控制条
        else:
            self._chrome_on = not self._chrome_on
        self._apply_chrome()

    def _apply_chrome(self) -> None:
        """界面元素的显隐只在这一处决定，避免各处 setVisible 互相打架。

        全屏（投影）时菜单栏、缩略图条、控制条、状态栏一律不显示，画面占满整块屏；
        退出全屏后恢复用户原本想不想看缩略图/控制条的选择。
        """
        if self._fullscreen:
            self.menuBar().setVisible(self._fs_chrome)
            self.bar.setVisible(self._fs_chrome)
            self.statusBar().setVisible(False)
            self.thumbs.setVisible(False)
            return
        self.menuBar().setVisible(self._chrome_on)
        self.bar.setVisible(self._chrome_on)
        self.statusBar().setVisible(True)
        self.thumbs.setVisible(self._thumbs_on and bool(self.paths))

    # ------------------------------------------------------------ 全屏
    def toggle_fullscreen(self) -> None:
        self._set_fullscreen(not self._fullscreen)

    def enter_present(self) -> None:
        """Enter：一键进入投影模式 —— 真全屏 + 随机播放 + 自动开始。

        已在全屏时再按一次退出（与 Esc 等价，播放/随机状态都保留），
        这样"Enter 进 / Enter 出"符合直觉，误触也能马上回来。
        """
        if self._fullscreen:
            self._set_fullscreen(False)
            return
        if not self.paths:
            self.statusBar().showMessage("先打开一个文件夹或图片，再按 Enter 投影", 4000)
            self._toast("先打开一个文件夹或图片，再按 Enter 投影", 3600)
            return
        if len(self.paths) > 1:
            self.set_shuffle(True)          # 随机池以当前这张为已放，不会立刻重复
        self._set_fullscreen(True, tip="")
        self.set_playing(True)              # 起表 + 按钮/菜单同步 + 全屏下藏鼠标
        head = "投影模式 · 随机播放" if self._shuffle else "投影模式"
        self._toast(
            f"{head} · 每 {self.spin.value()} 秒切换 · Esc / Enter 退出 · 空格暂停", 4200
        )

    def exit_fullscreen(self) -> None:
        if self._fullscreen:
            self._set_fullscreen(False)

    def _set_fullscreen(self, on: bool, tip: Optional[str] = None) -> None:
        if on == self._fullscreen:
            return
        self._fullscreen = on
        self._fs_chrome = False
        if on:
            self._prev_state = self.windowState()      # 记住最大化/普通，退出时还原
            self.showFullScreen()
            self.btn_full.setText("退出全屏")
            self._sync_cursor()                        # 全屏且播放中才藏鼠标
            self._toast(tip if tip is not None
                        else "全屏播放 · Esc / F11 退出 · 空格暂停 · R 随机 · Del 删除")
        else:
            if self._prev_state & Qt.WindowState.WindowMaximized:
                self.showMaximized()
            else:
                self.showNormal()
            self.btn_full.setText("全屏")
            self.view.unsetCursor()
            self._cursor_hidden = False
        self._apply_chrome()

    def _sync_cursor(self) -> None:
        """全屏且播放中时隐藏鼠标指针，避免投影时画面正中挂个箭头。"""
        want_hidden = self._fullscreen and self.playing
        if want_hidden and not self._cursor_hidden:
            self.view.setCursor(Qt.CursorShape.BlankCursor)
            self._cursor_hidden = True
        elif not want_hidden and self._cursor_hidden:
            self.view.unsetCursor()
            self._cursor_hidden = False

    # ------------------------------------------------------------ 状态
    def _update_ui(self) -> None:
        has = bool(self.paths)
        for w in (self.btn_prev, self.btn_next, self.btn_full):
            w.setEnabled(has)
        # 播放 / 暂停是同一个按钮：文案、提示、菜单项都跟着 playing 走，绝不各说各话
        self.btn_play.setEnabled(has)
        self.btn_play.setText("❚❚ 暂停" if self.playing else "▶ 播放")
        self.btn_play.setToolTip("暂停幻灯片（空格）" if self.playing else "开始幻灯片（空格）")
        self.act_play.setText("暂停幻灯片" if self.playing else "播放幻灯片")
        self.btn_zoom_in.setEnabled(has)
        self.btn_zoom_out.setEnabled(has)
        self.btn_fit.setEnabled(has)
        self.btn_one.setEnabled(has)
        self.btn_shuffle.setEnabled(len(self.paths) > 1)
        self.act_del.setEnabled(has)
        self.btn_shuffle.setChecked(self._shuffle)
        self.act_shuffle.setChecked(self._shuffle)

        if has and 0 <= self.index < len(self.paths):
            name = os.path.basename(self.paths[self.index])
            fm = QFontMetrics(self.lbl_name.font())
            self.lbl_name.setText(fm.elidedText(name, Qt.TextElideMode.ElideMiddle, 230))
            self.lbl_name.setToolTip(name)
            self.lbl_count.setText(f"{self.index + 1} / {len(self.paths)}")
            self.setWindowTitle(f"{name} — 光影相册")
        else:
            self.lbl_name.setText("未打开图片")
            self.lbl_name.setToolTip("")
            self.lbl_count.setText("0 / 0")
            self.setWindowTitle("光影相册 · Image Viewer")

        self._apply_chrome()      # 缩略图/菜单/控制条/状态栏的显隐统一在这里落地
        self._sync_cursor()       # 全屏 + 播放中才隐藏鼠标

    # ------------------------------------------------------------ 事件过滤
    def eventFilter(self, obj, event) -> bool:
        """两个用途：
        1. 焦点在「间隔」输入框时，↑/↓ 归输入框微调数值，不被缩放快捷键抢走；
        2. 全屏隐藏鼠标后，鼠标一动就把指针还回来。
        """
        if obj is self.spin and event.type() == QEvent.Type.ShortcutOverride:
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                event.accept()
                return True
        elif obj is self.view.viewport() and event.type() == QEvent.Type.MouseMove:
            if self._cursor_hidden:
                self.view.unsetCursor()
                self._cursor_hidden = False
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------ 拖放
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._show_drop_hint(True)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._show_drop_hint(False)

    def dropEvent(self, event) -> None:
        self._show_drop_hint(False)
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        event.acceptProposedAction()
        if paths:
            self.open_source(paths)   # 与菜单选择走同一入口
        else:
            self._warn("无法载入", "拖入的内容不是本地文件（可能来自网络链接或不支持的拖放源）。")

    def _show_drop_hint(self, on: bool) -> None:
        if on:
            self.drop_hint.setGeometry(self.view.geometry().adjusted(8, 8, -8, -8))
            self.drop_hint.show()
            self.drop_hint.raise_()
            self.statusBar().showMessage("松开鼠标即可载入：单个/多个图片文件，或整个文件夹")
        else:
            self.drop_hint.hide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.toast.reposition()
        if self.drop_hint.isVisible():
            self.drop_hint.setGeometry(self.view.geometry().adjusted(8, 8, -8, -8))

    # ------------------------------------------------------------ 帮助
    def show_help(self) -> None:
        heif_state = "已启用 ✓" if HEIF_OK else f"未启用 ✗（{HEIF_ERR}）"
        html = f"""
        <h3>打开方式（三种方式行为一致）</h3>
        <ul>
          <li>把<b>图片文件</b>或<b>整个文件夹</b>直接拖进窗口（支持多选）</li>
          <li>菜单「文件 → 选择文件夹…」<code>Ctrl+Shift+O</code>、「打开图片…」<code>Ctrl+O</code></li>
          <li>启动参数：<code>python image_viewer.py D:\\photos</code></li>
        </ul>
        <p style="color:#8b93a1">拖入文件夹会自动扫描其中的受支持图片；是否包含子文件夹由
        「文件 → 包含子文件夹」<code>Ctrl+R</code> 决定。空文件夹、全部为不支持的文件都会弹框明确提示。</p>

        <h3>支持的格式与解码方式</h3>
        <table cellspacing="0" cellpadding="4" border="0">
          <tr><td><b>格式</b></td><td><b>解码器</b></td><td><b>说明</b></td></tr>
          <tr><td>JPG / JPEG / JFIF / PJPEG</td><td>Pillow（libjpeg-turbo）</td>
              <td>JFIF 只是 JPEG 的文件容器写法，按内容嗅探，扩展名不影响</td></tr>
          <tr><td>PNG / APNG</td><td>Pillow（zlib）</td><td>保留透明通道；APNG 支持多帧</td></tr>
          <tr><td>WEBP</td><td>Pillow（libwebp）</td><td>静态 + 动态 WebP 均支持</td></tr>
          <tr><td>GIF</td><td>Pillow</td><td>按帧时长播放，自动处理帧间处置与透明</td></tr>
          <tr><td>BMP / DIB / TIFF / ICO</td><td>Pillow</td><td>—</td></tr>
          <tr><td>HEIC / HEIF / AVIF</td><td>pillow-heif（libheif）</td><td>{heif_state}</td></tr>
        </table>
        <p style="color:#8b93a1">色彩：带 ICC 配置的图片会先转 sRGB 再显示，避免偏色。<br>
        方向：按 EXIF Orientation 自动旋正竖拍照片。</p>

        <h3>快捷键</h3>
        <table cellspacing="0" cellpadding="3">
          <tr><td>空格</td><td>播放 / 暂停（同一个切换按钮，行为一致）</td></tr>
          <tr><td>← / →</td><td>上一张 / 下一张</td></tr>
          <tr><td>Home / End</td><td>第一张 / 最后一张</td></tr>
          <tr><td>↑ / ↓</td><td>放大 / 缩小（会同时暂停幻灯片）</td></tr>
          <tr><td>0 / 1</td><td>适应窗口 / 原始大小</td></tr>
          <tr><td>+ / - 或滚轮</td><td>缩放（滚轮以光标为中心）</td></tr>
          <tr><td>双击画面</td><td>适应窗口 ⇄ 100%</td></tr>
          <tr><td>拖动画布</td><td>放大后平移查看</td></tr>
          <tr><td>Enter</td><td><b>一键投影：真全屏 + 自动开随机 + 自动开始播放</b>（再按一次退出）</td></tr>
          <tr><td>F11 / Esc</td><td>全屏 / 退出全屏（真全屏：菜单、缩略图、控制条、状态栏全部隐藏）</td></tr>
          <tr><td>全屏中按 H</td><td>临时唤出 / 收起控制条</td></tr>
          <tr><td>R</td><td>随机播放开关（一轮内每张只出现一次）</td></tr>
          <tr><td>Del / Backspace</td><td>把当前图片移到回收站（可在资源管理器还原）</td></tr>
          <tr><td>T / H</td><td>显示隐藏缩略图 / 控制条</td></tr>
          <tr><td>Ctrl+O / Ctrl+Shift+O</td><td>打开图片 / 打开文件夹</td></tr>
        </table>
        <p style="color:#8b93a1">缩放范围：5% ~ 1600%（相对原图像素）；任何主动缩放都会自动暂停幻灯片。<br>
        全屏播放时鼠标指针会自动隐藏，移动鼠标即可恢复。</p>
        """
        box = QMessageBox(self)
        box.setWindowTitle("格式与快捷键说明")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(html)
        box.exec()

    # ------------------------------------------------------------ 退出
    def closeEvent(self, event) -> None:
        self._timer.stop()
        self._anim.stop()
        self._decode_pool.clear()
        self._thumb_pool.clear()
        self._decode_pool.waitForDone(1500)
        self._thumb_pool.waitForDone(1500)
        super().closeEvent(event)


def _resource_path(name: str) -> str:
    """兼容源码运行与 PyInstaller 单文件模式（后者解包到 sys._MEIPASS）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _out(msg: str) -> None:
    """打包成窗口程序后 stdout 可能为 None，这里兜一层，避免自检时抛异常。"""
    try:
        if sys.stdout:
            print(msg, flush=True)
    except Exception:
        pass


def run_selftest(target: str) -> int:
    """自检模式：把目标内的图片全部解码一遍并写报告，用来验证打包产物是否完整。"""
    try:
        from PySide6 import __version__ as qt_version
    except Exception:
        qt_version = "?"
    try:
        import pillow_heif
        heif_version = getattr(pillow_heif, "__version__", "?")
    except Exception:
        heif_version = "未安装"

    if os.path.isdir(target):
        paths = scan_folder(target, True)
    elif os.path.isfile(target):
        paths = [target]
    else:
        paths = []

    app = QApplication.instance() or QApplication(sys.argv[:1])  # noqa: F841  QImage 解码更稳妥

    lines = [
        "光影相册 · 运行环境自检",
        "时间      : " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "运行方式  : " + ("打包单文件（frozen）" if getattr(sys, "frozen", False) else "源码"),
        "Python    : " + sys.version.split()[0],
        "PySide6   : " + str(qt_version),
        "Pillow    : " + str(Image.__version__),
        "pillow-heif: " + str(heif_version) + ("  （HEIC 可用）" if HEIF_OK else f"  （HEIC 不可用：{HEIF_ERR}）"),
        "检测目录  : " + target,
        "支持扩展  : " + " ".join(sorted(e.lstrip(".").upper() for e in SUPPORTED_EXT)),
        "",
    ]

    ok = bad = 0
    for p in paths:
        name = os.path.basename(p)
        t0 = time.perf_counter()
        try:
            frames, _durations = decode_image(p)
            cost = (time.perf_counter() - t0) * 1000
            lines.append(
                f"[OK ] {name:<28s} {frames[0].width()}x{frames[0].height()} "
                f"{len(frames)} 帧  {cost:.0f}ms"
            )
            ok += 1
        except Exception as exc:
            lines.append(f"[ERR] {name:<28s} {type(exc).__name__}: {exc}")
            bad += 1

    lines += ["", f"合计：成功 {ok} 个，失败 {bad} 个", ""]

    # GUI 冒烟：建窗口 → 载入 → 截图，验证 Qt 平台插件与控件栈在打包环境里可用
    try:
        win = ViewerWindow()
        win.resize(1200, 800)
        win.move(-4000, -4000)      # 移到屏幕外，不打扰正在用电脑的人
        win.show()
        app.processEvents()
        if paths and os.path.isdir(target):
            win.open_source([target])
            deadline = time.time() + 5.0
            while time.time() < deadline and not win.frames:
                app.processEvents()
                time.sleep(0.02)
        shot = os.path.join(tempfile.gettempdir(), "image_viewer_selftest.png")
        saved = win.grab().save(shot)
        loaded = len(win.paths)
        frames = len(win.frames)
        win.close()
        app.processEvents()
        lines.append(f"GUI 自检  : 窗口创建正常，载入 {loaded} 张，当前图 {frames} 帧")
        lines.append(f"界面截图  : {shot if saved else '(保存失败)'}")
    except Exception as exc:
        lines.append(f"GUI 自检  : 失败 {type(exc).__name__}: {exc}")

    report = "\n".join(lines)

    out_path = os.path.join(tempfile.gettempdir(), "image_viewer_selftest.txt")
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(report)
    except Exception:
        out_path = "(写入失败)"

    _out(report)
    _out(f"\n报告已写入：{out_path}")
    return 0 if ok > 0 else 1


def main() -> int:
    argv = sys.argv[1:]
    if "--selftest" in argv:
        i = argv.index("--selftest")
        target = argv[i + 1] if len(argv) > i + 1 else os.getcwd()
        return run_selftest(target)

    app = QApplication(sys.argv)
    app.setApplicationName("光影相册")
    font = QFont("Microsoft YaHei UI")
    font.setPointSize(9)
    app.setFont(font)

    icon = _resource_path("icon.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))

    initial = argv[0] if argv else None
    win = ViewerWindow(initial)
    if os.path.exists(icon):
        win.setWindowIcon(QIcon(icon))
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
