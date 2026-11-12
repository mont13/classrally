/* Loaded only when the teacher-side Guide catalog is opened. No polling. */
export async function teacherToken() {
  let token = '';
  try { token = localStorage.getItem('quiz_user_token') || ''; } catch (_) { return ''; }
  if (!token) return '';
  const response = await fetch('/api/auth/profile', {headers: {Authorization: 'UserToken ' + token}});
  const user = response.ok ? await response.json() : null;
  return user?.role === 'teacher' ? token : '';
}

export async function ask(question, language, token, signal) {
  const response = await fetch('/api/teacher/help', {
    method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'UserToken ' + token},
    body: JSON.stringify({question, language}), signal
  });
  return {ok: response.ok, status: response.status, data: await response.json()};
}
