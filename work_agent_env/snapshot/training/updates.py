"""On-policy update/install barrier with durable uncertainty and no implicit optimizer replay."""
import hashlib
import hmac
import json
import re
from pathlib import Path

from integrations.benchmark_tasks.contracts import digest
from integrations.benchmark_tasks.store import save
from integrations.orchestration.journal import Journal
from training.markdown_contract import validate_policy
from training.skill_contract import TrainingRejected, sign_capture


class UpdateGate:
    def __init__(self, directory, *, run_id, capture_key):
        self.root = Path(directory)
        self.journal = Journal(self.root)
        self.run_id, self.capture_key = run_id, capture_key

    def ready(self, batch):
        body = {key: value for key, value in batch.items() if key not in ("id", "signature")}
        if (batch.get("id") != digest(body) or batch.get("training_admitted") is not True
                or batch.get("mode") != "training" or not isinstance(batch.get("signature"), str)
                or not hmac.compare_digest(sign_capture(body, self.capture_key), batch["signature"])):
            raise TrainingRejected("Only an authenticated admitted batch can authorize an update")
        policy = validate_policy(batch["policy"])
        if policy["run_id"] != self.run_id:
            raise TrainingRejected("Update run changed")
        current = self.current_policy(policy["step"], tokenizer_revision=policy["tokenizer_revision"])
        if current != policy:
            raise TrainingRejected("Stale policy batch cannot enter optimization")
        job = self.journal.claim({"kind": "parameter_update", "run_id": self.run_id, "step": policy["step"]})
        save(self.root / "sealed" / (batch["id"] + ".json"), batch)
        self.journal.transition(job, expected={"claimed"}, state="ready_for_native_optimizer",
            payload={"batch_id": batch["id"], "policy": policy})
        return job

    def begin_install(self, step):
        if type(step) is not int or step < 0:
            raise TrainingRejected("Missing native update step")
        identity = {"kind": "parameter_update", "run_id": self.run_id, "step": step}
        if step == 0:
            job = self.journal.claim(identity)
            expected = {"claimed"}
            payload = {"batch_id": None, "initial_install_only": True}
        else:
            job = digest(identity)
            row = self.journal.get(job)
            payload = row["payload"]
            expected = {"ready_for_native_optimizer"}
        self.journal.transition(job, expected=expected, state="install_sent", payload=payload)
        return job, payload

    def installed(self, job, *, step, checkpoint_sha256, checkpoint_ref, batch_id):
        row = self.journal.get(job)
        if (row["identity"] != {"kind": "parameter_update", "run_id": self.run_id, "step": step}
                or row["payload"].get("batch_id") != batch_id):
            raise TrainingRejected("Installation receipt belongs to a different update")
        if not isinstance(checkpoint_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", checkpoint_sha256):
            raise TrainingRejected("Installed policy needs an immutable checkpoint identity")
        record = {"run_id": self.run_id, "step": step, "checkpoint_sha256": checkpoint_sha256,
                  "checkpoint_ref": str(checkpoint_ref), "batch_id": batch_id,
                  "initial_install_only": step == 0}
        self.journal.transition(job, expected={"install_sent"}, state="installed", payload=record)
        save(self.root / "installed_policy.json", record)
        save(self.root / "updates" / (str(step) + ".json"), record)
        return record

    def unknown(self, job, *, error):
        self.journal.transition(job, expected={"install_sent", "installed"}, state="unknown", payload={"error": error})

    def current_policy(self, rollout_step, *, tokenizer_revision):
        installed = json.loads((self.root / "installed_policy.json").read_text())
        if installed["run_id"] != self.run_id or installed["step"] != rollout_step - 1:
            raise TrainingRejected("Sampling must wait for the previous update's confirmed weight installation")
        job = self.journal.get(digest({"kind": "parameter_update", "run_id": self.run_id, "step": installed["step"]}))
        if job["state"] != "installed" or job["payload"] != installed:
            raise TrainingRejected("Policy file and durable installation journal disagree")
        body = {"run_id": self.run_id, "step": rollout_step,
                "model_revision": installed["checkpoint_sha256"], "tokenizer_revision": tokenizer_revision}
        return {**body, "policy_id": digest(body)}


def checkpoint_identity(directory):
    """Hash real actor/checkpoint bytes without unpickling optimizer or weight files."""
    root = Path(directory).resolve(strict=True)
    files = []
    for path in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            raise TrainingRejected("Checkpoint symlinks are not accepted")
        if path.is_file():
            before = path.stat()
            with path.open("rb") as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise TrainingRejected("Checkpoint changed while hashing")
            files.append({"path": str(path.relative_to(root)), "sha256": sha, "bytes": before.st_size})
    if not any(Path(row["path"]).suffix in (".safetensors", ".pt", ".bin") for row in files):
        raise TrainingRejected("No actual checkpoint weights found")
    return digest(files), files
