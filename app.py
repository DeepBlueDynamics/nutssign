"""N.U.T.S. Signing Service (nuts-sign): private e-signatures for sign.nuts.services.

No accounts. Whoever creates an envelope gets a private management link (a
256-bit token); each signer gets their own private signing link. Nothing is
listed publicly. Sequential flow: the first signer (normally the person who set
the envelope up) signs, then the next signer is emailed a copy carrying the
signatures so far plus their link; completion emails everyone the final PDF
with a certificate page.
"""
from __future__ import annotations

import base64
import binascii
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sign import config, mailer, pdf
from sign.models import Envelope, SigField, Signer, now_iso
from sign.storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("nuts-sign")

HERE = Path(__file__).parent
app = FastAPI(title="nuts-sign", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(
    brand=config.BRAND,
    base_url=config.BASE_URL,
    tz=config.TIMEZONE,
    fmt=lambda iso: pdf.stamp_str(iso, config.TIMEZONE),
    mail_mode=mailer.mode,
)
store = Storage(config.DATA_DIR, config.GCS_BUCKET, config.GCS_PREFIX)

FIELD_SIZES = {"signature": (pdf.SIG_W, pdf.FIELD_H), "date": (pdf.DATE_W, pdf.FIELD_H),
               "initials": (pdf.INITIALS_W, pdf.FIELD_H), "text": (150.0, 24.0),
               "checkbox": (pdf.CHECK_W, pdf.CHECK_W)}


# ----------------------------------------------------------------------------- helpers
def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def sign_url(signer: Signer) -> str:
    return f"{config.BASE_URL}/sign/{signer.token}"


def manage_url(env: Envelope) -> str:
    return f"{config.BASE_URL}/m/{env.admin_token}"


def get_env(token: str) -> Envelope:
    env = store.find_by_admin_token(token)
    if env is None:
        raise HTTPException(404, "envelope not found")
    return env


def source_pdf(env: Envelope) -> bytes:
    data = store.read_bytes(f"{env.id}/source.pdf")
    if data is None:
        raise HTTPException(500, "source document missing")
    return data


def signature_images(env: Envelope) -> Dict[str, Optional[bytes]]:
    return {s.id: store.read_bytes(f"{env.id}/signatures/{s.id}.png") for s in env.signers if s.status == "signed"}


def interim_pdf(env: Envelope) -> bytes:
    """Source with every completed signer's fields burned in (no certificate)."""
    return pdf.stamp(source_pdf(env), env, signature_images(env), config.TIMEZONE)


_PAGES_LOCK = threading.Lock()


def ensure_pages(env: Envelope) -> str:
    """Make sure page images exist for the current signing stage; returns the folder.
    Single-flight: a browser asks for every page at once, and only one thread may
    render (pdfium is not thread-safe and the render is the expensive part)."""
    signed = sum(1 for s in env.signers if s.status == "signed")
    folder = "pages" if signed == 0 else f"pages-s{signed}"
    last = f"{env.id}/{folder}/page-{env.page_count}.png"
    if store.exists(last):
        return folder
    with _PAGES_LOCK:
        if store.exists(last):
            return folder
        src = source_pdf(env) if signed == 0 else interim_pdf(env)
        pngs = pdf.render_pages(src, config.RENDER_DPI)
        for i, png in enumerate(pngs, 1):
            store.write_bytes(f"{env.id}/{folder}/page-{i}.png", png)
    return folder


def page_png(env: Envelope, n: int) -> bytes:
    if n < 1 or n > env.page_count:
        raise HTTPException(404, "no such page")
    folder = ensure_pages(env)
    data = store.read_bytes(f"{env.id}/{folder}/page-{n}.png")
    if data is None:
        raise HTTPException(500, "page image missing")
    return data


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def message_page(request: Request, title: str, text: str, status: int = 200) -> HTMLResponse:
    resp = page(request, "message.html", title=title, text=text)
    resp.status_code = status
    return resp


def pdf_name(env: Envelope, suffix: str) -> str:
    stem = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in env.title).strip().replace(" ", "-") or "document"
    return f"{stem[:60]}{suffix}.pdf"


def no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


