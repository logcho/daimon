import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fmtDuration } from "../lib/format";
import type { ChatMessage, Step } from "../types";
import { useSpinnerFrame, useThinkingVerb } from "./ThinkingIndicator";
import { categoryParts, MAX_EXPANDED, StepSummary, summarise } from "./StepSummary";

/** A tool call. Shows what it was called *with*, not just its name — twelve
 *  `read_file` lines say nothing; twelve paths say what the agent read. */
function StepLine({ step, indented }: { step: Step; indented?: boolean }) {
  const name = step.tool ?? step.label;
  // Only worth showing once it's long enough to notice.
  const elapsed = step.elapsed_ms && step.elapsed_ms >= 100 ? fmtDuration(step.elapsed_ms / 1000) : null;

  const mark =
    step.status === "done" ? (
      <span className="text-emerald-400">✓</span>
    ) : step.status === "error" ? (
      <span className="text-red-400">✕</span>
    ) : step.status === "running" ? (
      <span className="block h-3 w-3 animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
    ) : (
      <span className="block h-2 w-2 rounded-full border border-neutral-600" />
    );

  return (
    <span className={`flex items-baseline gap-2 font-mono text-xs ${indented ? "ml-4" : ""}`}>
      <span className="shrink-0 self-center">{mark}</span>
      <span className={step.status === "running" ? "text-[#4f8dff]" : "text-neutral-300"}>{name}</span>
      {step.detail && <span className="truncate text-neutral-500">{step.detail}</span>}
      {elapsed && <span className="shrink-0 text-neutral-600">{elapsed}</span>}
    </span>
  );
}

/** The rows behind a disclosure, capped. Expanding a 200-step run should not
 *  lock the webview, and past a certain length nobody is reading anyway. */
function StepRows({ steps, indented }: { steps: Step[]; indented?: boolean }) {
  return (
    <ul className="space-y-1">
      {steps.slice(0, MAX_EXPANDED).map((step) => (
        <li key={step.id}>
          <StepLine step={step} indented={indented} />
        </li>
      ))}
      {steps.length > MAX_EXPANDED && (
        <li className={`font-mono text-xs text-neutral-600 ${indented ? "ml-4" : ""}`}>
          … {steps.length - MAX_EXPANDED} more
        </li>
      )}
    </ul>
  );
}

/** The collapsed line for a run of plain tool calls.
 *
 *  While the run is live it names the call happening *now* — that is the whole
 *  reason to show anything at all. Once it has moved on, the individual calls
 *  are detail and the shape of the run is the useful part, so it becomes the
 *  same coarse tally a finished turn gets. Failures are never folded away
 *  silently: a run with an error says so on its collapsed line. */
function RunLabel({ steps }: { steps: Step[] }) {
  const last = steps[steps.length - 1];
  const running = last.status === "running";
  const failed = steps.filter((s) => s.status === "error").length;
  // Never folded away silently, and not only when the run has stopped: a call
  // that failed three tools ago still matters while the next one is spinning.
  const failures = failed > 0 && (
    <span className="shrink-0 text-red-400">✕ {failed} failed</span>
  );

  if (running) {
    return (
      <span className="flex min-w-0 items-baseline gap-2">
        <span className="block h-3 w-3 shrink-0 self-center animate-spin rounded-full border-2 border-[#4f8dff]/25 border-t-[#4f8dff]" />
        <span className="shrink-0">{steps.length} tools ·</span>
        <span className="shrink-0 text-[#4f8dff]">{last.tool ?? last.label}</span>
        {last.detail && <span className="truncate text-neutral-500">{last.detail}</span>}
        {failures}
      </span>
    );
  }

  return (
    <span className="flex min-w-0 items-baseline gap-2">
      <span className="truncate">
        {[`${steps.length} tools`, ...categoryParts(steps)].join(" · ")}
      </span>
      {failures}
    </span>
  );
}

/** A consecutive run of plain tool calls, as one row.
 *
 *  A single call renders as itself — hiding one line behind a disclosure costs
 *  a click and saves nothing. */
function ToolRun({ steps, indented }: { steps: Step[]; indented?: boolean }) {
  if (steps.length === 1) return <StepLine step={steps[0]} indented={indented} />;
  return (
    <StepSummary label={<RunLabel steps={steps} />} indented={indented}>
      <StepRows steps={steps} indented />
    </StepSummary>
  );
}

