import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getGenerationSettings,
  resetGenerationSettings,
  saveGenerationSettings,
  testGenerationConnection,
} from './api';
import type { GenerationConnection, GenerationHealth } from './types';

export default function ConnectionSettings({
  onClose,
  onActiveChanged,
}: {
  onClose: () => void;
  onActiveChanged: (health: GenerationHealth) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [active, setActive] = useState<GenerationConnection | null>(null);
  const [url, setUrl] = useState('');
  const [model, setModel] = useState('');
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [result, setResult] = useState<GenerationHealth | null>(null);
  const [message, setMessage] = useState('');
  const load = useCallback(async () => {
    try {
      const settings = await getGenerationSettings();
      setActive(settings);
      setUrl(settings.base_url);
      setModel(settings.model);
      setError('');
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    // The loader updates state only after the settings request resolves.
    // oxlint-disable-next-line react/set-state-in-effect
    void load();
    return () => element?.close();
  }, [load]);
  const change = () => {
    setResult(null);
    setMessage('');
    setError('');
  };
  const run = async (action: 'test' | 'save' | 'reset') => {
    setBusy(action);
    setError('');
    setResult(null);
    setMessage('');
    try {
      const config = { base_url: url, model };
      const health =
        action === 'test'
          ? await testGenerationConnection(config)
          : action === 'save'
            ? await saveGenerationSettings(config)
            : await resetGenerationSettings();
      setResult(health);
      setModels(health.models ?? []);
      if (action !== 'test') {
        onActiveChanged(health);
        await load();
        setMessage(
          action === 'save'
            ? 'Saved. New concept runs will use this connection.'
            : 'Launcher connection restored.',
        );
      } else {
        setMessage('Test only — your active connection has not changed.');
      }
    } catch (err) {
      setError(`${(err as Error).message} No new connection was saved.`);
    } finally {
      setBusy('');
    }
  };
  return (
    <dialog
      ref={dialog}
      className="connection-dialog"
      aria-labelledby="connection-title"
      onCancel={onClose}
    >
      <div className="connection-heading">
        <h2 id="connection-title">Generation connection</h2>
        <button
          className="btn-ghost"
          type="button"
          onClick={onClose}
          aria-label="Close connection settings"
        >
          ×
        </button>
      </div>
      <p>
        Choose the Ollama server that generates your concepts. The address is reached from the
        GrantBridge API server, not your browser.
      </p>
      {!active ? (
        <p>
          {error || 'Loading connection settings…'}{' '}
          {error && <button onClick={() => void load()}>Retry</button>}
        </p>
      ) : (
        <>
          <div className="connection-active">
            <strong>Active connection</strong>
            <p>
              {active.base_url}
              <br />
              {active.model}
            </p>
            <p>
              {active.tunnel_host
                ? `SSH tunnel to ${active.tunnel_host} over your network / VPN.`
                : 'Direct HTTP(S) endpoint.'}{' '}
              {active.source === 'saved'
                ? 'Saved on this API server.'
                : 'Configured by the API launcher.'}
            </p>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run('save');
            }}
          >
            <fieldset disabled={Boolean(busy)}>
              <label>
                Ollama server URL
                <input
                  type="url"
                  required
                  value={url}
                  placeholder="https://ollama.example.org"
                  onChange={(e) => {
                    setUrl(e.target.value);
                    setModels([]);
                    change();
                  }}
                  aria-describedby="server-url-help"
                />
              </label>
              <p id="server-url-help">
                Use the base URL, without /api/chat. Local Ollama: http://127.0.0.1:11434. An SSH
                tunnel must already be running; this form does not open or reconfigure tunnels.
              </p>
              <label>
                Model
                <input
                  required
                  value={model}
                  list="ollama-models"
                  onChange={(e) => {
                    setModel(e.target.value);
                    change();
                  }}
                  placeholder={active?.model}
                />
              </label>
              <datalist id="ollama-models">
                {models.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
              {models.length > 0 && (
                <label>
                  Models available on the tested server
                  <select
                    value={models.includes(model) ? model : ''}
                    onChange={(e) => {
                      setModel(e.target.value);
                      change();
                    }}
                  >
                    <option value="" disabled>
                      Choose an installed model
                    </option>
                    {models.map((name) => (
                      <option key={name}>{name}</option>
                    ))}
                  </select>
                </label>
              )}
              <p>
                Only use a server you trust. Saving routes call text and confirmed portfolio
                evidence there when you generate concepts. Use HTTPS or an SSH/VPN tunnel for remote
                servers. Credentials in URLs are not supported.
              </p>
              <div className="connection-buttons">
                <button type="button" className="btn-sm" onClick={() => void run('test')}>
                  {busy === 'test' ? 'Checking…' : 'Test connection'}
                </button>
                <button type="submit" className="btn-primary">
                  {busy === 'save' ? 'Testing & saving…' : 'Save connection'}
                </button>
                <button type="button" className="btn-ghost" onClick={() => void run('reset')}>
                  {busy === 'reset' ? 'Restoring…' : 'Restore launcher settings'}
                </button>
              </div>
            </fieldset>
          </form>
          <div
            aria-live="polite"
            aria-atomic="true"
            className={`connection-result ${result?.ready ? 'connection-success' : ''}`}
          >
            {busy && (
              <p>
                {busy === 'reset'
                  ? 'Restoring launcher settings…'
                  : 'Contacting the server and checking its model list…'}
              </p>
            )}
            {result && (
              <>
                <strong>{result.ready ? 'Connected' : 'Connection not ready'}</strong>
                <p>{result.detail}</p>
                <p>
                  {result.base_url} · {result.model}
                </p>
                <small>
                  Checked{' '}
                  {result.checked_at
                    ? new Date(result.checked_at).toLocaleTimeString()
                    : 'just now'}
                  {result.latency_ms !== undefined && ` · ${result.latency_ms} ms`}. This checks
                  model availability, not a full generation run.
                </small>
              </>
            )}
            {message && <p>{message}</p>}
          </div>
        </>
      )}
      {error && active && (
        <p role="alert" className="error-note">
          {error}
        </p>
      )}
    </dialog>
  );
}
