"""External RSI versions use only the existing registry's diagnostic namespace."""
import os
from pathlib import Path
import re
import uuid

from integrations.skill_rsi_contract import MAX_SKILL_BYTES, RSIError, checked_skill
from training.skill_registry import SkillConflict, SkillRegistry
from workenv.core import no_links
from workenv.leases import owned_lock


class SkillVersions:
    def __init__(self, root):
        self.root = Path(root).resolve()
        directory = self.root/'.runtime/skill-rsi/registry'
        no_links(directory,self.root)
        directory.mkdir(parents=True,exist_ok=True);os.chmod(directory,0o700)
        no_links(directory/'skills.sqlite3',self.root)
        self.registry = SkillRegistry(directory)
        os.chmod(self.registry.database,0o600)

    @staticmethod
    def version(value):
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value):
            raise RSIError('invalid_skill_version')
        return value

    def register(self, body):
        if (not isinstance(body,dict) or set(body) != {'schema_version','request_id','skill','expected_head'}
            or body['schema_version'] != 'skill-rsi-registration-v1'):
            raise RSIError('invalid_registration_request')
        try:
            if str(uuid.UUID(body['request_id'])) != body['request_id']:
                raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise RSIError('invalid_request_id') from None
        skill = checked_skill(body['skill'])
        expected = self.version(body['expected_head']) if body['expected_head'] is not None else None
        no_links(self.registry.database,self.root)
        if self.registry.database.stat().st_size > 64*1024*1024:
            raise RSIError('skill_registry_size_limit')
        try:
            with owned_lock(self.root/'.runtime/skill-rsi/registry.lock',self.root,'skill_rsi_registry'):
                with self.registry.connect() as db:
                    exists = db.execute("SELECT 1 FROM origins WHERE namespace='diagnostic' AND origin_id=?",(body['request_id'],)).fetchone()
                    if not exists and db.execute('SELECT COUNT(*) FROM origins').fetchone()[0] >= 256:
                        raise RSIError('skill_registry_version_limit')
                version = self.registry.publish_diagnostic(skill,origin_id=body['request_id'],expected_head=expected)
        except SkillConflict:
            raise RSIError('skill_version_conflict') from None
        except FileExistsError:
            raise RSIError('skill_registry_busy') from None
        return self.describe(version)

    def load(self, version):
        version = self.version(version)
        no_links(self.registry.database,self.root)
        try:
            return checked_skill(self.registry.get('diagnostic',version))
        except KeyError:
            raise RSIError('skill_version_not_found') from None

    def describe(self, version):
        skill = self.load(version)
        return {'schema_version':'skill-rsi-version-v1','namespace':'diagnostic','version':version,
                'is_current_head':self.registry.head('diagnostic',skill['skill_id']) == version,
                'training_eligible':False}

    def current_head(self, skill_id):
        """A diagnostic-only read for CAS clients; a missing head is explicit null."""
        try:
            if (not isinstance(skill_id,str) or not 0 < len(skill_id.strip()) <= 100
                or len(skill_id.encode('utf-8')) > MAX_SKILL_BYTES):
                raise ValueError()
        except (ValueError,UnicodeError):
            raise RSIError('invalid_skill_identity') from None
        no_links(self.registry.database,self.root)
        if self.registry.database.stat().st_size > 64*1024*1024:
            raise RSIError('skill_registry_size_limit')
        version = self.registry.head('diagnostic',skill_id)
        if version is not None:
            try:
                skill = self.load(version)
                if skill['skill_id'] != skill_id:
                    raise ValueError()
            except (ValueError,TypeError,KeyError):
                raise RSIError('skill_head_binding_invalid') from None
        return {'schema_version':'skill-rsi-head-v1','namespace':'diagnostic',
                'skill_id':skill_id,'version':version,'training_eligible':False}
