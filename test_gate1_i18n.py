"""Gate 1: localization engine, catalogs, hardcoded-string audit and runtime switching."""
import ast
import glob
import os
import re
import sys

import pytest

import i18n

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 1. Hardcoded user-facing strings ─────────────────────────────────────────────────────────

# Modules that are developer tools or language data, not UI.
NON_UI_MODULES = {
    "conftest.py", "benchmark_stress.py", "perf_profile.py",   # dev tools
    "search_lexicon.py",                                       # Czech/English search vocabulary
    "i18n.py",                                                 # the engine itself (native language names)
    "regions.py",                                              # place gazetteer: names per language as data
}
CZECH = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„]")   # “ is also English
DEV_CALLS = {"print", "debug", "info", "warning", "error", "exception", "log", "_log", "_dbg", "critical",
             "tr", "tr_lang", "N_", "has", "getattr", "setattr", "hasattr", "setObjectName", "setProperty",
             "property", "mark", "get", "startswith", "endswith", "replace", "split", "join", "format",
             "execute", "executemany", "connect", "findChild", "findChildren", "setStyleSheet", "fromTheme",
             "runJavaScript", "open", "environ", "Popen", "run"}
MESSAGEBOX = {"information", "warning", "question", "critical", "about"}
UI_CALLS = {
    # Qt setters / constructors
    "setText", "setToolTip", "setPlaceholderText", "setWindowTitle", "setTitle", "addItem", "addItems",
    "addAction", "addMenu", "addTab", "setTabText", "showMessage", "setAccessibleName",
    "setAccessibleDescription", "setStatusTip", "setWhatsThis", "setLabelText", "setSpecialValueText",
    "setPrefix", "setSuffix", "drawText", "QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox",
    "QAction", "QMenu", "getText", "getItem", "getInt", "getDouble", "getSaveFileName", "getOpenFileName",
    "getOpenFileNames", "getExistingDirectory", "getColor",
    # Kairos widgets / helpers
    "KButton", "NavItem", "NavSection", "SidebarNav", "Command", "show_toast", "EmptyState", "DetailPage",
    "set_title", "set_subtitle", "add_action", "set_content", "set_task", "set_status", "_status", "_row",
    "_group", "_hint", "_confirm", "_error", "_field", "_section", "_value", "_caption", "_review_button",
    "ToggleSwitch", "SegmentedControl", "Segment", "MenuSelect", "HintPill", "Toast", "Sheet", "ConfirmSheet",
    "HudIconButton", "ExportSheet", "_make_toolbar", "_make_search", "Chapter", "Tip",
}
# Words that are the same in every language (brands, standards, file formats, key names)
NEUTRAL = re.compile(
    r"^(?:Kairos|Kairos Spark|Windows Hello|ExifTool|SigLIP 2|CLIP|MP4|H\.264|ZIP|HDR|SDR|sRGB|GPS|XMP|RAM|"
    r"Live Photo|Live Photos|Google Maps|OK|Esc|Ctrl|Alt|Shift|Enter|Tab|F\d+|Iris|Ember|Moss|"
    r"[A-Z]|[A-Z]\+[A-Z]|\d+ ?(?:px|MB|GB|KB|B|s|ms|Hz|%)|[\W\d_]*)$")
IDENTIFIER = re.compile(r"^[a-z0-9_.:\-/#%(){}\[\]*?=<>!|&,;@^$\\ ]*$")


def _ok_marker(lines, lineno):
    for ln in (lineno - 1, lineno - 2):
        if 0 <= ln < len(lines) and "i18n-ok" in lines[ln]:
            return True
    return False


def _call_name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def _literal_text(node):
    if isinstance(node, ast.Constant):
        return node.value
    return "".join(v.value for v in node.values if isinstance(v, ast.Constant))


def _looks_like_text(text):
    stripped = text.strip()
    if not re.search(r"[^\W\d_]{2,}", stripped):
        return False
    if NEUTRAL.match(stripped) or IDENTIFIER.match(stripped):
        return False
    if stripped.startswith(("<", "#", "QWidget", "QLabel", "QPushButton", "background", "color:", "font")) \
            or "{" in stripped and ":" in stripped and ";" in stripped:      # markup / QSS
        return False
    return True


