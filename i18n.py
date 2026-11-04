"""Kairos localization engine (Gate 1).

English is the source language. Every user-facing string lives in a Project Fluent catalog
(locales/<lang>/kairos.ftl) and is looked up with ``tr(<message id>, **args)``.

Design
------
* Catalogs are parsed with the reference parser (``fluent.syntax``); resolution is done here so
  the runtime has no Babel dependency. Supported syntax: text, variables, string / number
  literals, message and term references, ``NUMBER()`` and select expressions with CLDR plural
  categories (``one`` / ``few`` / ``many`` / ``other``) or exact numeric keys (``[0]``).
* Fallback chain: active language -> English -> the message id itself. A missing argument
  renders as ``{$name}``. Nothing here raises: a broken catalog must never take down the UI.
* Runtime switching: ``set_language()`` swaps bundles, re-renders every visible widget string
  that came from the previous catalog (reverse lookup, see ``Retranslator``) and emits
  ``notifier().changed`` so views that compose text from data can rebuild it.
* Locale-aware formatting goes through QLocale: ``fmt_int``, ``fmt_number``, ``fmt_size``,
  ``fmt_date``, ``month_name`` ...
"""
from __future__ import annotations

import os
import re
import sys
import threading

from fluent.syntax import FluentParser, ast as fast

SOURCE_LANGUAGE = "en"

# (code, native name) – order = order in Settings
LANGUAGES = (
    ("en", "English"),
    ("cs", "Čeština"),
    ("de", "Deutsch"),
    ("es", "Español"),
    ("fr", "Français"),
    ("ja", "日本語"),
    ("zh-CN", "简体中文"),
)
LANGUAGE_CODES = tuple(code for code, _name in LANGUAGES)
SYSTEM = "system"          # config value: follow the Windows display language

_QT_LOCALE_NAMES = {"en": "en_US", "cs": "cs_CZ", "de": "de_DE", "es": "es_ES",
                    "fr": "fr_FR", "ja": "ja_JP", "zh-CN": "zh_CN"}


def _locales_dir():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "locales")


LOCALES_DIR = _locales_dir()
CATALOG_NAME = "kairos.ftl"


# ── CLDR cardinal plural rules (integers and decimals) ───────────────────────────────────────
def plural_category(lang, n):
    """CLDR cardinal category of n for lang: 'zero' | 'one' | 'two' | 'few' | 'many' | 'other'."""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return "other"
    is_int = f == int(f)
    i = int(abs(f))
    base = lang.split("-")[0]
    if base in ("ja", "zh"):
        return "other"
    if base == "cs":
        if not is_int:
            return "many"
        if i == 1:
            return "one"
        if 2 <= i <= 4:
            return "few"
        return "other"
    if base == "fr":
        if i in (0, 1):
            return "one"
        if is_int and i != 0 and i % 1_000_000 == 0:
            return "many"
        return "other"
    if base == "es":
        if is_int and i == 1:
            return "one"
        if is_int and i != 0 and i % 1_000_000 == 0:
            return "many"
        return "other"
    # en, de
    return "one" if is_int and i == 1 else "other"


