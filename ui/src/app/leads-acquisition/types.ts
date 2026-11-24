export type ViewName =
  | "incoming"
  | "number_verifier"
  | "decision_maker"
  | "non_qualified"
  | "qualified"
  | "calling_agent"
  | "call_results"
  | "transferred";

export interface ServiceInfo {
  id: string;
  name: string;
}

export interface SummaryNodes {
  incoming: { total: number };
  number_verifier: { passed: number; rejected: number; errors: number; waiting: number };
  decision_maker: { reviewed: number; errors: number; waiting: number };
  non_qualified: { total: number };
  qualified: { total: number; by_service: Record<string, number> };
  calling_agent: {
    waiting: number;
    calling: number;
    not_sent: number;
    not_called: number;
    interrupted: number;
  };
  call_results: { total: number; called: number; failed: number; timeout: number };
  transferred: { total: number };
}

export type RunnerState = "idle" | "running" | "stopping" | "completed" | "stopped" | "error";

export interface RunnerStatus {
  state: RunnerState;
  active: boolean;
  batch_id?: string | null;
  batch_size: number;
  total_batches: number;
  current_batch: number;
  batches_completed: number;
  leads_fetched: number;
  leads_processed: number;
  current_batch_leads: number;
  started_at: string | null;
  finished_at: string | null;
  message: string;
  error: string | null;
  source_url?: string | null;
  /** Leads whose call reached nobody are called again in a later batch */
  retry_failed_calls?: boolean;
  /** Saved batches from earlier runs (processed first) and their unfinished leads */
  pending_batches?: number;
  pending_leads?: number;
}

export interface BatchInfo {
  batch_id: string;
  label: string;
  started_at: string | null;
  updated_at: string | null;
  total_leads: number;
  qualified: number;
  called: number;
  transferred: number;
  in_progress: number;
  is_legacy: boolean;
  is_active: boolean;
}

export interface SummaryResponse {
  tracker_found: boolean;
  stale: boolean;
  demo: boolean;
  data_as_of: string | null;
  refresh_seconds: number;
  threshold: number;
  services: ServiceInfo[];
  activity: {
    verifying: number;
    deciding: number;
    queued: number;
    calling: number;
    interrupted: number;
  };
  nodes: SummaryNodes;
  runner?: RunnerStatus;
  selected_batch_id?: string | null;
  batches?: BatchInfo[];
}

export interface OpenViewRequest {
  view: ViewName;
  filter?: string;
}

export interface OpenLeadRequest {
  id: string;
  list: string[];
  section?: "call";
}

export interface ColumnDef {
  key: string;
  label: string;
}

export interface LeadsResponse {
  view: ViewName;
  columns: ColumnDef[];
  rows: Record<string, unknown>[];
  total: number;
  page: number;
  page_size: number;
  filter_counts?: Record<string, number>;
}

export interface TransferredRow {
  lead_id: string;
  business: string;
  assigned_service: string;
  transferred_at: string;
  duration_label: string;
  call_disposition: string;
}

export interface JourneyStep {
  stage: string;
  at: string | null;
  state: "done" | "current" | "error" | "skipped" | "pending";
}

export interface ServiceScoreDetail {
  service_id: string;
  service_name: string;
  probability: number;
  meets_threshold: boolean;
  reason: string;
}

export interface AudioTrack {
  id: string;
  label: string;
  url: string;
}

export interface TranscriptTurn {
  speaker: "agent" | "customer" | "other";
  at: string | null;
  text: string;
}

export interface LeadDetailResponse {
  lead_id: string;
  business: string;
  stage: string;
  status: string;
  status_label: string;
  status_detail: string;
  journey: JourneyStep[];
  number_check: {
    result: string;
    reason: string;
    raw_reason: string;
    phone: string;
    phone_raw: string;
    location: string;
    time_zone: string;
  };
  decision: {
    result: string;
    assigned_service: string | null;
    assigned_service_id: string | null;
    best_service: string | null;
    best_probability: number | null;
    threshold: number;
    model: string;
    summary: string;
    verdict: string;
    scores: ServiceScoreDetail[];
    call_reason: string;
  };
  call: {
    status: string;
    status_label: string;
    disposition: string;
    duration_s: number | null;
    duration_label: string;
    attempts: number | null;
    tags: string[];
    transferred: boolean;
    agent_name: string;
    campaign_name: string;
    dograh_run_id: string | number | null;
    transcript_available: boolean;
    transcript_url: string | null;
    recording_url: string | null;
    audio_tracks: AudioTrack[];
    collected: Record<string, unknown>;
    fields: Record<string, unknown>;
  } | null;
  lead_fields: Record<string, unknown>;
}
