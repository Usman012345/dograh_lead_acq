import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BatchRunnerModal } from "./components/BatchRunnerModal";
import { CloseIcon, PhoneForwardIcon, PlayIcon, StopIcon } from "./components/Icons";
import { LeadDetailModal } from "./components/LeadDetailModal";
import { PipelineFlow } from "./components/PipelineFlow";
import { TransferredPanel } from "./components/TransferredPanel";
import { ViewModal } from "./components/ViewModal";
import { formatClock, minutesSince, plural } from "./lib/format";
import { OpenLeadRequest, OpenViewRequest, RunnerStatus, SummaryResponse, TransferredRow } from "./types";

const QUIET_AFTER_MINUTES = 30;

function headline(summary: SummaryResponse | null): React.ReactNode {
  if (!summary || !summary.tracker_found || summary.nodes.incoming.total === 0) {
    return (
      <>
        <h2>Your lead pipeline is ready</h2>
        <p>As soon as leads arrive, you will see each one checked, scored by AI and called here.</p>
      </>
    );
  }
  const { incoming, qualified, call_results, transferred } = summary.nodes;
  const inProgress =
    summary.activity.verifying + summary.activity.deciding + summary.activity.queued + summary.activity.calling;
  return (
    <>
      <h2>
        <em>{plural(transferred.total, "conversation")}</em> handed to your team from{" "}
        <em>{plural(incoming.total, "lead")}</em>
      </h2>
      <p>
        {qualified.total.toLocaleString()} qualified · {plural(call_results.called, "call")} completed
        {inProgress > 0 ? ` · ${inProgress.toLocaleString()} in progress` : ""}
      </p>
    </>
  );
}

