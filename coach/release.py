"""Immutable, reproducible local release files; preparing them never uploads."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import zipfile
import io
from coach.config import ROOT
from coach.bundle import build
from coach import series

STORE=ROOT/'state/releases'


def prepare(game,state,draft):
    from coach.loop import draft_id
    if not draft.get('ready'):raise ValueError('Only validated drafts can be packaged')
    if draft_id(draft)!=draft.get('id'):raise ValueError('Draft source/book changed; validate a fresh snapshot before packaging')
    series.stamp(state,draft)
    epoch=(state.get('counted_series') or {}).get('id','local')
    number=draft.get('series_version') or draft.get('revision') or 1
    identity=draft['id'];prefix=f'{game}-v{number}'
    blob=build(game,draft['strategy'],draft.get('code'))
    folder=STORE/epoch/f'{prefix}-{identity[:12]}';folder.mkdir(parents=True,exist_ok=True)
    archive=folder/f'{prefix}.zip'
    if archive.exists() and archive.read_bytes()!=blob:
        raise ValueError('Reviewed archive already exists with different bytes; use a new validated snapshot')
    archive.write_bytes(blob)
    files={}
    with zipfile.ZipFile(io.BytesIO(blob)) as bundle:
        for name in ('agent.py','strategy.py'):
            data=bundle.read(name);target=folder/f'{prefix}-{name}'
            if target.exists() and target.read_bytes()!=data:raise ValueError('Reviewed source file changed; inspect before rebuilding')
            target.write_bytes(data)
            files[target.name]=hashlib.sha256(data).hexdigest()
    files[archive.name]=hashlib.sha256(blob).hexdigest()
    metadata={'prepared_at':datetime.now(timezone.utc).isoformat(),'game':game,'series_id':epoch,
        'local_version':number,'build_revision':draft.get('revision'),'draft_id':identity,
        'name':draft['strategy'].get('name'),'validation_stage':draft.get('validation_stage'),
        'validation_status':(draft.get('behavior_assessment') or {}).get('status'),
        'report_sha256':hashlib.sha256((draft.get('report') or '').encode()).hexdigest(),
        'bundle_sha256':files[archive.name],'files':files,
        'note':'Local file numbering is separate from AGX-assigned version IDs. Preparing this archive does not submit it.'}
    manifest=folder/'manifest.json';manifest.write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
    return {'draft_id':identity,'bundle_sha256':metadata['bundle_sha256'],'archive':str(archive),
        'manifest':str(manifest),'local_version':number,'prepared_at':metadata['prepared_at']}