# ----------------------------------------------------------------------------- email flow
def _email_log(env: Envelope, signer: Optional[Signer], what: str, to: str, result: Dict[str, str]) -> None:
    status = result.get("status")
    detail = f"{what} -> {to} ({status})" + (f": {result.get('error')}" if status == "error" else "")
    env.log("email", signer_id=signer.id if signer else None, detail=detail)


def _others(env: Envelope, signer: Signer) -> str:
    names = [s.name for s in env.signers if s.id != signer.id]
    return ", ".join(names) if names else "the other party"


def email_manage_link(env: Envelope) -> None:
    if not env.creator_email:
        return
    url = manage_url(env)
    text = (f"Your envelope \"{env.title}\" is ready.\n\nPrivate management link (keep it to yourself):\n{url}\n\n"
            f"Use it to place fields, send for signature, watch progress, and download the signed document.\n")
    html = (f"<p>Your envelope <b>{env.title}</b> is ready.</p><p>Private management link (keep it to yourself):<br>"
            f"<a href=\"{url}\">{url}</a></p><p style=\"color:#555\">Use it to place fields, send for signature, "
            f"watch progress, and download the signed document.</p>")
    result = mailer.send(env.creator_email, f"Your envelope: {env.title}", text, html)
    _email_log(env, None, "manage-link", env.creator_email, result)


def email_invite(env: Envelope, signer: Signer, copy_pdf: Optional[bytes], copy_label: str) -> None:
    url = sign_url(signer)
    intro = f"Hello {signer.name},\n\n{env.message}\n\n" if env.message else f"Hello {signer.name},\n\n"
    text = (f"{intro}Please review and sign \"{env.title}\".\n\nSign here: {url}\n\n"
            f"{copy_label}\n\nThis link is unique to you. Do not forward it.\n")
    html = (f"<p>Hello {signer.name},</p>" + (f"<p>{env.message}</p>" if env.message else "") +
            f"<p>Please review and sign <b>{env.title}</b>.</p>"
            f"<p><a href=\"{url}\" style=\"display:inline-block;background:#1a237e;color:#fff;padding:10px 18px;"
            f"border-radius:6px;text-decoration:none\">Review and sign</a></p>"
            f"<p style=\"color:#555\">{copy_label}</p><p style=\"color:#888;font-size:12px\">This link is unique to you. Do not forward it.</p>")
    attachments = [(pdf_name(env, ""), copy_pdf, "application/pdf")] if copy_pdf else []
    result = mailer.send(signer.email, f"Please sign: {env.title}", text, html, attachments)
    signer.status = "sent"
    signer.sent_at = now_iso()
    _email_log(env, signer, "invite", signer.email, result)


def email_signed_copy(env: Envelope, signer: Signer, copy_pdf: bytes, next_names: str) -> None:
    text = (f"Hello {signer.name},\n\nYour signature on \"{env.title}\" has been recorded. "
            f"A copy with your signature is attached. It has now been sent to {next_names} to sign; "
            f"you will receive the fully signed document when everyone has signed.\n")
    html = (f"<p>Hello {signer.name},</p><p>Your signature on <b>{env.title}</b> has been recorded. "
            f"A copy with your signature is attached. It has now been sent to {next_names} to sign; "
            f"you will receive the fully signed document when everyone has signed.</p>")
    result = mailer.send(signer.email, f"Signed: {env.title}", text, html,
                         [(pdf_name(env, "-partially-signed"), copy_pdf, "application/pdf")])
    _email_log(env, signer, "signed-copy", signer.email, result)


