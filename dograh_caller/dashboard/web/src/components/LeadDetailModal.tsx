import React, { useEffect, useMemo, useRef, useState } from "react";
import { LeadDetailResponse, ServiceInfo, TranscriptTurn } from "../types";
import {
  formatDateTime,
  formatFieldValue,
  formatPhone,
  humanizeKey,
  isHttpLink,
  serviceColor,
  transcriptOffset,
} from "../lib/format";
import {
  AlertIcon,
  BadgeCheckIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ClockIcon,
  CloseIcon,
  ExternalIcon,
  FileTextIcon,
  MinusCircleIcon,
  PhoneForwardIcon,
  PhoneIcon,
  ShieldPhoneIcon,
  SkipIcon,
  SparkIcon,
} from "./Icons";
import { useFocusTrap } from "./useFocusTrap";

const STEP_STATE_TEXT: Record<string, string> = {
  done: "Complete",
  current: "In progress",
  error: "Stopped",
  skipped: "Not needed",
  pending: "Not reached",
};

function callReasonTitle(reason: string): string {
  if (reason.startsWith("Not called")) return "Why this number was not called";
  if (reason.startsWith("Called")) return "Why this number was called";
  return "Call decision";
}

const FieldValue: React.FC<{ value: unknown }> = ({ value }) => {
  const [expanded, setExpanded] = useState(false);
  const text = formatFieldValue(value);
  const isLong = text.length > 280;
  if (isHttpLink(text)) {
    return (
      <a href={text.trim()} target="_blank" rel="noopener noreferrer" className="text-link">
        {text.trim()}
      </a>
    );
  }
  return (
    <div>
      <div className="field-text">{isLong && !expanded ? `${text.slice(0, 280).trimEnd()}…` : text}</div>
      {isLong && (
        <button type="button" className="link-btn" onClick={() => setExpanded((open) => !open)}>
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
};

const FieldList: React.FC<{ fields: Record<string, unknown>; raw?: boolean }> = ({ fields, raw }) => (
  <dl className="field-list">
    {Object.entries(fields).map(([key, value]) => (
      <div key={key}>
        <dt>{raw ? key : humanizeKey(key)}</dt>
        <dd>
          <FieldValue value={value} />
        </dd>
      </div>
    ))}
  </dl>
);

const Fact: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="fact">
    <span>{label}</span>
    <strong>{children || "—"}</strong>
  </div>
);

const Transcript: React.FC<{ leadId: string; dataAsOf: string | null }> = ({ leadId, dataAsOf }) => {
  const [turns, setTurns] = useState<TranscriptTurn[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let current = true;
    setTurns(null);
    setFailed(false);
    fetch(`/api/leads/${encodeURIComponent(leadId)}/transcript`)
      .then(async (resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return (await resp.json()) as { turns: TranscriptTurn[] };
      })
      .then((json) => current && setTurns(json.turns))
      .catch(() => current && setFailed(true));
    return () => {
      current = false;
    };
  }, [leadId, dataAsOf]);

  if (failed) return <p className="soft-note">The transcript could not be loaded right now.</p>;
  if (!turns) return <div className="transcript-loading">Loading conversation…</div>;
  if (turns.length === 0) return <p className="soft-note">Nothing was said on this call.</p>;
  const start = turns[0]?.at ?? null;
  return (
    <ol className="transcript" aria-label="Call transcript">
      {turns.map((turn, index) => (
        <li key={index} className={`turn speaker-${turn.speaker}`}>
          <span className="turn-meta">
            {turn.speaker === "agent" ? "AI agent" : turn.speaker === "customer" ? "Customer" : "Note"}
            {transcriptOffset(turn.at, start) && <time>{transcriptOffset(turn.at, start)}</time>}
          </span>
          <p>{turn.text}</p>
        </li>
      ))}
    </ol>
  );
};

const AudioPlayer: React.FC<{ tracks: { id: string; label: string; url: string }[] }> = ({ tracks }) => {
  const [selected, setSelected] = useState(tracks[0]?.id ?? "");
  const [failed, setFailed] = useState(false);
  const track = tracks.find((t) => t.id === selected) ?? tracks[0];
  useEffect(() => setFailed(false), [track?.url]);
  if (!track) return null;
  return (
    <div className="audio-block">
      {tracks.length > 1 && (
        <div className="segmented" role="group" aria-label="Choose recording">
          {tracks.map((t) => (
            <button key={t.id} type="button" className={t.id === track.id ? "is-active" : ""} aria-pressed={t.id === track.id} onClick={() => setSelected(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
      )}
      {failed ? (
        <p className="soft-note">This recording could not be loaded right now.</p>
      ) : (
        <audio key={track.url} controls preload="none" src={track.url} onError={() => setFailed(true)}>
          Your browser cannot play this recording.
        </audio>
      )}
    </div>
  );
};

interface Props {
  leadId: string;
  leadIds: string[];
  section?: "call";
  dataAsOf: string | null;
  services: ServiceInfo[];
  onNavigate: (id: string) => void;
  onClose: () => void;
}

export const LeadDetailModal: React.FC<Props> = ({ leadId, leadIds, section, dataAsOf, services, onNavigate, onClose }) => {
  const panelRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const callRef = useRef<HTMLElement>(null);
  const [detail, setDetail] = useState<LeadDetailResponse | null>(null);
  const [failed, setFailed] = useState(false);

  useFocusTrap(panelRef, true, onClose);

  useEffect(() => {
    let current = true;
    setFailed(false);
    fetch(`/api/leads/${encodeURIComponent(leadId)}`)
      .then(async (resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return (await resp.json()) as LeadDetailResponse;
      })
      .then((json) => current && setDetail(json))
      .catch(() => current && setFailed(true));
    return () => {
      current = false;
    };
  }, [leadId, dataAsOf]);

  const shown = detail?.lead_id === leadId ? detail : null;

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: 0 });
  }, [leadId]);

  useEffect(() => {
    if (shown && section === "call") callRef.current?.scrollIntoView({ block: "start" });
  }, [shown?.lead_id, section]);

  const index = leadIds.indexOf(leadId);
  const previous = index > 0 ? leadIds[index - 1] : null;
  const next = index >= 0 && index < leadIds.length - 1 ? leadIds[index + 1] : null;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target?.closest("input, textarea, select, audio")) return;
      if (event.key === "ArrowLeft" && previous) onNavigate(previous);
      if (event.key === "ArrowRight" && next) onNavigate(next);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [previous, next, onNavigate]);

  const scores = useMemo(
    () => [...(shown?.decision.scores ?? [])].sort((a, b) => b.probability - a.probability),
    [shown],
  );
  const thresholdPct = Math.round((shown?.decision.threshold ?? 0.8) * 100);
  const result = shown?.decision.result ?? "waiting";
  const bestPct = shown?.decision.best_probability != null && scores.length > 0
    ? Math.round(shown.decision.best_probability * 100)
    : null;
  const verdictTone = result === "qualified" ? "good" : result === "error" ? "bad" : result === "non_qualified" ? "muted" : "wait";
  const verdictLabel =
    result === "qualified" ? "Qualified" :
    result === "non_qualified" ? "Non-Qualified" :
    result === "error" ? "Could not assess" :
    shown?.number_check.result === "rejected" || shown?.number_check.result === "error" ? "Not reviewed" : "Waiting for review";
  const verdictText =
    shown?.decision.verdict ||
    (result === "error"
      ? "The AI review could not be completed, so this lead was not called."
      : shown?.number_check.result === "rejected" || shown?.number_check.result === "error"
        ? "The phone number did not pass, so AI did not review this lead."
        : "This lead has not been reviewed by AI yet.");
  const leadFields = shown ? Object.entries(shown.lead_fields).filter(([, value]) => value !== "" && value !== null) : [];

  return (
    <div className="backdrop detail-layer" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={panelRef} className="detail-drawer" role="dialog" aria-modal="true" aria-labelledby="detail-title" tabIndex={-1}>
        <header className="detail-head">
          <div className="detail-names">
            <span className="mono lead-id">{leadId}</span>
            <h2 id="detail-title">{shown?.business ?? "Loading lead…"}</h2>
            {shown && <p>{shown.status_label}</p>}
          </div>
          <div className="detail-actions">
            {index >= 0 && leadIds.length > 1 && (
              <>
                <button type="button" className="icon-btn" disabled={!previous} onClick={() => previous && onNavigate(previous)} aria-label="Previous lead">
                  <ChevronLeftIcon size={17} />
                </button>
                <span className="detail-position">{index + 1} of {leadIds.length}</span>
                <button type="button" className="icon-btn" disabled={!next} onClick={() => next && onNavigate(next)} aria-label="Next lead">
                  <ChevronRightIcon size={17} />
                </button>
              </>
            )}
            <button type="button" className="icon-btn" onClick={onClose} aria-label="Close lead details">
              <CloseIcon />
            </button>
          </div>
        </header>

        <div className="detail-body" ref={bodyRef}>
          {failed && !shown ? (
            <div className="empty-state is-error">
              <AlertIcon size={24} />
              <strong>This lead could not be loaded</strong>
              <span>Close this panel and try again.</span>
            </div>
          ) : !shown ? (
            <div className="table-skeleton"><span /><span /><span /><span /></div>
          ) : (
            <>
              <section className="detail-card" aria-labelledby="journey-title">
                <h3 id="journey-title">Lead journey</h3>
                <ol className="journey">
                  {shown.journey.map((step) => (
                    <li key={step.stage} className={`state-${step.state}`}>
                      <span className="journey-mark">
                        {step.state === "done" ? <CheckIcon size={13} /> : step.state === "error" ? <CloseIcon size={13} /> : step.state === "current" ? <ClockIcon size={13} /> : <SkipIcon size={13} />}
                      </span>
                      <strong>{step.stage}</strong>
                      <span>{STEP_STATE_TEXT[step.state]}</span>
                      {step.at && <time>{formatDateTime(step.at)}</time>}
                    </li>
                  ))}
                </ol>
              </section>

              {shown.status_detail && (
                <div className="inline-alert">
                  <AlertIcon size={15} />
                  {shown.status_detail}
                </div>
              )}

              <section className="detail-card" aria-labelledby="number-title">
                <h3 id="number-title"><ShieldPhoneIcon size={15} /> Number check</h3>
                <div className="fact-grid">
                  <Fact label="Result">{shown.number_check.result === "passed" ? "Valid" : shown.number_check.result === "rejected" ? "Rejected" : shown.number_check.result === "error" ? "Could not check" : "Waiting"}</Fact>
                  <Fact label="Details">{shown.number_check.reason}</Fact>
                  <Fact label="Number">{formatPhone(shown.number_check.phone)}</Fact>
                  <Fact label="Location">{shown.number_check.location}</Fact>
                  <Fact label="Time zone">{shown.number_check.time_zone.replace(/_/g, " ")}</Fact>
                </div>
              </section>

              <section className="detail-card" aria-labelledby="reasoning-title">
                <h3 id="reasoning-title"><SparkIcon size={15} /> Decision reasoning</h3>

                <div className={`verdict tone-${verdictTone}`} aria-label="Decision">
                  <div className="verdict-score">
                    {bestPct !== null ? <strong>{bestPct}<small>%</small></strong> : <strong className="no-score">—</strong>}
                    <span>best match</span>
                  </div>
                  <div className="verdict-copy">
                    <span className="verdict-badge">
                      {verdictTone === "good" ? <BadgeCheckIcon size={14} /> : verdictTone === "bad" ? <AlertIcon size={14} /> : verdictTone === "wait" ? <ClockIcon size={14} /> : <MinusCircleIcon size={14} />}
                      {verdictLabel}
                    </span>
                    <h4>{verdictText}</h4>
                    {shown.decision.summary && <p>{shown.decision.summary}</p>}
                  </div>
                </div>

                <div className="assigned-service-row">
                  <span className="assigned-label">Assigned service</span>
                  {shown.decision.assigned_service ? (
                    <strong className="service-cell">
                      <span
                        className="service-dot"
                        style={{
                          background: serviceColor(
                            services,
                            shown.decision.assigned_service_id ?? shown.decision.assigned_service,
                          ),
                        }}
                      />
                      {shown.decision.assigned_service}
                    </strong>
                  ) : (
                    <strong className="muted-text">
                      {result === "non_qualified" ? "None (below requirement)" : "Not assigned"}
                    </strong>
                  )}
                </div>

                {scores.length === 0 ? (
                  <p className="soft-note">No service scores are available for this lead.</p>
                ) : (
                  <>
                    <ul className="score-list">
                      {scores.map((score) => {
                        const pct = Math.round(score.probability * 100);
                        return (
                          <li key={score.service_id}>
                            <div className="score-top">
                              <span className="service-cell">
                                <span className="service-dot" style={{ background: serviceColor(services, score.service_id) }} />
                                {score.service_name}
                              </span>
                              <strong>{pct}%</strong>
                              <span className={`score-state ${score.meets_threshold ? "meets" : ""}`}>
                                {score.meets_threshold ? <CheckIcon size={12} /> : <MinusCircleIcon size={12} />}
                                {score.meets_threshold ? "Meets requirement" : "Below requirement"}
                              </span>
                            </div>
                            <div className="score-track" role="img" aria-label={`${score.service_name}: ${pct}%. The requirement is ${thresholdPct}%.`}>
                              <span className={`score-fill ${score.meets_threshold ? "meets" : ""}`} style={{ width: `${pct}%` }} />
                              <span className="score-rule" style={{ left: `${thresholdPct}%` }} />
                            </div>
                            <p>{score.reason}</p>
                          </li>
                        );
                      })}
                    </ul>
                    <p className="scale-note">The marker shows the {thresholdPct}% requirement. {shown.decision.model ? `Reviewed by ${shown.decision.model}.` : ""}</p>
                  </>
                )}

                <div className="call-reason">
                  <PhoneIcon size={16} />
                  <div>
                    <span>{callReasonTitle(shown.decision.call_reason)}</span>
                    <strong>{shown.decision.call_reason || "No call decision has been made yet."}</strong>
                  </div>
                </div>
              </section>

              {shown.call && (
                <section className="detail-card call-card" aria-labelledby="call-title" ref={callRef}>
                  <h3 id="call-title">
                    <PhoneIcon size={15} /> Call result
                    {shown.call.transferred && (
                      <span className="transfer-flag"><PhoneForwardIcon size={13} /> Transferred to your team</span>
                    )}
                  </h3>
                  <div className="fact-grid">
                    <Fact label="Status">{shown.call.status_label}</Fact>
                    <Fact label="Outcome">{shown.call.disposition}</Fact>
                    <Fact label="Duration">{shown.call.duration_label}</Fact>
                    <Fact label="Attempts">{shown.call.attempts}</Fact>
                    <Fact label="Agent">{shown.call.agent_name}</Fact>
                    <Fact label="Campaign">{shown.call.campaign_name.replace(/_/g, " ")}</Fact>
                  </div>

                  {shown.call.audio_tracks.length > 0 && (
                    <div className="call-section">
                      <h4>Recording</h4>
                      <AudioPlayer key={leadId} tracks={shown.call.audio_tracks} />
                    </div>
                  )}

                  {Object.keys(shown.call.collected).length > 0 && (
                    <div className="call-section">
                      <h4>What the agent learned</h4>
                      <FieldList fields={shown.call.collected} />
                    </div>
                  )}

                  {(shown.call.transcript_available || shown.call.transcript_url) && (
                    <div className="call-section">
                      <h4><FileTextIcon size={14} /> Conversation</h4>
                      {shown.call.transcript_available ? (
                        <Transcript leadId={leadId} dataAsOf={dataAsOf} />
                      ) : (
                        <a href={shown.call.transcript_url ?? undefined} target="_blank" rel="noopener noreferrer" className="text-link">
                          Open transcript <ExternalIcon size={13} />
                        </a>
                      )}
                    </div>
                  )}

                  {Object.keys(shown.call.fields).length > 0 && (
                    <details className="more-details">
                      <summary>All call data ({Object.keys(shown.call.fields).length} fields)</summary>
                      <FieldList fields={shown.call.fields} raw />
                    </details>
                  )}
                </section>
              )}

              <section className="detail-card">
                <details className="more-details" open>
                  <summary>All lead details ({leadFields.length} fields)</summary>
                  {leadFields.length === 0 ? (
                    <p className="soft-note">No original lead details were recorded.</p>
                  ) : (
                    <FieldList fields={Object.fromEntries(leadFields)} />
                  )}
                </details>
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