/** One sub-agent's line for a finished spawn: what it was asked, how much work
 *  it did, how long it took — the same facts `render.subagent_line` shows in
 *  the CLI. Its own steps hang behind the disclosure. */
function subagentLabel(step: Step, children: Step[]): string {
  const parts = [step.agent_label ?? step.label];
  if (step.subagent_query ?? step.detail) parts.push(String(step.subagent_query ?? step.detail));
  const stats: string[] = [];
  if (children.length) stats.push(`${children.length} tool${children.length === 1 ? "" : "s"}`);
  if (step.elapsed_ms) stats.push(fmtDuration(step.elapsed_ms / 1000));
  return stats.length ? `${parts.join(": ")} · ${stats.join(" · ")}` : parts.join(": ");
}

type Group = { kind: "run"; steps: Step[] } | { kind: "agent"; step: Step };

/** Steps grouped so a turn reads as *what was delegated* rather than as every
 *  call it took to get there.
 *
 *  Sub-agents are the structure worth seeing: each spawn keeps its own block,
 *  grouped by `agent_id` (an earlier version matched on the literal string
 *  "research:", which never covered `task` spawns). Everything else — the
 *  parent's own reads, edits and shells — collapses into one row per
 *  consecutive run. Grouping in sequence rather than partitioning keeps the
 *  order honest: a run that happened after a spawn still renders after it.
 *
 *  A *finished* sub-agent collapses to one line whether or not the turn itself
 *  is done: once it has reported back, its children are detail, not progress.
 *  A running one shows a rolling row of what it is doing, because that is the
 *  only sign it is getting anywhere. */
