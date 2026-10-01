import { useEffect, useState } from 'react';
import {
  createAssertion,
  getResearcherProfile,
  importFromOpenAlex,
  listPortfolio,
  updateAssertion,
} from './api';
import type { ResearcherProfile } from './api';
import type { Assertion, Component, Confidence } from './types';
import { COMPONENTS, CONFIDENCES } from './types';

const COMPONENT_LABELS: Record<Component, string> = {
  topics_trajectory: 'Topics & trajectory',
  methods: 'Methods',
  software: 'Software',
  datasets: 'Datasets',
  infrastructure: 'Infrastructure',
  preliminary_findings: 'Preliminary findings',
  populations_access: 'Populations & access',
  collaborators: 'Collaborators',
  career_eligibility: 'Career & eligibility',
  prior_proposals: 'Prior proposals (private)',
};

const CONFIDENCE_LABELS: Record<Confidence, string> = {
  confirmed: 'Confirmed',
  inferred: 'Inferred',
  needs_review: 'Needs review',
  missing: 'Missing',
};

function AddAssertionForm({ onAdded }: { onAdded: (a: Assertion) => void }) {
  const [component, setComponent] = useState<Component>('methods');
  const [claim, setClaim] = useState('');
  const [source, setSource] = useState('');
  const [confidence, setConfidence] = useState<Confidence>('confirmed');
  const [phdDate, setPhdDate] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!claim.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createAssertion({
        component,
        claim: claim.trim(),
        source: source.trim() || undefined,
        confidence,
        phd_award_date: component === 'career_eligibility' && phdDate ? phdDate : undefined,
      });
      onAdded(created);
      setClaim('');
      setSource('');
      setPhdDate('');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="portfolio-form" onSubmit={submit}>
      <div className="section-title">Add an assertion</div>
      <div className="portfolio-form-row">
        <select
          aria-label="Portfolio component"
          value={component}
          onChange={(e) => setComponent(e.target.value as Component)}
        >
          {COMPONENTS.map((c) => (
            <option key={c} value={c}>
              {COMPONENT_LABELS[c]}
            </option>
          ))}
        </select>
        <select
          aria-label="Assertion confidence"
          value={confidence}
          onChange={(e) => setConfidence(e.target.value as Confidence)}
        >
          {CONFIDENCES.map((c) => (
            <option key={c} value={c}>
              {CONFIDENCE_LABELS[c]}
            </option>
          ))}
        </select>
      </div>
      <input
        type="text"
        aria-label="Portfolio claim"
        placeholder="Claim (e.g. 'Glycaemic variability from glucose monitors')"
        value={claim}
        onChange={(e) => setClaim(e.target.value)}
      />
      <input
        type="text"
        aria-label="Assertion source"
        placeholder="Source (e.g. 'CV', 'a paper DOI') — optional"
        value={source}
        onChange={(e) => setSource(e.target.value)}
      />
      {component === 'career_eligibility' && (
        <label>
          PhD award date (optional; full date)
          <input
            type="date"
            value={phdDate}
            max={new Date().toISOString().slice(0, 10)}
            onChange={(e) => setPhdDate(e.target.value)}
          />
          <small>
            Stored in your private eligibility profile; confirm against your award document.
          </small>
        </label>
      )}
      <button className="btn-primary" type="submit" disabled={busy || !claim.trim()}>
        Add assertion
      </button>
      {error && <div className="error-note">{error}</div>}
    </form>
  );
}

