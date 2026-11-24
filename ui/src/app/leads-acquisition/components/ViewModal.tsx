import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ColumnDef, LeadsResponse, OpenLeadRequest, ServiceInfo, SummaryResponse, ViewName } from "../types";
import { apiPath } from "../lib/api";
import { formatDateTime, formatPhone, isHttpLink, serviceColor } from "../lib/format";
import {
  AlertIcon,
  BadgeCheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ClipboardIcon,
  ClockIcon,
  CloseIcon,
  FileTextIcon,
  HeadsetIcon,
  InboxIcon,
  MinusCircleIcon,
  PhoneForwardIcon,
  PlayIcon,
  SearchIcon,
  ShieldPhoneIcon,
  SparkIcon,
} from "./Icons";
import { useFocusTrap } from "./useFocusTrap";

interface FilterOption {
  value: string;
  label: string;
  count: number;
}

const VIEW_META: Record<ViewName, { title: string; description: string; icon: React.ReactNode }> = {
  incoming: { title: "Incoming Leads", description: "Every lead that entered the pipeline.", icon: <InboxIcon /> },
  number_verifier: {
    title: "Number Verifier",
    description: "Each phone number is checked before any AI review or call.",
    icon: <ShieldPhoneIcon />,
  },
  decision_maker: {
    title: "Decision Maker",
    description: "AI estimates how likely each business is to need each service.",
    icon: <SparkIcon />,
  },
  non_qualified: {
    title: "Non-Qualified Leads",
    description: "These leads did not reach the requirement for any service, so they were not called.",
    icon: <MinusCircleIcon />,
  },
  qualified: {
    title: "Qualified Leads",
    description: "These leads met the requirement and were matched to their best service.",
    icon: <BadgeCheckIcon />,
  },
  calling_agent: {
    title: "Calling Agent",
    description: "Qualified leads waiting for, or currently on, a call with the AI agent.",
    icon: <HeadsetIcon />,
  },
  call_results: {
    title: "Call Results",
    description: "Everything returned from each finished call.",
    icon: <ClipboardIcon />,
  },
  transferred: {
    title: "Transferred Calls",
    description: "Conversations the AI agent handed to your team.",
    icon: <PhoneForwardIcon />,
  },
};

function filtersFor(
  view: ViewName,
  summary: SummaryResponse,
  filterCounts?: Record<string, number>,
): FilterOption[] {
  const n = summary.nodes;
  const countOf = (key: string, fallback: number) =>
    filterCounts && typeof filterCounts[key] === "number" ? filterCounts[key] : fallback;
  const withAll = (all: number, options: FilterOption[]) => [
    { value: "", label: "All", count: countOf("", all) },
    ...options.filter((option) => option.count > 0),
  ];
  switch (view) {
    case "incoming": {
      const interrupted = countOf("interrupted", summary.activity.interrupted);
      return interrupted > 0
        ? withAll(n.incoming.total, [{ value: "interrupted", label: "Stopped mid-run", count: interrupted }])
        : [];
    }
    case "number_verifier":
      return withAll(n.incoming.total, [
        { value: "passed", label: "Valid", count: countOf("passed", n.number_verifier.passed) },
        { value: "rejected", label: "Rejected", count: countOf("rejected", n.number_verifier.rejected) },
        { value: "errors", label: "Could not check", count: countOf("errors", n.number_verifier.errors) },
        { value: "waiting", label: "Waiting", count: countOf("waiting", n.number_verifier.waiting) },
      ]);
    case "decision_maker":
      return withAll(n.number_verifier.passed, [
        { value: "qualified", label: "Qualified", count: countOf("qualified", n.qualified.total) },
        { value: "non_qualified", label: "Non-Qualified", count: countOf("non_qualified", n.non_qualified.total) },
        { value: "errors", label: "To retry", count: countOf("errors", n.decision_maker.errors) },
        { value: "waiting", label: "Waiting", count: countOf("waiting", n.decision_maker.waiting) },
      ]);
    case "calling_agent": {
      const c = n.calling_agent;
      return withAll(c.waiting + c.calling + c.not_sent + c.not_called + c.interrupted, [
        { value: "calling", label: "On a call", count: countOf("calling", c.calling) },
        { value: "waiting", label: "Queued", count: countOf("waiting", c.waiting) },
        { value: "not_sent", label: "Not sent", count: countOf("not_sent", c.not_sent) },
        { value: "not_called", label: "Test run", count: countOf("not_called", c.not_called) },
        { value: "interrupted", label: "Stopped", count: countOf("interrupted", c.interrupted) },
      ]);
    }
    case "call_results":
      return withAll(n.call_results.total, [
        { value: "called", label: "Completed", count: countOf("called", n.call_results.called) },
        {
          value: "not_completed",
          label: "Not completed",
          count: countOf("not_completed", n.call_results.failed + n.call_results.timeout),
        },
        { value: "failed", label: "Failed", count: countOf("failed", n.call_results.failed) },
        { value: "timeout", label: "Timed out", count: countOf("timeout", n.call_results.timeout) },
      ]);
    default:
      return [];
  }
}

