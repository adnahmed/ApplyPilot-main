"""Saved qualification settings for the next managed runtime."""
from applypilot.onboarding.storage import read_json, setup_lock, write_json
from applypilot.control import OPTIONS


def read(root):
    from applypilot.config import DEFAULTS
    saved = read_json(root / OPTIONS, {})
    return {'min_score': saved.get('min_score', DEFAULTS['min_score'])}


def save(root, data):
    if set(data) != {'min_score'} or type(data['min_score']) is not int or not 1 <= data['min_score'] <= 10:
        raise ValueError('Choose a minimum job fit score from 1 to 10')
    with setup_lock(root):
        current = read_json(root / OPTIONS, {})
        write_json(root / OPTIONS, {**current, **data})
    return {'saved': True, 'message': 'Minimum fit score saved. It applies on the next start or resume.'}
