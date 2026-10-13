"""Private, reversible research choices; no strategy thresholds live here."""
from __future__ import annotations

import hashlib
import json

from research_store import load_config

CONTEXT_MODULES = frozenset(('hot', 'sector-radar', 'night-radar'))


class StrategyPausedError(RuntimeError):
    """A deliberate pause, not a market-data failure or an empty universe."""


def load_policy():
    try:
        value = load_config('research_policy.json')
    except FileNotFoundError:
        return {'schema_version': 1, 'strategies': {}, 'configured': False, 'valid': True}
    except (OSError, ValueError, TypeError):
        return {'schema_version': 1, 'strategies': {}, 'configured': True, 'valid': False}
    valid = (isinstance(value, dict) and value.get('schema_version') == 1
             and isinstance(value.get('strategies'), dict)
             and all(isinstance(key, str) and isinstance(item, dict)
                     and item.get('state') in ('active', 'paused')
                     and ('reason' not in item or isinstance(item['reason'], str))
                     for key, item in value.get('strategies', {}).items()))
    if not valid:
        return {'schema_version': 1, 'strategies': {}, 'configured': True, 'valid': False}
    return dict(value, configured=True, valid=True)


def policy_status(strategy, *, policy=None):
    policy = load_policy() if policy is None else policy
    if strategy in CONTEXT_MODULES:
        return {'state': 'active', 'reason': '行情背景工具；不独立授予选股资格。',
                'configured': policy['configured']}
    if not policy['valid']:
        return {'state': 'paused', 'reason': '研究选择配置不可用；暂停选股，保留历史资料。',
                'configured': True}
    item = policy['strategies'].get(strategy, {})
    state = item.get('state', 'active')
    return {'state': state,
            'reason': item.get('reason') or ('已暂停选股运行与参考；历史资料保留。' if state == 'paused' else ''),
            'configured': policy['configured']}


def is_paused(strategy):
    return policy_status(strategy)['state'] == 'paused'


def selection_enabled(strategy):
    return not is_paused(strategy)


def require_enabled(strategy):
    state = policy_status(strategy)
    if state['state'] == 'paused':
        raise StrategyPausedError(state['reason'])


def policy_fingerprint():
    policy = load_policy()
    if not policy['configured']:
        return None
    content = {'schema_version': policy['schema_version'], 'valid': policy['valid'],
               'strategies': policy['strategies']}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def paused_outcome(strategy):
    state = policy_status(strategy)
    return {'module': strategy, 'status': 'paused', 'changed': False, 'summary': state['reason']}