type Tone = "good" | "warn" | "bad" | "wait" | "muted";

const PILL_TONES: Record<string, Tone> = {
  Passed: "good",
  Qualified: "good",
  "Call completed": "good",
  Yes: "good",
  Rejected: "warn",
  "Non-Qualified": "muted",
  No: "muted",
  Error: "bad",
  "Call failed": "bad",
  "Call timed out": "warn",
  Waiting: "wait",
  "Waiting for campaign": "wait",
  Calling: "wait",
  "Not sent": "warn",
  Interrupted: "warn",
  "Qualified (test run, not called)": "muted",
};

const PILL_TEXT: Record<string, string> = {
  Passed: "Valid",
  Error: "Could not assess",
  "Waiting for campaign": "Queued",
  Calling: "On a call",
  "Qualified (test run, not called)": "Test run",
};

const PILL_KEYS = new Set([
  "verification_result_label",
  "decision_result_label",
  "call_progress_label",
  "call_status_label",
]);

export const StatusPill: React.FC<{ text: string; columnKey?: string }> = ({ text, columnKey }) => {
  const tone = PILL_TONES[text] ?? "muted";
  const icon =
    tone === "good" ? <BadgeCheckIcon size={13} /> :
    tone === "wait" ? <ClockIcon size={13} /> :
    tone === "muted" ? <MinusCircleIcon size={13} /> :
    <AlertIcon size={13} />;
  const label =
    columnKey === "verification_result_label" && text === "Error"
      ? "Could not check"
      : PILL_TEXT[text] ?? text;
  return (
    <span className={`pill tone-${tone}`}>
      {icon}
      {label}
    </span>
  );
};

const ProbabilityCell: React.FC<{ text: string; thresholdPct: number }> = ({ text, thresholdPct }) => {
  const value = Number.parseInt(text, 10);
  if (Number.isNaN(value)) return <span className="empty-cell">—</span>;
  const meets = value >= thresholdPct;
  return (
    <span className="prob-cell">
      <span className="prob-number">{value}%</span>
      <span className="prob-track" aria-hidden="true">
        <span className={`prob-fill ${meets ? "meets" : ""}`} style={{ width: `${value}%` }} />
        <span className="prob-rule" style={{ left: `${thresholdPct}%` }} />
      </span>
    </span>
  );
};

interface CellProps {
  column: ColumnDef;
  row: Record<string, unknown>;
  services: ServiceInfo[];
  thresholdPct: number;
  onOpenCall: () => void;
}

