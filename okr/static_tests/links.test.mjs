// Link-scheme filtering: evidence and other links render only for http(s);
// file evidence opens only through the authorised /api download path.
import test from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

const UNSAFE = [
  'javascript:alert(1)',
  ' JavaScript:alert(1)',
  'java\tscript:alert(1)',
  'data:text/html,<script>alert(1)</script>',
  'vbscript:msgbox(1)',
  'file:///etc/passwd',
  '//evil.example/x',
  '/relative/path',
  'mailto:a@b.c',
  '',
  null,
];

test('safeHttpUrl (api.js and views.js) accepts only absolute http(s) URLs', () => {
  const { JoyOKR } = load(['domain.js', 'api.js', 'store.js', 'views.js']);
  for (const check of [JoyOKR.api.safeHttpUrl, JoyOKR.views.helpers.safeHttpUrl]) {
    for (const value of UNSAFE) assert.equal(check(value), '', String(value));
    assert.equal(check('https://docs.example.com/a?b=1'), 'https://docs.example.com/a?b=1');
    assert.equal(check('HTTP://Example.com'), 'http://example.com/');
    assert.equal(check('https://x.example/"onmouseover="alert(1)'), 'https://x.example/%22onmouseover=%22alert(1)');
  }
});

test('evidence download URLs are "/api" + the authorised download_path only', () => {
  const { JoyOKR } = load(['api.js']);
  const { evidenceDownloadUrl } = JoyOKR.api;
  assert.equal(evidenceDownloadUrl('/okr/evidence/12/download/'), '/api/okr/evidence/12/download/');
  for (const path of [
    '/media/okr/evidence/x.png', '/okr/evidence/12/download/../../../admin/', 'https://evil.example/okr/evidence/1/download/',
    '//evil.example/okr/evidence/1/download/', '/okr/evidence/0/download/', '/okr/evidence/abc/download/', 'okr/evidence/1/download/', '',
  ]) {
    assert.equal(evidenceDownloadUrl(path), '', path);
  }
});

test('views safeDownloadPath only allows same-origin download paths', () => {
  const { JoyOKR } = load(['domain.js', 'api.js', 'store.js', 'views.js']);
  const { safeDownloadPath } = JoyOKR.views.helpers;
  assert.equal(safeDownloadPath('/api/okr/evidence/3/download/'), '/api/okr/evidence/3/download/');
  for (const path of ['//evil.example/a/download/', 'https://evil.example/a/download/', '/api/okr/../x/download/', '/api/okr/evidence/3/', 'javascript:alert(1)//download/']) {
    assert.equal(safeDownloadPath(path), '', path);
  }
});
