"""Regression: syntax highlighting must reuse compiled expressions per block."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

from easychange.ui.editor import BasicHighlighter


def test_compiled_rules_are_reused_across_document_rehighlights():
    app = QApplication.instance() or QApplication([])
    document = QTextDocument()
    highlighter = BasicHighlighter(document)
    compiled = highlighter._compiled_rules
    assert len(compiled) == len(highlighter.rules)
    assert [expression.pattern() for expression, _ in compiled] == [pattern for pattern, _ in highlighter.rules]
    document.setPlainText("class Example:\n    def method(self):\n        return 42\n" * 100)
    highlighter.rehighlight()
    assert highlighter._compiled_rules is compiled
    assert [expression.pattern() for expression, _ in compiled] == [pattern for pattern, _ in highlighter.rules]
    assert any(block.layout().formats() for block in (document.firstBlock(), document.firstBlock().next()))
