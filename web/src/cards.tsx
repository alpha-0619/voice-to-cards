/**
 * The six card shapes, plus the two the engine sets itself.
 *
 * There is no component per industry here, and that is the constraint the
 * whole design rests on: a pack picks a shape from a closed vocabulary, so
 * adding an industry adds a directory and nothing in this file. A boarding
 * pass and a booked service visit are the same object, and they render through
 * the same component.
 *
 * Fields are read through the manifest rather than by hardcoded name. Two
 * consequences worth stating:
 *
 *   - a field the pack declares but the tool did not return is drawn as a
 *     visible gap, not skipped, because a missing arrival time should look
 *     missing;
 *   - a field the tool returned but no card declares is listed under
 *     "undeclared", because silently dropping it is how a renamed field goes
 *     unnoticed for a month.
 */

import type { ActionButton, CardData, CardSpec, Layout } from "./types.gen";

interface CardProps {
  layout: Layout;
  data: CardData;
  spec?: CardSpec;
  actions: ActionButton[];
  onAction: (action: ActionButton) => void;
}

const text = (data: CardData, key: string): string | undefined => {
  const value = data[key];
  if (value === undefined || value === null || value === "") return undefined;
  // Arrays are rendered as lists, never stringified: `String(["a","b"])` gives
  // "a,b", which looks like data rather than a formatting accident.
  if (Array.isArray(value)) return undefined;
  return typeof value === "string" ? value : String(value);
};

const list = (data: CardData, key: string): string[] =>
  Array.isArray(data[key]) ? (data[key] as unknown[]).map(String) : [];

const humanise = (key: string) =>
  key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

/** Declared fields, minus the ones a shape renders prominently itself. */
function supporting(spec: CardSpec | undefined, data: CardData, exclude: string[]): string[] {
  const declared = spec ? [...spec.fields, ...spec.optional_fields] : Object.keys(data);
  return declared.filter((f) => !exclude.includes(f));
}

/** Anything the tool returned that no card declares. */
function undeclared(spec: CardSpec | undefined, data: CardData): string[] {
  if (!spec) return [];
  const declared = new Set([...spec.fields, ...spec.optional_fields]);
  return Object.keys(data).filter((k) => !declared.has(k));
}

