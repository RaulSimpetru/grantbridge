import test from 'node:test';
import assert from 'node:assert/strict';
import { stripEvidenceIds } from '../src/evidence.ts';

test('cited evidence IDs leave the prose and the sentence still reads', () => {
  assert.equal(
    stripEvidenceIds(
      "The researcher's expertise is in dietary coaching (PROFILE_001), not imaging.",
    ),
    "The researcher's expertise is in dietary coaching, not imaging.",
  );
  assert.equal(
    stripEvidenceIds(
      'Digital biomarkers (PROFILE_002, CALL_SCOPE) and a pilot study [PROFILE_004 and ARCHIVE_3fa9c0d1e2b4a5968778abcd].',
    ),
    'Digital biomarkers and a pilot study.',
  );
  // A bare ID is part of the sentence; removing it would leave a hole.
  assert.equal(stripEvidenceIds('As required by CALL_SCOPE.'), 'As required by CALL_SCOPE.');
});