def email_completed(env: Envelope, final_pdf: bytes) -> None:
    names = ", ".join(s.name for s in sorted(env.signers, key=lambda s: s.order))
    recipients = [(s, s.email, f"{sign_url(s)}/signed.pdf") for s in env.signers]
    extra = [e for e in (env.creator_email, config.NOTIFY_EMAIL) if e and e.lower() not in {s.email.lower() for s in env.signers}]
    for s, to, url in recipients:
        text = (f"Hello {s.name},\n\n\"{env.title}\" has been signed by all parties ({names}). "
                f"The completed document with its certificate of completion is attached.\n\nDownload: {url}\n")
        html = (f"<p>Hello {s.name},</p><p><b>{env.title}</b> has been signed by all parties ({names}). "
                f"The completed document with its certificate of completion is attached.</p>"
                f"<p><a href=\"{url}\">Download the signed document</a></p>")
        result = mailer.send(to, f"Completed: {env.title}", text, html,
                             [(pdf_name(env, "-signed"), final_pdf, "application/pdf")])
        _email_log(env, s, "completed-copy", to, result)
    for to in dict.fromkeys(extra):
        result = mailer.send(to, f"Completed: {env.title}",
                             f"\"{env.title}\" was signed by {names}. Signed copy attached.\n\nManage: {manage_url(env)}\n", None,
                             [(pdf_name(env, "-signed"), final_pdf, "application/pdf")])
        _email_log(env, None, "completed-copy", to, result)


def dispatch(env: Envelope) -> None:
    """Email only the signers whose turn it is. Later signers hear nothing until
    the person before them has signed; their invite then carries the signed copy."""
    signed_any = any(s.status == "signed" for s in env.signers)
    copy_pdf = interim_pdf(env) if signed_any else source_pdf(env)
    label = "A copy with the signatures collected so far is attached." if signed_any else "A copy of the document is attached."
    for s in env.signers_due():
        if s.status == "pending":
            email_invite(env, s, copy_pdf, label)


def complete(env: Envelope) -> None:
    env.status = "completed"
    env.completed_at = now_iso()
    env.log("completed", detail="all signers have signed")
    final_pdf, stamped_sha = pdf.finalize(env, source_pdf(env), signature_images(env), config.TIMEZONE, config.BASE_URL)
    env.signed_sha256 = pdf.sha256(final_pdf)
    store.write_bytes(f"{env.id}/signed.pdf", final_pdf)
    env.log("finalized", detail=f"signed document sha256 {stamped_sha[:16]}...; certificate appended")
    email_completed(env, final_pdf)


# ----------------------------------------------------------------------------- public pages
@app.get("/health")
def health():
    return {"ok": True, "service": config.SERVICE_NAME, "mail": mailer.mode(),
            "storage": "gcs" if config.GCS_BUCKET else "local"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return page(request, "home.html")


@app.get("/new", response_class=HTMLResponse)
def new_envelope(request: Request):
    return page(request, "new.html")


@app.get("/envelopes/new")
def new_envelope_legacy():
    return RedirectResponse("/new", status_code=301)


@app.post("/envelopes")
async def create_envelope(request: Request, title: str = Form(""), message: str = Form(""),
                          creator_email: str = Form(""), sequential: str = Form("on"),
                          file: UploadFile = File(...),
                          signer_name: List[str] = Form(default=[]), signer_email: List[str] = Form(default=[]),
                          signer_role: List[str] = Form(default=[]), signer_order: List[str] = Form(default=[])):
    data = await file.read()
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF larger than {config.MAX_UPLOAD_MB} MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "upload must be a PDF")
    env = Envelope(title=title.strip() or (file.filename or "Document").rsplit(".", 1)[0],
                   message=message.strip(), creator_email=creator_email.strip().lower(),
                   sequential=(sequential == "on"))
    for i, (name, email) in enumerate(zip(signer_name, signer_email)):
        if not name.strip() or not email.strip():
            continue
        role = signer_role[i].strip() if i < len(signer_role) else ""
        try:
            order = int(signer_order[i]) if i < len(signer_order) and signer_order[i].strip() else i + 1
        except ValueError:
            order = i + 1
        env.signers.append(Signer(name=name.strip(), email=email.strip().lower(), role=role, order=order))
    if not env.signers:
        raise HTTPException(400, "add at least one signer")
    sizes = pdf.page_sizes(data)
    env.page_count, env.page_sizes, env.source_sha256 = len(sizes), sizes, pdf.sha256(data)
    store.write_bytes(f"{env.id}/source.pdf", data)
    for i, png in enumerate(pdf.render_pages(data, config.RENDER_DPI), 1):
        store.write_bytes(f"{env.id}/pages/page-{i}.png", png)
    env.fields = pdf.auto_place(data, env.signers)
    env.log("created", ip=client_ip(request), detail=f"{env.page_count} pages, {len(env.fields)} fields auto-placed")
    email_manage_link(env)
    store.save(env)
    return RedirectResponse(f"/m/{env.admin_token}", status_code=303)


