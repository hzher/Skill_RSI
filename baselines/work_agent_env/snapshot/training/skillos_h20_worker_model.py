"""Actual vLLM0.11 GPU worker adapter. Imported only by a future model service."""
import ast
import hashlib
import json
import os
from pathlib import Path

from vllm.v1.worker.gpu_worker import Worker

from training.skillos_h20_sources import Sources


class H20PolicyFingerprintWorker(Worker):
    def load_model(self):
        super().load_model()
        config = json.loads(Path(os.environ['SKILLOS_H20_DEPLOYMENT']).read_text())
        sources = Sources(config)
        def version_binding(tree):
            changes = []
            class Version(ast.NodeTransformer):
                def visit_Constant(self, node):
                    if node.value == '0.19.0':
                        changes.append({'boundary': 'vLLM version', 'from': '0.19.0', 'to': '0.11.0'})
                        return ast.copy_location(ast.Constant('0.11.0'), node)
                    return node
            return Version().visit(tree), changes
        module = sources.load('runtime/trainer-project-v1/service_backend/skillos_weight_fingerprint.py',
                              extra=True, transform=version_binding)
        proof = module.WeightSyncExtension.weight_sync_fingerprint(self)
        proof.update(worker_pid=os.getpid(), gpu_env=os.environ['CUDA_VISIBLE_DEVICES'],
            policy_version=os.environ['SKILLOS_POLICY_VERSION'], role=os.environ['SKILLOS_ROLE'],
            adapter_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            source_adaptations=sources.audit)
        target = Path(os.environ['SKILLOS_FINGERPRINT_PATH'])
        if not target.is_absolute() or target.exists():
            raise ValueError('Fresh absolute worker fingerprint path required')
        from training.skillos_h20_state import atomic
        atomic(target, proof, new=True)