const Cell: React.FC<CellProps> = ({ column, row, services, thresholdPct, onOpenCall }) => {
  const raw = row[column.key];
  const text = raw === null || raw === undefined ? "" : String(raw);
  const key = column.key;

  if (key === "lead_id") return <span className="mono lead-id">{text}</span>;
  if (key === "business") return <span className="cell-strong">{text || "Unnamed business"}</span>;
  if (!text) return <span className="empty-cell">—</span>;
  if (key === "phone") return <span className="nowrap">{formatPhone(text)}</span>;
  if (key === "received_at" || key === "transferred_at") return <span className="nowrap">{formatDateTime(text)}</span>;
  if (key === "decision_summary") return <span className="why-clamp" title={text}>{text}</span>;
  if (key === "best_probability_pct") return <ProbabilityCell text={text} thresholdPct={thresholdPct} />;
  if (PILL_KEYS.has(key)) return <StatusPill text={text} columnKey={key} />;
  if (key === "transferred_label") {
    return text === "Yes" ? <StatusPill text="Yes" /> : <span className="muted-text">No</span>;
  }
  if (key === "assigned_service" || key === "best_service") {
    return (
      <span className="service-cell">
        <span className="service-dot" style={{ background: serviceColor(services, text) }} />
        {text}
      </span>
    );
  }
  if (key === "transcript_url" || key === "recording_url") {
    const label = key === "transcript_url" ? "Read" : "Listen";
    const icon = key === "transcript_url" ? <FileTextIcon size={13} /> : <PlayIcon size={12} />;
    return (
      <button
        type="button"
        className="media-chip"
        onClick={(event) => {
          event.stopPropagation();
          onOpenCall();
        }}
      >
        {icon}
        {label}
      </button>
    );
  }
  if (isHttpLink(text)) {
    return (
      <a href={text} target="_blank" rel="noopener noreferrer" className="text-link" onClick={(event) => event.stopPropagation()}>
        Open link
      </a>
    );
  }
  return <span className={key.startsWith("dograh:") ? "field-cell" : undefined} title={text.length > 60 ? text : undefined}>{text}</span>;
};

interface Props {
  view: ViewName;
  initialFilter?: string;
  summary: SummaryResponse;
  isDetailOpen: boolean;
  onClose: () => void;
  onSelectLead: (request: OpenLeadRequest) => void;
}

const PAGE_SIZE = 50;

