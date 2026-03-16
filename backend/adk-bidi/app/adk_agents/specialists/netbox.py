"""NetboxAgent — ADK specialist for NetBox queries via nb-cli."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from app.job_workspace import JobWorkspaceStore
from app.tools.job_workspace_tools import build_job_workspace_tools

log = logging.getLogger("athena.adk_agents.specialists.netbox")

_MODEL = os.getenv("ATHENA_SPECIALIST_MODEL", "gemini-3.1-flash-lite-preview")
_CLI = os.getenv("ATHENA_NETBOX_CLI", "nb-cli")
_DEFAULT_TIMEOUT = int(os.getenv("ATHENA_NETBOX_CLI_TIMEOUT", "20"))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run_nb(args: list[str]) -> dict:
    """Run an nb-cli command and return its JSON output as a dict."""
    cmd = [_CLI, "--format", "json"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_DEFAULT_TIMEOUT,
        )
        raw = result.stdout.strip()
        if not raw:
            stderr = result.stderr.strip()
            return {
                "ok": False,
                "error_code": "api_error",
                "detail": stderr or "Empty response from nb-cli",
                "isRetryable": False,
            }
        try:
            data = json.loads(raw)
            # nb-cli returns structured JSON; attach ok=true for agent clarity
            if isinstance(data, dict) and "error" in data:
                return {
                    "ok": False,
                    "error_code": "api_error",
                    "detail": str(data["error"]),
                    "isRetryable": False,
                }
            return {"ok": True, **data} if isinstance(data, dict) else {"ok": True, "results": data}
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error_code": "api_error",
                "detail": f"Non-JSON output: {raw[:300]}",
                "isRetryable": False,
            }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error_code": "api_error",
            "detail": f"nb-cli timed out after {_DEFAULT_TIMEOUT}s",
            "isRetryable": True,
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "error_code": "auth_error",
            "detail": f"nb-cli not found at '{_CLI}'. Install with: pip install nb-cli-tool",
            "isRetryable": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error_code": "api_error",
            "detail": str(exc),
            "isRetryable": False,
        }


# ── FunctionTools ──────────────────────────────────────────────────────────────

def search_netbox_devices(
    site: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    name_contains: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Search for network devices in NetBox by site, role, status, or name.

    Use this tool when the user asks about devices at a location, with a specific
    network role (core-router, access-switch, firewall, etc.), or by device name.

    Required: at least one of site, role, status, or name_contains.
    Optional:
      site (str)         — site slug (e.g. 'nyc01', 'lon-dc1')
      role (str)         — device role slug (e.g. 'core-router', 'access-switch')
      status (str)       — 'active', 'planned', 'staged', 'failed', 'decommissioning', 'offline'
      name_contains (str)— substring filter on device name (e.g. 'core')
      limit (int)        — max results, default 50

    Returns: {'ok': bool, 'count': int, 'results': list[device]} on success.
    Each device has: id, name, device_role, site, status, primary_ip, platform, rack.

    Errors: auth_error (NBCLI_URL/NBCLI_TOKEN missing), api_error, not_found.

    Do NOT use this to list interfaces — use list_netbox_interfaces(device_id=...) instead.
    Do NOT use this to look up IP prefixes — use search_netbox_prefixes() instead.
    """
    args = ["device", "list", "--limit", str(limit)]
    if site:
        args += ["--filter", f"site={site}"]
    if role:
        args += ["--filter", f"role={role}"]
    if status:
        args += ["--filter", f"status={status}"]
    if name_contains:
        args += ["--filter", f"name__icontains={name_contains}"]
    return _run_nb(args)


def get_netbox_device(device_id: str) -> dict:
    """Fetch full details for a single NetBox device by its numeric ID.

    Use this tool when you have a device ID from a prior search and need complete
    details: platform, tenant, rack position, serial, comments, tags.

    Required: device_id (str) — numeric NetBox device ID (e.g. '42').

    Returns: {'ok': bool, 'id': ..., 'name': ..., 'device_role': ..., ...} on success.

    Errors: not_found, auth_error, api_error.

    Use search_netbox_devices() first if you only have a device name or site.
    """
    return _run_nb(["device", "show", str(device_id)])