# ── Bundle: parsed catalog + resolver ────────────────────────────────────────────────────────
class Bundle:
    def __init__(self, lang, source=""):
        self.lang = lang
        self.messages = {}
        self.terms = {}
        self.errors = []
        if source:
            self.add_source(source)

    def add_source(self, source):
        self.add_entries(FluentParser(with_spans=False).parse(source).body)

    def add_entries(self, body):
        for entry in body:
            if isinstance(entry, fast.Message):
                if entry.value is not None:
                    self.messages[entry.id.name] = entry
            elif isinstance(entry, fast.Term):
                self.terms[entry.id.name] = entry
            elif isinstance(entry, fast.Junk):
                self.errors.append(entry.content.strip()[:120])

    def has(self, key):
        return key in self.messages

    def format(self, key, args=None):
        msg = self.messages.get(key)
        if msg is None:
            return None
        return self._pattern(msg.value, args or {}, 0)

    # -- resolution
    def _pattern(self, pattern, args, depth):
        if depth > 12:                      # cyclic references
            return "???"
        out = []
        for el in pattern.elements:
            if isinstance(el, fast.TextElement):
                out.append(el.value)
            else:
                out.append(self._expr(el.expression, args, depth))
        return "".join(out)

    def _value(self, expr, args, depth):
        """Raw value of an expression (number stays a number – for selectors)."""
        if isinstance(expr, fast.VariableReference):
            return args.get(expr.id.name, _Missing(expr.id.name))
        if isinstance(expr, fast.NumberLiteral):
            return float(expr.value) if "." in expr.value else int(expr.value)
        if isinstance(expr, fast.StringLiteral):
            return expr.parse()["value"]
        if isinstance(expr, fast.FunctionReference) and expr.arguments.positional:
            return self._value(expr.arguments.positional[0], args, depth)
        return self._expr(expr, args, depth)

    def _expr(self, expr, args, depth):
        if isinstance(expr, fast.Placeable):
            return self._expr(expr.expression, args, depth)
        if isinstance(expr, fast.SelectExpression):
            return self._select(expr, args, depth)
        if isinstance(expr, fast.MessageReference):
            msg = self.messages.get(expr.id.name)
            if msg is None:
                return "{" + expr.id.name + "}"
            if expr.attribute is not None:
                attr = next((a for a in msg.attributes if a.id.name == expr.attribute.name), None)
                return self._pattern(attr.value, args, depth + 1) if attr else "{" + expr.id.name + "}"
            return self._pattern(msg.value, args, depth + 1)
        if isinstance(expr, fast.TermReference):
            term = self.terms.get(expr.id.name)
            if term is None:
                return "{-" + expr.id.name + "}"
            targs = dict(args)
            if expr.arguments is not None:
                for na in expr.arguments.named:
                    targs[na.name.name] = self._value(na.value, args, depth)
            return self._pattern(term.value, targs, depth + 1)
        value = self._value(expr, args, depth)
        return _display(value, self.lang)

    def _select(self, expr, args, depth):
        value = self._value(expr.selector, args, depth)
        default = None
        category = None
        for variant in expr.variants:
            if variant.default:
                default = variant
            key = variant.key
            if isinstance(key, fast.NumberLiteral):
                try:
                    if isinstance(value, (int, float)) and not isinstance(value, bool) \
                            and float(key.value) == float(value):
                        return self._pattern(variant.value, args, depth + 1)
                except ValueError:
                    pass
            else:
                name = key.name
                if isinstance(value, str) and value == name:
                    return self._pattern(variant.value, args, depth + 1)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if category is None:
                        category = plural_category(self.lang, value)
                    if name == category:
                        return self._pattern(variant.value, args, depth + 1)
        return self._pattern(default.value, args, depth + 1) if default else ""


class _Missing:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return "{$" + self.name + "}"


def _display(value, lang):
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return fmt_int(value, lang)
    if isinstance(value, float):
        return fmt_number(value, lang=lang)
    return str(value)


# ── Module state ─────────────────────────────────────────────────────────────────────────────
_lock = threading.RLock()
_bundles = {}
_current = SOURCE_LANGUAGE
_missing_reported = set()


def _load_bundle(lang):
    with _lock:
        b = _bundles.get(lang)
        if b is not None:
            return b
        b = Bundle(lang)
        path = os.path.join(LOCALES_DIR, lang, CATALOG_NAME)
        try:
            body = _cached_body(path)
            if body is not None:
                b.add_entries(body)
            else:
                with open(path, encoding="utf-8") as f:
                    resource = FluentParser(with_spans=False).parse(f.read())
                b.add_entries(resource.body)
                _store_body(path, resource.body)
        except OSError as e:
            b.errors.append(f"cannot read {path}: {e}")
        if b.errors:
            print(f"[i18n] {lang}: {len(b.errors)} catalog error(s): {b.errors[:3]}")
        _bundles[lang] = b
        return b


