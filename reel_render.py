"""Throwback Reels – vykreslení snímku sestřihu v čase t (jediný renderer pro přehrávač i export).

Přehrávač (reels_view.ReelPlayer) i export MP4 (reel_export) volají ReelRenderer.render(painter,
w, h, t) nad stejným plánem (reels.plan) – náhled v aplikaci a video jsou snímek po snímku
totéž. Pohyb výřezu (Ken Burns) a časování střihů počítá Director (director.py).

Záběr „fill“: výřez fotky 16:9 jede po dráze kb (k tvářím). Záběr „contain“ (fotka na výšku):
celá fotka uprostřed na rozmazané a ztmavené kopii sebe sama, s jemným přítahem.
Střih: na běžné době krátké prolnutí, na těžké době delší (plan: seg["fade"]).
"""
from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPen

import director

ASPECT = 16 / 9
BG = QColor(9, 9, 13)
BLUR_EDGE = 48                # rozmazané pozadí: zmenšit na tolik px a zase zvětšit
TITLE_FADE_S = 0.9


def _font(px, weight=QFont.Weight.DemiBold):
    f = QFont("Segoe UI Variable Display")
    f.setFamilies(["Segoe UI Variable Display", "Segoe UI", "Arial"])
    f.setPixelSize(max(6, int(px)))
    f.setWeight(weight)
    return f


