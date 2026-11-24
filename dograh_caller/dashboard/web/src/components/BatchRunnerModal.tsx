import React, { useMemo, useRef, useState } from "react";
import { RunnerStatus } from "../types";
import { AlertIcon, CloseIcon, LayersIcon, PlayIcon } from "./Icons";
import { useFocusTrap } from "./useFocusTrap";

const BATCH_SIZE_PRESETS = [50, 100, 300, 500, 1000] as const;
const NUM_BATCHES_PRESETS = [1, 2, 5, 10] as const;

interface Props {
  defaultBatchSize?: number;
  defaultNumBatches?: number;
  sourceUrl?: string | null;
  demo?: boolean;
  /** Default for the "retry failed calls" option (server's RETRY_FAILED_CALLS) */
  defaultRetryFailedCalls?: boolean;
  /** Unfinished leads saved from earlier runs; they are processed first */
  pendingLeads?: number;
  onClose: () => void;
  onStarted: (status: RunnerStatus) => void;
}

function parseIntInRange(raw: string, min: number, max: number): number | null {
  const trimmed = raw.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number.parseInt(trimmed, 10);
  if (!Number.isFinite(parsed) || parsed < min || parsed > max) return null;
  return parsed;
}

function formatApiError(payload: unknown, fallbackStatus: number): string {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
    if (Array.isArray(record.detail) && record.detail.length > 0) {
      const first = record.detail[0];
      if (first && typeof first === "object" && "msg" in first) {
        return String((first as { msg: unknown }).msg);
      }
    }
    if (typeof record.message === "string" && record.message.trim()) return record.message;
    if (typeof record.error === "string" && record.error.trim()) return record.error;
  }
  return `Could not start batch run (HTTP ${fallbackStatus}).`;
}