# Pillar 5: parsing both start-up catalogs costs ~70 ms per launch. The parsed AST is pickled
# next to the other caches, keyed by the catalog's size + mtime (+ format version); any edit to
# the .ftl simply misses the cache and re-parses.
PARSE_CACHE_VERSION = 1


def _parse_cache_path(path):
    try:
        import paths
        st = os.stat(path)
    except Exception:
        return None
    lang = os.path.basename(os.path.dirname(path))
    key = f"{lang}-{st.st_size}-{st.st_mtime_ns}-v{PARSE_CACHE_VERSION}"
    return os.path.join(paths.CACHE_ROOT, "i18n", key + ".pickle")


def _cached_body(path):
    cache = _parse_cache_path(path)
    if not cache or not os.path.exists(cache):
        return None
    try:
        import pickle
        with open(cache, "rb") as f:
            body = pickle.load(f)
        return body if isinstance(body, list) else None
    except Exception:
        return None


def _store_body(path, body):
    cache = _parse_cache_path(path)
    if not cache:
        return
    try:
        import glob
        import pickle
        folder = os.path.dirname(cache)
        os.makedirs(folder, exist_ok=True)
        lang = os.path.basename(cache).split("-", 1)[0]
        for old in glob.glob(os.path.join(folder, lang + "-*.pickle")):     # stale catalog versions
            try:
                os.remove(old)
            except OSError:
                pass
        tmp = cache + f".{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            pickle.dump(body, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, cache)
    except Exception:
        pass


def bundle(lang=None):
    return _load_bundle(lang or _current)


def reload_catalogs():
    """Drop cached bundles (tests / live catalog editing)."""
    with _lock:
        _bundles.clear()


def current():
    return _current


def tr(key, **args):
    """Localized string for message id ``key``. Falls back to English, then to the id."""
    lang = _current
    text = _load_bundle(lang).format(key, args)
    if text is None and lang != SOURCE_LANGUAGE:
        text = _load_bundle(SOURCE_LANGUAGE).format(key, args)
    if text is None:
        if key not in _missing_reported:
            _missing_reported.add(key)
            print(f"[i18n] missing message: {key}")
        return key
    return text


def N_(key):
    """Marks a message id in module-level tables; resolve with tr() where it is shown."""
    return key


def tr_lang(lang, key, **args):
    """tr() for an explicit language (exports, tests); same fallback chain."""
    text = _load_bundle(lang).format(key, args)
    if text is None and lang != SOURCE_LANGUAGE:
        text = _load_bundle(SOURCE_LANGUAGE).format(key, args)
    return key if text is None else text


def has(key, lang=None):
    return _load_bundle(lang or _current).has(key)


def native_name(code):
    return dict(LANGUAGES).get(code, code)


def normalize(code):
    """Map any locale tag ('cs_CZ', 'zh-Hans-CN', 'de-AT') to a supported code, or None."""
    if not code:
        return None
    c = code.replace("_", "-")
    if c in LANGUAGE_CODES:
        return c
    low = c.lower()
    if low.startswith("zh"):
        if any(t in low for t in ("hant", "-tw", "-hk", "-mo")):
            return None                       # Traditional Chinese not shipped yet -> English
        return "zh-CN"
    base = low.split("-")[0]
    return base if base in LANGUAGE_CODES else None


def system_language():
    """Windows display language if supported, else English."""
    try:
        from PyQt6.QtCore import QLocale
        for tag in QLocale.system().uiLanguages():
            code = normalize(tag)
            if code:
                return code
    except Exception:
        pass
    return SOURCE_LANGUAGE


