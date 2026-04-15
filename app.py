import os
import sqlite3
import uuid
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='public', static_url_path='')

PORT           = int(os.environ.get('PORT', 3000))
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'zenmaid-admin')
DB_PATH        = os.path.join(os.path.dirname(__file__), 'requests.db')


# ── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT    NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                status      TEXT    NOT NULL DEFAULT 'Pending',
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                merged_into INTEGER  DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS votes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                voter_id   TEXT    NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(request_id, voter_id)
            );
        ''')


# ── Auth helper ───────────────────────────────────────────────────────────────

def is_admin():
    return request.headers.get('Authorization') == f'Bearer {ADMIN_PASSWORD}'


def require_admin():
    if not is_admin():
        return jsonify(error='Unauthorized'), 401


# ── Static pages ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('public', 'index.html')


@app.route('/admin.html')
def admin():
    return send_from_directory('public', 'admin.html')


# ── Public API ────────────────────────────────────────────────────────────────

@app.route('/api/requests', methods=['GET'])
def list_requests():
    voter_id = request.args.get('voter_id', '')
    status   = request.args.get('status', '')

    sql = '''
        SELECT r.*,
            COUNT(v.id) AS vote_count,
            MAX(CASE WHEN v.voter_id = ? THEN 1 ELSE 0 END) AS has_voted
        FROM requests r
        LEFT JOIN votes v ON v.request_id = r.id
        WHERE r.merged_into IS NULL
    '''
    params = [voter_id]

    if status:
        sql += ' AND r.status = ?'
        params.append(status)

    sql += ' GROUP BY r.id ORDER BY vote_count DESC, r.created_at DESC'

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/requests', methods=['POST'])
def create_request():
    data  = request.get_json() or {}
    title = (data.get('title') or '').strip()
    desc  = (data.get('description') or '').strip()

    if not title:
        return jsonify(error='Title is required'), 400

    with get_db() as conn:
        cur = conn.execute(
            'INSERT INTO requests (title, description) VALUES (?, ?)',
            (title, desc)
        )
        row = conn.execute('SELECT * FROM requests WHERE id = ?', (cur.lastrowid,)).fetchone()

    result = dict(row)
    result['vote_count'] = 0
    result['has_voted']  = 0
    return jsonify(result), 201


@app.route('/api/requests/<int:req_id>/vote', methods=['POST'])
def vote(req_id):
    data     = request.get_json() or {}
    voter_id = (data.get('voter_id') or '').strip()

    if not voter_id:
        return jsonify(error='voter_id required'), 400

    with get_db() as conn:
        row = conn.execute(
            'SELECT id FROM requests WHERE id = ? AND merged_into IS NULL', (req_id,)
        ).fetchone()
        if not row:
            return jsonify(error='Not found'), 404

        existing = conn.execute(
            'SELECT id FROM votes WHERE request_id = ? AND voter_id = ?',
            (req_id, voter_id)
        ).fetchone()

        if existing:
            conn.execute('DELETE FROM votes WHERE request_id = ? AND voter_id = ?', (req_id, voter_id))
            voted = False
        else:
            conn.execute('INSERT INTO votes (request_id, voter_id) VALUES (?, ?)', (req_id, voter_id))
            voted = True

        count = conn.execute(
            'SELECT COUNT(*) FROM votes WHERE request_id = ?', (req_id,)
        ).fetchone()[0]

    return jsonify(voted=voted, vote_count=count)


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.route('/api/admin/verify', methods=['POST'])
def verify():
    data = request.get_json() or {}
    if data.get('password') == ADMIN_PASSWORD:
        return jsonify(ok=True)
    return jsonify(error='Invalid password'), 401


@app.route('/api/admin/requests', methods=['GET'])
def admin_list():
    err = require_admin()
    if err: return err

    with get_db() as conn:
        rows = conn.execute('''
            SELECT r.*,
                COUNT(v.id)  AS vote_count,
                m.title      AS merged_into_title
            FROM requests r
            LEFT JOIN votes    v ON v.request_id = r.id
            LEFT JOIN requests m ON m.id = r.merged_into
            GROUP BY r.id
            ORDER BY
                CASE WHEN r.merged_into IS NULL THEN 0 ELSE 1 END,
                vote_count DESC,
                r.created_at DESC
        ''').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/admin/requests/<int:req_id>', methods=['PUT'])
def admin_update(req_id):
    err = require_admin()
    if err: return err

    data   = request.get_json() or {}
    status = data.get('status')
    title  = data.get('title')
    desc   = data.get('description')

    valid_statuses = {'Pending', 'Cannot Do', 'Complete'}
    if status and status not in valid_statuses:
        return jsonify(error='Invalid status'), 400

    sets, params = [], []
    if status is not None:
        sets.append('status = ?');      params.append(status)
    if title is not None:
        sets.append('title = ?');       params.append(title.strip())
    if desc is not None:
        sets.append('description = ?'); params.append(desc.strip())

    if not sets:
        return jsonify(error='Nothing to update'), 400

    params.append(req_id)
    with get_db() as conn:
        conn.execute(f'UPDATE requests SET {", ".join(sets)} WHERE id = ?', params)
        row = conn.execute('''
            SELECT r.*, COUNT(v.id) AS vote_count
            FROM requests r LEFT JOIN votes v ON v.request_id = r.id
            WHERE r.id = ? GROUP BY r.id
        ''', (req_id,)).fetchone()

    return jsonify(dict(row))


@app.route('/api/admin/merge', methods=['POST'])
def admin_merge():
    err = require_admin()
    if err: return err

    data      = request.get_json() or {}
    keep_id   = data.get('keep_id')
    merge_ids = data.get('merge_ids', [])

    if not keep_id or not merge_ids:
        return jsonify(error='keep_id and merge_ids required'), 400
    if keep_id in merge_ids:
        return jsonify(error='Cannot merge a request into itself'), 400

    with get_db() as conn:
        for merge_id in merge_ids:
            voters = conn.execute(
                'SELECT voter_id FROM votes WHERE request_id = ?', (merge_id,)
            ).fetchall()
            for v in voters:
                conn.execute(
                    'INSERT OR IGNORE INTO votes (request_id, voter_id) VALUES (?, ?)',
                    (keep_id, v['voter_id'])
                )
            conn.execute('UPDATE requests SET merged_into = ? WHERE id = ?', (keep_id, merge_id))

        kept = conn.execute('''
            SELECT r.*, COUNT(v.id) AS vote_count
            FROM requests r LEFT JOIN votes v ON v.request_id = r.id
            WHERE r.id = ? GROUP BY r.id
        ''', (keep_id,)).fetchone()

    return jsonify(dict(kept))


@app.route('/api/admin/requests/<int:req_id>/unmerge', methods=['POST'])
def admin_unmerge(req_id):
    err = require_admin()
    if err: return err

    with get_db() as conn:
        conn.execute('UPDATE requests SET merged_into = NULL WHERE id = ?', (req_id,))
    return jsonify(ok=True)


@app.route('/api/admin/requests/<int:req_id>', methods=['DELETE'])
def admin_delete(req_id):
    err = require_admin()
    if err: return err

    with get_db() as conn:
        conn.execute('DELETE FROM votes    WHERE request_id = ?', (req_id,))
        conn.execute('DELETE FROM requests WHERE id = ?',         (req_id,))
    return jsonify(ok=True)


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    print()
    print('  ZenMaid Feature Requests')
    print('  -----------------------------------------')
    print(f'  User portal:  http://localhost:{PORT}')
    print(f'  Admin panel:  http://localhost:{PORT}/admin.html')
    print(f'  Admin pass:   {ADMIN_PASSWORD}')
    print('  (set ADMIN_PASSWORD env var to change)')
    print()
    app.run(host='0.0.0.0', port=PORT, debug=False)
