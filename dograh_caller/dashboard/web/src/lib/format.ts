import { ServiceInfo } from "../types";

// Categorical slots in fixed order, validated against the dark card surface.
// A service keeps its color by its position in services.yaml, never by rank.
const SERVICE_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"];
const OTHER_SERVICE_COLOR = "#8b95a7";

export function serviceColor(services: ServiceInfo[], idOrName: string): string {
  const index = services.findIndex((s) => s.id === idOrName || s.name === idOrName);
  return index >= 0 && index < SERVICE_COLORS.length ? SERVICE_COLORS[index] : OTHER_SERVICE_COLOR;
}

export function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? singular : pluralForm}`;
}

export function percent(part: number, whole: number): number {
  return whole > 0 ? Math.round((part / whole) * 100) : 0;
}

function parseDate(value: string): Date | null {
  const text = value.trim();
  if (!text) return null;
  const date = new Date(text.includes("T") ? text : text.replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(value: unknown): string {
  if (typeof value !== "string") return "";
  const date = parseDate(value);
  if (!date) return value;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function formatClock(value: string | null): string {
  if (!value) return "";
  const date = parseDate(value);
  return date ? date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }) : "";
}

export function minutesSince(value: string | null): number | null {
  if (!value) return null;
  const date = parseDate(value);
  return date ? (Date.now() - date.getTime()) / 60000 : null;
}

export function transcriptOffset(at: string | null, start: string | null): string {
  if (!at || !start) return "";
  const a = parseDate(at);
  const b = parseDate(start);
  if (!a || !b) return "";
  const total = Math.max(0, Math.round((a.getTime() - b.getTime()) / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function formatPhone(value: unknown): string {
  const text = String(value ?? "").trim();
  const match = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(text);
  return match ? `(${match[1]}) ${match[2]}-${match[3]}` : text;
}

export function humanizeKey(key: string): string {
  if (key.includes(" > ")) return key;
  const spaced = key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (/[A-Z]/.test(key) && key.includes(" ")) return spaced;
  return spaced.replace(/\b\w/g, (c) => c.toUpperCase());
}

export function formatFieldValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value) && value.every((v) => typeof v !== "object")) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

export function isHttpLink(value: unknown): boolean {
  return typeof value === "string" && /^https?:\/\/\S+$/i.test(value.trim());
}