export const App: React.FC = () => {
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [runner, setRunner] = useState<RunnerStatus | null>(null);
  const [apiUnreachable, setApiUnreachable] = useState(false);
  const [transferredRows, setTransferredRows] = useState<TransferredRow[]>([]);
  const [activeView, setActiveView] = useState<OpenViewRequest | null>(null);
  const [activeLead, setActiveLead] = useState<OpenLeadRequest | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [batchModalOpen, setBatchModalOpen] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [dismissedRunKey, setDismissedRunKey] = useState<string | null>(null);
  const runnerSeqRef = useRef(0);

  const fetchSummary = useCallback(async () => {
    const seqBefore = runnerSeqRef.current;
    try {
      const resp = await fetch("/api/summary");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const json = (await resp.json()) as SummaryResponse;
      setSummary(json);
      if (json.runner && runnerSeqRef.current === seqBefore) {
        setRunner(json.runner);
      }
      setApiUnreachable(false);
    } catch {
      setApiUnreachable(true);
    }
  }, []);

  const fetchTransferred = useCallback(async () => {
    try {
      const resp = await fetch("/api/leads?view=transferred&page_size=200&sort=lead_id&order=desc");
      if (!resp.ok) return;
      const json = await resp.json();
      setTransferredRows(Array.isArray(json.rows) ? json.rows : []);
    } catch {
      // Keep the current list through a brief connection problem.
    }
  }, []);

  useEffect(() => {
    fetchSummary();
  }, [fetchSummary]);

  useEffect(() => {
    if (summary?.tracker_found) fetchTransferred();
    else if (summary) setTransferredRows([]);
  }, [summary?.data_as_of, summary?.tracker_found, fetchTransferred]);

  useEffect(() => {
    const interval = runner?.active ? 1500 : Math.max(2000, (summary?.refresh_seconds ?? 5) * 1000);
    const timer = setInterval(() => {
      if (!document.hidden) fetchSummary();
    }, interval);
    return () => clearInterval(timer);
  }, [runner?.active, summary?.refresh_seconds, fetchSummary]);

  const handleRunnerStarted = useCallback(
    (nextStatus: RunnerStatus) => {
      runnerSeqRef.current += 1;
      setRunner(nextStatus);
      setDismissedRunKey(null);
      setBatchModalOpen(false);
      fetchSummary();
    },
    [fetchSummary],
  );

  const handleStopRunner = useCallback(async () => {
    if (stopping) return;
    setStopping(true);
    try {
      const resp = await fetch("/api/runner/stop", { method: "POST" });
      if (resp.ok) {
        const nextStatus = (await resp.json()) as RunnerStatus;
        runnerSeqRef.current += 1;
        setRunner(nextStatus);
      }
      fetchSummary();
    } catch {
      // Keep UI responsive; next summary poll reconciles state.
    } finally {
      setStopping(false);
    }
  }, [stopping, fetchSummary]);

  const status = useMemo(() => {
    if (apiUnreachable) return { text: "Reconnecting", tone: "bad" };
    if (!summary) return { text: "Loading", tone: "quiet" };
    if (summary.stale) return { text: "Showing last saved data", tone: "warn" };
    if (!summary.tracker_found) return { text: "Waiting for first leads", tone: "quiet" };
    const a = summary.activity;
    const working = a.verifying + a.deciding + a.queued + a.calling > 0;
    if (!working) return { text: "Up to date", tone: "good" };
    const quietFor = minutesSince(summary.data_as_of);
    if (quietFor !== null && quietFor > QUIET_AFTER_MINUTES) return { text: "No recent activity", tone: "warn" };
    return { text: "Working live", tone: "live" };
  }, [apiUnreachable, summary]);

  const closeView = useCallback(() => setActiveView(null), []);
  const closeLead = useCallback(() => setActiveLead(null), []);
  const openView = useCallback((request: OpenViewRequest) => setActiveView(request), []);
  const openLead = useCallback((request: OpenLeadRequest) => setActiveLead(request), []);
  const navigateLead = useCallback(
    (id: string) => setActiveLead((current) => (current ? { ...current, id, section: undefined } : current)),
    [],
  );

  const runKey = runner ? `${runner.state}:${runner.started_at ?? ""}:${runner.finished_at ?? ""}` : null;
  const showRunnerStrip = Boolean(
    runner && (runner.active || (runner.state !== "idle" && dismissedRunKey !== runKey)),
  );
  const runnerProgressPct = useMemo(() => {
    if (!runner || runner.total_batches <= 0) return 0;
    if (runner.state === "completed") return 100;
    const completed = runner.batches_completed;
    const activeFraction = runner.active ? Math.max(completed, Math.max(0, runner.current_batch - 1) + 0.35) : completed;
    return Math.min(100, Math.round((activeFraction / runner.total_batches) * 100));
  }, [runner]);

  return (
    <div className="app-shell">
      <header className="top-bar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <h1>Lead Pipeline</h1>
          {summary?.demo && <span className="demo-chip">Demo data</span>}
        </div>
        <div className="top-status">
          <span className="rule-chip">Qualifies at {Math.round((summary?.threshold ?? 0.8) * 100)}%+</span>
          {summary?.data_as_of && <span className="updated">Updated {formatClock(summary.data_as_of)}</span>}
          <span className={`status-pill tone-${status.tone}`} role="status">
            <span className="status-light" aria-hidden="true" />
            {status.text}
          </span>
          {runner?.active ? (
            <div className="runner-top-controls">
              <span className="runner-mini-progress">
                Batch {Math.max(1, runner.current_batch)}/{runner.total_batches}
              </span>
              <button
                type="button"
                className="runner-btn tone-stop"
                disabled={runner.state === "stopping" || stopping}
                onClick={handleStopRunner}
              >
                <StopIcon size={13} />
                {runner.state === "stopping" || stopping ? "Stopping..." : "Stop"}
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="runner-btn tone-start"
              onClick={() => setBatchModalOpen(true)}
            >
              <PlayIcon size={13} />
              Start Batch
            </button>
          )}
          <button type="button" className="panel-toggle" onClick={() => setPanelOpen((open) => !open)} aria-expanded={panelOpen}>
            <PhoneForwardIcon size={15} />
            {summary?.nodes.transferred.total ?? 0}
            <span className="sr-only"> transferred calls</span>
          </button>
        </div>
      </header>

      <div className="workspace">
        <main className="main-pane">
          {apiUnreachable && (
            <div className="notice tone-bad" role="alert">
              The dashboard lost its connection. It will reconnect automatically.
            </div>
          )}
          {summary?.stale && !apiUnreachable && (
            <div className="notice tone-warn" role="status">
              Showing the last saved data while new results are being written.
            </div>
          )}
          {showRunnerStrip && runner && (
            <section className={`runner-strip state-${runner.state}`} aria-live="polite" aria-label="Batch run status">
              <div className="runner-strip-top">
                <div className="runner-strip-title">
                  <span className={`runner-strip-badge state-${runner.state}`}>
                    <span className="runner-strip-dot" aria-hidden="true" />
                    {runner.state === "running"
                      ? "Running"
                      : runner.state === "stopping"
                        ? "Stopping"
                        : runner.state === "completed"
                          ? "Completed"
                          : runner.state === "stopped"
                            ? "Stopped"
                            : "Error"}
                  </span>
                  <strong className="runner-strip-batch">
                    {runner.active
                      ? `Batch ${Math.max(1, runner.current_batch)} of ${runner.total_batches}`
                      : `${runner.batches_completed} of ${runner.total_batches} ${runner.total_batches === 1 ? "batch" : "batches"}`}
                  </strong>
                  <span className="runner-strip-counts">
                    {runner.leads_fetched.toLocaleString()} fetched · {runner.leads_processed.toLocaleString()} processed
                  </span>
                </div>
                <div className="runner-strip-right">
                  {runner.message && (
                    <span className="runner-strip-msg">{runner.error || runner.message}</span>
                  )}
                  {!runner.active && runKey && (
                    <button
                      type="button"
                      className="icon-btn runner-strip-close"
                      onClick={() => setDismissedRunKey(runKey)}
                      aria-label="Dismiss batch run status"
                    >
                      <CloseIcon size={14} />
                    </button>
                  )}
                </div>
              </div>
              <div className="runner-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={runnerProgressPct}>
                <span
                  className={`runner-progress-fill ${runner.active ? "is-active" : ""}`}
                  style={{ width: `${runnerProgressPct}%` }}
                />
              </div>
            </section>
          )}
          <div className="headline">{headline(summary)}</div>
          <PipelineFlow summary={summary} onOpen={openView} />
        </main>

        <TransferredPanel
          total={summary?.nodes.transferred.total ?? 0}
          rows={transferredRows}
          services={summary?.services ?? []}
          openOnSmallScreens={panelOpen}
          onCloseSmallScreen={() => setPanelOpen(false)}
          onSelectLead={openLead}
          onOpenAll={() => setActiveView({ view: "transferred" })}
        />
      </div>

      {batchModalOpen && (
        <BatchRunnerModal
          defaultBatchSize={runner?.batch_size || 300}
          defaultNumBatches={runner?.total_batches || 1}
          sourceUrl={runner?.source_url ?? null}
          demo={Boolean(summary?.demo)}
          onClose={() => setBatchModalOpen(false)}
          onStarted={handleRunnerStarted}
        />
      )}

      {activeView && summary && (
        <ViewModal
          key={`${activeView.view}:${activeView.filter ?? ""}`}
          view={activeView.view}
          initialFilter={activeView.filter}
          summary={summary}
          isDetailOpen={activeLead !== null}
          onClose={closeView}
          onSelectLead={openLead}
        />
      )}

      {activeLead && (
        <LeadDetailModal
          leadId={activeLead.id}
          leadIds={activeLead.list}
          section={activeLead.section}
          dataAsOf={summary?.data_as_of ?? null}
          services={summary?.services ?? []}
          onNavigate={navigateLead}
          onClose={closeLead}
        />
      )}
    </div>
  );
};
