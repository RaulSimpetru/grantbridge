import type { Decision } from './types';

// Folds the append-only decision log into current UI state. An "undo" is a
// later event of the off-kind, never a rewrite -- see decisions.py.
export function isActive(
  decisions: Decision[],
  targetType: Decision['target_type'],
  targetId: string,
  onKind: Decision['kind'],
  offKind: Decision['kind'],
): boolean {
  let active = false;
  for (const d of decisions) {
    if (d.target_type !== targetType || d.target_id !== targetId) continue;
    if (d.kind === onKind) active = true;
    else if (d.kind === offKind) active = false;
  }
  return active;
}

export function isSaved(
  decisions: Decision[],
  targetType: Decision['target_type'],
  targetId: string,
): boolean {
  return isActive(decisions, targetType, targetId, 'save', 'unsave');
}

export function isDismissed(
  decisions: Decision[],
  targetType: Decision['target_type'],
  targetId: string,
): boolean {
  return isActive(decisions, targetType, targetId, 'dismiss', 'undo_dismiss');
}

// At most one concept per job is chosen: the latest choose/unchoose event
// naming any concept in the job wins, regardless of which concept it names.
export function chosenConceptId(decisions: Decision[], jobId: string): string | null {
  let chosen: string | null = null;
  const prefix = `${jobId}/`;
  for (const d of decisions) {
    if (d.target_type !== 'concept' || !d.target_id.startsWith(prefix)) continue;
    if (d.kind === 'choose') chosen = d.target_id.slice(prefix.length);
    else if (d.kind === 'unchoose' && chosen === d.target_id.slice(prefix.length)) chosen = null;
    else if (d.kind === 'dismiss' && chosen === d.target_id.slice(prefix.length)) chosen = null;
  }
  return chosen;
}
