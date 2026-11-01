/**
 * An answer may list alternatives separated by "|" ("New York, NY | New
 * York"): a picker tries each in order, a multi-value widget takes them all.
 */
export function alternatives(value: unknown): string[] {
  return String(value ?? "")
    .split("|")
    .map((s) => s.trim())
    .filter(Boolean);
}
