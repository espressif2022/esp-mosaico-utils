export type DeviceState = "offline" | "connecting" | "idle" | "busy" | "needs_recovery";

export type Device = {
  state: DeviceState;
  owner_session_id?: string | null;
  busy_reasons?: { kind: string; operation_id?: string; job_id?: number; channel?: number }[];
  device_id: string;
  hardware_mac?: string;
  alias?: string;
  suggested_alias?: string;
  connected: boolean;
  cached?: boolean;
  firmware_mode?: "normal" | "recovery" | "rom" | "unknown";
  app_version?: string;
  project_name?: string;
  endpoint?: string;
  transport_name?: string;
  boot_id?: number;
  boot_id_text?: string;
  capability_names?: string[];
  demo?: boolean;
};

export type DeviceStatus = Device & {
  stale: boolean;
  mode: "develop" | "observe";
  uptime_us?: number;
  free_internal?: number;
  min_free_internal?: number;
  heap_used?: number;
  heap_total?: number;
  log_dropped_bytes?: number;
  lifecycle_state?: string;
  clock_uncertainty_us?: number;
  queue?: { running: string[]; queued: string[] };
};

export type GatewayHealth = {
  project_session?: { session_id: string; project_path: string } | null;
  system_update_trust_configured: boolean;
};

export type Operation = {
  operation_id: string;
  device_id: string;
  actor_type: "agent" | "developer" | string;
  actor_name: string;
  action: string;
  status: string;
  params: Record<string, unknown>;
  result?: unknown;
  error?: string;
  created_ns: number;
  started_ns?: number;
  finished_ns?: number;
  queue_position?: number;
  progress?: {
    stage: string;
    progress_permille: number;
    device_progress_permille?: number;
    job_id?: number;
    bytes_received?: number;
    bytes_total?: number;
    partition?: string;
    updated_ns?: number;
  } | null;
};

export type GatewayEvent = {
  event_id?: number;
  category?: string;
  kind?: string;
  device_id?: string;
  text?: string;
  host_receive_ns?: number;
  host_receive_wall_ns?: number;
  parsed?: { level?: string; stamp?: string; tag?: string; message?: string };
  operation?: Operation;
  connection_state?: string;
  event_name?: string;
};

export type Audit = {
  audit_id: number;
  actor_type: string;
  actor_name: string;
  action: string;
  details: Record<string, unknown>;
  created_ns: number;
};

export type FileVolume = {
  id: string;
  capabilities: number;
  capability_names: string[];
};

export type FileEntry = {
  name: string;
  kind: "file" | "directory";
  size: number;
  mtime_s: number;
  etag: string;
};

export type FileList = {
  volume: string;
  path: string;
  entries: FileEntry[];
  cursor: number;
  next_cursor: number | null;
  snapshot: false;
};
