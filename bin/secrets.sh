#!/usr/bin/env bash
source ${BASH_SOURCE[0]%/*}/common.sh

need "envsubst"
need "kubectl"
need "kubeseal"
need "yq"
load_env

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
  kubeseal --format=yaml \
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

function check_secret_exists() {
  file=${1}
  resourceSuffix=${2}

  base=$(basename "${file}")
  parent=$(dirname "${file}")
  ns=$(extract_ns "${parent}")
  sealedName="${base%%.*}${resourceSuffix}"
  sealedFile="cluster/secrets/${ns}/${sealedName}.yaml"

  [ "${file}" -ot "${sealedFile}" ] && return
  echo "secret missing or outdated for: ${file}"
  EXIT_CODE=1
}

function check_secrets() {
  export EXIT_CODE=0
  file=${1}
  ext=${file#*\.}

  if [ "${ext}" == "values.yaml" ]; then
    check_secret_exists "${file}" "-values" --from-file=values.yaml=/dev/stdin
  fi

  if [ "${ext}" == "crypt.env" ]; then
    check_secret_exists "${file}" "" --from-env-file=/dev/stdin
  fi

  exit ${EXIT_CODE}
}

function usage() {
  echo "== secrets management"
  echo "usage: secrets.sh <check|refresh|write|wipe>"
  exit
}

[[ -z "$1" ]] && usage
case "$1" in
  check)
    check_secrets "${@:2}"
    ;;
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
