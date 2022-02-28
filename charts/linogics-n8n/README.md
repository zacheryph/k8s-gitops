# n8n

![Version: 0.136.0-v6](https://img.shields.io/badge/Version-0.136.0--v6-informational?style=flat-square) ![Type: application](https://img.shields.io/badge/Type-application-informational?style=flat-square) ![AppVersion: 0.136.0](https://img.shields.io/badge/AppVersion-0.136.0-informational?style=flat-square)

A N8N Helm chart for Kubernetes. n8n is a free and open fair-code licensed node-based Workflow Automation Tool.

**Homepage:** <https://github.com/linogics/helm-charts/tree/master/charts/n8n>

## Source Code

* <https://n8n.io/>
* <https://github.com/n8n-io/n8n>
* <https://github.com/linogics/helm-charts/tree/master/charts/n8n>

## Requirements

| Repository                         | Name       | Version |
| ---------------------------------- | ---------- | ------- |
| https://charts.bitnami.com/bitnami | mariadb    | 9.4.4   |
| https://charts.bitnami.com/bitnami | mysql      | 8.8.6   |
| https://charts.bitnami.com/bitnami | postgresql | 10.9.4  |
| https://charts.bitnami.com/bitnami | redis      | 15.3.1  |

## Values

| Key                                        | Type   | Default                 | Description |
| ------------------------------------------ | ------ | ----------------------- | ----------- |
| affinity                                   | object | `{}`                    |             |
| autoscaling.enabled                        | bool   | `false`                 |             |
| autoscaling.maxReplicas                    | int    | `100`                   |             |
| autoscaling.minReplicas                    | int    | `1`                     |             |
| autoscaling.targetCPUUtilizationPercentage | int    | `80`                    |             |
| env                                        | object | `{}`                    |             |
| envFrom                                    | list   | `[]`                    |             |
| envValueFrom                               | object | `{}`                    |             |
| extraVolumeMounts                          | list   | `[]`                    |             |
| extraVolumes                               | list   | `[]`                    |             |
| fullnameOverride                           | string | `""`                    |             |
| image.pullPolicy                           | string | `"IfNotPresent"`        |             |
| image.repository                           | string | `"n8nio/n8n"`           |             |
| image.tag                                  | string | `""`                    |             |
| imagePullSecrets                           | list   | `[]`                    |             |
| ingress.annotations                        | object | `{}`                    |             |
| ingress.className                          | string | `""`                    |             |
| ingress.enabled                            | bool   | `false`                 |             |
| ingress.hosts[0].host                      | string | `"chart-example.local"` |             |
| ingress.hosts[0].paths[0].path             | string | `"/"`                   |             |
| ingress.hosts[0].paths[0].pathType         | string | `"Prefix"`              |             |
| ingress.labels                             | object | `{}`                    |             |
| ingress.tls                                | list   | `[]`                    |             |
| livenessProbe.enabled                      | bool   | `true`                  |             |
| livenessProbe.httpGet.path                 | string | `"/healthz"`            |             |
| livenessProbe.httpGet.port                 | string | `"http"`                |             |
| livenessProbe.initialDelaySeconds          | int    | `15`                    |             |
| livenessProbe.periodSeconds                | int    | `10`                    |             |
| mariadb.auth.database                      | string | `"n8n"`                 |             |
| mariadb.auth.existingSecret                | string | `""`                    |             |
| mariadb.auth.password                      | string | `"Change_Me_!"`         |             |
| mariadb.auth.rootPassword                  | string | `"Change_Me_!"`         |             |
| mariadb.auth.username                      | string | `"n8n"`                 |             |
| mariadb.enabled                            | bool   | `false`                 |             |
| mariadb.persistence.enabled                | bool   | `true`                  |             |
| mariadb.persistence.existingClaim          | string | `""`                    |             |
| mariadb.persistence.size                   | string | `"8Gi"`                 |             |
| mysql.auth.database                        | string | `"n8n"`                 |             |
| mysql.auth.existingSecret                  | string | `""`                    |             |
| mysql.auth.password                        | string | `"Change_Me_!"`         |             |
| mysql.auth.rootPassword                    | string | `"Change_Me_!"`         |             |
| mysql.auth.username                        | string | `"n8n"`                 |             |
| mysql.enabled                              | bool   | `false`                 |             |
| mysql.persistence.enabled                  | bool   | `true`                  |             |
| mysql.persistence.existingClaim            | string | `""`                    |             |
| mysql.persistence.size                     | string | `"2Gi"`                 |             |
| nameOverride                               | string | `""`                    |             |
| nodeSelector                               | object | `{}`                    |             |
| persistence.accessMode                     | string | `"ReadWriteOnce"`       |             |
| persistence.annotations                    | object | `{}`                    |             |
| persistence.enabled                        | bool   | `false`                 |             |
| persistence.size                           | string | `"2Gi"`                 |             |
| podAnnotations                             | object | `{}`                    |             |
| podSecurityContext.fsGroup                 | int    | `1000`                  |             |
| podSecurityContext.fsGroupChangePolicy     | string | `"OnRootMismatch"`      |             |
| podSecurityContext.runAsGroup              | int    | `1000`                  |             |
| podSecurityContext.runAsUser               | int    | `1000`                  |             |
| postgresql.enabled                         | bool   | `false`                 |             |
| postgresql.existingSecret                  | string | `""`                    |             |
| postgresql.persistence.enabled             | bool   | `true`                  |             |
| postgresql.persistence.existingClaim       | string | `""`                    |             |
| postgresql.persistence.size                | string | `"2Gi"`                 |             |
| postgresql.postgresqlDatabase              | string | `"n8n"`                 |             |
| postgresql.postgresqlPassword              | string | `"Change_Me_!"`         |             |
| postgresql.postgresqlPostgresPassword      | string | `"Change_Me_!"`         |             |
| postgresql.postgresqlUsername              | string | `"n8n"`                 |             |
| readinessProbe.enabled                     | bool   | `true`                  |             |
| readinessProbe.httpGet.path                | string | `"/healthz"`            |             |
| readinessProbe.httpGet.port                | string | `"http"`                |             |
| readinessProbe.initialDelaySeconds         | int    | `15`                    |             |
| readinessProbe.periodSeconds               | int    | `10`                    |             |
| redis.architecture                         | string | `"standalone"`          |             |
| redis.auth.enabled                         | bool   | `false`                 |             |
| redis.auth.existingSecret                  | string | `""`                    |             |
| redis.auth.password                        | string | `"Change_Me_!"`         |             |
| redis.enabled                              | bool   | `false`                 |             |
| redis.persistence.enabled                  | bool   | `true`                  |             |
| redis.persistence.existingClaim            | string | `""`                    |             |
| redis.persistence.size                     | string | `"2Gi"`                 |             |
| replicaCount                               | int    | `1`                     |             |
| resources                                  | object | `{}`                    |             |
| securityContext.allowPrivilegeEscalation   | bool   | `false`                 |             |
| securityContext.capabilities.drop[0]       | string | `"ALL"`                 |             |
| securityContext.readOnlyRootFilesystem     | bool   | `true`                  |             |
| securityContext.runAsNonRoot               | bool   | `true`                  |             |
| securityContext.runAsUser                  | int    | `1000`                  |             |
| service.port                               | int    | `80`                    |             |
| service.type                               | string | `"ClusterIP"`           |             |
| serviceAccount.annotations                 | object | `{}`                    |             |
| serviceAccount.create                      | bool   | `false`                 |             |
| serviceAccount.name                        | string | `""`                    |             |
| tolerations                                | list   | `[]`                    |             |

----------------------------------------------
Autogenerated from chart metadata using [helm-docs v1.5.0](https://github.com/norwoodj/helm-docs/releases/v1.5.0)
