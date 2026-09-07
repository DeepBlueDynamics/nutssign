# N.U.T.S. Signing Service (`nuts-sign`) — sign.nuts.services

Small self-hosted e-signature service, branded "N.U.T.S. Signing Service" in
the UI, emails, and certificate (override with `BRAND`). Upload a PDF, add signers, place
signature and date fields (auto-detected from `ROLE SIGNATURE` headings or by
clicking on the page), send. Each signer gets an emailed link, draws or types a
signature, and every one of their fields is burned into the PDF. When the last
signer finishes, everyone receives the completed document with a certificate of
completion (signers, timestamps, IPs, document hashes, audit trail) appended.

Built for the 203 N Alamo St lease renewal: the landlord sets up the envelope
and signs first, only then does the tenant receive an email (a copy carrying
the landlord's signature plus their own signing link), and both parties
receive the fully signed PDF at the end.

| Item | Value |
| --- | --- |
| Runtime | Python 3.12, FastAPI, Jinja; pypdf + reportlab for stamping, pdfplumber for anchor detection, pypdfium2 for page images |
| Cloud Run service | `nuts-sign` (`gnosis-459403`, `us-central1`) |
| Domain | `sign.nuts.services` |
| Storage | `DATA_DIR` (local disk); mirrored to `gs://gnosis-459403-nuts-sign` when `GCS_BUCKET` is set |
| Email | AgentMail (`AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX_ID`); console mode when unset |
| Auth | None on admin pages by default (`ADMIN_TOKEN` gates them if set); signing links are 256-bit tokens |

## Run locally

```bash
cd nuts-sign
bash deploy.sh local            # builds nuts-sign:local, runs nuts-sign-local on :8090, volume nuts-sign-data
open http://localhost:8090/
bash functional-test.sh         # creates, sends, signs as both parties, checks the signed PDF
```

Without an AgentMail key the service runs in console mode: nothing is emailed,
each envelope page shows the signing links, and every message is written to
`/data/outbox/` inside the volume (`docker exec nuts-sign-local ls /data/outbox`).

To test real email locally, copy `.env.example` to `.env`, fill in
`AGENTMAIL_API_KEY` and `AGENTMAIL_INBOX_ID`, and rerun `bash deploy.sh local`.

## Deploy

```bash
bash deploy.sh setup     # once: bucket, empty secret, IAM
bash deploy.sh           # Cloud Build + Cloud Run
gcloud beta run domain-mappings create --service nuts-sign --domain sign.nuts.services \
  --region us-central1 --project gnosis-459403      # once; then CNAME sign -> ghs.googlehosted.com
```

Turn email on after deploying (no redeploy needed):

```bash
printf '%s' "$AGENTMAIL_API_KEY" | gcloud secrets versions add nuts-sign-agentmail-key --data-file=- --project gnosis-459403
gcloud run services update nuts-sign --region us-central1 --project gnosis-459403 \
  --update-secrets AGENTMAIL_API_KEY=nuts-sign-agentmail-key:latest \
  --update-env-vars AGENTMAIL_INBOX_ID=noreply@nuts.services,NOTIFY_EMAIL=you@example.com
```

`/health` reports `"mail": "agentmail"` once the key is live.

Sender address: AgentMail only sends from inboxes it hosts, so
`noreply@nuts.services` needs the domain verified there first. In the AgentMail
Console open Domains, add `nuts.services`, publish the MX and TXT (SPF, DKIM,
DMARC) records it returns at the DNS host, wait for the status to reach
VERIFIED, then create the inbox `noreply` on that domain. Until then, point
`AGENTMAIL_INBOX_ID` at any existing inbox (for example one on agentmail.to).
A send to a missing inbox fails loudly and is recorded in the envelope's audit
trail rather than falling back to another sender.

## Signing flow

1. **Create** an envelope: PDF, title, message, signers with roles and order
   (Landlord 1, Tenant 2 by default). Page images are rendered and fields are
   auto-placed above every small `Signature` / `Date` label that sits under an
   ALL-CAPS `… SIGNATURE` heading whose role matches a signer. Every checkbox
   glyph (`☐`) in the document becomes a required checkbox for the signer
   named on that line; boxes on one line form a single choice (the floodplain
   notice's "is / is not" pairs), and the signer cannot sign until each
   statement has one box marked. Marked boxes are stamped with an X.
2. **Adjust fields** on the envelope page: pick signer + field type and click
   on a page; × removes a field. Fields lock when the envelope is sent.
3. **Send**: only the first signer (the person who set the envelope up, in
   the lease case) gets an invite with the link and a copy of the document.
   The envelope page also shows a "Sign as …" button for whoever is up, so
   the sender can sign immediately. Later signers hear nothing yet.
4. **Sign**: the signer reviews the pages (yellow tabs mark their fields),
   ticks the E-SIGN/UETA consent, and draws or types a signature. Date fields
   fill with the signing date in `TIMEZONE`.
5. **Advance**: the next signer receives an invite with the partially signed
   copy attached; the signer who just finished receives a copy too.
6. **Complete**: the final PDF is stamped, a certificate page is appended, and
   the file is emailed to every signer (and `NOTIFY_EMAIL`). Signers can also
   download it from their link.

## Data layout

```
DATA_DIR/
  index.json                      signing token -> envelope id
  outbox/                         console-mode emails
  <envelope>/envelope.json        signers, fields, audit trail
  <envelope>/source.pdf
  <envelope>/pages/page-N.png     original page images (pages-sK/ after K signatures)
  <envelope>/signatures/<id>.png  drawn signatures
  <envelope>/signed.pdf           final document + certificate
```

## Endpoints

There is no public list of envelopes and no account system. Creating an
envelope returns a private management link (`/m/{token}`, a 256-bit token,
optionally emailed to the creator); every signer gets a private signing link.
Management, signer, and file responses carry `Cache-Control: no-store` and
`X-Robots-Tag: noindex`. An operator-only list exists at `/admin` when
`ADMIN_TOKEN` is set; otherwise that path is a 404.

| Route | Purpose |
| --- | --- |
| `GET /` | Splash page |
| `GET /new`, `POST /envelopes` | Create; redirects to the management link |
| `GET /m/{token}` | Detail, field editor, signer links, audit trail |
| `POST /m/{token}/fields`, `DELETE …/fields/{fid}`, `POST …/autoplace`, `POST …/fields/clear` | Field editing (draft only) |
| `POST /m/{token}/send`, `…/remind/{signer}`, `…/void` | Workflow |
| `GET /m/{token}/source.pdf`, `…/signed.pdf`, `…/pages/{n}.png`, `…/json` | Files and JSON (includes signing links) |
| `GET /admin` | Operator list, only with `ADMIN_TOKEN` |
| `GET /sign/{token}` | Signer page (wait page if not their turn) |
| `POST /sign/{token}` | `{"kind":"drawn","image":"data:image/png;base64,…","consent":true}` or `{"kind":"typed","typed_name":"…","consent":true}` |
| `GET /sign/{token}/done`, `…/signed.pdf`, `…/page/{n}.png` | Signer views |
| `GET /health` | Status, mail mode, storage mode |

## Environment

See `.env.example`. `BASE_URL` must be the public URL because it is what goes
into the emails. `RENDER_DPI` (default 100) controls page image size.

# nutssign
