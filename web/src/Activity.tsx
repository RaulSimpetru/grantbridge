import { useEffect, useMemo, useState } from 'react';
import { listActivity } from './api';
import type { ActivityItem, Call, ConceptJob, Decision } from './types';

const STAGE_LABELS: Record<string, string> = {
  generate: 'Generate',
  check_eligibility_and_grounding: 'Schema & citation check',
  critique: 'Critique novelty & feasibility',
  revise: 'Revise',
  diversity_check: 'Diversity check',
};

type Filter = 'all' | 'decisions' | 'concepts' | 'watcher';

function callTitle(calls: Call[] | null, topicCode: string): string {
  return calls?.find((c) => c.topic_code === topicCode)?.title ?? topicCode;
}

function describeDecision(
  decision: Decision,
  jobsById: Record<string, ConceptJob>,
  calls: Call[] | null,
): string {
  if (decision.target_type === 'call') {
    const title = callTitle(calls, decision.target_id);
    switch (decision.kind) {
      case 'save':
        return `Saved "${title}"`;
      case 'unsave':
        return `Unsaved "${title}"`;
      case 'dismiss':
        return `Dismissed "${title}"`;
      case 'undo_dismiss':
        return `Undid dismissal of "${title}"`;
      case 'develop':
        return `Requested concept generation for "${title}"`;
      default:
        return `${decision.kind} on "${title}"`;
    }
  }
  // concept target_id is "<jobId>/<conceptId>"
  const [jobId, conceptId] = decision.target_id.split('/');
  const job = jobsById[jobId];
  const concept = job?.concepts?.find((c) => c.concept_id === conceptId);
  const label = concept ? `"${concept.title}"` : `direction ${conceptId ?? decision.target_id}`;
  const forCall = job ? ` for "${callTitle(calls, job.topic_code)}"` : '';
  switch (decision.kind) {
    case 'save':
      return `Saved concept ${label}${forCall}`;
    case 'unsave':
      return `Unsaved concept ${label}${forCall}`;
    case 'dismiss':
      return `Dismissed concept ${label}${forCall}${decision.reason ? ` — ${decision.reason}` : ''}`;
    case 'undo_dismiss':
      return `Undid dismissal of concept ${label}${forCall}`;
    case 'choose':
      return `Chose concept ${label}${forCall}`;
    case 'unchoose':
      return `Un-chose concept ${label}${forCall}`;
    case 'undevelop':
      return `No longer pursuing concept ${label}${forCall}`;
    default:
      return `${decision.kind} on concept ${label}${forCall}`;
  }
}

function DecisionRow({
  decision,
  jobsById,
  calls,
}: {
  decision: Decision;
  jobsById: Record<string, ConceptJob>;
  calls: Call[] | null;
}) {
  return (
    <div className="activity-row">
      <div className="activity-row-main">
        {decision.actor === 'watcher' && <span className="activity-actor">watch loop</span>}
        {describeDecision(decision, jobsById, calls)}
      </div>
      <div className="activity-row-time mono">{new Date(decision.created_at).toLocaleString()}</div>
    </div>
  );
}

function ConceptRunRow({
  job,
  calls,
  onOpen,
  startedByWatcher,
}: {
  job: ConceptJob;
  calls: Call[] | null;
  onOpen: () => void;
  startedByWatcher: boolean;
}) {
  const draftTitles = job.draft?.concepts.map((c) => c.title) ?? [];
  const finalTitles = job.concepts?.map((c) => c.title) ?? [];
  const evidenceCount = Object.keys(job.evidence).length;

  return (
    <div className="activity-row activity-row-run">
      <div className="activity-row-main">
        {startedByWatcher && <span className="activity-actor">watch loop</span>}
        <div>
          {job.trial ? 'Exploratory trial' : 'Concept generation'} for "
          {callTitle(calls, job.topic_code)}" —{' '}
          <span className={`activity-status activity-status-${job.status}`}>{job.status}</span>
          {job.retry_of && <span className="activity-retry-tag">retry</span>}
        </div>
        <div className="activity-stage-row">
          {job.stages.map((s) => (
            <span
              key={s.name}
              className={`activity-stage-chip activity-stage-chip-${s.status}`}
              title={s.detail ?? undefined}
            >
              {STAGE_LABELS[s.name] ?? s.name} · {s.status}
            </span>
          ))}
        </div>
        {job.status === 'succeeded' && (
          <div className="activity-run-detail">
            {draftTitles.length > 0 && <div>Draft: {draftTitles.join(' · ')}</div>}
            {finalTitles.length > 0 && <div>Final: {finalTitles.join(' · ')}</div>}
            <div>
              {job.trial && 'Exploratory trial · eligibility not established · '}
              {evidenceCount} evidence item{evidenceCount === 1 ? '' : 's'} supplied · model{' '}
              {job.model}
              {job.model_digest ? ` (${job.model_digest.slice(0, 12)})` : ''}
            </div>
          </div>
        )}
        {job.status === 'failed' && <div className="activity-run-detail">Failed: {job.error}</div>}
        {job.status === 'interrupted' && (
          <div className="activity-run-detail">
            Interrupted by a server restart before it finished.
          </div>
        )}
        <button className="btn-toggle" onClick={onOpen}>
          Open in Concept Studio
        </button>
      </div>
      <div className="activity-row-time mono">{new Date(job.created_at).toLocaleString()}</div>
    </div>
  );
}

