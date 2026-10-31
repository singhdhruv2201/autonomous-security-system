"""Small explicit detector set. Results contain categories, never matched content."""
import re
import time
import unicodedata

PATTERNS = {
    'aws_key': re.compile(r'(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])'),
    'slack_token': re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,200}\b'),
    'stripe_key': re.compile(r'\bsk_live_[A-Za-z0-9]{20,200}\b'),
    'bearer_token': re.compile(r'\bBearer\s{1,8}[A-Za-z0-9._~+/-]{20,512}={0,2}',re.I),
    'refresh_token': re.compile(r'''\brefresh_token["'\\\s:=]{1,12}[A-Za-z0-9._~+/-]{20,512}''',re.I),
    'pii_ssn': re.compile(r'(?<!\d)(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)'),
}


def scan(text, *, max_bytes=65536, timeout_ms=10, clock=time.perf_counter):
    started=clock()
    if len(text)>max_bytes or len(text.encode('utf-8'))>max_bytes:
        return {'status':'unknown','detectors':[]}
    normalized=unicodedata.normalize('NFKC',text)
    if len(normalized.encode('utf-8'))>max_bytes:
        return {'status':'unknown','detectors':[]}
    matches=[]
    for name,pattern in PATTERNS.items():
        if (clock()-started)*1000 > timeout_ms:
            return {'status':'unknown','detectors':[]}
        if pattern.search(normalized): matches.append(name)
    if (clock()-started)*1000 > timeout_ms:
        return {'status':'unknown','detectors':[]}
    return {'status':'matched' if matches else 'passed','detectors':matches}
