"""Preserve source layout as a small, inert tree, never executable source HTML."""

from html.parser import HTMLParser
from urllib.parse import urlsplit

TAGS = {
	'div',
	'p',
	'h1',
	'h2',
	'h3',
	'h4',
	'h5',
	'h6',
	'strong',
	'em',
	'a',
	'ul',
	'ol',
	'li',
	'br',
	'table',
	'thead',
	'tbody',
	'tr',
	'th',
	'td',
	'blockquote',
	'sup',
	'sub',
}
SKIP = {'script', 'style', 'iframe', 'object', 'svg', 'template'}
VOID = {
	'area',
	'base',
	'br',
	'col',
	'embed',
	'hr',
	'img',
	'input',
	'link',
	'meta',
	'param',
	'source',
	'track',
	'wbr',
}


class SourceParser(HTMLParser):
	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.nodes = []
		self.stack = [('', self.nodes, False)]

	def handle_starttag(self, tag, attrs):
		_, target, skipped = self.stack[-1]
		skipped = skipped or tag in SKIP
		kind = {'b': 'strong', 'i': 'em'}.get(tag, tag)
		if not skipped and kind in TAGS:
			node = {'type': kind, 'children': []}
			for attr in {
				'ol': ['start'],
				'li': ['value'],
				'td': ['colspan', 'rowspan'],
				'th': ['colspan', 'rowspan'],
			}.get(kind, []):
				value = dict(attrs).get(attr) or ''
				if value.isascii() and value.isdigit() and len(value) <= 5 and int(value) <= 10000:
					node[attr] = int(value)
			if kind == 'a':
				href = (dict(attrs).get('href') or '').strip()
				try:
					url = urlsplit(href)
					if (
						url.scheme in ('https', 'http')
						and url.hostname
						and not url.username
						and not url.password
					):
						node['href'] = href
				except ValueError:
					pass
			target.append(node)
			target = node['children']
		if tag not in VOID:
			self.stack.append((tag, target, skipped))

	def handle_endtag(self, tag):
		for index in range(len(self.stack) - 1, 0, -1):
			if self.stack[index][0] == tag:
				del self.stack[index:]
				break

	def handle_data(self, data):
		if not self.stack[-1][2]:
			text = data.replace('\ufeff', '').replace('\u00a0', ' ')
			if text:
				self.stack[-1][1].append({'type': 'text', 'text': text})


def source_document(html: str) -> list[dict]:
	parser = SourceParser()
	parser.feed(html)
	parser.close()
	return parser.nodes
