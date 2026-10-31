"""Explicit lifecycle for the existing single static credential per principal."""
import math

UNSET = object()


def state(db, principal_id):
    row = db.execute('SELECT * FROM access_state WHERE principal_id=?',(principal_id,)).fetchone()
    return dict(row) if row else dict(principal_id=principal_id,principal_status='active',
                                     credential_status='active',expires_at=None,version=1)


def change(db, principal_id, expected_version, now, *, principal_status=None, credential_status=None, expires_at=UNSET):
    from .store import CRMError
    if not db.execute('SELECT 1 FROM principals WHERE id=?',(principal_id,)).fetchone():
        raise CRMError(404,'Principal not found')
    old = state(db,principal_id)
    if old['version'] != expected_version:
        raise CRMError(409,'Access state changed')
    if (principal_status not in {None,'active','disabled'} or credential_status not in {None,'active','revoked'}
            or (principal_status is None and credential_status is None and expires_at is UNSET)
            or (expires_at is not UNSET and expires_at is not None and
                (type(expires_at) not in (int,float) or not math.isfinite(expires_at) or expires_at < 0))):
        raise CRMError(422,'Invalid access transition')
    # Revocation is terminal for this static credential; enabling a principal does not revive it.
    if old['credential_status']=='revoked' and credential_status=='active':
        raise CRMError(409,'Revoked credentials cannot be restored')
    new = {**old,'version':old['version']+1}
    if principal_status is not None: new['principal_status']=principal_status
    if credential_status is not None: new['credential_status']=credential_status
    if expires_at is not UNSET: new['expires_at']=expires_at
    db.execute('INSERT OR REPLACE INTO access_state VALUES (?,?,?,?,?)',
               tuple(new[k] for k in ('principal_id','principal_status','credential_status','expires_at','version')))
    return new