function StepList({ steps }: { steps: Step[] }) {
  const topLevel = steps.filter((s) => s.parent_step_id === undefined);
  const childrenOf = (id: string) => steps.filter((s) => s.parent_step_id === id);

  const groups: Group[] = [];
  for (const step of topLevel) {
    if (step.agent_id) {
      groups.push({ kind: "agent", step });
      continue;
    }
    const last = groups[groups.length - 1];
    if (last?.kind === "run") last.steps.push(step);
    else groups.push({ kind: "run", steps: [step] });
  }

  return (
    <ul className="space-y-1">
      {groups.map((group) => {
        if (group.kind === "run") {
          return (
            <li key={`run-${group.steps[0].id}`}>
              <ToolRun steps={group.steps} />
            </li>
          );
        }
        const step = group.step;
        const children = step.agent_id ? childrenOf(step.agent_id) : [];
        if (children.length === 0) {
          return (
            <li key={step.id}>
              <StepLine step={step} />
            </li>
          );
        }
        return (
          <li key={step.id}>
            {step.status === "running" ? (
              <>
                <StepLine step={step} />
                <div className="mt-1">
                  <ToolRun steps={children} indented />
                </div>
              </>
            ) : (
              <StepSummary label={subagentLabel(step, children)}>
                <StepRows steps={children} indented />
              </StepSummary>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** The step the turn is actually inside right now, or nothing between calls.
 *
 *  The *last* running step, not the first: steps arrive in order and a
 *  sub-agent's own call always follows the spawn that owns it, so scanning
 *  backwards names what is really happening. Taking the first would say
 *  "task" for minutes on end while the interesting part — the file it is
 *  reading — went unsaid. */
function runningStep(steps: Step[]): Step | undefined {
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].status === "running") return steps[i];
  }
  return undefined;
}

/** Wall clock since the turn opened, ticking once a second.
 *
 *  `startedAt` has been recorded ever since the Thinking step arrives
 *  (`sessionEvents.ts`) and was only ever read *after* the turn ended. A turn
 *  that has been going four minutes should be able to say so while it is
 *  still going. */
function useElapsed(startedAt: number | undefined): string | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!startedAt) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [startedAt]);
  if (!startedAt) return null;
  const seconds = (now - startedAt) / 1000;
  // Under a second there is nothing to report and the number only flickers.
  return seconds >= 1 ? fmtDuration(seconds) : null;
}

/** What the turn is doing, for as long as it is doing it.
 *
 *  This used to be gated on the bubble still being *empty*
 *  (`empty && message.thinking`), so the first streamed token took it away.
 *  Models routinely open with a line of preamble before their first tool
 *  call, which meant a turn minutes from finishing read as a finished reply —
 *  the single biggest way this transcript misrepresented what was happening.
 *
 *  It now lives exactly as long as the Thinking step does, and names the step
 *  that is running rather than rotating a verb over the top of it. The verbs
 *  stay for the moments when there is genuinely nothing to name. */
function TurnFooter({ message, stalled }: { message: ChatMessage; stalled?: boolean }) {
  const step = runningStep(message.steps);
  const frame = useSpinnerFrame();
  const verb = useThinkingVerb();
  const elapsed = useElapsed(message.startedAt);

  // The socket is down and the turn was open when it went. The work may well
  // still be running on the server — turns are deliberately detached from the
  // client that started them — so this says what is actually known rather
  // than either spinning as if nothing happened or declaring it dead.
  if (stalled) {
    return (
      <span className="flex min-w-0 items-baseline gap-2 text-xs text-amber-300/90">
        <span className="shrink-0 font-mono">⚠</span>
        <span className="min-w-0">reconnecting — this turn may still be running</span>
        {elapsed && <span className="ml-auto shrink-0 font-mono text-neutral-600">{elapsed}</span>}
      </span>
    );
  }

  return (
    <span className="flex min-w-0 items-baseline gap-2 text-xs text-[#4f8dff]">
      <span className="inline-block w-3 shrink-0 self-center text-center font-mono">{frame}</span>
      {step ? (
        <>
          <span className="shrink-0 font-mono">{step.tool ?? step.label}</span>
          {step.detail && (
            <span className="min-w-0 truncate font-mono text-neutral-500">{step.detail}</span>
          )}
        </>
      ) : (
        <span className="min-w-0 truncate">{verb}</span>
      )}
      {elapsed && <span className="ml-auto shrink-0 font-mono text-neutral-600">{elapsed}</span>}
    </span>
  );
}

interface Props {
  message: ChatMessage;
  /** The bus is disconnected and this turn was open when it went. */
  stalled?: boolean;
}

export function MessageBubble({ message, stalled }: Props) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md border border-[#4f8dff]/30 bg-[#4f8dff]/[0.18] px-3.5 py-2 text-sm leading-relaxed text-neutral-50 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.15)] backdrop-blur-sm">
          {message.content}
        </div>
      </div>
    );
  }

  const empty = message.content === "";
  // Live steps are what the user watches; a finished turn's are a record, and
  // a record does not need forty lines. Left uncollapsed, one session's
  // transcript grew by every tool call the agent had ever made.
  const live = message.thinking;
  const steps = message.steps;

  return (
    <div className="flex justify-start">
      <div className="liquid-glass-subtle max-w-[85%] space-y-2 rounded-2xl rounded-bl-md px-3.5 py-2.5">
        {steps.length > 0 &&
          (live ? (
            <StepList steps={steps} />
          ) : (
            <StepSummary label={summarise(steps, message.elapsedMs)}>
              <StepList steps={steps} />
            </StepSummary>
          ))}
        {/* Three different glyphs with no legend said less than the text
            beside them already did. A retry is the one that is not routine —
            the model call died and is being restarted — so it reads as a
            warning; continuing and compacting stay the quiet progress record
            they are. Repeated retries collapse in the fold rather than
            stacking up here. */}
        {message.notices?.map((notice) => (
          <p
            key={notice.id}
            className={`flex items-baseline gap-1.5 font-mono text-xs ${
              notice.kind === "retry" ? "text-amber-300/90" : "text-neutral-500"
            }`}
          >
            <span className="shrink-0">{notice.kind === "retry" ? "⚠" : "·"}</span>
            <span className="min-w-0">{notice.text}</span>
          </p>
        ))}
        {!empty && (
          <div className="daimon-prose prose prose-invert prose-sm max-w-none text-sm leading-relaxed text-white">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          </div>
        )}
        {message.error && (
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-red-400">{message.error}</p>
        )}
        {/* Last, and unconditional on the text above it. */}
        {message.thinking && <TurnFooter message={message} stalled={stalled} />}
      </div>
    </div>
  );
}
