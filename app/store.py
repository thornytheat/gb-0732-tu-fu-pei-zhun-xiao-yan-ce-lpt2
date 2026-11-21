import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.errors import RegisterError
from app.manifest import canonical_json

SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signatures (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sheet TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version = 1),
    scope_kind TEXT,
    scope_label TEXT,
    scope_polygon TEXT,
    excluded_corners TEXT,
    decision_digest TEXT,
    PRIMARY KEY (run_id, sheet)
);
'''
SIGNATURE_COLUMNS = ('scope_kind', 'scope_label', 'scope_polygon', 'excluded_corners', 'decision_digest')


class Store:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def connect(self, mode='DEFERRED'):
        connection = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute(f'BEGIN {mode}')
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect('IMMEDIATE') as connection:
            for statement in SCHEMA.strip().removesuffix(';').split(';'):
                connection.execute(statement)
            existing = {row['name'] for row in connection.execute('PRAGMA table_info(signatures)')}
            # Older registers carry only the reviewer; add the scope columns in place.
            for column in SIGNATURE_COLUMNS:
                if column not in existing:
                    connection.execute(f'ALTER TABLE signatures ADD COLUMN {column} TEXT')

    @staticmethod
    def _signature(row):
        record = {'sheet': row['sheet'], 'reviewer': row['reviewer'], 'signed_at': row['signed_at'],
                  'version': row['version']}
        if row['scope_kind'] is not None:
            record['scope'] = {'kind': row['scope_kind'], 'label': row['scope_label'],
                               'polygon': json.loads(row['scope_polygon']) if row['scope_polygon'] else [],
                               'excluded_corners': json.loads(row['excluded_corners']) if row['excluded_corners'] else []}
        if row['decision_digest'] is not None:
            record['decision_digest'] = row['decision_digest']
        return record

    @staticmethod
    def _read(connection, run_id, required=True):
        row = connection.execute('SELECT snapshot FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if not row:
            if required:
                raise RegisterError('RUN_NOT_FOUND', '未找到该批次', 404)
            return None
        signatures = connection.execute(
            'SELECT * FROM signatures WHERE run_id = ? ORDER BY sheet', (run_id,))
        snapshot = json.loads(row['snapshot'])
        digest = snapshot.get('manifest', {}).get('decision_digest')
        records = [Store._signature(signature) for signature in signatures]
        for record in records:
            # 采信或拼缝证据已变的旧签审不得再算作本册的有效签审。
            record['stale'] = bool(digest and record.get('decision_digest')
                                   and record['decision_digest'] != digest)
        return {**snapshot, 'signatures': records}

    def get(self, run_id, required=True):
        with self.connect() as connection:
            return self._read(connection, run_id, required)

    def insert(self, snapshot):
        manifest = snapshot['manifest']
        run_id = manifest['run_id']
        serialized = canonical_json(snapshot)
        with self.connect('IMMEDIATE') as connection:
            insert = connection.execute('INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(run_id) DO NOTHING',
                                        (run_id, manifest['created_at'], serialized))
            return self._read(connection, run_id), insert.rowcount == 0

    def sign(self, run_id, sheet, reviewer, expected_version, expected_digest=None, expected_scope=None):
        with self.connect('IMMEDIATE') as connection:
            snapshot = self._read(connection, run_id)
            decision = next((row for row in snapshot['reviews'] if row['code'] == sheet), None)
            if decision is None:
                raise RegisterError('SHEET_NOT_FOUND', '该批次没有此图幅', 404)
            if not decision.get('signable', decision.get('status') in ('READY', 'LIMITED')):
                raise RegisterError('SIGNATURE_BLOCKED', '图幅尚未满足签审条件', 409,
                                    {'status': decision['status'], 'reason': decision['reason'],
                                     'corners': decision.get('coverage', {}).get('corners', [])})
            digest = snapshot['manifest'].get('decision_digest')
            # 采信控制点一旦改变，旧裁定不得沿用：到场摘要必须与当前裁定一致。
            if expected_digest is not None and expected_digest != digest:
                raise RegisterError('DECISION_STALE', '控制点采信或拼缝证据已变化，请刷新后重签', 409,
                                    {'expected': expected_digest, 'current': digest})
            scope = decision['scope'] or {}
            # 签审范围取自裁定，不允许客户端自行扩大或改写。
            if expected_scope is not None and expected_scope != scope.get('kind'):
                raise RegisterError('SIGNATURE_SCOPE_CONFLICT', '签审范围与图幅裁定不一致，请刷新', 409,
                                    {'expected': expected_scope, 'current': scope.get('kind')})
            existing = next((row for row in snapshot['signatures'] if row['sheet'] == sheet), None)
            if existing:
                if existing['reviewer'] == reviewer and expected_version in (0, existing['version']):
                    return existing
                raise RegisterError('SIGNATURE_VERSION_CONFLICT', '签审版本已变化，请刷新', 409,
                                    existing['version'])
            if expected_version != 0:
                raise RegisterError('SIGNATURE_VERSION_CONFLICT', '签审版本已变化，请刷新', 409, 0)
            signed_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            connection.execute(
                'INSERT INTO signatures VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)',
                (run_id, sheet, reviewer, signed_at, scope.get('kind'), scope.get('label'),
                 canonical_json(scope.get('polygon') or []), canonical_json(scope.get('excluded_corners') or []),
                 digest),
            )
            return {'sheet': sheet, 'reviewer': reviewer, 'signed_at': signed_at, 'version': 1,
                    'scope': scope or None, 'decision_digest': digest, 'stale': False}
