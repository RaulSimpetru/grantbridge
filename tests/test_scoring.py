from grant_recommender.scoring import score_calls

CALLS = [
	{'topic_code': 'A', 'scope_text': 'Glycaemic variability with wearable glucose monitors.'},
	{
		'topic_code': 'B',
		'scope_text': 'Battery recycling and circular economy for electric vehicles.',
	},
	{'topic_code': 'C', 'scope_text': 'Cultural heritage digitisation and access.'},
]


def test_score_calls_ranks_the_closer_call_first():
	portfolio = [
		{
			'claim': 'Glycaemic variability with glucose monitors',
			'included': True,
			'confidence': 'confirmed',
		}
	]

	result = score_calls(CALLS, portfolio)

	assert result['A']['rank'] == 1
	assert {result['A']['rank'], result['B']['rank'], result['C']['rank']} == {1, 2, 3}
	assert all(r['n_portfolio_claims'] == 1 for r in result.values())


def test_score_calls_returns_none_for_every_call_with_an_empty_portfolio():
	assert score_calls(CALLS, []) == dict.fromkeys(['A', 'B', 'C'], None)


def test_score_calls_excludes_assertions_marked_not_included():
	portfolio = [{'claim': 'Glycaemic variability with glucose monitors', 'included': False}]

	assert score_calls(CALLS, portfolio) == dict.fromkeys(['A', 'B', 'C'], None)


def test_score_calls_with_no_calls_returns_empty():
	assert score_calls([], [{'claim': 'anything', 'included': True}]) == {}


def test_score_calls_excludes_assertions_marked_missing_confidence():
	portfolio = [
		{
			'claim': 'Glycaemic variability with glucose monitors',
			'included': True,
			'confidence': 'missing',
		}
	]

	assert score_calls(CALLS, portfolio) == dict.fromkeys(['A', 'B', 'C'], None)