export default function Activity({
  calls,
  onOpenConceptRun,
}: {
  calls: Call[] | null;
  onOpenConceptRun: (topicCode: string, jobId: string) => void;
}) {
  const [items, setItems] = useState<ActivityItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>('all');

  // Polled, so a run the watch loop starts, and its status, show up without a reload.
  useEffect(() => {
    const load = () =>
      listActivity()
        .then((next) => {
          setItems(next);
          setError(null); // one failed poll, e.g. during an API restart, must not stick
        })
        .catch((err: Error) => setError(err.message));
    void load();
    const id = window.setInterval(load, 5000);
    return () => window.clearInterval(id);
  }, []);

  const jobsById = useMemo(() => {
    const map: Record<string, ConceptJob> = {};
    for (const item of items ?? []) {
      if (item.type === 'concept_run' && item.concept_run)
        map[item.concept_run.id] = item.concept_run;
    }
    return map;
  }, [items]);

  // watch.py records its develop decision with the job id in `reason`, so that is
  // the only link back from a run to the loop that started it.
  const watcherJobIds = useMemo(() => {
    const ids = new Set<string>();
    for (const item of items ?? []) {
      const d = item.decision;
      if (item.type === 'decision' && d?.actor === 'watcher' && d.kind === 'develop' && d.reason)
        ids.add(d.reason);
    }
    return ids;
  }, [items]);

  const filtered = useMemo(() => {
    if (!items) return [];
    if (filter === 'all') return items;
    if (filter === 'decisions') return items.filter((i) => i.type === 'decision');
    if (filter === 'watcher')
      return items.filter((i) =>
        i.type === 'decision'
          ? i.decision?.actor === 'watcher'
          : Boolean(i.concept_run && watcherJobIds.has(i.concept_run.id)),
      );
    return items.filter((i) => i.type === 'concept_run');
  }, [items, filter, watcherJobIds]);

  return (
    <div className="activity-scroll">
      <div className="dossier-title">Activity</div>
      <p className="portfolio-note">
        Decisions and concept-generation history, newest first. Ranking history isn't recorded — the
        Opportunities feed always re-scores against your current portfolio, so there's no honest
        "calls considered on this date" to show.
      </p>

      <div className="concept-view-toggle">
        <button
          className={filter === 'all' ? 'btn-sm btn-sm-primary' : 'btn-sm'}
          onClick={() => setFilter('all')}
        >
          All
        </button>
        <button
          className={filter === 'decisions' ? 'btn-sm btn-sm-primary' : 'btn-sm'}
          onClick={() => setFilter('decisions')}
        >
          Decisions
        </button>
        <button
          className={filter === 'concepts' ? 'btn-sm btn-sm-primary' : 'btn-sm'}
          onClick={() => setFilter('concepts')}
        >
          Concept runs
        </button>
        <button
          className={filter === 'watcher' ? 'btn-sm btn-sm-primary' : 'btn-sm'}
          onClick={() => setFilter('watcher')}
        >
          Watch loop only
        </button>
      </div>

      {error && <div className="error-note">Couldn't load activity: {error}</div>}
      {!error && items === null && <div className="empty-note">Loading activity…</div>}
      {!error && items !== null && filtered.length === 0 && (
        <div className="empty-note">
          {filter === 'watcher'
            ? 'The watch loop has not started a run yet. It runs only when the API is started with --watch SECONDS, and prints why it is idle in that terminal.'
            : 'Nothing here yet — save a call or develop some concepts.'}
        </div>
      )}

      <div className="activity-list">
        {filtered.map((item) =>
          item.type === 'decision' && item.decision ? (
            <DecisionRow
              key={item.decision.id}
              decision={item.decision}
              jobsById={jobsById}
              calls={calls}
            />
          ) : item.type === 'concept_run' && item.concept_run ? (
            <ConceptRunRow
              key={item.concept_run.id}
              job={item.concept_run}
              calls={calls}
              onOpen={() => onOpenConceptRun(item.concept_run!.topic_code, item.concept_run!.id)}
              startedByWatcher={watcherJobIds.has(item.concept_run.id)}
            />
          ) : null,
        )}
      </div>
    </div>
  );
}
