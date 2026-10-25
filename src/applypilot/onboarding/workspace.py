"""Read the existing workspace as material, without duplicating or migrating owner data."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

from .storage import digest, public, read_bytes, read_json

# Compatibility is a view, not a destructive rewrite of legacy profile schemas.
PROFILE_ALIASES = {
    "experience.professional_summary": ("experience.positioning_summary", "experience.summary"),
    'experience.current_job_title': ('experience.current_title',),
    'experience.current_company': ('experience.company', 'experience.current_employer'),
    'experience.years_of_experience_total': ('experience.years_of_experience',),
    'skills_boundary.languages': ('skills_boundary.programming_languages',),
    'skills_boundary.programming_languages': ('skills_boundary.languages',),
    'skills_boundary.databases': ('skills_boundary.databases_and_messaging',),
    'skills_boundary.devops': ('skills_boundary.cloud_and_platform',),
    'personal.portfolio_url': ('personal.website_url',),
}


def at(obj, path):
    for key in path.split('.'):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def profile_value(profile, path):
    for candidate in (path, *PROFILE_ALIASES.get(path, ())):
        value = at(profile, candidate)
        if value not in (None, '', [], {}):
            return value
    return None


def fill_missing(current, incoming):
    """Only fill absent/empty fields; False and zero are real owner answers."""
    result = deepcopy(current)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = fill_missing(result[key], value)
        elif key not in result or result[key] in (None, '', [], {}):
            result[key] = deepcopy(value)
    return result


def canonical_profile(profile):
    """Expose stored aliases through the same names used by editable forms."""
    result = deepcopy(profile)
    for path in PROFILE_ALIASES:
        value = profile_value(profile, path)
        if value is None:
            continue
        section, key = path.split('.', 1)
        result.setdefault(section, {})[key] = deepcopy(value)
    work = result.get('work_authorization') or {}
    countries = work.get('by_country') or {}
    if isinstance(countries, dict) and countries:
        from applypilot.profile import _country
        normalized = {}
        for country, facts in countries.items():
            code = _country(country, 'Work authorization country')
            if code is None or code in normalized:
                raise ValueError('Work authorization contains an unknown or duplicate country')
            normalized[code] = facts
        work['by_country'] = normalized
    return result


def connected_identity(root, provider, profile=None):
    profile = profile if isinstance(profile, dict) else read_json(root / 'profile.json', {})
    personal = profile.get('personal') or {}
    value = personal.get(provider + '_url')
    if isinstance(value, str) and value.strip():
        return value.strip()
    if provider == 'github':
        saved = read_json(root / 'setup' / 'github-auth.json', {})
        login = saved.get('login')
        if isinstance(login, str) and login.strip():
            return login.strip()
    return ''


def resume_material(root: Path):
    # The legacy CLI's master text is authoritative; generated tailored PDFs never are.
    for name in ('resume.txt', 'resume.md', 'resume.pdf', 'resume.docx'):
        path = root / name
        if not path.is_file():
            continue
        raw = read_bytes(path, 8_000_000)
        if not raw.strip():
            continue
        if path.suffix in {'.txt', '.md'}:
            text = raw.decode('utf-8-sig')[:120_000]
        else:
            from .documents import extract
            text, _, _ = extract(name, raw)
        return {'id': 'current-resume', 'name': name, 'kind': 'resume', 'text': text,
                'characters': len(text), 'sha256': digest(raw), 'extension': path.suffix,
                'existing': True, 'path': str(path), 'truncated': len(text) >= 120_000}
    return None


def profile_fact_source(profile):
    """Expose typed owner facts as quotable material, excluding repository claims."""
    sections = {section: deepcopy(profile[section]) for section in
                ('personal', 'experience', 'skills_boundary', 'resume_facts')
                if isinstance(profile.get(section), dict) and profile[section]}
    if not sections:
        return None
    text = json.dumps(public(sections), ensure_ascii=False, indent=2)
    return {'id': 'saved-profile-facts', 'name': 'Your saved profile facts and skill categories',
            'kind': 'supporting', 'text': text, 'characters': len(text), 'existing': True,
            'extension': '.json', 'typed_profile': True, 'payload': public(sections)}


def material_sources(root: Path, *, include_impact=True):
    sources = []
    resume = resume_material(root)
    if resume:
        sources.append(resume)
    profile = public(read_json(root / 'profile.json', {}))
    typed_facts = profile_fact_source(profile)
    if typed_facts:
        sources.append(typed_facts)
    for name, kind in (('profile.json', 'profile'), ('searches.yaml', 'searches'), ('searches.target.yaml', 'searches')):
        if name == 'searches.target.yaml' and (root / 'searches.yaml').is_file():
            continue
        path = root / name
        if not path.is_file():
            continue
        text = json.dumps(profile, ensure_ascii=False) if kind == 'profile' else read_bytes(path).decode('utf-8-sig')
        sources.append({'id': 'current-' + name, 'name': name, 'kind': kind, 'text': text,
                        'characters': len(text), 'existing': True, 'extension': path.suffix,
                        'path': str(path), 'payload': profile if kind == 'profile' else None})
    # Evidence is material. Keep each record's provenance, rather than claiming authorship.
    from applypilot.profile import resume_evidence_entries
    for row in profile.get('public_evidence', []) if isinstance(profile, dict) else []:
        if not isinstance(row, dict):
            continue
        approved = resume_evidence_entries({'public_evidence': [row]})
        eligible = bool(approved)
        allowed = approved[0]['allowed_claims'] if eligible else []
        contribution = {'title': approved[0].get('title') if eligible else row.get('title'),
                        'approved_contributions': allowed}
        text = json.dumps(contribution, ensure_ascii=False)
        sources.append({'id': 'existing-evidence-' + digest(row)[:20], 'name': row.get('title') or row.get('ref') or 'Project evidence',
                        'kind': 'supporting', 'text': text[:24000], 'characters': min(len(text), 24000),
                        'existing': True, 'extension': '.json', 'url': row.get('url'), 'evidence': True,
                        'profile_eligible': eligible})
    for row in read_json(root / 'setup' / 'github.json', [], limit=32_000_000):
        if not isinstance(row, dict):
            continue
        text = json.dumps(row, ensure_ascii=False)
        sources.append({'id': 'collected-github-' + digest(row.get('ref', ''))[:20],
                        'name': row.get('title') or row.get('ref') or 'GitHub project',
                        'kind': 'supporting', 'text': text[:24000], 'characters': min(len(text), 24000),
                        'existing': True, 'extension': '.json', 'url': row.get('url'), 'evidence': True,
                        'unreviewed': True})
    if include_impact:
        from applypilot.backup.diary import view
        diary = view(root, page_size=None)
        for row in diary['entries']:
            text = row['text']
            sources.append({'id': 'impact-' + row['id'], 'name': row['title'], 'kind': 'supporting',
                            'text': text[:24000], 'characters': min(len(text), 24000), 'existing': True,
                            'extension': '.json', 'impact': True})
    # Append imported sources so an explicit replace-master chooses the latest upload.
    sources.extend(read_json(root / 'setup' / 'sources.json', []))
    return sources


def inventory(root: Path):
    """Small, secret-free inventory so a returning user can see the actual data root."""
    files = []
    for name in ('profile.json', 'searches.yaml', 'searches.target.yaml', 'resume.txt', 'resume.md',
                 'resume.pdf', 'resume.docx', 'impact_ledger.json', 'main.pgstore'):
        path = root / name
        if path.is_file() and not path.is_symlink():
            files.append({'name': name, 'bytes': path.stat().st_size, 'path': str(path)})
    return {'root': str(root.resolve()), 'files': files,
            'existing': any(x['name'] in {'profile.json', 'main.pgstore'} for x in files)}
