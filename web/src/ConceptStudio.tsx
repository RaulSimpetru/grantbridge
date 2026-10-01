import { useEffect, useRef, useState } from 'react';
import {
  archiveDocumentUrl,
  createConceptJob,
  createDecision,
  getConceptJob,
  listConceptJobs,
  listDecisions,
} from './api';
import { chosenConceptId, isDismissed, isSaved } from './decisions';
import { stripEvidenceIds } from './evidence';
import type { Call, Concept, ConceptJob, Decision, Stage } from './types';
import ProposalWorkspace from './ProposalWorkspace';

const STAGE_LABELS: Record<string, string> = {
  generate: 'Generate',
  check_eligibility_and_grounding: 'Schema & citation check',
  critique: 'Critique novelty & feasibility',
  revise: 'Revise',
  diversity_check: 'Diversity check',
};

const DISMISS_REASONS = [
  'Weak scientific fit',
  'Missing capability',
  'Wrong career stage',
  'Not a priority now',
];

function directionLabel(index: number): string {
  return `Direction ${String.fromCharCode(65 + index)}`;
}

function StageProgress({ stages }: { stages: Stage[] }) {
  return (
    <div className="stage-progress">
      {stages.map((s, i) => (
        <div key={s.name} className="stage-step">
          {/* Dot and connector share one rail so every stage lines up, whatever
              number of lines its label wraps to. */}
          <div className="stage-rail">
            <div className={`stage-dot stage-dot-${s.status}`} aria-hidden="true" />
            {i < stages.length - 1 && <div className="stage-connector" />}
          </div>
          <div className="stage-label">
            {STAGE_LABELS[s.name] ?? s.name}
            <small> {s.status}</small>
            {s.detail && (
              <details>
                <summary>Details</summary>
                {s.detail}
              </details>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// Typical wall time per model stage on a local 12B, used only to pace the bar
// while a stage runs. The bar eases toward 95% and waits there rather than
// claiming a completion the model has not reported.
const STAGE_BUDGET_S: Record<string, number> = { generate: 50, critique: 25, revise: 35 };

function GenerationConsole({ job }: { job: ConceptJob }) {
  const endRef = useRef<HTMLDivElement | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const running = job.stages.find((s) => s.status === 'running');
  const live = running?.live ?? '';
  // Measured from the server's stage timestamp, so reopening the page mid-run
  // shows how long the stage has actually taken, not how long it has been on
  // screen. Falls back to 0 for runs started before the field existed.
  const startedAt = running?.started_at ? Date.parse(running.started_at) : null;
  const elapsed = startedAt ? Math.max(0, (now - startedAt) / 1000) : 0;

  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
    // Keyed on the stage name: `running` is a new object on every poll, and
    // the timer only needs restarting when the stage changes.
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [running?.name]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [live]);

  if (job.status !== 'running' || !running) return null;

  const done = job.stages.filter((s) => s.status === 'succeeded').length;
  const budget = STAGE_BUDGET_S[running.name];
  // Deterministic stages finish in well under a second; a timed bar for them
  // would just flash, so they show as indeterminate instead.
  const withinStage = budget ? Math.min(0.95, elapsed / budget) : 0;
  const percent = budget
    ? Math.round(((done + withinStage) / job.stages.length) * 100)
    : Math.round((done / job.stages.length) * 100);

  return (
    <div className="gen-console">
      <div className="gen-console-bar-row">
        <div className={`gen-console-bar${budget ? '' : ' gen-console-bar-indeterminate'}`}>
          <div
            className="gen-console-bar-fill"
            style={budget ? { width: `${percent}%` } : undefined}
          />
        </div>
        <span className="gen-console-meta mono">
          {budget ? `${percent}%` : 'working'} · {STAGE_LABELS[running.name] ?? running.name} ·{' '}
          {elapsed.toFixed(0)}s
        </span>
      </div>
      <pre className="gen-console-out" aria-live="polite" aria-label="Model output">
        {live ? (
          <>
            {live}
            <span className="gen-console-caret" />
          </>
        ) : (
          <span className="gen-console-waiting">
            {budget
              ? 'Waiting for the model’s first words…'
              : 'Running a plain code check. The model is not involved in this step.'}
          </span>
        )}
        <div ref={endRef} />
      </pre>
    </div>
  );
}

function truncate(text: string, max = 90): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function EvidenceChips({ concept, job }: { concept: Concept; job: ConceptJob }) {
  return (
    <div className="evidence-chip-row">
      {[
        ...concept.call_evidence,
        ...concept.researcher_evidence,
        ...(concept.archive_evidence ?? []),
      ].map((ev, i) => {
        const source = job.evidence[ev.source_id];
        const archive = job.archive_context?.passages.find((p) => p.source_id === ev.source_id);
        return (
          <details key={`${ev.source_id}-${i}`} className="evidence-chip">
            {/* The passage is shared by all three concepts, so labelling the pill with it
                made every card show the same chip. The claim is what differs per concept. */}
            <summary>{truncate(ev.claim_supported) || ev.source_id}</summary>
            <p className="evidence-passage">{source?.text ?? 'Source passage unavailable'}</p>
            <small>
              {ev.source_id} · {source?.section.replaceAll('_', ' ')}
            </small>
            {archive && (
              <p>
                Historical inspiration, not personal capability.{' '}
                <a href={archiveDocumentUrl(archive.document_id)}>{archive.document_name} ↗</a> ·
                text lines {archive.line_start}–{archive.line_end}
              </p>
            )}
          </details>
        );
      })}
    </div>
  );
}

interface ConceptActionsProps {
  jobId: string;
  concept: Concept;
  decisions: Decision[];
  onChanged: () => void;
}

function ConceptActions({ jobId, concept, decisions, onChanged }: ConceptActionsProps) {
  const targetId = `${jobId}/${concept.concept_id}`;
  const saved = isSaved(decisions, 'concept', targetId);
  const dismissed = isDismissed(decisions, 'concept', targetId);
  const chosen = chosenConceptId(decisions, jobId) === concept.concept_id;
  const [dismissing, setDismissing] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const act = async (kind: Decision['kind'], reason?: string) => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await createDecision({ kind, target_type: 'concept', target_id: targetId, reason });
      setDismissing(false);
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (dismissed) {
    return (
      <div className="concept-actions">
        {error && <p role="alert">{error}</p>}
        <span className="tag-pending">Dismissed</span>
        <button className="btn-toggle" onClick={() => act('undo_dismiss')}>
          Undo
        </button>
      </div>
    );
  }

  return (
    <div className="concept-actions">
      {error && <p role="alert">{error}</p>}
      <button
        className={saved ? 'btn-sm btn-sm-chosen' : 'btn-sm'}
        onClick={() => act(saved ? 'unsave' : 'save')}
      >
        {saved ? 'Saved ✓' : 'Save'}
      </button>
      {!dismissing ? (
        <button className="btn-sm" onClick={() => setDismissing(true)}>
          Dismiss
        </button>
      ) : (
        <div className="reason-chip-row">
          <button className="reason-chip" onClick={() => setDismissing(false)}>
            Cancel
          </button>
          {DISMISS_REASONS.map((reason) => (
            <button key={reason} className="reason-chip" onClick={() => act('dismiss', reason)}>
              {reason}
            </button>
          ))}
        </div>
      )}
      <button
        className={chosen ? 'btn-sm btn-sm-chosen' : 'btn-sm btn-sm-primary'}
        onClick={() => act(chosen ? 'unchoose' : 'choose')}
      >
        {chosen ? 'Chosen ✓ — click to undo' : 'Choose this direction'}
      </button>
    </div>
  );
}

function ConceptCard({
  index,
  concept,
  job,
  decisions,
  open,
  onToggleOpen,
  onChanged,
}: {
  index: number;
  concept: Concept;
  job: ConceptJob;
  decisions: Decision[];
  open: boolean;
  onToggleOpen: () => void;
  onChanged: () => void;
}) {
  const critique = job.critique?.concept_critiques.find((c) => c.concept_id === concept.concept_id);
  return (
    <div className={open ? 'concept-card concept-card-open' : 'concept-card'}>
      <div className="concept-label">{directionLabel(index)}</div>
      <div className="concept-title">{concept.title}</div>
      <div className="concept-hyp">{stripEvidenceIds(concept.central_hypothesis)}</div>
      <div className="risk-line">
        <b>Principal risk:</b> {stripEvidenceIds(concept.principal_feasibility_risk)}
      </div>
      <button className="btn-sm" style={{ alignSelf: 'flex-start' }} onClick={onToggleOpen}>
        {open ? 'Show less' : 'Open direction'}
      </button>
      {open && (
        <div className="concept-detail">
          <div className="detail-section">
            <div className="detail-label">Problem &amp; central hypothesis</div>
            <div className="detail-text">{stripEvidenceIds(concept.problem)}</div>
          </div>
          <div className="detail-section">
            <div className="detail-label">Why it fits the call</div>
            <div className="detail-text">{stripEvidenceIds(concept.call_fit)}</div>
          </div>
          <div className="detail-section">
            <div className="detail-label">Why the researcher can execute it</div>
            <div className="detail-text">{stripEvidenceIds(concept.researcher_fit)}</div>
          </div>
          <div className="detail-section">
            <div className="detail-label">Objectives</div>
            <ul>
              {concept.objectives.map((o) => (
                <li key={o}>{stripEvidenceIds(o)}</li>
              ))}
            </ul>
          </div>
          <div className="detail-section">
            <div className="detail-label">
              Required assets &amp; collaborators (needed, not yet held)
            </div>
            <ul>
              {concept.required_assets_and_collaborators.map((a) => (
                <li key={a}>{stripEvidenceIds(a)}</li>
              ))}
            </ul>
          </div>
          <div className="detail-section">
            <div className="detail-label">Novelty claim</div>
            <div className="detail-text">{stripEvidenceIds(concept.novelty)}</div>
          </div>
          <div className="detail-section">
            <div className="detail-label">Call, profile &amp; historical inspiration</div>
            <EvidenceChips concept={concept} job={job} />
          </div>
          {critique && (
            <div className="trace-block">
              <b>What the model criticised, unchecked:</b> {critique.grounding_and_fit}{' '}
              {critique.novelty_and_feasibility}{' '}
              <b>
                {job.revision_audit ? 'What it suggests changing:' : 'Historical revision advice:'}
              </b>{' '}
              {critique.revision_action}
            </div>
          )}
        </div>
      )}
      <ConceptActions
        jobId={job.id}
        concept={concept}
        decisions={decisions}
        onChanged={onChanged}
      />
    </div>
  );
}

function CompareView({
  job,
  decisions,
  onChanged,
}: {
  job: ConceptJob;
  decisions: Decision[];
  onChanged: () => void;
}) {
  const concepts = job.concepts ?? [];
  return (
    <>
      <div className="mobile-compare">
        {concepts.map((c, i) => (
          <article className="concept-card" key={c.concept_id}>
            <h3>
              {directionLabel(i)} · {c.title}
            </h3>
            {[
              ['Hypothesis', c.central_hypothesis],
              ['Novelty', c.novelty],
              ['Feasibility risk', c.principal_feasibility_risk],
              ['Required assets & collaborators', c.required_assets_and_collaborators.join('; ')],
            ].map(([label, value]) => (
              <section key={label}>
                <h4>{label}</h4>
                <p>{stripEvidenceIds(value)}</p>
              </section>
            ))}
            <h4>Evidence</h4>
            <EvidenceChips concept={c} job={job} />
            <h4>Words shared with the other concepts</h4>
            {Object.entries(job.diversity?.[c.concept_id] ?? {}).map(([id, score]) => (
              <p key={id}>
                {Math.round(score * 100)}% with{' '}
                {directionLabel(concepts.findIndex((x) => x.concept_id === id))}
              </p>
            ))}
            <ConceptActions
              jobId={job.id}
              concept={c}
              decisions={decisions}
              onChanged={onChanged}
            />
          </article>
        ))}
      </div>
      <div className="compare-scroll">
        <table className="compare-table">
          <thead>
            <tr>
              <th />
              {concepts.map((c, i) => (
                <th key={c.concept_id}>
                  <div className="compare-label">{directionLabel(i)}</div>
                  <div className="compare-title">{c.title}</div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className="compare-row-label">Hypothesis</td>
              {concepts.map((c) => (
                <td key={c.concept_id}>{stripEvidenceIds(c.central_hypothesis)}</td>
              ))}
            </tr>
            <tr>
              <td className="compare-row-label">Novelty</td>
              {concepts.map((c) => (
                <td key={c.concept_id}>{stripEvidenceIds(c.novelty)}</td>
              ))}
            </tr>
            <tr>
              <td className="compare-row-label">Feasibility risk</td>
              {concepts.map((c) => (
                <td key={c.concept_id}>{stripEvidenceIds(c.principal_feasibility_risk)}</td>
              ))}
            </tr>
            <tr>
              <td className="compare-row-label">
                Required assets/collaborators (verify availability)
              </td>
              {concepts.map((c) => (
                <td key={c.concept_id}>
                  {c.required_assets_and_collaborators.map((a) => (
                    <span key={a} className="tag tag-amber">
                      {stripEvidenceIds(a)}
                    </span>
                  ))}
                </td>
              ))}
            </tr>
            <tr>
              <td className="compare-row-label">Evidence coverage</td>
              {concepts.map((c) => (
                <td key={c.concept_id}>
                  <EvidenceChips concept={c} job={job} />
                </td>
              ))}
            </tr>
            <tr>
              <td className="compare-row-label">Words shared with the other concepts</td>
              {concepts.map((c) => {
                const overlaps = job.diversity?.[c.concept_id] ?? {};
                return (
                  <td key={c.concept_id}>
                    {Object.entries(overlaps).map(([otherId, score]) => {
                      const otherIndex = concepts.findIndex((o) => o.concept_id === otherId);
                      return (
                        <div key={otherId}>
                          {Math.round(score * 100)}% with {directionLabel(otherIndex)}
                        </div>
                      );
                    })}
                    {Object.keys(overlaps).length === 0 && '—'}
                  </td>
                );
              })}
            </tr>
            <tr>
              <td className="compare-row-label">Actions</td>
              {concepts.map((c) => (
                <td key={c.concept_id}>
                  <ConceptActions
                    jobId={job.id}
                    concept={c}
                    decisions={decisions}
                    onChanged={onChanged}
                  />
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}

function JobView({
  job,
  decisions,
  onRefresh,
  onRequestNew,
  canGenerate,
}: {
  job: ConceptJob;
  decisions: Decision[];
  onRefresh: () => void;
  onRequestNew: (retryOf?: string) => void;
  canGenerate: boolean;
}) {
  const [viewMode, setViewMode] = useState<'cards' | 'compare' | 'drafting'>('cards');
  const [openId, setOpenId] = useState<string | null>(null);

  return (
    <div className="concept-studio">
      <StageProgress stages={job.stages} />
      <GenerationConsole job={job} />

      {job.status === 'failed' && (
        <div className="error-note">
          Generation failed at "
          {STAGE_LABELS[job.stages.find((s) => s.status === 'failed')?.name ?? ''] ?? 'a stage'}
          ": {job.error}
          <div>
            <button
              className="btn-primary"
              disabled={!canGenerate}
              onClick={() => onRequestNew(job.id)}
            >
              Retry
            </button>
          </div>
        </div>
      )}

      {job.status === 'running' && (
        <div className="empty-note">Writing. On a local model this takes a few minutes.</div>
      )}

      {job.status === 'interrupted' && (
        <div className="error-note">
          The server restarted before this run finished, so it produced nothing.
          <div>
            <button
              className="btn-primary"
              disabled={!canGenerate}
              onClick={() => onRequestNew(job.id)}
            >
              Retry
            </button>
          </div>
        </div>
      )}

      {job.status === 'succeeded' && job.concepts && (
        <>
          <div className="concept-view-toggle">
            <button
              className={viewMode === 'cards' ? 'btn-sm btn-sm-primary' : 'btn-sm'}
              onClick={() => setViewMode('cards')}
            >
              Cards
            </button>
            <button
              className={viewMode === 'compare' ? 'btn-sm btn-sm-primary' : 'btn-sm'}
              onClick={() => setViewMode('compare')}
            >
              Compare
            </button>
            <button
              className={viewMode === 'drafting' ? 'btn-sm btn-sm-primary' : 'btn-sm'}
              onClick={() => setViewMode('drafting')}
            >
              Draft proposal
            </button>
          </div>

          {job.revision_audit && (
            <p className="fit-note">
              {job.revision_audit.notice}
              {job.revision_audit.restored_fields.length > 0 &&
                ` Restored ${job.revision_audit.restored_fields.length} core-field changes attempted during revision.`}
            </p>
          )}
          {viewMode === 'drafting' && (
            <ProposalWorkspace
              key={job.id}
              job={job}
              chosenId={chosenConceptId(decisions, job.id)}
            />
          )}

          {viewMode === 'cards' && (
            <div className="concept-card-grid">
              {job.concepts.map((c, i) => (
                <ConceptCard
                  key={c.concept_id}
                  index={i}
                  concept={c}
                  job={job}
                  decisions={decisions}
                  open={openId === c.concept_id}
                  onToggleOpen={() => setOpenId(openId === c.concept_id ? null : c.concept_id)}
                  onChanged={onRefresh}
                />
              ))}
            </div>
          )}
          {viewMode === 'compare' && (
            <CompareView job={job} decisions={decisions} onChanged={onRefresh} />
          )}

          <button
            className="btn-ghost"
            hidden={viewMode === 'drafting'}
            disabled={!canGenerate}
            onClick={() => onRequestNew()}
          >
            Request a new set
          </button>
        </>
      )}
    </div>
  );
}

export default function ConceptStudio({
  call,
  initialJobId,
  generationReady,
}: {
  call: Call;
  initialJobId?: string;
  generationReady: boolean;
}) {
  const [jobs, setJobs] = useState<ConceptJob[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const pollRef = useRef<number | null>(null);
  const pendingInitialJobId = useRef(initialJobId);

  const refreshDecisions = () => {
    listDecisions()
      .then(setDecisions)
      .catch((err: Error) => setError(err.message));
  };

  const refreshJobs = () => {
    listConceptJobs(call.topic_code)
      .then((data) => {
        setJobs(data);
        // Read the ref out here: StrictMode invokes the updater twice, and clearing
        // it inside would leave the second pass falling through to the default.
        const pending = pendingInitialJobId.current;
        // An interrupted run has no output, so it is never the one to open by default.
        const fallback = data.filter((j) => j.status === 'succeeded').at(-1) ?? data.at(-1);
        setSelectedId((current) => {
          if (current) return current;
          if (pending && data.some((j) => j.id === pending)) return pending;
          return fallback?.id ?? null;
        });
        pendingInitialJobId.current = undefined;
      })
      .catch((err: Error) => {
        setError(err.message);
        setJobs([]);
      });
  };

  useEffect(() => {
    setJobs(null);
    setSelectedId(null);
    setError(null);
    pendingInitialJobId.current = initialJobId;
    refreshJobs();
    refreshDecisions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [call.topic_code, initialJobId]);

  useEffect(() => {
    const selected = jobs?.find((j) => j.id === selectedId);
    if (!selected || selected.status !== 'running') {
      if (pollRef.current) window.clearInterval(pollRef.current);
      return;
    }
    pollRef.current = window.setInterval(() => {
      getConceptJob(selected.id)
        .then((updated) => {
          setJobs((current) => (current ?? []).map((j) => (j.id === updated.id ? updated : j)));
          setError(null);
        })
        .catch((err: Error) => setError(`Connection interrupted: ${err.message}. Retrying…`));
      // Fast enough that the streamed output reads as typing rather than
      // arriving in blocks. The payload is one job on a loopback server.
    }, 600);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [jobs, selectedId]);

  const startJob = async (retryOf?: string) => {
    const trial = jobs?.find((j) => j.id === (retryOf ?? selectedId))?.trial ?? false;
    if (starting || !generationReady || (!trial && call.eligibility.state !== 'eligible')) return;
    setStarting(true);
    setError(null);
    try {
      const job = await createConceptJob(call.topic_code, retryOf, trial);
      setJobs((current) => [...(current ?? []), job]);
      setSelectedId(job.id);
      refreshDecisions();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setStarting(false);
    }
  };

  if (jobs === null) return <div className="empty-note">Loading concept jobs…</div>;

  const selected = jobs.find((j) => j.id === selectedId) ?? null;
  const priorJobs = jobs.filter((j) => j.id !== selectedId);
  // A call can collect many interrupted runs; every one of them used to get its own button.
  // Runs that produced something come first, then the most recent of the rest.
  const shownPriorJobs = [...priorJobs]
    .sort(
      (a, b) =>
        Number(b.status === 'succeeded') - Number(a.status === 'succeeded') ||
        b.created_at.localeCompare(a.created_at),
    )
    .slice(0, 8);

  return (
    <div className="concept-studio-pane">
      <div className="dossier-title">Concept studio — {call.title ?? call.topic_code}</div>
      {selected?.trial && (
        <p className="pending-card">
          <strong>Exploratory run.</strong> You haven’t checked yet whether you can apply for this
          call.
        </p>
      )}
      {selected?.archive_context && (
        <details className="pending-card">
          <summary>
            Archive-informed run · {selected.archive_context.passages.length} approved passages
          </summary>
          <p>Question: {selected.archive_context.query}</p>
          <p>
            Sent to {selected.archive_context.connection.base_url} ·{' '}
            {selected.archive_context.connection.model}
          </p>
          <p>
            For another archive-informed run, return to Past applications → Explore ideas and
            preview the passages again.
          </p>
          {selected.archive_context.passages.map((p) => (
            <p key={p.source_id}>
              <a href={archiveDocumentUrl(p.document_id)}>{p.document_name}</a> · lines{' '}
              {p.line_start}–{p.line_end}
            </p>
          ))}
        </details>
      )}
      {selected?.concepts && (
        <p className="fit-note">
          Check the funding rules yourself. The model can read an optional module or an example as a
          requirement.
        </p>
      )}
      {call.eligibility.state !== 'eligible' && (
        <p className="pending-card">
          {call.eligibility.reason} Review this in Opportunities before starting a new run.
        </p>
      )}
      {!generationReady && (
        <p className="pending-card">
          Generation service unavailable. Existing runs remain readable.
        </p>
      )}
      {error && <div className="error-note">{error}</div>}

      {jobs.length === 0 && (
        <div className="empty-note">
          No concepts generated yet for this call.
          <div>
            <button
              className="btn-primary"
              onClick={() => startJob()}
              disabled={starting || !generationReady || call.eligibility.state !== 'eligible'}
            >
              Develop 3 concepts
            </button>
          </div>
        </div>
      )}

      {priorJobs.length > 0 && (
        <div className="job-history">
          <span>Other runs:</span>
          {shownPriorJobs.map((j) => (
            <button key={j.id} className="btn-toggle" onClick={() => setSelectedId(j.id)}>
              {j.id.slice(0, 6)} · {j.status}
            </button>
          ))}
          {priorJobs.length > shownPriorJobs.length && (
            <span className="job-history-more">
              +{priorJobs.length - shownPriorJobs.length} older
            </span>
          )}
        </div>
      )}

      {selected && (
        <JobView
          job={selected}
          decisions={decisions}
          onRefresh={refreshDecisions}
          onRequestNew={startJob}
          canGenerate={
            !starting &&
            generationReady &&
            !selected.archive_context &&
            (Boolean(selected.trial) || call.eligibility.state === 'eligible')
          }
        />
      )}
    </div>
  );
}
