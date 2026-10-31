"""Fixed read-only activity service client and shared bounded filter contract."""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, Literal

from fastapi import Query
from .connectors import NoRedirect


class ActivityUnavailable(Exception):
    pass


def activity_query(limit: Annotated[int, Query(ge=1, le=200)] = 50,
                   before: Annotated[int | None, Query(ge=1, le=2**53)] = None,
                   snapshot: Annotated[int | None, Query(ge=0, le=2**53)] = None,
                   actor_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
                   outcome: Literal['allowed','denied','failed'] | None = None):
    return {key:value for key,value in locals().items() if value is not None}


class ActivityClient:
    def __init__(self, url, token):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {'','/'} or parsed.query or parsed.fragment or len(token)<32
                or (parsed.scheme=='http' and parsed.hostname not in {'127.0.0.1','localhost','awasi-ingest'})):
            raise ValueError('Invalid activity service configuration')
        self.url, self.token = url.rstrip('/'), token

    def get(self, path, company, params=None):
        query = '?'+urllib.parse.urlencode(params) if params else ''
        request = urllib.request.Request(self.url+path+query,headers={'Authorization':'Bearer '+self.token})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=5) as response:
                body = response.read(2*1024*1024+1)
            if len(body)>2*1024*1024:
                raise ActivityUnavailable('oversized')
            payload = json.loads(body)
            if not isinstance(payload,dict) or payload.get('company_id') != company:
                raise ActivityUnavailable('scope_mismatch')
            return payload
        except urllib.error.HTTPError as error:
            error.close()
            raise ActivityUnavailable('unavailable') from None
        except (OSError,urllib.error.URLError,ValueError,TypeError):
            raise ActivityUnavailable('unavailable') from None
