#!/usr/bin/env bash

NS=$1
OLD=$2
NEW=$3

cat job.yaml | env NS="${NS}" OLDVOL="${OLD}" NEWVOL="${NEW}" envsubst -
