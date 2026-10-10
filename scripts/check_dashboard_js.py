#!/usr/bin/env python3
"""Parse every executable inline dashboard script with Node."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            attrs = dict(attrs)
            self.current = [] if not attrs.get('src') and attrs.get('type', '') in ('', 'text/javascript', 'module') else None
            self.module = attrs.get('type') == 'module'

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.current is not None:
            self.scripts.append((''.join(self.current), self.module))
            self.current = None


def main():
    root = Path(__file__).resolve().parents[1]
    parser = Scripts()
    parser.feed((root / 'src/tokdash/static/index.html').read_text(encoding='utf-8'))
    for script, module in parser.scripts:
        subprocess.run(['node', '--check', '--input-type=module' if module else '--input-type=commonjs'], input=script, text=True, check=True, cwd=root)
    print(f'{len(parser.scripts)} dashboard scripts parsed')


if __name__ == '__main__':
    main()
