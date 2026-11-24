"""Colour maths for themes and pictures: the xterm-256 palette as RGB,
OKLab mixing (evenly spaced ramps), WCAG contrast ratios, and the nearest
xterm-256 colour to an RGB value. Standard library only, no state.

References: OKLab, Björn Ottosson (2020), https://bottosson.github.io/posts/oklab/;
WCAG 2.2 contrast ratio and relative luminance, https://www.w3.org/TR/WCAG22/#dfn-contrast-ratio.
"""

# The 16 base colours differ per terminal; these are xterm's defaults, used
# only where a picture or a measurement needs some value for them. Themes
# use indices 16-255, which every 256-colour terminal draws the same.
_BASE16 = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205),
           (0, 205, 205), (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0),
           (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]
_CUBE = [0, 95, 135, 175, 215, 255]


def xterm_rgb(n):
    """(r, g, b) 0-255 for xterm colour index n."""
    n = max(0, min(int(n), 255))
    if n < 16:
        return _BASE16[n]
    if n >= 232:
        v = 8 + 10 * (n - 232)
        return (v, v, v)
    n -= 16
    return (_CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6])


def to_hex(rgb):
    return "#%02x%02x%02x" % tuple(int(round(max(0, min(255, c)))) for c in rgb)


def from_hex(text):
    text = text.lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _unlin(c):
    c = max(0.0, min(1.0, c))
    return 255.0 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055)


def luminance(rgb):
    """WCAG relative luminance, 0 (black) to 1 (white)."""
    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG contrast ratio of two RGB colours, 1 to 21."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def to_oklab(rgb):
    r, g, b = (_lin(c) for c in rgb)
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def from_oklab(lab):
    L, a, b = lab
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (_unlin(4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_),
            _unlin(-1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_),
            _unlin(-0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_))


def mix(a, b, t):
    """RGB colour t of the way from a to b (0 = a, 1 = b), mixed in OKLab so
    equal steps look equally far apart."""
    la, lb = to_oklab(a), to_oklab(b)
    return from_oklab(tuple(x + (y - x) * t for x, y in zip(la, lb)))


def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(to_oklab(a), to_oklab(b)))


_TABLE = None


def nearest_256(rgb, floor_bg=None, floor=None, exclude=()):
    """Index (16-255) of the xterm colour closest to rgb in OKLab. With
    floor_bg and floor, only colours at least `floor`:1 against floor_bg
    qualify (falling back to the best available if none does). Indices in
    `exclude` are skipped while any other passing colour exists."""
    global _TABLE
    if _TABLE is None:
        _TABLE = [(n, xterm_rgb(n), to_oklab(xterm_rgb(n))) for n in range(16, 256)]
    if exclude:
        n = nearest_256(rgb, floor_bg, floor)
        if n not in exclude:
            return n
        target = to_oklab(rgb)
        cands = sorted((sum((x - y) ** 2 for x, y in zip(target, lab)), m) for m, crgb, lab in _TABLE
                       if m not in exclude and (floor_bg is None or contrast(crgb, floor_bg) >= floor))
        return cands[0][1] if cands else n
    target = to_oklab(rgb)
    best, best_ok = None, None
    for n, crgb, lab in _TABLE:
        d = sum((x - y) ** 2 for x, y in zip(target, lab))
        if best is None or d < best[0]:
            best = (d, n)
        if floor_bg is not None and contrast(crgb, floor_bg) >= floor and (best_ok is None or d < best_ok[0]):
            best_ok = (d, n)
    if floor_bg is not None and best_ok is not None:
        return best_ok[1]
    return best[1]


def reach(rgb, bg, floor, toward):
    """rgb moved toward `toward` (in OKLab) just far enough to reach
    `floor`:1 against bg; rgb itself when it already does."""
    if contrast(rgb, bg) >= floor:
        return rgb
    lo, hi = 0.0, 1.0
    if contrast(toward, bg) < floor:
        return toward
    for _ in range(24):
        mid = (lo + hi) / 2
        if contrast(mix(rgb, toward, mid), bg) >= floor:
            hi = mid
        else:
            lo = mid
    return mix(rgb, toward, hi)


def with_lightness(rgb, L):
    """rgb at OKLab lightness L, hue and chroma kept (chroma reduced only as
    far as needed to stay inside sRGB)."""
    _, a, b = to_oklab(rgb)
    for k in range(21):
        f = 1 - k / 20
        lab = (L, a * f, b * f)
        out = from_oklab(lab)
        # inside the gamut when nothing was clipped by _unlin
        if all(-0.5 <= c <= 255.5 for c in _raw_rgb(lab)):
            return out
    return from_oklab((L, 0.0, 0.0))


def _raw_rgb(lab):
    L, a, b = lab
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
           -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
           -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)
    return tuple(255.0 * (12.92 * c if c <= 0.0031308 else 1.055 * max(c, 0) ** (1 / 2.4) - 0.055)
                 for c in lin)


def to_floor(rgb, bg, floor, darken):
    """rgb made darker (or lighter) in OKLab lightness only, just far enough
    to reach `floor`:1 against bg; rgb itself when it already does."""
    if contrast(rgb, bg) >= floor:
        return rgb
    L0 = to_oklab(rgb)[0]
    lo, hi = (0.0, L0) if darken else (L0, 1.0)
    end = with_lightness(rgb, lo if darken else hi)
    if contrast(end, bg) < floor:
        return end
    for _ in range(24):
        mid = (lo + hi) / 2
        ok = contrast(with_lightness(rgb, mid), bg) >= floor
        if darken:
            lo, hi = (mid, hi) if ok else (lo, mid)
        else:
            lo, hi = (lo, mid) if ok else (mid, hi)
    return with_lightness(rgb, lo if darken else hi)
