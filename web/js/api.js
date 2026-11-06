// The page's one way into Python: fetches to the window's own origin, answered inside the window by main.py
// (serve_files, answer_call), the same way the page's files are: no server and no network.
// Python answers {ok, data} or {ok: false, error} as JSON (app/bridge.py, App.call).
// A call that breaks on the way (not a refusal from Python, which the user is shown) goes to data\app.log with
// its method, path and the step that failed; one with no answer after SLOW_MS is logged too (a Save As or Open
// dialog left open also counts).
const SLOW_MS = 30000;

async function call(method, path, body = null) {
  const where = `${method} ${path}`;
  const fail = (step, error) => {
    const text = `api ${where}: ${step}: ${error?.message || error}`;
    window.reportPageError?.(text);
    return new Error(`앱 내부 오류가 났습니다: ${text}`);
  };
  const slow = setTimeout(() => window.reportPageError?.(`api ${where}: no answer after ${SLOW_MS / 1000} s`), SLOW_MS);
  let response;
  try {
    response = await fetch(path, method === 'POST'
      ? { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) }
      : { method });
  } catch (error) {
    throw fail('not sent', error);
  } finally {
    clearTimeout(slow);
  }
  if (!response.ok) throw fail('answered', `HTTP ${response.status}`);
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw fail('unreadable answer', error);
  }
  if (!payload?.ok) throw new Error(payload?.error || '요청에 실패했습니다.');
  return payload.data;
}

export const get = (path) => call('GET', path);
export const post = (path, body = {}) => call('POST', path, body);

// Settings typed but not saved yet: Python keeps the newest set and saves it if the window closes first.
let draftSeq = 0;
export function keepDraft(changes) {
  post('/api/settings/draft', { seq: ++draftSeq, changes }).catch(() => {});
}
