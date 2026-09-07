#!/bin/bash
# End-to-end test against a running nuts-sign (default http://localhost:8090):
# create an envelope from a PDF, send it, sign as each signer in order, and
# check the completed document. Needs curl and python (3.x).
#   ./functional-test.sh [pdf] [base_url]
set -e
PDF="${1:-../../../research/denalease/Lease-Mary Sheppard-20260925.pdf}"
BASE="${2:-http://localhost:8090}"
PY=""
for cand in "python3" "python" "py -3"; do
  if $cand -c "import sys" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "no working python found"; exit 1; }
TMP="$(mktemp -d)"

step() { printf '\n==> %s\n' "$*"; }
jsonq() { $PY -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }

step "health"
curl -sf "$BASE/health"; echo

step "create envelope from $PDF"
LOC=$(curl -s -o /dev/null -w '%{redirect_url}' -F "file=@${PDF};type=application/pdf" \
  -F "title=Residential Lease Agreement - 203 N Alamo St" \
  -F "message=Please review and sign the lease renewal." -F "sequential=on" -F "creator_email=landlord@example.com" \
  -F "signer_name=Dena Jones" -F "signer_email=landlord@example.com" -F "signer_role=Landlord" -F "signer_order=1" \
  -F "signer_name=Mary Sheppard" -F "signer_email=tenant@example.com" -F "signer_role=Tenant" -F "signer_order=2" \
  "$BASE/envelopes")
MT="${LOC##*/}"
M="$BASE/m/$MT"
echo "management link: $M"
[ "${#MT}" -ge 40 ] || { echo "FAIL: expected a management token in the redirect, got '$LOC'"; exit 1; }
curl -s "$M/json" > "$TMP/env.json"
ID=$(jsonq "d['id']" < "$TMP/env.json")
echo "envelope id: $ID"
echo "public listing gone: / -> $(curl -s -o /dev/null -w '%{http_code}' "$BASE/") (splash), /api/envelopes -> $(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/envelopes"), /envelopes/$ID -> $(curl -s -o /dev/null -w '%{http_code}' "$BASE/envelopes/$ID")"
FIELDS=$(jsonq "len(d['fields'])" < "$TMP/env.json")
SIGF=$(jsonq "sum(1 for f in d['fields'] if f['kind']=='signature')" < "$TMP/env.json")
CBF=$(jsonq "sum(1 for f in d['fields'] if f['kind']=='checkbox')" < "$TMP/env.json")
echo "auto-placed fields: $FIELDS (signature fields: $SIGF, checkbox fields: $CBF)"
[ "$SIGF" -ge 2 ] || { echo "FAIL: expected signature fields for both signers"; exit 1; }

step "send"
curl -s -o /dev/null -X POST "$M/send"
curl -s "$M/json" > "$TMP/env.json"
echo "status: $(jsonq "d['status']" < "$TMP/env.json") | signers: $(jsonq "[(s['name'], s['status']) for s in d['signers']]" < "$TMP/env.json")"
T1=$(jsonq "[s for s in d['signers'] if s['order']==1][0]['token']" < "$TMP/env.json")
T2=$(jsonq "[s for s in d['signers'] if s['order']==2][0]['token']" < "$TMP/env.json")

step "tenant link before landlord signs -> wait page"
curl -s "$BASE/sign/$T2" | grep -q "Not your turn" && echo "ok: tenant sees wait page"

step "landlord signs (typed)"
if [ "$CBF" -gt 0 ]; then
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"kind":"typed","typed_name":"Dena Jones","consent":true}' "$BASE/sign/$T1")
  [ "$CODE" = "400" ] && echo "ok: signing without marking the required boxes is refused (400)" || { echo "FAIL: expected 400 without checkbox choices, got $CODE"; exit 1; }
fi
# Mark the right-hand box ("is not") of every checkbox group belonging to the landlord.
$PY - "$TMP/env.json" "$TMP/landlord.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
me = [s for s in d["signers"] if s["order"] == 1][0]["id"]
groups = {}
for f in d["fields"]:
    if f["kind"] == "checkbox" and f["signer_id"] == me:
        groups.setdefault(f.get("group") or f["id"], []).append(f)
checks = {sorted(fs, key=lambda f: f["x"])[-1]["id"]: True for fs in groups.values()}
json.dump({"kind": "typed", "typed_name": "Dena Jones", "consent": True, "checks": checks}, open(sys.argv[2], "w"))
print("marking", len(checks), "box(es)")
EOF
curl -s -X POST -H 'Content-Type: application/json' --data-binary "@$TMP/landlord.json" "$BASE/sign/$T1"; echo
curl -s "$M/json" > "$TMP/env.json"
echo "signers: $(jsonq "[(s['name'], s['status']) for s in d['signers']]" < "$TMP/env.json")"

step "tenant signs (drawn PNG)"
$PY - "$TMP/sig.json" <<'EOF'
import base64, json, struct, sys, zlib
W, H = 300, 90
rows = []
for y in range(H):
    row = bytearray()
    for x in range(W):
        # a wavy stroke: transparent background, opaque navy ink
        on = abs(y - (45 + 25 * __import__("math").sin(x / 22.0))) < 3 or (abs(x - 40) < 3 and 20 < y < 70)
        row += bytes([26, 35, 126, 255]) if on else bytes([0, 0, 0, 0])
    rows.append(b"\x00" + bytes(row))
raw = b"".join(rows)
def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
json.dump({"kind": "drawn", "image": "data:image/png;base64," + base64.b64encode(png).decode(), "consent": True}, open(sys.argv[1], "w"))
EOF
curl -s -X POST -H 'Content-Type: application/json' --data-binary "@$TMP/sig.json" "$BASE/sign/$T2"; echo
curl -s "$M/json" > "$TMP/env.json"
STATUS=$(jsonq "d['status']" < "$TMP/env.json")
echo "status: $STATUS"
[ "$STATUS" = "completed" ] || { echo "FAIL: envelope not completed"; exit 1; }

step "download signed pdf"
curl -sf -o "$TMP/signed.pdf" "$BASE/sign/$T2/signed.pdf"
ls -la "$TMP/signed.pdf"
if command -v pdfinfo >/dev/null; then pdfinfo "$TMP/signed.pdf" | grep Pages; fi
if command -v pdftotext >/dev/null; then
  pdftotext "$TMP/signed.pdf" - | grep -c "Certificate of Completion" | sed 's/^/certificate pages: /'
fi
echo "audit events: $(jsonq "[e['event'] for e in d['audit']]" < "$TMP/env.json")"
echo
echo "PASS  envelope $ID  signed pdf: $TMP/signed.pdf"
echo "SIGNED_PDF=$TMP/signed.pdf"
