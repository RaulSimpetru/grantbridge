import test from 'node:test';
import assert from 'node:assert/strict';
import { chosenConceptId, isSaved, isDismissed } from '../src/decisions.ts';
import type { Decision } from '../src/types.ts';

const event = (kind: Decision['kind'], target = 'run/1'): Decision => ({
  id: 'event',
  kind,
  target_type: 'concept',
  target_id: target,
  created_at: '2030-01-05',
  reason: null,
});
test('choosing another direction replaces the previous choice', () => {
  assert.equal(chosenConceptId([event('choose'), event('choose', 'run/2')], 'run'), '2');
});
test('dismissing the chosen direction clears selection', () => {
  assert.equal(chosenConceptId([event('choose'), event('dismiss')], 'run'), null);
});
test('undoing an old choice cannot clear a newer choice', () => {
  assert.equal(
    chosenConceptId([event('choose'), event('choose', 'run/2'), event('unchoose')], 'run'),
    '2',
  );
});
test('save and dismiss undo preserve independent decisions and runs', () => {
  const events = [
    event('save'),
    event('dismiss'),
    event('undo_dismiss'),
    event('choose', 'different/2'),
  ];
  assert.equal(isSaved(events, 'concept', 'run/1'), true);
  assert.equal(isDismissed(events, 'concept', 'run/1'), false);
  assert.equal(chosenConceptId(events, 'run'), null);
});