def _blurred(img):
    if img.isNull():
        return img
    small = img.scaled(BLUR_EDGE, BLUR_EDGE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return small.scaled(small.width() * 8, small.height() * 8, Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)


class ReelRenderer:
    def __init__(self, plan, max_edge=2048, loader=None):
        self.plan = plan
        self.segs = plan["segs"]
        self.max_edge = int(max_edge)
        self.loader = loader
        self.images = {}             # id -> QImage
        self.blurs = {}              # id -> QImage (jen záběry „contain“ a titulek)

    def load_all(self, should_stop=None, progress=None):
        """Načte obrázky záběrů (náhled 2048 px, když existuje). Mimo GUI vlákno – jen QImage."""
        load = self.loader
        if load is None:
            from viewer_hud import load_image_file as load
        shots = [g for g in self.segs if g["kind"] == "shot"]
        for k, g in enumerate(shots):
            if should_stop and should_stop():
                return False
            if g["id"] in self.images:
                continue
            img = load(g["path"], QSize(self.max_edge, self.max_edge)) if g.get("path") else QImage()
            if img.isNull() and g.get("thumb"):
                img = load(g["thumb"], QSize(self.max_edge, self.max_edge))
            img = img.convertToFormat(QImage.Format.Format_RGB32) if not img.isNull() else img
            self.images[g["id"]] = img
            if g.get("fit") == "contain" or k == 0:
                self.blurs[g["id"]] = _blurred(img)
            if progress:
                progress(k + 1, len(shots))
        return True

    # ── kreslení ──
    def render(self, p, w, h, t):
        """Snímek v čase t do obdélníku 0,0–w,h (painter p)."""
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.fillRect(QRectF(0, 0, w, h), BG)
        segs = self.segs
        t = max(0.0, min(self.plan["total"], t))
        i = director.index_at(segs, t)
        seg = segs[i]
        u = t - seg["t0"]
        fade = seg.get("fade", 0.0)
        if i > 0 and fade > 0 and u < fade:
            self._draw(p, segs[i - 1], t, w, h)
            p.setOpacity(director.smootherstep(u / fade))
            self._draw(p, seg, t, w, h)
            p.setOpacity(1.0)
        else:
            self._draw(p, seg, t, w, h)

    def _draw(self, p, seg, t, w, h):
        kind = seg["kind"]
        if kind == "shot":
            self._shot(p, seg, t, w, h)
        elif kind == "title":
            self._title(p, seg, t, w, h)
        else:
            self._end(p, seg, t, w, h)

    def _shot(self, p, seg, t, w, h):
        img = self.images.get(seg["id"])
        if img is None or img.isNull():
            return
        u = (t - seg["t0"]) / max(1e-6, seg["dur"])
        cx, cy, s = director.kb_at(seg["kb"], u)
        iw, ih = img.width(), img.height()
        if seg.get("fit") == "contain":
            blur = self.blurs.get(seg["id"])
            if blur is not None and not blur.isNull():
                self._cover(p, blur, w, h)
                p.fillRect(QRectF(0, 0, w, h), QColor(0, 0, 0, 120))
            # celá fotka na výšku s jemným přítahem (plán: kb pro výstup 16:9 -> s = 1 / přiblížení)
            zoom = max(1.0, min(1.15, 1.0 / max(0.5, s)))
            ph = h * 0.94 * zoom
            pw = ph * iw / ih
            p.drawImage(QRectF((w - pw) / 2 + (0.5 - cx) * pw * 0.1, (h - ph) / 2 + (0.5 - cy) * ph * 0.1, pw, ph), img)
        else:
            sh = s * ih
            sw = sh * (w / h)
            if sw > iw:                        # užší fotka než výstup: výška podle šířky
                sw = iw
                sh = sw * h / w
            src = QRectF(cx * iw - sw / 2, cy * ih - sh / 2, sw, sh)
            src.moveLeft(max(0.0, min(iw - sw, src.left())))
            src.moveTop(max(0.0, min(ih - sh, src.top())))
            p.drawImage(QRectF(0, 0, w, h), img, src)
        cap = seg.get("caption")
        if cap:
            self._caption(p, cap, t - seg["t0"], seg["dur"], w, h)

    @staticmethod
    def _cover(p, img, w, h):
        iw, ih = img.width(), img.height()
        k = max(w / iw, h / ih)
        p.drawImage(QRectF((w - iw * k) / 2, (h - ih * k) / 2, iw * k, ih * k), img)

    def _caption(self, p, text, u, dur, w, h):
        a = director.smoothstep(u / 0.5) * director.smoothstep((dur - u) / 0.5)
        if a <= 0:
            return
        grad = QLinearGradient(0, h * 0.72, 0, h)
        grad.setColorAt(0, QColor(0, 0, 0, 0))
        grad.setColorAt(1, QColor(0, 0, 0, int(150 * a)))
        p.fillRect(QRectF(0, h * 0.72, w, h * 0.28), grad)
        p.setFont(_font(h * 0.036, QFont.Weight.Medium))
        p.setPen(QColor(255, 255, 255, int(235 * a)))
        p.drawText(QRectF(w * 0.05, h * 0.82, w * 0.9, h * 0.1), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)

    def _backdrop(self, p, w, h, dim):
        first = next((g for g in self.segs if g["kind"] == "shot"), None)
        blur = self.blurs.get(first["id"]) if first else None
        if blur is not None and not blur.isNull():
            self._cover(p, blur, w, h)
            p.fillRect(QRectF(0, 0, w, h), QColor(9, 9, 13, dim))

    def _title(self, p, seg, t, w, h):
        self._backdrop(p, w, h, 150)
        u = t - seg["t0"]
        a = director.smoothstep(u / TITLE_FADE_S)
        rise = (1 - director.smootherstep(u / 1.6)) * h * 0.03
        p.setPen(QColor(255, 255, 255, int(255 * a)))
        p.setFont(_font(h * 0.072, QFont.Weight.Bold))
        p.drawText(QRectF(w * 0.08, h * 0.36 + rise, w * 0.84, h * 0.18),
                   int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), self.plan.get("title", ""))
        sub = self.plan.get("subtitle", "")
        if sub:
            b = director.smoothstep((u - 0.5) / TITLE_FADE_S)
            p.setPen(QColor(255, 255, 255, int(190 * b)))
            p.setFont(_font(h * 0.034, QFont.Weight.Normal))
            p.drawText(QRectF(w * 0.08, h * 0.56 + rise, w * 0.84, h * 0.08), int(Qt.AlignmentFlag.AlignCenter), sub)
        p.setPen(QPen(QColor(255, 255, 255, int(110 * a)), max(1.0, h / 540)))
        p.drawLine(int(w * 0.46), int(h * 0.555 + rise), int(w * 0.54), int(h * 0.555 + rise))

    def _end(self, p, seg, t, w, h):
        self._backdrop(p, w, h, 200)
        u = t - seg["t0"]
        a = director.smoothstep(u / TITLE_FADE_S) * director.smoothstep((seg["dur"] - u) / 0.8 + 0.25)
        p.setPen(QColor(255, 255, 255, int(235 * a)))
        p.setFont(_font(h * 0.05, QFont.Weight.DemiBold))
        p.drawText(QRectF(0, h * 0.40, w, h * 0.1), int(Qt.AlignmentFlag.AlignCenter), self.plan.get("title", ""))
        rng = self.plan.get("range")
        if rng:
            p.setPen(QColor(255, 255, 255, int(170 * a)))
            p.setFont(_font(h * 0.03, QFont.Weight.Normal))
            p.drawText(QRectF(0, h * 0.50, w, h * 0.07), int(Qt.AlignmentFlag.AlignCenter), _range_text(rng))
        p.setPen(QColor(255, 255, 255, int(120 * a)))
        p.setFont(_font(h * 0.024, QFont.Weight.Medium))
        p.drawText(QRectF(0, h * 0.86, w, h * 0.06), int(Qt.AlignmentFlag.AlignCenter), "Kairos")

    def frame(self, t, w, h):
        """Samostatný snímek jako QImage (export, testy)."""
        img = QImage(w, h, QImage.Format.Format_RGB32)      # stejný formát jako zdroje: bez převodu (60 -> 2 ms)
        p = QPainter(img)
        try:
            self.render(p, w, h, t)
        finally:
            p.end()
        return img


def _range_text(rng):
    a, b = (str(x or "")[:10] for x in rng)

    def cz(s):
        y, m, d = s.split("-") if s.count("-") == 2 else ("", "", "")
        return f"{int(d)}. {int(m)}. {y}" if y else s
    return cz(a) if a == b else f"{cz(a)} – {cz(b)}"
