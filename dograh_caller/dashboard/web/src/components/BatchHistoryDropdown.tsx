import React, { useEffect, useRef, useState } from "react";
import { formatDateTime } from "../lib/format";
import { BatchInfo } from "../types";
import { CheckIcon, ChevronDownIcon, LayersIcon } from "./Icons";

interface Props {
  batches: BatchInfo[];
  selectedBatchId: string | null;
  onSelect: (batchId: string) => void;
}

export const BatchHistoryDropdown: React.FC<Props> = ({ batches, selectedBatchId, onSelect }) => {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onMouseDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const selectedBatch =
    selectedBatchId === "all"
      ? null
      : batches.find((b) => b.batch_id === selectedBatchId) ?? batches[0] ?? null;

  const buttonLabel =
    selectedBatchId === "all"
      ? "All Batches"
      : selectedBatch
        ? selectedBatch.label
        : "Batches";

  const totalAllLeads = batches.reduce((acc, b) => acc + b.total_leads, 0);

  return (
    <div className="batch-dropdown" ref={rootRef}>
      <button
        type="button"
        className={`batch-trigger ${open ? "is-open" : ""}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((cur) => !cur)}
      >
        <LayersIcon size={14} />
        <span className="batch-trigger-label">{buttonLabel}</span>
        {selectedBatch?.is_active && <span className="batch-trigger-live" aria-hidden="true" />}
        {batches.length > 0 && (
          <span className="batch-trigger-count">
            {selectedBatchId === "all"
              ? totalAllLeads.toLocaleString()
              : (selectedBatch?.total_leads ?? 0).toLocaleString()}
          </span>
        )}
        <ChevronDownIcon size={14} className="batch-trigger-chevron" />
      </button>

      {open && (
        <div className="batch-menu" role="menu" aria-label="Select batch">
          <div className="batch-menu-head">
            <span>Batches</span>
            <span className="batch-menu-sub">{batches.length} recorded</span>
          </div>

          {batches.length === 0 ? (
            <div className="batch-menu-empty">No previous batches yet.</div>
          ) : (
            <div className="batch-menu-list">
              {batches.map((batch, index) => {
                const isSelected = selectedBatchId === batch.batch_id || (!selectedBatchId && index === 0);
                const isLatest = index === 0 && !batch.is_active;
                return (
                  <button
                    key={batch.batch_id}
                    type="button"
                    role="menuitemradio"
                    aria-checked={isSelected}
                    className={`batch-item ${isSelected ? "is-selected" : ""}`}
                    onClick={() => {
                      setOpen(false);
                      onSelect(batch.batch_id);
                    }}
                  >
                    <div className="batch-item-main">
                      <div className="batch-item-title-row">
                        <span className="batch-item-title">{batch.label}</span>
                        {batch.is_active && <span className="batch-badge tone-live">Live</span>}
                        {isLatest && <span className="batch-badge tone-latest">Latest</span>}
                        {batch.is_legacy && <span className="batch-badge tone-muted">History</span>}
                      </div>
                      <div className="batch-item-meta">
                        {batch.started_at ? formatDateTime(batch.started_at) : "Starting…"} ·{" "}
                        {batch.total_leads.toLocaleString()} leads · {batch.qualified.toLocaleString()} qualified ·{" "}
                        {batch.called.toLocaleString()} called
                      </div>
                    </div>
                    {isSelected && <CheckIcon size={15} className="batch-item-check" />}
                  </button>
                );
              })}

              {batches.length > 1 && (
                <>
                  <div className="batch-menu-divider" role="separator" />
                  <button
                    type="button"
                    role="menuitemradio"
                    aria-checked={selectedBatchId === "all"}
                    className={`batch-item ${selectedBatchId === "all" ? "is-selected" : ""}`}
                    onClick={() => {
                      setOpen(false);
                      onSelect("all");
                    }}
                  >
                    <div className="batch-item-main">
                      <div className="batch-item-title-row">
                        <span className="batch-item-title">All Batches</span>
                        <span className="batch-badge tone-muted">Combined</span>
                      </div>
                      <div className="batch-item-meta">
                        {batches.length} batches · {totalAllLeads.toLocaleString()} leads total
                      </div>
                    </div>
                    {selectedBatchId === "all" && <CheckIcon size={15} className="batch-item-check" />}
                  </button>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