# ----------------------------------------------------------------------------- management (private link)
@app.get("/m/{token}", response_class=HTMLResponse)
def envelope_detail(request: Request, token: str):
    try:
        env = get_env(token)
    except HTTPException:
        return message_page(request, "Link not valid", "This management link is not valid.", 404)
    base = f"/m/{env.admin_token}"
    payload = {"base": base, "status": env.status, "page_sizes": env.page_sizes,
               "signers": [{"id": s.id, "name": s.name, "role": s.role} for s in env.signers],
               "fields": [f.model_dump() for f in env.fields], "sizes": FIELD_SIZES}
    return no_store(page(request, "envelope.html", env=env, base=base, payload=payload, sign_url=sign_url,
                         manage_url=manage_url(env), signed_exists=env.status == "completed"))


@app.post("/m/{token}/fields")
def add_field(token: str, body: dict = Body(...)):
    env = get_env(token)
    if env.status != "draft":
        raise HTTPException(409, "fields are locked after sending")
    if env.signer(body.get("signer_id", "")) is None:
        raise HTTPException(400, "unknown signer")
    kind = body.get("kind", "signature")
    if kind not in FIELD_SIZES:
        raise HTTPException(400, "unknown field kind")
    pg = int(body.get("page", 1))
    if pg < 1 or pg > env.page_count:
        raise HTTPException(400, "page out of range")
    w, h = FIELD_SIZES[kind]
    pw, ph = env.page_sizes[pg - 1]
    x = min(max(float(body.get("x", 0)), 0.0), pw - w)
    y = min(max(float(body.get("y", 0)), 0.0), ph - h)
    f = SigField(signer_id=body["signer_id"], kind=kind, page=pg, x=x, y=y,
                 w=float(body.get("w", w)), h=float(body.get("h", h)))
    env.fields.append(f)
    store.save(env)
    return f.model_dump()


@app.delete("/m/{token}/fields/{field_id}")
def delete_field(token: str, field_id: str):
    env = get_env(token)
    if env.status != "draft":
        raise HTTPException(409, "fields are locked after sending")
    env.fields = [f for f in env.fields if f.id != field_id]
    store.save(env)
    return {"ok": True}


@app.post("/m/{token}/autoplace")
def autoplace(token: str):
    env = get_env(token)
    if env.status != "draft":
        raise HTTPException(409, "fields are locked after sending")
    env.fields = pdf.auto_place(source_pdf(env), env.signers)
    env.log("autoplace", detail=f"{len(env.fields)} fields")
    store.save(env)
    return RedirectResponse(f"/m/{env.admin_token}", status_code=303)


@app.post("/m/{token}/fields/clear")
def clear_fields(token: str):
    env = get_env(token)
    if env.status != "draft":
        raise HTTPException(409, "fields are locked after sending")
    env.fields = []
    store.save(env)
    return RedirectResponse(f"/m/{env.admin_token}", status_code=303)


@app.post("/m/{token}/send")
def send_envelope(request: Request, token: str):
    with store.lock:
        env = get_env(token)
        if env.status != "draft":
            raise HTTPException(409, "envelope already sent")
        missing = [s.name for s in env.signers if not any(f.kind == "signature" for f in env.fields_for(s.id))]
        if missing:
            raise HTTPException(400, "every signer needs at least one signature field; missing for: " + ", ".join(missing))
        env.status = "sent"
        env.sent_at = now_iso()
        env.log("sent", ip=client_ip(request), detail="sequential" if env.sequential else "any order")
        dispatch(env)
        store.save(env)
    return RedirectResponse(f"/m/{env.admin_token}", status_code=303)


