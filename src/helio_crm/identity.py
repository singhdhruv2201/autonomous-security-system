"""A bounded authoritative directory projection, independent of graph availability."""
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class EmployeeStatus(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: Annotated[str, Field(min_length=1,max_length=200)]
    status: Literal['active','inactive']


class IdentityProjection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    company_id: Literal['helio']
    source: Literal['directory']
    version: Annotated[int, Field(ge=1,le=2**53)]
    observed_at: Annotated[float, Field(ge=0,allow_inf_nan=False)]
    expires_at: Annotated[float, Field(ge=0,allow_inf_nan=False)]
    complete: Literal[True]
    employees: Annotated[list[EmployeeStatus], Field(min_length=1,max_length=10000)]


def publish(db, payload, now):
    from .store import CRMError
    try:
        data = IdentityProjection.model_validate(payload)
    except ValueError:
        raise CRMError(422,'Invalid identity projection') from None
    identities = {e.id:e.status for e in data.employees}
    known = {r[0] for r in db.execute('SELECT id FROM employees')}
    if (len(identities)!=len(data.employees) or set(identities)!=known or
            not data.observed_at <= now < data.expires_at <= data.observed_at+120):
        raise CRMError(422,'Invalid identity projection')
    previous = db.execute('SELECT version,observed_at FROM identity_projection WHERE id=1').fetchone()
    if previous and (data.version <= previous['version'] or data.observed_at < previous['observed_at']):
        raise CRMError(409,'Identity projection is stale or replayed')
    db.execute('INSERT OR REPLACE INTO identity_projection VALUES (1,?,?,?,?)',
               (data.version,data.observed_at,data.expires_at,json.dumps(identities,sort_keys=True)))
    return {'version':data.version,'observed_at':data.observed_at,'expires_at':data.expires_at}
