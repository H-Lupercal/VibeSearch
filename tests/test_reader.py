import copy

import pytest

from vibesearch.reader import ReaderError, reader_bundle


def detail(path='galleries/42/1.png'):
    return {'id': 7, 'media_id': '42', 'num_pages': 1,
            'title': {'english': '<neutral>', 'pretty': 'Neutral'},
            'pages': [{'number': 1, 'path': path, 'width': 2, 'height': 3}]}


def test_verified_bundle_and_privacy():
    raw = detail()
    before = copy.deepcopy(raw)
    result = reader_bundle(raw)
    assert result['gallery_id'] == 7
    assert result['pages'][0]['extension'] == 'png'
    assert result['title'] == '<neutral>'
    assert 'title' not in reader_bundle(raw, display='ids')
    assert raw == before


@pytest.mark.parametrize('path', ['https://i1.nhentai.net/a.png', '//i1.nhentai.net/a.png',
    '../a.png', 'galleries/../a.png', 'a\\b.png', 'a.png?q=x', 'a.png#x',
    'a%2fb.png', 'a%252e.png', 'a\x00.png', '/a.png', 'a.gif', 'a//b.png'])
def test_bad_paths(path):
    with pytest.raises(ReaderError) as error:
        reader_bundle(detail(path))
    assert error.value.status_code == 409
    assert path not in str(error.value)


@pytest.mark.parametrize('change', [ {'num_pages': 0}, {'num_pages': 2}, {'id': True},
    {'media_id': ''}, {'pages': []}, {'pages': [{'number': 2, 'path': 'a.png', 'width': 2, 'height': 3}]},
    {'pages': [{'number': 1, 'path': 'a.png', 'width': True, 'height': 3}]}])
def test_invalid_descriptors(change):
    raw = detail()
    raw.update(change)
    with pytest.raises(ReaderError):
        reader_bundle(raw)
