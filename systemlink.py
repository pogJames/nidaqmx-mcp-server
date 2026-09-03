"""TDMS capture upload to the SystemLink File service.

POST {SERVER_URI}/nifile/v1/service-groups/Default/upload-files, multipart
file+metadata, 201 -> {"uri": ".../files/<id>"}. Nothing is kept on disk; a failed
upload raises.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

import numpy as np
import requests
import urllib3
from mcp.server.fastmcp import FastMCP
from nptdms import ChannelObject, GroupObject, RootObject, TdmsWriter

import config

SERVER_URI = config.SYSTEMLINK_URI
UPLOAD_PATH = "/nifile/v1/service-groups/Default/upload-files"
USER = config.SYSTEMLINK_USER
PASSWORD = config.SYSTEMLINK_PASSWORD
WORKSPACE = config.SYSTEMLINK_WORKSPACE
TIMEOUT_S = config.SYSTEMLINK_TIMEOUT_S
VERIFY_TLS = config.SYSTEMLINK_VERIFY_TLS
CACHE_DIR = Path(__file__).parent / "data" / "cache"

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _error(resp: requests.Response) -> str:
    try:
        return resp.json()["error"]["message"]
    except Exception:
        return (resp.text or resp.reason or "")[:200]


def _post(path: Path, props: dict[str, str]) -> str:
    """Upload one file; the id is the last segment of the returned URI."""
    if not (USER and PASSWORD):
        raise RuntimeError("set USER and PASSWORD at the top of systemlink.py")
    with open(path, "rb") as fh:
        resp = requests.post(
            SERVER_URI + UPLOAD_PATH,
            auth=(USER, PASSWORD),
            files={
                "file": (path.name, fh, "application/octet-stream"),
                "metadata": (None, json.dumps(props), "application/json"),
            },
            params={"workspace": WORKSPACE} if WORKSPACE else None,
            verify=VERIFY_TLS,
            timeout=TIMEOUT_S,
        )
    if not resp.ok:
        raise RuntimeError(f"upload of {path.name} failed "
                           f"[{resp.status_code}]: {_error(resp)}")
    return resp.json()["uri"].rsplit("/", 1)[-1]


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _to_channels(data, group: str, channel: str, props: dict,
                 names: Sequence[str] | None = None) -> list[ChannelObject]:
    """One channel for 1-D data, one per row for [chans][samples]."""
    arr = np.asarray(data, dtype=float)
    if arr.ndim == 1:
        return [ChannelObject(group, names[0] if names else channel, arr, properties=props)]
    labels = names if names else [f"{channel}_{i}" for i in range(len(arr))]
    return [ChannelObject(group, label, row, properties=props)
            for label, row in zip(labels, arr)]


def _write_tdms(path: Path, data, *, group: str, channel: str, units: str,
                sample_rate: float | None, file_props: dict,
                names: Sequence[str] | None = None) -> None:
    ch_props: dict[str, Any] = {"unit_string": units}
    if sample_rate:                       # gives TDMS viewers a real time axis
        ch_props["wf_increment"] = 1.0 / sample_rate
        ch_props["wf_start_offset"] = 0.0
    segment = [
        RootObject(properties=file_props),
        GroupObject(group, properties={}),
        *_to_channels(data, group, channel, ch_props, names),
    ]
    with TdmsWriter(str(path)) as writer:
        writer.write_segment(segment)


# NAMING ==========================================================================
# `kind` is the discriminator every uploaded file carries, and the filename prefix
# follows it so the SystemLink browser is scannable without opening metadata. It is
# set by the producing tool, never inferred from units: a 9234 read in voltage mode
# reports Volts and is still vibration data.

Kind = Literal["measurement", "vibration", "test-report", "vibration-report"]

PREFIX: dict[str, str] = {
    "measurement": "meas",
    "vibration": "vib",
    "test-report": "runrep",
    "vibration-report": "vibrep",
}


def name_for(kind: Kind, *parts: str, ext: str, stamp: bool = True) -> str:
    """`<prefix>_<parts…>_<utc>.<ext>`, empty parts dropped.

    `stamp=False` for fixtures: a reference recording has no acquisition time of its
    own, and a timestamp would imply one."""
    bits = [PREFIX[kind], *(_slug(p) for p in parts if p)]
    if stamp:
        bits.append(_timestamp())
    return "_".join(bits) + "." + ext.lstrip(".")


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in "-.") else "-" for c in str(text)]
    return "".join(keep).strip("-")


def upload_samples(
    data: Sequence[float],
    *,
    kind: Kind = "measurement",
    name: str | None = None,
    device: str = "",
    channel: str = "ai0",
    names: Sequence[str] | None = None,
    units: str = "Volts",
    sample_rate: float | None = None,
    group: str = "Measurement",
    tags: dict | None = None,
) -> dict:
    """Write `data` to TDMS and upload it. 1-D for one channel, 2-D for several.

    `names` labels the channels; `units` is what the samples are in. `kind` picks the
    filename prefix and the metadata class. Returns {"file_id", "filename",
    "properties"}.
    """
    arr = np.asarray(data, dtype=float)
    channels = list(names) if names else [channel]
    filename = name or name_for(kind, device, "-".join(channels), ext="tdms")
    if not filename.endswith(".tdms"):
        filename += ".tdms"

    # duration and sample count live inside the TDMS binary; as properties they make a
    # recording listable without downloading it.
    n = int(arr.shape[-1]) if arr.ndim else 0
    props: dict[str, Any] = {
        "kind": kind,
        "source": "daqmx",
        "created_by": "upload_samples",
        **({"device": device} if device else {}),
        "channels": ",".join(channels),
        "units": units,
        "samples": n,
        "original_name": filename,
        **({"sample_rate_hz": sample_rate} if sample_rate else {}),
        **({"duration_s": round(n / sample_rate, 4)} if sample_rate and n else {}),
        **(tags or {}),
    }
    props = {k: str(v) for k, v in props.items()}   # metadata is str -> str

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / filename
        _write_tdms(path, data, group=group, channel=channel, units=units,
                    sample_rate=sample_rate, file_props=props, names=names)
        file_id = _post(path, props)

    return {"file_id": file_id, "filename": filename, "properties": props}


def upload_file(path: str, *, name: str | None = None, tags: dict | None = None) -> dict:
    """Upload an existing file. `name` renames it on the way up; the caller supplies
    `kind` and `source` through `tags` — this helper does not know what it is holding."""
    p = Path(path)
    filename = name or p.name
    props = {k: str(v) for k, v in {"original_name": filename, **(tags or {})}.items()}
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / filename          # the upload name is what SystemLink shows
        staged.write_bytes(p.read_bytes())
        file_id = _post(staged, props)
    return {"file_id": file_id, "filename": filename, "properties": props}


def update_metadata(file_id: str, properties: dict, *, replace: bool = False) -> dict:
    """Merge (or replace) properties on an already-uploaded file. Setting `Name`
    renames it, so a file can be brought onto the convention without re-uploading."""
    r = requests.post(
        f"{SERVER_URI}/nifile/v1/service-groups/Default/files/{file_id}/update-metadata",
        auth=(USER, PASSWORD), verify=VERIFY_TLS, timeout=TIMEOUT_S,
        json={"replaceExisting": replace,
              "properties": {k: str(v) for k, v in properties.items()}})
    if not r.ok:
        raise RuntimeError(f"metadata update for {file_id} failed "
                           f"[{r.status_code}]: {_error(r)}")
    return {"file_id": file_id, "updated": properties}


def query_files(**properties: str) -> list[dict]:
    """Files whose properties all match, e.g. query_files(kind="vibration").

    Server-side: PropertyQuery only does string EQUAL/CONTAINS, so there is no numeric
    comparison — filter on rate or duration after this returns."""
    body = {"propertiesQuery": [{"key": k, "operation": "EQUAL", "value": str(v)}
                                for k, v in properties.items()]}
    r = requests.post(
        f"{SERVER_URI}/nifile/v1/service-groups/Default/query-files",
        auth=(USER, PASSWORD), verify=VERIFY_TLS, timeout=TIMEOUT_S, json=body)
    if not r.ok:
        raise RuntimeError(f"file query failed [{r.status_code}]: {_error(r)}")
    return r.json().get("availableFiles", [])


def file_info(file_id: str) -> dict:
    """Metadata for one file, by id."""
    r = requests.get(f"{SERVER_URI}/nifile/v1/service-groups/Default/files",
                     auth=(USER, PASSWORD), verify=VERIFY_TLS, timeout=TIMEOUT_S,
                     params={"id": file_id})
    if not r.ok:
        raise RuntimeError(f"lookup of {file_id} failed [{r.status_code}]: {_error(r)}")
    files = r.json().get("availableFiles", [])
    if not files:
        raise RuntimeError(f"no file with id {file_id}")
    return files[0]


def fetch(file_id: str) -> Path:
    """Download once into the cache and return the local path.

    The service has no partial read — a Range request is ignored and the whole file
    comes back — so every access is a full transfer, and the workflow fetches the same
    file twice (analyse, then report). Caching makes that one transfer. File ids are
    immutable, so a cached copy can never go stale.

    Written via a temp file and renamed: a download interrupted halfway would otherwise
    leave a truncated file that every later call would trust."""
    info = file_info(file_id)
    name = info["properties"].get("Name") or info["properties"].get("original_name", "")
    out = CACHE_DIR / f"{file_id}{Path(name).suffix or '.bin'}"
    if out.exists():
        return out

    r = requests.get(
        f"{SERVER_URI}/nifile/v1/service-groups/Default/files/{file_id}/data",
        auth=(USER, PASSWORD), verify=VERIFY_TLS, timeout=TIMEOUT_S)
    if not r.ok:
        raise RuntimeError(f"download of {file_id} failed [{r.status_code}]: {_error(r)}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    tmp.write_bytes(r.content)
    tmp.replace(out)
    return out


def _summary(f: dict) -> dict:
    """The listing view: identity plus the properties worth scanning, without the
    _links noise or the server-side path."""
    p = f.get("properties", {})
    keep = ("kind", "source", "class", "device", "channels", "units",
            "sample_rate_hz", "duration_s", "samples", "rpm", "bearing",
            "plan_id", "verdict", "source_file", "leading_candidate")
    return {"file_id": f.get("id"),
            "name": p.get("Name") or p.get("original_name"),
            "size_bytes": f.get("size64"),
            "created": f.get("created"),
            **{k: p[k] for k in keep if k in p}}


# MCP SURFACE =====================================================================

def install(mcp: FastMCP) -> None:
    """Register the file-store tools."""

    @mcp.tool()
    def list_files(kind: Kind | None = None, name_contains: str | None = None) -> dict:
        """What is stored on SystemLink, filtered by `kind`.

        Kinds: `measurement` (a finite AI/AO capture from the measure_* tools),
        `vibration` (an accelerometer recording for bearing analysis),
        `test-report` and `vibration-report` (rendered HTML).

        Recordings carry `channels`, `sample_rate_hz`, `duration_s` and — for
        vibration — `rpm` and `bearing`, so a file can be chosen without downloading
        it. Pass the `file_id` to get_file to obtain a local path to analyse."""
        props: dict[str, str] = {}
        if kind:
            props["kind"] = kind
        rows = query_files(**props)
        if name_contains:
            needle = name_contains.lower()
            rows = [f for f in rows
                    if needle in (f["properties"].get("Name", "") or "").lower()]
        rows.sort(key=lambda f: f.get("created") or "", reverse=True)
        return {"files": [_summary(f) for f in rows], "count": len(rows)}

    @mcp.tool()
    def get_file(file_id: str) -> dict:
        """Download a stored file to the local cache and return its path.

        The File service has no partial read, so this transfers the whole file; it is
        cached by id, and a second call on the same id costs nothing. Feed the returned
        `path` to analyze_vibration or vibration_report."""
        path = fetch(file_id)
        info = file_info(file_id)
        return {"path": str(path), "size_bytes": path.stat().st_size,
                "url": f"{SERVER_URI}/nifile/v1/service-groups/Default"
                       f"/files/{file_id}/data",
                **_summary(info)}
