"""Code 128 barcode as a self-contained SVG (no inline styles, safe under our CSP)."""
from reportlab.graphics.barcode import code128
from xml.sax.saxutils import escape

_WIDTH = {"a": 1, "b": 2, "c": 3, "d": 4}


def code128_svg(value, module=2, height=60, quiet=10):
    c = code128.Code128(value, barWidth=1)
    c.validate()
    c.encode()
    c.decompose()
    x = quiet
    bars = []
    for ch in c.decomposed:
        w = _WIDTH[ch.lower()]
        if ch.isupper():
            bars.append('<rect x="%d" y="0" width="%d" height="%d" fill="#000"/>' % (x * module, w * module, height))
        x += w
    total = (x + quiet) * module
    label_h = 16
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-label="Barcode %s">'
            '<rect width="100%%" height="100%%" fill="#fff"/>%s'
            '<text x="%d" y="%d" font-family="monospace" font-size="13" text-anchor="middle" fill="#000">%s</text></svg>'
            % (total, height + label_h, total, height + label_h, escape(value), "".join(bars), total // 2, height + 13, escape(value)))