@app.post("/m/{token}/remind/{signer_id}")
def remind(token: str, signer_id: str):
    with store.lock:
        env = get_env(token)
        s = env.signer(signer_id)
        if s is None or not env.is_turn(s):
            raise HTTPException(409, "that signer does not have a live signing link right now")
        copy_pdf = interim_pdf(env) if any(x.status == "signed" for x in env.signers) else source_pdf(env)
        email_invite(env, s, copy_pdf, "A copy of the document is attached.")
        env.log("reminded", signer_id=s.id)
        store.save(env)
    return RedirectResponse(f"/m/{env.admin_token}", status_code=303)


@app.post("/m/{token}/void")
def void_envelope(request: Request, token: str):
    with store.lock:
        env = get_env(token)
        if env.status == "completed":
            raise HTTPException(409, "completed envelopes cannot be voided")
        env.status = "voided"
        env.log("voided", ip=client_ip(request))
        store.save(env)
    return RedirectResponse(f"/m/{env.admin_token}", status_code=303)


@app.get("/m/{token}/source.pdf")
def download_source(token: str):
    env = get_env(token)
    return no_store(Response(source_pdf(env), media_type="application/pdf",
                             headers={"Content-Disposition": f'inline; filename="{pdf_name(env, "")}"'}))


@app.get("/m/{token}/signed.pdf")
def download_signed(token: str):
    env = get_env(token)
    data = store.read_bytes(f"{env.id}/signed.pdf")
    if data is None:
        raise HTTPException(404, "not completed yet")
    return no_store(Response(data, media_type="application/pdf",
                             headers={"Content-Disposition": f'inline; filename="{pdf_name(env, "-signed")}"'}))


