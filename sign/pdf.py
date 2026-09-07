"""PDF engine: page sizes, page rendering, anchor-based field placement,
signature stamping, and the certificate of completion."""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import os
import threading
import zoneinfo
from typing import Dict, List, Optional, Tuple

import pdfplumber
import pypdfium2 as pdfium
import reportlab
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import config
from .models import Envelope, SigField, Signer

# Default field geometry in PDF points (1/72 in).
SIG_W, DATE_W, INITIALS_W, CHECK_W = 216.0, 115.0, 60.0, 12.0
FIELD_H = 30.0
LIFT = 34.0  # distance from an anchor label's top up to the top of the ink box
BOX_GLYPHS = ("☐", "□", "☑", "▢")  # ☐ □ ☑ ▢ drawn in the document as checkboxes


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------- inspection
def page_sizes(pdf_bytes: bytes) -> List[List[float]]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    out = []
    for page in reader.pages:
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        if int(page.get("/Rotate", 0) or 0) % 180 == 90:
            w, h = h, w
        out.append([w, h])
    return out


_RENDER_LOCK = threading.Lock()  # pdfium is not thread-safe; FastAPI sync handlers run in a thread pool


def render_pages(pdf_bytes: bytes, dpi: int) -> List[bytes]:
    with _RENDER_LOCK:
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            out = []
            for i in range(len(doc)):
                page = doc[i]
                bitmap = page.render(scale=dpi / 72.0)
                image = bitmap.to_pil().convert("RGB")
                buf = io.BytesIO()
                image.save(buf, "PNG", optimize=True)
                out.append(buf.getvalue())
                page.close()
            return out
        finally:
            doc.close()


# ----------------------------------------------------------------------------- auto placement
def _match_signer(role_text: str, index: int, ordered: List[Signer]) -> Optional[Signer]:
    r = role_text.lower().strip()
    if r:
        for s in ordered:
            sr = s.role.lower().strip()
            if sr and (sr in r or r in sr):
                return s
    return ordered[index] if index < len(ordered) else None


def _signer_in_text(text: str, ordered: List[Signer]) -> Signer:
    """The signer named (or whose role is named) in a line of text, else the first signer."""
    t = text.lower()
    for s in ordered:
        if s.name.lower() in t:
            return s
    for s in ordered:
        if s.role and s.role.lower() in t:
            return s
    return ordered[0]


