"""Executable HTTP contract for the ESP-Iris Gateway adapter."""

from __future__ import annotations

from typing import Any

from .device_state import DEVICE_STATES


def build_openapi(auth_required: bool) -> dict[str, Any]:
    control_paths = {
        "/v1/devices/{device_id}/rpc/raw": "Raw RPC",
        "/v1/devices/{device_id}/restart": "Restart device",
        "/v1/devices/{device_id}/factory-recovery": "Enter factory recovery",
        "/v1/devices/{device_id}/ota": "Validated OTA",
        "/v1/devices/{device_id}/system-update": "Authenticated system update",
        "/v1/devices/{device_id}/input": "Pointer or touch gesture",
        "/v1/devices/{device_id}/console": "Submit one console command line",
        "/v1/devices/{device_id}/screenshot": "Capture screenshot",
        "/v1/devices/{device_id}/mirror/start": "Start media mirror",
        "/v1/devices/{device_id}/mirror/stop": "Stop media mirror",
    }
    paths: dict[str, Any] = {
        "/v1/project": {"get": {"summary": "Local project session, discovery and shared ownership; project Gateways only"}},
        "/v1/project/clients": {"post": {"summary": "Register an idempotent project client lease; requires current session_id"}},
        "/v1/project/clients/{client_id}/renew": {"post": {"summary": "Renew a client lease using its private lease_token and session_id"}},
        "/v1/project/clients/{client_id}/release": {"post": {"summary": "Release only this client's lease; does not stop the Gateway"}},
        "/v1/project/acquire": {"post": {"summary": "Explicitly acquire a device or discovered endpoint"}},
        "/v1/project/release": {"post": {"summary": "Release an idle owned device"}},
        "/v1/project/takeovers": {"post": {
            "summary": "Request a device from its current owner into this receiving session",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "additionalProperties": False,
                "properties": {"device_id": {"type": ["string", "null"]}, "endpoint": {"type": ["string", "null"]},
                               "takeover_id": {"type": "string", "format": "uuid"},
                               "force": {"type": "boolean", "default": False},
                               "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600, "default": 120}},
                "oneOf": [
                    {"required": ["device_id"], "properties": {"device_id": {"type": "string"}, "endpoint": {"type": "null"}}},
                    {"required": ["endpoint"], "properties": {"endpoint": {"type": "string"}, "device_id": {"type": "null"}}},
                ],
            }}}},
        }},
        "/v1/project/takeovers/{takeover_id}": {"get": {"summary": "Read durable takeover state"}},
        "/v1/project/takeovers/{takeover_id}/resume": {"post": {"summary": "Continue receiver identity validation"}},
        "/v1/project/takeovers/{takeover_id}/abort": {"post": {"summary": "Roll back an incomplete takeover from the original owner"}},
        "/v1/project/takeovers/{takeover_id}/reconcile": {"post": {"summary": "Resolve reserved ownership after both original sessions died"}},
        "/v1/project/reconcile": {"post": {"summary": "Explicitly reconcile a dead owner's ordinary device claim"}},
        "/v1/health": {"get": {"summary": "Gateway health"}},
        "/v1/auth/login": {"post": {"summary": "Developer password login"}},
        "/v1/devices": {"get": {"summary": "Connected and cached devices"}},
        "/v1/host-operations": {
            "post": {"summary": "Submit a local process-owned ROM operation",
                     "description": "Requires a private local request file ID; executable commands are never accepted over HTTP."}
        },
        "/v1/devices/{device_id}": {
            "get": {"summary": "Current or cached status"},
            "delete": {
                "summary": "Remove an offline device from inventory",
                "description": "Preserves operations, events, logs, and audit history.",
            },
        },
        "/v1/devices/{device_id}/memory": {
            "get": {"summary": "Live internal RAM, SPIRAM and task stack watermarks"}
        },
        "/v1/devices/{device_id}/system-inventory": {
            "get": {"summary": "Live bootloader and partition-table inventory"}
        },
        "/v1/devices/{device_id}/crashes": {
            "get": {"summary": "Live crash metadata and archived diagnosis"}
        },
        "/v1/devices/{device_id}/crashes/core-dump": {
            "get": {"summary": "Preserve and download the retained Core Dump"}
        },
        "/v1/devices/{device_id}/crashes/archive": {
            "post": {"summary": "Archive and decode current crash evidence"}
        },
        "/v1/mode": {
            "get": {"summary": "Get global mode"},
            "put": {"summary": "Switch develop or observe mode"},
        },
        "/v1/events": {"get": {"summary": "Cursor-based event history"}},
        "/v1/events/ws": {"get": {"summary": "Resumable event WebSocket"}},
        "/v1/operations": {"get": {"summary": "Device operation records"}},
        "/v1/operations/{operation_id}/reconcile": {
            "post": {"summary": "Append a read-only observation of an uncertain operation; never replay writes"}
        },
        "/v1/operations/{operation_id}/reconciliations": {
            "get": {"summary": "Append-only reconciliation evidence; original status is unchanged"}
        },
        "/v1/devices/{device_id}/files/volumes": {
            "get": {"summary": "Registered file volumes and capabilities"}
        },
        "/v1/devices/{device_id}/files/stat": {
            "get": {"summary": "File or directory metadata"}
        },
        "/v1/devices/{device_id}/files": {
            "get": {"summary": "Paginated directory entries"}
        },
        "/v1/devices/{device_id}/file": {
            "get": {"summary": "Stream a file with HTTP Range support"},
            "put": {"summary": "Stream a create or atomic file replacement"},
            "delete": {"summary": "Delete a file or empty directory"},
        },
        "/v1/devices/{device_id}/directories": {
            "post": {"summary": "Create one directory"}
        },
        "/v1/devices/{device_id}/file-rename": {
            "post": {"summary": "Rename within one logical volume"}
        },
        "/v1/firmware-artifacts": {
            "get": {"summary": "Archived firmware bundles"},
            "post": {"summary": "Archive BIN, ELF and map as one validated bundle"},
        },
        "/v1/system-audit": {"get": {"summary": "Gateway system audit"}},
        "/v1/metrics": {"get": {"summary": "Gateway process metrics"}},

    }
    for path, summary in control_paths.items():
        paths[path] = {"post": {"summary": summary}}
    compatibility_schema = {
        "type": "object", "additionalProperties": False,
        "description": "Explicit device expectations, checked before writes and bound to the operation ID.",
        "properties": {
            **{field: {"type": "string", "minLength": 1, "maxLength": 64}
               for field in ("chip_target", "product_contract", "board_id", "layout_id")},
            "recovery_abi": {"type": "integer", "minimum": 1, "maximum": 65535},
        },
    }
    paths["/v1/devices/{device_id}/ota"]["post"]["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["artifact_id"],
                    "properties": {
                        "artifact_id": {"type": "string"},
                        "compatibility": compatibility_schema,
                        "preconditions": {
                            "type": "object", "additionalProperties": False,
                            "description": "Checked on live Recovery before OTA BEGIN; bound to the operation ID.",
                            "properties": {
                                "recovery_version": {"type": "string", "minLength": 1, "maxLength": 64},
                                "partition_table_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
                            },
                        },
                        "execution_mode": {
                            "type": "string",
                            "enum": ["recovery", "application"],
                            "default": "recovery",
                        },
                        "validation_mode": {
                            "type": "string",
                            "enum": ["elf_sha256", "version"],
                            "default": "elf_sha256",
                        },
                    },
                }
            }
        },
    }
    paths["/v1/devices/{device_id}/factory-recovery"]["post"]["requestBody"] = {
        "required": False,
        "content": {"application/json": {"schema": {
            "type": "object",
            "properties": {"wait": {"type": "boolean", "default": False},
                           "timeout": {"type": "number", "minimum": 0.1, "maximum": 30, "default": 30}},
        }}},
    }
    paths["/v1/devices/{device_id}/system-update"]["post"]["requestBody"] = {
        "required": True,
        "content": {
            "application/vnd.esp-iris.system-update+zip": {
                "schema": {"type": "string", "format": "binary"}
            }
        },
    }
    paths["/v1/devices/{device_id}/system-update"]["post"]["parameters"] = [{
        "in": "header", "name": "X-Iris-Compatibility", "required": False,
        "content": {"application/json": {"schema": compatibility_schema}},
    }]
    paths["/v1/host-operations"]["post"]["requestBody"] = {
        "required": True, "content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": False,
            "required": ["request_id"], "properties": {"request_id": {"type": "string", "format": "uuid"}},
        }}},
    }
    paths["/v1/devices"]["get"]["responses"] = {
        "200": {"description": "Device inventory", "content": {"application/json": {"schema": {
            "type": "object", "properties": {"devices": {"type": "array", "items": {"$ref": "#/components/schemas/Device"}}},
        }}}},
    }
    paths["/v1/devices/{device_id}/jobs/{job_id}"] = {
        "get": {"summary": "Query job"},
        "delete": {"summary": "Cancel job"},
    }
    document = {
        "openapi": "3.1.0",
        "info": {
            "title": "ESP-Iris Developer Gateway",
            "version": "1.0.0",
            "description": "Gateway-only device control and observation API.",
        },
        "servers": [{"url": "/"}],
        "components": {
            "schemas": {"Device": {
                "type": "object", "required": ["device_id", "state"],
                "properties": {
                    "device_id": {"type": "string"},
                    "state": {"type": "string", "enum": list(DEVICE_STATES)},
                    "firmware_mode": {"type": "string", "enum": ["normal", "recovery", "rom", "unknown"]},
                    "owner_session_id": {"type": ["string", "null"]},
                    "busy_reasons": {"type": "array", "items": {"type": "object"}},
                },
            }},
            "securitySchemes": {
                "cookieAuth": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "esp_iris_session",
                },
                "agentToken": {"type": "http", "scheme": "bearer"},
            }
        },
        "paths": paths,
    }
    if auth_required:
        document["security"] = [{"cookieAuth": []}, {"agentToken": []}]
    return document


__all__ = ["build_openapi"]
