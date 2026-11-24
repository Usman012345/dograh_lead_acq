import React from "react";
import { OpenViewRequest, SummaryResponse, ViewName } from "../types";
import { percent } from "../lib/format";

interface Step {
  label: string;
  value: number;
  view: ViewName;
  filter?: string;
  note: string;
}

export const FunnelCard: React.FC<{
  summary: SummaryResponse | null;
  onOpen: (request: OpenViewRequest) => void;
}> = ({ summary, onOpen }) => {
  const n = summary?.nodes;
  const received = n?.incoming.total ?? 0;
  const valid = n?.number_verifier.passed ?? 0;
  const qualified = n?.qualified.total ?? 0;
  const completed = n?.call_results.called ?? 0;
  const transferred = n?.transferred.total ?? 0;

  const steps: Step[] = [
    { label: "Leads received", value: received, view: "incoming", note: "Every lead that entered the pipeline" },
    { label: "Valid numbers", value: valid, view: "number_verifier", filter: "passed", note: `${percent(valid, received)}% of leads received` },
    { label: "Qualified", value: qualified, view: "qualified", note: `${percent(qualified, valid)}% of valid numbers` },
    { label: "Calls completed", value: completed, view: "call_results", filter: "called", note: `${percent(completed, qualified)}% of qualified leads` },
    { label: "Transferred to team", value: transferred, view: "transferred", note: `${percent(transferred, completed)}% of completed calls` },
  ];

  return (
    <section className="insight-card funnel-card" aria-labelledby="funnel-title">
      <header className="insight-head">
        <h2 id="funnel-title">From lead to live conversation</h2>
        <span>% of leads received</span>
      </header>
      <ol className="funnel-list">
        {steps.map((step) => {
          const share = percent(step.value, received);
          return (
            <li key={step.label}>
              <button
                type="button"
                className="funnel-row"
                onClick={() => onOpen({ view: step.view, filter: step.filter })}
                aria-label={`${step.label}: ${step.value}. ${step.note}. Open list.`}
              >
                <span className="funnel-label">{step.label}</span>
                <span className="funnel-value">{step.value.toLocaleString()}</span>
                <span className="funnel-share">{share}%</span>
                <span className="funnel-track">
                  <span className="funnel-bar" style={{ width: step.value > 0 ? `max(6px, ${share}%)` : 0 }} />
                </span>
                <span className="funnel-tip" role="presentation">
                  <strong>{step.value.toLocaleString()}</strong> {step.note}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
};