def resolve(setting):
    """Config value ('system' | code) -> concrete language code."""
    if not setting or setting == SYSTEM:
        return system_language()
    return normalize(setting) or SOURCE_LANGUAGE


# ── Change notification + runtime retranslation ──────────────────────────────────────────────
_notifier = None


def notifier():
    """QObject with ``changed(str)`` – emitted after the active language changed."""
    global _notifier
    if _notifier is None:
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Notifier(QObject):
            changed = pyqtSignal(str)

        _notifier = _Notifier()
    return _notifier


def set_language(code, retranslate=True):
    """Switch the active language at runtime. Returns the code actually applied."""
    global _current
    code = normalize(code) or SOURCE_LANGUAGE
    old = _current
    _load_bundle(code)               # the English fallback loads on the first missing key (tr)
    _current = code
    _apply_qt_locale(code)
    if code != old:
        if retranslate:
            try:
                Retranslator(old, code).run()
            except Exception as e:          # never fatal
                print(f"[i18n] retranslate failed: {e}")
        try:
            notifier().changed.emit(code)
        except Exception as e:
            print(f"[i18n] change notification failed: {e}")
    return code


def _apply_qt_locale(code):
    try:
        from PyQt6.QtCore import QLocale
        QLocale.setDefault(qlocale(code))
    except Exception:
        pass


class Retranslator:
    """Re-renders strings already shown in widgets after a language switch.

    Every visible text that equals a rendering of a catalog message in the old language is
    reverse-matched (exact text for plain messages, a compiled pattern for messages with
    arguments / plural variants), its arguments recovered, and re-rendered in the new
    language. Views that build text from data additionally listen to ``notifier().changed``.
    """

    PROPS = (("text", "setText"), ("toolTip", "setToolTip"), ("placeholderText", "setPlaceholderText"),
             ("windowTitle", "setWindowTitle"), ("title", "setTitle"), ("accessibleName", "setAccessibleName"),
             ("statusTip", "setStatusTip"))

    def __init__(self, old, new):
        self.old, self.new = old, new
        self.index = reverse_index(old)

    def translate(self, text):
        """Text in the old language -> text in the new language, or None if not from the catalog."""
        if not text or not isinstance(text, str):
            return None
        hit = self.index.match(text)
        if hit is None:
            return None
        key, args = hit
        return tr_lang(self.new, key, **args)

    def _fix(self, obj, getter, setter):
        get = getattr(obj, getter, None)
        put = getattr(obj, setter, None)
        if not callable(get) or not callable(put):
            return
        try:
            value = get()
        except TypeError:
            return
        new = self.translate(value)
        if new is not None and new != value:
            put(new)

    def retranslate_object(self, obj):
        from PyQt6.QtWidgets import QComboBox, QTabWidget, QTabBar
        for getter, setter in self.PROPS:
            self._fix(obj, getter, setter)
        if isinstance(obj, QComboBox):
            for i in range(obj.count()):
                new = self.translate(obj.itemText(i))
                if new is not None:
                    obj.setItemText(i, new)
        elif isinstance(obj, QTabWidget):
            for i in range(obj.count()):
                new = self.translate(obj.tabText(i))
                if new is not None:
                    obj.setTabText(i, new)
        elif isinstance(obj, QTabBar):
            for i in range(obj.count()):
                new = self.translate(obj.tabText(i))
                if new is not None:
                    obj.setTabText(i, new)
        for action in obj.actions() if hasattr(obj, "actions") else ():
            for getter, setter in (("text", "setText"), ("toolTip", "setToolTip")):
                self._fix(action, getter, setter)
        hook = getattr(obj, "retranslate", None)
        if callable(hook):
            hook()

    def run(self):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return 0
        n = 0
        for w in app.allWidgets():
            try:
                self.retranslate_object(w)
                n += 1
            except RuntimeError:            # deleted C++ object
                continue
        return n


