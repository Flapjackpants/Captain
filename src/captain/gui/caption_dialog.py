"""Text+ caption style, timing, and project-frame preview dialog."""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFontComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..captions import normalize_caption_settings


def _color_button(color: str) -> QPushButton:
    button = QPushButton(color)
    button.setProperty("captionColor", color)
    button.setStyleSheet(f"background-color: {color}; color: #ffffff;")
    return button


class CaptionPreview(QWidget):
    def __init__(self, width: int, height: int, pixmap: QPixmap | None, parent=None):
        super().__init__(parent)
        self.project_width = max(1, int(width))
        self.project_height = max(1, int(height))
        self.pixmap = pixmap or QPixmap()
        self.settings: dict[str, Any] = {}
        self.texts: list[str] = []
        self.preview_index = 0
        self.setMinimumSize(400, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_preview(self, texts: list[str], settings: dict[str, Any], index: int = 0) -> None:
        self.texts = [t for t in texts if t]
        self.settings = normalize_caption_settings(settings)
        self.preview_index = max(0, min(index, len(self.texts) - 1)) if self.texts else 0
        if self.settings["auto_fit"] and self.texts:
            self.settings["font_size"] = self._fit_font_size(self.texts)
        self.update()

    def _font(self, pixel_size: int | None = None) -> QFont:
        s = self.settings
        font = QFont(str(s.get("font_family") or "Arial"))
        style = str(s.get("font_style", "Regular")).lower()
        font.setBold("bold" in style)
        font.setItalic("italic" in style)
        font.setPixelSize(max(1, int(pixel_size or s.get("font_size", 96))))
        return font

    def _fit_font_size(self, texts: list[str]) -> int:
        center_x = float(self.settings.get("position_x", 0.5)) + 0.5 - float(
            self.settings.get("anchor_x", 0.5)
        )
        center_y = float(self.settings.get("position_y", 0.85)) + 0.5 - float(
            self.settings.get("anchor_y", 0.5)
        )
        transform_scale_x = float(self.settings.get("scale_x", 1.0))
        transform_scale_y = float(self.settings.get("scale_y", 1.0))
        angle = math.radians(float(self.settings.get("rotation", 0.0)))
        cos_angle, sin_angle = abs(math.cos(angle)), abs(math.sin(angle))
        transform_bound = max(
            cos_angle * transform_scale_x + sin_angle * transform_scale_y,
            sin_angle * transform_scale_x + cos_angle * transform_scale_y,
        )
        available_width = max(
            0.0, 2 * min(center_x - 0.05, 0.95 - center_x) / transform_bound
        )
        available_height = max(
            0.0, 2 * min(center_y - 0.10, 0.90 - center_y) / transform_bound
        )
        safe_width = max(
            1,
            int(
                self.project_width
                * min(0.90 * float(self.settings.get("layout_width", 1.0)), available_width)
            ),
        )
        safe_height = max(
            1,
            int(
                self.project_height
                * min(0.80 * float(self.settings.get("layout_height", 1.0)), available_height)
            ),
        )
        lo, hi = 1, min(self.project_width, self.project_height)
        alignment = self._alignment_flag()
        while lo < hi:
            mid = (lo + hi + 1) // 2
            metrics = QFontMetricsF(self._font(mid))
            fits = True
            for text in texts:
                rect = metrics.boundingRect(
                    QRect(0, 0, safe_width, safe_height),
                    alignment | Qt.TextFlag.TextWordWrap,
                    text,
                )
                if rect.width() > safe_width + 0.5 or rect.height() > safe_height + 0.5:
                    fits = False
                    break
            if fits:
                lo = mid
            else:
                hi = mid - 1
        return max(1, lo)

    def _alignment_flag(self):
        value = self.settings.get("alignment", "center")
        if value == "left":
            return Qt.AlignmentFlag.AlignLeft
        if value == "right":
            return Qt.AlignmentFlag.AlignRight
        return Qt.AlignmentFlag.AlignHCenter

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#111116"))
        target = QRect(0, 0, self.project_width, self.project_height)
        target = target.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QRect(
            (self.width() - target.width()) // 2,
            (self.height() - target.height()) // 2,
            target.width(),
            target.height(),
        )
        if not self.pixmap.isNull():
            p.drawPixmap(target, self.pixmap)
        else:
            p.fillRect(target, QColor("#25252b"))
            p.setPen(QColor("#898991"))
            p.drawText(target, Qt.AlignmentFlag.AlignCenter, "Timeline frame unavailable")

        s = self.settings
        p.save()
        p.setClipRect(target)
        sx = target.width() / self.project_width
        sy = target.height() / self.project_height
        p.translate(target.topLeft())
        p.scale(sx, sy)

        text = self.texts[self.preview_index] if self.texts else "Caption preview"
        x = float(s.get("position_x", 0.5)) * self.project_width
        y = float(s.get("position_y", 0.85)) * self.project_height
        anchor_x = float(s.get("anchor_x", 0.5)) * self.project_width
        anchor_y = float(s.get("anchor_y", 0.5)) * self.project_height
        scale_x = float(s.get("scale_x", 1.0))
        scale_y = float(s.get("scale_y", 1.0))
        p.translate(QPointF(x, y))
        p.rotate(float(s.get("rotation", 0.0)))
        p.scale(scale_x, scale_y)
        p.translate(QPointF(-anchor_x, -anchor_y))

        safe = QRectF(
            self.project_width * 0.05,
            self.project_height * 0.10,
            self.project_width * 0.90 * float(s.get("layout_width", 1.0)),
            self.project_height * 0.80 * float(s.get("layout_height", 1.0)),
        )
        font = self._font(int(s.get("font_size", 96)))
        p.setFont(font)
        vertical = {
            "top": Qt.AlignmentFlag.AlignTop,
            "center": Qt.AlignmentFlag.AlignVCenter,
            "bottom": Qt.AlignmentFlag.AlignBottom,
        }.get(str(s.get("vertical_alignment", "center")), Qt.AlignmentFlag.AlignVCenter)
        text_layout = QTextLayout(text, font)
        text_option = QTextOption()
        text_option.setAlignment(Qt.AlignmentFlag.AlignLeft)
        text_option.setWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
            if s.get("layout_type", "Frame") == "Frame"
            else QTextOption.WrapMode.NoWrap
        )
        text_layout.setTextOption(text_option)
        text_layout.beginLayout()
        lines = []
        y_line = 0.0
        while True:
            line = text_layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(safe.width())
            if self._alignment_flag() == Qt.AlignmentFlag.AlignLeft:
                line_x = 0.0
            elif self._alignment_flag() == Qt.AlignmentFlag.AlignRight:
                line_x = max(0.0, safe.width() - line.naturalTextWidth())
            else:
                line_x = max(0.0, (safe.width() - line.naturalTextWidth()) / 2.0)
            line.setPosition(QPointF(line_x, y_line))
            y_line += line.height() * float(s.get("line_spacing", 1.0))
            lines.append(line)
        text_layout.endLayout()
        content_height = y_line
        if vertical == Qt.AlignmentFlag.AlignVCenter:
            y_offset = max(0.0, (safe.height() - content_height) / 2.0)
        elif vertical == Qt.AlignmentFlag.AlignBottom:
            y_offset = max(0.0, safe.height() - content_height)
        else:
            y_offset = 0.0
        glyph_path = QPainterPath()
        for line in lines:
            line_y = line.position().y() + y_offset
            glyph_path.addText(
                safe.left() + line.position().x(),
                safe.top() + line_y + line.ascent(),
                font,
                text[line.textStart() : line.textStart() + line.textLength()],
            )
        outline = float(s.get("outline_width", 0.0)) if s.get("outline_enabled") else 0.0
        if outline > 0:
            if s.get("shadow_enabled"):
                shadow = QColor(str(s.get("shadow_color", "#000000")))
                shadow.setAlphaF(float(s.get("shadow_opacity", 0.65)))
                p.save()
                p.translate(max(1.0, outline), max(1.0, outline))
                p.setPen(QPen(shadow, outline))
                p.setBrush(shadow)
                p.drawPath(glyph_path)
                p.restore()
            outline_color = QColor(str(s.get("outline_color", "#000000")))
            outline_color.setAlphaF(float(s.get("outline_opacity", 1.0)))
            pen = QPen(outline_color)
            pen.setWidthF(outline)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(glyph_path)
        elif s.get("shadow_enabled"):
            shadow = QColor(str(s.get("shadow_color", "#000000")))
            shadow.setAlphaF(float(s.get("shadow_opacity", 0.65)))
            p.save()
            p.translate(3, 3)
            p.setPen(QPen(shadow, 3))
            p.setBrush(shadow)
            p.drawPath(glyph_path)
            p.restore()
        color = QColor(str(s.get("text_color", "#FFFFFF")))
        color.setAlphaF(float(s.get("image_opacity", 1.0)))
        p.setPen(color)
        text_layout.draw(p, QPointF(safe.left(), safe.top() + y_offset))
        p.restore()
        p.setPen(QColor("#a2a2aa"))
        p.drawText(QRect(target.left(), target.bottom() + 4, target.width(), 20), Qt.AlignmentFlag.AlignCenter,
                   f"{self.project_width} × {self.project_height}")


