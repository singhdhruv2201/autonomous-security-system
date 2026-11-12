import pytest


@pytest.mark.parametrize('text,kind',[
    ('AKIA'+'A'*16,'aws_key'), ('xoxb-'+'a'*24,'slack_token'),
    ('sk_live_'+'z'*24,'stripe_key'), ('Bearer '+'z'*32,'bearer_token'),
    ('refresh_token="'+'b'*30+'"','refresh_token'),('SSN 123-45-6789','pii_ssn')])
def test_explicit_detectors_return_only_redacted_categories(text,kind):
    from helio_crm.scanner import scan
    result=scan(text)
    assert result=={'status':'matched','detectors':[kind]}
    assert text not in str(result)


@pytest.mark.parametrize('text',['Renewal scheduled','AKIA example','sk_live_short','SSN 000-00-0000','Bearer placeholder','contact@example.test'])
def test_benign_examples_are_not_secret_matches(text):
    from helio_crm.scanner import scan
    assert scan(text)['status']=='passed'


def test_oversize_timeout_and_unicode_normalization():
    from helio_crm.scanner import scan
    assert scan('x'*101,max_bytes=100)['status']=='unknown'
    ticks=iter([0,1])
    assert scan('ordinary',clock=lambda:next(ticks),timeout_ms=1)['status']=='unknown'
    assert scan('ＡＫＩＡ'+'Ａ'*16,clock=lambda:0)['detectors']==['aws_key']
    assert scan('a'*65536,clock=lambda:0)['status']=='passed'
