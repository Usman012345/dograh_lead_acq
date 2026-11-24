import React, { useEffect, useMemo, useRef, useState } from "react";
import { LeadDetailResponse, ServiceInfo, TranscriptTurn } from "../types";
import { apiPath } from "../lib/api";
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
    fetch(apiPath(`/leads/${encodeURIComponent(leadId)}/transcript`))
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
    fetch(apiPath(`/leads/${encodeURIComponent(leadId)}`))
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
        <header className="drawer-head">
          <div className="drawer-titles">
            <span className="mono drawer-lead-id">{leadId}</span>
            <h2 id="detail-title">{shown?.business || (shown ? "Unnamed business" : "Loading details…")}</h2>
          </div>
          <div className="drawer-actions">
            {leadIds.length > 1 && (
              <div className="nav-group">
                <button
                  type="button"
                  className="icon-btn"
                  disabled={!previous}
                  onClick={() => previous && onNavigate(previous)}
                  aria-label="Previous lead"
                >
                  <ChevronLeftIcon />
                </button>
                <span className="nav-position">
                  {index >= 0 ? `${index + 1} of ${leadIds.length}` : `${leadIds.length} leads`}
                </span>
                <button
                  type="button"
                  className="icon-btn"
                  disabled={!next}
                  onClick={() => next && onNavigate(next)}
                  aria-label="Next lead"
                >
                  <ChevronRightIcon />
                </button>
              </div>
            )}
            <button type="button" className="icon-btn" onClick={onClose} aria-label="Close lead detail">
              <CloseIcon />
            </button>
          </div>
        </header>

        <div className="drawer-body" ref={bodyRef}>
          {failed && (
            <div className="detail-notice tone-bad">
              Could not load details for this lead. Check that the dashboard server is running and try again.
            </div>
          )}

          {shown && (
            <>
              <section className="drawer-section" aria-labelledby="journey-title">
                <h3 id="journey-title" className="section-title">
                  Pipeline Journey
                </h3>
                <ol className="journey-strip">
                  {shown.journey.map((step) => {
                    const icon =
                      step.state === "done" ? <CheckIcon size={12} /> :
                      step.state === "current" ? <ClockIcon size={12} /> :
                      step.state === "error" ? <AlertIcon size={12} /> :
                      step.state === "skipped" ? <SkipIcon size={12} /> :
                      null;
                    return (
                      <li key={step.stage} className={`journey-step state-${step.state}`}>
                        <span className="step-icon">{icon}</span>
                        <div className="step-content">
                          <span className="step-name">{step.stage}</span>
                          <span className="step-state">{STEP_STATE_TEXT[step.state] ?? step.state}</span>
                          {step.at && <time className="step-time">{formatDateTime(step.at)}</time>}
                        </div>
                      </li>
                    );
                  })}
                </ol>
              </section>

              <section className="drawer-section" aria-labelledby="phone-title">
                <h3 id="phone-title" className="section-title">
                  <ShieldPhoneIcon size={16} /> Phone Verification
                </h3>
                <div className="fact-grid">
                  <Fact label="Formatted number">{formatPhone(shown.number_check.phone)}</Fact>
                  <Fact label="Original value">{shown.number_check.phone_raw}</Fact>
                  <Fact label="Outcome">
                    <span className={`pill tone-${shown.number_check.result === "passed" ? "good" : "warn"}`}>
                      {shown.number_check.result === "passed" ? <BadgeCheckIcon size={13} /> : <AlertIcon size={13} />}
                      {shown.number_check.reason}
                    </span>
                  </Fact>
                  <Fact label="Location">
                    {[shown.number_check.location, shown.number_check.time_zone].filter(Boolean).join(" · ")}
                  </Fact>
                </div>
              </section>

              <section className="drawer-section" aria-labelledby="decision-title">
                <div className="section-head">
                  <h3 id="decision-title" className="section-title">
                    <SparkIcon size={16} /> AI Qualification
                  </h3>
                  {shown.decision.model && <span className="mono model-chip">{shown.decision.model}</span>}
                </div>

                <div className={`verdict-banner tone-${verdictTone}`}>
                  <div className="verdict-head">
                    <span className="verdict-label">{verdictLabel}</span>
                    {bestPct !== null && (
                      <span className="verdict-pct">
                        Best match <strong>{bestPct}%</strong> (qualifies at {thresholdPct}%+)
                      </span>
                    )}
                  </div>
                  <p className="verdict-desc">{verdictText}</p>
                </div>

                {scores.length > 0 && (
                  <div className="scores-list" role="list" aria-label="Fit estimate by service">
                    {scores.map((score) => {
                      const pct = Math.round(score.probability * 100);
                      const isAssigned = shown.decision.assigned_service_id === score.service_id;
                      const meets = score.meets_threshold;
                      return (
                        <div key={score.service_id} className={`score-card ${isAssigned ? "is-assigned" : ""}`}>
                          <div className="score-top">
                            <span className="score-name">
                              <span className="service-dot" style={{ background: serviceColor(services, score.service_id) }} />
                              <strong>{score.service_name}</strong>
                              {isAssigned && <span className="assigned-chip">Selected service</span>}
                            </span>
                            <span className="score-value">
                              <span className="prob-number">{pct}%</span>
                              <span className="prob-track" aria-hidden="true">
                                <span className={`prob-fill ${meets ? "meets" : ""}`} style={{ width: `${pct}%` }} />
                                <span className="prob-rule" style={{ left: `${thresholdPct}%` }} />
                              </span>
                            </span>
                          </div>
                          {score.reason && <p className="score-reason">{score.reason}</p>}
                        </div>
                      );
                    })}
                  </div>
                )}

                {shown.decision.call_reason && (
                  <div className="call-reason-block">
                    <span className="call-reason-title">{callReasonTitle(shown.decision.call_reason)}</span>
                    <p>{shown.decision.call_reason}</p>
                  </div>
                )}
              </section>

              <section className="drawer-section" aria-labelledby="call-title" ref={callRef}>
                <h3 id="call-title" className="section-title">
                  <PhoneIcon size={16} /> Outbound Call
                </h3>
                {shown.call ? (
                  <>
                    <div className="fact-grid">
                      <Fact label="Outcome">
                        <span className="pill tone-good">
                          <BadgeCheckIcon size={13} />
                          {shown.call.status_label || shown.call.status}
                        </span>
                      </Fact>
                      <Fact label="Disposition">{shown.call.disposition || "—"}</Fact>
                      <Fact label="Duration">{shown.call.duration_label || "—"}</Fact>
                      <Fact label="Handed to team">
                        {shown.call.transferred ? (
                          <span className="pill tone-good">
                            <PhoneForwardIcon size={13} /> Transferred
                          </span>
                        ) : (
                          <span className="muted-text">No</span>
                        )}
                      </Fact>
                      {shown.call.campaign_name && <Fact label="Campaign">{shown.call.campaign_name}</Fact>}
                      {shown.call.dograh_run_id && <Fact label="Dograh run ID">{String(shown.call.dograh_run_id)}</Fact>}
                    </div>

                    {shown.call.audio_tracks && shown.call.audio_tracks.length > 0 && (
                      <div className="media-block">
                        <span className="media-block-title">Call Recording</span>
                        <AudioPlayer tracks={shown.call.audio_tracks} />
                      </div>
                    )}

                    {shown.call.transcript_available && (
                      <div className="media-block">
                        <span className="media-block-title">Transcript</span>
                        <Transcript leadId={leadId} dataAsOf={dataAsOf} />
                      </div>
                    )}

                    {shown.call.collected && Object.keys(shown.call.collected).length > 0 && (
                      <div className="collected-block">
                        <span className="media-block-title">Information Collected by Agent</span>
                        <FieldList fields={shown.call.collected} />
                      </div>
                    )}
                  </>
                ) : (
                  <p className="soft-note">
                    {shown.decision.result === "qualified"
                      ? "This lead is qualified and queued for an outbound call."
                      : "This lead was not called."}
                  </p>
                )}
              </section>

              <section className="drawer-section" aria-labelledby="fields-title">
                <h3 id="fields-title" className="section-title">
                  <FileTextIcon size={16} /> Lead Details
                </h3>
                {leadFields.length > 0 ? (
                  <FieldList fields={Object.fromEntries(leadFields)} />
                ) : (
                  <p className="soft-note">No additional fields recorded for this lead.</p>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