def auto_place(pdf_bytes: bytes, signers: List[Signer]) -> List[SigField]:
    """Find signature blocks laid out as an ALL-CAPS heading ending in SIGNATURE
    (e.g. TENANT SIGNATURE) followed by small 'Signature' / 'Date' labels under
    the lines, and place a field above each label for the matching signer.
    Checkbox glyphs (☐) become required checkbox fields for the signer named on
    that line; boxes on the same line form one choice group."""
    ordered = sorted(signers, key=lambda s: s.order)
    fields: List[SigField] = []
    if not ordered:
        return fields
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            words = page.extract_words(extra_attrs=["size"])
            for c in page.chars:
                if c["text"] not in BOX_GLYPHS:
                    continue
                line = " ".join(w["text"] for w in words if abs(w["top"] - c["top"]) < 4)
                signer = _signer_in_text(line, ordered)
                size = max(float(c["x1"]) - float(c["x0"]), float(c["bottom"]) - float(c["top"]), 9.0)
                fields.append(SigField(signer_id=signer.id, kind="checkbox", page=pno,
                                       x=float(c["x0"]), y=float(c["top"]), w=size, h=size,
                                       group=f"p{pno}-{int(c['top'])}", required=True))
            lines: Dict[int, list] = {}
            for w in words:
                lines.setdefault(int(w["top"] // 3), []).append(w)
            headings: List[Tuple[float, str]] = []
            for ws in lines.values():
                ws.sort(key=lambda w: w["x0"])
                text = " ".join(w["text"] for w in ws)
                if "SIGNATURE" in text and text == text.upper() and len(ws) <= 4:
                    role = text.replace("SIGNATURE", "").replace(":", "").strip()
                    headings.append((min(w["top"] for w in ws), role))
            if not headings:
                continue
            headings.sort()
            for w in words:
                if w["size"] > 9.5:
                    continue
                if w["text"] == "Signature":
                    kind, width = "signature", SIG_W
                elif w["text"] == "Date":
                    kind, width = "date", DATE_W
                elif w["text"] == "Initials":
                    kind, width = "initials", INITIALS_W
                else:
                    continue
                above = [h for h in headings if h[0] < w["top"]]
                if not above:
                    continue
                signer = _match_signer(above[-1][1], len(above) - 1, ordered)
                if signer is None:
                    continue
                x = float(w["x0"])
                width = min(width, float(page.width) - x - 18)
                y = max(0.0, float(w["top"]) - LIFT)
                fields.append(SigField(signer_id=signer.id, kind=kind, page=pno, x=x, y=y, w=width, h=FIELD_H))
    return fields


# ----------------------------------------------------------------------------- stamping
_SIG_FONT: Optional[str] = None


def signature_font() -> str:
    global _SIG_FONT
    if _SIG_FONT:
        return _SIG_FONT
    path = os.path.join(os.path.dirname(reportlab.__file__), "fonts", "VeraIt.ttf")
    try:
        pdfmetrics.registerFont(TTFont("SigScript", path))
        _SIG_FONT = "SigScript"
    except Exception:
        _SIG_FONT = "Helvetica-Oblique"
    return _SIG_FONT


def local_dt(iso: Optional[str], tz: str) -> Optional[dt.datetime]:
    if not iso:
        return None
    try:
        zone = zoneinfo.ZoneInfo(tz)
    except Exception:
        zone = dt.timezone.utc
    return dt.datetime.fromisoformat(iso).astimezone(zone)


def date_str(iso: Optional[str], tz: str) -> str:
    d = local_dt(iso, tz)
    return d.strftime("%m/%d/%Y") if d else ""


def stamp_str(iso: Optional[str], tz: str) -> str:
    d = local_dt(iso, tz)
    return d.strftime("%b %d, %Y %I:%M:%S %p %Z") if d else ""


def _draw_signature(c: canvas.Canvas, signer: Signer, png: Optional[bytes], x: float, yb: float, w: float, h: float):
    if signer.signature_kind == "drawn" and png:
        img = Image.open(io.BytesIO(png)).convert("RGBA")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        iw, ih = img.size
        scale = min((w - 4) / iw, (h - 2) / ih)
        c.drawImage(ImageReader(img), x + 2, yb + 2, iw * scale, ih * scale, mask="auto")
        return
    name = signer.typed_name or signer.name
    font = signature_font()
    size = 20
    while size > 8 and pdfmetrics.stringWidth(name, font, size) > w - 6:
        size -= 1
    c.setFont(font, size)
    c.setFillColor(colors.HexColor("#1a237e"))
    c.drawString(x + 3, yb + 8, name)


def stamp(pdf_bytes: bytes, env: Envelope, sig_images: Dict[str, Optional[bytes]], tz: str) -> bytes:
    """Burn every field belonging to a signer who has signed into the PDF."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    by_page: Dict[int, List[SigField]] = {}
    for f in env.fields:
        s = env.signer(f.signer_id)
        if s and s.status == "signed":
            by_page.setdefault(f.page, []).append(f)
    for i, page in enumerate(reader.pages, 1):
        flds = by_page.get(i, [])
        if flds:
            pw, ph = float(page.mediabox.width), float(page.mediabox.height)
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(pw, ph))
            for f in flds:
                signer = env.signer(f.signer_id)
                x, yb = f.x, ph - (f.y + f.h)
                if f.kind == "signature":
                    _draw_signature(c, signer, sig_images.get(signer.id), x, yb, f.w, f.h)
                elif f.kind == "date":
                    c.setFont("Helvetica", 10)
                    c.setFillColor(colors.black)
                    c.drawString(x + 2, yb + 9, f.value or date_str(signer.signed_at, tz))
                elif f.kind == "checkbox":
                    if f.value:
                        pad = f.w * 0.24
                        c.setStrokeColor(colors.black)
                        c.setLineWidth(1.6)
                        c.line(x + pad, yb + pad, x + f.w - pad, yb + f.h - pad)
                        c.line(x + pad, yb + f.h - pad, x + f.w - pad, yb + pad)
                elif f.kind == "initials":
                    font = signature_font()
                    c.setFont(font, 14)
                    c.setFillColor(colors.HexColor("#1a237e"))
                    initials = "".join(p[0] for p in (signer.typed_name or signer.name).split() if p).upper()
                    c.drawString(x + 3, yb + 8, f.value or initials)
                else:
                    c.setFont("Helvetica", 10)
                    c.setFillColor(colors.black)
                    c.drawString(x + 2, yb + 9, f.value or "")
            c.save()
            buf.seek(0)
            page.merge_page(PdfReader(buf).pages[0])
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ----------------------------------------------------------------------------- certificate
def certificate(env: Envelope, tz: str, stamped_sha: str, base_url: str) -> bytes:
    body = ParagraphStyle("b", fontName="Helvetica", fontSize=9, leading=12)
    small = ParagraphStyle("s", parent=body, fontSize=7.5, leading=9.5)
    mono = ParagraphStyle("m", parent=body, fontName="Courier", fontSize=7.5, leading=9.5)
    h1 = ParagraphStyle("h1", parent=body, fontName="Helvetica-Bold", fontSize=16, leading=20, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=10.5, leading=13, spaceBefore=10, spaceAfter=4)
    grid = TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDEDED")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ])
    kv_style = TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EDEDED")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ])

    story = [Paragraph("Certificate of Completion", h1),
             Paragraph("Electronic signature record generated by %s for the document below. "
                       "Times are shown in %s." % (config.BRAND, tz), small), Spacer(1, 6)]
    kv = [["Document", Paragraph(env.title, body)],
          ["Envelope ID", Paragraph(env.id, mono)],
          ["Pages (before this certificate)", str(env.page_count)],
          ["Sent", stamp_str(env.sent_at, tz)],
          ["Completed", stamp_str(env.completed_at, tz)],
          ["Signing order", "Sequential" if env.sequential else "Any order"],
          ["Original document SHA-256", Paragraph(env.source_sha256, mono)],
          ["Signed document SHA-256", Paragraph(stamped_sha, mono)],
          ["Service", Paragraph(base_url, body)]]
    t = Table(kv, colWidths=[1.9 * inch, 5.1 * inch])
    t.setStyle(kv_style)
    story += [t, Paragraph("Signers", h2)]
    rows = [["Name / Role", "Email", "Method", "Consented", "Signed", "IP address"]]
    for s in sorted(env.signers, key=lambda s: s.order):
        method = {"drawn": "Drawn signature", "typed": "Typed signature"}.get(s.signature_kind or "", "-")
        rows.append([Paragraph(f"{s.name}<br/><font size=7 color='#555555'>{s.role or ''}</font>", body),
                     Paragraph(s.email, small), Paragraph(method, small),
                     Paragraph(stamp_str(s.consent_at, tz), small), Paragraph(stamp_str(s.signed_at, tz), small),
                     Paragraph(f"{s.ip or '-'}<br/><font size=6 color='#555555'>{(s.user_agent or '')[:70]}</font>", small)])
    t = Table(rows, colWidths=[1.35 * inch, 1.45 * inch, 0.95 * inch, 1.05 * inch, 1.05 * inch, 1.15 * inch], repeatRows=1)
    t.setStyle(grid)
    story += [t, Paragraph("Audit trail", h2)]
    rows = [["Time", "Event", "Signer", "IP", "Detail"]]
    for e in env.audit:
        s = env.signer(e.signer_id) if e.signer_id else None
        rows.append([Paragraph(stamp_str(e.ts, tz), small), Paragraph(e.event, small),
                     Paragraph(s.name if s else "", small), Paragraph(e.ip or "", small), Paragraph(e.detail or "", small)])
    t = Table(rows, colWidths=[1.5 * inch, 1.2 * inch, 1.2 * inch, 1.0 * inch, 2.1 * inch], repeatRows=1)
    t.setStyle(grid)
    story += [t, Spacer(1, 10),
              Paragraph("Each signer agreed to conduct this transaction electronically before signing "
                        "(E-SIGN Act, 15 U.S.C. 7001 et seq.; Texas Uniform Electronic Transactions Act, "
                        "Tex. Bus. &amp; Com. Code ch. 322). The signed document hash above covers every page "
                        "preceding this certificate.", small)]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.75 * inch, bottomMargin=0.75 * inch, title=f"Certificate - {env.title}")
    doc.build(story)
    return buf.getvalue()


def finalize(env: Envelope, source: bytes, sig_images: Dict[str, Optional[bytes]], tz: str, base_url: str) -> Tuple[bytes, str]:
    """Stamp all signatures and append the certificate. Returns (pdf, stamped_sha256)."""
    stamped = stamp(source, env, sig_images, tz)
    stamped_sha = sha256(stamped)
    cert = certificate(env, tz, stamped_sha, base_url)
    writer = PdfWriter()
    for p in PdfReader(io.BytesIO(stamped)).pages:
        writer.add_page(p)
    for p in PdfReader(io.BytesIO(cert)).pages:
        writer.add_page(p)
    writer.add_metadata({"/Title": env.title, "/Producer": config.BRAND})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), stamped_sha