export const ViewModal: React.FC<Props> = ({ view, initialFilter, summary, isDetailOpen, onClose, onSelectLead }) => {
  const meta = VIEW_META[view];
  const services = summary.services;
  const dataAsOf = summary.data_as_of;
  const thresholdPct = Math.round(summary.threshold * 100);
  const panelRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState(initialFilter ?? "");
  const [serviceFilter, setServiceFilter] = useState("");
  const [sort, setSort] = useState("lead_id");
  const [order, setOrder] = useState<"asc" | "desc">("asc");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<LeadsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useFocusTrap(panelRef, !isDetailOpen, onClose);

  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery((current) => {
        if (current !== searchInput) setPage(1);
        return searchInput;
      });
    }, 180);
    return () => clearTimeout(timer);
  }, [searchInput]);

  const load = useCallback(
    async (signal: AbortSignal) => {
      const top = scrollRef.current?.scrollTop ?? 0;
      const left = scrollRef.current?.scrollLeft ?? 0;
      try {
        const params = new URLSearchParams({ view, sort, order, page: String(page), page_size: String(PAGE_SIZE) });
        if (query.trim()) params.set("q", query.trim());
        if (filter) params.set("filter", filter);
        if (serviceFilter) params.set("service", serviceFilter);
        const resp = await fetch(apiPath(`/leads?${params.toString()}`), { signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const json = (await resp.json()) as LeadsResponse;
        setData(json);
        setError(false);
        requestAnimationFrame(() => {
          if (scrollRef.current) {
            scrollRef.current.scrollTop = top;
            scrollRef.current.scrollLeft = left;
          }
        });
      } catch (err) {
        if ((err as Error).name !== "AbortError") setError(true);
      } finally {
        if (!signal.aborted) setLoading(false);
      }
    },
    [view, sort, order, page, query, filter, serviceFilter],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load, dataAsOf]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  useEffect(() => {
    if (data && page > totalPages) setPage(totalPages);
  }, [data, page, totalPages]);

  const toggleSort = (key: string) => {
    if (sort === key) setOrder((current) => (current === "asc" ? "desc" : "asc"));
    else {
      setSort(key);
      setOrder(key === "best_probability_pct" ? "desc" : "asc");
    }
    setPage(1);
  };

  const filters = useMemo(() => filtersFor(view, summary, data?.filter_counts), [view, summary, data?.filter_counts]);
  const pageIds = useMemo(() => (data?.rows ?? []).map((row) => String(row.lead_id ?? "")), [data]);

  return (
    <div className="backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={panelRef} className="view-modal" role="dialog" aria-modal="true" aria-labelledby="view-title" tabIndex={-1}>
        <header className="view-head">
          <span className="view-icon">{meta.icon}</span>
          <div className="view-titles">
            <h2 id="view-title">{meta.title}</h2>
            <p>{meta.description}</p>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label={`Close ${meta.title}`}>
            <CloseIcon />
          </button>
        </header>

        <div className="view-controls">
          <div className="search-wrap">
            <SearchIcon size={14} className="search-lens" />
            <input
              type="search"
              className="search-input"
              placeholder="Search business name, phone, notes…"
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
              aria-label="Search leads"
            />
          </div>

          {filters.length > 1 && (
            <div className="filter-chips" role="tablist" aria-label="Filter leads">
              {filters.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  role="tab"
                  className={`filter-chip ${filter === opt.value ? "is-active" : ""}`}
                  aria-selected={filter === opt.value}
                  onClick={() => {
                    setFilter(opt.value);
                    setPage(1);
                  }}
                >
                  {opt.label}
                  <span className="filter-chip-count">{opt.count.toLocaleString()}</span>
                </button>
              ))}
            </div>
          )}

          {view === "qualified" && services.length > 1 && (
            <div className="filter-chips" role="group" aria-label="Filter by service">
              <button
                type="button"
                className={`filter-chip ${serviceFilter === "" ? "is-active" : ""}`}
                onClick={() => {
                  setServiceFilter("");
                  setPage(1);
                }}
              >
                All services
              </button>
              {services.map((svc) => (
                <button
                  key={svc.id}
                  type="button"
                  className={`filter-chip ${serviceFilter === svc.id ? "is-active" : ""}`}
                  onClick={() => {
                    setServiceFilter(svc.id);
                    setPage(1);
                  }}
                >
                  <span className="service-dot" style={{ background: serviceColor(services, svc.id) }} />
                  {svc.name}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="table-container" ref={scrollRef}>
          {loading && !data && <div className="table-loading">Loading leads…</div>}
          {error && <div className="table-error">Could not load leads for this view. Please try again.</div>}
          {data && data.rows.length === 0 && (
            <div className="table-empty">
              <strong>No matching leads</strong>
              <span>Try clearing search or filters to see more leads.</span>
            </div>
          )}
          {data && data.rows.length > 0 && (
            <table className="leads-table">
              <thead>
                <tr>
                  {data.columns.map((column) => {
                    const sorted = sort === column.key;
                    return (
                      <th key={column.key} aria-sort={sorted ? (order === "asc" ? "ascending" : "descending") : "none"}>
                        <button type="button" onClick={() => toggleSort(column.key)}>
                          {column.label}
                          {sorted && (order === "asc" ? " ▲" : " ▼")}
                        </button>
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row) => {
                  const id = String(row.lead_id ?? "");
                  return (
                    <tr
                      key={id}
                      tabIndex={0}
                      onClick={() => onSelectLead({ id, list: pageIds })}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onSelectLead({ id, list: pageIds });
                        }
                      }}
                    >
                      {data.columns.map((column) => (
                        <td key={column.key}>
                          <Cell
                            column={column}
                            row={row}
                            services={services}
                            thresholdPct={thresholdPct}
                            onOpenCall={() => onSelectLead({ id, list: pageIds, section: "call" })}
                          />
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {data && totalPages > 1 && (
          <footer className="table-footer">
            <span className="footer-count">
              Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, data.total)} of {data.total.toLocaleString()} leads
            </span>
            <div className="pagination">
              <button
                type="button"
                className="icon-btn"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                aria-label="Previous page"
              >
                <ChevronLeftIcon size={14} />
              </button>
              <span>
                Page {page} of {totalPages}
              </span>
              <button
                type="button"
                className="icon-btn"
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                aria-label="Next page"
              >
                <ChevronRightIcon size={14} />
              </button>
            </div>
          </footer>
        )}
      </div>
    </div>
  );
};