def find_hardcoded(path):
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    tree = ast.parse(src)
    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[c] = n
    docstrings = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
            docstrings.add(n.body[0].value)
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if n in docstrings or isinstance(parent.get(n), (ast.JoinedStr, ast.FormatSpec if hasattr(ast, "FormatSpec") else ast.JoinedStr)):
                continue
        elif isinstance(n, ast.JoinedStr):
            if isinstance(parent.get(n), (ast.JoinedStr, ast.FormattedValue)):
                continue
        else:
            continue
        text = _literal_text(n)
        if not text or _ok_marker(lines, n.lineno):
            continue
        # context: nearest enclosing call(s)
        p, ui, dev = n, False, False
        while p in parent:
            p = parent[p]
            if isinstance(p, ast.Call):
                name = _call_name(p)
                if name in DEV_CALLS and not (name in MESSAGEBOX and isinstance(p.func, ast.Attribute)
                                              and getattr(p.func.value, "id", "") == "QMessageBox"):
                    dev = True
                    break
                if name in UI_CALLS or name in MESSAGEBOX:
                    ui = True
            if isinstance(p, ast.keyword) and p.arg in ("source", "anchor", "icon", "objectName", "key", "kind"):
                dev = True                     # identifiers passed alongside UI text
                break
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                break
        if dev:
            continue
        if CZECH.search(text) or (ui and _looks_like_text(text)):
            hits.append(f"{os.path.basename(path)}:{n.lineno}: {text[:70]!r}")
    return hits


def ui_modules():
    out = []
    for f in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        name = os.path.basename(f)
        if name.startswith("test_") or name in NON_UI_MODULES:
            continue
        out.append(f)
    return out


def test_no_hardcoded_user_facing_strings():
    hits = []
    for f in ui_modules():
        hits.extend(find_hardcoded(f))
    assert not hits, f"{len(hits)} hardcoded user-facing strings:\n" + "\n".join(hits[:80])


def test_map_page_defaults_in_sync():
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import i18n_sync_pages
    src = open(os.path.join(HERE, "map_vault.html"), encoding="utf-8").read()
    m = i18n_sync_pages.MARK.search(src)
    assert m and m.group(0) == "/*I18N-DEFAULTS*/" + i18n_sync_pages.defaults_json() + "/*END*/",         "run: python tools/i18n_sync_pages.py"


def test_pages_have_no_czech_ui_text():
    """map_vault.html: visible text comes from setStrings(), comments may stay."""
    for name in ("map_vault.html",):
        src = open(os.path.join(HERE, name), encoding="utf-8").read()
        src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        src = re.sub(r"(?m)//.*$", "", src)
        bad = [ln.strip()[:90] for ln in src.splitlines() if CZECH.search(ln)]
        assert not bad, f"{name}: {bad[:10]}"


# ── 2. Catalogs ──────────────────────────────────────────────────────────────────────────────

def _ids(lang):
    return set(i18n.bundle(lang).messages)


def test_catalogs_parse_without_errors():
    i18n.reload_catalogs()
    for code in i18n.LANGUAGE_CODES:
        b = i18n.bundle(code)
        assert not b.errors, (code, b.errors[:3])
        assert len(b.messages) > 500, code


def test_every_language_covers_the_source_catalog():
    en = _ids("en")
    for code in i18n.LANGUAGE_CODES:
        missing = sorted(en - _ids(code))
        assert not missing, f"{code} misses {len(missing)}: {missing[:15]}"
        extra = sorted(_ids(code) - en)
        assert not extra, f"{code} has ids unknown to English: {extra[:15]}"


def _vars(bundle, key):
    from fluent.syntax import ast as fast
    found = set()

    def walk(node):
        if isinstance(node, fast.VariableReference):
            found.add(node.id.name)
        for v in vars(node).values() if isinstance(node, fast.BaseNode) else ():
            if isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, fast.BaseNode):
                walk(v)
    walk(bundle.messages[key].value)
    return found


def test_placeables_match_english():
    en = i18n.bundle("en")
    for code in i18n.LANGUAGE_CODES:
        b = i18n.bundle(code)
        for key in en.messages:
            assert _vars(b, key) <= _vars(en, key) | {"count"}, (code, key, _vars(b, key), _vars(en, key))
            # every translation must show the same data (counts may be implied by plural form)
            assert _vars(en, key) - _vars(b, key) <= {"count", "n", "years", "disks"}, (code, key)