class _ReverseIndex:
    def __init__(self):
        self.exact = {}
        self.patterns = []      # (anchor, regex, key, numeric_groups)

    def match(self, text):
        hit = self.exact.get(text)
        if hit is not None:
            return hit, {}
        for anchor, rx, key in self.patterns:
            if anchor and anchor not in text:
                continue
            m = rx.fullmatch(text)
            if m:
                return key, {k: _parse_arg(v) for k, v in m.groupdict().items()}
        return None


def _parse_arg(v):
    """Recovered argument text -> int when it is a (locale-grouped) integer."""
    if re.fullmatch(r"\d{1,3}(?:[   ,.'’]\d{3})*|\d+", v):
        try:
            return int(re.sub(r"\D", "", v))
        except ValueError:
            pass
    return v


_reverse = {}


def reverse_index(lang):
    idx = _reverse.get(lang)
    if idx is not None:
        return idx
    b = _load_bundle(lang)
    idx = _ReverseIndex()
    for key, msg in b.messages.items():
        for parts in _expand(b, msg.value, 0):
            literal = "".join(p for p in parts if isinstance(p, str))
            groups = [p for p in parts if not isinstance(p, str)]
            if not groups:
                idx.exact.setdefault(literal, key)
                continue
            if len(literal.strip()) < 3:
                continue                   # "{ $name }" alone would match anything
            seen = set()
            rx = []
            for p in parts:
                if isinstance(p, str):
                    rx.append(re.escape(p))
                elif p.name in seen:
                    rx.append(f"(?P={p.name})")
                else:
                    seen.add(p.name)
                    rx.append(f"(?P<{p.name}>.+?)")
            anchor = max((p for p in parts if isinstance(p, str)), key=len)
            try:
                idx.patterns.append((anchor, re.compile("".join(rx), re.S), key))
            except re.error:
                pass
    idx.patterns.sort(key=lambda t: -len(t[0]))     # most specific first
    _reverse[lang] = idx
    return idx


class _Var:
    def __init__(self, name):
        self.name = name


def _expand(b, pattern, depth):
    """All literal/variable sequences a pattern can render (select variants expanded)."""
    seqs = [[]]
    if depth > 6:
        return seqs
    for el in pattern.elements:
        if isinstance(el, fast.TextElement):
            for s in seqs:
                s.append(el.value)
            continue
        e = el.expression
        while isinstance(e, fast.Placeable):
            e = e.expression
        if isinstance(e, fast.VariableReference):
            opts = [[_Var(e.id.name)]]
        elif isinstance(e, fast.FunctionReference) and e.arguments.positional \
                and isinstance(e.arguments.positional[0], fast.VariableReference):
            opts = [[_Var(e.arguments.positional[0].id.name)]]
        elif isinstance(e, fast.StringLiteral):
            opts = [[e.parse()["value"]]]
        elif isinstance(e, fast.NumberLiteral):
            opts = [[e.value]]
        elif isinstance(e, fast.SelectExpression):
            opts = []
            for v in e.variants:
                opts.extend(_expand(b, v.value, depth + 1))
        elif isinstance(e, fast.MessageReference) and e.id.name in b.messages:
            opts = _expand(b, b.messages[e.id.name].value, depth + 1)
        elif isinstance(e, fast.TermReference) and e.id.name in b.terms:
            opts = _expand(b, b.terms[e.id.name].value, depth + 1)
        else:
            opts = [[]]
        seqs = [s + o for s in seqs for o in opts][:64]
    # merge adjacent literals
    out = []
    for s in seqs:
        merged = []
        for p in s:
            if isinstance(p, str) and merged and isinstance(merged[-1], str):
                merged[-1] += p
            else:
                merged.append(p)
        out.append(merged)
    return out


# ── Locale-aware formatting (QLocale) ────────────────────────────────────────────────────────
def qlocale(lang=None):
    from PyQt6.QtCore import QLocale
    return QLocale(_QT_LOCALE_NAMES.get(lang or _current, "en_US"))


