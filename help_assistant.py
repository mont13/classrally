"""Grounded, read-only UI help. Never send user text or application data to AI."""
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
MAX_QUESTION = 500
MAX_BODY = 4096
TIMEOUT = 30
LANGUAGES = {'cs': 'Czech', 'en': 'English', 'sk': 'Slovak', 'de': 'German', 'uk': 'Ukrainian'}


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.lower())
                   if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def chapters():
    return json.loads((ROOT / 'static/help.json').read_text(encoding='utf-8'))


def select_chapters(question, *, portal=False):
    """At most three chapters; fixed aliases only, never query fragments."""
    text = normalize(question)
    # An explicit portal question must not turn into generic PIN/login advice
    # when that integration is disabled on this instance.
    if not portal and re.search(r'(?<!\w)(?:portal\w*|sso)(?!\w)', text):
        return []
    ranked = []
    for chapter in chapters():
        if chapter['id'] == 'portal' and not portal:
            continue
        matches = [alias for alias in chapter['keywords']
                   if re.search(r'(?<!\w)' + re.escape(normalize(alias)) + r'\w*', text)]
        if matches:
            ranked.append((len(matches), chapter, matches))
    ranked.sort(key=lambda row: -row[0])
    return [(chapter, matches) for _, chapter, matches in ranked[:3]]


@lru_cache(maxsize=5)
def translations(language):
    return json.loads((ROOT / f'static/i18n/{language}.json').read_text(encoding='utf-8'))


def localized_text(text, language):
    def replace(match):
        value = translations(language)
        for key in match[1].split('.'):
            value = value[key]
        return '„' + value + '“'
    return re.sub(r'\{\{([\w.]+)\}\}', replace, text)


def answer(selected, language, config):
    """Use exactly the generator's configured transport; no override or retry."""
    language = language if language in LANGUAGES else 'cs'
    context = '\n\n'.join(localized_text(c['text'], language) for c, _ in selected)
    topics = ', '.join(dict.fromkeys(alias for _, matches in selected for alias in matches))
    system = (
        'You are the ClassRally UI helper. Answer ONLY about operating ClassRally using the supplied '
        'verified reference. Never invent buttons, capabilities or facts. If the reference does not '
        'answer the topic, explicitly say you do not know and recommend Buggy (if available) or the '
        'Guide catalog. You cannot perform actions for the teacher. No tools, no account or student '
        'data are available. Give at most 4 short numbered steps, plain text, no HTML. '
        f'Answer in {LANGUAGES[language]}. Preserve the exact quoted UI labels, even if in another language. '
        'For an ambiguous topic explain the relevant alternatives or ask for clarification.\n\n'
        'VERIFIED REFERENCE:\n' + context
    )
    root = config['base_url'].rstrip('/')
    if root.endswith('/v1'):
        root = root[:-3]
    headers = {'Content-Type': 'application/json'}
    if config.get('api_key'):
        headers['Authorization'] = 'Bearer ' + config['api_key']
    payload = {'model': config['model'], 'messages': [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': 'Explain the UI procedure for these recognized topics: ' + topics}],
        'temperature': 0.2, 'max_tokens': 2048}
    request = Request(root + '/v1/chat/completions', data=json.dumps(payload).encode('utf-8'),
                      headers=headers, method='POST')
    with urlopen(request, timeout=TIMEOUT) as response:
        result = json.loads(response.read(65537).decode('utf-8'))
    content = result['choices'][0]['message']['content']
    if not isinstance(content, str):
        raise ValueError('invalid response')
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.S).strip()
    if not content or len(content) > 4000:
        raise ValueError('invalid response')
    return content
