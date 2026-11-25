"""Read only the launcher's explicit Skill catalog or authorized versioned library."""
from .vendor.library import DeltaLibrary, quality


def current_skill(config, name):
    if name not in config.get('skills', {}):
        raise ValueError('Skill outside the provided catalog')
    capability = config.get('skill_write')
    if not capability:
        return config['skills'][name]
    if capability.get('contract') != 'public-skill-authoring-v1':
        raise ValueError('Unsupported Skill library capability')
    library = DeltaLibrary(capability['library'], capability['sequence'])
    entry = library.state()['active'][name]
    content = (library.root / 'raw_versions' / (entry['raw_sha256'] + '.md')).read_text()
    return {**entry, 'content': content, 'description': quality(content)['description']}
