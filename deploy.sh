#!/bin/bash
# nuts-sign deploy script
#   ./deploy.sh local      build the image and run it on http://localhost:8090 (data in volume nuts-sign-data)
#   ./deploy.sh setup      one-time GCP infra: bucket + secret + IAM
#   ./deploy.sh            Cloud Build + Cloud Run deploy (sign.nuts.services)
set -e
cd "$(dirname "$0")"

PROJECT_ID="gnosis-459403"
REGION="us-central1"
SERVICE="nuts-sign"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}"
BUCKET="${PROJECT_ID}-nuts-sign"
DOMAIN="sign.nuts.services"
LOCAL_PORT="${LOCAL_PORT:-8090}"

if [ "$1" = "local" ]; then
  echo "==> Building ${SERVICE}:local..."
  docker build -t "${SERVICE}:local" .
  echo "==> Replacing container ${SERVICE}-local on port ${LOCAL_PORT}..."
  docker rm -f "${SERVICE}-local" >/dev/null 2>&1 || true
  ENV_FILE_ARG=""
  [ -f .env ] && ENV_FILE_ARG="--env-file .env"
  docker run -d --name "${SERVICE}-local" \
    -p "${LOCAL_PORT}:8080" \
    -v "${SERVICE}-data:/data" \
    ${ENV_FILE_ARG} \
    -e "BASE_URL=${BASE_URL:-http://localhost:${LOCAL_PORT}}" \
    "${SERVICE}:local" >/dev/null
  for i in $(seq 1 20); do
    if curl -sf "http://localhost:${LOCAL_PORT}/health" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  curl -s "http://localhost:${LOCAL_PORT}/health"; echo
  echo "==> ${SERVICE} is running at http://localhost:${LOCAL_PORT}/"
  exit 0
fi

if [ "$1" = "setup" ]; then
  echo "==> Creating bucket gs://${BUCKET} (envelopes, PDFs, signatures)..."
  gcloud storage buckets create "gs://${BUCKET}" \
    --project "${PROJECT_ID}" --location "${REGION}" \
    --uniform-bucket-level-access || true
  echo "==> Creating secret nuts-sign-agentmail-key (add a version to enable email)..."
  gcloud secrets create nuts-sign-agentmail-key --project "${PROJECT_ID}" --replication-policy=automatic || true
  echo "==> Granting the Cloud Run runtime SA bucket + secret access..."
  PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
  RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member "serviceAccount:${RUNTIME_SA}" --role roles/storage.objectAdmin \
    --project "${PROJECT_ID}" >/dev/null
  gcloud secrets add-iam-policy-binding nuts-sign-agentmail-key \
    --member "serviceAccount:${RUNTIME_SA}" --role roles/secretmanager.secretAccessor \
    --project "${PROJECT_ID}" >/dev/null
  echo "setup done. Load the AgentMail key with:"
  echo "  printf '%s' \"\$AGENTMAIL_API_KEY\" | gcloud secrets versions add nuts-sign-agentmail-key --data-file=- --project ${PROJECT_ID}"
  exit 0
fi

# Mount the AgentMail secret only when it has a version; otherwise the service
# runs in console mode and the key can be added later without a redeploy:
#   gcloud run services update nuts-sign --region us-central1 --project gnosis-459403 \
#     --update-secrets AGENTMAIL_API_KEY=nuts-sign-agentmail-key:latest \
#     --update-env-vars AGENTMAIL_INBOX_ID=noreply@nuts.services
SECRET_ARGS=""
if gcloud secrets versions list nuts-sign-agentmail-key --project "${PROJECT_ID}" \
     --filter="state=enabled" --format="value(name)" 2>/dev/null | grep -q .; then
  SECRET_ARGS="AGENTMAIL_API_KEY=nuts-sign-agentmail-key:latest"
fi

echo "==> Building and pushing image via Cloud Build..."
gcloud builds submit --tag "${IMAGE}:latest" --project "${PROJECT_ID}" --timeout 15m .

echo "==> Deploying ${SERVICE} to Cloud Run..."
# --max-instances 1: envelopes are read-modify-write JSON files mirrored to GCS;
# a single instance keeps that trivially consistent.
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}:latest" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --platform managed \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 1 \
  --concurrency 40 \
  --port 8080 \
  --update-env-vars "BASE_URL=https://${DOMAIN},GCS_BUCKET=${BUCKET},TIMEZONE=America/Chicago" \
  ${SECRET_ARGS:+--update-secrets "$SECRET_ARGS"}
# --update-env-vars / --update-secrets merge rather than replace, so values set
# by hand on the service (AGENTMAIL_INBOX_ID, NOTIFY_EMAIL, ADMIN_TOKEN) survive.

echo ""
echo "==> Service URL:"
gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT_ID}" --format="value(status.url)"
echo "==> Domain mapping (create once):"
echo "  gcloud beta run domain-mappings create --service ${SERVICE} --domain ${DOMAIN} --region ${REGION} --project ${PROJECT_ID}"
echo "  then CNAME ${DOMAIN} -> ghs.googlehosted.com"
