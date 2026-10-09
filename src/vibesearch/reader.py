"""Verified source descriptors, independent of catalog and network access."""
from urllib.parse import urlsplit


class ReaderError(Exception):
    """A controlled error safe for browser responses (never source/provider text)."""

    def __init__(self, message='Reader descriptors unavailable', status_code=409):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def safe_path(path: object, *, relative=True) -> str:
    if not isinstance(path, str) or not path or '%' in path or '\\' in path:
        raise ReaderError()
    if any(ord(c) < 33 or ord(c) == 127 for c in path):
        raise ReaderError()
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or '?' in path or '#' in path:
        raise ReaderError()
    if relative and path.startswith('/'):
        raise ReaderError()
    parts = path.lstrip('/').split('/')
    if any(p in ('', '.', '..') or ':' in p for p in parts):
        raise ReaderError()
    if parts[-1].rsplit('.', 1)[-1] not in {'webp', 'png', 'jpg', 'jpeg'}:
        raise ReaderError()
    return path


def _positive(value):
    return type(value) is int and value > 0


def reader_bundle(raw, display='full') -> dict:
    if display not in {'full', 'ids', 'id-only', 'ids-only'}:
        raise ReaderError('Invalid display mode', 422)
    if not isinstance(raw, dict):
        raise ReaderError()
    gallery_id, count, media = raw.get('id'), raw.get('num_pages'), raw.get('media_id')
    if not _positive(gallery_id) or not _positive(count) or not isinstance(media, str) or not media.isascii() or not media.isdigit():
        raise ReaderError()
    pages = raw.get('pages')
    if not isinstance(pages, list) or len(pages) != count:
        raise ReaderError()
    descriptors = []
    for number, page in enumerate(pages, 1):
        if not isinstance(page, dict) or type(page.get('number')) is not int or page['number'] != number:
            raise ReaderError()
        path = safe_path(page.get('path'))
        if not _positive(page.get('width')) or not _positive(page.get('height')):
            raise ReaderError()
        descriptors.append({'number': number, 'path': path, 'extension': path.rsplit('.', 1)[-1],
                            'width': page['width'], 'height': page['height']})
    result = {'gallery_id': gallery_id, 'media_id': media, 'num_pages': count, 'pages': descriptors}
    title = raw.get('title')
    if display == 'full' and isinstance(title, dict):
        result['title'] = next((title[k] for k in ('english', 'pretty', 'japanese') if isinstance(title.get(k), str) and title[k]), '')
    return result