def test_all_message_ids_used_in_code_exist():
    used = set()
    pat = re.compile(r"""\b(?:tr|N_|tr_lang\([^,]+,)\(\s*["']([a-z0-9][a-z0-9\-]*)["']""")
    # Indirect ids stored in tables and translated later (e.g. tokens.ACCENTS[k]["label"]) –
    # 'accent-classic' slipped through the direct-call scan above.
    label_pat = re.compile(r"""["'](?:label|label_key|title_key)["']\s*:\s*["']([a-z0-9]+-[a-z0-9\-]+)["']""")
    for f in ui_modules() + [os.path.join(HERE, "i18n.py"), os.path.join(HERE, "tokens.py")]:
        src = open(f, encoding="utf-8").read()
        used |= set(pat.findall(src)) | set(label_pat.findall(src))
    missing = sorted(used - _ids("en"))
    assert not missing, missing


# ── 3. Engine behaviour ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def lang():
    old = i18n.current()
    yield i18n.set_language
    i18n.set_language(old, retranslate=False)


@pytest.mark.parametrize("code,n,expected", [
    ("en", 1, "1 item"), ("en", 5, "5 items"),
    ("cs", 1, "1 položka"), ("cs", 3, "3 položky"), ("cs", 5, "5 položek"), ("cs", 22, "22 položek"),
    ("de", 1, "1 Objekt"), ("de", 2, "2 Objekte"),
    ("fr", 0, "0 élément"), ("fr", 2, "2 éléments"),
    ("es", 1, "1 elemento"), ("es", 7, "7 elementos"),
    ("ja", 1, "1 項目"), ("zh-CN", 3, "3 个项目"),
])
def test_plural_rules(code, n, expected):
    assert i18n.tr_lang(code, "count-items", count=n) == expected


def test_cldr_categories():
    assert [i18n.plural_category("cs", n) for n in (1, 2, 4, 5, 11, 1.5)] == ["one", "few", "few", "other", "other", "many"]
    assert i18n.plural_category("fr", 0) == "one" and i18n.plural_category("fr", 2) == "other"
    assert i18n.plural_category("ja", 1) == "other" and i18n.plural_category("zh-CN", 1) == "other"
    assert i18n.plural_category("en", 1) == "one" and i18n.plural_category("de", 1.0) == "one"


def test_missing_key_falls_back_to_english_then_to_id(lang, tmp_path, monkeypatch):
    lang("de", retranslate=False)
    b = i18n.bundle("de")
    saved = b.messages.pop("nav-library")
    try:
        assert i18n.tr("nav-library") == i18n.tr_lang("en", "nav-library") == "Library"
    finally:
        b.messages["nav-library"] = saved
    assert i18n.tr("no-such-message-id") == "no-such-message-id"
    assert "{$count}" in i18n.tr("count-items")          # missing argument never raises