export const BatchRunnerModal: React.FC<Props> = ({
  defaultBatchSize = 300,
  defaultNumBatches = 1,
  sourceUrl = null,
  demo = false,
  defaultRetryFailedCalls = false,
  pendingLeads = 0,
  onClose,
  onStarted,
}) => {
  const panelRef = useRef<HTMLDivElement>(null);
  const [batchSizeInput, setBatchSizeInput] = useState(String(defaultBatchSize));
  const [numBatchesInput, setNumBatchesInput] = useState(String(defaultNumBatches));
  const [retryFailedCalls, setRetryFailedCalls] = useState(defaultRetryFailedCalls);
  const [submitting, setSubmitting] = useState(false);
  const [apiError, setApiError] = useState<string | null>(null);

  useFocusTrap(panelRef, true, () => {
    if (!submitting) onClose();
  });

  const parsedBatchSize = useMemo(() => parseIntInRange(batchSizeInput, 1, 1000), [batchSizeInput]);
  const parsedNumBatches = useMemo(() => parseIntInRange(numBatchesInput, 1, 100), [numBatchesInput]);

  const batchSizeError =
    parsedBatchSize === null ? "Enter a whole number between 1 and 1,000." : null;
  const numBatchesError =
    parsedNumBatches === null ? "Enter a whole number between 1 and 100." : null;
  const isValid = parsedBatchSize !== null && parsedNumBatches !== null;

  const totalLeads = isValid ? parsedBatchSize * parsedNumBatches : null;

  const stepBatches = (delta: number) => {
    const current = parsedNumBatches ?? 1;
    const next = Math.min(100, Math.max(1, current + delta));
    setNumBatchesInput(String(next));
    setApiError(null);
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!isValid || submitting) return;

    setSubmitting(true);
    setApiError(null);
    try {
      const resp = await fetch("/api/runner/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          batch_size: parsedBatchSize,
          num_batches: parsedNumBatches,
          retry_failed_calls: retryFailedCalls,
        }),
      });
      let payload: unknown = null;
      try {
        payload = await resp.json();
      } catch {
        payload = null;
      }
      if (!resp.ok) {
        setApiError(formatApiError(payload, resp.status));
        return;
      }
      onStarted(payload as RunnerStatus);
    } catch {
      setApiError("Could not reach the dashboard server. Check that it is running and try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
      }}
    >
      <div
        ref={panelRef}
        className="runner-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="runner-modal-title"
        tabIndex={-1}
      >
        <header className="runner-modal-head">
          <span className="runner-modal-icon">
            <LayersIcon size={20} />
          </span>
          <div className="runner-modal-titles">
            <h2 id="runner-modal-title">Start Lead Batch Run</h2>
            <p>
              {demo
                ? "Demo mode: simulates pulling and processing batches offline without contacting the reverse proxy or Dograh."
                : sourceUrl
                  ? `Pulls batches directly from ${sourceUrl}, processes each lead, and automatically stops when finished.`
                  : "Pulls batches sequentially, processes each lead, and automatically stops when finished."}
            </p>
          </div>
          <button
            type="button"
            className="icon-btn"
            disabled={submitting}
            onClick={onClose}
            aria-label="Close Start Lead Batch Run"
          >
            <CloseIcon />
          </button>
        </header>

        <form className="runner-form" onSubmit={handleSubmit} noValidate>
          {apiError && (
            <div className="runner-error" role="alert">
              <AlertIcon size={16} />
              <span>{apiError}</span>
            </div>
          )}

          <div className="runner-field">
            <div className="runner-field-head">
              <label htmlFor="runner-batch-size">Leads per batch</label>
              <span className="runner-range-hint">1 – 1,000</span>
            </div>
            <input
              id="runner-batch-size"
              type="number"
              className={`runner-input ${batchSizeError ? "is-invalid" : ""}`}
              min={1}
              max={1000}
              step={1}
              inputMode="numeric"
              value={batchSizeInput}
              disabled={submitting}
              onChange={(event) => {
                setBatchSizeInput(event.target.value);
                setApiError(null);
              }}
            />
            {batchSizeError && <p className="runner-field-error">{batchSizeError}</p>}
            <div className="preset-row" role="group" aria-label="Quick presets for leads per batch">
              {BATCH_SIZE_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  className={`preset-chip ${parsedBatchSize === preset ? "is-active" : ""}`}
                  disabled={submitting}
                  onClick={() => {
                    setBatchSizeInput(String(preset));
                    setApiError(null);
                  }}
                >
                  {preset.toLocaleString()}
                </button>
              ))}
            </div>
          </div>

          <div className="runner-field">
            <div className="runner-field-head">
              <label htmlFor="runner-num-batches">Number of batches</label>
              <span className="runner-range-hint">1 – 100</span>
            </div>
            <div className="stepper-row">
              <button
                type="button"
                className="stepper-btn"
                disabled={submitting || (parsedNumBatches !== null && parsedNumBatches <= 1)}
                onClick={() => stepBatches(-1)}
                aria-label="Decrease number of batches"
              >
                −
              </button>
              <input
                id="runner-num-batches"
                type="number"
                className={`runner-input ${numBatchesError ? "is-invalid" : ""}`}
                min={1}
                max={100}
                step={1}
                inputMode="numeric"
                value={numBatchesInput}
                disabled={submitting}
                onChange={(event) => {
                  setNumBatchesInput(event.target.value);
                  setApiError(null);
                }}
              />
              <button
                type="button"
                className="stepper-btn"
                disabled={submitting || (parsedNumBatches !== null && parsedNumBatches >= 100)}
                onClick={() => stepBatches(1)}
                aria-label="Increase number of batches"
              >
                +
              </button>
            </div>
            {numBatchesError && <p className="runner-field-error">{numBatchesError}</p>}
            <div className="preset-row" role="group" aria-label="Quick presets for number of batches">
              {NUM_BATCHES_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  className={`preset-chip ${parsedNumBatches === preset ? "is-active" : ""}`}
                  disabled={submitting}
                  onClick={() => {
                    setNumBatchesInput(String(preset));
                    setApiError(null);
                  }}
                >
                  {preset} {preset === 1 ? "batch" : "batches"}
                </button>
              ))}
            </div>
          </div>

          <label className="runner-check">
            <input
              type="checkbox"
              checked={retryFailedCalls}
              disabled={submitting}
              onChange={(event) => setRetryFailedCalls(event.target.checked)}
            />
            <span>
              <strong>Retry failed calls</strong>
              <span className="runner-check-hint">
                Leads whose call reached nobody (busy, no answer, failed, voicemail) are called again in a
                later batch. Leads that could not be dialed at all are always retried.
              </span>
            </span>
          </label>

          {!demo && pendingLeads > 0 && (
            <div className="runner-pending" role="note">
              <AlertIcon size={16} />
              <span>
                {pendingLeads.toLocaleString()} saved {pendingLeads === 1 ? "lead" : "leads"} from an earlier run
                will be processed first, {parsedBatchSize !== null ? parsedBatchSize.toLocaleString() : "your chosen number of"} per
                batch, before any new leads are pulled.
              </span>
            </div>
          )}

          <div className="runner-summary-card" aria-live="polite">
            <div className="runner-summary-main">
              {totalLeads !== null && parsedNumBatches !== null && parsedBatchSize !== null ? (
                <>
                  <strong>
                    {parsedNumBatches} {parsedNumBatches === 1 ? "batch" : "batches"} ×{" "}
                    {parsedBatchSize.toLocaleString()} leads = up to {totalLeads.toLocaleString()} leads total
                  </strong>
                </>
              ) : (
                <strong className="muted-text">Enter valid batch settings to calculate total leads</strong>
              )}
            </div>
            <p className="runner-summary-note">
              The pipeline stops automatically once finished and waits for your next Start click.
            </p>
          </div>

          <footer className="runner-modal-foot">
            <button type="button" className="btn-secondary" disabled={submitting} onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn-primary-glow" disabled={!isValid || submitting}>
              <PlayIcon size={13} />
              {submitting
                ? "Starting…"
                : totalLeads !== null
                  ? `Start Run (up to ${totalLeads.toLocaleString()} leads)`
                  : "Start Run"}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
};
