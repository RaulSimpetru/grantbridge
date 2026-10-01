"""The model picker. What matters is that it can only select something Ollama
actually has, and that a pull is never started by accident."""

import pytest
from xnano import Terminal

from grant_recommender import models, models_panel

INSTALLED = [
	{'name': 'demo:12b-mlx', 'size': 8_000_000_000, 'parameters': '12B', 'installed': True},
	{'name': 'demo:14b', 'size': 9_000_000_000, 'parameters': '14B', 'installed': True},
	{'name': 'demo:8b', 'size': 5_000_000_000, 'parameters': '8B', 'installed': True},
]


@pytest.fixture
def picker(monkeypatch):
	monkeypatch.setattr(models, 'installed', lambda: INSTALLED)
	monkeypatch.setattr(models, 'active', lambda: 'demo:12b-mlx')
	monkeypatch.setattr(
		models_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	grid = models_panel.ModelPicker()
	terminal = Terminal.offscreen(cols=150, rows=30)
	terminal.attach_grid(grid)
	grid.open()
	grid.repaint()
	return grid, terminal


def test_it_lists_what_is_installed_with_sizes_and_marks_the_one_in_use(picker):
	grid, terminal = picker
	terminal.render(grid)
	out = terminal.get_output()

	assert 'demo:12b-mlx' in out
	assert '7.5 GB' in out  # 8e9 bytes is 7.5 GiB
	assert 'in use' in out
	assert 'downloaded' in out


def test_typing_filters_the_list(picker):
	grid, _ = picker
	grid.search.content = 'demo:1'

	assert [m['name'] for m in grid.visible()] == ['demo:12b-mlx', 'demo:14b']


def test_a_name_that_matches_nothing_is_offered_as_a_pull(picker):
	grid, terminal = picker
	grid.search.content = 'llama4:70b'
	grid.repaint()
	terminal.render(grid)

	entries = grid.visible()
	assert entries[0]['name'] == 'llama4:70b'
	assert entries[0]['installed'] is False
	assert 'not downloaded' in terminal.get_output()


def test_enter_on_an_installed_model_switches_to_it(picker, monkeypatch):
	grid, _ = picker
	chosen = []
	monkeypatch.setattr(models, 'select', lambda name: chosen.append(name))

	grid.select_next()
	grid.choose()

	assert chosen == ['demo:14b']
	assert 'now using demo:14b' in grid._status


def test_enter_on_a_model_that_is_not_installed_pulls_it(picker, monkeypatch):
	grid, _ = picker
	pulled = []
	monkeypatch.setattr(models, 'pull', lambda name, on_progress=None: pulled.append(name))
	monkeypatch.setattr(
		models, 'select', lambda name: pytest.fail('must not switch before pulling')
	)
	grid.search.content = 'llama4:70b'

	grid.choose()

	assert pulled == ['llama4:70b']
	# Downloading gigabytes then silently switching would be a surprise.
	assert 'enter again to use it' in grid._status


def test_a_failed_pull_is_reported_and_clears_the_busy_flag(picker, monkeypatch):
	grid, _ = picker

	def boom(name, on_progress=None):
		raise RuntimeError('no such model')

	monkeypatch.setattr(models, 'pull', boom)
	grid.search.content = 'nope:1b'

	grid.choose()

	assert 'pull failed' in grid._status
	assert grid.busy is False


def test_an_unreachable_ollama_is_reported_not_raised(monkeypatch):
	def boom():
		raise OSError('connection refused')

	monkeypatch.setattr(models, 'installed', boom)
	monkeypatch.setattr(
		models_panel.threading,
		'Thread',
		lambda target, args=(), daemon=None: type('T', (), {'start': lambda s: target(*args)})(),
	)
	grid = models_panel.ModelPicker()

	grid.open()

	assert 'could not reach Ollama' in grid._status


def test_selection_is_clamped_to_the_filtered_list(picker):
	grid, _ = picker
	grid.search.content = 'demo:8b'

	grid.select_next()
	grid.select_next()

	# One match, so there is nowhere to move to.
	assert grid.selected == 0


def test_closing_makes_the_search_box_inert_again(picker):
	grid, _ = picker
	assert grid.search.input is True

	grid.close()

	# Left capturing, it would swallow every keystroke on the screen beneath.
	assert grid.search.input is False
	assert grid.active is False


def test_human_size_reads_as_a_person_would_say_it():
	assert models.human_size(7_700_000_000) == '7.2 GB'
	assert models.human_size(500_000_000) == '477 MB'
	assert models.human_size(0) == ''


@pytest.mark.parametrize(
	('name', 'expected'),
	[
		('gemma4:12b-mlx', '12B'),
		('gemma4:26b-mlx', '26B'),
		('llama3.1:8b', '8B'),
		('qwen2.5:1.5b', '1.5B'),
		# A version number is not a parameter count.
		('qwen3.8:latest', ''),
		('mistral:latest', ''),
	],
)
def test_parameter_size_falls_back_to_the_tag(name, expected):
	"""Ollama leaves parameter_size empty for MLX builds and fills it for GGUF,
	so the column was blank for one and populated for the other."""
	assert models.parameters_from_name(name) == expected


def test_metadata_wins_over_the_name_when_both_exist(monkeypatch):
	class _Resp:
		status_code = 200

		def raise_for_status(self):
			pass

		def json(self):
			return {
				'models': [
					{'name': 'demo:14b', 'size': 1, 'details': {'parameter_size': '14.2B'}},
					{'name': 'demo:12b-mlx', 'size': 2, 'details': {'parameter_size': ''}},
				]
			}

	monkeypatch.setattr(models.requests, 'get', lambda *a, **k: _Resp())
	monkeypatch.setattr(models, 'base_url', lambda: 'http://x')

	by_name = {m['name']: m['parameters'] for m in models.installed()}

	# Metadata is more precise where it exists.
	assert by_name['demo:14b'] == '14.2B'
	assert by_name['demo:12b-mlx'] == '12B'


def test_every_row_uses_the_same_columns(picker):
	"""A model whose size Ollama does not report must not shunt the rest of the
	row sideways."""
	grid, terminal = picker
	grid._models = [
		{'name': 'known:1b', 'size': 1_000_000_000, 'parameters': '1.2B', 'installed': True},
		{'name': 'unknown:latest', 'size': 2_000_000_000, 'parameters': '', 'installed': True},
	]
	grid.repaint()
	terminal.render(grid)

	rows = [line for line in terminal.get_output().splitlines() if 'known' in line]
	assert len(rows) == 2
	# The trailing marker lands in the same column whether or not the row had a
	# parameter count, which is what makes the list read as a table.
	assert rows[0].index('downloaded') == rows[1].index('downloaded')