def fmt_int(n, lang=None):
    try:
        return qlocale(lang).toString(int(n))
    except Exception:
        return str(n)


def fmt_number(x, decimals=None, lang=None):
    """Locale decimal separator / grouping. decimals=None -> up to 2, trailing zeros trimmed."""
    try:
        loc = qlocale(lang)
        if decimals is None:
            s = loc.toString(float(x), "f", 2)
            dp = loc.decimalPoint()
            if dp in s:
                s = s.rstrip("0").rstrip(dp)
            return s
        return loc.toString(float(x), "f", int(decimals))
    except Exception:
        return str(x)


def fmt_size(num_bytes, lang=None):
    """Human file size in the active locale: '1.5 GB', '1,5 GB'."""
    n = float(num_bytes or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while n >= 1000 and i < len(units) - 1:
        n /= 1000.0
        i += 1
    decimals = 0 if i == 0 or n >= 100 else 1
    return tr_lang(lang or _current, "unit-size", value=fmt_number(n, decimals, lang), unit=units[i])


def _to_qdate(d):
    from PyQt6.QtCore import QDate, QDateTime
    import datetime as _dt
    if isinstance(d, QDate):
        return d
    if isinstance(d, QDateTime):
        return d.date()
    if isinstance(d, (_dt.datetime, _dt.date)):
        return QDate(d.year, d.month, d.day)
    if isinstance(d, str) and len(d) >= 10:
        try:
            return QDate(int(d[0:4]), int(d[5:7]), int(d[8:10]))
        except ValueError:
            return QDate()
    return QDate()


# skeleton -> per-language pattern (QLocale has no CLDR skeletons)
_DATE_PATTERNS = {
    "long": {"en": "MMMM d, yyyy", "cs": "d. MMMM yyyy", "de": "d. MMMM yyyy", "es": "d 'de' MMMM 'de' yyyy",
             "fr": "d MMMM yyyy", "ja": "yyyy年M月d日", "zh-CN": "yyyy年M月d日"},
    "long_weekday": {"en": "dddd, MMMM d, yyyy", "cs": "dddd d. MMMM yyyy", "de": "dddd, d. MMMM yyyy",
                     "es": "dddd, d 'de' MMMM 'de' yyyy", "fr": "dddd d MMMM yyyy",
                     "ja": "yyyy年M月d日 dddd", "zh-CN": "yyyy年M月d日 dddd"},
    "day_month": {"en": "MMMM d", "cs": "d. MMMM", "de": "d. MMMM", "es": "d 'de' MMMM", "fr": "d MMMM",
                  "ja": "M月d日", "zh-CN": "M月d日"},
    "weekday_day_month": {"en": "dddd, MMMM d", "cs": "dddd d. MMMM", "de": "dddd, d. MMMM",
                          "es": "dddd, d 'de' MMMM", "fr": "dddd d MMMM", "ja": "M月d日 dddd",
                          "zh-CN": "M月d日 dddd"},
    "month_year": {"en": "MMMM yyyy", "cs": "LLLL yyyy", "de": "MMMM yyyy", "es": "MMMM 'de' yyyy",
                   "fr": "MMMM yyyy", "ja": "yyyy年M月", "zh-CN": "yyyy年M月"},
    "short": {"en": "M/d/yyyy", "cs": "d. M. yyyy", "de": "d.M.yyyy", "es": "d/M/yyyy", "fr": "dd/MM/yyyy",
              "ja": "yyyy/MM/dd", "zh-CN": "yyyy/M/d"},
    "medium": {"en": "MMM d, yyyy", "cs": "d. M. yyyy", "de": "d. MMM yyyy", "es": "d MMM yyyy",
               "fr": "d MMM yyyy", "ja": "yyyy年M月d日", "zh-CN": "yyyy年M月d日"},
}


def fmt_date(d, style="long", lang=None):
    """Localized date. style: long | long_weekday | day_month | weekday_day_month | month_year | short | medium."""
    lang = lang or _current
    qd = _to_qdate(d)
    if not qd.isValid():
        return ""
    pat = _DATE_PATTERNS.get(style, _DATE_PATTERNS["long"]).get(lang) or _DATE_PATTERNS["long"]["en"]
    loc = qlocale(lang)
    if "LLLL" in pat:                    # standalone month name (Czech nominative: "březen 2024")
        pat = pat.replace("LLLL", "'" + month_name(qd.month(), lang, standalone=True) + "'")
    text = loc.toString(qd, pat)
    if style in ("month_year",) and lang in ("cs", "es", "fr", "de", "en"):
        text = text[:1].upper() + text[1:]
    return text


def fmt_time(t, lang=None):
    """'09:05' / QTime / datetime -> locale short time ('9:05 AM', '9:05')."""
    from PyQt6.QtCore import QTime
    import datetime as _dt
    if isinstance(t, str):
        try:
            hh, mm = int(t[0:2]), int(t[3:5])
        except ValueError:
            return t
        qt = QTime(hh, mm)
    elif isinstance(t, _dt.datetime):
        qt = QTime(t.hour, t.minute)
    elif isinstance(t, QTime):
        qt = t
    else:
        return ""
    loc = qlocale(lang)
    return loc.toString(qt, loc.timeFormat(loc.FormatType.ShortFormat).replace(":ss", ""))


def month_name(month, lang=None, standalone=True, short=False):
    """1..12 -> month name. standalone=True is the nominative ('březen'); False the genitive ('března')."""
    from PyQt6.QtCore import QLocale
    loc = qlocale(lang)
    fmt = QLocale.FormatType.ShortFormat if short else QLocale.FormatType.LongFormat
    name = loc.standaloneMonthName(int(month), fmt) if standalone else loc.monthName(int(month), fmt)
    return name


def weekday_name(iso_weekday, lang=None, short=False):
    """1 = Monday .. 7 = Sunday."""
    from PyQt6.QtCore import QLocale
    loc = qlocale(lang)
    fmt = QLocale.FormatType.ShortFormat if short else QLocale.FormatType.LongFormat
    return loc.standaloneDayName(int(iso_weekday), fmt)


def capitalize(text, lang=None):
    """Sentence-case first letter (no-op for CJK)."""
    return text[:1].upper() + text[1:] if text else text


# ── Pages (map / minimap HTML): strings pushed into JS ───────────────────────────────────────
def page_strings(prefix):
    """All messages whose id starts with prefix, rendered with JS-side placeholders kept.

    Messages with variables are exported as Fluent source-ish templates the page fills with
    its own tiny formatter: '{n}' placeholders and plural variants as {"one": ..., "other": ...}.
    """
    b = _load_bundle(_current)
    en = _load_bundle(SOURCE_LANGUAGE)
    out = {}
    for key in sorted(set(en.messages) | set(b.messages)):
        if not key.startswith(prefix):
            continue
        src = b if key in b.messages else en
        out[key[len(prefix):]] = _js_template(src, src.messages[key].value)
    return out


def _js_template(b, pattern):
    # a single select on a variable at top level -> plural dict; otherwise plain template
    sel = None
    for el in pattern.elements:
        if isinstance(el, fast.Placeable) and isinstance(el.expression, fast.SelectExpression):
            sel = el.expression
    if sel is None:
        return _js_flat(b, pattern)
    variants = {}
    for v in sel.variants:
        name = v.key.value if isinstance(v.key, fast.NumberLiteral) else v.key.name
        parts = []
        for el in pattern.elements:
            if el is not None and isinstance(el, fast.Placeable) and el.expression is sel:
                parts.append(_js_flat(b, v.value))
            elif isinstance(el, fast.TextElement):
                parts.append(el.value)
            else:
                parts.append(_js_flat(b, fast.Pattern([el])))
        variants["=" + name if isinstance(v.key, fast.NumberLiteral) else name] = "".join(parts)
        if v.default:
            variants["*"] = "".join(parts)
    var = sel.selector
    if isinstance(var, fast.FunctionReference):
        var = var.arguments.positional[0]
    return {"$plural": var.id.name if isinstance(var, fast.VariableReference) else "n", "lang": b.lang,
            "forms": variants}


def _js_flat(b, pattern):
    out = []
    for el in pattern.elements:
        if isinstance(el, fast.TextElement):
            out.append(el.value)
            continue
        e = el.expression
        if isinstance(e, fast.VariableReference):
            out.append("{" + e.id.name + "}")
        elif isinstance(e, fast.FunctionReference) and e.arguments.positional:
            out.append("{" + e.arguments.positional[0].id.name + "}")
        elif isinstance(e, fast.StringLiteral):
            out.append(e.parse()["value"])
        elif isinstance(e, fast.TermReference) and e.id.name in b.terms:
            out.append(_js_flat(b, b.terms[e.id.name].value))
        elif isinstance(e, fast.MessageReference) and e.id.name in b.messages:
            out.append(_js_flat(b, b.messages[e.id.name].value))
    return "".join(out)


def init_from_config(cfg):
    """Startup: apply the configured language (no retranslation – nothing is built yet)."""
    return set_language(resolve((cfg or {}).get("language", SYSTEM)), retranslate=False)


# ── Data written by older builds / the scanner in the canonical (Czech) form ─────────────────
# The library stores a few generated labels verbatim (auto event titles, the default person
# name, the unknown-place marker). They are data, not UI: they stay as stored and are
# localized here at display time, so switching language never rewrites the database.
STORED_UNKNOWN_PERSON = "Neznámá osoba"  # i18n-ok: stored data marker
STORED_UNKNOWN_PLACE = "Neznámé místo"  # i18n-ok: stored data marker
STORED_TRIP = "Výlet"  # i18n-ok: stored data marker
_TRIP_AREA = re.compile(r"^(.+?) a okolí$")  # i18n-ok: stored data pattern


def is_unknown_person(name):
    n = (name or "").strip().lower()
    return not n or n.startswith(STORED_UNKNOWN_PERSON.lower()) or n.startswith("unknown")


def person_name(name):
    """Stored person name -> display name ('Neznámá osoba' -> 'Unknown Person')."""
    return tr("person-unknown") if is_unknown_person(name) else name


def place_name(name):
    if not name or name == STORED_UNKNOWN_PLACE:
        return tr("place-unknown")
    return name


def event_kicker(kicker):
    """'Výlet' prefix of a stored auto title -> localized kicker."""
    return tr("event-kicker-trip") if kicker == STORED_TRIP else kicker


def event_place(text):
    """'Frauenau a okolí' -> 'Frauenau and surroundings'; unknown place marker localized."""
    m = _TRIP_AREA.match(text or "")
    if m:
        return tr("event-city-area", city=m.group(1))
    import regions                       # natural region (trips.region_name): "Šumava" -> UI language
    reg = regions.localized(text, current())
    if isinstance(reg, tuple):
        return tr("event-region-pair", a=reg[0], b=reg[1])
    if reg:
        return reg
    if text == STORED_UNKNOWN_PLACE:
        return tr("place-unknown")
    return text


def event_title(title):
    """Full stored event title localized for display (dates kept as stored)."""
    t = (title or "").strip()
    m = re.match(r"^" + STORED_TRIP + r"(?:: (.+?))?(\s*\([^()]*\))?$", t)
    if not m:
        return t
    place, dates = m.group(1), (m.group(2) or "")
    head = tr("event-kicker-trip")
    return (f"{head}: {event_place(place)}" if place else head) + dates