function FieldGrid({
  data,
  spec,
  exclude,
}: {
  data: CardData;
  spec?: CardSpec;
  exclude: string[];
}) {
  const keys = supporting(spec, data, exclude);
  const required = new Set(spec?.fields ?? []);
  if (keys.length === 0) return null;
  return (
    <dl className="grid">
      {keys.map((key) => {
        // Arrays first: `text()` deliberately returns undefined for them, so
        // checking emptiness before checking for a list would silently drop
        // every list-valued field.
        const items = list(data, key);
        const value = text(data, key);
        if (items.length === 0 && value === undefined && !required.has(key)) return null;
        return (
          <div key={key} className={items.length > 0 ? "cell wide" : "cell"}>
            <dt>{humanise(key)}</dt>
            {items.length > 0 ? (
              <dd>
                <ol className="steps">
                  {items.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ol>
              </dd>
            ) : (
              <dd className={value === undefined ? "absent" : undefined}>
                {value ?? "not provided"}
              </dd>
            )}
          </div>
        );
      })}
    </dl>
  );
}

function Undeclared({ data, spec }: { data: CardData; spec?: CardSpec }) {
  const extra = undeclared(spec, data);
  if (extra.length === 0) return null;
  return (
    <p className="undeclared">
      <strong>Undeclared:</strong> {extra.join(", ")}. The tool returned these and no card
      declares them, so nothing renders them. Usually a renamed field.
    </p>
  );
}

function Buttons({
  actions,
  onAction,
}: {
  actions: ActionButton[];
  onAction: (a: ActionButton) => void;
}) {
  if (actions.length === 0) return null;
  return (
    <div className="actions">
      {actions.map((action) => (
        <button key={action.id} className={`btn ${action.variant}`} onClick={() => onAction(action)}>
          {action.label}
        </button>
      ))}
    </div>
  );
}

export function Card({ layout, data, spec, actions, onAction }: CardProps) {
  switch (layout) {
    case "pass":
      return <Pass data={data} spec={spec} />;
    case "status":
      return <Status data={data} spec={spec} />;
    case "plan":
      return <Plan data={data} spec={spec} />;
    case "range":
      return <Range data={data} spec={spec} />;
    case "steps":
      return <Steps data={data} spec={spec} />;
    case "detail":
      return <Detail data={data} spec={spec} />;
    case "urgent":
      return <Urgent data={data} actions={actions} onAction={onAction} />;
    case "notice":
      return <Notice data={data} actions={actions} onAction={onAction} />;
    default: {
      // Exhaustiveness check. `layout` is a union generated from the engine, so
      // adding a shape without implementing it here is a compile error, not a
      // blank panel found later.
      const never: never = layout;
      return <p className="undeclared">Unrenderable layout: {String(never)}</p>;
    }
  }
}

/** A ticket-like artifact: headline entity, grid of facts, reference code. */
function Pass({ data, spec }: { data: CardData; spec?: CardSpec }) {
  const headline =
    text(data, "passenger") ?? text(data, "customer") ?? text(data, "service") ?? "";
  const route = text(data, "route") ?? text(data, "address") ?? "";
  const code = text(data, "barcode") ?? text(data, "job_ref") ?? "";
  return (
    <article className="card pass">
      <header>
        <span className="eyebrow">{text(data, "flight") ?? text(data, "service") ?? "Confirmed"}</span>
        <h2>{headline}</h2>
        {route && <p className="route">{route}</p>}
      </header>
      <FieldGrid data={data} spec={spec} exclude={["passenger", "customer", "route", "address", "barcode"]} />
      {code && (
        <footer className="code">
          <CodeStrip value={code} />
          <span>{code}</span>
        </footer>
      )}
      <Undeclared data={data} spec={spec} />
    </article>
  );
}

/** Something changed: the new state, and what it means for you. */
function Status({ data, spec }: { data: CardData; spec?: CardSpec }) {
  const status = text(data, "status") ?? "UPDATED";
  const scheduled = text(data, "scheduled");
  const estimated = text(data, "estimated");
  const eta = text(data, "eta_minutes");
  return (
    <article className="card status">
      <header>
        <span className={`badge ${status.toLowerCase().replace(/[^a-z]/g, "")}`}>{status}</span>
        <h2>{text(data, "flight") ?? text(data, "job_ref") ?? ""}</h2>
      </header>
      {(scheduled || estimated) && (
        <p className="shift">
          {scheduled && <span className="was">{scheduled}</span>}
          {scheduled && estimated && <span className="arrow">&rarr;</span>}
          {estimated && <span className="now">{estimated}</span>}
        </p>
      )}
      {eta && (
        <p className="shift">
          <span className="now">{eta} min</span> <span className="was">away</span>
        </p>
      )}
      {text(data, "reason") && <p className="reason">{text(data, "reason")}</p>}
      {text(data, "issue") && <p className="reason">{text(data, "issue")}</p>}
      <FieldGrid
        data={data}
        spec={spec}
        exclude={["status", "flight", "job_ref", "scheduled", "estimated", "reason", "issue", "eta_minutes"]}
      />
      <Undeclared data={data} spec={spec} />
    </article>
  );
}

/** A sequence against a deadline, with a risk level. */
function Plan({ data, spec }: { data: CardData; spec?: CardSpec }) {
  const risk = text(data, "risk") ?? "";
  const minutes = text(data, "minutes_to_connect");
  const steps = list(data, "steps");
  return (
    <article className="card plan">
      <header>
        <span className={`badge ${risk.toLowerCase()}`}>{risk || "PLAN"}</span>
        <h2>
          {text(data, "inbound")} <span className="arrow">&rarr;</span> {text(data, "onward")}
        </h2>
        {text(data, "destination") && <p className="route">to {text(data, "destination")}</p>}
      </header>
      {minutes && (
        <p className="countdown">
          <strong>{minutes}</strong> minutes to connect
        </p>
      )}
      <FieldGrid
        data={data}
        spec={spec}
        exclude={["risk", "inbound", "onward", "destination", "minutes_to_connect", "steps"]}
      />
      {steps.length > 0 && (
        <ol className="steps">
          {steps.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
      )}
      <Undeclared data={data} spec={spec} />
    </article>
  );
}

/** A band between two numbers, and what is included. */
function Range({ data, spec }: { data: CardData; spec?: CardSpec }) {
  const currency = text(data, "currency") ?? "";
  return (
    <article className="card range">
      <header>
        <span className="eyebrow">Estimate</span>
        <h2>{text(data, "service")}</h2>
      </header>
      <p className="band">
        <span className="figure">
          {currency} {text(data, "range_low")}
        </span>
        <span className="arrow">&ndash;</span>
        <span className="figure">
          {currency} {text(data, "range_high")}
        </span>
      </p>
      {text(data, "includes") && <p className="reason">{text(data, "includes")}</p>}
      {text(data, "note") && <p className="note">{text(data, "note")}</p>}
      <FieldGrid
        data={data}
        spec={spec}
        exclude={["service", "range_low", "range_high", "currency", "includes", "note"]}
      />
      <Undeclared data={data} spec={spec} />
    </article>
  );
}

/** An ordered list of instructions. */
function Steps({ data, spec }: { data: CardData; spec?: CardSpec }) {
  const steps = list(data, "steps");
  return (
    <article className="card steps-card">
      <header>
        <span className="eyebrow">
          {text(data, "walk_minutes") ? `${text(data, "walk_minutes")} min walk` : "Directions"}
        </span>
        <h2>{text(data, "destination")}</h2>
      </header>
      <ol className="steps">
        {steps.map((step, i) => (
          <li key={i}>{step}</li>
        ))}
      </ol>
      <FieldGrid data={data} spec={spec} exclude={["destination", "steps", "walk_minutes"]} />
      <Undeclared data={data} spec={spec} />
    </article>
  );
}

/** A prose summary plus supporting fields. */
function Detail({ data, spec }: { data: CardData; spec?: CardSpec }) {
  return (
    <article className="card detail">
      <header>
        <h2>{text(data, "title") ?? "Details"}</h2>
      </header>
      {text(data, "summary") && <p className="summary">{text(data, "summary")}</p>}
      <FieldGrid data={data} spec={spec} exclude={["title", "summary"]} />
      <Undeclared data={data} spec={spec} />
    </article>
  );
}

/** Engine-owned: a safety handover. Deliberately the loudest thing on screen. */
function Urgent({
  data,
  actions,
  onAction,
}: {
  data: CardData;
  actions: ActionButton[];
  onAction: (a: ActionButton) => void;
}) {
  return (
    <article className="card urgent" role="alert">
      <p className="urgent-message">{text(data, "message")}</p>
      <Buttons actions={actions} onAction={onAction} />
    </article>
  );
}

/** Engine-owned: a refusal or a recoverable failure. */
function Notice({
  data,
  actions,
  onAction,
}: {
  data: CardData;
  actions: ActionButton[];
  onAction: (a: ActionButton) => void;
}) {
  return (
    <article className="card notice">
      <p>{text(data, "message")}</p>
      <Buttons actions={actions} onAction={onAction} />
    </article>
  );
}

/**
 * A deterministic bar pattern derived from the reference.
 *
 * Explicitly decorative: it is not a scannable symbology and is not labelled as
 * one. The reference itself is printed underneath in text, which is the part
 * that carries the information. A fake barcode that looks scannable is worse
 * than no barcode, because someone will try to scan it.
 */
function CodeStrip({ value }: { value: string }) {
  const bars = Array.from({ length: 42 }, (_, i) => {
    const seed = value.charCodeAt(i % value.length) + i * 7;
    return (seed % 3) + 1;
  });
  return (
    <svg className="code-strip" viewBox={`0 0 ${bars.length * 3} 28`} aria-hidden="true">
      {bars.map((w, i) => (
        <rect key={i} x={i * 3} y="0" width={w * 0.6} height="28" />
      ))}
    </svg>
  );
}
