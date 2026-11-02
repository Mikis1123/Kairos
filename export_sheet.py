"""Stage 4 – list „Exportovat“ (volby soukromí a balení) a vlákno exportu (privacy_export.py drží logiku).

ExportSheet je list v okně (ui_sheets.Sheet): přepínače „Odstranit polohu“, „Odstranit jména osob“,
„Odstranit zařízení a autora“, „Neutrální názvy souborů“, „Pohybová videa Live Photos“ a dva
segmentové přepínače Obsah (Soubory / Webová prezentace) a Balení (Složka / ZIP). Poslední volby
se pamatují v configu (export_options), soukromí je výchozí.
"""
from i18n import tr
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

import privacy_export as pe
from ui_controls import SegmentedControl, ToggleSwitch
from ui_sheets import Sheet

OPTION_KEYS = ("strip_gps", "strip_people", "strip_camera", "neutral_names", "include_live", "layout", "package")


def options_from_config(config, title="Export"):
    saved = (config or {}).get("export_options") or {}
    opts = pe.ExportOptions(title=title)
    for k in OPTION_KEYS:
        if k in saved and isinstance(saved[k], type(getattr(opts, k))):
            setattr(opts, k, saved[k])
    if opts.layout not in ("files", "web"):
        opts.layout = "files"
    if opts.package not in ("folder", "zip"):
        opts.package = "folder"
    return opts


def options_to_config(opts):
    return {k: getattr(opts, k) for k in OPTION_KEYS}


def _row(label, control):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 2, 0, 2)
    lbl = QLabel(label)
    lbl.setObjectName("sheetBody")
    lay.addWidget(lbl)
    lay.addStretch(1)
    lay.addWidget(control)
    return w


class ExportSheet(Sheet):
    def __init__(self, host, count, opts, exiftool_ok=True):
        body = tr("export-sheet-body", count=count)
        super().__init__(host, tr("action-export"), body, tr("action-export-ellipsis"))
        self.opts = opts
        self.t_gps = ToggleSwitch(tr("export-strip-location"))
        self.t_people = ToggleSwitch(tr("export-strip-people"))
        self.t_camera = ToggleSwitch(tr("export-strip-device"))
        self.t_names = ToggleSwitch(tr("export-neutral-names"))
        self.t_live = ToggleSwitch(tr("export-include-live"))
        for t, on in ((self.t_gps, opts.strip_gps), (self.t_people, opts.strip_people),
                      (self.t_camera, opts.strip_camera), (self.t_names, opts.neutral_names),
                      (self.t_live, opts.include_live)):
            t.setChecked(bool(on))
            self.content.addWidget(t)
        self.seg_layout = SegmentedControl([(tr("export-format-files"), "files"), (tr("export-format-web"), "web")], current=opts.layout)
        self.seg_package = SegmentedControl([(tr("export-format-folder"), "folder"), ("ZIP", "zip")], current=opts.package)
        self.content.addWidget(_row(tr("export-content"), self.seg_layout))
        self.content.addWidget(_row(tr("export-packaging"), self.seg_package))
        self.lbl_note = QLabel()
        self.lbl_note.setObjectName("sheetBody")
        self.lbl_note.setWordWrap(True)
        self.content.addWidget(self.lbl_note)
        self.exiftool_ok = exiftool_ok
        for t in (self.t_gps, self.t_people, self.t_camera):
            t.toggled.connect(self._sync)
        self.seg_layout.changed.connect(lambda _k: self._sync())
        self._sync()

    @staticmethod
    def _checked_key(seg):
        for s in seg.segments:
            if s.isChecked():
                return s.key
        return seg.segments[0].key

    def _sync(self):
        web = self._checked_key(self.seg_layout) == "web"
        self.t_live.setEnabled(not web)
        strips = self.t_gps.isChecked() or self.t_people.isChecked() or self.t_camera.isChecked()
        if strips and not self.exiftool_ok:
            note = tr("export-exiftool-missing")
        elif strips:
            note = tr("export-verified-note")
        else:
            note = tr("export-keep-all-note")
        if web:
            note += " " + tr("export-web-note")
        self.lbl_note.setText(note)
        self.btn_action.setEnabled(not (strips and not self.exiftool_ok))

    def chosen(self):
        o = pe.ExportOptions(title=self.opts.title)
        o.strip_gps = self.t_gps.isChecked()
        o.strip_people = self.t_people.isChecked()
        o.strip_camera = self.t_camera.isChecked()
        o.neutral_names = self.t_names.isChecked()
        o.include_live = self.t_live.isChecked()
        o.layout = self._checked_key(self.seg_layout)
        o.package = self._checked_key(self.seg_package)
        return o


def ask_options(host, count, opts, exiftool_ok=True):
    """Ukáže list; vrací zvolené ExportOptions, nebo None (zrušeno)."""
    sheet = ExportSheet(host, count, opts, exiftool_ok)
    value = []
    sheet.done.connect(lambda ok: value.append(sheet.chosen() if ok else None))
    sheet.exec()
    return value[0] if value else None


class PrivacyExportJob(QThread):
    progress = pyqtSignal(float)
    finished_ok = pyqtSignal(object)            # ExportResult
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, items, opts, dest, names, parent=None):
        super().__init__(parent)
        self.items, self.opts, self.dest, self.names = items, opts, dest, names
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            res = pe.export(self.items, self.opts, self.dest, self.names, progress=self.progress.emit,
                            cancelled=lambda: self._stop)
            self.finished_ok.emit(res)
        except pe.ExportCancelled:
            self.cancelled.emit()
        except Exception as e:
            self.failed.emit(str(e))
