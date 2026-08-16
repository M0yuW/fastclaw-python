type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function compact(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) return JSON.stringify(value);
  return "{" + Object.entries(value as JsonObject).map(([key, item]) => `${key}: ${compact(item)}`).join(", ") + "}";
}

/** Render a safe one-line tool-call summary without coercing nested objects. */
export function formatToolSummary(argumentsText: string, toolName = ""): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(argumentsText);
  } catch {
    return argumentsText;
  }
  if (!isObject(parsed)) return compact(parsed);

  const entry = isObject(parsed.entry) ? parsed.entry : parsed;
  if (toolName === "football_ledger" || parsed.operation === "append" || parsed.operation === "update" || parsed.operation === "settle") {
    const operation = typeof parsed.operation === "string" ? parsed.operation : "ledger";
    const competition = entry.competition ?? parsed.competition;
    const match = entry.match ?? parsed.match;
    const date = entry.date ?? parsed.date;
    const parts = [operation, competition, match, date].filter((value) => value !== undefined && value !== null && value !== "");
    return parts.map(compact).join(" · ");
  }

  return Object.entries(parsed)
    .map(([key, value]) => `${key}: ${compact(value)}`)
    .join(", ");
}
