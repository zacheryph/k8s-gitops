#!/usr/bin/env bash
REPO_ROOT=$(git rev-parse --show-toplevel)
export REPO_ROOT

need() {
    which "$1" &>/dev/null || die "Binary '$1' is missing but required"
}

need "envsubst"
need "kubectl"
need "kubeseal"
need "yq"

if [ "$(uname)" == "Darwin" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${REPO_ROOT}/.secrets.env"
  set +a
else
  # shellcheck disable=SC1091
  . "${REPO_ROOT}/.secrets.env"
fi

function extract_ns() {
  yq r "${1}/kustomization.yaml" "namespace"
}

function envsub() {
  envsubst -no-unset -i "${1}"
}

# gensecret ns name args...
function gensecret() {
  ns=$1
  name=$2

  kubectl create secret generic \
    --dry-run=client \
    --output=json \
    --namespace="${ns}" \
    "${name}" \
    "${@:3}"
}

function seal() {
  kubeseal --controller-name=sealed-secrets --format=yaml \
    | yq d - "spec.template" \
    | yq d - "metadata.creationTimestamp"
}

# ... file resource-suffix gensecret-argument
function write_sealed_secret() {
  file=${1}
  resourceSuffix=${2}

  base=$(basename "${file}")
  parent=$(dirname "${file}")
  ns=$(extract_ns "${parent}")
  sealedName="${base%%.*}${resourceSuffix}"
  sealedFile="cluster/secrets/${ns}/${sealedName}.yaml"

  [ "${file}" -ot "${sealedFile}" ] && return

  echo "generating: ${sealedFile}"
  mkdir -p "cluster/secrets/${ns}"
  envsub "${file}" \
    | gensecret "${ns}" "${sealedName}" "${@:3}" \
    | seal \
    > "${sealedFile}"
}

function refresh_secrets() {
  # *.values.yaml
  while IFS= read -r -d '' file
  do
    write_sealed_secret "${file}" "-values" --from-file=values.yaml=/dev/stdin
  done <  <(find cluster -type f -name '*.values.yaml' -print0)

  # *.crypt.env
  while IFS= read -r -d '' file
  do
    write_sealed_secret "${file}" "" --from-env-file=/dev/stdin
  done <  <(find cluster -type f -name '*.crypt.env' -print0)
}

function write_kustomization() {
  cat <<EOF | sed -r 's/^ {4}//' > cluster/secrets/kustomization.yaml
    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization
    resources:
    $(find cluster/secrets/**/*.yaml | sed 's|^cluster/secrets/|- ./|')
EOF
}

function wipe_secrets() {
  echo "== wiping all secrets"
  rm -rf cluster/secrets
}

function usage() {
  echo "== secrets management"
  echo "usage: secrets.sh <refresh|write|wipe>"
  exit
}

[[ -z "$1" ]] && usage
case "$1" in
  refresh)
    refresh_secrets
    write_kustomization
    ;;
  write)
    wipe_secrets
    refresh_secrets
    write_kustomization
    ;;
  wipe)
    wipe_secrets
    ;;
  *)
    usage
    ;;
esac
