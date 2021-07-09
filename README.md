<img src="https://camo.githubusercontent.com/5b298bf6b0596795602bd771c5bddbb963e83e0f/68747470733a2f2f692e696d6775722e636f6d2f7031527a586a512e706e67" align="left" width="144px" height="144px"/>

#### Homelab K8s-Gitops
> GitOps state for my cluster using flux v2

[![Discord](https://img.shields.io/badge/discord-chat-7289DA.svg?maxAge=60&style=flat-square)](https://discord.gg/DNCynrJ)
[![k3s](https://img.shields.io/badge/k3s-v1.21.0-orange?style=flat-square)](https://k3s.io/)
[![GitHub issues](https://img.shields.io/github/issues/zacheryph/k8s-gitops?style=flat-square)](https://github.com/zacheryph/k8s-gitops/issues)
[![GitHub last commit](https://img.shields.io/github/last-commit/zacheryph/k8s-gitops?color=purple&style=flat-square)](https://github.com/zacheryph/k8s-gitops/commits/master)

<br/>

## Overview

## Secret Management

All secret management is handled via SOPS and Flux variable expansion.

## Hardware

Cluster is 3 built 1u servers with the following hardware.

* Inwin 1W-RF100S Chassis
* ASRock Rack E3C246D2I
* Intel Core i3-9100
* 16GB Memory
* 128GB M.2 2242 SSD (OS)
* 2x 6TB HGST Ultrastar (longhorn)

## Cluster

Below is the layout of the cluster resource files and what
is contained. they are listed in the order they get loaded.

* base - _"flux bootstrap"_
  * flux-system - flux gitops controllers & configuration
* crds - custom resource definitions
* namespaces - self explainatory
* operators - operators that handle/manage resources
* core - underlying infrastructure services
  * cert-manager - handles tls certificates
  * hardware - node feature discovery
  * kasten - k10 backup system
  * metallb - bgp load balancers
  * rook-ceph - PVC storage
* apps
  * dev - development tools
  * home - home automation
  * media - media management
  * network - networking related tools
  * services - general services
  * system-ingress - ingress related resources
  * system-monitor - grafana/prometheus/loki stack