@app.get("/m/{token}/pages/{n}.png")
def admin_page_png(token: str, n: int):
    env = get_env(token)
    return Response(page_png(env, n), media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@app.get("/m/{token}/json")
def envelope_json(token: str):
    env = get_env(token)
    data = env.model_dump()
    data["manage_url"] = manage_url(env)
    for s in data["signers"]:
        s["sign_url"] = f"{config.BASE_URL}/sign/{s['token']}"
    return no_store(JSONResponse(data))


# ----------------------------------------------------------------------------- operator list (only with ADMIN_TOKEN)
@app.get("/admin", response_class=HTMLResponse)
def admin_list(request: Request):
    if not config.ADMIN_TOKEN:
        raise HTTPException(404, "not found")
    tok = request.cookies.get("admin") or request.headers.get("x-admin-token") or request.query_params.get("admin") or ""
    if tok != config.ADMIN_TOKEN:
        return PlainTextResponse("admin token required: open /admin?admin=TOKEN once", status_code=401)
    resp = no_store(page(request, "admin.html", envelopes=store.list_envelopes(), manage_url=manage_url))
    if request.query_params.get("admin"):
        resp.set_cookie("admin", tok, httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


# ----------------------------------------------------------------------------- signer pages
def _lookup(token: str):
    found = store.find_by_token(token)
    if not found:
        raise HTTPException(404, "invalid link")
    return found


@app.get("/sign/{token}", response_class=HTMLResponse)
def sign_page(request: Request, token: str):
    try:
        env, signer = _lookup(token)
    except HTTPException:
        return message_page(request, "Link not valid", "This signing link is not valid. Ask the sender for a new one.", 404)
    if env.status == "voided":
        return message_page(request, "Document voided", f"\"{env.title}\" was voided by the sender and can no longer be signed.")
    if signer.status == "signed" or env.status == "completed":
        return RedirectResponse(f"/sign/{token}/done", status_code=303)
    if env.status != "sent":
        return message_page(request, "Not ready", "This document has not been sent for signature yet.")
    if not env.is_turn(signer):
        ahead = [s.name for s in env.signers if s.order < signer.order and s.status != "signed"]
        return no_store(page(request, "sign_wait.html", env=env, signer=signer, ahead=ahead))
    if not signer.viewed_at:
        with store.lock:
            env = store.load(env.id)
            signer = env.signer(signer.id)
            signer.viewed_at = now_iso()
            env.log("viewed", signer_id=signer.id, ip=client_ip(request), detail=(request.headers.get("user-agent") or "")[:120])
            store.save(env)
    fields = env.fields_for(signer.id)
    payload = {"token": token, "page_sizes": env.page_sizes, "fields": [f.model_dump() for f in fields],
               "signer": {"id": signer.id, "name": signer.name}}
    return no_store(page(request, "sign.html", env=env, signer=signer, fields=fields, payload=payload))


@app.get("/sign/{token}/page/{n}.png")
def signer_page_png(token: str, n: int):
    env, _ = _lookup(token)
    return Response(page_png(env, n), media_type="image/png", headers={"Cache-Control": "private, max-age=120"})


def _decode_png(data_url: str) -> bytes:
    if not data_url.startswith("data:image/png;base64,"):
        raise HTTPException(400, "signature must be a PNG data URL")
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "bad signature image")
    if not raw.startswith(b"\x89PNG") or len(raw) > 2 * 1024 * 1024:
        raise HTTPException(400, "bad signature image")
    return raw


@app.post("/sign/{token}")
def do_sign(request: Request, token: str, body: dict = Body(...)):
    if not body.get("consent"):
        raise HTTPException(400, "you must agree to sign electronically")
    kind = body.get("kind")
    png: Optional[bytes] = None
    typed = ""
    if kind == "drawn":
        png = _decode_png(str(body.get("image", "")))
    elif kind == "typed":
        typed = str(body.get("typed_name", "")).strip()
        if not typed:
            raise HTTPException(400, "type your name to sign")
    else:
        raise HTTPException(400, "unknown signature kind")
    checks = body.get("checks") or {}
    if not isinstance(checks, dict):
        raise HTTPException(400, "checks must be an object of field id -> true/false")
    with store.lock:
        env, signer = _lookup(token)
        if env.status != "sent" or signer.status == "signed":
            raise HTTPException(409, "this document is not open for your signature")
        if not env.is_turn(signer):
            raise HTTPException(409, "it is not your turn to sign yet")
        groups: Dict[str, List[bool]] = {}
        marked = 0
        for f in env.fields_for(signer.id):
            if f.kind != "checkbox":
                continue
            f.value = "X" if checks.get(f.id) else None
            marked += 1 if f.value else 0
            if f.required:
                groups.setdefault(f.group or f.id, []).append(bool(f.value))
        if any(not any(v) for v in groups.values()):
            raise HTTPException(400, "Please mark one box in each statement before signing")
        ts = now_iso()
        signer.status, signer.signed_at, signer.consent_at = "signed", ts, ts
        signer.ip, signer.user_agent = client_ip(request), (request.headers.get("user-agent") or "")[:200]
        signer.signature_kind, signer.typed_name = kind, (typed or None)
        if png:
            store.write_bytes(f"{env.id}/signatures/{signer.id}.png", png)
        for f in env.fields_for(signer.id):
            if f.kind == "date":
                f.value = pdf.date_str(ts, config.TIMEZONE)
        env.log("consented", signer_id=signer.id, ip=signer.ip, detail="agreed to electronic signature")
        env.log("signed", signer_id=signer.id, ip=signer.ip,
                detail=f"{kind} signature applied to {len(env.fields_for(signer.id))} fields"
                       + (f"; {marked} box(es) marked" if groups or marked else ""))
        if env.all_signed():
            complete(env)
        else:
            dispatch(env)
            next_names = ", ".join(s.name for s in env.signers_due()) or _others(env, signer)
            email_signed_copy(env, signer, interim_pdf(env), next_names)
        store.save(env)
        ensure_pages(env)
    return {"ok": True, "completed": env.status == "completed", "next": f"/sign/{token}/done"}


@app.get("/sign/{token}/done", response_class=HTMLResponse)
def sign_done(request: Request, token: str):
    try:
        env, signer = _lookup(token)
    except HTTPException:
        return message_page(request, "Link not valid", "This signing link is not valid.", 404)
    waiting = [s.name for s in env.signers if s.status != "signed"]
    return no_store(page(request, "sign_done.html", env=env, signer=signer, waiting=waiting))


@app.get("/sign/{token}/signed.pdf")
def signer_download(token: str):
    env, _ = _lookup(token)
    data = store.read_bytes(f"{env.id}/signed.pdf")
    if env.status != "completed" or data is None:
        raise HTTPException(404, "the document is not completed yet")
    return no_store(Response(data, media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{pdf_name(env, "-signed")}"'}))
