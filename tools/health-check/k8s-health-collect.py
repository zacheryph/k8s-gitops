#!/usr/bin/env python3
"""
k8s cluster health collector.

Deterministic data-gathering pass for the k8s health-check cron. No LLM.
Reads the cluster state via the in-cluster Python kubernetes client, emits
severity-tagged findings (OK / INFO / WARN / CRIT) grouped by section, and
dumps both a full state snapshot and a findings-only file for the
narrator LLM to consume.

Output files (default: /opt/data/cron/output/k8s-health/):
  - <ISO-timestamp>.json  : full cluster state (for diffing / forensics)
  - findings.json          : severity-tagged findings (what the LLM reads)
  - latest.json            : symlink to the most recent timestamp file

Stdout: brief summary the cron job injects into the LLM prompt as context.

Sections (in order; each is a function that returns List[Finding]):
  1. pods               — CrashLoop / ImagePull / Pending / restart counts
  2. workloads          — Deployments / StatefulSets / DaemonSets readiness
  3. flux_kustomizations
  4. flux_helmreleases
  5. flux_sources       — Git/Helm/OCI Repositories, HelmCharts, Buckets
  6. certificates       — Ready / expiry
  7. cnpg               — CloudNativePG clusters
  8. strimzi            — Kafka brokers / topics / users
  9. longhorn_volumes
 10. longhorn_nodes
 11. events             — Warning events, deduped

Run manually:  python3 /opt/data/scripts/k8s-health-collect.py
Prints the summary, exit 0 always (errors are surfaced as findings).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

# In-cluster auth (also works from a real shell on the Hermes pod).
# The execute_code sandbox strips KUBERNETES_SERVICE_HOST/PORT; in a
# shell context those are set by the kubelet and we can skip the
# manual override.
if not os.environ.get("KUBERNETES_SERVICE_HOST"):
    os.environ["KUBERNETES_SERVICE_HOST"] = socket.gethostbyname("kubernetes.default.svc")
    os.environ["KUBERNETES_SERVICE_PORT"] = "443"

from kubernetes import client, config  # noqa: E402  (env must be set first)
config.load_incluster_config()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OUT_DIR = Path(os.environ.get("K8S_HEALTH_OUT_DIR", "/opt/data/cron/output/k8s-health"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Thresholds
CERT_WARN_DAYS = 30
CERT_CRIT_DAYS = 7
RESTART_WARN_COUNT = 20           # lifetime threshold (raised from 5 to cut noise)
RECENT_CRASH_HOURS = 1            # lastState.terminated in this window = CRIT
PENDING_WARN_MINUTES = 5
EVENT_LOOKBACK_HOURS = 6          # matches the cron cadence
MAX_FINDINGS_PER_SECTION = 50     # cap to keep findings.json bounded

SEVERITY_ORDER = {"CRIT": 0, "WARN": 1, "INFO": 2, "OK": 3}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_rfc3339(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # tolerate trailing 'Z'
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def age_minutes(ts: Optional[str]) -> Optional[float]:
    dt = parse_rfc3339(ts)
    if not dt:
        return None
    return (now_utc() - dt).total_seconds() / 60.0


def days_until(ts: Optional[str]) -> Optional[float]:
    dt = parse_rfc3339(ts)
    if not dt:
        return None
    return (dt - now_utc()).total_seconds() / 86400.0


def cond(obj: dict, ctype: str) -> Optional[dict]:
    """Return the named condition from a k8s status object, or None."""
    for c in obj.get("status", {}).get("conditions", []) or []:
        if c.get("type") == ctype:
            return c
    return None


def is_ready(obj: dict) -> bool:
    c = cond(obj, "Ready")
    return bool(c and c.get("status") == "True")


# ---------------------------------------------------------------------------
# Finding model
# ---------------------------------------------------------------------------

class Finding:
    __slots__ = ("severity", "section", "resource", "message", "details")

    def __init__(self, severity: str, section: str, resource: str,
                 message: str, details: Optional[dict] = None):
        self.severity = severity
        self.section = section
        self.resource = resource
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "section": self.section,
            "resource": self.resource,
            "message": self.message,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Section collectors
# ---------------------------------------------------------------------------

def _list_pods() -> tuple[list, list]:
    core = client.CoreV1Api()
    pods = core.list_pod_for_all_namespaces().items
    events = core.list_event_for_all_namespaces().items
    return pods, events


def section_pods() -> list[Finding]:
    out: list[Finding] = []
    try:
        pods, _ = _list_pods()
    except Exception as e:
        return [Finding("CRIT", "pods", "cluster",
                        f"could not list pods: {e}")]

    bad_phases = Counter()
    for p in pods:
        ns = p.metadata.namespace
        name = p.metadata.name
        phase = p.status.phase

        # Phase-level checks (Pending is the most interesting)
        if phase == "Pending":
            age = age_minutes(p.metadata.creation_timestamp.isoformat()
                              if p.metadata.creation_timestamp else None)
            mins = f" ({age:.0f}m)" if age is not None else ""
            out.append(Finding("WARN", "pods", f"{ns}/{name}",
                               f"Pod Pending{mins}",
                               {"namespace": ns, "pod": name, "phase": phase,
                                "age_minutes": round(age) if age else None}))
            bad_phases["Pending"] += 1
        elif phase == "Failed":
            out.append(Finding("CRIT", "pods", f"{ns}/{name}",
                               "Pod Failed",
                               {"namespace": ns, "pod": name, "phase": phase}))
            bad_phases["Failed"] += 1
        elif phase == "Unknown":
            out.append(Finding("WARN", "pods", f"{ns}/{name}",
                               "Pod phase Unknown",
                               {"namespace": ns, "pod": name, "phase": phase}))
            bad_phases["Unknown"] += 1

        # Container-level checks
        for cs in (p.status.container_statuses or []) + (p.status.init_container_statuses or []):
            cname = cs.name
            waiting = (cs.state.waiting.reason if cs.state and cs.state.waiting else None)
            terminated = (cs.state.terminated.reason if cs.state and cs.state.terminated else None)

            if waiting in ("CrashLoopBackOff",):
                out.append(Finding("CRIT", "pods", f"{ns}/{name}",
                                   f"Container {cname} {waiting} "
                                   f"(restarts={cs.restart_count})",
                                   {"namespace": ns, "pod": name,
                                    "container": cname, "reason": waiting,
                                    "restarts": cs.restart_count}))
            elif waiting in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
                out.append(Finding("CRIT", "pods", f"{ns}/{name}",
                                   f"Container {cname} {waiting}",
                                   {"namespace": ns, "pod": name,
                                    "container": cname, "reason": waiting}))
            elif waiting in ("CreateContainerConfigError", "CreateContainerError"):
                out.append(Finding("CRIT", "pods", f"{ns}/{name}",
                                   f"Container {cname} {waiting}",
                                   {"namespace": ns, "pod": name,
                                    "container": cname, "reason": waiting}))
            elif waiting and waiting not in ("ContainerCreating", "PodInitializing"):
                out.append(Finding("WARN", "pods", f"{ns}/{name}",
                                   f"Container {cname} waiting: {waiting}",
                                   {"namespace": ns, "pod": name,
                                    "container": cname, "reason": waiting,
                                    "restarts": cs.restart_count}))

            # Suppress the lifetime-restart WARN when the container is
            # currently healthy. A pod that has been Running+Ready for
            # weeks with restarts=25 from a one-off cluster event
            # (e.g. May 16 node reboot) is not an active problem. The
            # "recently crashed" check below already covers fresh
            # terminations within the last hour with much higher
            # signal (as CRIT). This block also still flags WARNs
            # within a 24h window so a pod that crashed earlier today
            # but has since recovered doesn't fall through the cracks.
            ls = cs.last_state
            last_term_age_hours: Optional[float] = None
            if ls and ls.terminated and ls.terminated.finished_at:
                last_term_age_hours = (
                    (now_utc() - parse_rfc3339(
                        ls.terminated.finished_at.isoformat()
                    )).total_seconds() / 3600
                )
            recently_crashed = (
                last_term_age_hours is not None
                and 0 <= last_term_age_hours < 24
            )

            if (cs.restart_count >= RESTART_WARN_COUNT
                    and phase == "Running"
                    and (not cs.ready or recently_crashed)):
                age_note = (f" (last crash {last_term_age_hours*60:.0f}m ago)"
                            if recently_crashed else "")
                out.append(Finding("WARN", "pods", f"{ns}/{name}",
                                   f"Container {cname} restarted "
                                   f"{cs.restart_count} times{age_note}",
                                   {"namespace": ns, "pod": name,
                                    "container": cname,
                                    "restarts": cs.restart_count,
                                    "ready": cs.ready,
                                    "last_crash_hours_ago": (
                                        round(last_term_age_hours, 1)
                                        if last_term_age_hours is not None
                                        else None)}))

            # "Recently crashed" — lastState.terminated in the last hour
            # is a much higher-signal indicator than lifetime restart count.
            if ls and ls.terminated:
                t = ls.terminated
                finished = t.finished_at
                if finished:
                    age = (now_utc() - parse_rfc3339(finished.isoformat())).total_seconds() / 3600
                    if 0 <= age < RECENT_CRASH_HOURS and t.reason in ("Error", None):
                        out.append(Finding("CRIT", "pods", f"{ns}/{name}",
                                           f"Container {cname} crashed "
                                           f"({age*60:.0f}m ago, restart #{cs.restart_count})",
                                           {"namespace": ns, "pod": name,
                                            "container": cname,
                                            "last_terminated_reason": t.reason,
                                            "last_terminated_exit": t.exit_code,
                                            "minutes_since": round(age*60),
                                            "restarts": cs.restart_count}))

            if terminated == "OOMKilled":
                out.append(Finding("WARN", "pods", f"{ns}/{name}",
                                   f"Container {cname} OOMKilled "
                                   f"(restarts={cs.restart_count})",
                                   {"namespace": ns, "pod": name,
                                    "container": cname, "reason": "OOMKilled",
                                    "restarts": cs.restart_count}))

    return out[:MAX_FINDINGS_PER_SECTION]


def section_workloads() -> list[Finding]:
    out: list[Finding] = []
    apps = client.AppsV1Api()
    try:
        deps = apps.list_deployment_for_all_namespaces().items
    except Exception as e:
        return [Finding("CRIT", "workloads", "cluster", f"list deployments failed: {e}")]
    for d in deps:
        spec = d.spec.replicas or 0
        ready = d.status.ready_replicas or 0
        if spec == 0:
            continue
        if ready < spec:
            sev = "CRIT" if ready == 0 else "WARN"
            out.append(Finding(sev, "workloads",
                               f"{d.metadata.namespace}/{d.metadata.name}",
                               f"Deployment {ready}/{spec} ready",
                               {"kind": "Deployment", "namespace": d.metadata.namespace,
                                "name": d.metadata.name, "ready": ready, "desired": spec}))

    try:
        sts = apps.list_stateful_set_for_all_namespaces().items
    except Exception as e:
        out.append(Finding("CRIT", "workloads", "cluster", f"list statefulsets failed: {e}"))
        sts = []
    for s in sts:
        spec = s.spec.replicas or 0
        ready = s.status.ready_replicas or 0
        if spec == 0:
            continue
        if ready < spec:
            sev = "CRIT" if ready == 0 else "WARN"
            out.append(Finding(sev, "workloads",
                               f"{s.metadata.namespace}/{s.metadata.name}",
                               f"StatefulSet {ready}/{spec} ready",
                               {"kind": "StatefulSet", "namespace": s.metadata.namespace,
                                "name": s.metadata.name, "ready": ready, "desired": spec}))

    try:
        dss = apps.list_daemon_set_for_all_namespaces().items
    except Exception as e:
        out.append(Finding("CRIT", "workloads", "cluster", f"list daemonsets failed: {e}"))
        dss = []
    for ds in dss:
        desired = ds.status.desired_number_scheduled or 0
        ready = ds.status.number_ready or 0
        if desired == 0:
            continue
        if ready < desired:
            sev = "CRIT" if ready == 0 else "WARN"
            out.append(Finding(sev, "workloads",
                               f"{ds.metadata.namespace}/{ds.metadata.name}",
                               f"DaemonSet {ready}/{desired} ready",
                               {"kind": "DaemonSet", "namespace": ds.metadata.namespace,
                                "name": ds.metadata.name, "ready": ready, "desired": desired}))
    return out[:MAX_FINDINGS_PER_SECTION]


def _flux_objects() -> dict[str, list]:
    api = client.CustomObjectsApi()
    out: dict[str, list] = {}
    sources = [
        ("kustomize.toolkit.fluxcd.io", "v1", "kustomizations", "kustomizations"),
        ("helm.toolkit.fluxcd.io", "v2", "helmreleases", "helmreleases"),
        ("source.toolkit.fluxcd.io", "v1", "gitrepositories", "gitrepositories"),
        ("source.toolkit.fluxcd.io", "v1", "helmrepositories", "helmrepositories"),
        ("source.toolkit.fluxcd.io", "v1", "ocirepositories", "ocirepositories"),
        ("source.toolkit.fluxcd.io", "v1", "helmcharts", "helmcharts"),
        ("source.toolkit.fluxcd.io", "v1", "buckets", "buckets"),
    ]
    for g, v, p, key in sources:
        try:
            res = api.list_cluster_custom_object(g, v, p)
            out[key] = res.get("items", [])
        except Exception as e:
            if "403" in str(e):
                continue
            out[key] = []
            out[f"{key}__error"] = [str(e)[:120]]
    return out


def _flux_not_ready(findings: list, items: list, section: str, kind: str) -> None:
    for o in items:
        if is_ready(o):
            continue
        ns = o["metadata"].get("namespace", "")
        name = o["metadata"]["name"]
        c = cond(o, "Ready")
        msg = (c or {}).get("message", "Ready=False")
        sev = "WARN" if c else "INFO"
        findings.append(Finding(sev, section, f"{ns}/{name}" if ns else name,
                                f"{kind} not Ready: {msg[:100]}",
                                {"kind": kind, "namespace": ns or None,
                                 "name": name,
                                 "ready_status": (c or {}).get("status"),
                                 "ready_reason": (c or {}).get("reason"),
                                 "ready_message": msg[:200]}))


def section_flux_kustomizations() -> list[Finding]:
    out: list[Finding] = []
    objs = _flux_objects()
    _flux_not_ready(out, objs.get("kustomizations", []),
                    "flux_kustomizations", "Kustomization")
    return out[:MAX_FINDINGS_PER_SECTION]


def section_flux_helmreleases() -> list[Finding]:
    out: list[Finding] = []
    objs = _flux_objects()
    _flux_not_ready(out, objs.get("helmreleases", []),
                    "flux_helmreleases", "HelmRelease")
    return out[:MAX_FINDINGS_PER_SECTION]


def section_flux_sources() -> list[Finding]:
    out: list[Finding] = []
    objs = _flux_objects()
    for key, kind in [
        ("gitrepositories", "GitRepository"),
        ("helmrepositories", "HelmRepository"),
        ("ocirepositories", "OCIRepository"),
        ("helmcharts", "HelmChart"),
        ("buckets", "Bucket"),
    ]:
        _flux_not_ready(out, objs.get(key, []), "flux_sources", kind)
    return out[:MAX_FINDINGS_PER_SECTION]


def section_certificates() -> list[Finding]:
    out: list[Finding] = []
    api = client.CustomObjectsApi()
    try:
        certs = api.list_cluster_custom_object("cert-manager.io", "v1",
                                               "certificates").get("items", [])
    except Exception as e:
        return [Finding("CRIT", "certificates", "cluster",
                        f"list certificates failed: {str(e)[:120]}",
                        {})]
    for c in certs:
        ns = c["metadata"].get("namespace", "")
        name = c["metadata"]["name"]
        ready = is_ready(c)
        not_after = c.get("status", {}).get("notAfter")
        days = days_until(not_after)
        if not ready:
            cnd = cond(c, "Ready") or {}
            out.append(Finding("CRIT", "certificates", f"{ns}/{name}",
                               f"Certificate not Ready: "
                               f"{cnd.get('message', '')[:80]}",
                               {"namespace": ns, "name": name,
                                "not_after": not_after}))
            continue
        if days is None:
            out.append(Finding("WARN", "certificates", f"{ns}/{name}",
                               "Certificate Ready but no notAfter set",
                               {"namespace": ns, "name": name}))
        elif days < CERT_CRIT_DAYS:
            out.append(Finding("CRIT", "certificates", f"{ns}/{name}",
                               f"Certificate expires in {days:.1f} days",
                               {"namespace": ns, "name": name,
                                "not_after": not_after, "days_left": round(days, 1)}))
        elif days < CERT_WARN_DAYS:
            out.append(Finding("WARN", "certificates", f"{ns}/{name}",
                               f"Certificate expires in {days:.1f} days",
                               {"namespace": ns, "name": name,
                                "not_after": not_after, "days_left": round(days, 1)}))
    return out[:MAX_FINDINGS_PER_SECTION]


def section_cnpg() -> list[Finding]:
    out: list[Finding] = []
    api = client.CustomObjectsApi()
    try:
        clusters = api.list_cluster_custom_object(
            "postgresql.cnpg.io", "v1", "clusters").get("items", [])
    except Exception as e:
        if "403" in str(e):
            return []  # RBAC not yet in place; skip silently
        return [Finding("CRIT", "cnpg", "cluster",
                        f"list clusters failed: {str(e)[:120]}")]
    for c in clusters:
        ns = c["metadata"].get("namespace", "")
        name = c["metadata"]["name"]
        st = c.get("status", {}) or {}
        instances = st.get("instances", 0)
        ready_instances = st.get("readyInstances", 0)
        phase = st.get("phase", "Unknown")
        ready_cond = cond(c, "Ready")
        ready_ok = bool(ready_cond and ready_cond.get("status") == "True")
        if not ready_ok:
            sev = "CRIT" if ready_cond and ready_cond.get("status") == "False" else "WARN"
            out.append(Finding(sev, "cnpg", f"{ns}/{name}",
                               f"Cluster not Ready: {phase} "
                               f"({ready_instances}/{instances} ready, "
                               f"{(ready_cond or {}).get('message','')[:60]})",
                               {"namespace": ns, "name": name, "phase": phase,
                                "instances": instances, "ready_instances": ready_instances,
                                "ready_message": (ready_cond or {}).get("message","")[:200]}))
        elif ready_instances < instances:
            out.append(Finding("WARN", "cnpg", f"{ns}/{name}",
                               f"Cluster Ready but {ready_instances}/{instances} instances ready",
                               {"namespace": ns, "name": name, "phase": phase,
                                "instances": instances, "ready_instances": ready_instances}))
    return out[:MAX_FINDINGS_PER_SECTION]


def section_strimzi() -> list[Finding]:
    out: list[Finding] = []
    api = client.CustomObjectsApi()
    for g, v, p, kind in [
        ("kafka.strimzi.io", "v1", "kafkas", "Kafka"),
        ("kafka.strimzi.io", "v1", "kafkatopics", "KafkaTopic"),
        ("kafka.strimzi.io", "v1", "kafkausers", "KafkaUser"),
    ]:
        try:
            items = api.list_cluster_custom_object(g, v, p).get("items", [])
        except Exception as e:
            if "403" in str(e):
                continue
            out.append(Finding("CRIT", "strimzi", "cluster",
                               f"list {p} failed: {str(e)[:120]}"))
            continue
        for o in items:
            if is_ready(o):
                continue
            ns = o["metadata"].get("namespace", "")
            name = o["metadata"]["name"]
            c = cond(o, "Ready") or {}
            out.append(Finding("WARN", "strimzi", f"{ns}/{name}",
                               f"{kind} not Ready: {c.get('message','')[:80]}",
                               {"kind": kind, "namespace": ns, "name": name,
                                "ready_message": c.get("message", "")[:200]}))
    return out[:MAX_FINDINGS_PER_SECTION]


def section_longhorn_volumes() -> list[Finding]:
    out: list[Finding] = []
    api = client.CustomObjectsApi()
    try:
        vols = api.list_cluster_custom_object(
            "longhorn.io", "v1beta2", "volumes").get("items", [])
    except Exception as e:
        if "403" in str(e):
            return []
        return [Finding("CRIT", "longhorn_volumes", "cluster",
                        f"list volumes failed: {str(e)[:120]}")]
    for v in vols:
        ns = v["metadata"].get("namespace", "longhorn-system")
        name = v["metadata"]["name"]
        st = v.get("status", {}) or {}
        state = st.get("state", "Unknown")
        robustness = st.get("robustness", "Unknown")
        ks = st.get("kubernetesStatus", {}) or {}
        pv_status = ks.get("pvStatus", "N/A")
        # Healthy: state is "attached" and robustness is "healthy" (regardless of pvStatus,
        # since Released is normal for a deleted PVC).
        if state == "attached" and robustness == "healthy":
            continue
        sev = "OK"
        if robustness == "Faulted":
            sev = "CRIT"
        elif robustness == "Degraded":
            sev = "WARN"
        elif state == "Detached" and pv_status == "Bound":
            # Bound PVC but volume is detached — workload can't read its data
            sev = "WARN"
        elif state == "Faulted":
            sev = "CRIT"
        if sev == "OK":
            continue
        out.append(Finding(sev, "longhorn_volumes", f"{ns}/{name}",
                           f"Volume state={state} robustness={robustness} "
                           f"pvStatus={pv_status}",
                           {"namespace": ns, "name": name, "state": state,
                            "robustness": robustness, "pv_status": pv_status,
                            "pvc_namespace": ks.get("namespace"),
                            "pvc_name": ks.get("pvcName")}))
    return out[:MAX_FINDINGS_PER_SECTION]


def section_longhorn_nodes() -> list[Finding]:
    out: list[Finding] = []
    api = client.CustomObjectsApi()
    try:
        nodes = api.list_cluster_custom_object(
            "longhorn.io", "v1beta2", "nodes").get("items", [])
    except Exception as e:
        if "403" in str(e):
            return []
        return [Finding("CRIT", "longhorn_nodes", "cluster",
                        f"list nodes failed: {str(e)[:120]}")]
    for n in nodes:
        ns = n["metadata"].get("namespace", "longhorn-system")
        name = n["metadata"]["name"]
        st = n.get("status", {}) or {}
        # Longhorn conditions are a LIST (not a dict) — use cond() helper.
        ready_cond = cond(n, "Ready")
        ready_ok = bool(ready_cond and ready_cond.get("status") == "True")
        disks = st.get("diskStatus", {}) or {}
        schedulable = st.get("schedulable", True)
        if not ready_ok:
            out.append(Finding("CRIT", "longhorn_nodes", f"{ns}/{name}",
                               f"Longhorn node not Ready: "
                               f"{(ready_cond or {}).get('message','?')[:60]}",
                               {"namespace": ns, "name": name,
                                "ready_message": (ready_cond or {}).get("message","")[:200]}))
        for disk_name, d in disks.items():
            if isinstance(d, dict):
                storage_max = d.get("storageMaximum", 0) or 0
                storage_available = d.get("storageAvailable", 0) or 0
                if storage_max and (storage_available / storage_max) < 0.05:
                    out.append(Finding("WARN", "longhorn_nodes", f"{ns}/{name}",
                                       f"Disk {disk_name} nearly full "
                                       f"({storage_available}/{storage_max} bytes)",
                                       {"namespace": ns, "node": name,
                                        "disk": disk_name,
                                        "available": storage_available,
                                        "maximum": storage_max}))
        if not schedulable:
            out.append(Finding("INFO", "longhorn_nodes", f"{ns}/{name}",
                               "Longhorn node marked unschedulable",
                               {"namespace": ns, "name": name,
                                "schedulable": False}))
    return out[:MAX_FINDINGS_PER_SECTION]


def section_events() -> list[Finding]:
    out: list[Finding] = []
    core = client.CoreV1Api()
    cutoff = now_utc() - timedelta(hours=EVENT_LOOKBACK_HOURS)
    try:
        events = core.list_event_for_all_namespaces().items
    except Exception as e:
        return [Finding("CRIT", "events", "cluster",
                        f"list events failed: {str(e)[:120]}")]

    # Dedup by (reason, kind, namespace, name, message[:60]); keep latest timestamp.
    bucket: dict[tuple, dict] = {}
    for e in events:
        if e.type != "Warning":
            continue
        ts = e.last_timestamp or e.event_time or e.first_timestamp
        if ts and ts < cutoff:
            continue
        io = e.involved_object or {}
        key = (e.reason or "",
               io.kind or "", io.namespace or "", io.name or "",
               (e.message or "")[:60])
        prev = bucket.get(key)
        if not prev or (ts and prev.get("ts") and ts > prev["ts"]):
            bucket[key] = {
                "ts": ts,
                "reason": e.reason,
                "kind": io.kind,
                "namespace": io.namespace,
                "name": io.name,
                "message": e.message,
                "count": e.count or 1,
            }
    for k, v in bucket.items():
        ns = v["namespace"] or ""
        res = f"{ns}/{v['kind']}/{v['name']}" if ns else f"{v['kind']}/{v['name']}"
        out.append(Finding("WARN", "events", res,
                           f"{v['reason']}: {(v['message'] or '')[:100]}"
                           f" (x{v['count']})",
                           {"namespace": v["namespace"], "kind": v["kind"],
                            "name": v["name"], "reason": v["reason"],
                            "count": v["count"],
                            "message": (v["message"] or "")[:300]}))
    return out[:MAX_FINDINGS_PER_SECTION]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

SECTIONS = [
    ("pods", section_pods),
    ("workloads", section_workloads),
    ("flux_kustomizations", section_flux_kustomizations),
    ("flux_helmreleases", section_flux_helmreleases),
    ("flux_sources", section_flux_sources),
    ("certificates", section_certificates),
    ("cnpg", section_cnpg),
    ("strimzi", section_strimzi),
    ("longhorn_volumes", section_longhorn_volumes),
    ("longhorn_nodes", section_longhorn_nodes),
    ("events", section_events),
]


def collect() -> dict:
    started = now_utc()
    t0 = time.monotonic()
    full: dict[str, Any] = {"started_at": started.isoformat(), "sections": {}}
    findings: list[Finding] = []
    sections_run: list[str] = []
    sections_failed: list[dict] = []

    for name, fn in SECTIONS:
        try:
            t1 = time.monotonic()
            res = fn()
            dt = time.monotonic() - t1
            findings.extend(res)
            sections_run.append(f"{name} ({len(res)} findings, {dt:.2f}s)")
        except Exception as e:
            tb = traceback.format_exc(limit=2)
            sections_failed.append({"section": name, "error": str(e)[:200],
                                    "traceback": tb})
            # Also surface the failure as a CRIT finding
            findings.append(Finding("CRIT", name, "collector",
                                    f"section failed: {str(e)[:120]}"))

    # Sort findings: severity first, then section, then resource
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9),
                                 f.section, f.resource))

    duration = time.monotonic() - t0
    counts = Counter(f.severity for f in findings)
    summary_counts = {k: counts.get(k, 0) for k in ("CRIT", "WARN", "INFO", "OK")}

    finished = now_utc()

    result = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round(duration, 3),
        "summary_counts": summary_counts,
        "sections_run": sections_run,
        "sections_failed": sections_failed,
        "findings": [f.to_dict() for f in findings],
    }
    return result


def write_outputs(result: dict) -> Path:
    ts = result["started_at"].replace(":", "-")  # safe for filenames
    snap = OUT_DIR / f"{ts}.json"
    snap.write_text(json.dumps(result, indent=2, default=str))
    (OUT_DIR / "findings.json").write_text(json.dumps(result, indent=2, default=str))
    # latest.json — symlink if possible, else copy
    latest = OUT_DIR / "latest.json"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(snap.name)
    except OSError:
        # On some FS, symlinks may not work; copy instead
        latest.write_text(snap.read_text())
    return snap


def format_slack_message(result: dict) -> str:
    """Return a Slack-mrkdwn message. Empty string = silent (no findings)."""
    sc = result["summary_counts"]
    crit = sc.get("CRIT", 0)
    warn = sc.get("WARN", 0)
    info = sc.get("INFO", 0)
    if crit == 0 and warn == 0 and info == 0:
        return ""
    # Group findings by (severity, section) and cap each section at 5
    from collections import defaultdict
    buckets: dict[tuple, list] = defaultdict(list)
    for f in result["findings"]:
        buckets[(f["severity"], f["section"])].append(f)

    lines: list[str] = []
    lines.append(f":rotating_light: *k8s health: {crit} CRIT, {warn} WARN"
                 f"{f', {info} INFO' if info else ''}*")
    for sev in ("CRIT", "WARN", "INFO"):
        sev_buckets = {s: lst for (sv, s), lst in buckets.items() if sv == sev}
        if not sev_buckets:
            continue
        section_summary = ", ".join(
            f"{s}:{len(lst)}" for s, lst in sorted(sev_buckets.items())
        )
        lines.append(f"")
        lines.append(f"*{sev}* ({section_summary})")
        for section in sorted(sev_buckets):
            items = sev_buckets[section]
            shown = items[:5]
            for f in shown:
                lines.append(f"• `{f['resource']}` — {f['message']}")
            if len(items) > 5:
                lines.append(f"• (+{len(items) - 5} more in {section})")
    return "\n".join(lines)


def main() -> int:
    result = collect()
    snap_path = write_outputs(result)

    sc = result["summary_counts"]
    crit, warn, info, ok_ = sc["CRIT"], sc["WARN"], sc["INFO"], sc["OK"]

    # Verbose summary → stderr (cron-no_agent mode delivers stdout only)
    print(f"k8s health: {crit} CRIT, {warn} WARN, {info} INFO, {ok_} OK "
          f"({result['duration_seconds']}s)", file=sys.stderr)
    print(f"snapshot:   {snap_path}", file=sys.stderr)
    print(f"findings:   {OUT_DIR / 'findings.json'}", file=sys.stderr)
    print(f"sections:   {len(result['sections_run'])} run, "
          f"{len(result['sections_failed'])} failed", file=sys.stderr)
    if crit or warn:
        top = [f for f in result["findings"]
               if f["severity"] in ("CRIT", "WARN")][:5]
        print("top findings:", file=sys.stderr)
        for f in top:
            print(f"  {f['severity']:4} {f['section']:22} {f['resource']:60} "
                  f"{f['message'][:80]}", file=sys.stderr)

    # Slack message → stdout (delivered verbatim in no_agent mode)
    msg = format_slack_message(result)
    if msg:
        print(msg)
    # else: empty stdout = silent (no message to Slack)
    return 0


if __name__ == "__main__":
    sys.exit(main())
