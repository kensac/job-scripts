export function createRecipeValues(context, list) {
  // ---- values --------------------------------------------------------------
  // A `values` map in the table names answers by code and lists the option
  // texts each code may appear as; our text picks the code whose texts it
  // matches, and those texts are tried in turn. A map named by a string
  // (a country or state abbreviation table) is not carried, so the value
  // stands as it is.
  function mapValues(values, ours) {
    if (!values || typeof values !== "object" || !ours) return [ours];
    const low = ours.toLowerCase();
    for (const texts of Object.values(values)) {
      const options = list(texts).map(String);
      if (options.some((t) => t.toLowerCase() === low || low.startsWith(t.toLowerCase()) || t.toLowerCase().startsWith(low))) {
        return [...new Set([ours, ...options])];
      }
    }
    return [ours];
  }
  const YES = /^(yes|true|y)\b/i;
  const NO = /^(no|false|n)\b/i;
  // A `valuePathMap` names a path per value: the radio for "African
  // American", the box for "true". Ours picks the key it means.
  function mapPath(valuePathMap, ours) {
    const low = String(ours ?? "").toLowerCase();
    const keys = Object.keys(valuePathMap);
    const key =
      keys.find((k) => k.toLowerCase() === low) ||
      keys.find((k) => low && (low.startsWith(k.toLowerCase()) || k.toLowerCase().startsWith(low))) ||
      (YES.test(low) ? keys.find((k) => /^(true|yes)$/i.test(k)) : null) ||
      (NO.test(low) ? keys.find((k) => /^(false|no)$/i.test(k)) : null);
    return key ? valuePathMap[key] : null;
  }
  // The value an action or variant asks for by name: a group entry's own
  // attribute inside a group, a date for today's-date names, otherwise a
  // scalar the profile carries under that name.
  function factValue(key, ctx) {
    if (ctx.item) {
      const v = entryValue(key, ctx.item, ctx.fact);
      if (v !== null && v !== "") return v;
    }
    if (/^(current_date|disability_date|today)/.test(key)) return formatDate(key, todayParts());
    const p = context.profile;
    return typeof p[key] === "string" || typeof p[key] === "number" ? String(p[key]) : null;
  }
  // The candidates an action or variant fills with: its own constant, the
  // fact it names, or the field's value, through its own values map.
  function candidates(spec, ctx, value) {
    let v = value;
    if (spec.valueKey) v = factValue(spec.valueKey, ctx) ?? v;
    if (spec.value !== undefined) v = spec.value === true ? "true" : spec.value === false ? "false" : spec.value;
    if (Array.isArray(v)) return v.map(String).filter(Boolean);
    const s = v == null ? "" : String(v);
    return s ? mapValues(spec.values, s) : [""];
  }

  // ---- dates and entry values for repeated groups --------------------------
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  function parseDate(s) {
    if (!s) return null;
    const t = String(s).trim();
    let m = t.match(/^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?/);
    if (m) return { y: +m[1], m: +m[2], d: +(m[3] || 1) };
    m = t.match(/^(\d{1,2})\/(?:(\d{1,2})\/)?(\d{4})$/);
    if (m) return { y: +m[3], m: +m[1], d: +(m[2] || 1) };
    m = t.match(/^([A-Za-z]{3,9})\.?\s+(\d{4})$/);
    if (m) {
      const idx = MONTHS.findIndex((n) => n.toLowerCase().startsWith(m[1].slice(0, 3).toLowerCase()));
      if (idx >= 0) return { y: +m[2], m: idx + 1, d: 1 };
    }
    m = t.match(/^(\d{4})$/);
    if (m) return { y: +m[1], m: 1, d: 1 };
    return null;
  }
  const todayParts = () => {
    const n = new Date();
    return { y: n.getFullYear(), m: n.getMonth() + 1, d: n.getDate() };
  };
  const pad = (n) => String(n).padStart(2, "0");
  // The table names a date field by the format it takes; a name with no
  // format takes a month and year for a start or end, a full date for
  // today's date or a birthday.
  function formatDate(name, d) {
    if (!d) return null;
    if (/month_text/.test(name)) return MONTHS[d.m - 1];
    if (/month_abbr/.test(name)) return MONTHS[d.m - 1].slice(0, 3);
    if (/_month$/.test(name)) return `${d.m} | ${pad(d.m)} | ${MONTHS[d.m - 1]}`;
    if (/_(year|YYYY)$/.test(name)) return String(d.y);
    if (/_(day|D)$/.test(name)) return String(d.d);
    if (/_DD$/.test(name)) return pad(d.d);
    if (/_M$/.test(name)) return String(d.m);
    if (/slashes_MMDDYYYY|_MMDDYYYY$/.test(name)) return `${pad(d.m)}/${pad(d.d)}/${d.y}`;
    if (/slashes_MDDYY/.test(name)) return `${d.m}/${pad(d.d)}/${String(d.y).slice(-2)}`;
    if (/slashes_MDYYYY/.test(name)) return `${d.m}/${d.d}/${d.y}`;
    if (/slashes_MMYYYY|_MMYYYY$/.test(name)) return `${pad(d.m)}/${d.y}`;
    if (/dashes_YYYYMMDD/.test(name)) return `${d.y}-${pad(d.m)}-${pad(d.d)}`;
    if (/dashes_YYYYMM/.test(name)) return `${d.y}-${pad(d.m)}`;
    if (/dashes_MMMYYYY/.test(name)) return `${MONTHS[d.m - 1].slice(0, 3)}-${d.y}`;
    if (/dashes_MMYYYY/.test(name)) return `${pad(d.m)}-${d.y}`;
    if (/month_DD_comma_YYYY/.test(name)) return `${MONTHS[d.m - 1]} ${pad(d.d)}, ${d.y}`;
    if (/_MM$/.test(name)) return pad(d.m);
    if (/^(current_date|disability_date|today|birthday)/.test(name)) return `${pad(d.m)}/${pad(d.d)}/${d.y}`;
    return `${pad(d.m)}/${d.y}`;
  }
  // What a nested field takes from one profile row, by the table's name for
  // it. null means the field is not ours to fill for this row.
  function entryValue(name, item, fact) {
    const start = parseDate(item.start);
    const end = parseDate(item.end);
    const now = new Date();
    if (/^(start|begin)/.test(name)) return formatDate(name, start);
    if (/^(grad|end|actual_completion)/.test(name)) return item.current ? null : formatDate(name, end);
    switch (name) {
      case "education":
      case "school":
      case "name":
        return fact === "education" ? item.school : item.company;
      case "major":
      case "field":
        return item.field || null;
      case "degree":
        return item.degree || null;
      case "gpa":
        return item.gpa || null;
      case "graduated":
      case "did_graduate":
      case "qualification_complete":
        return end && new Date(end.y, end.m - 1, 1) <= now ? "Yes" : "No";
      case "title":
        return item.title || null;
      case "company":
      case "employer":
        return item.company || null;
      case "location":
      case "city_state":
      case "city_state_full":
        return item.location || null;
      case "city":
        return item.location ? item.location.split(",")[0].trim() : null;
      case "state":
        return item.location && item.location.includes(",") ? item.location.split(",")[1].trim() : null;
      case "country":
        return item.location && item.location.split(",").length > 2 ? item.location.split(",").pop().trim() : null;
      case "description":
        return item.description || null;
      case "description_stripped":
        return item.description ? item.description.replace(/\s+/g, " ").trim() : null;
      case "currently_working":
        return item.current ? "Yes" : null;
      case "save":
      case "done":
      case "delay":
        return "";
      default:
        return null;
    }
  }

  return { mapPath, candidates, parseDate, todayParts, formatDate, entryValue };
}
