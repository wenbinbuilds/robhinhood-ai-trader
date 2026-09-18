"""Immutable local model versions; promotion is explicit and never live."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
import json
import subprocess


VALID_STATES = {'TRAINED', 'VALIDATED', 'SHADOW_COMPARE', 'SHADOW_APPROVED'}


@dataclass(frozen=True)
class ModelMetadata:
    model_id: str
    algorithm: str
    created_at: str
    training_start: str
    training_end: str
    feature_schema_version: str
    reward_version: str
    normalization_version: str
    hyperparameters: dict[str, Any]
    random_seed: int
    git_commit: str | None
    training_metrics: dict[str, Any]
    registry_state: str = 'TRAINED'


class ModelRegistry:
    def __init__(self, root): self.root = Path(root)

    def create_metadata(self, **fields):
        return ModelMetadata(model_id=fields.pop('model_id', datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]),
                             created_at=datetime.now(timezone.utc).isoformat(), git_commit=_git_commit(), **fields)

    def save(self, policy, metadata, normalizer):
        if metadata.registry_state not in VALID_STATES: raise ValueError('invalid registry state')
        directory = self.root / metadata.model_id
        if directory.exists(): raise FileExistsError('model IDs are immutable')
        directory.mkdir(parents=True)
        policy.save(directory/'model')
        (directory/'metadata.json').write_text(json.dumps(asdict(metadata), indent=2, sort_keys=True)+'\n')
        (directory/'normalization.json').write_text(json.dumps(normalizer.to_dict(), indent=2, sort_keys=True)+'\n')
        return directory

    def load_metadata(self, model_id):
        return ModelMetadata(**json.loads((self.root/model_id/'metadata.json').read_text()))

    def status(self):
        if not self.root.exists(): return []
        return [json.loads(p.read_text()) for p in sorted(self.root.glob('*/metadata.json'))]

    def promote(self, model_id, state):
        if state not in VALID_STATES or state == 'TRAINED': raise ValueError('invalid explicit promotion')
        path = self.root/model_id/'metadata.json'
        data = json.loads(path.read_text())
        current = data['registry_state']
        order = ['TRAINED', 'VALIDATED', 'SHADOW_COMPARE', 'SHADOW_APPROVED']
        if order.index(state) != order.index(current)+1: raise ValueError('promotion must be one explicit stage')
        data['registry_state'] = state
        path.write_text(json.dumps(data, indent=2, sort_keys=True)+'\n')


def _git_commit():
    try: return subprocess.run(['git','rev-parse','HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    except Exception: return None
