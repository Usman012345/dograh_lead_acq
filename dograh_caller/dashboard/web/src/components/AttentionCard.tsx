import React from "react";
import { OpenViewRequest, SummaryResponse, ViewName } from "../types";
import { AlertIcon, BadgeCheckIcon, ChevronRightIcon } from "./Icons";

interface Item {
  label: string;
  value: number;
  view: ViewName;
  filter: string;
}

export const AttentionCard: React.FC<{
  summary: SummaryResponse | null;
  onOpen: (request: OpenViewRequest) => void;
}> = ({ summary, onOpen }) => {
  const n = summary?.nodes;
  const candidates: Item[] = [
    {
      label: "Calls not completed",
      value: (n?.call_results.failed ?? 0) + (n?.call_results.timeout ?? 0),
      view: "call_results",
      filter: "not_completed",
    },
    { label: "AI reviews to retry", value: n?.decision_maker.errors ?? 0, view: "decision_maker", filter: "errors" },
    { label: "Missing details", value: n?.calling_agent.not_sent ?? 0, view: "calling_agent", filter: "not_sent" },
    { label: "Numbers unchecked", value: n?.number_verifier.errors ?? 0, view: "number_verifier", filter: "errors" },
    { label: "Stopped mid-run", value: summary?.activity.interrupted ?? 0, view: "incoming", filter: "interrupted" },
  ];
  const items = candidates.filter((item) => item.value > 0);

  return (
    <section className="insight-card attention-card" aria-labelledby="attention-title">
      <header className="insight-head">
        <h2 id="attention-title">
          {items.length > 0 ? <AlertIcon size={14} className="attention-icon" /> : <BadgeCheckIcon size={14} className="clear-icon" />}
          Needs attention
        </h2>
      </header>
      {items.length === 0 ? (
        <p className="attention-clear">All clear. Nothing needs action.</p>
      ) : (
        <ul className="attention-list">
          {items.map((item) => (
            <li key={item.label}>
              <button type="button" onClick={() => onOpen({ view: item.view, filter: item.filter })}>
                <strong>{item.value}</strong>
                <span>{item.label}</span>
                <ChevronRightIcon size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
};
