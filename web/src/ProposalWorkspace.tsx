import { useCallback, useEffect, useRef, useState } from 'react';
import { archiveDocumentUrl, request } from './api';
import { stripEvidenceIds } from './evidence';
import { draftMarkdown, sections } from './proposalDraft';
import type { Citation, Content, Draft, Section } from './proposalDraft';
import type { ConceptJob, GenerationConnection } from './types';
import './proposal.css';

interface Preview {
  id: string;
  draft_id: string;
  section: Section;
  revision: number;
  status: 'preview' | 'running' | 'succeeded' | 'failed';
  accepted_at?: string;
  packet: { system_prompt: string; user_prompt: string };
  connection: GenerationConnection;
  result: Content | null;
  error: string | null;
}
const post = (body?: unknown): RequestInit => ({
  method: 'POST',
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});

// `origin` is a stored enum; say where the text came from in words instead.
function originLabel(origin: string): string {
  switch (origin) {
    case 'concept (unverified AI text)':
      return 'Carried over from the concept the model wrote.';
    case 'outline':
      return 'Nothing written here yet.';
    case 'edited by user':
      return 'Your own text.';
    case 'AI suggestion accepted by user':
      return 'A model suggestion you accepted.';
    default:
      return `From: ${origin}.`;
  }
}

// The same for a preview's status, in "A request for <section> …".
const previewState: Record<Preview['status'], string> = {
  preview: 'is waiting for your approval',
  running: 'is being drafted',
  succeeded: 'is ready to review',
  failed: 'failed',
};

