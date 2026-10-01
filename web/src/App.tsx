import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Activity from './Activity';
import {
  createConceptJob,
  createDecision,
  generationHealth,
  listCalls,
  listDecisions,
  reviewEligibility,
} from './api';
import ConceptStudio from './ConceptStudio';
import ConnectionSettings from './ConnectionSettings';
import FundingSources from './FundingSources';
import { isDismissed, isSaved } from './decisions';
import Portfolio from './Portfolio';
import PastApplications from './PastApplications';
import SourceDocument from './SourceDocument';
import type { Call, Decision, GenerationHealth } from './types';

type View =
  'opportunities' | 'portfolio' | 'concepts' | 'activity' | 'sources' | 'past-applications';
const eligibilityLabel = (c: Call) =>
  c.eligibility.state === 'eligible'
    ? 'Eligible · reviewed'
    : c.eligibility.state === 'ineligible'
      ? 'Ineligible'
      : 'Eligibility uncertain';
const archived = (c: Call) => ['closed', 'archived'].includes(c.availability);
const deadlineLabel = (c: Call) =>
  archived(c)
    ? 'Closed / archived'
    : c.availability === 'later_stage'
      ? 'Later stage only'
      : c.availability === 'unknown'
        ? 'Verify deadline'
        : 'Deadline ahead';

