import React from "react";
import { OpenLeadRequest, ServiceInfo, TransferredRow } from "../types";
import { formatDateTime, serviceColor } from "../lib/format";
import { CloseIcon, PhoneForwardIcon } from "./Icons";

interface Props {
  total: number;
  rows: TransferredRow[];
  services: ServiceInfo[];
  openOnSmallScreens: boolean;
  onCloseSmallScreen: () => void;
  onSelectLead: (request: OpenLeadRequest) => void;
  onOpenAll: () => void;
}

export const TransferredPanel: React.FC<Props> = ({
  total,
  rows,
  services,
  openOnSmallScreens,
  onCloseSmallScreen,
  onSelectLead,
  onOpenAll,
}) => {
  const ids = rows.map((row) => row.lead_id);
  return (
    <aside className={`transfer-panel ${openOnSmallScreens ? "is-open" : ""}`} aria-label="Transferred Calls">
      <header className="transfer-head">
        <span className="transfer-icon">
          <PhoneForwardIcon size={17} />
        </span>
        <div>
          <h2>Transferred Calls</h2>
          <p>Conversations handed to your team</p>
        </div>
        <strong className="transfer-total">{total.toLocaleString()}</strong>
        <button type="button" className="icon-btn transfer-close" onClick={onCloseSmallScreen} aria-label="Close transferred calls">
          <CloseIcon size={16} />
        </button>
      </header>

      <div className="transfer-body">
        {rows.length === 0 ? (
          <div className="transfer-empty">
            <PhoneForwardIcon size={24} />
            <strong>No transfers yet</strong>
            <span>When the AI agent connects a prospect to your team, it appears here.</span>
          </div>
        ) : (
          <ul className="transfer-list">
            {rows.map((row) => (
              <li key={row.lead_id}>
                <button type="button" className="transfer-card" onClick={() => onSelectLead({ id: row.lead_id, list: ids, section: "call" })}>
                  <span className="transfer-biz">{row.business || "Unnamed business"}</span>
                  <span className="transfer-service">
                    <span className="service-dot" style={{ background: serviceColor(services, row.assigned_service) }} />
                    {row.assigned_service || "No service"}
                  </span>
                  <span className="transfer-meta">
                    <span className="mono">{row.lead_id}</span>
                    <span>{[row.duration_label, formatDateTime(row.transferred_at)].filter(Boolean).join(" · ")}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {rows.length > 0 && (
        <button type="button" className="transfer-all" onClick={onOpenAll}>
          View all transferred calls
        </button>
      )}
    </aside>
  );
};
