<img src="https://camo.githubusercontent.com/5b298bf6b0596795602bd771c5bddbb963e83e0f/68747470733a2f2f692e696d6775722e636f6d2f7031527a586a512e706e67" align="left" width="144px" height="144px"/>

#### Homelab K8s-Gitops
> GitOps state for my cluster using flux v2

[![Discord](https://img.shields.io/badge/discord-chat-7289DA.svg?maxAge=60&style=flat-square)](https://discord.gg/DNCynrJ)
[![k3s](https://img.shields.io/badge/k3s-v1.19.2-orange?style=flat-square)](https://k3s.io/)
[![GitHub issues](https://img.shields.io/github/issues/zacheryph/k8s-gitops?style=flat-square)](https://github.com/zacheryph/k8s-gitops/issues)
[![GitHub last commit](https://img.shields.io/github/last-commit/zacheryph/k8s-gitops?color=purple&style=flat-square)](https://github.com/zacheryph/k8s-gitops/commits/master)

<br/>

## Overview

## Secret Management

Secrets are managed by `bin/secrets.sh`. Below is a short description of
the commands and the two types of files that are automatically generated.
All secrets are able to use environment variables from `.secrets.env` which
is secured by git-crypt.

Refreshing of secrets have the caveat of only knowing if the source file is
newer than the sealed secret. This does not account for changed to
`.secrets.env` that affect the secret. If changes are made to existing values
you will need to touch the secret[s] affected or remove their sealed secret
counterparts.

Secrets are generated into `cluster/secrets`, and the `kustomization.yaml`
automatically generated containing them all. Each secret exists in their
respective namespace which is extracted from the `kustomization.yaml` within
the same directory the secret exists in.

As an added bonus there is a pre-commit hook to ensure all sealed secrets
exist and are up to date so that you do not forget to generate any new
ones.

### Secret Commands

* `./bin/secrets.sh check` -  ensures all `SealedSecret` resources exist
* `./bin/secrets.sh refresh` -  create & update any secrets necessary
* `./bin/secrets.sh write` -  recreate all secrets
* `./bin/secrets.sh wipe` -  destroy all `SealedSecret` resources

### Secret Types

#### `secret-name.crypt.env`

> env format file that creates a secret with a key for each
> _environment variable_. The secret name is the name of the
> file less the crypt.env suffix.

#### `secret-name.values.yaml`

> this is for `HelmRelease` style values. They will generate a secret
> with a `values.yaml` key containing the contents of this file. The
> secret generated will be names `secret-name-values`.

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
