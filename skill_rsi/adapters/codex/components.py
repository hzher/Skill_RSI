"""Package selected local Astra Skills and existing Codex-native interventions.

No discovery inputs, source traces or experiment results are copied into releases.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ...components import pack
from ...components.manifest import canonical, sha256
from ...review.projection import SECRET
from ._native.plugins.registry import definition, validate_plugins, plugin_catalog


def native_digest():
    root = Path(__file__).parent / '_native'
    return sha256(canonical({p.relative_to(root).as_posix(): sha256(p.read_bytes())
                             for p in sorted(root.rglob('*.py'))}))


def export_component(source, output, *, component_id, version, form, approved_sha256, review_ref):
    """The approved digest covers the exact local file, never a model's self-report."""
    raw = Path(source).read_bytes()
    if sha256(raw) != approved_sha256 or not isinstance(review_ref, str) or not review_ref.strip():
        raise ValueError('Provide the exact approved source digest and a review reference')
    if SECRET.search(raw.decode('utf-8')):
        raise ValueError('Source contains a credential pattern')
    if form == 'skill':
        if not raw.strip():
            raise ValueError('A Skill must contain operating instructions')
        entrypoint, kind, target, engine = 'payload/SKILL.md', 'append_context', 'context.skill.' + component_id, 'skill'
        payload = raw
    elif form == 'plugin':
        spec = json.loads(raw)
        validate_plugins([spec])
        definition(spec['kind'])
        entrypoint, kind, target, engine = 'payload/plugin.json', 'load_plugin', 'codex.plugin.' + component_id, 'native'
        payload = canonical({'specification': spec, 'native_code_digest': native_digest()})
    else:
        raise ValueError('This migration exports existing Skill and native plugin forms only')
    manifest = {
        'schema': 'skill-rsi.component.v1', 'component_id': component_id, 'version': version,
        'form': form, 'engine': engine, 'entrypoint': entrypoint,
        'required_capabilities': {'apply': [kind], 'suggest': ['suggest'], 'shadow': []},
        'scope': {'hosts': ['codex'], 'modes': ['shadow', 'suggest', 'apply'], 'adapter_versions': ['2']},
        'trigger': {'boundaries': ['capture_start']},
        'action': {'kind': kind, 'target': target, 'writes': []},
        'budget': {'max_action_bytes': max(65536, len(payload) + 4096)}, 'fallback': 'native',
    }
    with TemporaryDirectory(prefix='codex-component-') as directory:
        root = Path(directory)
        (root / 'payload').mkdir()
        (root / entrypoint).write_bytes(payload)
        (root / 'manifest.json').write_bytes(canonical(manifest))
        (root / 'review.json').write_bytes(canonical({
            'approved_sha256': approved_sha256, 'review_ref': review_ref,
            'status': 'candidate', 'causal_effect_verified': False,
            'source': 'xuyifan Codex harness migration',
        }))
        return pack(root, output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('catalog')
    export = sub.add_parser('export')
    export.add_argument('--source', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--component-id', required=True)
    export.add_argument('--version', required=True)
    export.add_argument('--form', choices=['skill', 'plugin'], required=True)
    export.add_argument('--approved-sha256', required=True)
    export.add_argument('--review-ref', required=True)
    args = parser.parse_args(argv)
    if args.command == 'catalog':
        print(json.dumps({'form': 'plugin', 'families': plugin_catalog()}, ensure_ascii=False))
    else:
        values = vars(args).copy()
        values.pop('command')
        print(json.dumps(export_component(**values).to_dict(), ensure_ascii=False))


if __name__ == '__main__':
    main()