function ImportBar({ onImported }: { onImported: (added: Assertion[]) => void }) {
  const [authorId, setAuthorId] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const runImport = async () => {
    if (!authorId.trim()) return;
    setBusy(true);
    setMessage(null);
    try {
      const added = await importFromOpenAlex(authorId.trim());
      onImported(added);
      setMessage(
        added.length > 0
          ? `Imported ${added.length} topic${added.length === 1 ? '' : 's'} as "inferred" — review each below.`
          : 'Nothing new to import (already in your portfolio, or the profile has no tagged topics).',
      );
    } catch (err) {
      setMessage((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="import-bar">
      <input
        type="text"
        placeholder="OpenAlex author ID or profile URL"
        aria-label="OpenAlex author ID"
        value={authorId}
        onChange={(e) => setAuthorId(e.target.value)}
      />
      <button className="btn-primary" onClick={runImport} disabled={busy || !authorId.trim()}>
        Import topics from OpenAlex
      </button>
      {message && <div className="import-message">{message}</div>}
    </div>
  );
}

function AssertionCard({
  assertion,
  onChange,
}: {
  assertion: Assertion;
  onChange: (updated: Assertion) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [claim, setClaim] = useState(assertion.claim);
  const [source, setSource] = useState(assertion.source ?? '');
  const [phdDate, setPhdDate] = useState(assertion.phd_award_date ?? '');
  const [error, setError] = useState('');
  const patch = async (fields: Partial<Assertion>) => {
    setError('');
    try {
      const updated = await updateAssertion(assertion.id, fields);
      onChange(updated);
      setEditing(false);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="assertion-card">
      <div className={`confidence-badge confidence-${assertion.confidence}`}>
        {CONFIDENCE_LABELS[assertion.confidence]}
      </div>
      <div className="assertion-claim">{assertion.claim}</div>
      {assertion.source && <div className="assertion-source">{assertion.source}</div>}
      {error && <p role="alert">{error}</p>}
      {assertion.phd_award_date && <p>PhD awarded: {assertion.phd_award_date}</p>}
      {editing && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void patch({
              claim: claim.trim(),
              source: source.trim() || null,
              ...(assertion.component === 'career_eligibility'
                ? { phd_award_date: phdDate || null }
                : {}),
            });
          }}
        >
          <label>
            Claim
            <textarea required value={claim} onChange={(e) => setClaim(e.target.value)} />
          </label>
          <label>
            Source passage or reference
            <input value={source} onChange={(e) => setSource(e.target.value)} />
          </label>
          {assertion.component === 'career_eligibility' && (
            <label>
              PhD award date
              <input
                type="date"
                max={new Date().toISOString().slice(0, 10)}
                value={phdDate}
                onChange={(e) => setPhdDate(e.target.value)}
              />
            </label>
          )}
          <button className="btn-primary" disabled={!claim.trim()}>
            Save correction
          </button>
          <button className="btn-ghost" type="button" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </form>
      )}
      <div className="assertion-controls">
        <button
          className="btn-toggle"
          onClick={() => {
            setClaim(assertion.claim);
            setSource(assertion.source ?? '');
            setEditing(true);
          }}
        >
          Edit claim & source
        </button>
        <select
          aria-label={`Review status for ${assertion.claim}`}
          value={assertion.confidence}
          onChange={(e) => patch({ confidence: e.target.value as Confidence })}
        >
          {CONFIDENCES.map((c) => (
            <option key={c} value={c}>
              {CONFIDENCE_LABELS[c]}
            </option>
          ))}
        </select>
        <button
          className="btn-toggle"
          onClick={() =>
            patch({ visibility: assertion.visibility === 'public' ? 'private' : 'public' })
          }
        >
          {assertion.visibility === 'public' ? 'Public' : 'Private'}
        </button>
        <button
          className="btn-toggle"
          title="Use this fact when matching calls and drafting"
          onClick={() => patch({ included: !assertion.included })}
        >
          {assertion.included
            ? assertion.confidence === 'confirmed'
              ? 'Used in recommendations'
              : 'Awaiting confirmation'
            : 'Excluded from recommendations'}
        </button>
      </div>
    </div>
  );
}

function ModelContext({ assertions }: { assertions: Assertion[] }) {
  const [profile, setProfile] = useState<ResearcherProfile | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let current = true;
    getResearcherProfile()
      .then((data) => {
        if (current) {
          setProfile(data);
          setError('');
        }
      })
      .catch((err) => {
        if (current) setError((err as Error).message);
      });
    return () => {
      current = false;
    };
  }, [assertions]);
  return (
    <section className="profile-context">
      <h2>Your research, in context</h2>
      <p>
        Only the facts you have confirmed are sent with a call. Nothing else on this computer is
        read.
      </p>
      {error ? (
        <p role="alert">Could not load the current model context: {error}</p>
      ) : profile ? (
        <>
          <p>
            <strong>
              {profile.used_count} confirmed {profile.used_count === 1 ? 'fact' : 'facts'}
            </strong>{' '}
            ready for the model. {profile.not_used_count} withheld.
          </p>
          <div className="profile-facts">
            {profile.assertions
              .filter((a) => a.included && a.confidence === 'confirmed')
              .map((a) => (
                <div className="profile-fact" key={a.id}>
                  <span>{COMPONENT_LABELS[a.component]}</span>
                  <p>{a.claim}</p>
                </div>
              ))}
          </div>
          <details>
            <summary>See exactly what gets sent</summary>
            <p>
              This is everything the model is given. It is not a window into what it already knows.
            </p>
            <pre className="profile-prompt">
              {profile.context || 'No confirmed, included researcher evidence yet.'}
            </pre>
            <p>
              Sent to {profile.connection.model} at {profile.connection.base_url}. “Private” is a
              visibility label; confirmed, included facts are still sent to this model.
            </p>
            <p>Changes apply to new runs. Earlier runs keep the evidence they were given.</p>
          </details>
        </>
      ) : (
        <p>Loading the model’s input…</p>
      )}
    </section>
  );
}