function exportDraft(draft: Draft) {
  const text = draftMarkdown(draft);
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `proposal-${draft.id.slice(0, 8)}.md`;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Editor({ initial, onBack }: { initial: Draft; onBack: () => void }) {
  const root = useRef<HTMLElement>(null);
  const [draft, setDraft] = useState(initial);
  const [section, setSection] = useState<Section>('aims');
  const [text, setText] = useState(initial.sections.aims.text);
  const [missing, setMissing] = useState(initial.sections.aims.missing_info.join('\n'));
  const [citations, setCitations] = useState<Citation[]>(initial.sections.aims.citations);
  const [sourceIds, setSourceIds] = useState(Object.keys(initial.evidence));
  const [instructions, setInstructions] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [approved, setApproved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const saved = draft.sections[section];
  const dirty =
    text !== saved.text ||
    missing !== saved.missing_info.join('\n') ||
    JSON.stringify(citations) !== JSON.stringify(saved.citations);
  const generating = preview?.status === 'running';
  const changeSection = (key: Section, next = draft) => {
    setSection(key);
    setText(next.sections[key].text);
    setMissing(next.sections[key].missing_info.join('\n'));
    setCitations(next.sections[key].citations);
    setMessage('');
    setError('');
    setApproved(false);
  };
  useEffect(() => {
    request<Preview | null>(`/proposals/${initial.id}/latest-preview`)
      .then(setPreview)
      .catch((e) => setError(e.message));
  }, [initial.id]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    const guardNavigation = (e: MouseEvent) => {
      if (
        e.target instanceof Element &&
        e.target.closest('button, a') &&
        !root.current?.contains(e.target)
      ) {
        e.preventDefault();
        e.stopPropagation();
        setError('Save this section or discard its unsaved edits before leaving the editor.');
      }
    };
    window.addEventListener('beforeunload', warn);
    window.addEventListener('click', guardNavigation, true);
    return () => {
      window.removeEventListener('beforeunload', warn);
      window.removeEventListener('click', guardNavigation, true);
    };
  }, [dirty]);
  useEffect(() => {
    if (preview?.status !== 'running') return;
    const timer = window.setInterval(() => {
      request<Preview>(`/proposals/previews/${preview.id}`)
        .then(setPreview)
        .catch((e) => setError(`Connection interrupted: ${e.message}. Retrying…`));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [preview?.id, preview?.status]);
  const act = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const save = () =>
    act(async () => {
      const next = await request<Draft>(`/proposals/${draft.id}/sections/${section}`, {
        method: 'PUT',
        body: JSON.stringify({
          revision: draft.revision,
          text,
          citations,
          missing_info: missing
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean),
        }),
      });
      setDraft(next);
      changeSection(section, next);
      setMessage('Section saved on this computer.');
      setApproved(false);
    });
  return (
    <section ref={root} className="proposal-workspace" aria-label="Proposal editor">
      <div className="proposal-toolbar">
        <button className="btn-sm" disabled={dirty || busy} onClick={onBack}>
          All drafts
        </button>
        <span>
          Revision {draft.revision} ·{' '}
          {dirty ? 'Unsaved changes' : `Saved ${new Date(draft.updated_at).toLocaleString()}`}
        </span>
        <button className="btn-sm" disabled={dirty || busy} onClick={() => exportDraft(draft)}>
          Export draft & sources (.md)
        </button>
      </div>
      <h2>{draft.title}</h2>
      <p className="fit-note">{draft.notice}</p>
      {draft.trial && (
        <p className="pending-card">Exploratory trial — eligibility is not established.</p>
      )}
      {draft.research_question && (
        <p>
          <strong>Your research question:</strong> {draft.research_question}
        </p>
      )}
      <div className="proposal-layout">
        <nav className="proposal-sections" aria-label="Proposal sections">
          {(Object.entries(sections) as [Section, string][]).map(([key, label]) => (
            <button
              key={key}
              className={section === key ? 'proposal-section active' : 'proposal-section'}
              aria-current={section === key ? 'step' : undefined}
              disabled={dirty || busy}
              onClick={() => changeSection(key)}
            >
              {label}
              <small>
                {draft.sections[key].text ? 'Working text' : 'Not drafted'} ·{' '}
                {draft.sections[key].missing_info.length} unresolved
              </small>
            </button>
          ))}
        </nav>
        <div className="proposal-editor">
          <h3>{sections[section]}</h3>
          <label>
            Working text
            <textarea
              aria-label="Section text"
              rows={16}
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                setApproved(false);
              }}
            />
          </label>
          <p className="fit-note">
            {originLabel(saved.origin)} Editing it here does not check whether any of it is true.
          </p>
          <label>
            What you still need to find out, one per line
            <textarea
              rows={4}
              value={missing}
              onChange={(e) => {
                setMissing(e.target.value);
                setApproved(false);
              }}
            />
          </label>
          <details>
            <summary>Section citations ({citations.length})</summary>
            {citations.map((c, i) => (
              <div className="proposal-citation" key={i}>
                <label>
                  Source
                  <select
                    value={c.source_id}
                    onChange={(e) =>
                      setCitations(
                        citations.map((v, n) =>
                          n === i ? { ...v, source_id: e.target.value } : v,
                        ),
                      )
                    }
                  >
                    {Object.keys(draft.evidence).map((id) => (
                      <option key={id}>{id}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Claim supported
                  <input
                    value={c.claim_supported}
                    onChange={(e) =>
                      setCitations(
                        citations.map((v, n) =>
                          n === i ? { ...v, claim_supported: e.target.value } : v,
                        ),
                      )
                    }
                  />
                </label>
                <button
                  className="btn-sm"
                  onClick={() => setCitations(citations.filter((_, n) => n !== i))}
                >
                  Remove citation {i + 1}
                </button>
              </div>
            ))}
            <button
              className="btn-sm"
              onClick={() =>
                setCitations([
                  ...citations,
                  { source_id: Object.keys(draft.evidence)[0], claim_supported: '' },
                ])
              }
            >
              Add citation
            </button>
          </details>
          <div className="proposal-toolbar">
            <button className="btn-primary" disabled={!dirty || busy} onClick={save}>
              {busy ? 'Working…' : 'Save section'}
            </button>
            {dirty && (
              <button className="btn-sm" onClick={() => changeSection(section)}>
                Discard unsaved edits
              </button>
            )}
          </div>
          {error && (
            <p className="error-note" role="alert">
              {error}
            </p>
          )}
          {message && <p role="status">{message}</p>}
          <section className="proposal-assistant" aria-label="Section drafting assistant">
            <h3>Draft this section</h3>
            <p>A suggestion stays separate until you accept it. Save your edits first.</p>
            <label>
              Direction for this section
              <textarea
                rows={3}
                maxLength={4000}
                value={instructions}
                onChange={(e) => {
                  setInstructions(e.target.value);
                  setApproved(false);
                }}
                placeholder="What should this section emphasise? What do you already know?"
              />
            </label>
            <details>
              <summary>Sources to include ({sourceIds.length})</summary>
              {Object.entries(draft.evidence).map(([id, source]) => (
                <div className="proposal-source" key={id}>
                  <label>
                    <input
                      type="checkbox"
                      checked={sourceIds.includes(id)}
                      onChange={(e) => {
                        setSourceIds(
                          e.target.checked ? [...sourceIds, id] : sourceIds.filter((v) => v !== id),
                        );
                        setApproved(false);
                      }}
                    />
                    {id} · {source.section.replaceAll('_', ' ')}
                  </label>
                  <details>
                    <summary>Read source passage</summary>
                    <p>{source.text}</p>
                    {draft.archive_context?.passages
                      .filter((p) => p.source_id === id)
                      .map((p) => (
                        <p key={p.source_id}>
                          <a href={archiveDocumentUrl(p.document_id)}>{p.document_name} ↗</a> · text
                          lines {p.line_start}–{p.line_end} · historical inspiration only
                        </p>
                      ))}
                  </details>
                </div>
              ))}
            </details>
            <button
              className="btn-sm btn-sm-primary"
              disabled={busy || dirty || generating || !sourceIds.length}
              onClick={() =>
                act(async () => {
                  setPreview(
                    await request<Preview>(
                      `/proposals/${draft.id}/preview`,
                      post({
                        section,
                        revision: draft.revision,
                        instructions,
                        source_ids: sourceIds,
                      }),
                    ),
                  );
                  setApproved(false);
                })
              }
            >
              Show me what would be sent
            </button>
            {preview && preview.section !== section && (
              <p>
                A request for {sections[preview.section]} {previewState[preview.status]}. Open that
                section to review it.
              </p>
            )}
            {preview && preview.section === section && (
              <div className="proposal-preview">
                <h4>Request preview · {sections[preview.section]}</h4>
                <p>
                  Destination: <strong>{preview.connection.base_url}</strong> ·{' '}
                  {preview.connection.model}
                </p>
                {preview.connection.tunnel_host && (
                  <p>SSH destination: {preview.connection.tunnel_host}</p>
                )}
                <p>
                  This sends the chosen concept, original research question, this saved section,
                  your instructions and the selected source passages. Other edited sections are not
                  sent.
                </p>
                <details>
                  <summary>Exact instructions & content sent to the model</summary>
                  <pre>
                    {preview.packet.system_prompt}
                    {'\n\n'}
                    {preview.packet.user_prompt}
                  </pre>
                </details>
                {preview.revision !== draft.revision && (
                  <p className="pending-card">
                    The draft changed since this preview. Make a fresh preview; this suggestion
                    cannot replace newer edits.
                  </p>
                )}
                {preview.status === 'preview' && (
                  <>
                    <label className="proposal-consent">
                      <input
                        type="checkbox"
                        checked={approved}
                        onChange={(e) => setApproved(e.target.checked)}
                      />
                      I approve sending exactly this preview to the destination shown above.
                    </label>
                    <button
                      className="btn-primary"
                      disabled={!approved || dirty || busy || preview.revision !== draft.revision}
                      onClick={() =>
                        act(async () => {
                          setPreview(
                            await request<Preview>(
                              `/proposals/previews/${preview.id}/generate`,
                              post({ approved: true }),
                            ),
                          );
                          setApproved(false);
                        })
                      }
                    >
                      Send & draft this section
                    </button>
                  </>
                )}
                {preview.status === 'running' && (
                  <p role="status">
                    Drafting. Your saved text is untouched, and you can come back to this later.
                  </p>
                )}
                {preview.status === 'failed' && (
                  <p className="error-note" role="alert">
                    {preview.error}
                  </p>
                )}
                {preview.status === 'succeeded' && preview.result && (
                  <>
                    <h4>Suggested text. Not saved.</h4>
                    <div className="proposal-suggestion">{preview.result.text}</div>
                    <h4>Still to confirm</h4>
                    <ul>
                      {preview.result.missing_info.map((q, i) => (
                        <li key={i}>{q}</li>
                      ))}
                    </ul>
                    <h4>Citation claims — verify against the passages</h4>
                    {preview.result.citations.map((c, i) => (
                      <p key={i}>
                        {c.source_id}: {c.claim_supported}
                      </p>
                    ))}
                    <button
                      className="btn-primary"
                      disabled={dirty || busy || preview.revision !== draft.revision}
                      onClick={() =>
                        act(async () => {
                          const next = await request<Draft>(
                            `/proposals/previews/${preview.id}/accept`,
                            post(),
                          );
                          setDraft(next);
                          changeSection(section, next);
                          setPreview(null);
                          setMessage(
                            'Suggestion applied and saved. Please review its claims and unresolved information.',
                          );
                        })
                      }
                    >
                      Use this text
                    </button>
                    <button
                      className="btn-sm"
                      onClick={() =>
                        act(async () => {
                          // Recorded on the server, or reopening the draft brings it back.
                          await request(`/proposals/previews/${preview.id}/discard`, post());
                          setPreview(null);
                        })
                      }
                    >
                      Discard it and keep my text
                    </button>
                  </>
                )}
              </div>
            )}
          </section>
        </div>
      </div>
    </section>
  );
}

export default function ProposalWorkspace({
  job,
  chosenId,
}: {
  job: ConceptJob;
  chosenId: string | null;
}) {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [active, setActive] = useState<Draft | null>(null);
  const [conceptId, setConceptId] = useState(chosenId ?? '');
  const [variant, setVariant] = useState<'original' | 'revised'>('original');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const refresh = useCallback(
    () =>
      request<Draft[]>(`/proposals?job_id=${encodeURIComponent(job.id)}`)
        .then(setDrafts)
        .catch((e) => setError(e.message)),
    [job.id],
  );
  useEffect(() => {
    refresh();
  }, [refresh]);
  if (active)
    return (
      <Editor
        key={active.id}
        initial={active}
        onBack={() => {
          setActive(null);
          refresh();
        }}
      />
    );
  const candidates = variant === 'original' ? (job.draft?.concepts ?? []) : (job.concepts ?? []);
  const chosen = candidates.find((c) => c.concept_id === conceptId);
  return (
    <section className="proposal-workspace">
      <h2>Proposal drafts</h2>
      <p>
        Pick a concept and work through aims, methods, work packages, risks and impact. Opening a
        draft does not contact the model.
      </p>
      {drafts.length > 0 && (
        <div className="proposal-list">
          {drafts.map((d) => (
            <button className="proposal-section" key={d.id} onClick={() => setActive(d)}>
              <strong>{d.title}</strong>
              <small>
                {d.variant} concept · revision {d.revision} · open saved draft
              </small>
            </button>
          ))}
        </div>
      )}
      <div className="proposal-picker">
        <label>
          Starting version
          <select
            value={variant}
            onChange={(e) => setVariant(e.target.value as 'original' | 'revised')}
          >
            <option value="original">Original concept — before critique</option>
            <option value="revised">Revised concept</option>
          </select>
        </label>
        <label>
          Research direction
          <select value={conceptId} onChange={(e) => setConceptId(e.target.value)}>
            <option value="">Choose a direction…</option>
            {candidates.map((c) => (
              <option key={c.concept_id} value={c.concept_id}>
                {c.title}
              </option>
            ))}
          </select>
        </label>
      </div>
      {chosen && (
        <div className="pending-card">
          <strong>{chosen.title}</strong>
          <p>{stripEvidenceIds(chosen.central_hypothesis)}</p>
          <ul>
            {chosen.objectives.map((o, i) => (
              <li key={i}>{stripEvidenceIds(o)}</li>
            ))}
          </ul>
        </div>
      )}
      {!job.revision_audit && (
        <p className="fit-note">
          This older run predates intent protection. Compare its original and revised concepts
          before selecting: revision may have changed the research question.
        </p>
      )}
      <button
        className="btn-primary"
        disabled={busy || !chosen}
        onClick={async () => {
          setBusy(true);
          setError('');
          try {
            setActive(
              await request<Draft>(
                '/proposals',
                post({ job_id: job.id, concept_id: conceptId, variant }),
              ),
            );
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? 'Opening…' : 'Open drafting workspace'}
      </button>
      {error && (
        <p className="error-note" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
