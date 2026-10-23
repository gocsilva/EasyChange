"""Keyword alternation must match the legacy independent regular expressions."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

from easychange.ui.editor import BasicHighlighter


KEYWORDS = """and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield public private protected static void int string var const let function interface namespace using""".split()


def _matches(expression, text):
    found = set()
    it = expression.globalMatch(text)
    while it.hasNext():
        match = it.next()
        found.add((match.capturedStart(), match.capturedLength()))
    return found


def test_grouped_keywords_match_every_legacy_token_and_boundary():
    app = QApplication.instance() or QApplication([])
    highlighter = BasicHighlighter(QTextDocument())
    assert len(highlighter.rules) == 5  # keywords, comments (2), strings, numbers
    grouped = QRegularExpression(highlighter.rules[0][0])
    assert grouped.isValid()
    cases = [
        " ".join(KEYWORDS),
        "classical assertive input intersection int interface",
        "def _foo public static void var const let",
        "λclass Unicode_using nonlocal await False",
        "  return return; CLASS Class class",
        "class / def # string \"async\" 100 await",
    ]
    for text in cases:
        legacy = set()
        for keyword in KEYWORDS:
            legacy |= _matches(QRegularExpression(r"\b" + keyword + r"\b"), text)
        assert _matches(grouped, text) == legacy


def test_keyword_grouping_preserves_formatter_priority():
    app = QApplication.instance() or QApplication([])
    for line in [
        'class A: # return 123',
        'public int length = 20; // const input',
        'str = "async while 42"; return str',
        'if x == True: return "public static"',
    ]:
        doc = QTextDocument()
        doc.setPlainText(line)
        highlighter = BasicHighlighter(doc)
        highlighter.rehighlight()
        actual = [
            (rng.start, rng.length, rng.format.foreground().color().name(),
             rng.format.fontWeight())
            for rng in doc.firstBlock().layout().formats()
        ]
        expected_styles = [None] * len(line)
        keyword_format = highlighter.rules[0][1]
        reference = [
            (QRegularExpression(r"\b" + word + r"\b"), keyword_format)
            for word in KEYWORDS
        ] + [
            (QRegularExpression(expr), fmt) for expr, fmt in highlighter.rules[1:]
        ]
        for regex, fmt in reference:
            for start, width in _matches(regex, line):
                for idx in range(start, start + width):
                    expected_styles[idx] = (fmt.foreground().color().name(),
                                             fmt.fontWeight())
        actual_styles = [None] * len(line)
        for start, width, color, weight in actual:
            for idx in range(start, start + width):
                actual_styles[idx] = (color, weight)
        assert actual_styles == expected_styles