def test_broken_catalog_never_raises(monkeypatch, tmp_path):
    (tmp_path / "xx").mkdir()
    (tmp_path / "xx" / "kairos.ftl").write_text("ok = fine\nbroken = { $\n", encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES_DIR", str(tmp_path))
    b = i18n.Bundle("xx")
    b.add_source((tmp_path / "xx" / "kairos.ftl").read_text(encoding="utf-8"))
    assert b.format("ok") == "fine" and b.errors


def test_normalize_and_resolve():
    assert i18n.normalize("cs_CZ") == "cs" and i18n.normalize("de-AT") == "de"
    assert i18n.normalize("zh-Hans-CN") == "zh-CN" and i18n.normalize("zh-TW") is None
    assert i18n.normalize("pt-BR") is None
    assert i18n.resolve("ja") == "ja" and i18n.resolve("xx") == "en"
    assert i18n.resolve("system") in i18n.LANGUAGE_CODES


def test_locale_formatting(lang):
    assert i18n.fmt_number(1.5, 1, "cs") == "1,5" and i18n.fmt_number(1.5, 1, "en") == "1.5"
    assert i18n.fmt_int(12345, "en") == "12,345"
    assert i18n.fmt_size(1_500_000_000, "de") == "1,5 GB"
    assert i18n.fmt_date("2025-06-14", "long", "en") == "June 14, 2025"
    assert i18n.fmt_date("2025-06-14", "long", "cs") == "14. června 2025"
    assert i18n.fmt_date("2025-06-14", "long", "ja") == "2025年6月14日"
    assert i18n.fmt_date("2025-03-01", "month_year", "cs") == "Březen 2025"
    assert i18n.fmt_date("2025-03-01", "month_year", "de") == "März 2025"
    assert i18n.fmt_date("2025-03-01", "month_year", "zh-CN") == "2025年3月"


def test_stored_data_labels_localized_at_display(lang):
    lang("en", retranslate=False)
    assert i18n.person_name("Neznámá osoba") == "Unknown Person"
    assert i18n.person_name("Jana") == "Jana"
    assert i18n.event_title("Výlet: Frauenau a okolí (09. 07. – 12. 07. 2026)") == \
        "Trip: Frauenau and surroundings (09. 07. – 12. 07. 2026)"
    assert i18n.event_title("Výlet (01. 02. 2025)") == "Trip (01. 02. 2025)"
    assert i18n.event_title("Svatba") == "Svatba"
    import events_view
    assert events_view.split_event_title("Výlet: Praha a okolí (1. 1. 2025)") == ("Trip", "Praha and surroundings")


def test_reverse_index_round_trip(lang):
    lang("en", retranslate=False)
    r = i18n.Retranslator("en", "cs")
    assert r.translate("Library") == i18n.tr_lang("cs", "nav-library")
    assert r.translate(i18n.tr_lang("en", "count-items", count=12)) == "12 položek"
    assert r.translate(i18n.tr_lang("en", "search-no-results", query="dog")) == \
        i18n.tr_lang("cs", "search-no-results", query="dog")
    assert r.translate("some user data") is None


def test_page_strings_export_plural_forms(lang):
    lang("cs", retranslate=False)
    s = i18n.page_strings("map-")
    assert s and all(isinstance(v, (str, dict)) for v in s.values())
    plural = s["count-photos"]
    assert plural["$plural"] == "count" and {"one", "few", "*"} <= set(plural["forms"])


# ── 4. Runtime switching in the real window ─────────────────────────────────────────────────

_APP = []


def _app():
    from PyQt6.QtCore import Qt, QCoreApplication
    from PyQt6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        _APP.append(QApplication(sys.argv))        # reference kept: a collected app = fatal
    return QApplication.instance()


def _visible_texts(root):
    from PyQt6.QtWidgets import QWidget
    out = []
    for w in [root] + root.findChildren(QWidget):
        for getter in ("text", "toolTip", "placeholderText"):
            fn = getattr(w, getter, None)
            if callable(fn):
                try:
                    v = fn()
                except TypeError:
                    continue
                if isinstance(v, str) and v:
                    out.append(v)
    return out


def test_runtime_switch_across_all_languages(tmp_path, monkeypatch, lang):
    import database
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "photos.db"))
    _app()
    lang("en", retranslate=False)
    import main
    win = main.MainWindow()
    try:
        nav = win.sidebar_nav
        assert nav.item("photos").text() == "Library"
        assert win.btn_settings.text() == "Settings"
        seen = {}
        for code in i18n.LANGUAGE_CODES[1:] + ("en",):
            assert win.apply_language(code) == code
            assert i18n.current() == code
            assert nav.item("photos").text() == i18n.tr_lang(code, "nav-library")
            assert nav.item("hidden").text() == i18n.tr_lang(code, "nav-locked")
            assert win.btn_import.text() == i18n.tr_lang(code, "nav-import")
            assert win.search_bar.placeholderText() == i18n.tr_lang(code, "search-placeholder")
            seen[code] = nav.item("photos").text()
        assert len(set(seen.values())) == len(i18n.LANGUAGE_CODES)
        # after a full round trip nothing Czech is left in the English UI
        leftovers = [t for t in _visible_texts(win) if CZECH.search(t)]
        assert not leftovers, leftovers[:10]
    finally:
        win.close()
        win.deleteLater()


def test_settings_language_picker(tmp_path, monkeypatch, lang):
    import database
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "photos.db"))
    _app()
    lang("en", retranslate=False)
    import settings_dialog
    dlg = settings_dialog.SettingsDialog()
    try:
        combo = dlg.combo_language
        codes = [combo.itemData(i) for i in range(combo.count())]
        assert codes == [i18n.SYSTEM] + list(i18n.LANGUAGE_CODES)
        assert [combo.itemText(i) for i in range(1, combo.count())] == [n for _c, n in i18n.LANGUAGES]
        dlg.set_language_choice("ja")
        assert dlg.selected_language() == "ja"
    finally:
        dlg.deleteLater()
