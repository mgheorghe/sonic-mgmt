"""UHD Connect per-port stats helper (Keysight UHD "connect" API).

Mirrors the working approach in snic/work/4_uhd_stats.py and
snic/pyIxia_gftv3/pyUhdLib/Uhd.py: per-port traffic + L1 counters come from

    POST http://<uhd_ip>:80/connect/api/v1/metrics/operations/query
        {"port_metrics": {"port_names": [...], "select_metrics": [...]}}

and metrics are cleared with

    POST http://<uhd_ip>:80/connect/api/v1/metrics/operations/clear

Port names are the UHD physical port names ("Port 1", "Port 13/3", ...), not the
logical connection endpoint names.
"""
import logging

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
    from urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
except Exception:  # pragma: no cover
    Retry = None

logger = logging.getLogger(__name__)

# Default metric set (same as 4_uhd_stats.py).
DEFAULT_METRICS = [
    "link_status",
    "frames_received_all",
    "frames_received_unicast",
    "frames_transmitted_all",
    "frames_transmitted_unicast",
    "frames_dropped_egress",
]

_TIMEOUT = 5


def _session():
    s = requests.Session()
    if Retry is not None:
        retries = Retry(total=3, backoff_factor=0.3, status_forcelist=(500, 502, 503, 504))
        s.mount("http://", HTTPAdapter(max_retries=retries))
        s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


def clear_metrics(uhd_ip):
    """Zero the UHD metric counters."""
    url = f"http://{uhd_ip}:80/connect/api/v1/metrics/operations/clear"  # noqa: E231
    try:
        r = _session().post(url, headers={"content-type": "application/json"},
                            verify=False, timeout=_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        logger.warning("UHD clear_metrics failed: %s", e)


def query_metrics(uhd_ip, port_names, metrics=None):
    """Return {port_name: {metric: value}} for the requested ports/metrics."""
    metrics = metrics or DEFAULT_METRICS
    url = f"http://{uhd_ip}:80/connect/api/v1/metrics/operations/query"  # noqa: E231
    body = {"port_metrics": {"port_names": port_names, "select_metrics": metrics}}
    try:
        r = _session().post(url, json=body, headers={"content-type": "application/json"},
                            verify=False, timeout=_TIMEOUT)
        blob = r.json()
    except Exception as e:
        logger.warning("UHD query_metrics failed: %s", e)
        return {}
    out = {}
    for pm in blob.get("port_metrics", {}).get("metrics", []):
        out[pm.get("port_name")] = pm.get("metrics", {})
    return out


def _is_link_up(val):
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "link_up", "up", "on")


def log_uhd_table(uhd_ip, port_names, label="", metrics=None, prev=None):
    """Query + log a UHD per-port metric table. Returns the raw snapshot (for deltas)."""
    snap = query_metrics(uhd_ip, port_names, metrics)
    if not snap:
        logger.info("  UHD %s: no metrics (ports not in loaded config / UHD down?)", label)
        return snap
    cols = metrics or DEFAULT_METRICS
    logger.info("  UHD per-port metrics %s (uhd=%s):", label, uhd_ip)
    logger.info("    %-16s  %s", "port", "  ".join("%-22s" % c for c in cols))
    for pn in port_names:
        m = snap.get(pn)
        if not m:
            continue
        cells = []
        for c in cols:
            v = m.get(c, "-")
            if c != "link_status" and prev and prev.get(pn):
                try:
                    v = "%d (+%d)" % (int(v), int(v) - int(prev[pn].get(c, 0)))
                except (ValueError, TypeError):
                    pass
            elif c == "link_status":
                v = "UP" if _is_link_up(v) else "DOWN"
            cells.append(str(v))
        logger.info("    %-16s  %s", pn, "  ".join("%-22s" % c for c in cells))
    return snap
