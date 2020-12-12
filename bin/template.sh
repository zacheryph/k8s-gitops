#!/usr/bin/env bash
# this is for viewing a HelmRelease template
# its very basic and will not load valuesFrom

function _usage() {
  echo "== usage: template.sh path/to/helm-release.yaml"
  echo "note:"
  echo "  - requires all helm repositories be installed locally"
  echo "  - requires release be from a helm repository and not git"
  echo
  exit
}

[ $# -ne 1 ] && _usage
releaseFile=${1}
[ -f "${releaseFile}" ] || _usage

# extract repository and chart name
name=$(yq r ${releaseFile} "metadata.name")
repo=$(yq r ${releaseFile} "spec.chart.spec.sourceRef.name")
chart=$(yq r ${releaseFile} "spec.chart.spec.chart")

# dump the values into template
yq r ${releaseFile} "spec.values" \
  | helm template ${name} "${repo}/${chart}" --values -
