<img src="https://camo.githubusercontent.com/5b298bf6b0596795602bd771c5bddbb963e83e0f/68747470733a2f2f692e696d6775722e636f6d2f7031527a586a512e706e67" align="left" width="144px" height="144px"/>

#### Homelab K8s-Gitops
> GitOps state for my cluster using flux v2

[![Discord](https://img.shields.io/badge/discord-chat-7289DA.svg?maxAge=60&style=flat-square)](https://discord.gg/DNCynrJ)
[![k3s](https://img.shields.io/badge/k3s-v1.19.2-orange?style=flat-square)](https://k3s.io/)
[![GitHub issues](https://img.shields.io/github/issues/zacheryph/k8s-gitops?style=flat-square)](https://github.com/zacheryph/k8s-gitops/issues)
[![GitHub last commit](https://img.shields.io/github/last-commit/zacheryph/k8s-gitops?color=purple&style=flat-square)](https://github.com/zacheryph/k8s-gitops/commits/master)

<br/>

## Overview

## Hardware

Cluster is 3 built 1u servers with the following hardware.

* Inwin 1W-RF100S Chassis
* ASRock Rack E3C246D2I
* Intel Core i3-9100
* 16GB Memory
* 128GB M.2 2242 SSD (OS)
* 2x 6TB HGST Ultrastar (longhorn)

## Services

* Flux-System - The flux v2 manifests
  * helm-repositories - `HelmRepository` resources
* System
  * ingress - ingress-nginx / cert-manager
  * kubedb - kubedb operator
  * longhorn - persistent storage
  * metallb - metallb running in bgp mode
  * prometheus - prometheus / grafana / loki
  * sealed-secrets - committable secrets
* Network
  * blocky - blocky dns server
  * minio - minio instances for public and internal use
* Services
  * dashboard - heimdall dashboard
  * home-assistant - hass / mosquitto-mqtt / openzwave
  * wiki - wiki.js instance
* Devops
  * drone - ci server
  * drone-build - namespace for done builds
  * drone-secrets - houses secrets for drone pipelines
  * gitea - git management server
  * registry - harbor docker registry
  * sonarqube - source code scanner
