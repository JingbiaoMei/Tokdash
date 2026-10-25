"""Usage-store migrations that strip the old per-row speed fields.

Timing moved out of the usage rows into the speed cache. A copied legacy row
loses its timing JSON, and the schema-12 repair restores the accounting row
even when the log it came from is gone.
"""
import json
from contextlib import closing
from pathlib import Path

from test_lazy_speed_cache import corpus
from tokdash import speed_cache as cache, compute
from tokdash.sources.coding_tools import CodingToolsUsageTracker


def test_migration_removes_legacy_usage_speed_json(corpus):
    store, _ = compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(store._connect()) as conn:
        row = conn.execute('SELECT id,raw_json FROM usage_entries LIMIT 1').fetchone()
        raw = json.loads(row['raw_json'])
        raw['_speed'] = dict(speed_tokens=60, speed_ms=1500, speed_calls=1, speed_kind='response_window', speed_token_basis='output_including_reasoning', speed_status='measured')
        conn.execute('UPDATE usage_entries SET raw_json=? WHERE id=?', (json.dumps(raw), row['id']))
        for field in cache.FIELDS:
            kind = 'REAL' if field == 'speed_ms' else 'INTEGER' if field in {'speed_tokens', 'speed_calls'} else 'TEXT'
            default = '0' if kind in {'REAL', 'INTEGER'} else "''"
            conn.execute(f'ALTER TABLE usage_entries ADD COLUMN {field} {kind} NOT NULL DEFAULT {default}')
        conn.execute("UPDATE meta SET value='11' WHERE key='schema_version'")
        conn.commit()
        selected = row['id']
    with closing(store._connect()) as conn:
        raw = json.loads(conn.execute('SELECT raw_json FROM usage_entries WHERE id=?', (selected,)).fetchone()[0])
        schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        fields = [r['name'] for r in conn.execute('PRAGMA table_info(usage_entries)')]
    assert '_speed' not in raw, 'copied legacy usage rows retain their timing JSON'


def test_schema12_repair_preserves_accounting_when_input_is_gone(corpus):
    store,_=compute._sync_usage_store(CodingToolsUsageTracker())
    with closing(store._connect()) as conn:
        row=conn.execute('SELECT * FROM usage_entries LIMIT 1').fetchone()
        original=json.loads(row['raw_json'])
        legacy=dict(original,_speed={'speed_ms':999},output_speed=12,speed_ms=23,speed_kind='decode')
        conn.execute('UPDATE usage_entries SET raw_json=? WHERE id=?',(json.dumps(legacy),row['id']))
        conn.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
        conn.commit()
    Path(row['file_path']).unlink()
    with closing(store._connect()) as conn:
        repaired=conn.execute('SELECT * FROM usage_entries WHERE id=?',(row['id'],)).fetchone()
        assert json.loads(repaired['raw_json'])==original
        assert dict(repaired,raw_json=row['raw_json'])==dict(row)