export default function Portfolio({
  onUpdated,
  onPastApplications,
}: {
  onUpdated: () => void;
  onPastApplications: () => void;
}) {
  const [assertions, setAssertions] = useState<Assertion[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listPortfolio()
      .then(setAssertions)
      .catch((err: Error) => setError(err.message));
  }, []);

  const replaceOne = (updated: Assertion) => {
    setAssertions((current) => (current ?? []).map((a) => (a.id === updated.id ? updated : a)));
    onUpdated();
  };
  const appendMany = (added: Assertion[]) => {
    setAssertions((current) => [...(current ?? []), ...added]);
    onUpdated();
  };

  if (error) return <div className="error-note">Couldn't load your portfolio: {error}</div>;
  if (assertions === null) return <div className="empty-note">Loading your portfolio…</div>;

  return (
    <div className="portfolio-scroll">
      <div className="portfolio-intro">
        <h1 className="dossier-title">Researcher profile</h1>
        <button className="btn-sm" onClick={onPastApplications}>
          Past applications ↗
        </button>
        <p className="portfolio-note">
          Tell GrantBridge what you can do, and where each fact came from. Only what you confirm
          reaches the model.
        </p>
      </div>

      <ModelContext assertions={assertions} />
      <h2>Review & edit your evidence</h2>
      <ImportBar onImported={appendMany} />

      {COMPONENTS.filter((component) => assertions.some((a) => a.component === component)).map(
        (component) => {
          const items = assertions.filter((a) => a.component === component);
          return (
            <div className="portfolio-section" key={component}>
              <div className="section-title">{COMPONENT_LABELS[component]}</div>
              {items.length === 0 ? (
                <div className="tag-pending">
                  No {COMPONENT_LABELS[component].toLowerCase()} assertions yet — missing, not
                  estimated.
                </div>
              ) : (
                <div className="assertion-grid">
                  {items.map((a) => (
                    <AssertionCard key={a.id} assertion={a} onChange={replaceOne} />
                  ))}
                </div>
              )}
            </div>
          );
        },
      )}

      {COMPONENTS.some((component) => !assertions.some((a) => a.component === component)) && (
        <details className="profile-missing">
          <summary>
            Complete your profile ·{' '}
            {
              COMPONENTS.filter((component) => !assertions.some((a) => a.component === component))
                .length
            }{' '}
            areas not yet recorded
          </summary>
          <p>Nothing recorded here yet. Add it below.</p>
          <ul>
            {COMPONENTS.filter(
              (component) => !assertions.some((a) => a.component === component),
            ).map((component) => (
              <li key={component}>{COMPONENT_LABELS[component]}</li>
            ))}
          </ul>
        </details>
      )}

      <AddAssertionForm onAdded={(a) => appendMany([a])} />
    </div>
  );
}
