"""Publish a complete immutable bundle atomically on shared artifact storage.

No locks, worker identities, leases, retry loops or registry dispatch. A crash
before the final rename leaves an unreferenced staging directory; a crash after
it leaves a complete bundle that the retried Activity can adopt and record.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

from applypilot.orchestration.contracts import digest

# Bump when render semantics change. This is an artifact format version, not a
# deployment identity or worker-specific value.
RENDERER_VERSION = 'applypilot-document-v1'


class ArtifactIntegrityError(ValueError):
    """An existing immutable artifact has changed; never silently overwrite it."""


def storage_root() -> Path:
    from applypilot import config
    return Path(os.getenv('APPLYPILOT_ARTIFACT_ROOT', str(config.APP_DIR / 'artifacts'))).expanduser().resolve()


def inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('Artifact paths must be portable relative paths')
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or ':' in relative:
        raise ValueError('Artifact path escapes the shared root')
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('Artifact path escapes the shared root')
    return target


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write(path, data):
    with Path(path).open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    # POSIX directory fsync protects the atomic rename from a machine crash.
    # Windows rename atomicity still applies, but directory fsync is unavailable.
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_output(path, doc_format):
    if not path.is_file() or not path.stat().st_size:
        raise ArtifactIntegrityError('Renderer produced no document')
    if doc_format == 'pdf':
        with path.open('rb') as stream:
            if stream.read(5) != b'%PDF-':
                raise ArtifactIntegrityError('Renderer output is not a PDF')
        # Parsing is intentionally done by the renderer's supported dependency.
        from pypdf import PdfReader
        if not PdfReader(path).pages:
            raise ArtifactIntegrityError('Rendered PDF has no pages')
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                required = {'[Content_Types].xml', '_rels/.rels', 'word/document.xml'}
                if not required <= set(archive.namelist()) or archive.testzip() is not None:
                    raise ArtifactIntegrityError('Renderer output is not a complete DOCX')
        except zipfile.BadZipFile as exc:
            raise ArtifactIntegrityError('Renderer output is not a DOCX') from exc


def identity(document_id, doc_format):
    return digest((document_id, doc_format, RENDERER_VERSION))


def read_bundle(root, document_id, doc_format):
    artifact_id = identity(document_id, doc_format)
    directory = inside(root, f'documents/{document_id}/{artifact_id}')
    if not directory.exists():
        return None
    try:
        value = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if (value['artifact_id'] != artifact_id or value['document_id'] != document_id
                or value['doc_format'] != doc_format or value['renderer_version'] != RENDERER_VERSION
                or value['relative_path'] != f'documents/{document_id}/{artifact_id}/document.{doc_format}'):
            raise ArtifactIntegrityError('Artifact manifest identity mismatch')
        output = inside(root, value['relative_path'])
        if (output.is_symlink() or _sha(output) != value['sha256']
                or _sha(directory / 'source.txt') != value['source_sha256']
                or digest(json.loads((directory / 'source.approval.json').read_text(encoding='utf-8')))
                   != value['approval_sha256']):
            raise ArtifactIntegrityError('Immutable rendered document changed')
        return value
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError('Incomplete or invalid immutable document bundle') from exc


def publish(root, document_id, value, doc_format, renderer, check_cancel=lambda: None):
    """Render one approved input. The first complete bundle wins concurrent retries."""
    root = Path(root).resolve()
    prior = read_bundle(root, document_id, doc_format)
    if prior is not None:
        return prior
    artifact_id = identity(document_id, doc_format)
    parent = inside(root, f'documents/{document_id}')
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / artifact_id
    staging = Path(tempfile.mkdtemp(prefix='.render-', dir=parent))
    try:
        source = staging / 'source.txt'
        _write(source, value['text'].encode('utf-8'))
        _write(source.with_suffix('.approval.json'),
               json.dumps(value['approval'], sort_keys=True, allow_nan=False).encode('utf-8'))
        check_cancel()
        output = staging / f'document.{doc_format}'
        renderer(source, output_path=output, doc_format=doc_format, metadata=value['metadata'])
        check_cancel()
        validate_output(output, doc_format)
        with output.open('rb') as stream:
            os.fsync(stream.fileno())
        manifest = {'artifact_id': artifact_id, 'document_id': document_id,
                    'doc_format': doc_format, 'renderer_version': RENDERER_VERSION,
                    'relative_path': f'documents/{document_id}/{artifact_id}/document.{doc_format}',
                    'sha256': _sha(output), 'source_sha256': value['source_sha256'],
                    'approval_sha256': digest(value['approval'])}
        _write(staging / 'manifest.json', json.dumps(manifest, sort_keys=True).encode('utf-8'))
        _sync_directory(staging)
        check_cancel()
        try:
            # Do not use os.replace: an existing nonempty immutable bundle must
            # never be overwritten by a later worker or retry.
            os.rename(staging, final)
            _sync_directory(parent)
        except OSError:
            if not final.is_dir():
                raise
        winner = read_bundle(root, document_id, doc_format)
        if winner is None:
            raise ArtifactIntegrityError('Artifact publication did not produce a complete bundle')
        return winner
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def index_path(source, doc_format):
    return Path(source).with_suffix(f'.{doc_format}.artifact.json')


def publish_index(source, value, manifest):
    """A source-to-render index is file metadata, not task execution state."""
    from applypilot.scoring.artifact_store import atomic_text
    atomic_text(index_path(source, manifest['doc_format']), json.dumps({
        'version': 1, 'source_sha256': value['source_sha256'],
        'approval_sha256': digest(value['approval']), **manifest}, sort_keys=True))


def resolve_rendered(source, doc_format, *, root=None):
    """Return a verified native render, or None when this source has no native index.

    A malformed index or corrupted artifact is a visible integrity failure. It
    must not silently fall back to a stale sibling PDF and conceal corruption.
    """
    source = Path(source).with_suffix('.txt')
    path = index_path(source, doc_format)
    if not path.exists():
        return None
    root = storage_root() if root is None else Path(root).resolve()
    try:
        index = json.loads(path.read_text(encoding='utf-8'))
        from applypilot.scoring.artifact_store import manifest_path
        proof = json.loads(manifest_path(source).read_text(encoding='utf-8'))
        if (index.get('version') != 1 or _sha(source) != index['source_sha256']
                or digest(proof) != index['approval_sha256']):
            raise ArtifactIntegrityError('Document source or approval changed; render the approved generation again')
        bundle = read_bundle(root, index['document_id'], doc_format)
        if bundle is None or any(index.get(key) != item for key, item in bundle.items()):
            raise ArtifactIntegrityError('Source index does not match its immutable artifact')
        return inside(root, bundle['relative_path'])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError('Invalid native document index') from exc
