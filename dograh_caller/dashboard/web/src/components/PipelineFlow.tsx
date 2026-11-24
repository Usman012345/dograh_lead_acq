import React, { useLayoutEffect, useMemo, useRef, useState } from "react";
import { OpenViewRequest, SummaryResponse, ViewName } from "../types";
import { percent, serviceColor } from "../lib/format";
import { AnimatedNumber } from "./AnimatedNumber";
import { AttentionCard } from "./AttentionCard";
import { FunnelCard } from "./FunnelCard";
import {
  BadgeCheckIcon,
  ClipboardIcon,
  HeadsetIcon,
  InboxIcon,
  MinusCircleIcon,
  ShieldPhoneIcon,
  SparkIcon,
} from "./Icons";

type Tone = "flow" | "ai" | "good" | "exit";

interface Chip {
  label: string;
  value: number;
  title?: string;
  color?: string;
  status?: "warn" | "bad" | "wait";
}

interface NodeSpec {
  id: ViewName;
  area: string;
  step: string;
  title: string;
  desc: string;
  icon: React.ReactNode;
  tone: Tone;
  value: number;
  valueLabel: string;
  share?: { value: number; of: string };
  chips: Chip[];
  active: boolean;
}

interface EdgeSpec {
  id: string;
  from: ViewName;
  to: ViewName;
  direction: "right" | "down";
  label?: string;
  tone: "flow" | "good" | "exit";
  active: boolean;
}

interface EdgeGeometry extends EdgeSpec {
  path: string;
  labelX: number;
  labelY: number;
}

interface Props {
  summary: SummaryResponse | null;
  onOpen: (request: OpenViewRequest) => void;
}

