from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QTextCharFormat, QSyntaxHighlighter, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:
        self.editor.paint_line_numbers(event)


class BasicHighlighter(QSyntaxHighlighter):
    def __init__(self, document, *, high_contrast: bool = False) -> None:
        super().__init__(document)
        self.rules: list[tuple[str, QTextCharFormat]] = []
        keyword = QTextCharFormat(); keyword.setForeground(QColor("#6aa9ff" if not high_contrast else "#ffffff")); keyword.setFontWeight(QFont.Weight.Bold)
        string = QTextCharFormat(); string.setForeground(QColor("#4caf79" if not high_contrast else "#d5f5a5"))
        comment = QTextCharFormat(); comment.setForeground(QColor("#84909b" if not high_contrast else "#c4cbd2"))
        number = QTextCharFormat(); number.setForeground(QColor("#d39bff" if not high_contrast else "#ffe29a"))
        for word in "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield public private protected static void int string var const let function interface namespace using".split():
            self.rules.append((rf"\b{word}\b", keyword))
        self.rules.extend([(r"#[^\n]*", comment), (r"//[^\n]*", comment), (r"\"([^\"\\]|\\.)*\"|'([^'\\]|\\.)*'", string), (r"\b\d+(?:\.\d+)?\b", number)])

    def highlightBlock(self, text: str) -> None:
        from PySide6.QtCore import QRegularExpression
        for expression, fmt in self.rules:
            matches = QRegularExpression(expression).globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


class CodeEditor(QPlainTextEdit):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)
        self.update_line_number_area_width(0)
        self.highlighter = BasicHighlighter(self.document())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.highlight_current_line()

    def line_number_area_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy: self.line_number_area.scroll(0, dy)
        else: self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()): self.update_line_number_area_width(0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        content = self.contentsRect()
        self.line_number_area.setGeometry(QRect(content.left(), content.top(), self.line_number_area_width(), content.height()))

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self.line_number_area)
        palette = self.palette()
        painter.fillRect(event.rect(), palette.alternateBase())
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        painter.setPen(palette.text().color())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(0, top, self.line_number_area.width() - 5, self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next(); top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            number += 1

    def highlight_current_line(self) -> None:
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(self.palette().alternateBase())
        selection.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor(); selection.cursor.clearSelection()
        self.setExtraSelections([selection])

    def goto_line(self, line: int, column: int = 1) -> None:
        cursor = QTextCursor(self.document().findBlockByNumber(max(0, line - 1)))
        cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor, max(0, column - 1))
        self.setTextCursor(cursor); self.centerCursor()