def list_netbox_interfaces(
    device_id: Optional[str] = None,
    device_name: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """List network interfaces for a specific NetBox device.

    Use this tool when the user asks about ports, interfaces, or connectivity on a device.

    Provide one of:
      device_id (str)   — numeric device ID (preferred; use after get_netbox_device or search)
      device_name (str) — exact device name (use if ID is not available)
      limit (int)       — max interfaces to return, default 100

    Returns: {'ok': bool, 'count': int, 'results': list[interface]} on success.
    Each interface has: id, name, device, type, enabled, mac_address, description, ip_addresses.

    Errors: auth_error, not_found (bad device ID), api_error.

    Do NOT use this to list IP addresses directly — use list_netbox_ip_addresses() for that.
    """
    args = ["interface", "list", "--limit", str(limit)]
    if device_id:
        args += ["--filter", f"device_id={device_id}"]
    elif device_name:
        args += ["--filter", f"device={device_name}"]
    else:
        return {
            "ok": False,
            "error_code": "validation_error",
            "detail": "Provide device_id or device_name.",
            "isRetryable": False,
        }
    return _run_nb(args)


def list_netbox_ip_addresses(
    device_id: Optional[str] = None,
    interface_id: Optional[str] = None,
    address_filter: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """List IP addresses assigned to a device or interface in NetBox.

    Use this tool when the user asks about IP addresses on a device or interface.

    Provide at least one of:
      device_id (str)      — numeric device ID
      interface_id (str)   — numeric interface ID
      address_filter (str) — IP prefix to filter by (e.g. '10.0.0.')
      limit (int)          — max results, default 100

    Returns: {'ok': bool, 'count': int, 'results': list[ip_address]} on success.
    Each result has: id, address, vrf, interface, status, dns_name.

    Errors: auth_error, not_found, api_error.
    """
    args = ["ip-address", "list", "--limit", str(limit)]
    if device_id:
        args += ["--filter", f"device_id={device_id}"]
    if interface_id:
        args += ["--filter", f"interface_id={interface_id}"]
    if address_filter:
        args += ["--filter", f"address__iscontained={address_filter}"]
    return _run_nb(args)


def search_netbox_prefixes(
    site: Optional[str] = None,
    vrf: Optional[str] = None,
    prefix: Optional[str] = None,
    family: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Search for IP prefixes (subnets) in NetBox.

    Use this tool when the user asks about subnets, IP ranges, or prefix allocations.

    Optional:
      site (str)    — site slug to filter by
      vrf (str)     — VRF name to filter by (e.g. 'management', 'global')
      prefix (str)  — exact prefix (e.g. '10.0.0.0/8') or partial match
      family (int)  — IP family: 4 (IPv4) or 6 (IPv6)
      status (str)  — 'active', 'container', 'reserved', 'deprecated'
      limit (int)   — max results, default 50

    Returns: {'ok': bool, 'count': int, 'results': list[prefix]} on success.
    Each prefix has: id, prefix, site, vrf, status, description.

    Errors: auth_error, api_error.

    Do NOT use this for IP address lookups — use list_netbox_ip_addresses() instead.
    """
    args = ["prefix", "list", "--limit", str(limit)]
    if site:
        args += ["--filter", f"site={site}"]
    if vrf:
        args += ["--filter", f"vrf={vrf}"]
    if prefix:
        args += ["--filter", f"prefix={prefix}"]
    if family is not None:
        args += ["--filter", f"family={family}"]
    if status:
        args += ["--filter", f"status={status}"]
    return _run_nb(args)


def netbox_generic_query(resource: str, filters: list[str], limit: int = 50) -> dict:
    """Run a generic NetBox query against any resource type.

    Use this tool for resources not covered by the typed tools above: vlans, racks,
    cables, power feeds, sites, tenants, virtual machines, clusters, etc.

    Required:
      resource (str)     — NetBox resource path (e.g. 'dcim/racks', 'virtualization/virtual-machines',
                           'ipam/vlans', 'tenancy/tenants')
      filters (list[str])— list of 'key=value' filter strings (e.g. ['site=nyc01', 'status=active'])
      limit (int)        — max results, default 50

    Returns: {'ok': bool, 'count': int, 'results': list[object]} on success.

    Errors: auth_error, not_found (invalid resource path), api_error.

    Use the typed tools (search_netbox_devices, list_netbox_interfaces, etc.) when available —
    they are more reliable than this generic query.
    """
    args = ["query", resource, "--limit", str(limit)]
    for f in filters or []:
        args += ["--filter", f]
    return _run_nb(args)


def preview_netbox_interface_update(
    interface_id: str,
    status: Optional[str] = None,
    description: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Preview an interface update in NetBox (dry-run — no changes applied).

    Use this tool to show the user what would change BEFORE making any modifications.
    Always call this before proposing a netbox_interface_update action.

    Required: interface_id (str) — numeric interface ID.
    Optional (provide at least one):
      status (str)      — new status value
      description (str) — new description text
      enabled (bool)    — True to enable, False to disable

    Returns: {'ok': bool, 'dry_run': true, 'would_change': {...}} showing the planned patch.

    Errors: auth_error, not_found, validation_error, api_error.

    This is read-only. To actually apply the change, include it in 'action_proposals' output.
    """
    data: dict = {}
    if status is not None:
        data["status"] = status
    if description is not None:
        data["description"] = description
    if enabled is not None:
        data["enabled"] = enabled
    if not data:
        return {
            "ok": False,
            "error_code": "validation_error",
            "detail": "Provide at least one field to update.",
            "isRetryable": False,
        }
    args = [
        "interface", "update", str(interface_id),
        "--data", json.dumps(data),
        "--dry-run",
        "--yes",
    ]
    result = _run_nb(args)
    if result.get("ok"):
        result["dry_run"] = True
        result["would_change"] = data
    return result


# ── Instruction ────────────────────────────────────────────────────────────────

_NETBOX_INSTRUCTION = """\
You are a NetBox specialist. You can query devices, interfaces, IP addresses, prefixes,
VLANs, and other network infrastructure objects using the nb-cli tool.

## Tools available

- `search_netbox_devices`        — search devices by site, role, status, or name
- `get_netbox_device`            — fetch full details for one device by ID
- `list_netbox_interfaces`       — list interfaces on a device (by device_id or device_name)
- `list_netbox_ip_addresses`     — list IP addresses for a device or interface
- `search_netbox_prefixes`       — search IP prefixes/subnets by site, VRF, or prefix
- `netbox_generic_query`         — generic query for VLANs, racks, VMs, tenants, cables, etc.
- `preview_netbox_interface_update` — dry-run preview of an interface change (no writes)
- `get_job_workspace_state`      — inspect scratchpad and prior related work
- `save_job_workspace_note`      — save key IDs or notes for use in follow-up steps
- `save_job_workspace_json`      — save structured results (device lists, interface tables)

## Rules for queries

- If the user gives a device name, use `search_netbox_devices(name_contains=...)` or
  `list_netbox_interfaces(device_name=...)` — do not guess device IDs.
- If you need interfaces AND IP addresses, call `list_netbox_interfaces` first to get
  interface IDs, then `list_netbox_ip_addresses(interface_id=...)` for each relevant one.
- For anything not covered by typed tools (VLANs, racks, VMs, cables), use
  `netbox_generic_query` with the correct NetBox resource path.
- Check `get_job_workspace_state` first if this looks like a continuation or correction —
  reuse stored device IDs instead of re-searching.
- Save device IDs and interface IDs when a later step might need them.

## Rules for write requests

- Never apply changes without first calling `preview_netbox_interface_update` (dry-run).
- Put the proposed change in `action_proposals` in your output so the user can confirm.
- Do not call any mutating operation directly — always dry-run first, propose, wait.
- If the user has already confirmed (continuation turn referencing prior scratchpad),
  you may proceed — check `get_job_workspace_state` for confirmation context.

## Output format for read requests

{
  "summary": "<1-2 sentence voice-friendly answer>",
  "artifacts": [
    {
      "type": "netbox_device_list",
      "content": "<summary of devices found>",
      "devices": [{"id": "...", "name": "...", "role": "...", "status": "...", "primary_ip": "..."}]
    }
  ],
  "follow_up_questions": ["<optional follow-up>"],
  "resource_handles": [
    {
      "source": "netbox",
      "kind": "device",
      "id": "<device_id>",
      "title": "<device name>",
      "url": "",
      "metadata": {"site": "", "role": "", "status": ""}
    }
  ]
}

## Output format for proposed write requests

{
  "summary": "<what was found and what is being proposed>",
  "artifacts": [
    {
      "type": "netbox_preview",
      "content": "<description of the proposed change>",
      "dry_run_result": {}
    }
  ],
  "follow_up_questions": [],
  "resource_handles": [],
  "action_proposals": [
    {
      "description": "<one-sentence description of the action for user confirmation>",
      "interface_id": "<id>",
      "patch": {"status": "...", "description": "..."}
    }
  ]
}

If an operation fails, include "error" in the output and explain clearly in summary.
Golden rule: never invent device names, IDs, or IP addresses. Report only what nb-cli returns.
"""


# ── Factory ────────────────────────────────────────────────────────────────────

def build_netbox_agent(
    workspace_store: JobWorkspaceStore | None = None,
    *,
    session_id: str = "",
    job_id: str = "",
) -> LlmAgent:
    """Build the NetBox specialist LlmAgent."""
    tools = [
        FunctionTool(search_netbox_devices),
        FunctionTool(get_netbox_device),
        FunctionTool(list_netbox_interfaces),
        FunctionTool(list_netbox_ip_addresses),
        FunctionTool(search_netbox_prefixes),
        FunctionTool(netbox_generic_query),
        FunctionTool(preview_netbox_interface_update),
    ]
    tools.extend(
        build_job_workspace_tools(
            workspace_store,
            session_id=session_id,
            job_id=job_id,
        )
    )
    return LlmAgent(
        name="netbox_specialist",
        model=_MODEL,
        instruction=_NETBOX_INSTRUCTION,
        tools=tools,
        output_key="netbox_result",
    )