export const PipelineFlow: React.FC<Props> = ({ summary, onOpen }) => {
  const gridRef = useRef<HTMLDivElement>(null);
  const nodeRefs = useRef<Partial<Record<ViewName, HTMLButtonElement | null>>>({});
  const [geometry, setGeometry] = useState<{ edges: EdgeGeometry[] }>({ edges: [] });

  const thresholdPct = Math.round((summary?.threshold ?? 0.8) * 100);
  const n = summary?.nodes;
  const activity = summary?.activity;
  const services = summary?.services ?? [];
  const reducedMotion =
    typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

  const nodes: NodeSpec[] = useMemo(() => {
    const incoming = n?.incoming.total ?? 0;
    const v = n?.number_verifier ?? { passed: 0, rejected: 0, errors: 0, waiting: 0 };
    const d = n?.decision_maker ?? { reviewed: 0, errors: 0, waiting: 0 };
    const nq = n?.non_qualified.total ?? 0;
    const q = n?.qualified ?? { total: 0, by_service: {} as Record<string, number> };
    const ca = n?.calling_agent ?? { waiting: 0, calling: 0, not_sent: 0, not_called: 0, interrupted: 0 };
    const cr = n?.call_results ?? { total: 0, called: 0, failed: 0, timeout: 0 };

    const compact = (chips: Chip[]) => chips.filter((chip) => chip.value > 0);

    return [
      {
        id: "incoming",
        area: "incoming",
        step: "Step 1",
        title: "Incoming Leads",
        desc: "Every lead we receive",
        icon: <InboxIcon />,
        tone: "flow",
        value: incoming,
        valueLabel: "received",
        chips: [],
        active: (activity?.verifying ?? 0) > 0,
      },
      {
        id: "number_verifier",
        area: "verifier",
        step: "Step 2",
        title: "Number Verifier",
        desc: "Confirms a callable US number",
        icon: <ShieldPhoneIcon />,
        tone: "flow",
        value: v.passed,
        valueLabel: "valid",
        share: { value: percent(v.passed, incoming), of: "of leads" },
        chips: compact([
          { label: "rejected", value: v.rejected, status: "warn" },
          { label: "errors", value: v.errors, status: "bad" },
          { label: "waiting", value: v.waiting, status: "wait" },
        ]),
        active: (activity?.verifying ?? 0) > 0,
      },
      {
        id: "decision_maker",
        area: "decision",
        step: "Step 3 · AI",
        title: "Decision Maker",
        desc: "Scores fit for every service",
        icon: <SparkIcon />,
        tone: "ai",
        value: d.reviewed,
        valueLabel: "reviewed",
        share: { value: percent(d.reviewed, v.passed), of: "of valid" },
        chips: compact([
          { label: "to retry", value: d.errors, status: "bad" },
          { label: "waiting", value: d.waiting, status: "wait" },
        ]),
        active: (activity?.deciding ?? 0) > 0,
      },
      {
        id: "qualified",
        area: "qualified",
        step: `${thresholdPct}% or higher`,
        title: "Qualified Leads",
        desc: "Matched to the best service",
        icon: <BadgeCheckIcon />,
        tone: "good",
        value: q.total,
        valueLabel: "qualified",
        share: { value: percent(q.total, d.reviewed), of: "of reviewed" },
        chips: services.map((s) => ({
          label: s.name.split(" ")[0],
          title: s.name,
          value: q.by_service[s.id] ?? 0,
          color: serviceColor(services, s.id),
        })),
        active: false,
      },
      {
        id: "non_qualified",
        area: "nonq",
        step: `Below ${thresholdPct}%`,
        title: "Non-Qualified Leads",
        desc: "Not a strong enough fit",
        icon: <MinusCircleIcon />,
        tone: "exit",
        value: nq,
        valueLabel: "not called",
        share: { value: percent(nq, d.reviewed), of: "of reviewed" },
        chips: [],
        active: false,
      },
      {
        id: "calling_agent",
        area: "calling",
        step: "Step 4",
        title: "Calling Agent",
        desc: "AI agent places the calls",
        icon: <HeadsetIcon />,
        tone: "flow",
        value: ca.waiting + ca.calling,
        valueLabel: "in progress",
        chips: compact([
          { label: "on calls", value: ca.calling, status: "wait" },
          { label: "queued", value: ca.waiting, status: "wait" },
          { label: "not sent", value: ca.not_sent, status: "warn" },
        ]),
        active: (activity?.calling ?? 0) + (activity?.queued ?? 0) > 0,
      },
      {
        id: "call_results",
        area: "results",
        step: "Step 5",
        title: "Call Results",
        desc: "Outcomes, audio, transcripts",
        icon: <ClipboardIcon />,
        tone: "flow",
        value: cr.total,
        valueLabel: "finished",
        share: { value: percent(cr.total, q.total), of: "of qualified" },
        chips: compact([
          { label: "completed", value: cr.called },
          { label: "failed", value: cr.failed, status: "bad" },
          { label: "timed out", value: cr.timeout, status: "warn" },
        ]),
        active: false,
      },
    ];
  }, [n, activity, services, thresholdPct]);

  const edges: EdgeSpec[] = useMemo(
    () => [
      { id: "in-ver", from: "incoming", to: "number_verifier", direction: "right", tone: "flow", active: (activity?.verifying ?? 0) > 0 },
      { id: "ver-dec", from: "number_verifier", to: "decision_maker", direction: "right", tone: "flow", active: (activity?.deciding ?? 0) > 0 },
      { id: "dec-q", from: "decision_maker", to: "qualified", direction: "right", label: `${thresholdPct}%+`, tone: "good", active: (activity?.deciding ?? 0) > 0 },
      { id: "dec-nq", from: "decision_maker", to: "non_qualified", direction: "down", label: `<${thresholdPct}%`, tone: "exit", active: false },
      { id: "q-call", from: "qualified", to: "calling_agent", direction: "down", tone: "good", active: (activity?.queued ?? 0) > 0 },
      { id: "call-res", from: "calling_agent", to: "call_results", direction: "down", tone: "flow", active: (activity?.calling ?? 0) > 0 },
    ],
    [activity, thresholdPct],
  );

  useLayoutEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;

    const measure = () => {
      const box = grid.getBoundingClientRect();
      const laidOut: EdgeGeometry[] = [];
      for (const edge of edges) {
        const from = nodeRefs.current[edge.from]?.getBoundingClientRect();
        const to = nodeRefs.current[edge.to]?.getBoundingClientRect();
        if (!from || !to) continue;
        const inset = 3;
        const [x1, y1, x2, y2] =
          edge.direction === "right"
            ? [from.right - box.left + inset, from.top + from.height / 2 - box.top, to.left - box.left - inset - 5, to.top + to.height / 2 - box.top]
            : [from.left + from.width / 2 - box.left, from.bottom - box.top + inset, to.left + to.width / 2 - box.left, to.top - box.top - inset - 5];
        let path = `M ${x1} ${y1} L ${x2} ${y2}`;
        const labelX = (x1 + x2) / 2;
        const labelY = (y1 + y2) / 2;
        if (edge.direction === "down" && Math.abs(x1 - x2) > 4) {
          const midY = (y1 + y2) / 2;
          path = `M ${x1} ${y1} L ${x1} ${midY} L ${x2} ${midY} L ${x2} ${y2}`;
        }
        laidOut.push({
          ...edge,
          path,
          labelX,
          labelY,
        });
      }
      setGeometry({ edges: laidOut });
    };

    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(grid);
    Object.values(nodeRefs.current).forEach((el) => el && observer.observe(el));
    return () => observer.disconnect();
  }, [edges]);

  return (
    <div className="flow-grid" ref={gridRef}>
      <svg className="flow-lines" aria-hidden="true">
        <defs>
          {(["flow", "good", "exit"] as const).map((tone) => (
            <marker key={tone} id={`arrow-${tone}`} viewBox="0 0 10 10" refX="1" refY="5" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M 0 0 L 10 5 L 0 10 Z" className={`flow-arrow tone-${tone}`} />
            </marker>
          ))}
        </defs>
        {geometry.edges.map((edge) => (
          <g key={edge.id}>
            <path d={edge.path} className={`flow-line tone-${edge.tone} ${edge.active ? "is-active" : ""}`} markerEnd={`url(#arrow-${edge.tone})`} />
            {edge.active && !reducedMotion && (
              <circle r="3.5" className={`flow-pulse tone-${edge.tone}`}>
                <animateMotion dur="1.5s" repeatCount="indefinite" path={edge.path} />
              </circle>
            )}
          </g>
        ))}
      </svg>
      {geometry.edges
        .filter((edge) => edge.label)
        .map((edge) => (
          <span
            key={edge.id}
            className={`flow-label tone-${edge.tone} dir-${edge.direction}`}
            style={{ left: edge.labelX, top: edge.labelY }}
          >
            {edge.label}
          </span>
        ))}

      {nodes.map((node) => (
        <button
          key={node.id}
          ref={(el) => {
            nodeRefs.current[node.id] = el;
          }}
          type="button"
          className={`flow-node tone-${node.tone} ${node.active ? "is-active" : ""}`}
          style={{ gridArea: node.area }}
          onClick={() => onOpen({ view: node.id })}
          aria-label={`${node.title}: ${node.value} ${node.valueLabel}. Open list.`}
        >
          <span className="node-head">
            <span className="node-icon">{node.icon}</span>
            <span className="node-names">
              <span className="node-step">{node.step}</span>
              <span className="node-title">{node.title}</span>
            </span>
          </span>
          <span className="node-desc">{node.desc}</span>
          <span className="node-metric">
            <span className="node-value">
              <AnimatedNumber value={node.value} />
            </span>
            <span className="node-value-label">{node.valueLabel}</span>
            {node.share && (
              <span className="node-share">
                {node.share.value}% {node.share.of}
              </span>
            )}
          </span>
          <span className="node-chips">
            {node.chips.map((chip) => (
              <span key={chip.label} className="node-chip" title={chip.title}>
                <span
                  className={`chip-dot ${chip.status ? `status-${chip.status}` : ""}`}
                  style={chip.color ? { background: chip.color } : undefined}
                />
                <strong>{chip.value}</strong> {chip.label}
              </span>
            ))}
          </span>
        </button>
      ))}

      <div className="flow-funnel" style={{ gridArea: "funnel" }}>
        <FunnelCard summary={summary} onOpen={onOpen} />
      </div>
      <div className="flow-attention" style={{ gridArea: "attention" }}>
        <AttentionCard summary={summary} onOpen={onOpen} />
      </div>
    </div>
  );
};