function EligibilityForm({ call, onUpdated }: { call: Call; onUpdated: () => void }) {
  const [state, setState] = useState<'eligible' | 'ineligible'>('eligible');
  const [reason, setReason] = useState('');
  const [source, setSource] = useState(call.source_url ?? '');
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return (
    <details className="review-form">
      <summary>Review eligibility against the official rules</summary>
      <p>
        This is your recorded assessment. Check host, career stage, consortium, submission window
        and call-specific restrictions. Changes to the call or portfolio invalidate this assessment.
      </p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError('');
          try {
            await reviewEligibility(call.topic_code, {
              state,
              reason,
              source,
              conditions_checked: true,
            });
            onUpdated();
          } catch (err) {
            setError((err as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          Assessment
          <select value={state} onChange={(e) => setState(e.target.value as typeof state)}>
            <option value="eligible">Requirements met</option>
            <option value="ineligible">Requirements not met</option>
          </select>
        </label>
        <label>
          Official source and section
          <input
            required
            minLength={10}
            value={source}
            onChange={(e) => setSource(e.target.value)}
          />
        </label>
        <label>
          Evidence and rationale
          <textarea
            required
            minLength={10}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Explain how the requirements are met, or identify the blocking rule."
          />
        </label>
        <label className="check-label">
          <input
            type="checkbox"
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
            required
          />
          I checked the current rules, deadline and relevant portfolio facts.
        </label>
        <button className="btn-primary" disabled={busy || !checked}>
          {busy ? 'Saving…' : 'Confirm and recalculate'}
        </button>
        <p>Stored privately for this call and portfolio version.</p>
        {error && <p role="alert">{error}</p>}
      </form>
    </details>
  );
}

export default function App() {
  const [view, setView] = useState<View>('opportunities');
  const [calls, setCalls] = useState<Call[] | null>(null);
  const [error, setError] = useState('');
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('current');
  const [provider, setProvider] = useState('all');
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [conceptsTopicCode, setConceptsTopicCode] = useState<string | null>(null);
  const [conceptsJobId, setConceptsJobId] = useState<string | undefined>();
  const [mobileDetail, setMobileDetail] = useState(false);
  const [busy, setBusy] = useState(false);
  const [health, setHealth] = useState<GenerationHealth | null>(null);
  const [checking, setChecking] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const healthRequest = useRef(0);
  const [notice, setNotice] = useState<{ text: string; undo?: () => void } | null>(null);
  // A notice belongs to the view that raised it; every route to another view goes through here.
  // oxlint-disable-next-line react/set-state-in-effect
  useEffect(() => setNotice(null), [view]);
  const callRequest = useRef(0);
  const refreshCalls = useCallback(async () => {
    const request = ++callRequest.current;
    try {
      const data = await listCalls();
      data.sort((a, b) => (a.scientific_fit?.rank ?? 999) - (b.scientific_fit?.rank ?? 999));
      if (request !== callRequest.current) return;
      setCalls(data);
      setSelectedCode((c) =>
        c && data.some((d) => d.topic_code === c)
          ? c
          : (data.find((c) => !archived(c))?.topic_code ?? data[0]?.topic_code ?? null),
      );
      setError('');
    } catch (err) {
      if (request === callRequest.current) setError((err as Error).message);
    }
  }, []);
  const refreshDecisions = useCallback(async () => {
    try {
      setDecisions(await listDecisions());
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);
  const checkHealth = useCallback(async () => {
    const request = ++healthRequest.current;
    setChecking(true);
    try {
      const result = await generationHealth();
      if (request === healthRequest.current) setHealth(result);
    } catch {
      if (request === healthRequest.current)
        setHealth({
          ready: false,
          model: '',
          detail: 'Cannot reach the GrantBridge API. Check that it is running.',
          checked_at: new Date().toISOString(),
        });
    } finally {
      if (request === healthRequest.current) setChecking(false);
    }
  }, []);
  useEffect(() => {
    // Updates in these loaders happen after network responses, not during the effect.
    // oxlint-disable-next-line react/set-state-in-effect
    void refreshCalls();
    void refreshDecisions();
    void checkHealth();
  }, [refreshCalls, refreshDecisions, checkHealth]);
  useEffect(() => {
    const id = window.setInterval(checkHealth, 30000);
    return () => clearInterval(id);
  }, [checkHealth]);
  const actOnCall = async (code: string, kind: Decision['kind']) => {
    try {
      await createDecision({ kind, target_type: 'call', target_id: code });
      await refreshDecisions();
      setNotice({
        text:
          kind === 'dismiss'
            ? 'Call dismissed'
            : kind === 'save'
              ? 'Call saved'
              : 'Decision updated',
        undo:
          kind === 'dismiss'
            ? () => {
                void actOnCall(code, 'undo_dismiss');
              }
            : undefined,
      });
    } catch (err) {
      setError((err as Error).message);
    }
  };
  const develop = async (code: string, trial = false) => {
    setBusy(true);
    setError('');
    try {
      const job = await createConceptJob(code, undefined, trial);
      setConceptsTopicCode(code);
      setConceptsJobId(job.id);
      setView('concepts');
      await refreshDecisions();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const filtered = useMemo(
    () =>
      (calls ?? []).filter((c) => {
        if (provider !== 'all' && (c.provider ?? 'eu') !== provider) return false;
        const q = query.trim().toLowerCase();
        if (q && !`${c.title} ${c.scope_text} ${c.topic_code}`.toLowerCase().includes(q))
          return false;
        if (filter === 'saved') return isSaved(decisions, 'call', c.topic_code);
        if (filter === 'dismissed') return isDismissed(decisions, 'call', c.topic_code);
        if (filter === 'all') return true;
        return (
          !isDismissed(decisions, 'call', c.topic_code) &&
          (filter === 'current' ? !archived(c) : c.eligibility.state === filter)
        );
      }),
    [calls, query, filter, decisions, provider],
  );
  const selected = filtered.find((c) => c.topic_code === selectedCode) ?? filtered[0];
  const selectedConceptCall = calls?.find((c) => c.topic_code === conceptsTopicCode);
  const openRun = (code: string, id: string) => {
    setConceptsTopicCode(code);
    setConceptsJobId(id);
    setView('concepts');
  };
  return (
    <div className="app">
      <header className="topbar">
        <div className="wordmark">
          <span className="brand-mark" aria-hidden="true">
            g
          </span>
          GrantBridge
        </div>
        <nav className="nav-tabs" aria-label="Main navigation">
          {(['opportunities', 'concepts', 'sources', 'activity'] as View[]).map((v) => (
            <button
              key={v}
              className={`nav-tab ${view === v ? 'nav-tab-active' : ''}`}
              aria-current={view === v ? 'page' : undefined}
              onClick={() => {
                setView(v);
                if (v === 'opportunities') {
                  void refreshCalls();
                  void refreshDecisions();
                }
              }}
            >
              {v === 'sources' ? 'Funding sources' : v[0].toUpperCase() + v.slice(1)}
            </button>
          ))}
        </nav>
        <div className="header-account">
          <details
            className="account-menu"
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.currentTarget.open = false;
                e.currentTarget.querySelector('summary')?.focus();
              }
            }}
          >
            <summary className="profile-button" aria-label="Researcher account">
              <svg
                aria-hidden="true"
                width="22"
                height="22"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
              >
                <circle cx="12" cy="8" r="3.5" />
                <path d="M5 21v-2a7 7 0 0 1 14 0v2" />
              </svg>
              <span>My profile</span>
            </summary>
            <div
              className="account-menu-items"
              onClick={(e) => {
                const menu = e.currentTarget.closest('details');
                if (menu) menu.open = false;
              }}
            >
              <button onClick={() => setView('portfolio')}>My profile</button>
              <button onClick={() => setView('past-applications')}>Past applications</button>
              <button onClick={() => setSettingsOpen(true)}>Model connection</button>
            </div>
          </details>
        </div>
      </header>
      <div className={`connection-bar ${health?.ready ? 'is-connected' : 'is-offline'}`}>
        <div className="connection-summary" role="status" aria-live="polite" aria-atomic="true">
          <span className="status-dot" aria-hidden="true" />
          <strong>
            {checking
              ? 'Checking connection…'
              : health?.ready
                ? 'Model connected'
                : health
                  ? 'Connection needs attention'
                  : 'Connecting…'}
          </strong>
          {health?.checked_at && !checking && (
            <small>
              Checked {new Date(health.checked_at).toLocaleTimeString()}
              {health.latency_ms !== undefined && ` · ${health.latency_ms} ms`}
            </small>
          )}
        </div>
        <div className="connection-tools">
          <button className="btn-ghost" disabled={checking} onClick={() => void checkHealth()}>
            {checking ? 'Checking…' : 'Check connection'}
          </button>
          <button className="btn-ghost" onClick={() => setSettingsOpen(true)}>
            Connection settings ↗
          </button>
        </div>
        {health && !health.ready && !checking && (
          <p className="connection-problem">{health.detail}</p>
        )}
      </div>
      {settingsOpen && (
        <ConnectionSettings
          onClose={() => setSettingsOpen(false)}
          onActiveChanged={(result) => {
            healthRequest.current++;
            setHealth(result);
            setChecking(false);
          }}
        />
      )}
      {error && (
        <div className="error-note" role="alert">
          {error}{' '}
          <button
            onClick={() => {
              void refreshCalls();
              void refreshDecisions();
            }}
          >
            Retry
          </button>
        </div>
      )}
      {notice && (
        <div className="notice" role="status">
          {notice.text} {notice.undo && <button onClick={notice.undo}>Undo</button>}{' '}
          <button aria-label="Close notification" onClick={() => setNotice(null)}>
            ×
          </button>
        </div>
      )}
      {view === 'portfolio' && (
        <Portfolio
          onUpdated={refreshCalls}
          onPastApplications={() => setView('past-applications')}
        />
      )}
      {view === 'past-applications' && (
        <PastApplications
          calls={calls ?? []}
          onStarted={(job) => openRun(job.topic_code, job.id)}
          onConnection={() => setSettingsOpen(true)}
        />
      )}
      {view === 'sources' && (
        <FundingSources
          onImported={(call) => {
            void refreshCalls();
            setSelectedCode(call.topic_code);
            setProvider('all');
            setFilter('all');
            setQuery('');
            setView('opportunities');
            setMobileDetail(true);
          }}
        />
      )}
      {view === 'activity' && <Activity calls={calls} onOpenConceptRun={openRun} />}
      {view === 'concepts' && (
        <div className="concepts-workspace">
          <label className="concept-call-picker">
            Funding call
            <select
              value={conceptsTopicCode ?? ''}
              onChange={(e) => {
                setConceptsTopicCode(e.target.value);
                setConceptsJobId(undefined);
              }}
            >
              <option value="">Choose a call</option>
              {calls?.map((c) => (
                <option key={c.topic_code} value={c.topic_code}>
                  {c.title ?? c.topic_code}
                </option>
              ))}
            </select>
          </label>
          {selectedConceptCall ? (
            <ConceptStudio
              key={`${conceptsTopicCode}/${conceptsJobId ?? ''}`}
              call={selectedConceptCall}
              initialJobId={conceptsJobId}
              generationReady={health?.ready ?? false}
            />
          ) : (
            <p>Select a call to view its saved concepts and generation history.</p>
          )}
        </div>
      )}
      {view === 'opportunities' && (
        <>
          <div className="workspace-heading">
            <div>
              <p className="eyebrow">YOUR FUNDING WORKSPACE</p>
              <h1>
                Opportunities <span className="heading-count">{calls?.length ?? '…'}</span>
              </h1>
              <p>EU and German funding from your connected sources.</p>
            </div>
            <button className="btn-sm" onClick={() => setView('sources')}>
              Manage funding sources ↗
            </button>
          </div>
          <div className={`workspace ${mobileDetail ? 'show-detail' : ''}`}>
            <aside className="feed-pane" aria-label="Funding opportunities">
              <div className="feed-controls">
                <div className="feed-heading">
                  <h2>Your watch-list</h2>
                  <span>{filtered.length} shown</span>
                </div>
                <label className="feed-filter">
                  Funder
                  <select value={provider} onChange={(e) => setProvider(e.target.value)}>
                    <option value="all">All connected funders</option>
                    <option value="eu">European Union</option>
                    <option value="dfg">DFG · Germany</option>
                    <option value="bmftr">BMFTR · Germany</option>
                  </select>
                </label>
                <label className="search-box">
                  <span className="sr-only">Search opportunities</span>
                  <input
                    placeholder="Search calls, methods, outcomes…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                </label>
                <label className="feed-filter">
                  Show
                  <select value={filter} onChange={(e) => setFilter(e.target.value)}>
                    {[
                      ['current', 'Current & upcoming'],
                      ['all', 'All, including archived'],
                      ['eligible', 'Eligible'],
                      ['uncertain', 'Eligibility uncertain'],
                      ['ineligible', 'Ineligible'],
                      ['saved', 'Saved'],
                      ['dismissed', 'Dismissed'],
                    ].map(([v, l]) => (
                      <option key={v} value={v}>
                        {l}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="feed-list">
                {!calls && !error && <p>Loading official call records…</p>}
                {calls && !filtered.length && (
                  <div className="empty-note">
                    No calls match. Try all calls or broaden your search. Confirm portfolio evidence
                    to improve matching.
                  </div>
                )}
                {filtered.map((c) => (
                  <button
                    key={c.topic_code}
                    className={`op-card ${selected?.topic_code === c.topic_code ? 'op-card-selected' : ''}`}
                    onClick={() => {
                      setSelectedCode(c.topic_code);
                      setMobileDetail(true);
                    }}
                  >
                    <span className="op-card-top">
                      <span className="funder-label">
                        {c.provider === 'dfg'
                          ? 'DFG'
                          : c.provider === 'bmftr'
                            ? 'BMFTR'
                            : !c.provider || c.provider === 'eu'
                              ? 'EU FUNDING'
                              : (c.call_identifier ?? c.provider)}
                      </span>
                      <span className={`eligibility-badge ${c.eligibility.state}`}>
                        {c.eligibility.state === 'uncertain' ? 'Needs review' : eligibilityLabel(c)}
                      </span>
                    </span>
                    <span className="op-title">{c.title ?? c.topic_code}</span>
                    <span className="op-meta op-code">{c.topic_code}</span>
                    <span className="op-row">
                      {deadlineLabel(c)} ·{' '}
                      {c.deadlines.map((d) => d.slice(0, 10)).join(' / ') || 'Not recorded'}
                    </span>
                    <span className="tag-fit">
                      {c.scientific_fit
                        ? `Scientific fit rank ${c.scientific_fit.rank} of ${calls?.length}`
                        : 'Confirm portfolio evidence to rank'}
                    </span>
                    {isSaved(decisions, 'call', c.topic_code) && <span>Saved ✓</span>}
                  </button>
                ))}
              </div>
            </aside>
            <main className="dossier-pane">
              <button className="mobile-back btn-ghost" onClick={() => setMobileDetail(false)}>
                ← Opportunities
              </button>
              {selected ? (
                <>
                  <div className="dossier-scroll">
                    <div className="call-overview">
                      <p className="eyebrow">
                        {selected.call_identifier ||
                          selected.framework_programme ||
                          'FUNDING OPPORTUNITY'}
                      </p>
                      <h2 className="dossier-title">{selected.title}</h2>
                      <p className="call-code">
                        {selected.topic_code}
                        {selected.programme_period && ` · ${selected.programme_period}`}
                      </p>
                      <div className="call-facts">
                        <div>
                          <span>{deadlineLabel(selected)}</span>
                          <strong>
                            {selected.deadlines.map((d) => d.slice(0, 10)).join(' / ') ||
                              'Check official source'}
                          </strong>
                        </div>
                        <div>
                          <span>Source record</span>
                          {selected.source_url && (
                            <a href={selected.source_url} target="_blank" rel="noreferrer">
                              View official call ↗
                            </a>
                          )}
                          {selected.fetched_at && (
                            <small>Captured {selected.fetched_at.slice(0, 10)}</small>
                          )}
                        </div>
                      </div>
                    </div>
                    {selected.source_warning && (
                      <details className="source-warning">
                        <summary>Source limitations — read before applying</summary>
                        <p>{selected.source_warning}</p>
                      </details>
                    )}
                    <section className="eligibility-panel">
                      <h2>Eligibility & consortium</h2>
                      <span className={`eligibility-badge ${selected.eligibility.state}`}>
                        {eligibilityLabel(selected)}
                      </span>
                      <p>{selected.eligibility.reason}</p>
                      {selected.eligibility.basis === 'researcher_review' && (
                        <p>
                          Researcher review · {selected.eligibility.reviewed_at?.slice(0, 10)} ·{' '}
                          {selected.eligibility.source}
                        </p>
                      )}
                      <details>
                        <summary>Official eligibility conditions</summary>
                        <SourceDocument
                          nodes={selected.topic_conditions_document}
                          fallback={
                            selected.topic_conditions ||
                            'Consult the official source and its annexes.'
                          }
                        />
                      </details>
                      {!archived(selected) && (
                        <EligibilityForm
                          key={selected.topic_code}
                          call={selected}
                          onUpdated={() => {
                            void refreshCalls();
                          }}
                        />
                      )}
                      <button className="btn-ghost" onClick={() => setView('portfolio')}>
                        Correct portfolio facts
                      </button>
                    </section>
                    <section>
                      <h2>Research fit</h2>
                      <p className="fit-note">
                        Where your work and the call use the same words. A starting point, not a
                        score.
                      </p>
                      {selected.dossier.evidence_pairs.length ? (
                        selected.dossier.evidence_pairs.map((p, i) => (
                          <details className="evidence-pair" key={i}>
                            <summary>
                              {p.claim} <span>Confirmed portfolio claim · keyword match</span>
                            </summary>
                            <div className="paired-passages">
                              <div>
                                <h3>Portfolio</h3>
                                <p>{p.claim}</p>
                                <small>
                                  Source: {p.source || 'Not attached — add a source in Portfolio'}
                                </small>
                              </div>
                              <div>
                                <h3>Call · {p.passage.section.replaceAll('_', ' ')}</h3>
                                <p>{p.passage.text}</p>
                              </div>
                            </div>
                          </details>
                        ))
                      ) : (
                        <p>
                          No evidence overlap found. Add and confirm relevant methods, assets or
                          trajectory in Portfolio.
                        </p>
                      )}
                    </section>
                    <section>
                      <h2>Call scope & expected outcomes</h2>
                      {selected.dossier.passages.map((p) => (
                        <details className="source-section" key={p.source_id}>
                          <summary>{p.section.replaceAll('_', ' ')}</summary>
                          <div className="scope-text">{p.text}</div>
                        </details>
                      ))}
                    </section>
                    <details className="tablet-scores">
                      <summary>Fit breakdown, assets & gaps</summary>
                      <ScorePanel call={selected} />
                    </details>
                  </div>
                  <div className="dossier-actions">
                    <div className="decision-actions">
                      <button
                        className="btn-sm"
                        onClick={() =>
                          void actOnCall(
                            selected.topic_code,
                            isSaved(decisions, 'call', selected.topic_code) ? 'unsave' : 'save',
                          )
                        }
                      >
                        {isSaved(decisions, 'call', selected.topic_code) ? 'Saved ✓' : 'Save call'}
                      </button>
                      <button
                        className="btn-ghost"
                        onClick={() =>
                          void actOnCall(
                            selected.topic_code,
                            isDismissed(decisions, 'call', selected.topic_code)
                              ? 'undo_dismiss'
                              : 'dismiss',
                          )
                        }
                      >
                        {isDismissed(decisions, 'call', selected.topic_code)
                          ? 'Undo dismiss'
                          : 'Dismiss'}
                      </button>
                    </div>
                    <div className="generation-actions">
                      <button
                        className="btn-sm"
                        disabled={busy || !health?.ready || !selected.scientific_fit}
                        onClick={() => void develop(selected.topic_code, true)}
                      >
                        Exploratory trial
                      </button>
                      <button
                        className="btn-primary"
                        disabled={
                          busy ||
                          selected.eligibility.state !== 'eligible' ||
                          !health?.ready ||
                          !selected.scientific_fit
                        }
                        onClick={() => void develop(selected.topic_code)}
                      >
                        {busy ? 'Starting…' : 'Develop 3 concepts'}
                      </button>
                    </div>
                    <p className="action-status">
                      {selected.eligibility.state !== 'eligible'
                        ? 'Check whether you can apply, then you can develop concepts. '
                        : !selected.scientific_fit
                          ? 'Confirm portfolio evidence first. '
                          : ''}
                      A trial run does not check whether you can apply.
                    </p>
                  </div>
                </>
              ) : (
                <p>Select a funding opportunity.</p>
              )}
            </main>
          </div>
        </>
      )}
    </div>
  );
}

function ScorePanel({ call }: { call: Call }) {
  return (
    <>
      <h2>Fit breakdown</h2>
      <section className="score-block">
        <h3>Scientific fit</h3>
        <strong>
          {call.scientific_fit ? `Rank ${call.scientific_fit.rank}` : 'Needs confirmed evidence'}
        </strong>
        {call.scientific_fit && (
          <p className="fit-note">
            BM25 {call.scientific_fit.bm25.toFixed(2)} · keyword overlap{' '}
            {call.scientific_fit.keyword_overlap.toFixed(2)} across{' '}
            {call.scientific_fit.n_portfolio_claims} confirmed claims. Retrieval signals, not
            funding probabilities.
          </p>
        )}
      </section>
      {(['capability', 'strategic', 'risk'] as const).map((k) => (
        <section className="score-block" key={k}>
          <h3>{k === 'risk' ? 'Evidence risks' : `${k[0].toUpperCase() + k.slice(1)} fit`}</h3>
          <strong>{call.dossier[k].label}</strong>
          <p className="fit-note">{call.dossier[k].explanation}</p>
        </section>
      ))}
      <section>
        <h3>Declared missing assets & partners</h3>
        {call.dossier.gaps.length ? (
          <ul>
            {call.dossier.gaps.map((g) => (
              <li key={g}>{g}</li>
            ))}
          </ul>
        ) : (
          <p className="fit-note">No gaps listed. That does not mean the team is complete.</p>
        )}
      </section>
    </>
  );
}