class CaptionSettingsDialog(QDialog):
    def __init__(
        self,
        width: int,
        height: int,
        preview_pixmap: QPixmap | None,
        caption_texts: list[str],
        word_texts: list[str],
        settings: dict[str, Any] | None = None,
        parent=None,
        preview_source: str = "timeline",
    ):
        super().__init__(parent)
        self.setWindowTitle("Create Captions · Text+")
        self.resize(1000, 720)
        self._values = normalize_caption_settings(settings)
        self._caption_texts = [t for t in caption_texts if t]
        self._word_texts = [t for t in word_texts if t]

        root = QVBoxLayout(self)
        warning = QLabel(
            "For best results, generate captions from a single compound clip."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #f0bd67; font-weight: 600;")
        root.addWidget(warning)

        body = QHBoxLayout()
        self.preview = CaptionPreview(width, height, preview_pixmap)
        left = QVBoxLayout()
        preview_head = QHBoxLayout()
        preview_label = "Preview at project resolution"
        if preview_source == "clip":
            preview_label += " · selected clip frame"
        elif preview_source == "neutral":
            preview_label += " · neutral background (no frame available)"
        preview_head.addWidget(QLabel(preview_label))
        preview_head.addStretch(1)
        self.preview_combo = QComboBox()
        self.preview_combo.addItem("Longest caption", -1)
        for i, text in enumerate(self._caption_texts):
            self.preview_combo.addItem(f"{i + 1}. {text[:42]}", i)
        self.preview_combo.currentIndexChanged.connect(self._refresh_preview)
        preview_head.addWidget(self.preview_combo)
        left.addLayout(preview_head)
        left.addWidget(self.preview, stretch=1)
        body.addLayout(left, stretch=3)

        self.tabs = QTabWidget()
        self._build_text_tab()
        self._build_layout_tab()
        self._build_transform_tab()
        self._build_shading_tab()
        self._build_image_tab()
        body.addWidget(self.tabs, stretch=2)
        root.addLayout(body, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        create = QPushButton("Create Captions")
        create.setObjectName("accent")
        create.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(create)
        root.addLayout(buttons)
        self._refresh_preview()

    def _spin(self, key: str, *, minv: float, maxv: float, step: float = 1.0, decimals: int = 0):
        box = QDoubleSpinBox()
        box.setRange(minv, maxv)
        box.setSingleStep(step)
        box.setDecimals(decimals)
        box.setValue(float(self._values[key]))
        box.valueChanged.connect(lambda value, k=key: self._set(k, value))
        return box

    def _set(self, key: str, value: Any) -> None:
        self._values[key] = value
        if key == "font_size" and self._values.get("auto_fit"):
            self._values["auto_fit"] = False
            self.auto_fit.setChecked(False)
        self._refresh_preview()

    def _set_scale_link(self, linked: bool) -> None:
        self._values["link_scale"] = linked
        if linked:
            self.scale_y.blockSignals(True)
            self.scale_y.setValue(self.scale_x.value())
            self.scale_y.blockSignals(False)
            self._values["scale_y"] = self.scale_x.value()
        self._refresh_preview()

    def _scale_changed(self, key: str, value: float) -> None:
        self._values[key] = value
        if self._values.get("link_scale"):
            other = self.scale_y if key == "scale_x" else self.scale_x
            other.blockSignals(True)
            other.setValue(value)
            other.blockSignals(False)
            self._values["scale_x"] = value
            self._values["scale_y"] = value
        self._refresh_preview()

    def _form_tab(self, title: str) -> tuple[QWidget, QFormLayout]:
        widget = QWidget()
        form = QFormLayout(widget)
        self.tabs.addTab(widget, title)
        return widget, form

    def _build_text_tab(self) -> None:
        _widget, form = self._form_tab("Text")
        self.font = QFontComboBox()
        if self._values["font_family"]:
            self.font.setCurrentFont(QFont(str(self._values["font_family"])))
        else:
            self.font.setCurrentFont(QFont("Arial"))
            self._values["font_family"] = self.font.currentFont().family() or "Arial"
        self.font.currentFontChanged.connect(lambda font: self._set("font_family", font.family()))
        form.addRow("Font", self.font)
        self.font_style = QComboBox()
        self.font_style.addItems(["Regular", "Bold", "Italic", "Bold Italic"])
        self.font_style.setCurrentText(str(self._values["font_style"]))
        self.font_style.currentTextChanged.connect(lambda v: self._set("font_style", v))
        form.addRow("Style", self.font_style)
        self.auto_fit = QCheckBox("Fit largest caption to 90% × 80% of the project frame")
        self.auto_fit.setChecked(bool(self._values["auto_fit"]))
        self.auto_fit.toggled.connect(lambda v: self._set("auto_fit", v))
        form.addRow(self.auto_fit)
        self.font_size = QSpinBox()
        self.font_size.setRange(1, 5000)
        self.font_size.setValue(int(self._values["font_size"]))
        self.font_size.valueChanged.connect(lambda v: self._set("font_size", v))
        form.addRow("Size (project pixels)", self.font_size)
        self.alignment = QComboBox()
        self.alignment.addItems(["left", "center", "right"])
        self.alignment.setCurrentText(str(self._values["alignment"]))
        self.alignment.currentTextChanged.connect(lambda v: self._set("alignment", v))
        form.addRow("Alignment", self.alignment)
        self.tracking = self._spin("tracking", minv=0.0, maxv=5.0, step=0.05, decimals=2)
        form.addRow("Tracking", self.tracking)
        self.line_spacing = self._spin("line_spacing", minv=0.25, maxv=4.0, step=0.05, decimals=2)
        form.addRow("Line spacing", self.line_spacing)

        self.word_by_word = QCheckBox("Word-by-word (one word at a time)")
        self.word_by_word.setChecked(bool(self._values["word_by_word"]))
        self.word_by_word.toggled.connect(lambda v: self._set("word_by_word", v))
        form.addRow(self.word_by_word)
        self.hold_to_next = QCheckBox("Hold through silence until the next caption starts")
        self.hold_to_next.setChecked(bool(self._values["hold_to_next"]))
        self.hold_to_next.toggled.connect(lambda v: self._set("hold_to_next", v))
        form.addRow(self.hold_to_next)
        self.write_on = QCheckBox("Enable write-on; finish when the last word begins")
        self.write_on.setChecked(bool(self._values["write_on"]))
        self.write_on.toggled.connect(lambda v: self._set("write_on", v))
        form.addRow(self.write_on)

    def _build_layout_tab(self) -> None:
        _widget, form = self._form_tab("Layout")
        for key, label in (
            ("position_x", "Center X (0–1)"),
            ("position_y", "Center Y (0–1)"),
        ):
            control = self._spin(key, minv=0.0, maxv=1.0, step=0.01, decimals=3)
            setattr(self, key, control)
            form.addRow(label, control)
        self.layout_type = QComboBox()
        self.layout_type.addItems(["Frame", "Point"])
        self.layout_type.setCurrentText(str(self._values["layout_type"]))
        self.layout_type.currentTextChanged.connect(lambda v: self._set("layout_type", v))
        form.addRow("Layout type", self.layout_type)
        self.layout_width = self._spin("layout_width", minv=0.01, maxv=2.0, step=0.05, decimals=2)
        self.layout_height = self._spin("layout_height", minv=0.01, maxv=2.0, step=0.05, decimals=2)
        form.addRow("Layout width", self.layout_width)
        form.addRow("Layout height", self.layout_height)
        self.vertical_alignment = QComboBox()
        self.vertical_alignment.addItems(["top", "center", "bottom"])
        self.vertical_alignment.setCurrentText(str(self._values["vertical_alignment"]))
        self.vertical_alignment.currentTextChanged.connect(
            lambda v: self._set("vertical_alignment", v)
        )
        form.addRow("Vertical alignment", self.vertical_alignment)

    def _build_transform_tab(self) -> None:
        _widget, form = self._form_tab("Transform")
        for key, label in (
            ("anchor_x", "Anchor X (0–1)"),
            ("anchor_y", "Anchor Y (0–1)"),
        ):
            control = self._spin(key, minv=0.0, maxv=1.0, step=0.01, decimals=3)
            setattr(self, key, control)
            form.addRow(label, control)
        self.link_scale = QCheckBox("Link Scale X and Y")
        self.link_scale.setChecked(bool(self._values["link_scale"]))
        self.link_scale.toggled.connect(self._set_scale_link)
        form.addRow(self.link_scale)
        self.scale_x = self._spin("scale_x", minv=0.01, maxv=10.0, step=0.05, decimals=2)
        self.scale_y = self._spin("scale_y", minv=0.01, maxv=10.0, step=0.05, decimals=2)
        self.scale_x.valueChanged.connect(lambda value: self._scale_changed("scale_x", value))
        self.scale_y.valueChanged.connect(lambda value: self._scale_changed("scale_y", value))
        form.addRow("Scale X", self.scale_x)
        form.addRow("Scale Y", self.scale_y)
        form.addRow("Rotation (degrees)", self._spin("rotation", minv=-360, maxv=360, step=1, decimals=1))

    def _pick_color(self, key: str, button: QPushButton) -> None:
        color = QColorDialog.getColor(QColor(str(self._values[key])), self, "Choose color")
        if color.isValid():
            self._values[key] = color.name(QColor.NameFormat.HexRgb)
            button.setText(color.name())
            button.setStyleSheet(f"background-color: {color.name()}; color: #ffffff;")
            self._refresh_preview()

    def _color_row(self, form: QFormLayout, key: str, label: str) -> QPushButton:
        button = _color_button(str(self._values[key]))
        button.clicked.connect(lambda _checked=False, k=key, b=button: self._pick_color(k, b))
        form.addRow(label, button)
        return button

    def _build_shading_tab(self) -> None:
        _widget, form = self._form_tab("Shading")
        self._color_row(form, "text_color", "Text fill")
        self.outline_enabled = QCheckBox("Outline")
        self.outline_enabled.setChecked(bool(self._values["outline_enabled"]))
        self.outline_enabled.toggled.connect(lambda v: self._set("outline_enabled", v))
        form.addRow(self.outline_enabled)
        self._color_row(form, "outline_color", "Outline color")
        self.outline_width = self._spin("outline_width", minv=0, maxv=50, step=0.5, decimals=1)
        form.addRow("Outline width", self.outline_width)
        self.outline_opacity = self._spin("outline_opacity", minv=0, maxv=1, step=0.05, decimals=2)
        form.addRow("Outline opacity", self.outline_opacity)
        self.shadow_enabled = QCheckBox("Shadow")
        self.shadow_enabled.setChecked(bool(self._values["shadow_enabled"]))
        self.shadow_enabled.toggled.connect(lambda v: self._set("shadow_enabled", v))
        form.addRow(self.shadow_enabled)
        self._color_row(form, "shadow_color", "Shadow color")
        self.shadow_opacity = self._spin("shadow_opacity", minv=0, maxv=1, step=0.05, decimals=2)
        form.addRow("Shadow opacity", self.shadow_opacity)

    def _build_image_tab(self) -> None:
        _widget, form = self._form_tab("Image")
        self.image_opacity = self._spin("image_opacity", minv=0, maxv=1, step=0.05, decimals=2)
        form.addRow("Text output opacity", self.image_opacity)
        hint = QLabel(
            "Text+ image output controls apply to the title layer; the preview background is reference-only."
        )
        hint.setWordWrap(True)
        form.addRow(hint)

    def _refresh_preview(self, *_args) -> None:
        texts = self._word_texts if self._values.get("word_by_word") else self._caption_texts
        selected = self.preview_combo.currentData() if hasattr(self, "preview_combo") else -1
        if hasattr(self, "preview_combo"):
            desired = [f"{i + 1}. {text[:42]}" for i, text in enumerate(texts[:200])]
            current = [
                self.preview_combo.itemText(i)
                for i in range(1, self.preview_combo.count())
            ]
            if current != desired:
                self.preview_combo.blockSignals(True)
                self.preview_combo.clear()
                self.preview_combo.addItem("Longest caption", -1)
                for i, text in enumerate(texts[:200]):
                    self.preview_combo.addItem(f"{i + 1}. {text[:42]}", i)
                self.preview_combo.setCurrentIndex(0)
                self.preview_combo.blockSignals(False)
                selected = -1
        if selected == -1 or selected is None or selected >= len(texts):
            index = max(range(len(texts)), key=lambda i: len(texts[i])) if texts else 0
        else:
            index = int(selected)
        self.preview.set_preview(texts, self._values, index)
        if hasattr(self, "font_size") and self._values.get("auto_fit"):
            size = int(self.preview.settings.get("font_size", self._values["font_size"]))
            self._values["font_size"] = size
            self.font_size.blockSignals(True)
            self.font_size.setValue(size)
            self.font_size.blockSignals(False)

    def values(self) -> dict[str, Any]:
        return normalize_caption_settings(self._values)
