#!/usr/bin/env bash
source ${BASH_SOURCE[0]%/*}/common.sh

need "jq"

function usage() {
  echo "== update.sh"
  echo "usage: update.sh <package>"
  echo "  flux:     local flux cli binary"
  exit
}

[[ -z "$1" ]] && usage
case "$1" in
  flux)
    shift ; shift ;
    source ${BASH_SOURCE[0]%/*}/update-flux.sh
    ;;
  *)
    usage
    ;;
esac
