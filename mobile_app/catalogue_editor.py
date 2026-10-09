"""Owner-only, version-checked catalogue editing; retains the JSON catalogue."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import uuid
from flask import Blueprint, g, jsonify, request, send_from_directory


def install_roles(store):
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS catalogue_owners (user_id TEXT PRIMARY KEY)')


def is_owner(store, account):
    if not account:
        return False
    with store.connect() as db:
        return db.execute('SELECT 1 FROM catalogue_owners WHERE user_id=?', (account['user_id'],)).fetchone() is not None


def validate_product(product):
    if not isinstance(product, dict):
        raise ValueError('A product must be an object.')
    for key in ('id', 'name', 'category'):
        if not isinstance(product.get(key), str) or not product[key].strip() or len(product[key]) > 200:
            raise ValueError(f'{key} must contain 1–200 characters.')
    def walk(value, depth=0):
        if depth > 16:
            raise ValueError('Sections may be nested at most 16 levels deep.')
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or not key.strip() or len(key) > 200:
                    raise ValueError('Field names must contain 1–200 characters.')
                walk(item, depth+1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth+1)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Numbers must be finite.')
        elif value is not None and not isinstance(value, (str, bool, int, float)):
            raise ValueError('Unsupported field value.')
    walk(product)
    if 'options' in product:
        options = product['options']
        if not isinstance(options, list) or any(not isinstance(o, dict) or not isinstance(o.get('code'), str) or not o['code'].strip() for o in options):
            raise ValueError('Options must be a list of groups, each with a non-empty text code.')
        if len({o['code'] for o in options}) != len(options):
            raise ValueError('Option codes must be unique within a product.')


def revision(raw):
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def catalogue_lock(path):
    with (path.parent / (path.name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def register_editor(web, service, matcher):
    install_roles(service.store)
    bp = Blueprint('catalogue_editor', __name__)
    assets = Path(__file__).parent / 'static'

    @bp.get('/catalogue-editor')
    def page():
        # This shell contains no catalogue data. The API enforces ownership.
        return send_from_directory(assets, 'catalogue-editor.html')

    @bp.get('/api/owner/catalogue')
    def get_catalogue():
        if not is_owner(service.store, g.account):
            return jsonify(error='Only the catalogue owner can edit products.'), 403
        try:
            raw = matcher.CATALOGUE_PATH.read_bytes()
            data = json.loads(raw)
            if not isinstance(data.get('products'), list):
                raise ValueError()
            return jsonify(catalogue=data, revision=revision(raw))
        except (OSError, ValueError, AttributeError):
            return jsonify(error='The catalogue file could not be read.'), 400

    @bp.post('/api/owner/products')
    def save_product():
        if not is_owner(service.store, g.account):
            return jsonify(error='Only the catalogue owner can edit products.'), 403
        body = request.get_json(silent=True)
        try:
            if not isinstance(body, dict) or not isinstance(body.get('revision'), str):
                raise ValueError('Reload the catalogue before saving.')
            product = body.get('product')
            validate_product(product)
            old_id = body.get('original_id')
            if old_id is not None and not isinstance(old_id, str):
                raise ValueError('Invalid original product ID.')
            path = Path(matcher.CATALOGUE_PATH)
            with catalogue_lock(path):
                raw = path.read_bytes()
                if revision(raw) != body['revision']:
                    return jsonify(error='The catalogue changed since you opened it. Your draft is kept. Download it, then reload the catalogue before saving.'), 409
                catalogue = json.loads(raw)
                products = catalogue['products']
                indexes = [i for i, p in enumerate(products) if p['id'] == old_id]
                if old_id is not None and len(indexes) != 1:
                    raise ValueError('The original product could not be found uniquely.')
                if any(p['id'] == product['id'] and (not indexes or i != indexes[0]) for i, p in enumerate(products)):
                    raise ValueError('That product ID already exists. Choose a unique ID.')
                if indexes:
                    products[indexes[0]] = product
                else:
                    products.append(product)
                output = (json.dumps(catalogue, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()
                backups = service.store.path.parent / 'catalogue-backups'
                backups.mkdir(mode=0o700, parents=True, exist_ok=True)
                backup = backups / (uuid.uuid4().hex + '.json')
                with backup.open('xb') as f:
                    f.write(raw)
                backup.chmod(0o600)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
                        temporary = Path(f.name)
                        f.write(output); f.flush(); os.fsync(f.fileno())
                    temporary.chmod(path.stat().st_mode & 0o777)
                    if path.read_bytes() != raw:
                        return jsonify(error='The catalogue changed while saving. Your draft is kept; reload before saving.'), 409
                    os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return jsonify(product=product, revision=revision(output), count=len(products))
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            return jsonify(error=str(exc) if isinstance(exc, ValueError) else 'The product or catalogue structure is invalid.'), 400
        except OSError:
            return jsonify(error='Could not save the catalogue. Check that its folder is writable. Your draft is kept.'), 500

    web.register_blueprint(bp)


if __name__ == '__main__':
    from history import History
    parser = argparse.ArgumentParser(description='Grant catalogue editing to an existing account.')
    parser.add_argument('--owner', required=True)
    parser.add_argument('--database', default=os.environ.get('HISTORY_DB', str(Path(__file__).parent/'data/history.sqlite3')))
    args = parser.parse_args()
    store = History(args.database); install_roles(store)
    with store.connect() as db:
        user = db.execute('SELECT id FROM users WHERE username=?', (args.owner.strip().lower(),)).fetchone()
        if not user:
            raise SystemExit('Account not found. Create the account first.')
        db.execute('INSERT OR IGNORE INTO catalogue_owners VALUES (?)', (user['id'],))
    print('Catalogue owner access granted.')
