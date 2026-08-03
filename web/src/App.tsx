import { useCallback, useEffect, useRef, useState } from "react";
import { Card } from "./cards";
import { streamEvents } from "./sse";
import type {
  ActionButton,
  CardReadyPayload,
  EngineErrorPayload,
  ReplyReadyPayload,
  ScenarioManifest,
  TracePayload,
} from "./types.gen";

type Source = "replay" | "live";

interface Turn {
  source: Source;
  utterance: string;
  understood: string;
  language: string;
  card?: CardReadyPayload;
  reply?: ReplyReadyPayload;
  failure?: EngineErrorPayload;
  traces: TracePayload[];
  /** Measured in this browser, from the moment the request was issued. */
  firstTextMs?: number;
  answerMs?: number;
}

const EMPTY: Turn = {
  source: "replay",
  utterance: "",
  understood: "",
  language: "en",
  traces: [],
};

export default function App() {
  const [manifest, setManifest] = useState<ScenarioManifest | null>(null);
  const [health, setHealth] = useState<{ has_key: boolean; model: string; effort: string } | null>(
    null,
  );
  const [turn, setTurn] = useState<Turn>(EMPTY);
  const [running, setRunning] = useState(false);
  const [input, setInput] = useState("");
  const [banner, setBanner] = useState<string | null>(null);
  const cancelRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    fetch("/api/scenario").then((r) => r.json()).then(setManifest).catch(() => setBanner(
      "Cannot reach the API. Start it with `uvicorn app.main:app --reload`.",
    ));
    fetch("/health").then((r) => r.json()).then(setHealth).catch(() => {});
  }, []);

  const start = useCallback((source: Source, utterance: string, url: string, body?: unknown) => {
    cancelRef.current?.();
    setBanner(null);
    setRunning(true);
    setTurn({ ...EMPTY, source, utterance });

    const began = performance.now();
    const since = () => Math.round(performance.now() - began);

    const handle = streamEvents(
      url,
      body,
      (event) => {
        setTurn((prev) => {
          switch (event.name) {
            case "understood_delta":
              return {
                ...prev,
                understood: prev.understood + event.data.text,
                firstTextMs: prev.firstTextMs ?? since(),
              };
            case "understood":
              return { ...prev, understood: event.data.text, language: event.data.language };
            case "card":
              return { ...prev, card: event.data, answerMs: prev.answerMs ?? since() };
            case "reply":
              return { ...prev, reply: event.data, answerMs: prev.answerMs ?? since() };
            case "error":
              return { ...prev, failure: event.data };
            case "trace":
              return { ...prev, traces: [...prev.traces, event.data] };
            default:
              return prev;
          }
        });
      },
      (error) => {
        setRunning(false);
        if (error) setBanner(error.message);
      },
    );
    cancelRef.current = handle.cancel;
  }, []);

  const runDemo = (id: string, utterance: string) =>
    start("replay", utterance, `/api/replay/${id}`);

  const runLive = () => {
    const utterance = input.trim();
    if (!utterance) return;
    start("live", utterance, "/api/converse", { utterance });
  };

  const onAction = (action: ActionButton) => {
    if (action.id === "retry" && turn.utterance) {
      if (turn.source === "live") start("live", turn.utterance, "/api/converse", {
        utterance: turn.utterance,
      });
    }
  };

  return (
    <div className="shell">
      <header className="masthead">
        <div>
          <h1>voice&#8203;-to&#8203;-cards</h1>
          <p className="sub">
            {manifest ? manifest.name : "loading…"}
            {manifest && (
              <span className="chips">
                {manifest.tools.length} tools &middot; {manifest.languages.join(" / ")}
              </span>
            )}
          </p>
        </div>
        {health && (
          <p className="config">
            <code>{health.model}</code> effort <code>{health.effort}</code>
            <span className={health.has_key ? "ok" : "warn"}>
              {health.has_key ? "key configured" : "no key — canned demos only"}
            </span>
          </p>
        )}
      </header>

      <p className="caveat">
        Timings below are wall-clock in this browser, from request to first readable
        text. They are not a benchmark of the model, and the streaming design has
        not been measured against a live key yet.
      </p>

      <section className="demos">
        {manifest?.demos.map((demo) => (
          <button
            key={demo.id}
            className="demo"
            disabled={running}
            onClick={() => runDemo(demo.id, demo.utterance)}
            title={demo.utterance}
          >
            {demo.label}
          </button>
        ))}
      </section>

      <div className="columns">
        <main className="stage">
          {turn.utterance && (
            <p className="utterance">
              <span className="who">{turn.source === "replay" ? "canned" : "you"}</span>
              {turn.utterance}
            </p>
          )}

          {turn.understood && (
            <p className="understood" lang={turn.language}>
              {turn.understood}
              {running && !turn.card && !turn.reply && <span className="caret" />}
            </p>
          )}

          {turn.firstTextMs !== undefined && (
            <p className="timing">
              first readable text at <strong>{turn.firstTextMs} ms</strong>
              {turn.answerMs !== undefined && (
                <>
                  {" "}
                  &middot; full answer at <strong>{turn.answerMs} ms</strong>
                </>
              )}
              {turn.source === "replay" && <em> (canned, no model call)</em>}
            </p>
          )}

          {turn.card && (
            <Card
              layout={turn.card.layout}
              data={turn.card.data}
              spec={manifest?.cards[turn.card.card]}
              actions={turn.card.actions}
              onAction={onAction}
            />
          )}

          {turn.reply && (
            <article className="card prose">
              <p>{turn.reply.text}</p>
              {turn.reply.sources.length > 0 && (
                <p className="sources">
                  {turn.reply.sources.map((s) => (
                    <a key={s} href={s} rel="noreferrer noopener" target="_blank">
                      {s}
                    </a>
                  ))}
                </p>
              )}
            </article>
          )}

          {turn.failure && (
            <article className="card notice">
              <p>{turn.failure.message}</p>
              <div className="actions">
                {turn.failure.actions.map((a) => (
                  <button key={a.id} className={`btn ${a.variant}`} onClick={() => onAction(a)}>
                    {a.label}
                  </button>
                ))}
              </div>
            </article>
          )}

          {banner && <p className="banner">{banner}</p>}

          <form
            className="composer"
            onSubmit={(e) => {
              e.preventDefault();
              runLive();
            }}
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                health?.has_key
                  ? "Say something the way a caller would…"
                  : "Free-form input needs an API key; try a canned demo above"
              }
              disabled={running}
            />
            <button className="btn primary" disabled={running || !input.trim()}>
              Send
            </button>
          </form>
        </main>

        <aside className="trace">
          <h2>Trace</h2>
          {turn.traces.length === 0 && <p className="hint">Run something to watch it decide.</p>}
          <ol>
            {turn.traces.map((t, i) => (
              <li key={i}>
                <span className="t">{t.t_ms}ms</span>
                <span className="stage-name">{t.stage}</span>
                <TraceDetail detail={t.detail} />
              </li>
            ))}
          </ol>
        </aside>
      </div>
    </div>
  );
}

function TraceDetail({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail).filter(([, v]) => v !== null && v !== "");
  if (entries.length === 0) return null;
  return (
    <span className="detail">
      {entries.map(([key, value]) => (
        <span key={key}>
          {key}=
          {typeof value === "object" ? JSON.stringify(value) : String(value)}
        </span>
      ))}
    </span>
  );
}
